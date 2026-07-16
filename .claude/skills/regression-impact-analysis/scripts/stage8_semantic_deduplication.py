#!/usr/bin/env python3
"""
stage8_semantic_deduplication.py - Stage 8: Semantic Deduplication.

Replaces / augments the prior `stage8_focused_deduplication.py` (call-graph
Jaccard) with a SEMANTIC dedup that operates on the FULL TEXT of each test
case (summary + description + steps).

Algorithm (5 steps):

    Step 1: BUILD FULL TEXT
            Combine summary + description + steps into one normalized
            text block per test case.

    Step 2: SEMANTIC SIMILARITY
            Embed every test once with the same `all-MiniLM-L6-v2` model
            used elsewhere in RIA. For every unordered pair compute
            cosine similarity. Embeddings are cached on the local file
            system keyed by (test_id, sha256(full_text)).

    Step 3: EXTRACT DIFFERENT WORDS
            Tokenise the two full texts (lowercase, alphanumerics
            only). Strip stopwords + a small RIA-specific set. Compute
            symmetric difference -> these are the candidate
            "behaviour-changing" tokens.

    Step 4: LLM JUDGEMENT (only when similarity >= threshold)
            Ask the LLM: "Given these different words, do the two tests
            verify the SAME behaviour or DIFFERENT behaviours?"
            -> Returns "SAME_BEHAVIOR" or "DIFFERENT_BEHAVIOR".
            On any LLM/network error we fall back to DIFFERENT_BEHAVIOR
            (conservative - keep both).

    Step 5: FINAL DECISION
            similarity < threshold              -> KEEP BOTH
            similarity >= threshold AND
                LLM says DIFFERENT_BEHAVIOR    -> KEEP BOTH
                LLM says SAME_BEHAVIOR         -> REMOVE the test with
                                                  the LOWER confidence
                                                  (Stage-7 confidence;
                                                  ties broken by issue
                                                  key lexicographically)

Public entry point:

    semantic_deduplicate(
        stage7_output_path,        # str | Path
        enriched_corpus_path,      # str | Path
        output_dir,                # str | Path
        similarity_threshold=0.85,
        embedding_model_name="sentence-transformers/all-MiniLM-L6-v2",
        llm_judge_enabled=True,
        cache_dir=None,            # default: <output_dir>/cache_stage8
    ) -> Dict[str, Any]

Outputs (under `output_dir`):
    stage8_semantic_dedup.json      # Kept tests + removed tests + reasons
    stage8_semantic_dedup_report.json  # Pairwise scores, LLM verdicts, etc.

Determinism:
    - Embedding model is fixed (`all-MiniLM-L6-v2`) at temperature-equivalent
      0 (deterministic forward pass).
    - Behaviour tie-breaker is conservative (no LLM): uncertain pairs keep
      both tests (see `_bedrock_invoke_safely`).
    - Pair iteration order is deterministic (sorted issue_key).
    - Tie-breaks fall back to issue_key string ordering.

Edge cases handled (table):
    Empty input                          -> writes valid empty output, exit 0
    Single test                          -> pass-through, no comparison
    Missing description                  -> use summary + steps only
    Missing steps                        -> use summary + description only
    Missing both desc & steps            -> use summary only
    Missing summary                      -> use description + steps fallback
    All fields empty                     -> treated as singleton (never merged)
    Embedding model unavailable          -> RuntimeError with actionable message
    LLM API error / timeout              -> 3 retries, then conservative
                                            (KEEP_BOTH = DIFFERENT_BEHAVIOR)
    No duplicates found                  -> kept = input, removed = []
    All tests identical                  -> keep one representative per cluster
    Invalid JSON input                   -> raise FileNotFoundError /
                                            json.JSONDecodeError early

CLI:
    python3 stage8_semantic_deduplication.py \
        --input  /path/to/stage7_llm_tc_judgment.json \
        --corpus /path/to/all_tcs_extracted_enriched_source.json \
        --output /path/to/stage8_semantic_dedup.json \
        [--threshold 0.85] [--no-llm-judge]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_LOG = logging.getLogger("stage8_semantic_dedup")
if not _LOG.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter(
        "[stage8] %(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    ))
    _LOG.addHandler(_h)
    _LOG.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_THRESHOLD = 0.85
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_LLM_RETRIES = 3
DEFAULT_LLM_BACKOFF_SEC = 2.0
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-]*")
WHITESPACE_RE = re.compile(r"\s+")
PUNCT_RE = re.compile(r"[^\w\s\-]")
MIN_TOKEN_LEN = 2

# Built-in English stopwords (avoids nltk dependency).
STOPWORDS: FrozenSet[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "so",
    "of", "for", "to", "in", "on", "at", "by", "with", "from", "as",
    "is", "are", "was", "were", "be", "been", "being", "am", "do",
    "does", "did", "doing", "have", "has", "had", "having",
    "this", "that", "these", "those", "it", "its", "they", "them",
    "their", "there", "here", "what", "which", "who", "whom", "whose",
    "when", "where", "why", "how", "all", "any", "both", "each",
    "few", "more", "most", "other", "some", "such", "no", "not",
    "only", "own", "same", "than", "too", "very", "can", "will",
    "just", "should", "would", "could", "may", "might", "must",
    "shall", "i", "me", "my", "we", "us", "our", "you", "your",
    "he", "him", "his", "she", "her", "hers", "into", "over",
    "under", "again", "further", "once", "out", "up", "down",
    "off", "above", "below",
})

# RIA-specific tokens that are noise across most test cases. Kept
# DELIBERATELY SMALL so we don't accidentally erase signal that
# distinguishes matrix variants. Note: "agent", "user", "page", etc. are
# NOT in this list because they often carry signal in the test summary.
RIA_NOISE: FrozenSet[str] = frozenset({
    "verify", "validate", "confirm", "ensure", "tc", "tcs",
    "step", "steps", "click", "clicks", "clicking",
    "open", "opens", "opened", "opening",
    "successfully", "success",
    "result", "results", "expected", "actual",
    "shall", "should", "must", "able",
})

# ---------------------------------------------------------------------------
# Lightweight JSON helpers
# ---------------------------------------------------------------------------

def _safe_load_json(path: Path) -> Optional[Any]:
    """Load JSON or return None if missing/unreadable."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as exc:
        raise json.JSONDecodeError(
            f"Invalid JSON in {path}: {exc.msg}", exc.doc, exc.pos
        )


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, default=str, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Step 1: Build full text
# ---------------------------------------------------------------------------

