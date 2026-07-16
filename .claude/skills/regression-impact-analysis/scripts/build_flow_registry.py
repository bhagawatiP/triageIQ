#!/usr/bin/env python3
"""
Stage 0a: Build Flow Registry (RIA v2 - Framework-Agnostic)

Discovers flows by scoring tests against entry points using 3-signal algorithm.
Auto-assigns flow tags to tests based on highest-scoring entry point.

3-Signal Scoring:
  Signal 1: Domain noun match (10 pts)
  Signal 2: Verb synonym match (5 pts)
  Signal 3: Keyword overlap (1-3 pts)

Output: flow_registry.json + enriched test corpus with auto-tags
"""

import argparse
import json
import os
import sys
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path as _Path


# ---------------------------------------------------------------------------
# Language-agnostic helpers (Java / Python / TypeScript / JavaScript).
# These let downstream scoring logic operate identically regardless of the
# active source language — camelCase for Java/JS/TS, snake_case for Python.
# ---------------------------------------------------------------------------
def detect_language_from_path(file_path):
    """Return one of {'java','python','typescript','javascript','unknown'}."""
    if not file_path:
        return 'unknown'
    fp = str(file_path).lower()
    if fp.endswith('.java') or fp.endswith('.kt'):
        return 'java'
    if fp.endswith('.py'):
        return 'python'
    if fp.endswith('.ts') or fp.endswith('.tsx'):
        return 'typescript'
    if fp.endswith('.js') or fp.endswith('.jsx') or fp.endswith('.mjs'):
        return 'javascript'
    return 'unknown'


# Compile once: PascalCase / camelCase splitter that preserves uppercase
# acronym groupings (SSO -> 'SSO', WorkPolicy -> 'Work', 'Policy').
_CAMEL_RE = re.compile(r'[A-Z]+(?=[A-Z][a-z]|[^A-Za-z]|$)|[A-Z]?[a-z]+')


def split_method_name(name, language=None):
    """Split a method/identifier into lowercase business-word tokens.

    Java / JavaScript / TypeScript / Kotlin: camelCase / PascalCase.
      'getCalloutAgents'  -> ['get', 'callout', 'agents']
      'WorkPolicyTemplate'-> ['work', 'policy', 'template']
      'getSSOConfig'      -> ['get', 'sso', 'config']

    Python: snake_case (an underscore separator), but we still apply
    camelCase fallback so PEP-8 violators don't lose information.
      'get_callout_agents' -> ['get', 'callout', 'agents']

    Unknown / empty: best-effort camelCase split.
    """
    if not name:
        return []
    if language == 'python' or '_' in name:
        # snake_case primary; if a single token also contains camelCase
        # (e.g. 'getDataAsync' written in a Python file by mistake),
        # also expand it via the camelCase regex.
        out = []
        for piece in name.split('_'):
            if not piece:
                continue
            sub = _CAMEL_RE.findall(piece)
            if sub:
                out.extend(p.lower() for p in sub if p)
            else:
                out.append(piece.lower())
        return out
    parts = _CAMEL_RE.findall(name)
    return [p.lower() for p in parts if p]

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from serena_mcp_client import (
    SerenaMCPClient,
    _is_non_application_file,
    _is_test_file,
    _is_trivial_method,
    is_legitimate_entry_point,
)
from configs.ria_config import RIA_OUTPUT_DIR, TC_DATA_PATH, REPO_ROOT, get_active_profile

# --------------------------------------------------------------------------
# Universal entry-point detector (data-driven; ZERO hardcoded patterns).
# Loaded lazily so import-time failures don't break the whole pipeline.
# --------------------------------------------------------------------------
_UNIVERSAL_DETECTOR = None
_UNIVERSAL_CG = None


def _get_universal_detector():
    """Return a memoised EntryPointDetector instance + InMemoryCallGraph.

    FAIL-FAST: previously this caught every exception and set a sentinel
    so the no-callers fallback would silently degrade. The detector is
    NOT optional — it enforces YAML-driven entry-point filters that
    decide whether a method is a real flow surface. Surfacing the import
    error here exposes installation problems immediately.
    """
    global _UNIVERSAL_DETECTOR, _UNIVERSAL_CG
    if _UNIVERSAL_DETECTOR is not None:
        return _UNIVERSAL_DETECTOR, _UNIVERSAL_CG
    try:
        from core.detector import EntryPointDetector
        from core.call_graph import InMemoryCallGraph
    except ImportError as exc:
        raise RuntimeError(
            f"[build_flow_registry] Universal detector unavailable: {exc}\n"
            f"Root cause: core.detector or core.call_graph cannot be "
            f"imported. The detector is REQUIRED to apply YAML-driven "
            f"entry-point filters.\n"
            f"Fix: ensure the 'core' package ships alongside the RIA "
            f"scripts and is importable on sys.path."
        ) from exc
    _UNIVERSAL_DETECTOR = EntryPointDetector()
    _UNIVERSAL_CG = InMemoryCallGraph()  # caller info supplied separately
    return _UNIVERSAL_DETECTOR, _UNIVERSAL_CG


# --------------------------------------------------------------------------
# Post-discovery validation (defence-in-depth)
# --------------------------------------------------------------------------
# These markers are kept in addition to those baked into serena_mcp_client
# so the registry still rejects pollution even if a future caller bypasses
# the trace API. Keep in sync with _NON_APPLICATION_MARKERS over there.
RIA_TOOL_MARKERS = (
    'regression-impact-analysis/',
    '.github/skills/',
    '.github/agents/',
    '.github/archive/',
    '.github/RIA_INPUT/',
    '.github/RIA_OUTPUT/',
    'backup/skills/',
    '/scripts/ria',
    '/scripts/stage',
    '/scripts/build_',
    '/scripts/v7/',
    'generated-sources/',
    '/generated/',
    '/target/',
    '/build/',
    'node_modules/',
)


def _validate_entry_point(ep, repo_root=None):
    """
    Return (ok, reason). Lightweight defence-in-depth filter applied AFTER
    the new no-callers-based discovery. The discovery step already enforced
    the YAML-driven filters via core.detector.is_entry_point, so all we do
    here is:

      * drop entries with missing file/method,
      * drop non-application / test / trivial / non-source pollution,
      * (no framework-annotation requirement — per the simplification request
        "any method which has no caller should be treated as an entry point",
        the framework-pattern gate is intentionally removed).
    """
    file_path = ep.get('file', '')
    method = ep.get('method', '')
    if not file_path or not method:
        return False, 'missing file or method'
    if _is_non_application_file(file_path):
        return False, 'non-application file'
    if _is_test_file(file_path):
        return False, 'test file'
    if _is_trivial_method(method):
        return False, 'trivial method (getter/setter/boilerplate)'
    profile = get_active_profile()
    source_exts = profile.get('source_extensions', ['.java', '.kt'])
    if not any(file_path.endswith(ext) for ext in source_exts):
        return False, 'non-source file'
    return True, 'ok'


# --------------------------------------------------------------------------
# Simplified upward walk: "no internal callers" -> entry point
# --------------------------------------------------------------------------
#
# Replaces the framework-annotation-driven trace_call_chain_to_entry_points
# logic for FOCUSED mode. Algorithm:
#
#   1. Seed with (changed_file, changed_method).
#   2. While the worklist is non-empty, pop a node:
#        - find external callers (same as Serena's find_referencing_symbols)
#        - drop callers in test / non-application files
#        - if no qualifying callers remain -> ask core.detector whether the
#          node passes the YAML filters; if YES it is an entry point, record
#          it with the chain accumulated so far.
#        - otherwise push each surviving caller with an extended chain.
#   3. Cap the walk at max_depth to mirror the previous trace bound and
#      prevent runaway traversal on pathological codebases.
#
# This is a literal implementation of the user requirement:
#     "If any method which has no caller should be simply treated as
#      entry point methods"
#
# YAML filters (private / abstract / constructor / trivial / test_paths /
# vendor) still gate the result, so RIA-tool / generated / test files are
# never promoted.
# --------------------------------------------------------------------------
def _extract_public_methods_from_file(repo_root, rel_path):
    """
    Extract all public method names from a source file using simple regex.
    This is used by FOCUSED-MULTI-FILE mode to find methods in utility classes
    that we need to trace callers for.

    Returns: list of method names (strings)
    """
    abs_path = os.path.join(repo_root, rel_path)
    try:
        with open(abs_path, 'r', encoding='utf-8', errors='replace') as fh:
            content = fh.read()
    except Exception:
        return []

    lang = detect_language_from_path(rel_path)
    methods = []

    if lang == 'java':
        # Match: public ... methodName(...) {
        # Handles: public static void foo(), public String bar(), etc.
        pattern = r'\bpublic\s+(?:static\s+)?(?:final\s+)?[\w<>\[\],\s]+\s+(\w+)\s*\('
        matches = re.findall(pattern, content)
        methods.extend(matches)
    elif lang in ('typescript', 'javascript'):
        # Match: public methodName(...) or methodName(...) for top-level
        pattern = r'(?:public\s+)?(?:async\s+)?(\w+)\s*\('
        matches = re.findall(pattern, content)
        # Filter out common keywords
        methods.extend([m for m in matches if m not in ('if', 'for', 'while', 'switch', 'function')])
    elif lang == 'python':
        # Match: def methodName(...)
        pattern = r'\bdef\s+(\w+)\s*\('
        matches = re.findall(pattern, content)
        # Exclude private methods (start with _)
        methods.extend([m for m in matches if not m.startswith('_')])

    # Deduplicate and return
    return list(dict.fromkeys(methods))


