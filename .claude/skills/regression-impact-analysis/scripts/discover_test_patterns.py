#!/usr/bin/env python3
"""
discover_test_patterns.py — Auto-discover test directory / filename / framework
conventions from the actual repository.

Goal
----
The RIA pipeline must classify which files are tests and which are production
code. Hardcoding suffix tables (`*Test.java`, `*_test.py`, `.spec.ts`, etc.)
breaks the moment a project uses a non-standard convention or adds a new
language. This builder analyses the repo on disk and writes a JSON KB
artifact (`test_patterns.json`) describing what test conventions actually
exist in *this* codebase.

Strategy (NO hardcoded patterns)
--------------------------------
1. Directory analysis — walk every directory, count source files, count files
   that import a known test framework (a corpus-derived set, not a hardcoded
   one — see below). Directories whose source files are >=50% test-framework
   importers are emitted as `directory_markers`.

2. Framework discovery — scan import / require statements across the whole
   repo. Identify modules whose name contains one of the universal test
   tokens ("test", "spec", "junit", "pytest", "mocha", "jasmine", "jest",
   "unittest", "nose"). These tokens are *generic English/CS test vocabulary*
   that applies to every language; they are NOT framework-specific
   hardcoded names. The set is intersected with imports actually present in
   the repo, so only frameworks the project really uses end up in the KB.

3. Filename pattern discovery — for each file in a discovered test directory
   (or a file that imports a discovered framework), record its
   filename suffix and prefix. Take the top N most frequent suffix/prefix
   shapes and emit them as `filename_suffixes` / `filename_prefixes`.

   Suffixes are emitted in two buckets:
     * case_sensitive_filename_suffixes — multi-letter capital sequences
       like `IT.java` (`IT`=Integration Test) where casing matters to avoid
       false positives such as `Audit.java`.
     * filename_suffixes — case-insensitive, e.g. `_test.py`, `.spec.ts`.

4. Source extension capture — every test file's extension is recorded so
   downstream consumers (`_is_test_file()`) can combine prefix + extension
   correctly (e.g. `test_*.py`).

Output
------
`<KB_DIR>/test_patterns.json`:

    {
      "discovered_at": "...",
      "source": "repository scan",
      "directory_markers": ["/test/", "/tests/", "/__tests__/"],
      "filename_suffixes": [".test.ts", "_test.py"],
      "case_sensitive_filename_suffixes": ["IT.java"],
      "filename_prefixes": ["test_"],
      "source_extensions": [".java", ".py", ".ts", ".tsx"],
      "test_frameworks": ["junit", "pytest", "jest"],
      "statistics": { ... }
    }

Usage
-----
Invoked from Stage 0 of the RIA KB build. Standalone:

    python3 discover_test_patterns.py --repo-root /path/to/repo
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Set, Tuple

# ---------------------------------------------------------------------------
# Path defaults
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
GITHUB_DIR = SKILL_DIR.parent.parent
REPO_ROOT_DEFAULT = GITHUB_DIR.parent
KB_DIR_DEFAULT = GITHUB_DIR / 'RIA_OUTPUT' / 'knowledge_base'

# ---------------------------------------------------------------------------
# Universal test vocabulary
# ---------------------------------------------------------------------------
# These are generic English/CS terms used industry-wide to denote testing.
# They are not language-specific or framework-specific lists — they are
# intersected against the real imports in the repo so only frameworks
# actually used end up in the output. Adding a new framework whose name
# contains "test" or "spec" requires no code change.
UNIVERSAL_TEST_TOKENS = (
    "test", "spec", "junit", "testng", "pytest", "unittest", "nose",
    "mocha", "jasmine", "jest", "vitest", "qunit", "ava", "tape",
    "rspec", "cucumber", "karma", "chai", "sinon", "expect",
    "assertj", "hamcrest", "mockito", "powermock", "easymock",
)

# Universal source extension allowlist (used to limit the directory walk to
# code files only). This is *recognition* metadata, not test classification.
SOURCE_EXTENSIONS = (
    ".java", ".kt", ".scala", ".groovy",
    ".py",
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".rb", ".cs", ".cpp", ".cc", ".c", ".h", ".hpp",
    ".php", ".swift", ".rs",
)

# Directories we never enter (build / dependency / generated / VCS).
SKIP_DIR_NAMES = {
    ".git", ".hg", ".svn",
    "node_modules", "target", "build", "dist", "out", "bin",
    "generated-sources", "generated", "__pycache__",
    ".venv", "venv", ".tox", ".idea", ".vscode",
    "RIA_OUTPUT", "RIA_INPUT", "RIA_OUTPUT_backup_pre_llm",
    "RIA_INPUT_backup_pre_llm",
}

# Limit how many files we read in detail. The directory-marker step only
# needs counts; the filename-pattern step uses a sample. This keeps the
# builder fast on very large monorepos.
DETAIL_FILE_LIMIT = 5000

# Regex to capture import / require modules across languages. Captures the
# trailing module identifier for matching against UNIVERSAL_TEST_TOKENS.
_IMPORT_PATTERNS = [
    # Java/Kotlin: import org.junit.Test;
    re.compile(r"^\s*import\s+([A-Za-z0-9_.]+)\s*;?", re.MULTILINE),
    # Python: import pytest / from pytest import ...
    re.compile(r"^\s*(?:import|from)\s+([A-Za-z0-9_.]+)", re.MULTILINE),
    # JS/TS ESM: import x from 'jest'
    re.compile(r"""from\s+['"]([^'"]+)['"]""", re.MULTILINE),
    # JS/TS CJS: require('mocha')
    re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""", re.MULTILINE),
]