def build_full_text(test_case: Dict[str, Any]) -> str:
    """
    Combine summary + description + steps into a single normalised text.

    Missing fields are silently skipped. Step structure is flexible:
      - list of dicts with action/data/result/expected keys
      - list of strings
      - single string
    Newlines and tabs are collapsed to spaces. Output is lower-cased.
    """
    if not isinstance(test_case, dict):
        return ""

    parts: List[str] = []

    summary = test_case.get("summary")
    if isinstance(summary, str) and summary.strip():
        parts.append(summary.strip())

    description = test_case.get("description")
    if isinstance(description, str) and description.strip():
        parts.append(description.strip())

    steps = test_case.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict):
                for key in ("action", "data", "result", "expected"):
                    val = step.get(key)
                    if isinstance(val, str) and val.strip():
                        parts.append(val.strip())
            elif isinstance(step, str) and step.strip():
                parts.append(step.strip())
    elif isinstance(steps, str) and steps.strip():
        parts.append(steps.strip())

    full = " ".join(parts).lower()
    full = WHITESPACE_RE.sub(" ", full).strip()
    return full


# ---------------------------------------------------------------------------
# Step 2: Semantic similarity (with embedding cache)
# ---------------------------------------------------------------------------

class _EmbeddingProvider:
    """
    Lazy wrapper around fastembed.TextEmbedding so the model is loaded
    only once even when this module is imported repeatedly. Caches one
    model per instance.
    """

    def __init__(self, model_name: str = DEFAULT_EMBEDDING_MODEL):
        self.model_name = model_name
        self._model = None  # type: Any
        self._cache: Dict[str, Any] = {}  # text_hash -> np.ndarray

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        try:
            from fastembed import TextEmbedding  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "Embedding model 'fastembed' is not installed. "
                "Install with: pip install fastembed"
            ) from exc
        try:
            self._model = TextEmbedding(model_name=self.model_name)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to initialise embedding model "
                f"'{self.model_name}': {exc}"
            ) from exc

    def embed_many(self, texts: List[str]) -> List[Any]:
        """Embed a list of texts, returning a list of np.ndarray vectors."""
        self._ensure_model()
        # Replace empty strings with placeholder to avoid model errors.
        sanitised = [t if t and t.strip() else "empty test" for t in texts]
        return list(self._model.embed(sanitised))


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _embed_with_cache(
    test_ids: List[str],
    full_texts: List[str],
    provider: _EmbeddingProvider,
    cache_dir: Optional[Path],
) -> Dict[str, Any]:
    """
    Compute (or load) one embedding per test. Returns dict
    test_id -> np.ndarray (shape (D,)).
    """
    import numpy as np  # local import; numpy is always available with fastembed

    embeddings: Dict[str, Any] = {}
    needed_ids: List[str] = []
    needed_texts: List[str] = []

    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)

    # Check on-disk cache (per-test files).
    for tid, text in zip(test_ids, full_texts):
        digest = _hash_text(text)
        cached_path = (cache_dir / f"{tid}_{digest}.npy") if cache_dir else None
        if cached_path is not None and cached_path.exists():
            try:
                embeddings[tid] = np.load(cached_path)
                continue
            except Exception:
                # Corrupt cache file - re-embed.
                try:
                    cached_path.unlink()
                except Exception:
                    pass
        needed_ids.append(tid)
        needed_texts.append(text)

    if needed_texts:
        _LOG.info(
            "Embedding %d test(s) (cache hits=%d)",
            len(needed_texts), len(test_ids) - len(needed_texts),
        )
        vectors = provider.embed_many(needed_texts)
        for tid, text, vec in zip(needed_ids, needed_texts, vectors):
            arr = np.asarray(vec, dtype=np.float32)
            embeddings[tid] = arr
            if cache_dir is not None:
                digest = _hash_text(text)
                try:
                    np.save(cache_dir / f"{tid}_{digest}.npy", arr)
                except Exception as exc:
                    _LOG.warning(
                        "Failed to write embedding cache for %s: %s", tid, exc
                    )
    else:
        _LOG.info(
            "Embedding cache covered all %d tests; no fresh embeds needed",
            len(test_ids),
        )

    return embeddings