def _focused_walk_no_callers_entry_points(
    serena,
    repo_root,
    changed_file,
    changed_method,
    max_depth=10,
    max_callers_per_node=100,
):
    """
    Walk the call graph upward from (changed_file, changed_method) and
    collect every node that has no external caller. Returns a dict shaped
    identically to the old trace_call_chain_to_entry_points response so
    downstream code is untouched.
    """
    from core.detector import EntryPointDetector
    from core.call_graph import SerenaCallGraphAdapter

    detector = EntryPointDetector()
    call_graph = SerenaCallGraphAdapter(serena, repo_root=repo_root)

    seed_chain_node = f"{changed_file}:{changed_method}"

    entry_points = []
    dead_leaves = []
    seen_entries = set()
    visited = set()

    # Worklist items: (file, method, chain) where `chain` is the list of
    # "file:method" hops from the leaf (changed method) up to but NOT
    # including the current node.
    worklist = [(changed_file, changed_method, [seed_chain_node], 0)]

    # Tiny in-process cache for file content reads.
    _content_cache = {}

    def _read(rel):
        if rel in _content_cache:
            return _content_cache[rel]
        abs_path = os.path.join(repo_root, rel)
        try:
            with open(abs_path, 'r', encoding='utf-8', errors='replace') as fh:
                txt = fh.read()
        except Exception:
            txt = ''
        _content_cache[rel] = txt
        return txt

    def _record_entry(file_path, method, chain, reason):
        key = f"{file_path}:{method}"
        if key in seen_entries:
            return
        seen_entries.add(key)
        entry_points.append({
            'file': file_path,
            'method': method,
            'chain': list(chain),
            'entry_type': f"no-callers:{reason}" if reason else "no-callers",
        })

    while worklist:
        file_path, method, chain, depth = worklist.pop()
        node_key = f"{file_path}:{method}"
        if node_key in visited:
            continue
        visited.add(node_key)

        # Reject obvious pollution at the node itself.
        if _is_non_application_file(file_path) or _is_test_file(file_path):
            continue
        if _is_trivial_method(method):
            continue

        # Find callers via Serena (same semantics as legacy code).
        try:
            refs_result = serena.find_referencing_symbols(
                method,
                from_file=file_path,
                max_callers=max_callers_per_node,
            )
        except Exception:
            refs_result = {"references": []}
        refs = refs_result.get('references', []) or []

        # Keep only callers that:
        #   - are not in test / non-application files,
        #   - are not trivial helpers.
        # Note: same-file callers are intentionally KEPT — interlinked
        # methods inside a single class/file are legitimate callers and
        # must be followed upward, otherwise we report "0 entry points"
        # for tightly-cohesive helpers whose only callers live next to
        # them in the same source file.
        external_callers = []
        for ref in refs:
            ref_file = ref.get('file') or ''
            ref_method = ref.get('caller_method') or ''
            if not ref_file or not ref_method:
                continue
            if _is_non_application_file(ref_file) or _is_test_file(ref_file):
                continue
            if _is_trivial_method(ref_method):
                continue
            external_callers.append((ref_file, ref_method))

        if not external_callers:
            # No internal/external callers -> candidate entry point per the
            # new simplified rule. Ask the detector to apply YAML filters
            # (private / abstract / trivial / test paths / vendor /
            # language_specific). Detector wants a call_graph that says
            # "no callers" — SerenaCallGraphAdapter does exactly that for
            # this method, so we reuse it.
            content = _read(file_path)
            ok, reason = detector.is_entry_point(
                method_name=method,
                file_path=file_path,
                file_content=content,
                call_graph=call_graph,
            )
            if ok:
                _record_entry(file_path, method, chain, reason)
            else:
                dead_leaves.append({
                    'file': file_path,
                    'method': method,
                    'reason': reason,
                })
            continue

        # Otherwise recurse upward into each caller (depth-bounded).
        if depth >= max_depth:
            # Bound reached — record as dead leaf so it is visible in logs
            # but do NOT promote (we don't actually know if it's terminal).
            dead_leaves.append({
                'file': file_path,
                'method': method,
                'reason': 'max-depth-reached',
            })
            continue
        for caller_file, caller_method in external_callers:
            new_chain = list(chain) + [f"{caller_file}:{caller_method}"]
            worklist.append((caller_file, caller_method, new_chain, depth + 1))

    return {
        'entry_points': entry_points,
        'dead_leaves': dead_leaves,
        'chain_rejections': [],   # the new walk has no phantom-chain step
        'invalid_entry_points': [],
        'total_entry_points': len(entry_points),
    }


