#!/usr/bin/env python3
"""
term_idf.py - Corpus-wide term frequency index for IDF-weighted scoring.

Computes document frequency (DF) for terms across the test corpus, enabling
IDF (Inverse Document Frequency) weighted scoring. Terms that appear in
fewer tests are more discriminating and receive higher weight.

IDF formula: log2(total_tests / tests_containing_term)
  - "agent" in 7756/10034 tests -> IDF = 0.37 (nearly useless)
  - "shift gap" in 285/10034 tests -> IDF = 5.14 (highly specific)

Public API:

    build_idf_index(corpus_path, output_path=None) -> dict
        Builds the IDF index from a test corpus JSON file.
        Optionally saves to output_path for caching.

    load_idf_index(index_path) -> dict
        Loads a pre-built IDF index from disk.

    get_idf(term, idf_index) -> float
        Returns the IDF weight for a given term.

    score_phrases_against_test(phrases, test, idf_index) -> float
        Scores a set of phrases against a single test using IDF weighting.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# PORTABILITY FIX #2: Language-aware Snowball stemmer
# ---------------------------------------------------------------------------
# The Snowball stemmer supports 15+ natural languages. The previous
# implementation hardcoded 'english', which silently broke any non-English
# test corpus (the stemmer would still run, but produce nonsense stems).
#
# We now read RIA_LANGUAGE from the environment and map programming
# language codes to the appropriate Snowball natural-language algorithm.
# When the language code itself is a supported natural language (e.g.
# 'spanish', 'french'), it is used directly. Unsupported languages fall
# back to English so callers never see a hard failure.
# ---------------------------------------------------------------------------

# Language-to-stemmer mapping (Snowball stemmer supports 15+ languages).
# Programming languages map to English (their identifiers are English).
# Natural-language codes map to themselves.
_STEMMER_LANGUAGE_MAP = {
    'java': 'english',
    'kotlin': 'english',
    'typescript': 'english',
    'javascript': 'english',
    'python': 'english',
    'csharp': 'english',
    'spanish': 'spanish',
    'french': 'french',
    'german': 'german',
    'italian': 'italian',
    'portuguese': 'portuguese',
    'dutch': 'dutch',
    'russian': 'russian',
    'swedish': 'swedish',
    'norwegian': 'norwegian',
    'danish': 'danish',
    'finnish': 'finnish',
}

try:
    import snowballstemmer as _sb
    # Detect language from environment or default to English.
    _ria_language = os.getenv('RIA_LANGUAGE', 'java').lower()
    _stemmer_language = _STEMMER_LANGUAGE_MAP.get(_ria_language, 'english')
    try:
        _stemmer = _sb.stemmer(_stemmer_language)
        print(f"[term_idf] Using {_stemmer_language} stemmer for language: {_ria_language}")
    except (ValueError, KeyError):
        # Fallback to English if language not supported by snowballstemmer.
        _stemmer = _sb.stemmer('english')
        print(f"[term_idf] Language {_stemmer_language} not supported, using English stemmer")

    def _stem_word(w: str) -> str:
        return _stemmer.stemWord(w)
except ImportError:
    _stemmer = None
    def _stem_word(w: str) -> str:          # no-op fallback
        return w


# ---------------------------------------------------------------------------
# Layer 1 — Character normalisation (hyphens, punctuation, case)
# ---------------------------------------------------------------------------
_PUNCT_RE = re.compile(r"[^a-z0-9\s]")      # strip everything except letters, digits, spaces
_MULTI_SPACE = re.compile(r'\s{2,}')

def _normalize_chars(text: str) -> str:
    """Lowercase + strip all punctuation (hyphens, brackets, quotes, etc.)."""
    t = text.lower()
    t = _PUNCT_RE.sub(' ', t)
    t = _MULTI_SPACE.sub(' ', t)
    return t.strip()


# ---------------------------------------------------------------------------
# Layer 2 — Morphological stemming
# ---------------------------------------------------------------------------
def _stem_phrase(phrase: str) -> str:
    """Stem every word in *phrase* using Snowball English stemmer."""
    return ' '.join(_stem_word(w) for w in phrase.split())


# ---------------------------------------------------------------------------
# Layer 3 — Prefix-aware word matching
# ---------------------------------------------------------------------------
_MIN_PREFIX = 3          # shortest prefix we accept

def _words_match_prefix(phrase_word: str, text_word: str) -> bool:
    """True if *phrase_word* is a prefix (≥ 3 chars) of *text_word* or
    *text_word* is a prefix of *phrase_word*. Bidirectional so that both
    'min'→'minimum' and 'minimum'→'min' work."""
    if phrase_word == text_word:
        return True
    if len(phrase_word) >= _MIN_PREFIX and text_word.startswith(phrase_word):
        return True
    if len(text_word) >= _MIN_PREFIX and phrase_word.startswith(text_word):
        return True
    return False


def _phrase_matches_prefix(phrase_words: List[str], text_words_set: Set[str],
                           text: str) -> Optional[int]:
    """Check if *phrase_words* appear consecutively in *text* allowing
    prefix-level matches per word.  Returns the character offset of the
    match start, or None."""
    # Build a list of candidate start positions: every position in the
    # text's word-list where the first phrase word prefix-matches.
    text_wlist = text.split()
    first = phrase_words[0]
    pw_len = len(phrase_words)
    for i in range(len(text_wlist) - pw_len + 1):
        if _words_match_prefix(first, text_wlist[i]):
            if all(_words_match_prefix(phrase_words[j], text_wlist[i + j])
                   for j in range(1, pw_len)):
                # Compute character offset for overlap tracking
                offset = len(' '.join(text_wlist[:i])) + (1 if i > 0 else 0)
                return offset
    return None


# ---------------------------------------------------------------------------
# Text extraction from test records
# ---------------------------------------------------------------------------

def _extract_test_text(test: Dict) -> str:
    """Extract all searchable text from a test record.
    Applies Layer-1 normalisation (lowercase + hyphen/punctuation → space)."""
    parts = [
        test.get('summary') or '',
        test.get('description') or '',
    ]
    for step in test.get('steps', []):
        parts.append(step.get('action') or '')
        parts.append(step.get('data') or '')
        parts.append(step.get('result') or '')
    return _normalize_chars(' '.join(parts))


# ---------------------------------------------------------------------------
# IDF Index Building
# ---------------------------------------------------------------------------

# Fix #9: A small, conservative English stopword list applied consistently
# across every tokenization path in term_idf. Without this, 3-character
# stopwords could leak into the IDF index via the stemmer (e.g. "ands" -> "and"
# is added as a stemmed unigram with len(sw) >= 3, producing the audit-flagged
# anomaly where "and" had idf=9.39 with df=15). We block stopwords from being
# emitted as unigrams AND from anchoring bigrams/trigrams whose other tokens
# would otherwise produce noise pairs like "the agent" or "agent and".
_TERM_IDF_STOPWORDS = frozenset([
    'and', 'the', 'for', 'with', 'that', 'this', 'these', 'those',
    'are', 'was', 'were', 'has', 'had', 'have', 'will', 'should', 'would',
    'could', 'must', 'may', 'might', 'can', 'when', 'where', 'while',
    'from', 'into', 'onto', 'about', 'above', 'below', 'over', 'under',
    'all', 'any', 'some', 'none', 'each', 'every', 'both', 'either',
    'neither', 'very', 'just', 'only', 'also', 'than', 'then',
    'per', 'via', 'between', 'after', 'before', 'during',
    'but', 'not', 'nor', 'yet', 'too', 'few', 'own',
    'its', 'her', 'his', 'their', 'our', 'them', 'they',
    'who', 'whom', 'whose', 'what', 'which', 'why', 'how',
])


def _is_stopword_term(term: str) -> bool:
    """Return True if `term` is a single-token stopword.

    Multi-word terms (bigrams/trigrams) are kept even if they CONTAIN a
    stopword because the surrounding domain words still make the phrase
    discriminating (e.g. "agent and trade" remains useful as a co-occurrence
    signal). Only pure single-token stopwords are filtered.
    """
    return ' ' not in term and term in _TERM_IDF_STOPWORDS


def _tokenize_for_df(text: str) -> Set[str]:
    """
    Tokenize text into terms for document frequency counting.
    Returns a SET of unique terms found in this document (test).

    Generates both raw and stemmed variants for:
      - Unigrams (single words, 4+ chars, non-stopword)
      - Bigrams (adjacent word pairs)
      - Trigrams (adjacent word triples)

    Stemmed variants ensure that 'validating' and 'validate' map to the
    same IDF bucket.  Raw variants are kept so exact-match lookups still
    work.

    Fix #9: Stopword filter applied uniformly across raw and stemmed
    unigram emission paths so a token like "ands" cannot smuggle "and"
    into the index after stemming. This restores the invariant that no
    pure English stopword has an IDF score in the index.
    """
    # Text is already normalised (lowercase, hyphens→spaces)
    words = re.findall(r'[a-z]{3,}', text)

    terms = set()

    # Unigrams (4+ chars to be meaningful, and not a stopword)
    for w in words:
        if len(w) >= 4 and w not in _TERM_IDF_STOPWORDS:
            terms.add(w)
            sw = _stem_word(w)
            # Fix #9: Apply the stopword filter to the stemmed variant too.
            # Without this guard, 'ands' -> 'and' would be re-introduced as
            # a unigram with len(sw)=3 even though we excluded the stopword
            # form on the raw side.
            if (sw != w and len(sw) >= 3
                    and sw not in _TERM_IDF_STOPWORDS):
                terms.add(sw)

    # Bigrams
    for i in range(len(words) - 1):
        if len(words[i]) >= 3 and len(words[i + 1]) >= 3:
            raw = f"{words[i]} {words[i + 1]}"
            terms.add(raw)
            stemmed = f"{_stem_word(words[i])} {_stem_word(words[i + 1])}"
            if stemmed != raw:
                terms.add(stemmed)

    # Trigrams
    for i in range(len(words) - 2):
        if len(words[i]) >= 3 and len(words[i + 1]) >= 3 and len(words[i + 2]) >= 3:
            raw = f"{words[i]} {words[i + 1]} {words[i + 2]}"
            terms.add(raw)
            stemmed = f"{_stem_word(words[i])} {_stem_word(words[i + 1])} {_stem_word(words[i + 2])}"
            if stemmed != raw:
                terms.add(stemmed)

    return terms


# ---------------------------------------------------------------------------
# Document length computation for BM25
# ---------------------------------------------------------------------------

def _doc_length(text: str) -> int:
    """
    Document length in tokens used for BM25 length normalisation.

    Counts only the unigram tokens (3+ chars) we actually index.  Using a
    consistent definition for both DF tokenisation and length means the
    BM25 length-norm term `dl/avgdl` is on the same scale as the term
    occurrences it normalises.
    """
    if not text:
        return 0
    return sum(1 for _ in re.finditer(r'[a-z]{3,}', text))


# ---------------------------------------------------------------------------
# Corpus-size migration warning (UPGRADE 4)
# ---------------------------------------------------------------------------
# JSON-backed IDF index works well up to ~50K tests (sub-second load,
# < 200 MB on disk).  Beyond that, the linear scan in score_phrases_*
# starts to dominate end-to-end latency, and the JSON file size makes
# git/CI checkpoints painful.
#
# MIGRATION GUIDE (>50K tests):
#   1. Replace the JSON index with a Tantivy (Rust) or PyLucene index.
#      Both expose a Python binding; Tantivy is preferred for read-mostly
#      RIA workloads (single-writer, many-reader pattern).
#   2. Schema:
#         test_id     STORED|STRING
#         summary     TEXT
#         description TEXT
#         steps       TEXT
#      with a custom analyser that mirrors the snowball-stemmer +
#      char-normalisation pipeline used here (see _normalize_chars
#      and _stem_word).
#   3. Replace `build_idf_index()` with index-builder that emits a
#      Tantivy directory; replace `score_phrases_against_test()` with
#      a Tantivy `Query` over the stored fields.
#   4. The per-document `term_freq` and `length` payloads computed
#      below are NOT needed once Tantivy owns the postings: it computes
#      BM25 internally via `BM25Weight`.
#
# This module logs a WARNING when the corpus exceeds the threshold so
# operators see the migration prompt without surprise.
# ---------------------------------------------------------------------------

LARGE_CORPUS_WARN_THRESHOLD = 50_000


def _check_corpus_size_and_warn(total_documents: int) -> None:
    """Emit a single WARNING if the corpus is large enough to merit a
    migration to a real inverted index (Tantivy / Lucene).  Idempotent:
    safe to call from multiple paths.
    """
    if total_documents > LARGE_CORPUS_WARN_THRESHOLD:
        print(
            f"[term_idf] WARNING: corpus has {total_documents} documents "
            f"(> {LARGE_CORPUS_WARN_THRESHOLD}). The JSON-backed IDF index "
            f"may be slow to load and score. Consider migrating to a "
            f"Tantivy / PyLucene inverted index — see the MIGRATION GUIDE "
            f"comment block above _check_corpus_size_and_warn() in this "
            f"file for step-by-step instructions."
        )


def build_idf_index(corpus_path: str, output_path: Optional[str] = None) -> Dict:
    """
    Build an IDF index from the test corpus.

    Args:
        corpus_path: Path to the test corpus JSON (list of test dicts).
        output_path: Optional path to save the index as JSON.

    Returns:
        Dict with keys:
            - total_documents: int
            - document_frequency: dict of term -> count of tests containing it
            - idf: dict of term -> IDF score (log2(N/df))
            - avg_doc_length: float (UPGRADE 1: BM25 length-normalisation)
            - statistics: distribution statistics + bm25_params
            - version: str

    UPGRADE 1 (BM25):
      The index now also captures `avg_doc_length`, the corpus-wide mean
      number of indexed tokens per document.  This is the `avgdl` term in
      the BM25 length-normalisation factor `(1 - b + b * dl/avgdl)`.
      Per-document term frequencies and lengths are NOT persisted to keep
      the on-disk JSON small; instead, BM25 falls back to a doc-level
      approximation when run against an unindexed test (see
      `compute_bm25_score()`).
    """
    with open(corpus_path, 'r', encoding='utf-8') as f:
        corpus = json.load(f)

    total = len(corpus)
    if total == 0:
        return {
            'total_documents': 0,
            'document_frequency': {},
            'idf': {},
            'avg_doc_length': 0.0,
            'version': '3.0',
        }

    # Count document frequency for each term + accumulate document lengths
    df_counter: Counter = Counter()
    total_length = 0

    for test in corpus:
        text = _extract_test_text(test)
        terms = _tokenize_for_df(text)
        df_counter.update(terms)
        total_length += _doc_length(text)

    avg_doc_length = (total_length / total) if total > 0 else 0.0

    # Compute IDF for each term
    idf_scores = {}
    for term, df in df_counter.items():
        idf_scores[term] = math.log2(total / df)

    # Only keep terms with reasonable DF (appear in at least 2 tests)
    # and reasonable IDF (not too common, not too rare).
    # Fix #9: Final defensive sweep — drop any single-token stopword that
    # somehow survived tokenization (e.g. from a third-party tokenizer
    # injecting tokens or from a stale corpus). _tokenize_for_df already
    # blocks them at source; this is a second line of defense.
    filtered_df = {
        t: c for t, c in df_counter.items()
        if c >= 2 and not _is_stopword_term(t)
    }
    filtered_idf = {t: s for t, s in idf_scores.items() if t in filtered_df}

    # Compute distribution statistics for auto-threshold derivation.
    # These allow downstream stages to derive thresholds dynamically
    # instead of relying on hardcoded magic numbers.
    idf_values = sorted(filtered_idf.values())
    stats = {}
    if idf_values:
        n = len(idf_values)
        stats = {
            'count': n,
            'min': round(idf_values[0], 3),
            'max': round(idf_values[-1], 3),
            'mean': round(sum(idf_values) / n, 3),
            'p10': round(idf_values[int(n * 0.10)], 3),
            'p25': round(idf_values[int(n * 0.25)], 3),
            'p50': round(idf_values[int(n * 0.50)], 3),
            'p75': round(idf_values[int(n * 0.75)], 3),
            'p90': round(idf_values[int(n * 0.90)], 3),
            'p95': round(idf_values[int(n * 0.95)], 3),
            # Specificity threshold: IDF value at which a term appears in
            # fewer than ~3% of documents. Dynamically derived from corpus.
            'specificity_3pct': round(math.log2(total / max(1, int(total * 0.03))), 3),
            'specificity_5pct': round(math.log2(total / max(1, int(total * 0.05))), 3),
            'specificity_1pct': round(math.log2(total / max(1, int(total * 0.01))), 3),
            # UPGRADE 1: standard BM25 hyperparameters (Robertson & Zaragoza,
            # "The Probabilistic Relevance Framework: BM25 and Beyond"). k1
            # controls term-frequency saturation; b controls length norm.
            # Defaults match the Lucene/Tantivy/Elasticsearch consensus.
            'bm25_k1': 1.5,
            'bm25_b': 0.75,
            'avg_doc_length': round(avg_doc_length, 2),
        }

    result = {
        'total_documents': total,
        'document_frequency': filtered_df,
        'idf': filtered_idf,
        'avg_doc_length': round(avg_doc_length, 4),
        'statistics': stats,
        'version': '3.0',
    }

    # UPGRADE 4: warn when corpus exceeds the JSON-friendly threshold.
    _check_corpus_size_and_warn(total)

    if output_path:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result, f)
        print(f"[term_idf] Saved IDF index: {len(filtered_idf)} terms, "
              f"{total} documents -> {output_path}")
        print(f"[term_idf] avg_doc_length (BM25 avgdl): {avg_doc_length:.2f}")
        if stats:
            print(f"[term_idf] IDF statistics: P25={stats['p25']}, P50={stats['p50']}, "
                  f"P75={stats['p75']}, specificity_3pct={stats['specificity_3pct']}")

    return result


def load_idf_index(index_path: str) -> Dict:
    """Load a pre-built IDF index from disk."""
    with open(index_path, 'r', encoding='utf-8') as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# IDF-weighted scoring
# ---------------------------------------------------------------------------

def get_idf(term: str, idf_index: Dict) -> float:
    """
    Get the IDF weight for a term.

    Returns:
        IDF score (higher = more specific/rare).
        Default: median IDF from the index statistics (auto-derived).
        Minimum: P10 of IDF distribution (auto-derived floor).
        Falls back to 3.0/0.5 only if statistics are unavailable.

    Fix #9: Single-token English stopwords always return 0.0 regardless of
    whether they ended up in the index. This is a defensive belt-and-braces
    guard so that stale indexes built before the stopword filter was added
    cannot leak stopword IDF into scoring; new indexes never include them.
    """
    idf_map = idf_index.get('idf', {})
    stats = idf_index.get('statistics', {})

    term_lower = term.lower()
    if _is_stopword_term(term_lower):
        return 0.0

    # Auto-derived defaults from corpus statistics
    default_idf = stats.get('p50', 3.0)  # median = moderately specific
    min_idf = stats.get('p10', 0.5)      # P10 = floor for ultra-common terms

    score = idf_map.get(term_lower)
    if score is not None:
        return max(score, min_idf)
    # Unknown term: assume moderately specific (median)
    return default_idf


# ---------------------------------------------------------------------------
# UPGRADE 1: BM25 Okapi ranking
# ---------------------------------------------------------------------------
# BM25 (Best Matching 25) is the de-facto standard ranking function used by
# Lucene, Elasticsearch, Tantivy, Solr and every major search engine since
# Robertson & Walker (1994).  It addresses two flaws of raw IDF:
#
#   1. TERM-FREQUENCY SATURATION
#      Raw IDF gives the same weight whether a term appears 1 or 50 times.
#      BM25 saturates the contribution via `tf * (k1 + 1) / (tf + k1 * ...)`,
#      which converges to `k1 + 1` as tf → ∞.  k1=1.5 is the standard.
#
#   2. DOCUMENT-LENGTH NORMALISATION
#      A 50-line test step matching "shift" 5 times is NOT 5x more relevant
#      than a 5-line test step matching it once.  BM25 normalises tf by
#      `dl/avgdl` with strength `b`.  b=0.75 is the standard.
#
# Formula (Robertson 2009, eq. 2.3):
#
#   IDF_BM25(t) = ln( (N - df + 0.5) / (df + 0.5) + 1.0 )
#   Score(t,d)  = IDF_BM25(t) * tf * (k1 + 1) /
#                  (tf + k1 * (1 - b + b * dl/avgdl))
#
# We use natural log (math.log) for the IDF component to match Lucene's
# BM25Similarity exactly. The +1 inside the log prevents the negative IDF
# values that Robertson's original formula produces for very common terms
# (df > N/2) — Lucene calls this the `BM25 (with +1) IDF`.
#
# IMPORTANT: when the caller does not supply per-document term-frequency /
# length statistics (they are NOT persisted in the JSON index to keep it
# small), we fall back to tf=1 (term present) / tf=0 (term absent) and
# dl=avgdl (length-norm of 1.0).  In that mode BM25 collapses to a clipped
# version of the IDF score, which still benefits from the +1 / +0.5
# smoothing relative to raw `log2(N/df)`.
# ---------------------------------------------------------------------------

BM25_K1_DEFAULT = 1.5
BM25_B_DEFAULT = 0.75


def compute_bm25_score(term: str,
                       idf_index: Dict,
                       tf: float = 1.0,
                       doc_length: Optional[float] = None,
                       k1: Optional[float] = None,
                       b: Optional[float] = None) -> float:
    """
    BM25 Okapi score for a single (term, doc) pair.

    Args:
        term:         Query term (already normalised + optionally stemmed).
        idf_index:    Index dict produced by build_idf_index().
        tf:           Term frequency in the document. Default 1.0 (the
                      caller saw the term once in the doc).
        doc_length:   Document length in indexed tokens. If None, uses
                      avg_doc_length so the length-norm factor = 1.0.
        k1:           Term-saturation parameter. Default: index-stored
                      `bm25_k1` (1.5) → falls back to BM25_K1_DEFAULT.
        b:            Length-norm parameter. Default: index-stored
                      `bm25_b` (0.75) → falls back to BM25_B_DEFAULT.

    Returns:
        BM25 score (>= 0). Returns 0 when tf <= 0 (term absent).
    """
    if tf <= 0:
        return 0.0

    # Fix #9: belt-and-braces — never let a single-token stopword
    # contribute a non-zero BM25 weight, even if the index was built
    # before the stopword filter was added.
    if _is_stopword_term(term.lower()):
        return 0.0

    stats = idf_index.get('statistics', {}) or {}
    if k1 is None:
        k1 = float(stats.get('bm25_k1', BM25_K1_DEFAULT))
    if b is None:
        b = float(stats.get('bm25_b', BM25_B_DEFAULT))

    # Document-frequency lookup. If the term is not in the index, treat it
    # as appearing in 1 document (rare-term assumption) — the same default
    # used when get_idf() falls back to the median.
    df_map = idf_index.get('document_frequency', {}) or {}
    N = idf_index.get('total_documents', 0) or 0
    df = df_map.get(term.lower(), 0)

    if N <= 0:
        # Empty corpus — no statistical basis. Return 0 rather than NaN.
        return 0.0

    # IDF component with +1 smoothing (Lucene-style; never negative).
    # ln((N - df + 0.5) / (df + 0.5) + 1.0)
    idf = math.log((N - df + 0.5) / (df + 0.5) + 1.0)

    # Length-norm factor: when avgdl is unknown (legacy index without
    # `avg_doc_length`), we DISABLE length normalisation rather than
    # default avgdl to 1.0 — the latter would make every dl/avgdl >> 1
    # and crush every score by ~80x on a typical EEM test (dl ≈ 80).
    # Setting length_norm = 1.0 in that fallback collapses BM25 to a
    # pure TF-saturated IDF, which is still strictly better than raw IDF.
    raw_avgdl = (idf_index.get('avg_doc_length')
                 if idf_index.get('avg_doc_length') is not None
                 else stats.get('avg_doc_length'))
    try:
        avgdl = float(raw_avgdl) if raw_avgdl is not None else 0.0
    except (TypeError, ValueError):
        avgdl = 0.0

    if avgdl <= 0:
        # No avgdl available — disable length norm (factor = 1.0).
        length_norm = 1.0
    else:
        if doc_length is None:
            dl = avgdl
        else:
            dl = max(0.0, float(doc_length))
        length_norm = (1.0 - b) + b * (dl / avgdl)

    # BM25 TF saturation
    numerator = tf * (k1 + 1.0)
    denominator = tf + k1 * length_norm
    if denominator <= 0:
        return 0.0

    return idf * (numerator / denominator)


def get_bm25(term: str, idf_index: Dict,
             tf: float = 1.0,
             doc_length: Optional[float] = None) -> float:
    """
    BM25 wrapper that mirrors get_idf()'s signature for callers that just
    want a `weight per matched term`. Stemmed-fallback is the caller's
    responsibility (see score_phrases_against_test).
    """
    return compute_bm25_score(term, idf_index, tf=tf, doc_length=doc_length)


def score_phrases_against_test(phrases: List[str], test: Dict,
                                idf_index: Dict,
                                embedding_sim: Optional[float] = None,
                                use_bm25: bool = True,
                                ) -> Tuple[float, List[Dict]]:
    """
    Score diff-derived phrases against a single test using a 5-layer
    matching cascade.  Each phrase is tried at each layer in order; the
    first layer that matches wins.

    Layer precedence (highest confidence first):
        L1  Exact match            "daily shift gap" in text
        L2  Normalised match       hyphens/underscores removed, then exact
        L3  Stemmed match          Snowball-stem both sides, then exact
        L4  Prefix match           word-by-word prefix (min→minimum)
        L5  Semantic embedding     cosine similarity from pre-computed vectors

    The cascade ensures that a high-confidence exact hit is never downgraded
    by a fuzzy layer, while still catching abbreviation/morphology/synonym
    gaps when exact matching fails.

    UPGRADE 1 (BM25):
      The per-phrase weight is now BM25 (Okapi) instead of raw IDF when
      `use_bm25=True` (default). BM25 adds:
        - term-frequency saturation (a phrase appearing 50 times is not
          50x more relevant than 1 occurrence — diminishing returns via
          k1 = 1.5),
        - document-length normalisation (long tests don't get a free
          boost from sheer text volume — controlled by b = 0.75 and
          dl/avgdl).
      We compute the per-phrase TF as the count of non-overlapping matches
      in the test text (mirroring the overlap rules of the cascade) and
      the per-document length from the same tokenisation used by the IDF
      builder.  When `use_bm25=False`, the legacy raw-IDF weight is used
      verbatim — guaranteeing zero-regression behaviour.

    Args:
        embedding_sim: Pre-computed cosine similarity (0–1) between the
            diff text and this test's embedding vector.  Passed in by
            stage4 which holds the embedding index.  If None, Layer 5
            is skipped.
        use_bm25: When True (default), weight each matched phrase via the
            BM25 Okapi formula (TF saturation + length normalisation).
            When False, use the legacy raw-IDF weight (the pre-upgrade
            behaviour, kept for A/B comparison and migration safety).
    """
    raw_text = _extract_test_text(test)          # already normalised (L1)
    if not raw_text:
        return 0.0, []

    stemmed_text = _stem_phrase(raw_text)         # pre-stem for L3

    # UPGRADE 1: per-document length for BM25 length-norm. Computed once
    # per test (cheap regex scan over the already-normalised text).
    doc_length_value = float(_doc_length(raw_text)) if use_bm25 else None

    total_score = 0.0
    matched_details = []

    sorted_phrases = sorted(phrases, key=len, reverse=True)
    matched_regions: List[Tuple[int, int]] = []

    for phrase in sorted_phrases:
        # ---- normalise the phrase through the same layers ----
        phrase_norm = _normalize_chars(phrase)     # L1/L2
        phrase_stem = _stem_phrase(phrase_norm)     # L3
        phrase_words = phrase_norm.split()          # L4

        # --- L1 / L2: exact substring in normalised text ---
        match_layer = None
        start = raw_text.find(phrase_norm)
        if start >= 0:
            end = start + len(phrase_norm)
            match_layer = 'exact'
        else:
            # --- L3: stemmed substring in stemmed text ---
            start = stemmed_text.find(phrase_stem)
            if start >= 0:
                end = start + len(phrase_stem)
                match_layer = 'stem'
            else:
                # --- L4: prefix-aware consecutive word match ---
                text_words_set = set(raw_text.split())
                offset = _phrase_matches_prefix(phrase_words, text_words_set, raw_text)
                if offset is not None:
                    start = offset
                    end = offset + len(phrase_norm)
                    match_layer = 'prefix'

        if match_layer is None:
            continue

        # Overlap check — block ANY overlap, not just full containment
        overlaps = False
        for ms, me in matched_regions:
            if not (end <= ms or start >= me):
                overlaps = True
                break
        if overlaps:
            continue
        matched_regions.append((start, end))

        # ----- weight selection: BM25 (default) vs raw IDF (legacy) -----
        if use_bm25:
            # UPGRADE 1: BM25 Okapi weight. Per-phrase TF is the count of
            # additional non-overlapping occurrences in the test text
            # (>= 1 because we just located one match above). For the
            # cascade-friendly layer we score, choose the variant that
            # produced the match: stemmed text for the stem layer,
            # normalised text for exact / prefix layers.
            if match_layer == 'stem':
                hay = stemmed_text
                needle = phrase_stem
            else:
                hay = raw_text
                needle = phrase_norm
            # Count non-overlapping occurrences (cheap str.count is exact
            # for our normalised, space-separated text). Guard against the
            # zero-length pathological case.
            tf = float(hay.count(needle)) if needle else 1.0
            if tf < 1.0:
                tf = 1.0  # we already found at least one match above

            bm25_norm = compute_bm25_score(
                phrase_norm, idf_index, tf=tf, doc_length=doc_length_value)
            bm25_stem = compute_bm25_score(
                phrase_stem, idf_index, tf=tf, doc_length=doc_length_value)
            weight = max(bm25_norm, bm25_stem)
            weight_label = 'bm25'
        else:
            # Legacy path: raw IDF weight.
            idf_n = get_idf(phrase_norm, idf_index)
            idf_s = get_idf(phrase_stem, idf_index)
            weight = max(idf_n, idf_s)
            weight_label = 'idf'

        word_count = len(phrase_words)
        length_bonus = word_count * 0.5 + 0.5

        # Confidence discount: fuzzier layers contribute slightly less
        layer_confidence = {
            'exact': 1.0, 'stem': 0.95, 'prefix': 0.90,
        }
        confidence = layer_confidence[match_layer]

        contribution = weight * length_bonus * confidence
        total_score += contribution

        matched_details.append({
            'phrase': phrase,
            'idf': round(weight, 2),       # field name kept for back-compat
            'weight_kind': weight_label,    # 'bm25' or 'idf' for diagnostics
            'length_bonus': length_bonus,
            'contribution': round(contribution, 2),
            'match_layer': match_layer,
        })

    # --- L5: Semantic embedding ---
    # Two modes:
    #   Rescue: NO lexical layers matched → use embedding as sole signal.
    #           Threshold ≥0.40 (stricter to avoid false positives).
    #           Score scaled to be above IDF bypass threshold so downstream
    #           stages don't silently discard the match.
    #   Boost:  Lexical layers DID match → add an embedding bonus that
    #           rewards tests whose overall meaning aligns with the code
    #           change, not just those that happen to share keywords.
    stats = idf_index.get('statistics', {})
    if total_score == 0.0 and embedding_sim is not None and embedding_sim >= 0.40:
        # Rescue mode: use P75 IDF as base (higher than median) so the
        # resulting score can clear the Stage 5 IDF bypass threshold for
        # tests with strong semantic similarity (≥0.50).
        p75_idf = stats.get('p75', 5.0)
        contribution = p75_idf * embedding_sim * 2.0
        total_score = round(contribution, 2)
        matched_details.append({
            'phrase': '[semantic-embedding]',
            'idf': round(p75_idf, 2),
            'length_bonus': round(embedding_sim, 3),
            'contribution': round(contribution, 2),
            'match_layer': 'embedding',
        })
    elif total_score > 0.0 and embedding_sim is not None and embedding_sim >= 0.40:
        # Boost mode: additive bonus when BOTH lexical and semantic match.
        # Tests that match diff keywords AND are semantically close to the
        # code change should rank higher than tests that match keywords
        # coincidentally (e.g. split-shift tests matching context phrases).
        median_idf = stats.get('p50', 3.0)
        boost = median_idf * embedding_sim * 2.0
        total_score = round(total_score + boost, 2)
        matched_details.append({
            'phrase': '[embedding-boost]',
            'idf': round(median_idf, 2),
            'length_bonus': round(embedding_sim, 3),
            'contribution': round(boost, 2),
            'match_layer': 'embedding',
        })

    return round(total_score, 2), matched_details


def score_phrases_against_test_text(phrases: List[str], text: str,
                                     idf_index: Dict) -> Tuple[float, List[str]]:
    """
    Simplified scoring: returns total score and list of matched phrases.
    Uses Layer 1-3 cascade (exact → normalised → stemmed).
    """
    if not text:
        return 0.0, []

    text_norm = _normalize_chars(text)
    text_stem = _stem_phrase(text_norm)
    total_score = 0.0
    matched = []

    sorted_phrases = sorted(phrases, key=len, reverse=True)
    matched_regions: List[Tuple[int, int]] = []

    for phrase in sorted_phrases:
        phrase_norm = _normalize_chars(phrase)
        phrase_stem = _stem_phrase(phrase_norm)

        start = text_norm.find(phrase_norm)
        if start < 0:
            start = text_stem.find(phrase_stem)
        if start < 0:
            continue

        end = start + len(phrase_norm)

        overlaps = False
        for ms, me in matched_regions:
            if start >= ms and end <= me:
                overlaps = True
                break
        if overlaps:
            continue

        matched_regions.append((start, end))

        idf = max(get_idf(phrase_norm, idf_index),
                  get_idf(phrase_stem, idf_index))
        word_count = len(phrase_norm.split())
        length_bonus = word_count * 0.5 + 0.5

        total_score += idf * length_bonus
        matched.append(phrase)

    return round(total_score, 2), matched


# ---------------------------------------------------------------------------
# CLI (for testing / debugging)
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse
    import time
    from pathlib import Path

    parser = argparse.ArgumentParser(description='Build or query the IDF index.')
    parser.add_argument('--corpus', required=True, help='Path to test corpus JSON.')
    parser.add_argument('--output', help='Path to save the IDF index.')
    parser.add_argument('--query', nargs='*', help='Terms to look up IDF for.')
    args = parser.parse_args()

    if args.output or not args.query:
        start = time.time()
        index = build_idf_index(args.corpus, args.output)
        elapsed = time.time() - start
        print(f"Built IDF index in {elapsed:.2f}s: "
              f"{len(index['idf'])} terms from {index['total_documents']} tests")

    if args.query:
        if not args.output:
            index = build_idf_index(args.corpus)
        for term in args.query:
            idf = get_idf(term, index)
            df = index.get('document_frequency', {}).get(term.lower(), 0)
            print(f"  '{term}': DF={df}, IDF={idf:.2f}")