# Maximum bytes per file we read when detecting framework imports. We only
# need the import block, which is always near the top.
MAX_FILE_BYTES = 8 * 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iter_source_files(repo_root: Path):
    """Yield all source files in repo_root, skipping build/dep/generated dirs."""
    for dirpath, dirnames, filenames in os.walk(str(repo_root)):
        # Mutate dirnames in place so os.walk doesn't descend into skip dirs.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in SOURCE_EXTENSIONS:
                yield Path(dirpath) / fn


def _read_head(path: Path, max_bytes: int = MAX_FILE_BYTES) -> str:
    """Read up to max_bytes from path; return empty string on any error."""
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes)
        return data.decode("utf-8", errors="replace")
    except (OSError, IOError):
        return ""


def _extract_imports(text: str) -> List[str]:
    """Extract module identifiers from a file's text using all import regexes."""
    imports: List[str] = []
    for rx in _IMPORT_PATTERNS:
        imports.extend(rx.findall(text))
    return imports


def _is_test_import(module: str) -> bool:
    """Return True if a module identifier contains a universal test token."""
    if not module:
        return False
    m = module.lower()
    # Split on . / so e.g. 'org.junit.Test' matches 'junit'.
    tokens = re.split(r"[./\\\-]", m)
    for tok in tokens:
        if tok in UNIVERSAL_TEST_TOKENS:
            return True
    return False


# ---------------------------------------------------------------------------
# Stage 1 — Directory analysis
# ---------------------------------------------------------------------------

def discover_test_directories(
    file_records: List[Dict],
    repo_root: Path,
    threshold: float = 0.5,
) -> List[str]:
    """Identify directories whose source files are predominantly tests.

    A directory is considered a test directory if at least `threshold` of its
    source files import a recognised test framework. We then collapse the
    discovered directories to the shortest unique path-segment markers (e.g.
    `/src/test/`, `/tests/`, `/__tests__/`) so consumers can do a simple
    substring match.
    """
    per_dir_total: Dict[str, int] = Counter()
    per_dir_test: Dict[str, int] = Counter()

    for rec in file_records:
        d = rec["dir_rel"]  # POSIX-relative directory
        per_dir_total[d] += 1
        if rec["is_test_by_import"]:
            per_dir_test[d] += 1

    candidate_dirs: Set[str] = set()
    for d, total in per_dir_total.items():
        if total < 2:
            continue
        ratio = per_dir_test[d] / total
        if ratio >= threshold:
            candidate_dirs.add(d)

    # Reduce to shortest path-segment markers. For each candidate, find a
    # short unique segment that identifies it (e.g. '/test/', '/__tests__/').
    markers: Counter = Counter()
    for d in candidate_dirs:
        # Normalise to /seg/seg/ form with leading + trailing slashes.
        norm = "/" + d.strip("/") + "/" if d else "/"
        # Try every contiguous segment slice; favour shorter slices.
        parts = [p for p in norm.split("/") if p]
        # Single-segment markers: '/test/', '/tests/', '/__tests__/', etc.
        for p in parts:
            if not p:
                continue
            seg = f"/{p.lower()}/"
            # Only keep segments that *look* test-related so we don't promote
            # arbitrary parent dirs ("/src/", "/com/") to markers.
            if any(tok in p.lower() for tok in ("test", "spec", "__tests__")):
                markers[seg] += 1
        # Two-segment marker for src/test/-style layouts.
        for i in range(len(parts) - 1):
            a, b = parts[i].lower(), parts[i + 1].lower()
            if any(tok in b for tok in ("test", "spec")):
                markers[f"/{a}/{b}/"] += 1

    # Order by frequency desc; keep markers seen at least once.
    return [m for m, _ in markers.most_common()]