def find_entry_points(repo_root, serena, changed_file=None, changed_method=None):
    """
    Find entry points = methods with no external (non-test, non-same-file) callers.

    Args:
        repo_root: Repository root path
        serena: Serena MCP client
        changed_file: Optional. If provided, scan ONLY this file for entry points (focused mode)
        changed_method: Optional. If provided, always include this method as an entry point
    """
    from pathlib import Path

    entry_points = []

    if changed_file:
        # FOCUSED MODE: Scan only changed file(s)
        print(f"Finding entry points (focused on changed file)...")
        changed_path = Path(repo_root) / changed_file
        if not changed_path.exists():
            print(f"  WARNING: Changed file not found: {changed_file}")
            return []
        source_files = [changed_path]
        print(f"  Scanning changed file: {changed_file}")
    else:
        # FULL MODE: Scan entire codebase
        # Phase 2: scan globs come from the active language profile. For Java
        # (default), profile['scan_glob'] == "**/*.java" so this is identical
        # to Phase 1.
        profile = get_active_profile()
        scan_glob = profile.get('scan_glob', '**/*.java')
        lang_name = profile.get('name', 'Java')
        print(f"Finding entry points (full codebase scan, language={lang_name})...")
        candidate_files = list(Path(repo_root).glob(scan_glob))

        # Filter out test files, generated code, and build artifacts
        # using the same skip-list as the trace API for consistency.
        source_files = [
            f for f in candidate_files
            if not _is_non_application_file(str(f.relative_to(repo_root)))
            and not _is_test_file(str(f.relative_to(repo_root)))
        ]
        print(f"  Scanning {len(source_files)} source files...")

    # If changed_method provided, always include it as an entry point
    if changed_method and changed_file:
        entry_points.append({
            'file': changed_file,
            'method': changed_method
        })
        print(f"  Added changed method as entry point: {changed_method}")

    # `processed` is mutated only from the main collector thread (each
    # future is awaited sequentially in the for-loop below), so no lock
    # is required here.
    processed = 0

    # File-content cache to avoid reading the same file multiple times when
    # validating each method-symbol it contains. Accessed from worker
    # threads via `_read_file_content`, so guard with a lock.
    _content_cache: dict = {}
    _content_cache_lock = threading.Lock()

    def _read_file_content(abs_path: str) -> str:
        # Fast path: cache hit (lock-protected dict read).
        with _content_cache_lock:
            cached = _content_cache.get(abs_path)
            if cached is not None:
                return cached
        try:
            with open(abs_path, 'r', encoding='utf-8', errors='replace') as fh:
                txt = fh.read()
        except Exception:
            txt = ''
        with _content_cache_lock:
            # Re-check (another thread may have populated).
            existing = _content_cache.get(abs_path)
            if existing is not None:
                return existing
            _content_cache[abs_path] = txt
        return txt

    def _is_public_visibility(content: str, method_name: str,
                              rel_path: str) -> bool:
        """Language-agnostic 'public visibility' check.

        Java / Kotlin: declaration must contain 'public' and not 'private'
                       or 'protected'. Default-package methods (no
                       modifier) ARE NOT counted as public for safety.
        TypeScript:   declaration must NOT contain 'private' or 'protected'.
                      'public' keyword optional. Top-level exported
                      functions are also public.
        JavaScript:   no syntactic visibility — top-level / exported
                      methods are considered public.
        Python:       method name must NOT start with '_' (PEP 8).
        Unknown:      conservative — assume non-public, fail closed.
        """
        if not content or not method_name:
            return False
        lang = detect_language_from_path(rel_path)
        if lang == 'python':
            return not method_name.startswith('_')
        if lang in ('javascript',):
            # No real visibility modifier; treat as public.
            return True
        if lang == 'typescript':
            # Look near the method declaration for 'private' / 'protected'.
            # Use a small window around the first declaration to avoid
            # false positives from comments elsewhere in the file.
            decl_re = re.compile(r'(?:public|private|protected|static|async|export)?\s*'
                                 + re.escape(method_name) + r'\s*\(')
            m = decl_re.search(content)
            if not m:
                return False
            window = content[max(0, m.start() - 60):m.start() + len(method_name)]
            return 'private' not in window and 'protected' not in window
        if lang == 'java':
            # Find the method declaration window and check modifiers.
            decl_re = re.compile(
                r'(?:public|protected|private|static|final|synchronized|abstract|native|default)?\s+'
                r'[\w<>\[\],\s\?]+\s+' + re.escape(method_name) + r'\s*\('
            )
            m = decl_re.search(content)
            if not m:
                return False
            window = content[max(0, m.start() - 1):m.end()]
            if 'public' in window and 'private' not in window and 'protected' not in window:
                return True
            return False
        return False

    def _safe_no_callers_fallback(content: str, method_name: str,
                                  rel_path: str) -> bool:
        """
        Smart no-callers fallback: a candidate that has zero external callers
        is accepted as an entry point ONLY when every YAML-driven filter for
        the active language passes. This is the data-driven replacement for
        the legacy hardcoded heuristic — patterns now live in
        ``configs/languages/<lang>.yaml`` and adding a new language requires
        no Python edits.

        Returns True when the method should be reported as an entry point.
        """
        detector, _ = _get_universal_detector()
        if detector is False or detector is None:
            # Fail closed: if the detector isn't available, do NOT promote a
            # no-callers method (matches conservative legacy behaviour for
            # broken setups).
            return False

        # Locate the declaration so we can pass an accurate offset to the
        # detector (modifier-window logic depends on it).
        try:
            decl_re = detector.get_adapter(
                detector.detect_language(rel_path) or ''
            ).method_declaration_regex(method_name) if detector.detect_language(rel_path) else None
        except Exception:
            decl_re = None
        if decl_re is None:
            return False
        decl_match = decl_re.search(content)
        if not decl_match:
            return False

        # Build a synthetic call-graph view that says "no external callers".
        # The caller already verified this; we just reflect it.
        from core.call_graph import InMemoryCallGraph
        cg = InMemoryCallGraph()

        ok, _reason = detector.is_entry_point(
            method_name=method_name,
            file_path=rel_path,
            file_content=content,
            call_graph=cg,
            method_offset=decl_match.start(),
        )
        return bool(ok)

    def _scan_one_file(file_path) -> list:
        """
        Scan a single source file for entry-point methods. Returns a
        list of entry-point dicts (in declaration order within the
        file). Raises any underlying exception so the caller can apply
        fail-fast handling - this preserves the original semantics.

        Parallelization: the per-method Layer-2 work calls
        `serena.find_referencing_symbols()`, which spawns a `git grep`
        subprocess (releases the GIL during I/O). We dispatch one
        worker per method to a `ThreadPoolExecutor(max_workers=32)`
        and collect results in declaration order so the output is
        BYTE-IDENTICAL to the previous sequential implementation.

        Thread-safety:
          * `serena.find_referencing_symbols()` is documented as
            parallel-safe (own subprocess + internal locks).
          * `is_legitimate_entry_point`, `_is_public_visibility`,
            `_safe_no_callers_fallback`, `_is_trivial_method` are
            pure functions over the cached file content (`file_content`
            captured once at the top of this function).
          * `local_eps` is built only on the main thread AFTER each
            future completes — workers return per-method results and
            do not mutate shared state.
        """
        rel_path = str(file_path.relative_to(repo_root))

        # Get all symbols (methods) in this file.
        symbols = serena.get_symbols_overview(rel_path)
        file_content = _read_file_content(str(file_path))

        # Pre-filter symbols to method/function kinds, drop trivial
        # methods, and pre-compute the declaration regex match (cheap,
        # CPU-bound). This keeps all expensive `git grep` work in the
        # parallel section below and matches the sequential semantics
        # exactly.
        method_tasks: list = []  # list of (method_name, decl_match)
        for sym in symbols.get('symbols', []):
            if sym['kind'] not in ('method', 'function'):
                continue
            method_name = sym['name']

            # Centralised trivial-method filter (Java-bean pattern + boilerplate).
            if _is_trivial_method(method_name):
                continue

            decl_match = re.search(
                r'(?:public|protected|private|static|final|synchronized|abstract|native|default)\s+'
                r'[\w<>\[\],\s\?]+\s+' + re.escape(method_name) + r'\s*\(',
                file_content
            )
            method_tasks.append((method_name, decl_match))

        def _classify_method(method_name: str, decl_match):
            """
            Per-method worker. Returns an entry-point dict or None.
            Pure function over its arguments + the captured `file_content`
            and `serena` — no shared-state mutation.
            """
            # ---- LAYER 1: PRIMARY DETECTION ----
            # Use the full annotation/inheritance/config-wired
            # detector. This is the same call stage1 makes and
            # gives us a consistent definition of 'entry point'
            # across the pipeline.
            primary_hit = False
            if file_content and decl_match:
                try:
                    is_ep, _reason = is_legitimate_entry_point(
                        file_content,
                        method_name,
                        decl_match.start(),
                        file_path=rel_path,
                        repo_root=repo_root,
                    )
                    primary_hit = bool(is_ep)
                except Exception:
                    primary_hit = False

            if primary_hit:
                return {
                    'file': rel_path,
                    'method': method_name,
                    'detection': 'primary',
                }

            # ---- LAYER 2: SAFE NO-CALLERS FALLBACK ----
            # Check if method has external (non-test, non-same-file) callers.
            refs_result = serena.find_referencing_symbols(
                method_name,
                from_file=rel_path,
            )
            refs = refs_result.get('references', [])
            external_refs = [
                ref for ref in refs if ref.get('file') != rel_path
            ]

            if len(external_refs) == 0 and file_content and decl_match:
                # USER-REQUIREMENT GATE: a method qualifies as an
                # entry point when it is publicly visible AND has
                # no external callers. Visibility is determined
                # language-specifically (Java public, Python no
                # underscore prefix, TS no private/protected, JS
                # exported / top-level).
                is_public = _is_public_visibility(
                    file_content, method_name, rel_path
                )
                if is_public:
                    return {
                        'file': rel_path,
                        'method': method_name,
                        'detection': 'public-no-callers',
                    }
                # YAML-detector fallback retains language_specific
                # checks (private/abstract/test_paths/vendor) for
                # cases the simple visibility regex can't decide.
                if _safe_no_callers_fallback(
                    file_content, method_name, rel_path
                ):
                    return {
                        'file': rel_path,
                        'method': method_name,
                        'detection': 'fallback:no-callers-safe',
                    }
            return None

        n_methods = len(method_tasks)
        if n_methods == 0:
            return []

        # Single-method shortcut avoids ThreadPoolExecutor overhead.
        if n_methods == 1:
            mname, dmatch = method_tasks[0]
            result = _classify_method(mname, dmatch)
            return [result] if result is not None else []

        # Parallel per-method execution. `subprocess.run()` inside
        # `serena.find_referencing_symbols()` releases the GIL, so
        # threads scale near-linearly with worker count. Results are
        # collected in declaration order to preserve determinism.
        per_method_results: list = [None] * n_methods
        with ThreadPoolExecutor(max_workers=32) as method_pool:
            future_to_idx = {
                method_pool.submit(_classify_method, mname, dmatch): idx
                for idx, (mname, dmatch) in enumerate(method_tasks)
            }
            for fut in future_to_idx:
                idx = future_to_idx[fut]
                # Propagate worker exceptions to preserve fail-fast
                # semantics (the outer `_scan_one_file` caller wraps
                # any exception in a RuntimeError with file context).
                per_method_results[idx] = fut.result()

        local_eps: list = [r for r in per_method_results if r is not None]
        return local_eps

    # Run the per-file scan SEQUENTIALLY. The previous implementation
    # wrapped this loop in a ThreadPoolExecutor(max_workers=16), but
    # `_scan_one_file` itself uses ThreadPoolExecutor(max_workers=32) for
    # its per-method `git grep` calls. Nesting the two pools created up
    # to 16 * 32 = 512 contending threads, which dominated the runtime
    # via context-switching and `git grep` subprocess thrash.
    #
    # Removing the outer pool keeps:
    #   * Deterministic ordering (results are already in source_files order).
    #   * Identical per-method parallelism (the inner 32-worker pool inside
    #     `_scan_one_file` is unchanged).
    #   * Same fail-fast semantics (any exception propagates immediately).
    #
    # Net effect: 4,223 files processed one-at-a-time, each fanning out to
    # 32 parallel `git grep` workers — total ~32-way parallelism instead of
    # 512-way contention.
    total_files = len(source_files)
    per_file_results: list = [None] * total_files
    for idx, file_path in enumerate(source_files):
        try:
            per_file_results[idx] = _scan_one_file(file_path)
        except Exception as e:
            rel_path = str(file_path.relative_to(repo_root))
            raise RuntimeError(
                f"[build_flow_registry] Entry-point scan failed for "
                f"file '{rel_path}': {e}\n"
                f"Root cause: serena.get_symbols_overview() or one of "
                f"the entry-point detectors raised an exception.\n"
                f"Fix: Inspect the offending file and verify the "
                f"active language profile in configs/ria_config.py."
            ) from e
        # Progress logging from the main thread (no lock needed since
        # this loop is single-threaded at the file level).
        processed += 1
        if processed % 100 == 0:
            interim = sum(
                len(r) for r in per_file_results if r is not None
            )
            print(
                f"  Progress: {processed}/{total_files} files, "
                f"found {interim} entry points so far..."
            )

    # Flatten in source_files order to keep determinism.
    for batch in per_file_results:
        if batch:
            entry_points.extend(batch)

    print(f"  Found {len(entry_points)} entry points")
    return entry_points


def _get_stop_words():
    """Return runtime stop-word set from GENERIC_DOMAIN_NOUNS (KB-driven).

    FAIL-FAST: GENERIC_DOMAIN_NOUNS MUST have been populated from the
    discovery KB (Stage 0d output). The previous hard-coded English
    fallback masked Stage-ordering bugs (Stage 0a running before Stage
    0d) by silently using a tiny generic word list.
    """
    if not GENERIC_DOMAIN_NOUNS:
        raise RuntimeError(
            "[build_flow_registry] _get_stop_words: "
            "GENERIC_DOMAIN_NOUNS is empty. Stage 0d "
            "(build_discovered_vocabularies.py) must run before "
            "Stage 0a uses stop words.\n"
            "Fix: Run 'python3 ria_agent.py --rebuild-kb' so the "
            "discovery KB is built before flow registry."
        )
    return set(GENERIC_DOMAIN_NOUNS)


