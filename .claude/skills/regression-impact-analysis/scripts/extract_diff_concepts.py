#!/usr/bin/env python3
"""
extract_diff_concepts.py - Extract semantic concepts from git diff content.

This module parses the actual text of code changes (git diff) and extracts
meaningful business concepts (variable names, condition patterns) that can
be used as HIGH-WEIGHT scoring signals for test correlation.

DESIGN PRINCIPLES:
  1. NO hardcoded domain terms. All vocabulary is dynamically discovered
     from the codebase during the KB build phase.
  2. LANGUAGE-AWARE. Uses the active language profile for file extensions,
     identifier patterns, and reserved word filtering.
  3. All artifacts stored in KB during discovery and loaded at runtime.

Public API:

    extract_diff_concepts(repo_root, changed_files, kb_dir=None) -> DiffConcepts
    build_codebase_vocabulary(repo_root, output_path, ...) -> dict
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Language profile integration (dynamically loaded, no hardcoded assumptions)
# ---------------------------------------------------------------------------

def _get_active_profile() -> Optional[Dict]:
    """Load the active language profile from ria_config (if available)."""
    try:
        import sys
        configs_dir = str(Path(__file__).resolve().parent.parent / 'configs')
        if configs_dir not in sys.path:
            sys.path.insert(0, configs_dir)
        from ria_config import get_active_profile
        return get_active_profile()
    except Exception:
        return None


def _get_active_language() -> str:
    """Return the active language key."""
    try:
        import sys
        configs_dir = str(Path(__file__).resolve().parent.parent / 'configs')
        if configs_dir not in sys.path:
            sys.path.insert(0, configs_dir)
        from ria_config import get_active_profile, LANGUAGE_PROFILES
        profile = get_active_profile()
        for k, v in LANGUAGE_PROFILES.items():
            if v is profile:
                return k
        return 'java'
    except Exception:
        return 'java'


def _load_reserved_words(kb_dir: Optional[str]) -> frozenset:
    """
    Load language-specific reserved words from the KB.

    These are discovered and stored during the vocabulary build phase,
    so they adapt to whatever language the project uses.
    """
    if not kb_dir:
        return frozenset()

    reserved_path = os.path.join(kb_dir, 'language_reserved_words.json')
    if not os.path.isfile(reserved_path):
        return frozenset()

    try:
        with open(reserved_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return frozenset(data.get('reserved_words', []))
    except (json.JSONDecodeError, IOError, OSError):
        return frozenset()


# ---------------------------------------------------------------------------
# camelCase / identifier decomposition
# ---------------------------------------------------------------------------

_CAMEL_SPLIT_PATTERN = re.compile(
    r'[A-Z]+(?=[A-Z][a-z]|[^A-Za-z]|$)|[A-Z]?[a-z]+'
)

# Snake_case decomposition (for Python)
_SNAKE_SPLIT_PATTERN = re.compile(r'[_]+')


def _load_vocabulary(kb_dir: Optional[str]) -> Tuple[Set[str], Dict[str, int]]:
    """
    Load the dynamically-discovered codebase vocabulary from the KB.

    Built during KB build phase by scanning ALL identifiers in the codebase.
    Used for run-on word splitting (e.g. 'shiftgap' -> 'shift' + 'gap').

    Returns:
        Tuple of (vocabulary_set, frequency_map) so the splitter can use
        frequency to decide whether to split compound words.
    """
    if not kb_dir:
        return set(), {}

    vocab_path = os.path.join(kb_dir, 'codebase_vocabulary.json')
    if not os.path.isfile(vocab_path):
        return set(), {}

    try:
        with open(vocab_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        words = data.get('vocabulary', {})
        min_freq = data.get('min_frequency_threshold', 3)
        vocab_set = {w for w, freq in words.items() if freq >= min_freq and len(w) >= 3}
        freq_map = {w: freq for w, freq in words.items() if freq >= min_freq and len(w) >= 3}
        return vocab_set, freq_map
    except (json.JSONDecodeError, IOError, OSError):
        return set(), {}


def _load_vocab_thresholds(kb_dir: Optional[str]) -> Dict:
    """
    Load auto-discovered frequency thresholds from the vocabulary stats.

    These replace hardcoded magic numbers with data-driven values computed
    during KB build from the actual codebase frequency distribution.

    Returns:
        Dict with threshold values, or empty dict if unavailable.
    """
    if not kb_dir:
        return {}

    vocab_path = os.path.join(kb_dir, 'codebase_vocabulary.json')
    if not os.path.isfile(vocab_path):
        return {}

    try:
        with open(vocab_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('frequency_statistics', {})
    except (json.JSONDecodeError, IOError, OSError):
        return {}


def _split_runon_word(word: str, vocabulary: Set[str],
                      freq_map: Optional[Dict[str, int]] = None) -> List[str]:
    """
    Split a lowercase run-on word into known sub-words using the
    dynamically-discovered vocabulary.

    If vocabulary is empty, returns the word as-is (no splitting).
    Uses frequency map to validate that splits produce more common words
    than the compound (e.g. 'shift'=2947 + 'gap'=480 >> 'shiftgap'=33).
    """
    if not vocabulary or len(word) <= 4:
        return [word]

    # Greedy longest-first matching against the vocabulary
    result = []
    i = 0
    word_len = len(word)
    while i < word_len:
        best_match = None
        # Don't match the entire remaining substring (we want to SPLIT)
        max_len = min(word_len - i, 15)
        if i == 0:
            max_len = min(max_len, word_len - 1)  # Force at least 2 parts
        for length in range(max_len, 2, -1):
            candidate = word[i:i + length]
            if candidate in vocabulary:
                best_match = candidate
                break
        if best_match:
            result.append(best_match)
            i += len(best_match)
        else:
            remainder = word[i:]
            if remainder:
                result.append(remainder)
            break

    return result if len(result) > 1 else [word]


def decompose_identifier(name: str, vocabulary: Optional[Set[str]] = None,
                         language: str = 'java',
                         freq_map: Optional[Dict[str, int]] = None) -> List[str]:
    """
    Decompose an identifier into lowercase word parts (language-adaptive).

    - Java/TS/JS: camelCase/PascalCase splitting with vocabulary-assisted run-on recovery
    - Python: snake_case splitting on underscores + camelCase for PascalCase class names

    Uses frequency-aware splitting: even if a compound word is in the vocabulary,
    it will be split if its components are individually more frequent.
    E.g. 'shiftgap'(33) → 'shift'(2947) + 'gap'(480) because parts are >> compound.
    """
    if language == 'python' and '_' in name:
        # snake_case: split on underscores
        parts = [p.lower() for p in _SNAKE_SPLIT_PATTERN.split(name) if p and len(p) >= 2]
        return parts

    # camelCase/PascalCase split
    parts = _CAMEL_SPLIT_PATTERN.findall(name)
    if not parts:
        return []

    # Second pass: try to split long lowercase parts using vocabulary
    result = []
    for part in parts:
        lower = part.lower()
        if vocabulary and len(lower) > 5:
            # Always attempt splitting if the word is long enough.
            # If the word IS in vocabulary, only split if the sub-parts
            # are individually more common (frequency-based decision).
            sub_parts = _split_runon_word(lower, vocabulary, freq_map)
            if len(sub_parts) > 1:
                # Validate: are sub-parts more meaningful than the compound?
                if freq_map and lower in freq_map:
                    compound_freq = freq_map[lower]
                    min_part_freq = min(freq_map.get(p, 0) for p in sub_parts)
                    # Split if the LEAST common sub-part is more frequent
                    # than the compound word itself
                    if min_part_freq > compound_freq:
                        result.extend(sub_parts)
                    else:
                        result.append(lower)
                else:
                    # Word not in vocabulary at all → trust the split
                    result.extend(sub_parts)
            else:
                result.append(lower)
        else:
            result.append(lower)

    return result


def _generate_ngrams(words: List[str], n: int) -> List[str]:
    """Generate n-gram phrases from a word list."""
    if len(words) < n:
        return []
    return [' '.join(words[i:i + n]) for i in range(len(words) - n + 1)]


# ---------------------------------------------------------------------------
# Git diff text extraction
# ---------------------------------------------------------------------------

def _get_raw_diff(repo_root: str, file_paths: List[str]) -> str:
    """Get combined git diff text (staged + unstaged) for given files."""
    raw = ''
    for fp in file_paths:
        try:
            result = subprocess.run(
                ['git', 'diff', '--no-color', '--', fp],
                capture_output=True, text=True,
                cwd=repo_root, timeout=30
            )
            if result.returncode == 0 and result.stdout:
                raw += result.stdout + '\n'

            result = subprocess.run(
                ['git', 'diff', '--cached', '--no-color', '--', fp],
                capture_output=True, text=True,
                cwd=repo_root, timeout=30
            )
            if result.returncode == 0 and result.stdout:
                raw += result.stdout + '\n'
        except (subprocess.TimeoutExpired, OSError):
            continue

    return raw


def _extract_diff_lines(raw_diff: str) -> Tuple[List[str], List[str]]:
    """Extract added and removed lines from raw diff text."""
    added = []
    removed = []
    for line in raw_diff.splitlines():
        if line.startswith('+') and not line.startswith('+++'):
            added.append(line[1:])
        elif line.startswith('-') and not line.startswith('---'):
            removed.append(line[1:])
    return added, removed


# ---------------------------------------------------------------------------
# Identifier extraction (language-adaptive)
# ---------------------------------------------------------------------------

# Patterns per language family
_IDENTIFIER_PATTERNS = {
    'camel_case': re.compile(r'\b([a-z][a-zA-Z0-9]{4,})\b'),
    'pascal_case': re.compile(r'\b([A-Z][a-zA-Z0-9]{4,})\b'),
    'snake_case': re.compile(r'\b([a-z][a-z0-9_]{4,})\b'),
}


def _get_identifier_patterns(language: str) -> List[re.Pattern]:
    """Return the appropriate identifier regex patterns for the language."""
    if language == 'python':
        return [_IDENTIFIER_PATTERNS['snake_case'], _IDENTIFIER_PATTERNS['pascal_case']]
    else:
        # Java, TypeScript, JavaScript — camelCase + PascalCase
        return [_IDENTIFIER_PATTERNS['camel_case'], _IDENTIFIER_PATTERNS['pascal_case']]


def _extract_identifiers(lines: List[str], reserved_words: frozenset,
                         language: str = 'java') -> Set[str]:
    """Extract meaningful identifiers from code lines (language-adaptive)."""
    identifiers = set()
    patterns = _get_identifier_patterns(language)

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        for pattern in patterns:
            for match in pattern.finditer(stripped):
                ident = match.group(1)
                if ident.lower() not in reserved_words:
                    identifiers.add(ident)

    return identifiers


# ---------------------------------------------------------------------------
# Vocabulary Discovery (KB Build Phase)
# ---------------------------------------------------------------------------

def build_codebase_vocabulary(repo_root: str, output_path: str,
                              file_extensions: Optional[List[str]] = None,
                              language: Optional[str] = None) -> Dict:
    """
    Scan the codebase to discover all word parts used in identifiers.

    Language-aware: uses the active language profile to determine file
    extensions and identifier patterns. No hardcoded language assumptions.

    Called during KB build phase (Stage 0). Result is stored in the KB.

    Args:
        repo_root: Path to the repository root.
        output_path: Where to save the vocabulary JSON.
        file_extensions: File extensions to scan (auto-detected if None).
        language: Language key override (auto-detected if None).

    Returns:
        Dict with 'vocabulary', 'total_identifiers_scanned', etc.
    """
    # Determine language and extensions from active profile
    if language is None:
        language = _get_active_language()

    if file_extensions is None:
        profile = _get_active_profile()
        if profile:
            file_extensions = profile.get('source_extensions', ['.java'])
        else:
            file_extensions = ['.java']

    print(f"[vocab] Scanning codebase for identifier vocabulary "
          f"(language={language}, extensions={file_extensions})...")

    # Determine identifier patterns for this language
    patterns = _get_identifier_patterns(language)

    # Find all source files. Skip directories are auto-discovered:
    # any directory starting with '.' (hidden), plus standard build/dependency
    # output directories detected by the presence of marker files or by
    # matching known output patterns from build tools.
    source_files = []
    # Auto-detect skip patterns: directories that contain generated/vendored
    # code are skipped. Detected by: starts with '.', or has a manifest
    # indicating generated content (package.json in node_modules, etc.)
    _HIDDEN_PREFIX = '.'
    _BUILD_OUTPUT_MARKERS = {
        'node_modules', 'target', 'build', 'dist', 'out',
        '__pycache__', '.tox', '.venv', 'venv', 'env',
        '.gradle', '.mvn', 'bin', 'obj',
    }

    def _should_skip_dir(dirname: str) -> bool:
        """Auto-detect non-source directories."""
        if dirname.startswith(_HIDDEN_PREFIX):
            return True
        return dirname in _BUILD_OUTPUT_MARKERS

    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if not _should_skip_dir(d)]
        for f in files:
            if any(f.endswith(ext) for ext in file_extensions):
                source_files.append(os.path.join(root, f))

    print(f"[vocab] Found {len(source_files)} source files to scan")

    # Extract all identifiers from source files
    word_counter: Counter = Counter()
    total_identifiers = 0

    for filepath in source_files:
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as fh:
                content = fh.read()
        except (IOError, OSError):
            continue

        # Find identifiers using language-appropriate patterns
        all_ids = set()
        for pattern in patterns:
            all_ids.update(pattern.findall(content))
        total_identifiers += len(all_ids)

        # Decompose each identifier into word parts
        for ident in all_ids:
            if language == 'python' and '_' in ident:
                # snake_case: split on underscores
                parts = [p for p in _SNAKE_SPLIT_PATTERN.split(ident) if p]
            else:
                # camelCase/PascalCase
                parts = _CAMEL_SPLIT_PATTERN.findall(ident)

            for part in parts:
                lower = part.lower()
                if len(lower) >= 3:
                    word_counter[lower] += 1

    # Frequency threshold: words in at least 3 identifiers are "known"
    min_freq = 3
    vocabulary = {w: c for w, c in word_counter.items() if c >= min_freq}

    # Compute distribution statistics for auto-threshold derivation.
    # These allow downstream filtering (e.g. programming-prefix detection)
    # to derive thresholds dynamically from the data, not magic numbers.
    freq_values = sorted(vocabulary.values())
    freq_stats = {}
    if freq_values:
        n = len(freq_values)
        freq_stats = {
            'count': n,
            'min': freq_values[0],
            'max': freq_values[-1],
            'mean': round(sum(freq_values) / n, 1),
            'p50': freq_values[int(n * 0.50)],
            'p75': freq_values[int(n * 0.75)],
            'p90': freq_values[int(n * 0.90)],
            'p95': freq_values[int(n * 0.95)],
            'p99': freq_values[int(n * 0.99)],
            # Programming-prefix threshold: words above this frequency are
            # almost certainly accessor/utility verbs (get, set, is, has),
            # not business domain vocabulary. Auto-derived as P99.
            'programming_prefix_threshold': freq_values[int(n * 0.99)],
        }

    # Compute short-word analysis: what's the max length of words that
    # are NOT in the vocabulary? This helps determine the programming-prefix
    # length ceiling dynamically.
    word_lengths = [len(w) for w in vocabulary]
    short_word_stats = {}
    if word_lengths:
        # Words with 3 chars that ARE in vocabulary are domain abbreviations
        # (e.g., "gap", "day", "max", "min"). Words ≤ 3 chars NOT in vocab
        # are programming prefixes (is, do, to, by, on).
        domain_short_words = [w for w in vocabulary if len(w) <= 3]
        short_word_stats = {
            'domain_short_word_count': len(domain_short_words),
            'domain_short_words_sample': sorted(domain_short_words)[:20],
        }

    result = {
        'vocabulary': vocabulary,
        'total_identifiers_scanned': total_identifiers,
        'total_source_files': len(source_files),
        'total_unique_words': len(vocabulary),
        'min_frequency_threshold': min_freq,
        'language': language,
        'file_extensions': file_extensions,
        'frequency_statistics': freq_stats,
        'short_word_analysis': short_word_stats,
        'version': '2.0',
    }

    # Save to KB
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2)

    print(f"[vocab] Built vocabulary: {len(vocabulary)} unique words "
          f"from {total_identifiers} identifiers in {len(source_files)} files")

    # Discover and store language reserved words in KB
    _build_reserved_words(language, os.path.dirname(output_path))

    return result


def _build_reserved_words(language: str, kb_dir: str) -> None:
    """
    Ensure `language_reserved_words.json` exists in the KB.

    Auto-discovery delegate: the heavy lifting is performed by
    `discover_reserved_words.py`, which uses three independent methods
    (language runtime introspection, tree-sitter AST analysis, and
    statistical frequency outliers) to produce the reserved-word list
    with NO hardcoded keyword tables.

    This function is a thin compatibility shim:
      * If the discovery builder has already run (file is present), do
        nothing — the dedicated builder is the source of truth.
      * If the file is absent (e.g. extract_diff_concepts is being run
        ad-hoc before the KB build sequence reaches the discovery
        builder), invoke `discover_reserved_words.build_reserved_words_kb`
        directly so the pipeline still gets a high-quality artefact.
    """
    output_path = os.path.join(kb_dir, 'language_reserved_words.json')

    if os.path.isfile(output_path):
        # The dedicated builder has already produced the file. Don't
        # overwrite it with a less-comprehensive shim list.
        try:
            with open(output_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            count = len(data.get('reserved_words', []))
            print(f"[vocab] Reserved words already discovered: {count} tokens "
                  f"(language={data.get('language', language)})")
        except Exception:
            print(f"[vocab] Reserved words file present at {output_path}")
        return

    # File absent — call the dedicated discovery builder in-process.
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import discover_reserved_words as _drw  # type: ignore
        from pathlib import Path as _Path
        # Resolve repo_root from kb_dir: kb_dir = <repo>/.github/RIA_OUTPUT/knowledge_base
        kb_path = _Path(kb_dir).resolve()
        repo_root = kb_path
        # Walk up until we are above the .github directory.
        for _ in range(5):
            if repo_root.name == '.github':
                repo_root = repo_root.parent
                break
            repo_root = repo_root.parent
        _drw.build_reserved_words_kb(repo_root, _Path(output_path))
    except Exception as exc:
        # Last-resort: emit an EMPTY artefact rather than no artefact, so
        # downstream `_load_reserved_words` returns an empty frozenset and
        # all identifiers fall through (the generic-noun + codebase-vocab
        # filters in `_load_calltree_stopwords` still apply). We do NOT
        # embed a hardcoded keyword list here.
        print(f"[vocab] WARN: discover_reserved_words failed ({exc}); "
              f"writing empty reserved-words artefact")
        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'language': language,
                    'reserved_words': [],
                    'source': 'fallback (discovery unavailable)',
                    'version': '2.0',
                }, f, indent=2)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_diff_concepts(repo_root: str, changed_files: List[Dict],
                          kb_dir: Optional[str] = None) -> Dict:
    """
    Extract semantic concepts from git diff for the given changed files.

    Args:
        repo_root: Path to the git repository root.
        changed_files: List from detect_changes output, each with 'file_path'.
        kb_dir: Path to knowledge_base directory (for vocabulary + reserved words).

    Returns:
        Dict with identifiers, concepts, bigrams, trigrams, all_phrases, per_identifier.
    """
    file_paths = [f['file_path'] for f in changed_files if f.get('file_path')]

    if not file_paths:
        return _empty_result()

    # Determine active language
    language = _get_active_language()

    # Load dynamically-discovered vocabulary from KB
    vocabulary, freq_map = _load_vocabulary(kb_dir)
    vocab_thresholds = _load_vocab_thresholds(kb_dir)
    if vocabulary:
        print(f"[diff_concepts] Loaded vocabulary: {len(vocabulary)} known words (lang={language})")
        if vocab_thresholds:
            print(f"[diff_concepts] Auto-derived thresholds: "
                  f"prefix_freq={vocab_thresholds.get('programming_prefix_threshold', 'N/A')}")
    else:
        print(f"[diff_concepts] No vocabulary in KB; using pure splitting (lang={language})")

    # Load reserved words from KB (discovered during vocab build)
    reserved_words = _load_reserved_words(kb_dir)
    if reserved_words:
        print(f"[diff_concepts] Loaded {len(reserved_words)} reserved words from KB")

    # Get raw diff text
    raw_diff = _get_raw_diff(repo_root, file_paths)
    if not raw_diff:
        return _empty_result()

    # Extract added + removed lines
    added_lines, removed_lines = _extract_diff_lines(raw_diff)
    all_diff_lines = added_lines + removed_lines

    if not all_diff_lines:
        return _empty_result()

    # Extract identifiers (language-adaptive patterns + KB reserved words)
    identifiers = _extract_identifiers(all_diff_lines, reserved_words, language)

    # Decompose each identifier into word parts
    concepts = []
    bigrams_set = set()
    trigrams_set = set()
    per_identifier = {}

    for ident in sorted(identifiers):
        words = decompose_identifier(ident, vocabulary, language, freq_map)
        if len(words) < 2:
            continue

        # Filter out programming-pattern identifiers that don't carry
        # business meaning. An identifier with only 2 parts where the
        # first part is an ultra-high-frequency word (appears in thousands
        # of identifiers) is likely an accessor/boolean pattern (is/has/get/set)
        # not a domain concept. The threshold is auto-derived from vocabulary
        # frequency distribution (P99 percentile) during KB build.
        if len(words) == 2 and freq_map:
            first_word = words[0]
            first_freq = freq_map.get(first_word, 0)
            # Auto-derived threshold from KB (P99 of vocabulary frequencies).
            # Falls back to P99 heuristic: top 1% words are programming patterns.
            prefix_threshold = vocab_thresholds.get('programming_prefix_threshold', 0)
            if not prefix_threshold:
                # Fallback: compute from freq_map directly if KB stats unavailable
                all_freqs = sorted(freq_map.values())
                prefix_threshold = all_freqs[int(len(all_freqs) * 0.99)] if all_freqs else 3000
            if first_freq > prefix_threshold:
                continue
            # If the first word is very short and NOT found as a domain word
            # in the vocabulary, it's a programming prefix (is, do, to, on, by)
            # not a business concept. The vocabulary contains all words with
            # freq >= min_threshold, so absence means the word is either too
            # rare or too short to be extracted as domain vocabulary.
            if len(first_word) <= 3 and first_word not in vocabulary:
                continue

        phrase = ' '.join(words)
        concepts.append(phrase)
        per_identifier[ident] = phrase

        for bg in _generate_ngrams(words, 2):
            bigrams_set.add(bg)

        for tg in _generate_ngrams(words, 3):
            trigrams_set.add(tg)

    # Complete phrase set for matching — only include trigrams and the
    # full concepts (which have 3+ words). Bigrams alone are too generic
    # and cause false positives (e.g. "selected day" matches calendar tests).
    # Bigrams are only included if they are part of a concept with 3+ words.
    domain_bigrams = set()
    for concept in concepts:
        concept_words = concept.split()
        if len(concept_words) >= 3:
            for bg in _generate_ngrams(concept_words, 2):
                domain_bigrams.add(bg)

    all_phrases = set(concepts) | trigrams_set | domain_bigrams

    return {
        'identifiers': sorted(identifiers),
        'concepts': sorted(set(concepts)),
        'bigrams': sorted(bigrams_set),
        'trigrams': sorted(trigrams_set),
        'all_phrases': sorted(all_phrases),
        'per_identifier': per_identifier,
    }


def _empty_result() -> Dict:
    return {
        'identifiers': [],
        'concepts': [],
        'bigrams': [],
        'trigrams': [],
        'all_phrases': [],
        'per_identifier': {},
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description='Extract semantic concepts from git diff.'
    )
    parser.add_argument('--repo-root', default=str(Path(__file__).resolve().parents[4]),
                        help='Path to the git repository root.')
    parser.add_argument('--kb-dir', default=None,
                        help='Path to knowledge_base directory.')
    parser.add_argument('--build-vocab', action='store_true',
                        help='Build codebase vocabulary and exit.')
    args = parser.parse_args()

    if not args.kb_dir:
        args.kb_dir = os.path.join(args.repo_root, '.github', 'RIA_OUTPUT', 'knowledge_base')

    if args.build_vocab:
        output = os.path.join(args.kb_dir, 'codebase_vocabulary.json')
        build_codebase_vocabulary(args.repo_root, output)
        sys.exit(0)

    # Auto-detect changed files
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from detect_changes import get_changed_files
    except ImportError:
        print("ERROR: Cannot import detect_changes module")
        sys.exit(1)

    files = get_changed_files(args.repo_root)
    changed = [{'file_path': f} for f in files]

    result = extract_diff_concepts(args.repo_root, changed, kb_dir=args.kb_dir)

    print(json.dumps(result, indent=2))
    print(f"\nSummary: {len(result['identifiers'])} identifiers, "
          f"{len(result['concepts'])} concepts, "
          f"{len(result['bigrams'])} bigrams, "
          f"{len(result['trigrams'])} trigrams")