def calculate_semantic_similarity(vec1: Any, vec2: Any) -> float:
    """
    Cosine similarity between two numpy vectors. Returns a float in [0, 1]
    after clamping (the model returns >= 0 in practice; we clamp to be safe).
    """
    import numpy as np

    if vec1 is None or vec2 is None:
        return 0.0

    a = np.asarray(vec1, dtype=np.float32)
    b = np.asarray(vec2, dtype=np.float32)

    if a.size == 0 or b.size == 0:
        return 0.0

    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na < 1e-10 or nb < 1e-10:
        return 0.0

    cos = float(np.dot(a, b) / (na * nb))
    # Clamp tiny FP overshoots
    if cos > 1.0:
        cos = 1.0
    elif cos < 0.0:
        cos = 0.0
    return cos


# ---------------------------------------------------------------------------
# Step 3: Extract different words
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    """
    Split into lowercase alphanumeric tokens. Drops punctuation and tokens
    shorter than MIN_TOKEN_LEN.
    """
    if not text:
        return []
    cleaned = PUNCT_RE.sub(" ", text.lower())
    return [t for t in TOKEN_RE.findall(cleaned) if len(t) >= MIN_TOKEN_LEN]


def _normalise_token(tok: str) -> str:
    """
    Light normalisation - rule-based suffix stripping (no external lemmatiser).
    Handles common English plurals and gerund forms; does NOT alter domain
    terms like "ct", "min", "templateid".
    """
    t = tok.lower().strip()
    if len(t) <= 3:
        return t
    # plural / possessive
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    if t.endswith("sses"):
        return t[:-2]
    if t.endswith("ses") and len(t) > 4:
        return t[:-2]
    if t.endswith("s") and not t.endswith("ss") and len(t) > 3:
        return t[:-1]
    # gerund / past
    if t.endswith("ing") and len(t) > 5:
        return t[:-3]
    if t.endswith("ed") and len(t) > 4:
        return t[:-2]
    return t


def extract_different_words(
    text1: str,
    text2: str,
    extra_stopwords: Optional[Iterable[str]] = None,
) -> Tuple[Set[str], Set[str]]:
    """
    Compute (only_in_1, only_in_2) sets of NORMALISED, non-stopword tokens.

    Both English stopwords and a curated RIA noise list are removed.
    Tokens are lowercased and lightly stemmed (suffix rules) so trivial
    plural / gerund differences are NOT flagged as behaviour change.
    """
    extra = frozenset(s.lower() for s in (extra_stopwords or ()))
    drop = STOPWORDS | RIA_NOISE | extra

    def _set(text: str) -> Set[str]:
        out: Set[str] = set()
        for raw in _tokenize(text):
            norm = _normalise_token(raw)
            if not norm or norm in drop or norm.isdigit():
                continue
            out.add(norm)
        return out

    s1, s2 = _set(text1), _set(text2)
    return (s1 - s2), (s2 - s1)


# ---------------------------------------------------------------------------
# Step 4: LLM judgement
# ---------------------------------------------------------------------------

# Possible verdict strings (canonical).
VERDICT_DIFFERENT = "DIFFERENT_BEHAVIOR"
VERDICT_SAME = "SAME_BEHAVIOR"


def _bedrock_invoke_safely(prompt: str, *,
                            system: str = "",
                            max_retries: int = DEFAULT_LLM_RETRIES,
                            backoff_sec: float = DEFAULT_LLM_BACKOFF_SEC,
                            ) -> Optional[str]:
    """
    Behaviour-difference tie-breaker.

    RIA no longer calls a network LLM or a file mailbox from inside the
    pipeline, and per-pair judgment is not deferred to the Copilot agent
    (it would add a third pause for marginal dedup gain). This function
    therefore returns None, which ``_parse_verdict`` maps to
    DIFFERENT_BEHAVIOR — the CONSERVATIVE outcome that keeps both tests.
    Net effect: Stage 8 dedups only on embedding/lexical similarity and
    never suppresses a test on an uncertain behaviour call.
    """
    return None


def _parse_verdict(raw: Optional[str]) -> str:
    """
    Parse the LLM response into a verdict string. Defaults to
    DIFFERENT_BEHAVIOR (conservative) on any ambiguity.
    """
    if not raw:
        return VERDICT_DIFFERENT
    t = raw.strip().upper()
    # Strip any surrounding json/markdown noise.
    if "SAME_BEHAVIOR" in t:
        return VERDICT_SAME
    if "DIFFERENT_BEHAVIOR" in t:
        return VERDICT_DIFFERENT
    # Loose heuristic - the prompt asks for one of two strings, so any
    # other answer is treated as DIFFERENT (keep both - conservative).
    return VERDICT_DIFFERENT


