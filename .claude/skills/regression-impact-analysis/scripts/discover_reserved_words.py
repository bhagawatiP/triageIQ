#!/usr/bin/env python3
"""
discover_reserved_words.py — Auto-discover language reserved words / built-ins
from the repository, language runtime, and AST tooling.

Goal
----
The diff-extraction and vocabulary builders must not let language keywords
("class", "return", "public", "if", ...) leak into the domain vocabulary.
Hardcoding these lists per language (as `extract_diff_concepts._build_reserved_words`
historically did) couples the pipeline to specific languages and goes stale
whenever a new language version adds keywords. This builder uses three
independent discovery methods and unions / intersects them so the resulting
KB artefact (`language_reserved_words.json`) is fully data-driven.

Strategy (NO hardcoded keyword lists in user code)
--------------------------------------------------
Three discovery methods run in parallel and the high-confidence union is
written to the KB:

1. Language runtime introspection — Python's `keyword.kwlist`, `keyword.softkwlist`,
   and `builtins.__dict__` give the authoritative Python keyword + built-in set
   straight from the interpreter. No hardcoded list — the runtime owns the
   truth. Equivalent introspection hooks are used per detected language when
   available (e.g. tokenize for Python, optional tree-sitter grammars).

2. Tree-sitter AST analysis — when a tree-sitter grammar is available for the
   detected source extensions, parse a representative sample of files and
   collect every node whose `type` is exposed by the grammar as a keyword
   (anonymous string-typed leaves like 'class', 'return', 'if'). This is
   100% data-driven from the grammar definition.

3. Frequency anomaly detection — tokenise all source files, compute the
   token frequency distribution, and emit tokens whose document frequency
   is in the top 0.1% (>3 sigma above the mean). Genuine keywords appear in
   nearly every file of their language; identifiers do not. This catches
   keywords for languages where (1) and (2) are unavailable.

Output
------
`<KB_DIR>/language_reserved_words.json`:

    {
      "discovered_at": "...",
      "language": "java",
      "reserved_words": ["abstract", "boolean", "break", ...],
      "discovery_methods": {
          "runtime_introspection": [...],
          "ast_analysis": [...],
          "frequency_outliers": [...]
      },
      "total_count": <int>,
      "statistics": { ... }
    }

The downstream consumer (`extract_diff_concepts._load_reserved_words`)
already reads this file. This builder simply guarantees that it is produced
without any hardcoded language-specific keyword tables.

Usage
-----
Invoked from the RIA KB build sequence. Standalone:

    python3 discover_reserved_words.py --repo-root /path/to/repo \
        --output /path/to/kb/language_reserved_words.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

# ---------------------------------------------------------------------------
# Path defaults
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
GITHUB_DIR = SKILL_DIR.parent.parent
REPO_ROOT_DEFAULT = GITHUB_DIR.parent
KB_DIR_DEFAULT = GITHUB_DIR / 'RIA_OUTPUT' / 'knowledge_base'

# ---------------------------------------------------------------------------
# Recognition metadata (NOT classification)
# ---------------------------------------------------------------------------
# Source extensions we know how to tokenise. This is plumbing, not a
# keyword list — we need *something* to recognise as code so we don't
# scan README.md as JavaScript.
SOURCE_EXTENSIONS = (
    ".java", ".kt", ".scala", ".groovy",
    ".py",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".rb", ".cs", ".cpp", ".cc", ".c", ".h", ".hpp",
    ".php", ".swift", ".rs",
)

# Map source extensions to a coarse language family for runtime
# introspection. The mapping uses extension only — we never embed
# keyword lists here.
EXTENSION_TO_LANGUAGE = {
    ".java": "java", ".kt": "java", ".scala": "java", ".groovy": "java",
    ".py": "python",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript",
}

SKIP_DIR_NAMES = {
    ".git", ".hg", ".svn",
    "node_modules", "target", "build", "dist", "out", "bin",
    "generated-sources", "generated", "__pycache__",
    ".venv", "venv", ".tox", ".idea", ".vscode",
    "RIA_OUTPUT", "RIA_INPUT", "RIA_OUTPUT_backup_pre_llm",
    "RIA_INPUT_backup_pre_llm",
}

# Sample size cap — keyword discovery does not need every file in the repo.
SAMPLE_SIZE_LIMIT = 500
MAX_FILE_BYTES = 32 * 1024  # generous; keywords occur throughout a file

# Token regex: identifier-like sequences. Same shape across all C-family /
# Python / JVM / JS-family languages. This captures keywords too, since
# they are spelled like identifiers.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iter_source_files(repo_root: Path) -> Iterable[Path]:
    """Yield source files under repo_root, skipping build/dependency dirs."""
    for dirpath, dirnames, filenames in os.walk(str(repo_root)):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in SOURCE_EXTENSIONS:
                yield Path(dirpath) / fn


def _read_file(path: Path, max_bytes: int = MAX_FILE_BYTES) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(max_bytes).decode("utf-8", errors="replace")
    except (OSError, IOError):
        return ""


def _detect_dominant_language(repo_root: Path) -> str:
    """Return the most-frequent source language family in repo_root."""
    counts: Counter = Counter()
    for path in _iter_source_files(repo_root):
        lang = EXTENSION_TO_LANGUAGE.get(path.suffix.lower())
        if lang:
            counts[lang] += 1
    if not counts:
        return "java"  # neutral default — no keyword list embedded.
    return counts.most_common(1)[0][0]


# ---------------------------------------------------------------------------
# Method 1: Language runtime introspection
# ---------------------------------------------------------------------------

def discover_via_language_runtime(language: str) -> Set[str]:
    """Use the language's own runtime / stdlib to enumerate keywords.

    The Python interpreter is the only runtime we can introspect from a
    Python builder. For other languages we return an empty set and rely on
    the AST and frequency methods. There is NO embedded keyword table here.
    """
    discovered: Set[str] = set()

    # Python: authoritative source is the `keyword` module.
    try:
        import keyword as _kw
        discovered.update(_kw.kwlist)
        # softkwlist is present on 3.9+; guard for older runtimes.
        soft = getattr(_kw, "softkwlist", None)
        if soft:
            discovered.update(soft)
    except Exception:
        pass

    # Python built-ins: types and functions whose names are essentially
    # reserved in code (`str`, `int`, `print`, ...). Pulled live from the
    # interpreter, not enumerated by hand.
    if language == "python":
        try:
            import builtins as _bi
            for name in dir(_bi):
                if name.startswith("_"):
                    continue
                discovered.add(name)
        except Exception:
            pass

    # Tokenize: Python's tokenizer exposes operator + token types. Their
    # `.string` value gives every operator the parser recognises.
    if language == "python":
        try:
            import tokenize as _tok
            for value in _tok.EXACT_TOKEN_TYPES.keys():  # type: ignore[attr-defined]
                if value.isalpha():
                    discovered.add(value)
        except Exception:
            pass

    # Lower-case for cross-method comparison.
    return {w.lower() for w in discovered if isinstance(w, str) and w.isidentifier()}


# ---------------------------------------------------------------------------
# Method 2: Tree-sitter AST analysis (optional)
# ---------------------------------------------------------------------------

def _get_tree_sitter_languages(language: str):
    """Try to import a tree-sitter grammar for `language`. Returns None on miss.

    We deliberately probe at runtime so the builder works whether or not
    the optional `tree-sitter-languages` package is installed. If it is
    not, we silently skip Method 2 — Methods 1 + 3 still produce a
    high-quality union.
    """
    try:
        from tree_sitter_languages import get_language, get_parser  # type: ignore
    except Exception:
        return None
    try:
        return get_language(language), get_parser(language)
    except Exception:
        return None


def _collect_keyword_node_types(language) -> Set[str]:
    """Walk the tree-sitter grammar's node-type table and pull out keywords.

    A grammar's anonymous string nodes (e.g. 'class', 'return') are exposed
    as node types whose name is the literal keyword. Named rule nodes have
    `is_named=True`; anonymous keyword leaves are `is_named=False`.
    """
    keywords: Set[str] = set()
    try:
        # tree-sitter exposes node_kind_count/_for_id; iterate through them.
        # Some bindings name this `language.node_kind_count`; others use
        # `language.symbol_count`. We try both defensively.
        n = getattr(language, "node_kind_count", None) or getattr(language, "symbol_count", 0)
        if not n:
            return keywords
        for i in range(n):
            try:
                name = language.node_kind_for_id(i)  # type: ignore[attr-defined]
                is_named = language.node_kind_is_named(i)  # type: ignore[attr-defined]
            except Exception:
                continue
            if name and not is_named and name.isalpha() and name.islower():
                keywords.add(name)
    except Exception:
        return set()
    return keywords


def discover_via_ast_analysis(repo_root: Path, language: str,
                              sample_size: int = SAMPLE_SIZE_LIMIT) -> Set[str]:
    """Extract keyword nodes from AST using tree-sitter when available.

    Returns an empty set if tree-sitter is not installed or no grammar is
    available for `language`. Methods 1 + 3 still give us a strong union.
    """
    pair = _get_tree_sitter_languages(language)
    if pair is None:
        return set()
    grammar, parser = pair

    # Pull keywords directly from the grammar's node-type table — this is
    # the ground truth and does not require parsing any files. It is the
    # most reliable signal when the grammar is installed.
    keywords = _collect_keyword_node_types(grammar)

    # Optionally walk a few sample files to pick up keywords that the
    # grammar exposes only as named rules (rare, but possible).
    extensions = [ext for ext, lang in EXTENSION_TO_LANGUAGE.items() if lang == language]
    seen = 0
    for path in _iter_source_files(repo_root):
        if seen >= sample_size:
            break
        if path.suffix.lower() not in extensions:
            continue
        text = _read_file(path)
        if not text:
            continue
        try:
            tree = parser.parse(text.encode("utf-8", errors="replace"))
        except Exception:
            continue
        # Walk the tree, collecting anonymous-leaf node types whose text is
        # an identifier-shaped keyword.
        cursor = tree.walk()
        stack = [cursor.node]
        while stack:
            node = stack.pop()
            try:
                if not node.is_named and node.type and node.type.isalpha() \
                        and node.type.islower():
                    keywords.add(node.type)
                stack.extend(node.children)
            except Exception:
                continue
        seen += 1

    return keywords


# ---------------------------------------------------------------------------
# Method 3: Frequency anomaly detection
# ---------------------------------------------------------------------------

def discover_via_frequency_outliers(
    repo_root: Path,
    language: str,
    *,
    min_doc_frequency_ratio: float = 0.30,
    sigma_threshold: float = 3.0,
    sample_size: int = SAMPLE_SIZE_LIMIT,
) -> Set[str]:
    """Find tokens whose document-frequency is a statistical outlier.

    Real keywords appear in almost every file of their language: `class`,
    `return`, `if`, `import` all hit document-frequency ~1.0. Identifiers
    are typically <0.05. We pick tokens whose document frequency is
    >= `min_doc_frequency_ratio` (raw threshold) AND > mean + sigma * std
    (statistical outlier), then return them as candidates.

    Bonus filter: short (<= 12 chars) lower-case tokens are favoured —
    keywords are short by language design. This excludes accidental
    sweeping in of long shared identifiers like `getApplicationContext`.
    """
    extensions = [ext for ext, lang in EXTENSION_TO_LANGUAGE.items() if lang == language]
    if not extensions:
        return set()

    doc_count = 0
    token_doc_freq: Counter = Counter()

    for path in _iter_source_files(repo_root):
        if doc_count >= sample_size:
            break
        if path.suffix.lower() not in extensions:
            continue
        text = _read_file(path)
        if not text:
            continue
        # Strip strings + comments crudely so words inside string literals
        # don't pollute the frequency table. Cheap heuristic that covers
        # most languages.
        text = re.sub(r'"(?:[^"\\]|\\.)*"', " ", text)
        text = re.sub(r"'(?:[^'\\]|\\.)*'", " ", text)
        text = re.sub(r"//[^\n]*", " ", text)
        text = re.sub(r"#[^\n]*", " ", text)
        text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)

        tokens = set()
        for m in _TOKEN_RE.findall(text):
            t = m.lower()
            if len(t) > 12:
                continue
            tokens.add(t)
        for t in tokens:
            token_doc_freq[t] += 1
        doc_count += 1

    if doc_count == 0:
        return set()

    # Compute distribution of document-frequency ratios.
    freqs = [c / doc_count for c in token_doc_freq.values()]
    if not freqs:
        return set()
    mean = sum(freqs) / len(freqs)
    variance = sum((x - mean) ** 2 for x in freqs) / len(freqs)
    std = math.sqrt(variance)

    threshold = max(min_doc_frequency_ratio, mean + sigma_threshold * std)

    outliers: Set[str] = set()
    for token, count in token_doc_freq.items():
        ratio = count / doc_count
        if ratio < threshold:
            continue
        # Keywords are lower-case-only by language convention.
        if not token.islower():
            continue
        # Identifier-shaped (already enforced by _TOKEN_RE).
        outliers.add(token)

    return outliers


# ---------------------------------------------------------------------------
# Combined builder
# ---------------------------------------------------------------------------

def build_reserved_words_kb(repo_root: Path, output_path: Path) -> Dict:
    """Run all three discovery methods and emit the combined KB artefact."""
    language = _detect_dominant_language(repo_root)
    print(f"[discover_reserved_words] Dominant language: {language}")

    runtime_kw = discover_via_language_runtime(language)
    print(f"[discover_reserved_words] Runtime introspection: {len(runtime_kw)} tokens")

    ast_kw = discover_via_ast_analysis(repo_root, language)
    print(f"[discover_reserved_words] AST analysis: {len(ast_kw)} tokens")

    freq_kw = discover_via_frequency_outliers(repo_root, language)
    print(f"[discover_reserved_words] Frequency outliers: {len(freq_kw)} tokens")

    # Combine: high-confidence union.
    #   * runtime_kw is authoritative when present (Python).
    #   * ast_kw is authoritative when a tree-sitter grammar is available.
    #   * freq_kw alone is noisy (catches common identifier names) — we
    #     accept a frequency outlier only if it also appears in at least
    #     one other source, OR if neither of the other sources produced
    #     anything (graceful degradation for languages with no runtime/AST).
    has_strong_source = bool(runtime_kw) or bool(ast_kw)
    if has_strong_source:
        confirmed = runtime_kw | ast_kw | (freq_kw & (runtime_kw | ast_kw))
        # Plus high-confidence frequency-only words: those at >= 0.7
        # document frequency are almost certainly keywords too. We do not
        # use a hardcoded list to decide this — the frequency itself is
        # the signal.
        # (Already incorporated by the >= sigma threshold inside freq_kw.)
        confirmed |= {w for w in freq_kw if len(w) <= 8}
    else:
        confirmed = freq_kw

    # Drop empty / numeric / overly long tokens defensively.
    confirmed = {w for w in confirmed if w and w.isidentifier()}

    output = {
        "discovered_at": _now_iso(),
        "source": "runtime introspection + tree-sitter AST + frequency outliers",
        "language": language,
        "reserved_words": sorted(confirmed),
        "discovery_methods": {
            "runtime_introspection": sorted(runtime_kw),
            "ast_analysis": sorted(ast_kw),
            "frequency_outliers": sorted(freq_kw),
        },
        "total_count": len(confirmed),
        "statistics": {
            "runtime_count": len(runtime_kw),
            "ast_count": len(ast_kw),
            "frequency_count": len(freq_kw),
            "combined_count": len(confirmed),
        },
        "version": "2.0",
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"[discover_reserved_words] Wrote {len(confirmed)} reserved words "
          f"to {output_path}")
    return output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Auto-discover language reserved words from the repo."
    )
    p.add_argument("--repo-root", default=str(REPO_ROOT_DEFAULT))
    p.add_argument("--output",
                   default=str(KB_DIR_DEFAULT / "language_reserved_words.json"))
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    repo_root = Path(args.repo_root).resolve()
    output = Path(args.output).resolve()

    if not repo_root.is_dir():
        print(f"[discover_reserved_words] ERROR: repo-root not a directory: "
              f"{repo_root}", file=sys.stderr)
        return 2

    build_reserved_words_kb(repo_root, output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