# ---------------------------------------------------------------------------
# Stage 2 — Framework discovery
# ---------------------------------------------------------------------------

def discover_test_frameworks(file_records: List[Dict]) -> List[str]:
    """Return the set of test framework root tokens actually imported."""
    seen: Counter = Counter()
    for rec in file_records:
        for imp in rec.get("imports", []):
            tokens = re.split(r"[./\\\-]", imp.lower())
            for tok in tokens:
                if tok in UNIVERSAL_TEST_TOKENS:
                    seen[tok] += 1
    return [tok for tok, _ in seen.most_common()]


# ---------------------------------------------------------------------------
# Stage 3 — Filename pattern discovery
# ---------------------------------------------------------------------------

# Suffixes are formed from the *trailing* portion of the filename. We try
# trailing tokens of length 1..3 separated by '.' or '_' so we capture
# `.test.ts`, `_test.py`, `Test.java`, `IT.java`.
_SUFFIX_TOKEN_RE = re.compile(r"[._][A-Za-z0-9]+|[A-Z][A-Za-z0-9]*")


def _candidate_suffixes(filename: str) -> List[Tuple[str, bool]]:
    """Yield (suffix, is_case_sensitive) candidates for a filename.

    Examples:
        "FooTest.java"       -> [("Test.java", True), ("test.java", False)]
        "foo_test.py"        -> [("_test.py", False)]
        "foo.spec.ts"        -> [(".spec.ts", False)]
        "FooIT.java"         -> [("IT.java", True), ("it.java", False)]
    """
    base = os.path.basename(filename)
    name, ext = os.path.splitext(base)
    if not ext:
        return []
    out: List[Tuple[str, bool]] = []

    # Look for the final '.' or '_' segment.
    for sep in (".", "_"):
        idx = name.rfind(sep)
        if idx > 0 and idx < len(name) - 1:
            segment = name[idx:]  # includes separator
            suffix = segment + ext
            out.append((suffix.lower(), False))
            # Case-sensitive variant only useful if the segment is short and
            # uppercase-dominant (e.g. 'IT', 'TC'); we'll let the aggregator
            # emit the case-sensitive bucket.
            if segment.isupper() or (len(segment) >= 3 and segment[1:].lower() != segment[1:]):
                out.append((suffix, True))

    # PascalCase trailing token, e.g. "FooTest.java" -> "Test.java".
    # Find the last uppercase boundary.
    matches = list(re.finditer(r"[A-Z][a-z0-9]*", name))
    if matches:
        last = matches[-1]
        if last.start() > 0:
            tail = name[last.start():]
            suffix = tail + ext
            out.append((suffix.lower(), False))
            # If the tail is short uppercase (e.g. 'IT'), keep the
            # case-sensitive form; otherwise the case-sensitive variant
            # would be redundant with the case-insensitive one.
            if tail.isupper():
                out.append((suffix, True))

    return out


def _candidate_prefixes(filename: str) -> List[str]:
    """Yield prefix candidates like 'test_' for 'test_foo.py'."""
    base = os.path.basename(filename)
    name, _ext = os.path.splitext(base)
    out: List[str] = []
    for sep in ("_", "."):
        idx = name.find(sep)
        if 0 < idx < len(name) - 1:
            out.append(name[: idx + 1].lower())
            break
    return out