def llm_judge_behavior_difference(
    test1: Dict[str, Any],
    test2: Dict[str, Any],
    diff_words_1: Set[str],
    diff_words_2: Set[str],
    *,
    enabled: bool = True,
    max_retries: int = DEFAULT_LLM_RETRIES,
) -> Tuple[str, str]:
    """
    Ask the LLM whether two highly-similar tests verify the SAME behaviour
    or DIFFERENT behaviours. Returns (verdict, raw_response).

    The LLM ALWAYS receives the raw summaries (not only the diff-word
    sets) - because matrix variants (e.g. "[Web]" vs "[Mobile]" in the
    summary) often produce an empty diff after stemming/stopword removal,
    yet still exercise distinct behaviours.

    On any failure (LLM disabled, network error, parse error, etc.) returns
    (DIFFERENT_BEHAVIOR, "<reason>") so the caller keeps both tests
    (conservative).
    """
    if not enabled:
        return VERDICT_DIFFERENT, "llm-disabled"

    only1 = sorted(diff_words_1)[:50]
    only2 = sorted(diff_words_2)[:50]
    sum1 = (test1.get("summary") or "").strip()[:400]
    sum2 = (test2.get("summary") or "").strip()[:400]
    desc1 = (test1.get("description") or "").strip()[:600]
    desc2 = (test2.get("description") or "").strip()[:600]
    id1 = test1.get("issue_key") or test1.get("test_id") or "Test1"
    id2 = test2.get("issue_key") or test2.get("test_id") or "Test2"

    # If both summary AND description are byte-identical we have a true
    # duplicate (no signal whatsoever for the LLM to act on). Note that
    # we ONLY take this short-circuit when there is also no textual diff
    # in the rest of the body - otherwise we still consult the LLM.
    if sum1 == sum2 and desc1 == desc2 and not only1 and not only2:
        return VERDICT_SAME, "identical-summary-description-no-diff-words"

    system = (
        "You are a senior QA engineer judging whether two regression test "
        "cases are TRUE DUPLICATES (i.e. running both wastes time) or "
        "DELIBERATELY-DISTINCT MATRIX VARIANTS that must both run. "
        "Default to DIFFERENT_BEHAVIOR unless the two tests are clearly "
        "duplicates. Treat ANY of the following as DIFFERENT_BEHAVIOR: "
        "channel (Web vs Mobile), skill model (Single vs Multi vs Primary), "
        "agent scope (Search agent vs All agents vs specific cohort), "
        "functional flow (Extra Hours vs Trade vs Swap vs Absence vs "
        "Rule Engine vs Manual Adjust vs Migration vs Template Name "
        "validation), validation rule (Min Daily Shift Gap vs "
        "Min Continuous Work Hours vs Advance Notice vs Template Name), "
        "pre-condition, expected outcome, or error path. "
        "Respond with EXACTLY one token, all uppercase: SAME_BEHAVIOR or "
        "DIFFERENT_BEHAVIOR. Output nothing else."
    )

    prompt = f"""TEST_1 ({id1})
Summary: {sum1}
Description: {desc1}

TEST_2 ({id2})
Summary: {sum2}
Description: {desc2}

Tokens unique to {id1} (after light stemming + stopword removal):
{', '.join(only1) if only1 else '(none after normalisation)'}

Tokens unique to {id2} (after light stemming + stopword removal):
{', '.join(only2) if only2 else '(none after normalisation)'}

DECISION RULES (apply in order):
  1. If the two summaries name DIFFERENT functional flows
     (Trade, Swap, Extra Hours, Absence, Rule Engine, Manual Adjust,
     Migration, Template Name, Time Off) -> DIFFERENT_BEHAVIOR.
  2. If they name the same flow but DIFFERENT channels ([Web]/[Mobile]),
     skill models ([Single]/[Multi]/[Primary]), agent scopes
     ([Search agent]/[All agents]) or validation rules
     (Min Daily Shift Gap vs Min Continuous Work Hours)
     -> DIFFERENT_BEHAVIOR.
  3. If the only differences are test data values, step ordering, or
     synonyms (verify/validate, agentid=1/agentid=2) -> SAME_BEHAVIOR.
  4. When in doubt -> DIFFERENT_BEHAVIOR (it is safer to keep a
     potentially-redundant test than to drop a unique one).

Decide:"""
    raw = _bedrock_invoke_safely(
        prompt, system=system, max_retries=max_retries,
    )
    verdict = _parse_verdict(raw)
    return verdict, (raw or "<no-response>")


# ---------------------------------------------------------------------------
# Stage 5: Final decision (cluster + remove)
# ---------------------------------------------------------------------------

class _UnionFind:
    """Tiny union-find used to build duplicate clusters."""

    def __init__(self, n: int):
        self.parent = list(range(n))
        self.rank = [0] * n

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def _confidence_of(test: Dict[str, Any]) -> float:
    """Stage-7 confidence (0..1). Defaults to 0.5 if missing."""
    val = test.get("confidence")
    try:
        return float(val) if val is not None else 0.5
    except (TypeError, ValueError):
        return 0.5


def _representative_index(cluster: List[int],
                          tests: List[Dict[str, Any]]) -> int:
    """
    Pick the representative test for a cluster:
      1. Highest Stage-7 confidence
      2. Tie-break: lexicographically smallest issue_key
    """
    def sort_key(idx: int) -> Tuple[float, str]:
        t = tests[idx]
        return (
            -_confidence_of(t),
            str(t.get("issue_key") or t.get("test_id") or ""),
        )
    return sorted(cluster, key=sort_key)[0]


# ---------------------------------------------------------------------------
# Test extraction (merge stage7 judgments + enriched corpus)
# ---------------------------------------------------------------------------