def extract_keywords_from_test(test, synonym_groups=None):
    """
    Extract keywords from test using synonym groups as semantic dictionary (BUG-FR2 fix).

    Args:
        test: Test case dict
        synonym_groups: Dict of verb groups from synonym_groups.json

    Returns:
        Dict with verbs (semantic), domain_nouns (filtered), all_words
    """
    # Build verb whitelist from synonym groups.
    # MISC_GROUP cleanliness is now enforced upstream in
    # build_synonym_groups.similarity_cluster_unmatched(), which uses spaCy
    # Part-of-Speech tagging to admit only VERB-tagged tokens. That keeps the
    # verb whitelist consumed here free of UI/domain-noun pollution without
    # any local hardcoded filter list.
    all_verbs = set()
    if synonym_groups:
        for group_verbs in synonym_groups.values():
            for v in group_verbs:
                all_verbs.add(v.lower())

    # Stop words: prefer corpus-derived generic nouns (Stage 0d output),
    # fallback to a minimal English list for bootstrap runs.
    stop_words = _get_stop_words()

    # Combine test text
    step_text = ' '.join([
        (step.get('action') or '') + ' ' +
        (step.get('data') or '') + ' ' +
        (step.get('result') or '')
        for step in test.get('steps', [])
    ])

    combined = ' '.join([
        test.get('summary') or '',
        test.get('description') or '',
        step_text
    ]).lower()

    # Extract all words ≥3 chars
    words = re.findall(r'\b[a-z]{3,}\b', combined)

    # Classify semantically:
    # - If word in synonym groups → verb
    # - If not stop-word and ≥4 chars → domain noun
    # - Else → skip
    #
    # BUGFIX (2026-06-02): Allow dual verb/noun classification. Many domain
    # words function as BOTH verb and noun ("swap", "trade", "schedule",
    # "shift"). The original 'elif' made classification mutually exclusive,
    # causing Signal 1 (noun match) to fail for tests that mention verbs like
    # "swap" even when those verbs are also the distinguishing business noun
    # in the method name. Changed to independent 'if' statements so words can
    # be classified as both verb AND noun when appropriate.
    verbs = []
    domain_nouns = []

    for w in words:
        if all_verbs and w in all_verbs:
            verbs.append(w)
        if w not in stop_words and len(w) >= 4:
            domain_nouns.append(w)

    return {
        'nouns': list(set(domain_nouns)),  # Now filtered domain nouns
        'verbs': list(set(verbs)),          # Semantic verbs from synonym groups
        'all_words': list(set(words))
    }


def check_synonym_match(test_verb, method_verb, synonym_groups):
    """
    Check if test verb and method verb are in the same synonym group.
    Returns True if they're synonyms, False otherwise.
    """
    # Normalize verbs
    test_v = test_verb.lower().strip()
    method_v = method_verb.lower().strip()

    # Check each group
    for group_name, verbs in synonym_groups.items():
        verbs_lower = [v.lower() for v in verbs]
        if test_v in verbs_lower and method_v in verbs_lower:
            return True

    return False