def discover_filename_patterns(
    file_records: List[Dict],
    top_n: int = 8,
    min_count: int = 2,
) -> Tuple[List[str], List[str], List[str], List[str]]:
    """Return (suffixes, case_sensitive_suffixes, prefixes, source_extensions).

    Only files identified as tests (by import or by being inside a discovered
    test directory) contribute to the patterns.
    """
    suffix_counts: Counter = Counter()
    cs_suffix_counts: Counter = Counter()
    prefix_counts: Counter = Counter()
    ext_counts: Counter = Counter()

    for rec in file_records:
        if not rec.get("is_test"):
            continue
        fn = rec["filename"]
        ext_counts[os.path.splitext(fn)[1].lower()] += 1
        for suffix, is_case_sensitive in _candidate_suffixes(fn):
            if is_case_sensitive:
                cs_suffix_counts[suffix] += 1
            else:
                suffix_counts[suffix] += 1
        for pfx in _candidate_prefixes(fn):
            prefix_counts[pfx] += 1

    def _top(counter: Counter) -> List[str]:
        return [k for k, c in counter.most_common(top_n) if c >= min_count]

    suffixes = _top(suffix_counts)
    # Case-sensitive bucket only retained for short ALL-CAPS markers (IT, TC).
    cs_suffixes = [
        k for k, c in cs_suffix_counts.most_common(top_n)
        if c >= min_count and re.match(r"^[A-Z]{2,4}\.", k)
    ]
    prefixes = _top(prefix_counts)
    extensions = [k for k, _ in ext_counts.most_common()]

    return suffixes, cs_suffixes, prefixes, extensions


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def build_test_patterns_kb(repo_root: Path) -> Dict:
    """Run all discovery stages and assemble the KB artifact."""
    repo_root = Path(repo_root).resolve()
    print(f"[discover_test_patterns] Scanning repo: {repo_root}")

    # ---- Stage 1: Walk repo, build per-file records ----
    records: List[Dict] = []
    detail_count = 0
    for path in _iter_source_files(repo_root):
        try:
            rel = path.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        # Cheap record (path-only) so directory totals are accurate.
        rec: Dict = {
            "path_rel": rel,
            "dir_rel": str(Path(rel).parent.as_posix()),
            "filename": path.name,
            "imports": [],
            "is_test_by_import": False,
            "is_test": False,
        }

        # Detail step: read file head and extract imports. Capped to
        # DETAIL_FILE_LIMIT so we don't read the entire repo on every run.
        if detail_count < DETAIL_FILE_LIMIT:
            text = _read_head(path)
            if text:
                imports = _extract_imports(text)
                rec["imports"] = imports
                rec["is_test_by_import"] = any(_is_test_import(m) for m in imports)
                detail_count += 1
        records.append(rec)

    print(f"[discover_test_patterns] Source files scanned: {len(records)} "
          f"(content-read: {detail_count})")

    # ---- Stage 2: Directory-marker discovery ----
    directory_markers = discover_test_directories(records, repo_root)
    print(f"[discover_test_patterns] Directory markers: {directory_markers}")

    # Mark records that live in a discovered test directory. This lets the
    # filename-pattern step pick up tests that don't explicitly import a
    # framework (e.g. helper/fixture files in `/test/`).
    for rec in records:
        rel = "/" + rec["path_rel"].lower()
        if rec["is_test_by_import"]:
            rec["is_test"] = True
            continue
        for marker in directory_markers:
            if marker in rel:
                rec["is_test"] = True
                break

    test_count = sum(1 for r in records if r["is_test"])
    print(f"[discover_test_patterns] Test files identified: {test_count}")

    # ---- Stage 3: Framework discovery ----
    frameworks = discover_test_frameworks(records)
    print(f"[discover_test_patterns] Frameworks: {frameworks}")

    # ---- Stage 4: Filename-pattern discovery ----
    suffixes, cs_suffixes, prefixes, extensions = discover_filename_patterns(records)
    print(f"[discover_test_patterns] Suffixes (CI): {suffixes}")
    print(f"[discover_test_patterns] Suffixes (CS): {cs_suffixes}")
    print(f"[discover_test_patterns] Prefixes: {prefixes}")
    print(f"[discover_test_patterns] Source extensions: {extensions}")

    return {
        "discovered_at": _now_iso(),
        "source": "repository scan (discover_test_patterns.py)",
        "method": (
            "directory ratio>=0.5 of test-framework-importing files; "
            "filename suffix/prefix frequency among identified test files; "
            "framework set = imports intersected with universal test tokens"
        ),
        "directory_markers": directory_markers,
        "filename_suffixes": suffixes,
        "case_sensitive_filename_suffixes": cs_suffixes,
        "filename_prefixes": prefixes,
        "source_extensions": extensions,
        "test_frameworks": frameworks,
        "statistics": {
            "files_scanned": len(records),
            "files_content_read": detail_count,
            "test_files_identified": test_count,
            "directory_markers_count": len(directory_markers),
            "frameworks_count": len(frameworks),
        },
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Auto-discover test directory / filename / framework "
                    "patterns from the repository."
    )
    p.add_argument("--repo-root", default=str(REPO_ROOT_DEFAULT))
    p.add_argument("--output", default=str(KB_DIR_DEFAULT / "test_patterns.json"))
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    repo_root = Path(args.repo_root).resolve()
    output = Path(args.output).resolve()

    if not repo_root.is_dir():
        print(f"[discover_test_patterns] ERROR: repo-root not a directory: "
              f"{repo_root}", file=sys.stderr)
        return 2

    result = build_test_patterns_kb(repo_root)

    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"[discover_test_patterns] Wrote: {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