def _build_corpus_index(corpus: Optional[Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(corpus, list):
        return out
    for r in corpus:
        if not isinstance(r, dict):
            continue
        key = r.get("issue_key") or r.get("key") or r.get("issue_id")
        if isinstance(key, str) and key:
            out[key] = r
    return out


def _select_seed_tests(stage7: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Tests to deduplicate = Stage-7 judgments with verdict in
    {INDIRECT, DIRECT}. NOT_RELEVANT and UNJUDGED are skipped.
    """
    judgments = stage7.get("judgments") if isinstance(stage7, dict) else None
    if not isinstance(judgments, list):
        return []
    seeds: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for j in judgments:
        if not isinstance(j, dict):
            continue
        verdict = j.get("verdict")
        if verdict not in ("INDIRECT", "DIRECT"):
            continue
        key = j.get("issue_key") or j.get("test_id")
        if not isinstance(key, str) or not key or key in seen:
            continue
        seen.add(key)
        seeds.append(j)
    return seeds


def _enrich_with_corpus(seed: Dict[str, Any],
                        corpus_idx: Dict[str, Dict[str, Any]]
                        ) -> Dict[str, Any]:
    """
    Merge the Stage-7 judgment record with the enriched-corpus record so
    we have summary/description/steps available for full-text building.
    Stage-7 fields take precedence on overlap (they are the source of
    truth for verdict/confidence/etc.).
    """
    key = seed.get("issue_key") or seed.get("test_id")
    merged: Dict[str, Any] = {}
    if isinstance(key, str) and key in corpus_idx:
        for k, v in corpus_idx[key].items():
            merged[k] = v
    for k, v in seed.items():
        if v is not None and v != "" and v != [] and v != {}:
            merged[k] = v
    if isinstance(key, str):
        merged.setdefault("issue_key", key)
    return merged


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def deduplicate_tests(
    tests: List[Dict[str, Any]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    embedding_provider: Optional[_EmbeddingProvider] = None,
    cache_dir: Optional[Path] = None,
    llm_judge_enabled: bool = True,
    llm_max_retries: int = DEFAULT_LLM_RETRIES,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    """
    Main deduplication routine.

    Args:
        tests: list of test dicts (already merged with corpus). Each must
            have at least an `issue_key` (or `test_id`) and ideally
            summary/description/steps.
        threshold: cosine similarity at/above which we consult the LLM.
        embedding_provider: optional pre-constructed provider (useful for
            tests / sharing across runs).
        cache_dir: where to store per-test embedding cache. None disables.
        llm_judge_enabled: turn off the LLM step entirely.

    Returns:
        (kept_tests, removed_tests, report_dict)
    """
    n = len(tests)

    # --- Edge cases ---------------------------------------------------------
    if n == 0:
        return [], [], {
            "input_count": 0,
            "output_count": 0,
            "removed_count": 0,
            "pairs_evaluated": 0,
            "pairs_above_threshold": 0,
            "llm_calls_made": 0,
            "duration_seconds": 0.0,
            "threshold": threshold,
            "pairs": [],
            "clusters": [],
            "note": "empty input - nothing to deduplicate",
        }

    if n == 1:
        only = tests[0]
        return [only], [], {
            "input_count": 1,
            "output_count": 1,
            "removed_count": 0,
            "pairs_evaluated": 0,
            "pairs_above_threshold": 0,
            "llm_calls_made": 0,
            "duration_seconds": 0.0,
            "threshold": threshold,
            "pairs": [],
            "clusters": [{
                "representative": only.get("issue_key") or only.get("test_id"),
                "members": [only.get("issue_key") or only.get("test_id")],
            }],
            "note": "single test - pass-through",
        }

    t0 = time.time()

    # Stable order keyed by issue_key for determinism.
    sorted_tests = sorted(
        tests,
        key=lambda t: str(t.get("issue_key") or t.get("test_id") or ""),
    )
    test_ids: List[str] = [
        str(t.get("issue_key") or t.get("test_id") or f"_idx{i}")
        for i, t in enumerate(sorted_tests)
    ]

    # ----- Step 1: Build full text -----------------------------------------
    full_texts: List[str] = [build_full_text(t) for t in sorted_tests]

    # ----- Step 2: Embed (with cache) --------------------------------------
    if embedding_provider is None:
        embedding_provider = _EmbeddingProvider()

    embed_map = _embed_with_cache(
        test_ids=test_ids,
        full_texts=full_texts,
        provider=embedding_provider,
        cache_dir=cache_dir,
    )

    # ----- Vectorised similarity matrix -----------------------------------
    # Replace the O(n^2) nested loop with one numpy matrix multiplication.
    # This computes cosine similarity for ALL i<j pairs at once. Float64
    # is used (rather than the legacy float32) to maximise numerical
    # precision; the per-pair `round(sim, 4)` step that follows produces
    # the same recorded value as the legacy path for any non-pathological
    # vector pair.
    import numpy as _np  # local import; numpy is always available here

    # Build the embedding matrix in `test_ids` order.  Tests whose
    # embedding is missing get a zero-vector of the appropriate
    # dimensionality so they will trivially produce 0.0 similarity
    # against every other test (matching the legacy behaviour where
    # `calculate_semantic_similarity(None, ...) == 0.0`).
    _embed_dim = 0
    for _v in embed_map.values():
        if _v is not None:
            try:
                _embed_dim = int(_np.asarray(_v).shape[-1])
                if _embed_dim > 0:
                    break
            except Exception:
                continue
    # Fallback: dimensionless zero matrix means every similarity is 0.0
    # which is exactly what the legacy path returns.
    matrix = _np.zeros((n, max(_embed_dim, 1)), dtype=_np.float64)
    for idx, tid in enumerate(test_ids):
        vec = embed_map.get(tid)
        if vec is None:
            continue
        try:
            arr = _np.asarray(vec, dtype=_np.float64).reshape(-1)
        except Exception:
            continue
        if arr.size == 0:
            continue
        # If for some reason a vector has a different dimensionality
        # than the matrix width, skip it (treat as zero) - matches the
        # legacy `if a.size == 0 ...` short-circuit.
        if arr.shape[0] != matrix.shape[1]:
            continue
        matrix[idx] = arr

    # Compute norms; tests with norm < 1e-10 yield 0.0 similarity
    # against everything (this matches the legacy `na < 1e-10` guard).
    norms = _np.linalg.norm(matrix, axis=1)
    safe_norms = _np.where(norms < 1e-10, 1.0, norms)
    normalized = matrix / safe_norms[:, None]
    # Zero-out rows whose original norm was effectively zero so they
    # produce 0.0 similarity rather than leaking the safe_norms=1 trick.
    zero_mask = norms < 1e-10
    if _np.any(zero_mask):
        normalized[zero_mask] = 0.0

    sim_matrix = normalized @ normalized.T  # (n, n) float64 cosine sims
    # Clamp tiny FP overshoots to [0, 1] to mirror
    # `calculate_semantic_similarity`'s clamp.
    _np.clip(sim_matrix, 0.0, 1.0, out=sim_matrix)

    # Pairwise comparisons (upper triangle, i < j).
    uf = _UnionFind(n)
    pair_records: List[Dict[str, Any]] = []
    pairs_evaluated = 0
    pairs_above = 0
    llm_calls_made = 0

    # Phase A: build the deterministic list of pair records and decide
    # which ones (if any) need an LLM judgement.
    pending_llm_idx: List[int] = []  # indices into pair_records
    pending_llm_jobs: List[Tuple[int, int]] = []  # (i, j) for those records

    for i in range(n):
        for j in range(i + 1, n):
            pairs_evaluated += 1
            id_i, id_j = test_ids[i], test_ids[j]
            sim = float(sim_matrix[i, j])

            record: Dict[str, Any] = {
                "test_a": id_i,
                "test_b": id_j,
                "similarity": round(sim, 4),
                "decision": None,
                "reason": None,
                "llm_verdict": None,
                "llm_response": None,
                "diff_words_a_only": [],
                "diff_words_b_only": [],
            }

            if sim < threshold:
                record["decision"] = "KEEP_BOTH"
                record["reason"] = "similarity_below_threshold"
                pair_records.append(record)
                continue

            pairs_above += 1

            # ----- Step 3: Different words ---------------------------------
            only_a, only_b = extract_different_words(
                full_texts[i], full_texts[j],
            )
            record["diff_words_a_only"] = sorted(only_a)
            record["diff_words_b_only"] = sorted(only_b)
            # Stash the raw sets on the record so the parallel LLM
            # worker can read them without recomputing. They are
            # popped before the record is finalised so the schema of
            # `pair_records` is unchanged.
            record["_only_a_set"] = only_a
            record["_only_b_set"] = only_b
            record["_pair_ij"] = (i, j)

            pair_records.append(record)
            pending_llm_idx.append(len(pair_records) - 1)
            pending_llm_jobs.append((i, j))

    # Phase B: dispatch LLM judgements in parallel. Determinism is
    # preserved because the pair records (and their indices) are
    # already in the SAME deterministic (i, j) lexicographic order
    # the legacy sequential code produced; we only fan-out the LLM
    # calls, and merge results back into the corresponding records.
    if pending_llm_idx:
        if llm_judge_enabled:
            # We count an LLM call per record we attempt to judge so
            # the report's `llm_calls_made` matches the legacy path
            # exactly (the legacy code incremented this counter when
            # `llm_judge_enabled` was True, regardless of whether the
            # underlying invoke succeeded).
            llm_calls_made = len(pending_llm_idx)

        # The behaviour tie-breaker (`_bedrock_invoke_safely`) is now a
        # conservative no-op that returns None, so these workers just apply
        # the DIFFERENT_BEHAVIOR fallback. 8 workers is a safe cap.
        if llm_judge_enabled and len(pending_llm_idx) > 1:
            def _judge_one(rec_idx: int) -> Tuple[int, str, str]:
                rec = pair_records[rec_idx]
                i_, j_ = rec["_pair_ij"]
                v, r = llm_judge_behavior_difference(
                    sorted_tests[i_], sorted_tests[j_],
                    rec["_only_a_set"], rec["_only_b_set"],
                    enabled=True,
                    max_retries=llm_max_retries,
                )
                return rec_idx, v, r

            with ThreadPoolExecutor(max_workers=8) as _pool:
                futures = [_pool.submit(_judge_one, k) for k in pending_llm_idx]
                for fut in as_completed(futures):
                    rec_idx, verdict, raw = fut.result()
                    rec = pair_records[rec_idx]
                    rec["llm_verdict"] = verdict
                    rec["llm_response"] = (raw[:240] if isinstance(raw, str) else None)
        else:
            # Single pending pair OR LLM disabled: run inline so the
            # disabled-path short-circuit and verdict semantics in
            # `llm_judge_behavior_difference` are preserved.
            for rec_idx in pending_llm_idx:
                rec = pair_records[rec_idx]
                i_, j_ = rec["_pair_ij"]
                verdict, raw = llm_judge_behavior_difference(
                    sorted_tests[i_], sorted_tests[j_],
                    rec["_only_a_set"], rec["_only_b_set"],
                    enabled=llm_judge_enabled,
                    max_retries=llm_max_retries,
                )
                rec["llm_verdict"] = verdict
                rec["llm_response"] = (
                    raw[:240] if isinstance(raw, str) else None
                )

    # Phase C: apply union-find merges in DETERMINISTIC order
    # (sorted by (i, j)) so identical inputs always produce identical
    # cluster assignments regardless of which LLM future returned
    # first. This preserves the legacy sequential semantics.
    for rec_idx in pending_llm_idx:
        rec = pair_records[rec_idx]
        i_, j_ = rec["_pair_ij"]
        verdict = rec.get("llm_verdict")

        # ----- Step 5: Final decision --------------------------------
        if verdict == VERDICT_SAME:
            uf.union(i_, j_)
            rec["decision"] = "REMOVE_DUPLICATE"
            rec["reason"] = "high_similarity_same_behavior"
        else:
            rec["decision"] = "KEEP_BOTH"
            rec["reason"] = (
                "high_similarity_different_behavior"
                if llm_judge_enabled else
                "llm_disabled_conservative_keep"
            )

        # Strip the internal scratch keys so the record schema is
        # exactly what the legacy implementation produced.
        rec.pop("_only_a_set", None)
        rec.pop("_only_b_set", None)
        rec.pop("_pair_ij", None)

    # Build clusters
    cluster_map: Dict[int, List[int]] = {}
    for idx in range(n):
        cluster_map.setdefault(uf.find(idx), []).append(idx)
    clusters_idx = list(cluster_map.values())

    kept_idx: List[int] = []
    removed_records: List[Dict[str, Any]] = []
    cluster_records: List[Dict[str, Any]] = []
    for cluster in clusters_idx:
        if len(cluster) == 1:
            kept_idx.append(cluster[0])
            cluster_records.append({
                "representative": test_ids[cluster[0]],
                "members": [test_ids[cluster[0]]],
                "size": 1,
            })
            continue
        rep = _representative_index(cluster, sorted_tests)
        kept_idx.append(rep)
        cluster_records.append({
            "representative": test_ids[rep],
            "members": sorted(test_ids[i] for i in cluster),
            "size": len(cluster),
        })
        for o in cluster:
            if o == rep:
                continue
            o_id = test_ids[o]
            rep_id = test_ids[rep]
            # Find the pair record that justifies the removal (highest sim).
            justifying = None
            for p in pair_records:
                if {p["test_a"], p["test_b"]} == {o_id, rep_id} \
                        and p.get("decision") == "REMOVE_DUPLICATE":
                    justifying = p
                    break
            removed_records.append({
                "test_id": o_id,
                "kept_in_favor_of": rep_id,
                "similarity": (justifying or {}).get("similarity"),
                "llm_verdict": (justifying or {}).get("llm_verdict"),
                "diff_words_self_only": (justifying or {}).get(
                    "diff_words_a_only" if (justifying or {}).get("test_a") == o_id
                    else "diff_words_b_only", []
                ),
                "diff_words_kept_only": (justifying or {}).get(
                    "diff_words_b_only" if (justifying or {}).get("test_a") == o_id
                    else "diff_words_a_only", []
                ),
                "summary": sorted_tests[o].get("summary"),
            })

    kept_tests = [sorted_tests[i] for i in sorted(kept_idx)]
    removed_tests = removed_records

    duration = round(time.time() - t0, 3)
    report = {
        "input_count": n,
        "output_count": len(kept_tests),
        "removed_count": len(removed_records),
        "pairs_evaluated": pairs_evaluated,
        "pairs_above_threshold": pairs_above,
        "llm_calls_made": llm_calls_made,
        "duration_seconds": duration,
        "threshold": threshold,
        "embedding_model": embedding_provider.model_name,
        "llm_judge_enabled": llm_judge_enabled,
        "pairs": pair_records,
        "clusters": cluster_records,
    }
    if not removed_records:
        report["note"] = (
            f"No duplicates found - all {n} tests are unique"
        )
    return kept_tests, removed_tests, report


def semantic_deduplicate(
    stage7_output_path: Any,
    enriched_corpus_path: Any,
    output_dir: Any,
    similarity_threshold: float = DEFAULT_THRESHOLD,
    embedding_model_name: str = DEFAULT_EMBEDDING_MODEL,
    llm_judge_enabled: bool = True,
    cache_dir: Any = None,
) -> Dict[str, Any]:
    """
    End-to-end Stage-8 entry point. Reads the Stage-7 output + enriched
    corpus, runs the 5-step semantic deduplication, and writes:
        <output_dir>/stage8_semantic_dedup.json
        <output_dir>/stage8_semantic_dedup_report.json

    Returns the same dict written to `stage8_semantic_dedup.json`.
    """
    t_start = time.time()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if cache_dir is None:
        cache_dir = output_dir / "cache_stage8"
    cache_dir = Path(cache_dir)

    # ----- Load inputs -----------------------------------------------------
    stage7_path = Path(stage7_output_path)
    if not stage7_path.exists():
        raise FileNotFoundError(
            f"Stage-7 output not found: {stage7_path}"
        )
    stage7 = _safe_load_json(stage7_path) or {}
    if not isinstance(stage7, dict):
        raise ValueError(
            f"Stage-7 output must be a JSON object: {stage7_path}"
        )

    corpus_idx: Dict[str, Dict[str, Any]] = {}
    if enriched_corpus_path:
        corpus_path = Path(enriched_corpus_path)
        if corpus_path.exists():
            corpus_idx = _build_corpus_index(_safe_load_json(corpus_path))
        else:
            _LOG.warning(
                "Enriched corpus not found (%s); proceeding with stage7 fields only",
                corpus_path,
            )

    seeds = _select_seed_tests(stage7)
    enriched: List[Dict[str, Any]] = [
        _enrich_with_corpus(s, corpus_idx) for s in seeds
    ]

    _LOG.info(
        "Loaded %d Stage-7 seed tests (corpus enrichment hits: %d)",
        len(enriched),
        sum(1 for s in seeds if (s.get("issue_key") or "") in corpus_idx),
    )

    # ----- Run dedup -------------------------------------------------------
    provider = _EmbeddingProvider(model_name=embedding_model_name)
    kept, removed, report = deduplicate_tests(
        enriched,
        threshold=similarity_threshold,
        embedding_provider=provider,
        cache_dir=cache_dir,
        llm_judge_enabled=llm_judge_enabled,
    )

    duration_total = round(time.time() - t_start, 3)

    # ----- Build outputs ---------------------------------------------------
    dedup_payload = {
        "stage": "8",
        "description": "Semantic Deduplication (full-text + LLM behaviour judge)",
        "input_count": len(enriched),
        "output_count": len(kept),
        "removed_count": len(removed),
        "threshold": similarity_threshold,
        "embedding_model": embedding_model_name,
        "llm_judge_enabled": llm_judge_enabled,
        "duration_seconds": duration_total,
        "kept_tests": [
            {
                "test_id": t.get("issue_key") or t.get("test_id"),
                "summary": t.get("summary"),
                "verdict": t.get("verdict"),
                "confidence": t.get("confidence"),
                "scenario_match": t.get("scenario_match"),
                "reasoning": t.get("reasoning"),
            }
            for t in kept
        ],
        "removed_tests": removed,
    }

    report_payload = {
        "stage": "8",
        "description": "Semantic Deduplication report",
        **report,
        "wall_time_seconds": duration_total,
    }

    dedup_path = output_dir / "stage8_semantic_dedup.json"
    report_path = output_dir / "stage8_semantic_dedup_report.json"
    _write_json(dedup_path, dedup_payload)
    _write_json(report_path, report_payload)

    _LOG.info(
        "Wrote %s (kept=%d, removed=%d, threshold=%.2f, duration=%.2fs)",
        dedup_path, len(kept), len(removed), similarity_threshold, duration_total,
    )
    return dedup_payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Stage 8 - Semantic Deduplication for RIA test selection.",
    )
    p.add_argument("--input", required=True,
                   help="Path to stage7_llm_tc_judgment.json")
    p.add_argument("--corpus", default=None,
                   help="Path to all_tcs_extracted_enriched_source.json")
    p.add_argument("--output", default=None,
                   help="Direct path to write stage8_semantic_dedup.json. "
                        "If a directory is given, the canonical file name is used. "
                        "Defaults to ./stage8_semantic_dedup.json")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                   help=f"Cosine similarity threshold (default: {DEFAULT_THRESHOLD})")
    p.add_argument("--no-llm-judge", action="store_true",
                   help="Disable Step 4 (LLM judgement). Pairs at/above the "
                        "threshold are then treated CONSERVATIVELY (keep both).")
    p.add_argument("--cache-dir", default=None,
                   help="Embedding cache directory (default: <output>/cache_stage8)")
    p.add_argument("--model", default=DEFAULT_EMBEDDING_MODEL,
                   help=f"Embedding model (default: {DEFAULT_EMBEDDING_MODEL})")
    p.add_argument("--quiet", action="store_true",
                   help="Reduce log verbosity")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.quiet:
        _LOG.setLevel(logging.WARNING)

    # Resolve output dir / file
    output_arg = args.output
    if output_arg is None:
        output_dir = Path.cwd()
    else:
        output_path = Path(output_arg)
        if output_path.suffix.lower() == ".json":
            output_dir = output_path.parent
        else:
            output_dir = output_path

    payload = semantic_deduplicate(
        stage7_output_path=args.input,
        enriched_corpus_path=args.corpus,
        output_dir=output_dir,
        similarity_threshold=args.threshold,
        embedding_model_name=args.model,
        llm_judge_enabled=not args.no_llm_judge,
        cache_dir=args.cache_dir,
    )

    # If --output was a specific .json path different from the canonical,
    # also copy the payload there for convenience.
    if output_arg is not None:
        output_path = Path(output_arg)
        if output_path.suffix.lower() == ".json":
            canonical = output_dir / "stage8_semantic_dedup.json"
            if output_path.resolve() != canonical.resolve():
                _write_json(output_path, payload)

    print(
        f"[stage8] input={payload['input_count']} "
        f"output={payload['output_count']} "
        f"removed={payload['removed_count']} "
        f"threshold={payload['threshold']} "
        f"duration={payload['duration_seconds']}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