# --------------------------------------------------------------------------
# Generic-noun stop list (QUALITY FIX 2026-05-10).
#
# Per HTML spec lines 376-381 ("Signal 1 - Domain Noun Match (10 pts):
# Extract nouns from test: Trade, Policy, WorkPolicy. Check if ANY noun in
# entry point method name. Example: 'Trade' in submitTradeRequest() -> +10
# pts"), the noun signal is intended to fire on SPECIFIC business nouns
# (Trade, Policy, WorkPolicy) - not on platform-level generics.
#
# Empirical evidence on the EEM corpus before this fix:
#   - Method getCalloutAgents has words {get, callout, agents}
#   - The word "agents" appears in 5,375 / 9,825 tests (55% of corpus)
#   - Signal 1 was firing on "agents" alone, awarding 10 pts to tests that
#     have nothing to do with the callout flow (e.g. "User Login Logout
#     Report", "Generate report for French Language", etc.)
#   - 2,999 tests with score >= 16 were tagged as [GET_CALLOUT_AGENTS]
#     yet didn't even mention 'callout' anywhere.
#
# Fix: when scoring Signal 1, ignore method words that are in this
# stop-list. The signal must fire on a method word that is genuinely
# domain-distinguishing (e.g. 'callout', 'schedule', 'workpolicy',
# 'trade'), not on a platform-wide concept ('agent', 'user', 'data').
# --------------------------------------------------------------------------
# GENERIC_DOMAIN_NOUNS is populated at runtime from the discovery KB
# (discovered_generic_nouns.json) which is generated by
# build_discovered_vocabularies.py during Stage 0d. This eliminates the
# previously hardcoded English/platform vocabulary and lets the set adapt
# to whatever codebase / test corpus the pipeline runs against.
def _load_generic_nouns(kb_dir):
    """Load discovered generic nouns from KB (runtime-generated).

    FAIL-FAST: The discovered_generic_nouns.json file MUST exist; it is
    a Stage 0d artifact required by Stage 0a. Returning an empty set
    used to mask Stage ordering bugs.
    """
    from pathlib import Path as _Path
    path = _Path(kb_dir) / "discovered_generic_nouns.json"
    if not path.exists():
        raise FileNotFoundError(
            f"[build_flow_registry] discovered_generic_nouns.json not "
            f"found at: {path}\n"
            f"Root cause: Stage 0d (build_discovered_vocabularies.py) "
            f"did not run before Stage 0a (build_flow_registry.py).\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb' which "
            f"orchestrates the stages in the correct order."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    nouns = set(data.get("generic_nouns", []))
    if not nouns:
        raise RuntimeError(
            f"[build_flow_registry] {path} contains zero generic_nouns.\n"
            f"Fix: Re-run 'python3 ria_agent.py --rebuild-kb'."
        )
    return nouns


# Lazily filled in build_flow_registry() once we know the KB output dir.
GENERIC_DOMAIN_NOUNS = set()


# --------------------------------------------------------------------------
# Tag-quality thresholds (QUALITY FIX 2026-05-10).
# --------------------------------------------------------------------------
# MIN_TAG_SCORE: absolute floor for assigning ANY flow tag. A test cannot be
# tagged unless it clears this bar, regardless of how its score compares to
# the per-test best.
#
# Spec basis: HTML spec example (lines 418-424) shows realistic scenario
# scores of 15 and 12 with threshold 10.5. The spec NEVER shows a tagged
# test with score < 10. Score < 10 implies Signal 1 (domain noun, 10pts)
# did NOT fire - i.e. the test does not mention any word from the entry
# point method's name. Such tests have no semantic claim to be tagged.
#
# Setting MIN_TAG_SCORE = 10 enforces "domain noun match is REQUIRED" per
# the spec's intent, while still allowing the existing 70%-of-best
# multi-flow ratio to work above that floor.
#
# This default is also the algorithmic floor that maps to "Signal 1 fired
# at least once" — see calculate_adaptive_min_score() below for the
# corpus-aware override that keeps quality stable when scoring grows.
MIN_TAG_SCORE = 10

# MULTI_FLOW_THRESHOLD_RATIO: keeps the 70% multi-flow expansion (spec line
# 415: "threshold = best_score x 0.70 (70% of best)") but the absolute
# floor above takes precedence.
MULTI_FLOW_THRESHOLD_RATIO = 0.70

# --------------------------------------------------------------------------
# Note: the embedding-based tag-time rescue fallback was removed as part of
# the PREVENTION-OVER-DETECTION refactor.  Tests that fail the lexical
# 3-signal floor are now expected to be rescued by improving the synonym
# groups / component map (Stage 0 builders) rather than papered over with
# a similarity threshold.  See docs/RIA-pipeline-prevention-over-detection.md
# --------------------------------------------------------------------------


def calculate_adaptive_min_score(scores_list, default=MIN_TAG_SCORE):
    """
    Pick a score floor that adapts to the corpus's actual score distribution.

    Algorithm:
      - When the corpus is empty / scoring failed, return the default (10).
      - Otherwise compute the 90th percentile of NON-ZERO scores. If that
        percentile is below the default we keep the default (so the
        Signal-1 quality gate never weakens). If it is above, we use it
        because the corpus genuinely scores higher and we don't want to
        flood the registry with marginal matches.
    """
    nonzero = [s for s in scores_list if s and s > 0]
    if not nonzero:
        return default
    nonzero.sort()
    pct90 = nonzero[max(0, int(len(nonzero) * 0.90) - 1)]
    # Use the higher of the two; never weaken below the spec default.
    return max(default, int(pct90 // 2))


def calculate_multi_flow_ratio(scores_list, default=MULTI_FLOW_THRESHOLD_RATIO):
    """
    Pick the secondary-tag ratio dynamically from the score distribution.

    If scores cluster tightly (low coefficient of variation), keep 0.70.
    If scores are spread out (high CV), tighten to 0.80 so we don't pull
    in many marginal flows just because there's one outlier dominant.
    """
    nonzero = [s for s in scores_list if s and s > 0]
    if len(nonzero) < 5:
        return default
    mean = sum(nonzero) / len(nonzero)
    if mean <= 0:
        return default
    var = sum((s - mean) ** 2 for s in nonzero) / len(nonzero)
    cv = (var ** 0.5) / mean
    if cv > 0.5:
        return 0.80
    return default


def score_test_against_entry_point(test, entry_point, synonym_groups, test_keywords=None):
    """
    Score test against entry point using 3-signal algorithm.

    Signal 1: Domain noun match (10 pts) - word-boundary match (BUG-FR3 fix)
              + GENERIC_DOMAIN_NOUNS exclusion (QUALITY FIX 2026-05-10)
    Signal 2: Verb synonym match (5 pts) - uses synonym_groups (BUG-FR2 fix)
    Signal 3: Keyword overlap (1-3 pts)

    PERF (2026-05-26): Optional ``test_keywords`` lets callers pre-compute the
    keyword extraction once per test and reuse it across many entry points,
    eliminating ~1.8M redundant ``extract_keywords_from_test`` calls in the
    full-corpus scoring pass. When ``test_keywords`` is None (default), the
    behaviour is byte-identical to the original implementation: the function
    extracts keywords itself, preserving backward compatibility for external
    callers (e.g. ria_agent.py).
    """
    score = 0
    method_name = entry_point['method']

    # BUG-FR2 fix: pass synonym_groups so verbs are extracted semantically.
    # PERF: reuse caller-supplied keywords when available (cache hit path).
    if test_keywords is None:
        test_keywords = extract_keywords_from_test(test, synonym_groups)

    # Language-aware identifier split (Java/JS/TS camelCase OR Python snake_case).
    # Falls back to camelCase if no language hint is provided.
    lang_hint = detect_language_from_path(entry_point.get('file', ''))
    method_parts = split_method_name(method_name, language=lang_hint)
    method_words = set(p.lower() for p in method_parts if p)

    # Distinguishing method words = method words minus generic nouns. These
    # are the words that genuinely identify the flow's business domain.
    # Examples for getCalloutAgents -> {callout} (since 'get' and 'agents'
    # are generic).
    distinguishing_method_words = method_words - GENERIC_DOMAIN_NOUNS

    # Signal 1: Domain noun match (10 pts) - word-boundary match (BUG-FR3 fix)
    # QUALITY FIX 2026-05-10: only count the match if the matching method
    # word is DISTINGUISHING (i.e. not in GENERIC_DOMAIN_NOUNS). This
    # prevents the "agents" noun from awarding 10 pts to half the corpus.
    #
    # Edge case: if the method has NO distinguishing words (e.g. 'run',
    # 'call', 'doPost'), the noun signal cannot fire at all - which is
    # correct behaviour, because such methods carry no business identity.
    if distinguishing_method_words:
        for noun in test_keywords['nouns']:
            if noun in distinguishing_method_words:
                score += 10
                break  # Only count once

    # Signal 2: Verb synonym match (5 pts)
    method_verb = method_parts[0].lower() if method_parts else ''

    # Check if any test verb matches method verb via synonyms
    for test_verb in test_keywords['verbs']:
        if check_synonym_match(test_verb, method_verb, synonym_groups):
            score += 5
            break  # Only count once

    # Signal 3: Keyword overlap (1-3 pts)
    # QUALITY FIX 2026-05-10: overlap is computed against DISTINGUISHING
    # words only. Counting an overlap on 'agents' or 'get' inflates the
    # score on tests that have nothing in common with the flow.
    test_words = set(test_keywords['all_words'])
    overlap_words = distinguishing_method_words.intersection(test_words)
    overlap = len(overlap_words)
    score += min(overlap, 3)  # Cap at 3 points

    return score


# Fix #7: Single-word generic verbs that are valid Java/JS framework
# interface methods but carry no business meaning on their own. When the
# method name decomposes to ONLY one of these tokens (e.g. Callable.call,
# Runnable.run, Supplier.get) the resulting flow name "Call" / "Run" /
# "Get" collides with the verb itself and gives no hint about which
# component the flow belongs to. We prefix the class context in those
# cases so the report shows e.g. "TargetAgentsCallable: Call" instead of
# the bare verb.
_GENERIC_INTERFACE_VERBS = frozenset([
    'call', 'run', 'execute', 'apply', 'accept', 'get', 'set',
    'do', 'handle', 'invoke', 'process', 'perform', 'compute',
    'supply', 'consume', 'test',
])


def _class_context_from_file(file_path: str) -> str:
    """Extract the class/file basename to use as flow-name prefix.

    Strips common path separators and source-file extensions so the
    returned label is the bare PascalCase / snake_case identifier (e.g.
    'TargetAgentsCallable' from 'foo/bar/TargetAgentsCallable.java').
    Returns an empty string when no usable basename can be derived.
    """
    if not file_path:
        return ''
    base = file_path.replace('\\', '/').rsplit('/', 1)[-1]
    for ext in ('.java', '.kt', '.py', '.ts', '.tsx', '.js', '.jsx',
                '.mjs', '.cs', '.go', '.rb'):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    return base.strip()


def derive_flow_name(entry_point, synonym_groups=None):
    """
    Derive flow name from entry point method name using semantic analysis.

    Strategy:
    1. First word = verb (action) - always keep
    2. Filter out connector words semantically (common prepositions/conjunctions)
    3. Keep remaining words = domain nouns (entities)
    4. Check if word exists in synonym groups = likely a verb, keep it

    Fix #7: When the resulting name is a single generic interface verb
    (e.g. "Call", "Run", "Get"), the bare verb collides with the action
    keyword and tells the reader nothing about which component the flow
    belongs to. In that case we prefix the class/file basename to give
    business context (e.g. "TargetAgentsCallable: Call").
    """
    method_name = entry_point['method']

    # Language-aware identifier split. Java / JS / TS use camelCase;
    # Python uses snake_case. The helper centralises that decision.
    lang_hint = detect_language_from_path(entry_point.get('file', ''))
    # Use the original token (no lowercasing) so we can capitalize per word.
    if lang_hint == 'python' or '_' in method_name:
        raw_parts = []
        for piece in method_name.split('_'):
            if not piece:
                continue
            sub = _CAMEL_RE.findall(piece)
            if sub:
                raw_parts.extend(sub)
            else:
                raw_parts.append(piece)
        parts = raw_parts
    else:
        parts = _CAMEL_RE.findall(method_name)

    if len(parts) == 0:
        return method_name

    # Connector words (prepositions, conjunctions, articles). Prefer the
    # corpus-derived generic-noun set when available — that set is built
    # from MAD-z anomalies on the actual codebase + test corpus, so it
    # always contains the platform-wide "noise" words the current product
    # actually uses. The hardcoded list below is a strict English-only
    # fallback for first-run / no-KB bootstraps.
    connectors = set()
    if GENERIC_DOMAIN_NOUNS:
        # Use only the short tokens (<=4 chars) from the discovered set:
        # they are the prepositions / connectors that pollute flow names.
        connectors |= {t for t in GENERIC_DOMAIN_NOUNS if len(t) <= 4}
    connectors |= {
        'by', 'and', 'or', 'for', 'with', 'from', 'to', 'in', 'on', 'at',
        'of', 'the', 'a', 'an', 'is', 'as', 'via', 'per', 'into', 'onto',
        'if', 'be', 'do', 'so', 'no', 'not'
    }

    # Build all verb terms from synonym groups (if available)
    all_verbs = set()
    if synonym_groups:
        for group_verbs in synonym_groups.values():
            all_verbs.update(v.lower() for v in group_verbs)

    # Filter parts:
    # - Keep first word (verb)
    # - Keep words that are in synonym groups (actions/verbs)
    # - Keep words NOT in connectors (domain nouns)
    # - Skip connector words

    filtered = []
    for i, part in enumerate(parts):
        part_lower = part.lower()

        if i == 0:
            # Always keep first word (main verb)
            filtered.append(part)
        elif part_lower in connectors:
            # Skip connectors
            continue
        elif all_verbs and part_lower in all_verbs:
            # Keep if it's a verb from synonym groups
            filtered.append(part)
        elif len(part_lower) >= 3:
            # Keep words 3+ chars that aren't connectors (likely domain nouns)
            filtered.append(part)
        # else: skip short non-verb words (e.g., "Id", "No")

    if len(filtered) >= 1:
        # Capitalize first word, keep rest as-is
        derived = filtered[0].capitalize() + (
            ' ' + ' '.join(filtered[1:]) if len(filtered) > 1 else ''
        )
    else:
        derived = method_name

    # Fix #7: When the derived name is a single generic interface verb,
    # prefix the class context so the flow is identifiable in the report.
    # Use a space separator (not a colon) so the existing
    # `flow_tag = name.upper().replace(' ', '_')` pipeline produces a
    # well-formed [TARGETAGENTSCALLABLE_CALL] tag without special-casing.
    derived_tokens = [t.lower() for t in derived.split() if t]
    if (len(derived_tokens) == 1
            and derived_tokens[0] in _GENERIC_INTERFACE_VERBS):
        class_ctx = _class_context_from_file(entry_point.get('file', ''))
        if class_ctx and class_ctx.lower() != derived_tokens[0]:
            return f"{class_ctx} {derived}"
    return derived


def build_flow_registry(test_corpus_path, repo_root, synonym_groups_path, output_dir,
                        changed_file=None, changed_method=None,
                        changed_files=None, output_filename=None,
                        enriched_output=None, pipeline_type=None,
                        kb_input_dir=None):
    """
    Build flow registry using 3-signal scoring.

    Args:
        test_corpus_path: Path to all_tcs_extracted.json
        repo_root: Repository root
        synonym_groups_path: Path to synonym_groups.json
        output_dir: Output directory
        changed_file: Optional. If provided with changed_method, trace to affected entry points
        changed_method: Optional. If provided, trace call tree to find affected entry points
        changed_files: Optional list of files. When provided WITHOUT changed_method,
            scan each file for entry points and union the results. This is the
            FOCUSED-multi-file mode used by the dependency-change pipeline where
            multiple production files import a changed library but no specific
            method is the "entry" (e.g. TokenUtility.java + JwtAuthProvider.java
            both import io.jsonwebtoken.jjwt). Mutually exclusive with the
            single-file `changed_file` + `changed_method` FOCUSED mode.
        output_filename: Optional output filename (default: 'flow_registry.json').
            Used by the dependency pipeline to write 'flow_registry_dependency.json'
            so the source pipeline's registry is not overwritten.
        enriched_output: Optional explicit filename (relative to output_dir, or
            absolute path) for the enriched test corpus. Together with
            `pipeline_type` this lets the orchestrator route enrichment output
            into per-pipeline files (`*_source.json`, `*_dependency.json`).
            When neither `pipeline_type` nor `enriched_output` is supplied,
            the source-pipeline filename (`all_tcs_extracted_enriched_source.json`)
            is used by default — the legacy backward-compat file is no longer
            produced.
        pipeline_type: Optional. One of 'source' | 'dependency'. When set:
            - 'source': writes the enriched corpus ONLY to
              `all_tcs_extracted_enriched_source.json`. `enriched_output` is
              ignored when this is supplied.
            - 'dependency': writes the enriched corpus ONLY to
              `all_tcs_extracted_enriched_dependency.json`. The source-pipeline
              file is left untouched so the source pipeline's enriched corpus
              is preserved when dependency analysis runs after source analysis.
            When None, behaviour depends on `enriched_output` (explicit path)
            or defaults to the source-pipeline filename.
        kb_input_dir: Optional. Directory to read KB INPUT artifacts from
            (specifically discovered_generic_nouns.json). When None, defaults
            to `output_dir` for backward compatibility. This separation lets
            multi-method workers write OUTPUTS to per-worker scratch dirs
            while still reading shared one-time KB INPUTS from the canonical
            KB directory.
    """
    print(f"\n{'=' * 80}")
    print(f"STAGE 0a: Build Flow Registry")

    # Load discovered generic-noun vocabulary (Stage 0d output). This was a
    # hardcoded set in earlier revisions; it is now data-driven. Read inputs
    # from kb_input_dir when supplied so per-worker output dirs do not need
    # the one-time KB inputs duplicated.
    global GENERIC_DOMAIN_NOUNS
    _input_dir_for_inputs = kb_input_dir if kb_input_dir else output_dir
    GENERIC_DOMAIN_NOUNS = _load_generic_nouns(_input_dir_for_inputs)
    print(f"Loaded {len(GENERIC_DOMAIN_NOUNS)} discovered generic nouns from KB")

    # Initialize Serena MCP
    serena = SerenaMCPClient(repo_path=repo_root, enabled=True, max_symbols=10000)

    # Determine entry points based on mode
    if changed_files and not changed_method:
        # FOCUSED-MULTI-FILE MODE: Used by the dependency-change pipeline.
        # We have a list of production files that import a changed third-party
        # library but no specific changed method. For each file, extract all
        # public methods, then walk the call graph upward to find callers that
        # are true entry points (controllers, REST endpoints, etc.).
        print(f"Mode: FOCUSED-MULTI-FILE ({len(changed_files)} files, no specific method)")
        for cf in changed_files:
            print(f"  - {cf}")
        print(f"{'=' * 80}")

        all_eps: list = []
        seen_keys: set = set()

        for cf in changed_files:
            # Extract all public methods from this file
            try:
                methods = _extract_public_methods_from_file(repo_root, cf)
            except Exception as exc:
                print(f"  [WARN] method extraction failed for {cf}: {exc}")
                continue

            print(f"  File: {cf} - found {len(methods)} public methods")

            # For each method, walk up the call graph to find callers
            for method_name in methods:
                try:
                    result = _focused_walk_no_callers_entry_points(
                        serena=serena,
                        repo_root=repo_root,
                        changed_file=cf,
                        changed_method=method_name,
                        max_depth=10,
                        max_callers_per_node=100,
                    )

                    traced_eps = result.get('entry_points', [])
                    for ep in traced_eps:
                        # Validate each entry point
                        ok, reason = _validate_entry_point(ep, repo_root=repo_root)
                        if ok:
                            key = (ep.get('file'), ep.get('method'))
                            if key not in seen_keys:
                                seen_keys.add(key)
                                all_eps.append(ep)
                except Exception as exc:
                    print(f"    [WARN] call-graph walk failed for {cf}:{method_name}: {exc}")
                    continue

        print(f"  Discovered {len(all_eps)} unique entry point(s) across "
              f"{len(changed_files)} file(s)")
        entry_points = all_eps
    elif changed_file and changed_method:
        # FOCUSED MODE: Trace call tree from changed method to find affected entry points
        print(f"Mode: FOCUSED (tracing from changed method)")
        print(f"Changed method: {changed_method}")
        print(f"Changed file: {changed_file}")
        print(f"{'=' * 80}")

        print(f"\nWalking call tree (no-callers => entry-point rule)...")
        # New simplified discovery: a method with no internal/external
        # callers is an entry point (subject to YAML filters in
        # core.detector). Replaces the framework-annotation-driven
        # trace_call_chain_to_entry_points which rejected legitimate
        # surface methods that lacked an explicit @RestController /
        # @Path / extends-HttpServlet marker.
        result = _focused_walk_no_callers_entry_points(
            serena=serena,
            repo_root=repo_root,
            changed_file=changed_file,
            changed_method=changed_method,
            max_depth=10,
            max_callers_per_node=100,
        )

        # Extract entry points from walk result
        traced_entry_points = result.get('entry_points', [])
        dead_leaves = result.get('dead_leaves', [])
        chain_rejections = result.get('chain_rejections', [])
        print(f"  Found {len(traced_entry_points)} entry points (methods with no callers)")
        if dead_leaves:
            print(f"  Skipped {len(dead_leaves)} dead leaves (filtered by YAML rules)")
            for dl in dead_leaves[:10]:
                print(f"    dead-leaf: {dl.get('file')}:{dl.get('method')} ({dl.get('reason')})")
            if len(dead_leaves) > 10:
                print(f"    ... and {len(dead_leaves) - 10} more")

        # Post-discovery validation: drop anything that survived the trace
        # but is still pollution (RIA tool, generated, build, test, trivial)
        # OR is not a true framework entry point.
        validated: list = []
        rejection_reasons: dict = {}
        for ep in traced_entry_points:
            ok, reason = _validate_entry_point(ep, repo_root=repo_root)
            if ok:
                validated.append(ep)
            else:
                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1

        print(f"  After validation: {len(validated)} entry points")
        if rejection_reasons:
            for reason, count in sorted(rejection_reasons.items(),
                                        key=lambda kv: -kv[1]):
                print(f"    rejected ({count}): {reason}")

        # Convert to expected format, preserving entry_type and chain
        # for downstream stages and documentation.
        entry_points = [
            {
                'file': ep['file'],
                'method': ep['method'],
                'entry_type': ep.get('entry_type', 'unknown'),
                'chain': ep.get('chain', []),
            }
            for ep in validated
        ]
    else:
        # FULL MODE: Scan entire codebase
        print(f"Mode: FULL (entire codebase)")
        print(f"{'=' * 80}")
        entry_points = find_entry_points(repo_root, serena, changed_file=None, changed_method=None)
    if not entry_points:
        # FAIL-FAST: An empty entry-point set guarantees an empty flow
        # registry, which then triggers fail-fast errors in every
        # downstream stage. Surface the cause here instead of writing an
        # empty placeholder file that defers the error.
        raise RuntimeError(
            f"[build_flow_registry] No entry points discovered in "
            f"{repo_root}.\n"
            f"Root cause: either find_entry_points produced zero results "
            f"(scan glob did not match any source files) or every "
            f"candidate was filtered as test/generated/trivial.\n"
            f"Fix: Verify the active language profile in "
            f"configs/ria_config.py points at the correct source "
            f"extensions, and that the repository has at least one "
            f"non-test source file with a public method."
        )

    # Load synonym groups. FAIL-FAST: empty/missing synonym_groups silently
    # disables Signal 2 (verb match), capping scoring at 13 pts and producing
    # a sparse flow_registry. Stage 0 ordering (build_synonym_groups runs
    # before build_flow_registry) MUST guarantee this file exists.
    print("\nLoading synonym groups...")
    if not os.path.isfile(synonym_groups_path):
        raise FileNotFoundError(
            f"[build_flow_registry] synonym_groups.json not found at: "
            f"{synonym_groups_path}\n"
            f"Root cause: Stage 0 (build_synonym_groups.py) did not run "
            f"before Stage 0a (build_flow_registry.py).\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb' which "
            f"orchestrates the stages in the correct order."
        )
    try:
        with open(synonym_groups_path, 'r', encoding='utf-8') as f:
            synonym_data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(
            f"[build_flow_registry] Failed to parse synonym groups at "
            f"{synonym_groups_path}: {e}\n"
            f"Root cause: file is unreadable or contains invalid JSON.\n"
            f"Fix: Re-run 'python3 ria_agent.py --rebuild-kb'."
        ) from e
    synonym_groups = synonym_data.get('synonym_groups', {}) or {}
    if not synonym_groups:
        raise RuntimeError(
            f"[build_flow_registry] synonym_groups.json at "
            f"{synonym_groups_path} contains zero synonym groups.\n"
            f"Root cause: build_synonym_groups.py produced an empty map.\n"
            f"Fix: Re-run 'python3 ria_agent.py --rebuild-kb'."
        )
    print(f"  Loaded {len(synonym_groups)} synonym groups")

    # Load tests. FAIL-FAST: empty/missing corpus produces a registry with
    # zero tests tagged, which silently breaks every downstream stage. The
    # test corpus is the input that drives flow discovery; without it the
    # KB is meaningless.
    print("\nLoading test corpus...")
    if not os.path.isfile(test_corpus_path):
        raise FileNotFoundError(
            f"[build_flow_registry] Test corpus not found at: "
            f"{test_corpus_path}\n"
            f"Root cause: tc_extractor.py did not produce the corpus, or "
            f"the path is misconfigured.\n"
            f"Fix: Verify the corpus exists or re-run "
            f"'python3 ria_agent.py --rebuild-kb'."
        )
    try:
        with open(test_corpus_path, 'r', encoding='utf-8') as f:
            tests = json.load(f) or []
    except (json.JSONDecodeError, OSError) as e:
        raise RuntimeError(
            f"[build_flow_registry] Failed to parse test corpus at "
            f"{test_corpus_path}: {e}\n"
            f"Root cause: file is unreadable or contains invalid JSON.\n"
            f"Fix: Re-extract with tc_extractor.py and re-run."
        ) from e
    if not isinstance(tests, list):
        raise RuntimeError(
            f"[build_flow_registry] Test corpus at {test_corpus_path} is "
            f"not a JSON array (got {type(tests).__name__}).\n"
            f"Fix: regenerate the corpus with tc_extractor.py."
        )
    if not tests:
        raise RuntimeError(
            f"[build_flow_registry] Test corpus at {test_corpus_path} is "
            f"empty (zero tests).\n"
            f"Fix: regenerate the corpus with tc_extractor.py."
        )
    print(f"  Loaded {len(tests)} tests")

    # Score each test against ALL entry points
    print("\nScoring tests against entry points...")
    enriched_tests = []
    flow_registry = defaultdict(lambda: {'entry_points': set(), 'tests': []})
    untagged_count = 0  # Track tests that scored below threshold (no fallback)

    # PERF (2026-05-26): Local keyword cache.
    # Without this, extract_keywords_from_test() runs once per (test, entry_point)
    # pair = ~10K tests x ~30 entry points = ~300K extractions; with the noun
    # match short-circuit removed it can balloon further. The result is purely
    # a function of (test, synonym_groups) and synonym_groups is loaded once
    # above and never mutated, so caching by test identity is safe and yields
    # byte-identical scoring.
    #
    # Cache key: prefer test['issue_key'] (verified unique + non-null across the
    # full corpus); fall back to id(test) defensively for any edge case.
    # Cache scope: function-local - dies when this invocation returns. No
    # global state, no cross-call leakage.
    _keyword_cache: dict = {}

    def _extract_keywords_cached(_test):
        """Cached wrapper around extract_keywords_from_test.

        synonym_groups is captured from the enclosing scope (it is loaded once
        per build_flow_registry call and is invariant for the remainder of
        the call).
        """
        cache_key = _test.get('issue_key') or id(_test)
        cached = _keyword_cache.get(cache_key)
        if cached is not None:
            return cached
        result = extract_keywords_from_test(_test, synonym_groups)
        _keyword_cache[cache_key] = result
        return result

    # PASS 1: score every test, collect per-test best score so we can
    # compute the adaptive floor / ratio over the actual distribution.
    per_test_best: list = []
    per_test_scores: list = []  # list of (test, scores_dict)
    for i, test in enumerate(tests):
        if (i + 1) % 500 == 0:
            print(f"  Processed {i + 1}/{len(tests)} tests...")
        test_keywords = _extract_keywords_cached(test)
        scores = {}
        for ep in entry_points:
            ep_key = f"{ep['file']}:{ep['method']}"
            scores[ep_key] = score_test_against_entry_point(
                test, ep, synonym_groups, test_keywords=test_keywords
            )
        per_test_scores.append((test, scores))
        if scores:
            per_test_best.append(max(scores.values()))

    # Adaptive thresholds derived from the actual score distribution.
    adaptive_min_score = calculate_adaptive_min_score(
        per_test_best, default=MIN_TAG_SCORE
    )
    adaptive_ratio = calculate_multi_flow_ratio(
        per_test_best, default=MULTI_FLOW_THRESHOLD_RATIO
    )
    print(f"Adaptive thresholds: min_score={adaptive_min_score} "
          f"(default {MIN_TAG_SCORE}), ratio={adaptive_ratio:.2f} "
          f"(default {MULTI_FLOW_THRESHOLD_RATIO:.2f})")

    # FALLBACK REMOVED (P0-4): The embedding similarity fallback that
    # rescued tests scoring < adaptive_min_score has been removed.
    # Tests must score on the lexical 3-signal pipeline alone; rescuing
    # them via cosine similarity masked synonym_groups.json gaps and
    # produced false positives. The variables below are kept as no-ops
    # so any external caller importing them does not break.
    embeddings_index = None
    method_emb_cache: dict = {}
    fallback_tagged_count = 0

    # PASS 2: tag tests with flows above the adaptive thresholds.
    for test, scores in per_test_scores:
        # Multi-flow tagging: Assign ALL flows above threshold
        if scores:
            best_ep_key = max(scores, key=scores.get)
            best_score = scores[best_ep_key]

            # QUALITY FIX 2026-05-10: enforce absolute minimum score floor
            # in addition to the 70%-of-best multi-flow ratio. Spec lines
            # 376-381 require Signal 1 (10 pts) for genuine flow membership;
            # tests scoring below 10 have not demonstrated a domain-noun
            # match and should NOT be tagged. See MIN_TAG_SCORE comment
            # block above for the empirical evidence.
            if best_score >= adaptive_min_score:
                # Threshold: max(absolute floor, ratio * best score).
                # Both conditions must be met. The ratio still allows a
                # test to be tagged with multiple flows when it has multiple
                # genuine signals; the floor prevents a degenerate
                # everyone-scores-5 scenario from tagging the entire corpus.
                # Both values are corpus-adaptive (see PASS 1 above).
                score_threshold = max(adaptive_min_score, best_score * adaptive_ratio)

                # Find ALL entry points above threshold
                matched_flows = []
                flow_scores = {}

                for ep_key, score in scores.items():
                    if score >= score_threshold:
                        file_path, method = ep_key.split(':', 1)
                        ep = {'file': file_path, 'method': method}
                        flow_name = derive_flow_name(ep, synonym_groups)

                        # Gate: skip flows whose name is entirely composed
                        # of generic/framework tokens (e.g. "Call", "Run",
                        # "Execute").  These are JDK/framework interface
                        # methods (Callable.call, Runnable.run) whose names
                        # carry no business semantics.  The generic-noun set
                        # is data-driven (discovered_generic_nouns.json).
                        _fn_tokens = [t.lower() for t in
                                      re.findall(r'[A-Za-z]{3,}', flow_name)]
                        if _fn_tokens and all(
                            t in GENERIC_DOMAIN_NOUNS for t in _fn_tokens
                        ):
                            continue

                        flow_tag = f"[{flow_name.upper().replace(' ', '_')}]"

                        matched_flows.append({
                            'flow_name': flow_name,
                            'flow_tag': flow_tag,
                            'entry_point': ep_key,
                            'score': score
                        })
                        flow_scores[flow_tag] = score

                # BUG-FR5 fix: Sort matched_flows by score DESC so primary_flow
                # is the actual highest-scoring flow (not dict iteration order).
                matched_flows.sort(key=lambda x: x['score'], reverse=True)

                # Enrich test with ALL matched flows
                if matched_flows:
                    enriched_test = test.copy()
                    enriched_test['auto_tags'] = [mf['flow_tag'] for mf in matched_flows]
                    enriched_test['discovered_entry_points'] = [mf['entry_point'] for mf in matched_flows]
                    enriched_test['flow_scores'] = flow_scores  # Map of flow_tag -> score
                    enriched_test['primary_flow'] = matched_flows[0]['flow_tag']  # Highest scoring (post-sort)
                    enriched_tests.append(enriched_test)

                    # Add to flow registry for ALL matched flows
                    for mf in matched_flows:
                        flow_name = mf['flow_name']
                        # Store full entry point (file:method) for complete context
                        entry_point_full = mf['entry_point']  # format: "file/path.java:methodName"
                        flow_registry[flow_name]['entry_points'].add(entry_point_full)
                        flow_registry[flow_name]['tests'].append(test.get('issue_key'))

            # ----------------------------------------------------------------
            # NO FALLBACK: Tests that score below adaptive_min_score are REJECTED.
            #
            # REMOVED: Embedding similarity fallback (previously allowed tests
            # scoring < 10 to be tagged via semantic similarity >= 0.50).
            #
            # REASON: Silent fallbacks hide data quality issues. If a test
            # scores < 10, it means:
            #   1. Test description is too generic (lacks flow-specific keywords), OR
            #   2. synonym_groups.json is incomplete (missing synonyms), OR
            #   3. Test is genuinely unrelated to any flow (should not be tagged)
            #
            # FAIL FAST: The pipeline will now REPORT which tests failed to tag,
            # allowing the user to decide: improve test descriptions, enrich
            # synonym_groups.json, or accept that those tests don't map to flows.
            # ----------------------------------------------------------------
            elif best_score > 0 and best_score < adaptive_min_score:
                # Test scored below threshold - DO NOT TAG
                # Log for transparency but don't fail the entire pipeline
                untagged_count += 1
                if untagged_count <= 10:  # Log first 10 for debugging
                    print(f"[WARN] Test {test.get('issue_key')} scored {best_score} "
                          f"(threshold {adaptive_min_score}) - NOT TAGGED")
                    print(f"       Best entry point: {best_ep_key}")
                    print(f"       Summary: {test.get('summary', '')[:80]}...")
                # After Stage 0 completes, report total count
                pass  # Explicitly do nothing - no tagging, no fallback

    # Embedding fallback was removed (P0-4). No rescue summary to print.

    # Convert sets to lists for JSON serialization
    flows = []
    for i, (flow_name, data) in enumerate(flow_registry.items(), 1):
        flows.append({
            'flow_id': f'FLOW_{i:03d}',
            'flow_name': flow_name,
            'test_tags': [f"[{flow_name.upper().replace(' ', '_')}]"],
            'entry_points': list(data['entry_points']),
            'test_count': len(data['tests'])
        })

    # Save enriched test corpus.
    # Option A (separate enriched corpus per pipeline): the legacy single-file
    # `all_tcs_extracted_enriched.json` is no longer produced. Source pipeline
    # writes ONLY `all_tcs_extracted_enriched_source.json`; dependency pipeline
    # writes ONLY `all_tcs_extracted_enriched_dependency.json`. The RIA_INPUT
    # mirror that historically duplicated the corpus alongside the raw input
    # has also been removed — only the per-pipeline KB files are produced.

    # Determine which file(s) inside output_dir get the enriched corpus.
    enriched_kb_paths = []
    pt = (pipeline_type or '').strip().lower()
    if pt == 'source':
        # Source pipeline: write ONLY the per-pipeline source file. The
        # legacy backward-compat file is no longer produced.
        enriched_kb_paths.append(
            os.path.join(output_dir, 'all_tcs_extracted_enriched_source.json'))
    elif pt == 'dependency':
        # Dependency pipeline: write ONLY the per-pipeline dependency file.
        # Do NOT overwrite the source pipeline's enriched corpus.
        enriched_kb_paths.append(
            os.path.join(output_dir, 'all_tcs_extracted_enriched_dependency.json'))
    elif enriched_output:
        # Explicit override (advanced callers / tests).
        if os.path.isabs(enriched_output):
            enriched_kb_paths.append(enriched_output)
        else:
            enriched_kb_paths.append(
                os.path.join(output_dir, enriched_output))
    else:
        # No pipeline_type and no override: default to the source-pipeline
        # filename so we never produce the legacy/backward-compat file.
        enriched_kb_paths.append(
            os.path.join(output_dir, 'all_tcs_extracted_enriched_source.json'))

    enriched_kb_path = enriched_kb_paths[0]  # primary path used in logs
    for p in enriched_kb_paths:
        os.makedirs(os.path.dirname(p) or '.', exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(enriched_tests, f, indent=2, ensure_ascii=False)

    # Save flow registry
    registry_output = {
        'flows': flows,
        'total_flows': len(flows),
        'total_tests_tagged': len(enriched_tests),
        'source': '3-signal scoring algorithm'
    }

    # Output filename can be overridden so the dependency pipeline can write
    # 'flow_registry_dependency.json' without trampling the source pipeline's
    # canonical 'flow_registry.json'.
    output_filename_final = output_filename or 'flow_registry.json'
    output_path = os.path.join(output_dir, output_filename_final)
    os.makedirs(output_dir, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(registry_output, f, indent=2, ensure_ascii=False)

    print(f"\n{'=' * 80}")
    print(f"STAGE 0a COMPLETE")
    print(f"{'=' * 80}")
    print(f"Flows discovered: {len(flows)}")
    print(f"Tests tagged: {len(enriched_tests)}")
    print(f"Tests untagged (scored below threshold): {untagged_count}")

    # FAIL FAST if too many tests are untagged (data quality issue)
    untagged_pct = (untagged_count / len(tests) * 100) if tests else 0
    # FOCUSED mode: most tests legitimately don't match the small set of
    # focused entry points, so the >5% gate does not apply. Only enforce
    # the gate in FULL mode, where every test should map to SOMETHING.
    is_focused_mode = bool((changed_file and changed_method) or changed_files)
    if is_focused_mode:
        # FOCUSED mode (single-method or multi-file): skip quality gate (expected behavior)
        print(f"\n[INFO] {untagged_pct:.1f}% of tests scored below threshold (FOCUSED mode - expected)")
        print(f"[INFO] In FOCUSED mode, most tests correctly do not match the narrow set of affected entry points.")
        print(f"[INFO] Skipping >5% quality gate (only enforced in FULL mode).")
    elif untagged_pct > 5.0:  # FULL mode: more than 5% untagged = problem
        print(f"\n[ERROR] {untagged_pct:.1f}% of tests scored below threshold!")
        print(f"[ERROR] This indicates:")
        print(f"[ERROR]   1. Test descriptions are too generic")
        print(f"[ERROR]   2. synonym_groups.json is incomplete")
        print(f"[ERROR]   3. Scoring thresholds are too strict")
        print(f"[ERROR] Action: Review untagged tests above or enrich synonym_groups.json")
        raise RuntimeError(
            f"{untagged_count} tests ({untagged_pct:.1f}%) failed to tag. "
            f"Threshold: {adaptive_min_score} pts. Fix data quality issues before proceeding."
        )
    print(f"Tests tagged: {len(enriched_tests)}")
    print(f"Registry saved: {output_path}")
    for p in enriched_kb_paths:
        print(f"Enriched tests (KB dir):    {p}")

    return registry_output


def main():
    parser = argparse.ArgumentParser(description="Build Flow Registry (Stage 0a)")
    parser.add_argument("--test-corpus", default=TC_DATA_PATH, help="Test corpus path")
    parser.add_argument("--repo-root", default=REPO_ROOT, help="Repository root")
    parser.add_argument("--synonym-groups", default=os.path.join(RIA_OUTPUT_DIR, "knowledge_base", "synonym_groups.json"),
                        help="Synonym groups path")
    parser.add_argument("--output-dir", default=os.path.join(RIA_OUTPUT_DIR, "knowledge_base"),
                        help="Output directory")
    parser.add_argument("--changed-file", default=None, help="Changed file path (for focused mode)")
    parser.add_argument("--changed-method", default=None, help="Changed method name (for focused mode)")
    parser.add_argument("--changed-files", default=None,
                        help="Comma-separated list of changed file paths "
                             "(FOCUSED-multi-file mode for dependency pipeline; "
                             "scans each file for entry points without a specific method)")
    parser.add_argument("--output-filename", default=None,
                        help="Override output filename (default: flow_registry.json). "
                             "Use 'flow_registry_dependency.json' for dependency pipeline.")
    parser.add_argument("--enriched-output", default=None,
                        help="Override enriched corpus filename (relative to "
                             "--output-dir, or an absolute path). When omitted "
                             "and --pipeline-type is not supplied, the "
                             "source-pipeline filename "
                             "('all_tcs_extracted_enriched_source.json') is "
                             "used. Ignored when --pipeline-type is supplied.")
    parser.add_argument("--pipeline-type", default=None,
                        choices=['source', 'dependency'],
                        help="Pipeline that is invoking this build. "
                             "'source'     -> writes ONLY "
                             "all_tcs_extracted_enriched_source.json. "
                             "'dependency' -> writes ONLY "
                             "all_tcs_extracted_enriched_dependency.json (the "
                             "source-pipeline file is preserved). When "
                             "omitted, defaults to the source-pipeline "
                             "filename — the legacy backward-compat file is "
                             "no longer produced.")
    parser.add_argument("--kb-input-dir", default=None,
                        help="Optional directory to read KB INPUT artifacts "
                             "from (e.g. discovered_generic_nouns.json). "
                             "When omitted, falls back to --output-dir. "
                             "Used by multi-method workers to write outputs "
                             "to per-worker scratch dirs while still reading "
                             "shared one-time KB inputs from the canonical "
                             "KB directory.")

    args = parser.parse_args()

    # Parse comma-separated changed_files list
    changed_files_list = None
    if args.changed_files:
        changed_files_list = [
            f.strip() for f in args.changed_files.split(',') if f.strip()
        ]

    try:
        build_flow_registry(args.test_corpus, args.repo_root, args.synonym_groups, args.output_dir,
                          changed_file=args.changed_file, changed_method=args.changed_method,
                          changed_files=changed_files_list,
                          output_filename=args.output_filename,
                          enriched_output=args.enriched_output,
                          pipeline_type=args.pipeline_type,
                          kb_input_dir=args.kb_input_dir)
        sys.exit(0)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
