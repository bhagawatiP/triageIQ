#!/usr/bin/env python3
"""
detect_changes.py - Automatic Git Change Detection for RIA v2 Pipeline

Detects changed Java files (unstaged + staged + untracked) and extracts the
specific methods that were modified, so the RIA pipeline can run automatically
without the user having to specify --changed-method / --changed-file manually.

Public API:

    get_changed_files(repo_root) -> List[str]
        Returns relative paths of changed *.java* files (test/generated dirs
        are filtered out).

    get_git_diff(file_path, repo_root) -> dict
        Returns {'modified_lines': [...]} for a single file (staged + unstaged).

    parse_java_methods(file_content) -> List[dict]
        Extracts method declarations (name, line range, signature, is_test).

    extract_changed_methods(file_path, repo_root) -> List[dict]
        Combines git diff + Java parsing to return the methods that were
        actually modified.

    detect_code_changes(repo_root) -> dict
        Top-level entry point. Returns:
          {
            'changed_files': [
              {
                'file_path': '...',
                'changed_methods': [
                  {
                    'method_name': '...',
                    'class_name':  '...',
                    'line_start':  N,
                    'line_end':    N,
                    'changed_lines': [...],
                    'signature':   '...',
                    'is_test':     bool,
                  }, ...
                ]
              }, ...
            ],
            'total_changed_files':   int,
            'total_changed_methods': int,
            'errors':                [str, ...]
          }

Usage as a CLI (handy for debugging):

    python3 detect_changes.py [--repo-root /path/to/repo]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Phase 2: optional import of the language profile so detection can be
# language-aware. The import is wrapped to keep this module usable as a
# standalone script even if configs aren't on the path yet (matches the
# pattern used by other scripts here).
try:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from configs.ria_config import get_active_profile as _get_active_profile
except Exception:  # pragma: no cover - defensive
    _get_active_profile = None


# ---------------------------------------------------------------------------
# Configuration / filters
# ---------------------------------------------------------------------------

# Path components that, if present, mark the file as "skip" (test/generated/RIA).
EXCLUDE_PATH_FRAGMENTS = (
    '/test/',
    '/tests/',
    '/target/',
    '/build/',
    '/generated-sources/',
    '/generated/',
    '/.gradle/',
    '/.github/skills/regression-impact-analysis/',
    '/.github/RIA_OUTPUT/',
    '/.github/RIA_INPUT/',
)

# File-name patterns that indicate test files.
TEST_FILE_SUFFIXES = ('Test.java', 'Tests.java', 'IT.java', 'TestCase.java')


# ---------------------------------------------------------------------------
# Language-aware method discovery patterns
# ---------------------------------------------------------------------------
# The pipeline supports Java, TypeScript, JavaScript, and Python via
# LANGUAGE_PROFILES in ria_config.py. Each profile supplies a
# method_declaration_regex with a {METHOD} placeholder. For *discovery*
# (finding ALL methods in a file), we replace {METHOD} with a capturing
# group (\w+). For *search* (finding a known method), callers replace
# {METHOD} with the escaped name.
#
# Body-scoping strategy:
#   - Brace languages (Java/TS/JS): strip strings/comments, pre-compute
#     brace map, find opening '{' after signature -> look up matching '}'.
#   - Python: find the next line with deeper indentation -> the block ends
#     when indentation returns to the def line's level (or the file ends).

def _get_method_discovery_pattern() -> re.Pattern:
    """
    Build a discovery regex from the active profile's method_declaration_regex.
    Replaces {METHOD} with (\\w+) and adds anchoring for safety.

    FAIL-FAST CONTRACT (no fallbacks):
      - The active language profile MUST be importable.
      - profile['method_declaration_regex'] MUST be non-empty.
      - The compiled regex MUST be valid.
    Any failure raises RuntimeError so language-misconfiguration surfaces
    immediately instead of running the wrong-language Java regex on
    Python/TypeScript code.
    """
    profile = None
    last_err: Optional[Exception] = None
    if _get_active_profile is not None:
        try:
            profile = _get_active_profile()
        except Exception as e:
            last_err = e

    if profile is None:
        try:
            from configs.ria_config import get_active_profile
            profile = get_active_profile()
        except Exception as e:
            last_err = e

    if profile is None:
        raise RuntimeError(
            "[detect_changes] No language profile available. "
            "configs.ria_config.get_active_profile() failed.\n"
            f"Underlying error: {last_err!r}\n"
            "Root cause: configs/ria_config.py is missing, broken, or "
            "RIA_LANGUAGE env var points to an unknown language.\n"
            "Fix: Verify configs/ria_config.py is importable and "
            "RIA_LANGUAGE is set to a supported language "
            "(java, typescript, javascript, python)."
        )

    template = profile.get('method_declaration_regex', '')
    if not template:
        raise RuntimeError(
            "[detect_changes] Active language profile has no "
            "'method_declaration_regex'.\n"
            f"Profile keys: {list(profile.keys())}\n"
            "Fix: Add a method_declaration_regex with a {METHOD} "
            "placeholder to the active profile in configs/ria_config.py."
        )

    profiles = _get_language_profiles() or {}
    lang = next(
        (k for k, v in profiles.items() if v is profile),
        None
    )
    if lang is None:
        raise RuntimeError(
            "[detect_changes] Could not determine active language key "
            "from LANGUAGE_PROFILES. The active profile is not registered "
            "in LANGUAGE_PROFILES.\n"
            "Fix: Ensure get_active_profile() returns a profile that is "
            "also a value in LANGUAGE_PROFILES."
        )

    # Replace {METHOD} with a capturing group for discovery
    discovery = template.replace('{METHOD}', r'(\w+)')

    # For brace languages: anchor to line start and allow multi-line signatures
    if lang in ('java', 'typescript', 'javascript'):
        # Ensure it starts at line beginning
        if not discovery.startswith('^') and not discovery.startswith(r'^\s'):
            discovery = r'^[ \t]*' + discovery
    # Python patterns already have ^ anchor

    try:
        return re.compile(discovery, re.MULTILINE)
    except re.error as e:
        raise RuntimeError(
            f"[detect_changes] method_declaration_regex for language "
            f"'{lang}' is not a valid Python regex: {e}\n"
            f"Pattern: {discovery!r}\n"
            f"Fix: Correct the regex in configs/ria_config.py."
        ) from e


def _get_language_profiles():
    """Helper to access LANGUAGE_PROFILES from ria_config.

    FAIL-FAST: configs.ria_config is required infrastructure. If it
    cannot be imported, the pipeline cannot work, so we surface the
    underlying error rather than silently returning None and selecting
    the wrong-language Java regex.
    """
    from configs.ria_config import LANGUAGE_PROFILES
    return LANGUAGE_PROFILES


def _get_active_language() -> str:
    """Return the active language key ('java', 'typescript', etc.).

    FAIL-FAST: Returns the active key, raising RuntimeError if the
    language profile cannot be resolved. The previous default of 'java'
    silently ran the Java regex on Python/TS files, producing zero
    method matches and orphaned tests.
    """
    profile = None
    if _get_active_profile is not None:
        profile = _get_active_profile()
    else:
        from configs.ria_config import get_active_profile
        profile = get_active_profile()
    profiles = _get_language_profiles() or {}
    for k, v in profiles.items():
        if v is profile:
            return k
    raise RuntimeError(
        "[detect_changes] _get_active_language: active profile is not "
        "registered in LANGUAGE_PROFILES.\n"
        "Fix: Ensure configs/ria_config.py exposes the active profile "
        "as a value in LANGUAGE_PROFILES."
    )


# Java-specific hand-tuned method pattern (used when lang == 'java').
# This is NOT a fallback - it is the canonical Java discovery pattern,
# kept separate from _get_method_discovery_pattern() because Java's
# multi-modifier signatures are easier to match with a hand-written
# regex than via {METHOD}-template substitution.
_JAVA_METHOD_PATTERN = re.compile(
    r'^[ \t]*'
    r'(?:(?:public|protected|private|static|final|synchronized|'
    r'abstract|native|default|strictfp)\s+){0,6}'
    r'(?:<[^>]+>\s+)?'
    r'(?:[\w\.$]+(?:\s*<[^>{}();]*>)?(?:\s*\[\s*\])*)'
    r'\s+(\w+)\s*\('
    r'[^;{}\n]*\)'
    r'(?:\s*throws\s+[\w\.,\s]{1,200})?'
    r'\s*[{;]?\s*$',
    re.MULTILINE,
)

# Heuristic for "is this a *simple* getter/setter we should ignore?".  We
# require *both* a get/set/is<Capital>... name AND a body that is essentially
# just a one-line return / assignment.  The body-shape check is done at the
# call site using the raw lines; the regex below only flags the name pattern.
_GETTER_NAME_PATTERN = re.compile(r'^(?:get|set|is)[A-Z]\w*$')

# Class/container declaration patterns per language family
_CLASS_PATTERN_BRACE = re.compile(
    r'^[ \t]*(?:(?:public|protected|private|static|final|abstract|export|declare)\s+)*'
    r'(?:class|interface|enum|record|type|namespace)\s+(\w+)',
    re.MULTILINE,
)

# Python class pattern
_CLASS_PATTERN_PYTHON = re.compile(
    r'^class\s+(\w+)',
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------

def _run_git(args: List[str], repo_root: str) -> Tuple[int, str, str]:
    """Run a git command. Returns (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ['git'] + args,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode, proc.stdout or '', proc.stderr or ''
    except FileNotFoundError:
        return 127, '', 'git executable not found'
    except Exception as exc:  # pragma: no cover - defensive
        return 1, '', f'git invocation failed: {exc}'


def _is_git_repo(repo_root: str) -> bool:
    rc, _, _ = _run_git(['rev-parse', '--is-inside-work-tree'], repo_root)
    return rc == 0


# ---------------------------------------------------------------------------
# File-level filtering
# ---------------------------------------------------------------------------

def _active_source_extensions() -> Tuple[str, ...]:
    """
    Return the active language's source extensions from the profile.

    FAIL-FAST CONTRACT (no fallbacks):
      - The active language profile MUST be importable.
      - profile['source_extensions'] MUST be a non-empty iterable.
    The previous fallback to a hard-coded mixed-language extension list
    silently masked broken configs/ria_config.py imports and produced
    wrong-language file scans (e.g. Java regex on Python files).
    """
    last_err: Optional[Exception] = None
    prof = None
    if _get_active_profile is not None:
        try:
            prof = _get_active_profile()
        except Exception as e:
            last_err = e
    if prof is None:
        try:
            from configs.ria_config import get_active_profile
            prof = get_active_profile()
        except Exception as e:
            last_err = e
    if prof is None:
        raise RuntimeError(
            "[detect_changes] _active_source_extensions: cannot resolve "
            "active language profile.\n"
            f"Underlying error: {last_err!r}\n"
            "Root cause: configs/ria_config.py is missing/broken or "
            "RIA_LANGUAGE points to an unknown language.\n"
            "Fix: Verify configs/ria_config.py is importable and "
            "RIA_LANGUAGE is set to a supported language."
        )
    exts = tuple(prof.get('source_extensions') or ())
    if not exts:
        raise RuntimeError(
            "[detect_changes] _active_source_extensions: active profile "
            "has empty 'source_extensions'.\n"
            f"Profile keys: {list(prof.keys())}\n"
            "Fix: Add a non-empty 'source_extensions' tuple to the active "
            "profile in configs/ria_config.py."
        )
    return exts


def _active_test_suffixes() -> Tuple[str, ...]:
    """Active language's test-file suffixes.

    FAIL-FAST CONTRACT (no fallbacks):
      - The active language profile MUST be importable.
      - profile['test_patterns'] MUST be a non-empty iterable.
    """
    if _get_active_profile is None:
        from configs.ria_config import get_active_profile as _gap
        prof = _gap()
    else:
        prof = _get_active_profile()
    patterns = tuple(prof.get('test_patterns') or ())
    if not patterns:
        raise RuntimeError(
            "[detect_changes] _active_test_suffixes: active profile has "
            "empty 'test_patterns'.\n"
            f"Profile keys: {list(prof.keys())}\n"
            "Fix: Add a non-empty 'test_patterns' tuple to the active "
            "profile in configs/ria_config.py."
        )
    return patterns


def _should_skip_path(rel_path: str) -> bool:
    """
    True if this path should be skipped (test/generated/non-source).

    Phase 2: extension and test-suffix checks now consult the active
    language profile. For Java (the default and the only language used in
    Phase 1) this resolves to the same '.java' / *Test.java patterns as
    before, so behaviour is identical.
    """
    source_exts = _active_source_extensions()
    if not any(rel_path.endswith(ext) for ext in source_exts):
        return True

    norm = '/' + rel_path.replace('\\', '/').lstrip('/')
    for frag in EXCLUDE_PATH_FRAGMENTS:
        if frag in norm:
            return True

    base = os.path.basename(rel_path)
    test_suffixes = _active_test_suffixes()
    for suffix in test_suffixes:
        # Test patterns may be either suffixes ("Test.java") or substrings
        # ("test_") - check both shapes for parity with the profile config.
        if base.endswith(suffix) or suffix in base:
            return True

    return False


def get_changed_files(repo_root: str) -> List[str]:
    """
    Return changed Java files (relative paths) from:
      - git diff (unstaged)
      - git diff --cached (staged)
      - git ls-files --others --exclude-standard (untracked)

    Filters out:
      - non-Java files
      - test files (*Test.java, *IT.java, etc.)
      - files in test/generated/build directories
      - deleted files (cannot analyze them)
    """
    if not _is_git_repo(repo_root):
        return []

    files: set = set()

    # --- Unstaged ----------------------------------------------------------
    rc, out, _ = _run_git(['diff', '--name-only', '--diff-filter=AMR'], repo_root)
    if rc == 0:
        files.update(line.strip() for line in out.splitlines() if line.strip())

    # --- Staged ------------------------------------------------------------
    rc, out, _ = _run_git(['diff', '--cached', '--name-only', '--diff-filter=AMR'], repo_root)
    if rc == 0:
        files.update(line.strip() for line in out.splitlines() if line.strip())

    # --- Untracked ---------------------------------------------------------
    rc, out, _ = _run_git(['ls-files', '--others', '--exclude-standard'], repo_root)
    if rc == 0:
        files.update(line.strip() for line in out.splitlines() if line.strip())

    # --- Filter ------------------------------------------------------------
    result: List[str] = []
    for rel in sorted(files):
        if _should_skip_path(rel):
            continue
        full = os.path.join(repo_root, rel)
        if not os.path.isfile(full):
            # Could be deleted or renamed-out; skip.
            continue
        result.append(rel)

    return result


# ---------------------------------------------------------------------------
# Git diff -> changed line numbers
# ---------------------------------------------------------------------------

# Matches a unified-diff hunk header:  @@ -old,oldcount +new,newcount @@
_HUNK_PATTERN = re.compile(r'^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@')


def _parse_diff_hunks(diff_output: str) -> List[int]:
    """Return the list of *new-file* line numbers touched by this diff."""
    changed_lines: List[int] = []
    new_line_no = 0
    in_hunk = False

    for line in diff_output.splitlines():
        if line.startswith('@@'):
            m = _HUNK_PATTERN.match(line)
            if not m:
                in_hunk = False
                continue
            new_line_no = int(m.group(1))
            in_hunk = True
            continue

        if not in_hunk:
            continue

        # Skip "no newline at end of file" markers and binary indicators.
        if line.startswith('\\'):
            continue

        if line.startswith('+') and not line.startswith('+++'):
            changed_lines.append(new_line_no)
            new_line_no += 1
        elif line.startswith('-') and not line.startswith('---'):
            # deletion - no advance of new_line_no
            pass
        else:
            # context line
            new_line_no += 1

    return changed_lines


def get_git_diff(file_path: str, repo_root: str) -> Dict[str, List[int]]:
    """
    Combined staged + unstaged diff for a single file.

    Untracked / new files have no diff vs HEAD, so for those we return
    every line as 'modified'.
    """
    full = os.path.join(repo_root, file_path)
    if not os.path.isfile(full):
        return {'modified_lines': []}

    # Untracked (new) file?  No HEAD entry.
    rc, _, _ = _run_git(['ls-files', '--error-unmatch', file_path], repo_root)
    if rc != 0:
        # New file: treat every line as modified.
        try:
            with open(full, 'r', encoding='utf-8', errors='replace') as fh:
                n = sum(1 for _ in fh)
            return {'modified_lines': list(range(1, n + 1))}
        except Exception:
            return {'modified_lines': []}

    rc1, unstaged, _ = _run_git(['diff', '--unified=0', '--', file_path], repo_root)
    rc2, staged,   _ = _run_git(['diff', '--cached', '--unified=0', '--', file_path], repo_root)

    combined = ''
    if rc1 == 0:
        combined += unstaged
    if rc2 == 0:
        combined += '\n' + staged

    lines = sorted(set(_parse_diff_hunks(combined)))
    return {'modified_lines': lines}


# ---------------------------------------------------------------------------
# Java parsing
# ---------------------------------------------------------------------------

# Regex that matches comments, string literals, and char literals in one pass.
_COMMENT_STRING_RE = re.compile(
    r'//[^\n]*'             # line comment
    r"|/\*[\s\S]*?\*/"      # block comment
    r'|"(?:[^"\\\n]|\\.)*"'  # string literal
    r"|'(?:[^'\\]|\\.)*'"   # char literal
)


def _strip_strings_and_comments(src: str) -> str:
    """
    Replace string literals and comments with whitespace so brace counting
    is not confused by '{' or '}' inside them.  Preserves line breaks.
    Uses a single regex pass instead of character-by-character scanning.
    """
    def _replacer(m: re.Match) -> str:
        text = m.group(0)
        # Preserve newlines so line numbering stays correct
        return re.sub(r'[^\n]', ' ', text)
    return _COMMENT_STRING_RE.sub(_replacer, src)


def _find_method_end(lines: List[str], start_line_idx: int) -> int:
    """
    Given the index of the line containing the method's '{', return the
    1-indexed line number of the matching closing '}'.
    """
    brace = 0
    started = False
    for i in range(start_line_idx, len(lines)):
        opens = lines[i].count('{')
        closes = lines[i].count('}')
        if opens > 0:
            started = True
        brace += opens - closes
        if started and brace == 0:
            return i + 1  # 1-indexed
    return len(lines)


def _precompute_brace_matches(stripped: str) -> Dict[int, int]:
    """
    Pre-compute a mapping from each '{' position to its matching '}' position.
    Done in a single O(n) pass over the stripped source.
    """
    stack: List[int] = []
    matches: Dict[int, int] = {}
    for i, c in enumerate(stripped):
        if c == '{':
            stack.append(i)
        elif c == '}':
            if stack:
                open_pos = stack.pop()
                matches[open_pos] = i
    return matches


def _find_block_end_from_pos(stripped: str, brace_pos: int, _brace_map: Optional[Dict[int, int]] = None) -> int:
    """
    Return the *char index* of the '}' that matches the '{' at brace_pos
    (in the comment/string-stripped source).  Returns len(stripped) if not
    found.  brace_pos must point at the opening '{'.

    If _brace_map is provided (pre-computed), uses O(1) lookup.
    """
    if _brace_map is not None:
        return _brace_map.get(brace_pos, len(stripped) - 1)
    depth = 0
    i = brace_pos
    n = len(stripped)
    while i < n:
        c = stripped[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n - 1


def _find_class_for_line(class_ranges: List[Tuple[int, int, str]], line_no: int) -> Optional[str]:
    """
    class_ranges is a list of (start_line, end_line, class_name) sorted by
    start_line.  Return the *innermost* class containing line_no.
    """
    best = None
    best_span = None
    for start, end, name in class_ranges:
        if start <= line_no <= end:
            span = end - start
            if best_span is None or span < best_span:
                best_span = span
                best = name
    return best


def _build_class_ranges(stripped: str, brace_map: Optional[Dict[int, int]] = None, lang: str = 'java') -> List[Tuple[int, int, str]]:
    """Return list of (start_line, end_line, class_name). Language-aware."""
    pattern = _CLASS_PATTERN_PYTHON if lang == 'python' else _CLASS_PATTERN_BRACE
    ranges: List[Tuple[int, int, str]] = []
    for m in pattern.finditer(stripped):
        start_line_idx = stripped[:m.start()].count('\n')
        if lang == 'python':
            # Python: class body ends when indentation returns to class level
            end_line = _find_python_block_end(stripped, m.start())
        else:
            # Brace languages: find matching '}'
            brace_idx = stripped.find('{', m.end())
            if brace_idx < 0:
                continue
            end_pos = _find_block_end_from_pos(stripped, brace_idx, brace_map)
            end_line = stripped[:end_pos].count('\n') + 1
        ranges.append((start_line_idx + 1, end_line, m.group(1)))
    return ranges


def parse_java_methods(file_content: str) -> List[Dict]:
    """
    Parse source file and return method declarations. Multi-language aware.
    Dispatches to the correct strategy based on the active language profile.

    Each entry contains:
      - method_name : str
      - line_start  : int (1-indexed, line of the signature)
      - line_end    : int (1-indexed, line of closing '}' or block end)
      - signature   : str
      - is_test     : bool
      - class_name  : Optional[str]
    """
    if not file_content:
        return []

    lang = _get_active_language()

    if lang == 'python':
        return _parse_python_methods(file_content)
    else:
        return _parse_brace_language_methods(file_content, lang)


def _find_python_block_end(source: str, def_pos: int) -> int:
    """
    For Python: find the end line of a block starting at def_pos.
    The block ends when a subsequent non-empty line has indentation <= the
    def line's indentation, or at EOF.
    Returns 1-indexed line number.
    """
    lines = source.split('\n')
    def_line_idx = source[:def_pos].count('\n')
    def_line = lines[def_line_idx] if def_line_idx < len(lines) else ''
    def_indent = len(def_line) - len(def_line.lstrip())

    last_body_line = def_line_idx
    for i in range(def_line_idx + 1, len(lines)):
        line = lines[i]
        stripped_line = line.strip()
        if not stripped_line:
            continue  # skip blank lines
        line_indent = len(line) - len(line.lstrip())
        if line_indent <= def_indent:
            break
        last_body_line = i

    return last_body_line + 1  # 1-indexed


def _parse_python_methods(file_content: str) -> List[Dict]:
    """Parse Python source to extract function/method declarations."""
    lines = file_content.split('\n')
    # Python def pattern
    def_pattern = re.compile(r'^(\s*)(?:async\s+)?def\s+(\w+)\s*\(', re.MULTILINE)
    # Python class pattern for class_name resolution
    class_pattern = re.compile(r'^(\s*)class\s+(\w+)', re.MULTILINE)

    # Build class ranges for Python (indent-based)
    class_ranges: List[Tuple[int, int, str]] = []
    for m in class_pattern.finditer(file_content):
        start_line = file_content[:m.start()].count('\n') + 1
        end_line = _find_python_block_end(file_content, m.start())
        class_ranges.append((start_line, end_line, m.group(2)))

    # Test decorators for Python
    _PY_TEST_DECORATORS = ('@pytest.mark', '@mock.patch', '@patch')

    methods: List[Dict] = []
    for m in def_pattern.finditer(file_content):
        method_name = m.group(2)
        line_start = file_content[:m.start()].count('\n') + 1
        line_end = _find_python_block_end(file_content, m.start())

        # Detect test methods
        is_test = method_name.startswith('test_') or method_name.startswith('test')
        if not is_test and line_start >= 2:
            prev_line = lines[line_start - 2].strip()
            is_test = any(prev_line.startswith(d) for d in _PY_TEST_DECORATORS)

        # Skip dunder methods that are typically boilerplate
        if method_name.startswith('__') and method_name.endswith('__'):
            if method_name in ('__init__', '__call__', '__enter__', '__exit__'):
                pass  # Keep these - they have business logic
            else:
                continue

        signature_line = lines[line_start - 1].strip() if line_start <= len(lines) else ''
        class_name = _find_class_for_line(class_ranges, line_start)

        methods.append({
            'method_name': method_name,
            'line_start': line_start,
            'line_end': line_end,
            'signature': signature_line,
            'is_test': is_test,
            'class_name': class_name,
        })

    methods.sort(key=lambda d: d['line_start'])
    return methods


def _parse_brace_language_methods(file_content: str, lang: str = 'java') -> List[Dict]:
    """
    Parse brace-delimited language source (Java, TypeScript, JavaScript).
    Uses profile-driven method discovery regex with optimized brace matching.
    """
    if not file_content:
        return []

    stripped = _strip_strings_and_comments(file_content)
    raw_lines = file_content.split('\n')
    stripped_lines = stripped.split('\n')
    brace_map = _precompute_brace_matches(stripped)
    class_ranges = _build_class_ranges(stripped, brace_map, lang)

    methods: List[Dict] = []
    seen_starts: set = set()

    _CONTROL_KEYWORDS = {
        'if', 'for', 'while', 'switch', 'catch', 'synchronized',
        'return', 'throw', 'new', 'else', 'do', 'try', 'finally',
        'case', 'instanceof', 'assert', 'break', 'continue',
    }
    # Pattern selection: Java has a hand-tuned pattern that matches
    # multi-modifier signatures more reliably than the generic regex
    # composition. Other languages use the profile-driven pattern.
    if lang == 'java':
        method_pattern = _JAVA_METHOD_PATTERN
    else:
        method_pattern = _get_method_discovery_pattern()

    for m in method_pattern.finditer(stripped):
        method_name = m.group(1)
        if method_name in _CONTROL_KEYWORDS:
            continue
        # Reject if the return-type token is a control keyword (this filters
        # out things like 'return foo(bar);' that the regex may otherwise
        # match when modifiers are absent).
        head = m.group(0).strip().split('(', 1)[0]
        head_tokens = head.replace('<', ' ').replace('>', ' ').split()
        if head_tokens and head_tokens[-2:] and head_tokens[-2] in _CONTROL_KEYWORDS:
            continue

        # Compute line where signature starts (1-indexed).
        line_start = stripped[:m.start()].count('\n') + 1
        if line_start in seen_starts:
            continue
        seen_starts.add(line_start)

        # Find the opening brace.  It may be on the same line or a later one.
        #
        # Heuristic for abstract / interface methods:
        #   Between ')' and the body we may only see whitespace, a 'throws
        #   ClassA, ClassB' clause, default/static modifiers, and finally
        #   ';' (abstract) or '{' (concrete).
        #   So we treat the signature as ABSTRACT only if the FIRST
        #   non-whitespace, non-throws-token character is ';'.  This
        #   prevents a ';' deep inside the next method body from being
        #   mistaken for the terminator (which can happen if the user's
        #   working copy is mid-edit and a '{' is temporarily missing).
        sig_tail = stripped[m.end():]
        is_abstract = False
        # Abstract/interface method detection (Java-specific throws clause)
        if lang == 'java':
            i_tail = 0
            n_tail = len(sig_tail)
            while i_tail < n_tail and sig_tail[i_tail].isspace():
                i_tail += 1
            # 'throws X, Y, Z'
            if sig_tail[i_tail:i_tail + 7] == 'throws ':
                i_tail += 7
                while i_tail < n_tail and sig_tail[i_tail] not in '{;':
                    i_tail += 1
            if i_tail < n_tail and sig_tail[i_tail] == ';':
                is_abstract = True
        else:
            # TS/JS: abstract if no '{' follows on the line or next few chars
            i_tail = 0
            n_tail = min(len(sig_tail), 50)
            while i_tail < n_tail and sig_tail[i_tail].isspace():
                i_tail += 1
            if i_tail < n_tail and sig_tail[i_tail] == ';':
                is_abstract = True

        if is_abstract:
            semi_abs = m.end() + i_tail
            line_end = stripped[:semi_abs].count('\n') + 1
        else:
            # Concrete method: locate the body's opening '{'.  The regex's
            # trailing '\{?' may have CONSUMED that '{' (m.end() is then one
            # past the '{').  Scan backwards over whitespace from m.end()-1
            # to find it; if the regex didn't consume a '{', search forward
            # from m.end() instead.  If we cannot find a '{' at all (mid-
            # edit, malformed source), fall back to a single-line range.
            matched_text = m.group(0)
            if matched_text.rstrip().endswith('{'):
                # The '{' is part of the match.  Compute its absolute index.
                rel = matched_text.rfind('{')
                brace_idx = m.start() + rel
            else:
                brace_idx = stripped.find('{', m.end())

            if brace_idx >= 0:
                end_pos = _find_block_end_from_pos(stripped, brace_idx, brace_map)
                line_end = stripped[:end_pos].count('\n') + 1
            else:
                line_end = line_start

        # Extract signature: walk forward from line_start until we find a
        # line that contains the method name + '('.  The regex match may
        # begin on an empty line because the leading whitespace pattern
        # matched a '\n' first.
        signature_line = ''
        for k in range(max(0, line_start - 1), min(len(raw_lines), line_start + 5)):
            if method_name + '(' in raw_lines[k] or method_name + ' (' in raw_lines[k]:
                signature_line = raw_lines[k].strip()
                # Adjust line_start to the actual signature line.
                line_start = k + 1
                break
        if not signature_line and line_start - 1 < len(raw_lines):
            signature_line = raw_lines[line_start - 1].strip()

        # Detect test methods - language aware
        is_test = False
        if lang == 'java':
            # Java: @Test annotation on preceding lines
            i = line_start - 2
            while i >= 0:
                prev = raw_lines[i].strip()
                if not prev:
                    i -= 1
                    continue
                if prev.startswith('@'):
                    if '@Test' in prev or '@ParameterizedTest' in prev or '@RepeatedTest' in prev:
                        is_test = True
                        break
                    i -= 1
                    continue
                break
        elif lang in ('typescript', 'javascript'):
            # TS/JS: 'it(', 'test(', 'describe(' patterns or method name starts with test
            if method_name in ('it', 'test', 'describe', 'beforeEach', 'afterEach', 'beforeAll', 'afterAll'):
                is_test = True

        # Skip ONLY trivial getters/setters: name matches the get/set/is
        # pattern AND the body is a single executable line that is just a
        # 'return ...' or '... = ...' assignment.  Anything more complex
        # (loops, branches, multiple statements, side-effects) is a
        # business-logic method and should be analyzed.
        if (line_end > line_start and line_end - line_start <= 3
                and _GETTER_NAME_PATTERN.match(method_name)):
            body_lines = [
                raw_lines[i].strip()
                for i in range(line_start, line_end - 1)
                if i < len(raw_lines) and raw_lines[i].strip()
                and raw_lines[i].strip() not in ('{', '}')
            ]
            executable = [
                ln for ln in body_lines
                if ln not in ('{', '}') and not ln.startswith('//')
            ]
            if len(executable) <= 1:
                only = executable[0] if executable else ''
                if (only.startswith('return ')
                        or only.startswith('this.')
                        or '=' in only):
                    continue

        class_name = _find_class_for_line(class_ranges, line_start)

        methods.append({
            'method_name': method_name,
            'line_start':  line_start,
            'line_end':    line_end,
            'signature':   signature_line,
            'is_test':     is_test,
            'class_name':  class_name,
        })

    methods.sort(key=lambda d: d['line_start'])
    return methods


# ---------------------------------------------------------------------------
# Method-level change extraction
# ---------------------------------------------------------------------------

def extract_changed_methods(file_path: str, repo_root: str) -> List[Dict]:
    """
    Return the methods in `file_path` that contain any modified lines.

    Each method dict has the same shape as parse_java_methods, plus
    'changed_lines' (the subset of modified lines that fall inside the
    method).
    """
    full = os.path.join(repo_root, file_path)
    try:
        with open(full, 'r', encoding='utf-8', errors='replace') as fh:
            content = fh.read()
    except (IOError, OSError):
        return []

    methods = parse_java_methods(content)
    if not methods:
        return []

    diff = get_git_diff(file_path, repo_root)
    changed_lines = diff.get('modified_lines', [])
    if not changed_lines:
        return []

    changed_set = set(changed_lines)
    out: List[Dict] = []
    for m in methods:
        if m.get('is_test'):
            continue
        rng = set(range(m['line_start'], m['line_end'] + 1))
        hits = sorted(rng & changed_set)
        if not hits:
            continue
        entry = dict(m)
        entry['changed_lines'] = hits
        out.append(entry)

    # Edge case: file changed but no method matched (e.g. import changes,
    # field changes, signature-only changes that confused the parser).
    # FAIL-FAST: previous fallback (pick first non-test method) silently
    # tagged the change against an unrelated method, producing wrong
    # downstream impact. We now identify the enclosing method explicitly.
    if not out:
        first_changed = min(changed_lines)
        enclosing = None
        for m in methods:
            if m.get('is_test'):
                continue
            if m['line_start'] <= first_changed <= m['line_end']:
                enclosing = m
                break
        if enclosing is None:
            raise RuntimeError(
                f"[detect_changes] File '{file_path}' has changes on lines "
                f"{sorted(changed_lines)} but no non-test method encloses "
                f"line {first_changed}.\n"
                f"Root cause: change is outside any method (import, field, "
                f"class-level edit) OR the method parser failed to parse a "
                f"valid declaration.\n"
                f"Fix: Ensure the change is within a method body, OR fix "
                f"parse_java_methods()/the language profile's "
                f"method_declaration_regex so it covers this declaration."
            )
        entry = dict(enclosing)
        entry['changed_lines'] = changed_lines
        out.append(entry)

    return out


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------

def detect_code_changes(repo_root: str) -> Dict:
    """
    Detect all code changes in `repo_root` and return a structured report.

    On a non-git directory or on any I/O failure this still returns a valid
    (possibly empty) report; errors are appended to result['errors'].
    """
    repo_root = str(Path(repo_root).resolve())
    errors: List[str] = []

    if not _is_git_repo(repo_root):
        errors.append(f'Not a git repository: {repo_root}')
        return {
            'changed_files': [],
            'total_changed_files':   0,
            'total_changed_methods': 0,
            'errors': errors,
        }

    files = get_changed_files(repo_root)

    changed_files: List[Dict] = []
    total_methods = 0

    for rel in files:
        try:
            methods = extract_changed_methods(rel, repo_root)
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(f'Failed to parse {rel}: {exc}')
            continue

        if not methods:
            continue

        changed_files.append({
            'file_path': rel,
            'changed_methods': methods,
        })
        total_methods += len(methods)

    return {
        'changed_files':         changed_files,
        'total_changed_files':   len(changed_files),
        'total_changed_methods': total_methods,
        'errors':                errors,
    }


# ---------------------------------------------------------------------------
# Dependency-change detection has been REMOVED.
# The pom.xml / package.json / requirements.txt analysis pipeline was
# excised in favour of a source-only RIA flow. This module now exposes
# only `detect_code_changes()` for changed Java/TypeScript/Python methods.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_repo_root() -> str:
    # This file lives at <repo>/.github/skills/regression-impact-analysis/scripts/
    return str(Path(__file__).resolve().parents[4])


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='Detect changed Java methods from git status.',
    )
    parser.add_argument('--repo-root', default=_default_repo_root(),
                        help='Path to the git repository root.')
    parser.add_argument('--json', action='store_true',
                        help='Print full JSON report (default: human summary).')
    args = parser.parse_args(argv)

    report = detect_code_changes(args.repo_root)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report['total_changed_methods'] > 0 else 2

    print(f"Repo root        : {args.repo_root}")
    print(f"Changed files    : {report['total_changed_files']}")
    print(f"Changed methods  : {report['total_changed_methods']}")
    if report.get('errors'):
        print(f"Errors           : {len(report['errors'])}")
        for err in report['errors']:
            print(f"  - {err}")
    print()

    for fi in report['changed_files']:
        print(f"  {fi['file_path']}")
        for m in fi['changed_methods']:
            cls = f"{m['class_name']}." if m.get('class_name') else ''
            print(f"    - {cls}{m['method_name']}  "
                  f"(lines {m['line_start']}-{m['line_end']}, "
                  f"{len(m['changed_lines'])} changed)")

    return 0 if report['total_changed_methods'] > 0 else 2


if __name__ == '__main__':
    sys.exit(main())
