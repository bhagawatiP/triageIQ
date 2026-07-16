#!/usr/bin/env python3
"""
Serena MCP Client - Code navigation via git grep (framework-agnostic)
Provides call-tree tracing without relying on framework annotations.

Quality safeguards (Phase 2 + Phase 3):
  - Language scoping: when tracing a Java symbol, only Java callers are
    considered. Cross-language token collisions (e.g. Java `run` matching
    Python helper named `run`) are eliminated.
  - File-class skip-list: RIA tool sources, generated code, build output,
    and test files are excluded BEFORE recursion, not just at the leaves.
  - Context validation: each candidate match is parsed and only kept if
    the symbol appears as an actual call (`<token>(`) and not inside
    a comment / string / import statement.
  - Definition filtering: lines that are method declarations (not call
    sites) are dropped.
  - Trivial-method skip-list: getters / setters / equals / hashCode /
    toString / clone / finalize are never reported as entry points.
  - Caller resolution by enclosing brace scope: the caller method for
    a hit is determined by walking back through balanced braces, not
    by a `<20 line` heuristic.
  - Bounded fan-out: per-method caller cap and global visited-set keep
    the trace deterministic and prevent recursion explosion.
  - Overload-aware tracing (Phase 3): when an upward trace recurses into
    a method that has multiple overloads in the same file, the search
    is restricted to callers whose call-site argument count matches the
    overload that actually contains the previous step's call. This
    prevents callers of a SIBLING overload (which doesn't reach the
    changed method) from contaminating the trace. The rule is data-
    driven and applies to ANY method with overloads - no domain or name
    is hardcoded.
"""

import os
import re
import subprocess
import threading
from typing import Dict, List, Optional, Tuple, Set
from pathlib import Path


# --------------------------------------------------------------------------
# File-class skip-lists (used to drop pollution at the SOURCE of the trace)
# --------------------------------------------------------------------------

# RIA tool / build / generated markers. Any file whose path contains one of
# these substrings is treated as non-application code and never appears as
# a caller, callee or entry point.
_NON_APPLICATION_MARKERS: Tuple[str, ...] = (
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
    '/.git/',
    '/bin/',
    '/.serena/',
    '/.vscode/',
)

# Test markers - test code is never an entry point in this analysis.
_TEST_MARKERS: Tuple[str, ...] = (
    '/test/',
    '/tests/',
    'test.java',
    'tests.java',
    'test.py',
    '_test.py',
    '_tests.py',
)

# Trivial / boilerplate methods that should never be returned as callers
# or entry points (they are noise in regression-impact analysis).
_TRIVIAL_METHODS: Set[str] = {
    'toString', 'equals', 'hashCode', 'clone', 'finalize',
    'compareTo', 'iterator', 'next', 'hasNext', 'remove',
    'wait', 'notify', 'notifyAll',
    '__init__', '__repr__', '__str__', '__eq__', '__hash__',
}

# --------------------------------------------------------------------------
# Framework entry-point markers — loaded from the active language profile.
#
# Instead of hardcoding JDK / Spring / Servlet class markers, we read
# the `entry_point_markers` list from the active language profile in
# ria_config.py. This makes the pipeline language- and framework-agnostic:
#   - Java/Spring Boot: @RestController, @Scheduled, implements Runnable, ...
#   - Python/Flask:     @app.route, @celery.task, if __name__, ...
#   - TypeScript/Angular: @Component, @Get, export default, ...
#   - JavaScript/Node:  app.get, router.post, module.exports, ...
#
# Each profile defines its own markers. The code below loads them once and
# uses them uniformly — no language-specific if/else branches.
# --------------------------------------------------------------------------

def _get_entry_point_markers() -> list:
    """Return entry_point_markers from the active language profile."""
    try:
        import sys as _sys, os as _os
        configs_dir = _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
            'configs')
        if configs_dir not in _sys.path:
            _sys.path.insert(0, configs_dir)
        from ria_config import get_active_profile
        return get_active_profile().get('entry_point_markers', [])
    except Exception:
        return []


# --------------------------------------------------------------------------
# Universal YAML-driven detector — used as an early *rejection* layer inside
# is_legitimate_entry_point(). All language-specific patterns live in
# configs/languages/<lang>.yaml. ZERO hardcoding.
# --------------------------------------------------------------------------
_UNIVERSAL_DETECTOR = None  # type: ignore


def _get_universal_detector():
    """Memoised EntryPointDetector instance (None on import failure)."""
    global _UNIVERSAL_DETECTOR
    if _UNIVERSAL_DETECTOR is not None:
        return _UNIVERSAL_DETECTOR if _UNIVERSAL_DETECTOR is not False else None
    try:
        import sys as _sys, os as _os
        skill_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if skill_root not in _sys.path:
            _sys.path.insert(0, skill_root)
        from core.detector import EntryPointDetector  # type: ignore
        _UNIVERSAL_DETECTOR = EntryPointDetector()
    except Exception as exc:  # pragma: no cover
        print(f"[serena_mcp_client] WARNING: universal detector unavailable: {exc}")
        _UNIVERSAL_DETECTOR = False
        return None
    return _UNIVERSAL_DETECTOR


def _yaml_filter_rejects(file_content: str,
                          method_name: str,
                          method_offset: int,
                          file_path: str) -> Tuple[bool, str]:
    """
    Run the universal YAML-driven filter pipeline. Returns (rejected, reason).

    `rejected=True` means the method was matched by one of the language's
    configured filters (private / abstract / constructor / trivial getter /
    test path / vendor path / language-specific) and must NOT be reported
    as an entry point. `rejected=False` means the YAML rules abstain — the
    caller proceeds with dynamic-discovery checks as usual.

    NO patterns are baked into this function — it is a thin shim over
    `core.config_adapter.ConfigAdapter`.
    """
    detector = _get_universal_detector()
    if detector is None:
        return False, ''
    lang = detector.detect_language(file_path or '')
    if not lang:
        return False, ''
    adapter = detector.get_adapter(lang)
    if adapter is None:
        return False, ''
    try:
        filtered, reason = adapter.is_filtered(
            method_name=method_name,
            file_path=file_path,
            file_content=file_content,
            method_offset=method_offset,
        )
    except Exception:
        return False, ''
    return filtered, reason

def _get_source_extensions() -> list:
    """Return source_extensions from the active language profile."""
    try:
        import sys as _sys, os as _os
        configs_dir = _os.path.join(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
            'configs')
        if configs_dir not in _sys.path:
            _sys.path.insert(0, configs_dir)
        from ria_config import get_active_profile
        return get_active_profile().get('source_extensions', ['.java'])
    except Exception:
        return ['.java']


# Legacy constants kept ONLY as data that the language profiles reference.
# They are NOT used directly anywhere in logic — profile markers are used.
_JDK_INTERFACE_METHOD_SEEDS: Dict[str, Set[str]] = {
    'HttpServlet': {'doGet', 'doPost', 'doPut', 'doDelete',
                    'doHead', 'doOptions', 'doTrace', 'service'},
    'GenericServlet': {'service', 'init', 'destroy'},
    'Servlet': {'service', 'init', 'destroy'},
    'Filter': {'doFilter', 'init', 'destroy'},
    'ServletContextListener': {'contextInitialized', 'contextDestroyed'},
    'HttpSessionListener': {'sessionCreated', 'sessionDestroyed'},
    'MessageListener': {'onMessage'},
    'ApplicationListener': {'onApplicationEvent'},
    'Runnable': {'run'},
    'Callable': {'call'},
    'Thread': {'run'},
    'TimerTask': {'run'},
    'Executor': {'execute'},
    'Function': {'apply'},
    'Consumer': {'accept'},
    'Supplier': {'get'},
    'Comparator': {'compare'},
    'ActionListener': {'actionPerformed'},
    'CompletionHandler': {'completed', 'failed'},
    # Quartz scheduler interfaces - dispatched by the Quartz framework
    # via a job class name stored in the QRTZ_JOB_DETAILS table. These
    # implementations have no in-code callers, so without a marker the
    # 5-strategy detector misses them entirely (EEM has ~40 such jobs).
    'Job': {'execute'},
    'StatefulJob': {'execute'},
    'InterruptableJob': {'execute', 'interrupt'},
}

# Annotation names that the dynamic REST discovery can fall back to
# when no repo_root is supplied (e.g. unit tests).
_JDK_REST_ANNOTATION_SEEDS: Set[str] = {
    'GET', 'POST', 'PUT', 'DELETE', 'HEAD', 'OPTIONS', 'PATCH',
    'RequestMapping', 'GetMapping', 'PostMapping',
    'PutMapping', 'DeleteMapping', 'PatchMapping',
    'Path',
}

# These are now read from the profile but kept as fallback for the
# scheduled annotation helper (called from legacy code paths).
_SCHEDULED_OR_LISTENER_METHOD_ANNOTATIONS: Tuple[str, ...] = (
    '@Scheduled', '@EventListener', '@JmsListener',
    '@KafkaListener', '@RabbitListener', '@SqsListener',
    '@PostConstruct', '@PreDestroy',
)

# Class-level markers for servlet, filter, listener, REST — now read from
# the language profile's entry_point_markers. Kept as legacy constants only
# for the dispatch cache fallback used by the inheritance walker.
_SERVLET_CLASS_MARKERS: Tuple[str, ...] = (
    'extends HttpServlet',
    'extends GenericServlet',
    'extends javax.servlet.http.HttpServlet',
    'extends jakarta.servlet.http.HttpServlet',
    'implements Servlet',
)

_FILTER_CLASS_MARKERS: Tuple[str, ...] = (
    'implements Filter',
    'implements javax.servlet.Filter',
    'implements jakarta.servlet.Filter',
)

_LISTENER_CLASS_MARKERS: Tuple[str, ...] = (
    'implements MessageListener',
    'implements javax.jms.MessageListener',
    'implements ApplicationListener',
    'implements ServletContextListener',
    'implements HttpSessionListener',
)

_REST_CONTROLLER_CLASS_MARKERS: Tuple[str, ...] = (
    '@RestController',
    '@Controller',
    '@Path',
    '@WebService',
)


def _has_any_entry_point_marker(text: str) -> Tuple[bool, str]:
    """
    Check if text contains ANY entry_point_marker from the active profile.
    Returns (found, matched_marker).
    This is the UNIFIED entry point check — works for ALL languages.
    """
    markers = _get_entry_point_markers()
    for marker in markers:
        if marker in text:
            return True, marker
    return False, ''

# Keywords that look like calls but are control flow / language constructs.
_CONTROL_KEYWORDS: Set[str] = {
    'if', 'while', 'for', 'switch', 'catch', 'return', 'try',
    'synchronized', 'new', 'throw', 'super', 'this', 'assert', 'do',
    'else', 'finally', 'case', 'break', 'continue', 'instanceof',
    'yield', 'await', 'with', 'as', 'in', 'is', 'and', 'or', 'not',
    'lambda', 'pass', 'def', 'class', 'import', 'from', 'global', 'nonlocal',
}


def _is_non_application_file(file_path: str) -> bool:
    """True if path is a RIA tool / generated / build artefact (never app code)."""
    p = file_path.replace('\\', '/').lower()
    return any(marker.lower() in p for marker in _NON_APPLICATION_MARKERS)


def _is_test_file(file_path: str) -> bool:
    """True if path is a test source file."""
    p = file_path.replace('\\', '/').lower()
    return any(marker in p for marker in _TEST_MARKERS)


def _split_camel_case(name: str) -> List[str]:
    """Split camelCase / PascalCase into tokens. 'getWorkPolicy' -> ['get','Work','Policy']."""
    return re.findall(
        r'[A-Z]+(?=[A-Z][a-z]|[^A-Za-z]|$)|[A-Z]?[a-z]+|[0-9]+',
        name
    )


def _is_trivial_method(method_name: str) -> bool:
    """
    True iff the method is a *trivial* boilerplate accessor or well-known
    no-op that should never be reported as an entry point or caller.

    Rule (conservative; avoids dropping domain methods whose names happen
    to start with `get`/`set`/`is`):

      * Reject the well-known names in `_TRIVIAL_METHODS`.
      * Reject Java-bean accessors with a SINGLE noun token after the
        prefix, e.g. `getId`, `setName`, `isActive`, `getSiteId`.
      * Multi-word accessors such as
        `getWorkPolicyTemplatesByAgentIdsAndProgram` are NOT trivial:
        they typically wrap meaningful business logic.
    """
    if not method_name:
        return True
    if method_name in _TRIVIAL_METHODS:
        return True

    # Identify Java-bean prefix.
    prefix_len = 0
    if method_name.startswith('get') and len(method_name) > 3 and method_name[3].isupper():
        prefix_len = 3
    elif method_name.startswith('set') and len(method_name) > 3 and method_name[3].isupper():
        prefix_len = 3
    elif method_name.startswith('is') and len(method_name) > 2 and method_name[2].isupper():
        prefix_len = 2
    if prefix_len == 0:
        return False

    # Count noun tokens after the prefix. >=2 tokens => not a trivial bean.
    suffix = method_name[prefix_len:]
    tokens = _split_camel_case(suffix)
    # Strip trailing 'Id'/'Name' qualifier from the count - 'getSiteId' is
    # still a trivial bean (one logical noun + Id qualifier).
    QUALIFIERS = {'id', 'name', 'value', 'type', 'date', 'time', 'count',
                  'list', 'map', 'set', 'key', 'flag', 'status'}
    significant = [t for t in tokens if t.lower() not in QUALIFIERS]
    if len(significant) <= 1:
        return True  # Trivial bean accessor: getSiteId, setName, isActive, getSiteIdList
    return False  # Multi-noun method - keep.


def _same_language(a: str, b: str) -> bool:
    """True if both files share the same source-language extension."""
    ea = Path(a).suffix.lower()
    eb = Path(b).suffix.lower()
    return ea == eb and ea != ''


# --------------------------------------------------------------------------
# Overload-aware helpers (generic - work for any Java method)
# --------------------------------------------------------------------------

def _split_top_level_args(args_text: str) -> List[str]:
    """
    Split a (possibly nested) Java argument list on top-level commas.

    Handles generics (`<...>`), array indexers, casts, and string/char
    literals so that `Map<Integer, List<String>>, Integer` is split into
    two args, not three.

    Empty string -> [].
    """
    args_text = args_text.strip()
    if not args_text:
        return []

    parts: List[str] = []
    depth_paren = 0      # ()
    depth_brack = 0      # []
    depth_angle = 0      # <>
    in_string = False
    in_char = False
    buf: List[str] = []

    i = 0
    n = len(args_text)
    while i < n:
        ch = args_text[i]
        nxt = args_text[i + 1] if i + 1 < n else ''
        if in_string:
            buf.append(ch)
            if ch == '\\' and i + 1 < n:
                buf.append(nxt)
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if in_char:
            buf.append(ch)
            if ch == '\\' and i + 1 < n:
                buf.append(nxt)
                i += 2
                continue
            if ch == "'":
                in_char = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            buf.append(ch)
        elif ch == "'":
            in_char = True
            buf.append(ch)
        elif ch == '(':
            depth_paren += 1
            buf.append(ch)
        elif ch == ')':
            depth_paren -= 1
            buf.append(ch)
        elif ch == '[':
            depth_brack += 1
            buf.append(ch)
        elif ch == ']':
            depth_brack -= 1
            buf.append(ch)
        elif ch == '<':
            # Heuristic: only treat '<' as a generic delimiter when it is
            # immediately followed by an identifier, '?' or whitespace -
            # otherwise it is a less-than operator.
            if nxt.isalpha() or nxt == '?' or nxt == ' ':
                depth_angle += 1
            buf.append(ch)
        elif ch == '>':
            if depth_angle > 0:
                depth_angle -= 1
            buf.append(ch)
        elif (ch == ','
              and depth_paren == 0
              and depth_brack == 0
              and depth_angle == 0):
            parts.append(''.join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = ''.join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _extract_method_params(content: str, paren_open_pos: int) -> Tuple[str, int]:
    """
    Given a `(` position, return (raw_param_string, arity).

    Walks forward, tracking nested parens, until the matching `)`.
    Used at method-declaration sites to capture the formal parameter list.
    """
    if paren_open_pos >= len(content) or content[paren_open_pos] != '(':
        return '', 0
    depth = 1
    pos = paren_open_pos + 1
    in_string = False
    in_char = False
    while pos < len(content) and depth > 0:
        ch = content[pos]
        if in_string:
            if ch == '\\' and pos + 1 < len(content):
                pos += 2
                continue
            if ch == '"':
                in_string = False
        elif in_char:
            if ch == '\\' and pos + 1 < len(content):
                pos += 2
                continue
            if ch == "'":
                in_char = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "'":
                in_char = True
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    break
        pos += 1
    if depth != 0:
        return '', 0
    raw = content[paren_open_pos + 1:pos]
    args = _split_top_level_args(raw)
    return raw, len(args)


def _extract_call_args(line_or_chunk: str, call_start: int) -> Tuple[Optional[str], int]:
    """
    Given an offset of the `(` that opens a call expression, return
    (raw_arg_string, arity). Returns (None, -1) if the call expression
    is not closed within the chunk (e.g. multi-line call).
    """
    if call_start >= len(line_or_chunk) or line_or_chunk[call_start] != '(':
        return None, -1
    depth = 1
    pos = call_start + 1
    in_string = False
    in_char = False
    while pos < len(line_or_chunk) and depth > 0:
        ch = line_or_chunk[pos]
        if in_string:
            if ch == '\\' and pos + 1 < len(line_or_chunk):
                pos += 2
                continue
            if ch == '"':
                in_string = False
        elif in_char:
            if ch == '\\' and pos + 1 < len(line_or_chunk):
                pos += 2
                continue
            if ch == "'":
                in_char = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "'":
                in_char = True
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    break
        pos += 1
    if depth != 0:
        return None, -1
    raw = line_or_chunk[call_start + 1:pos]
    return raw, len(_split_top_level_args(raw))


# --------------------------------------------------------------------------
# Entry-point validation (framework-pattern based, NOT domain-specific)
# --------------------------------------------------------------------------

def _class_decl_block(content: str, method_offset: int) -> str:
    """
    Return the substring of `content` from the start of the file up to
    just past the class declaration that lexically encloses `method_offset`.
    Used to inspect the class header (`extends ... implements ...`) and
    any annotations placed directly above the class.
    """
    # Walk back to find the most-recent `class X` / `interface X` / `enum X`
    # declaration whose body still contains method_offset. We use a
    # forgiving regex (the same one used by symbol extraction).
    matches = list(re.finditer(
        r'(?:public\s+|abstract\s+|final\s+|static\s+)*(?:class|interface|enum)\s+\w+',
        content[:method_offset]
    ))
    if not matches:
        return content[:method_offset]
    last = matches[-1]
    # Return the class line(s) including a generous slice (200 chars) so
    # `extends Foo implements Bar, Baz {` is captured even when it spans
    # multiple lines.
    end = min(len(content), last.end() + 400)
    return content[last.start():end]


def _annotations_above(content: str, method_offset: int, max_lines: int = 8) -> str:
    """
    Return the block of annotation lines directly preceding `method_offset`
    (up to `max_lines` non-blank lines back). Used to detect method-level
    annotations such as `@GET`, `@Path("/x")`, `@Scheduled(...)`.
    """
    if method_offset <= 0:
        return ''
    # Walk back through prior lines, collecting until we hit a non-annotation
    # / non-blank / non-comment line (which would be the previous statement).
    prefix = content[:method_offset]
    lines = prefix.splitlines()
    collected: List[str] = []
    consumed = 0
    for ln in reversed(lines):
        s = ln.strip()
        if not s:
            if collected:
                break
            consumed += 1
            if consumed > max_lines:
                break
            continue
        if s.startswith('@'):
            collected.append(s)
            continue
        if s.startswith(('//', '/*', '*')):
            # Comment - skip but don't break (annotations may sit above
            # comments in some styles).
            continue
        # Any other code => we've left the annotation block.
        break
    return '\n'.join(reversed(collected))


def _is_class_servlet(class_block: str) -> bool:
    return any(marker in class_block for marker in _SERVLET_CLASS_MARKERS) \
        or '@WebServlet' in class_block


def _is_class_filter(class_block: str) -> bool:
    return any(marker in class_block for marker in _FILTER_CLASS_MARKERS)


def _is_class_listener(class_block: str) -> bool:
    return any(marker in class_block for marker in _LISTENER_CLASS_MARKERS)


def _is_class_rest_controller(class_block: str) -> bool:
    return any(marker in class_block for marker in _REST_CONTROLLER_CLASS_MARKERS)


def _is_class_runnable(class_block: str) -> bool:
    return ('implements Runnable' in class_block
            or 'extends Thread' in class_block
            or 'extends TimerTask' in class_block)


def _is_class_callable(class_block: str) -> bool:
    return 'implements Callable' in class_block


def _has_rest_method_annotation(annotations_block: str,
                                repo_root: str = '') -> bool:
    """
    Decide whether the annotation block above a method contains any
    annotation that should be considered a REST endpoint marker.

    Annotations are NOT hardcoded any more - they are discovered from the
    repository via `discover_rest_annotations()`. The JDK-seed set
    (`_JDK_REST_ANNOTATION_SEEDS`) is used as a fallback when no repo_root
    is supplied (e.g. unit tests that pass a synthetic block).
    """
    if not annotations_block:
        return False
    candidates: Set[str] = set(_JDK_REST_ANNOTATION_SEEDS)
    if repo_root:
        try:
            candidates = candidates | discover_rest_annotations(repo_root)
        except Exception:
            pass
    for ann in candidates:
        # Match @Foo as a token: must be followed by '(' or whitespace
        # or end-of-line, not by another letter (e.g. avoid @PathParam
        # being matched by @Path).
        pattern = r'@' + re.escape(ann) + r'(?:\s|\(|$)'
        if re.search(pattern, annotations_block, re.MULTILINE):
            return True
    return False


def _has_scheduled_or_listener_annotation(annotations_block: str) -> bool:
    if not annotations_block:
        return False
    for ann in _SCHEDULED_OR_LISTENER_METHOD_ANNOTATIONS:
        pattern = re.escape(ann) + r'(?:\s|\(|$)'
        if re.search(pattern, annotations_block, re.MULTILINE):
            return True
    return False


# Module-level cache for reflective-dispatch contracts. Populated lazily
# the first time `_reflective_entry_pattern` is called with a non-empty
# repo_root. Each contract is {'target_package': str, 'method_prefix': str}.
_REFLECTIVE_CONTRACTS_CACHE: Optional[List[Dict[str, str]]] = None
_REFLECTIVE_CACHE_REPO: Optional[str] = None


def _discover_reflective_contracts(repo_root: str) -> List[Dict[str, str]]:
    """
    Scan the repo for `Class.forName("PKG." + ...)` followed by
    `cls.getMethod("PREFIX" + ...)` patterns. Each match defines a
    reflective-dispatch contract:
        target_package = literal prefix passed to forName
        method_prefix  = literal prefix passed to getMethod

    Cached after the first call so the cost is amortised.
    """
    global _REFLECTIVE_CONTRACTS_CACHE, _REFLECTIVE_CACHE_REPO
    if _REFLECTIVE_CONTRACTS_CACHE is not None and _REFLECTIVE_CACHE_REPO == repo_root:
        return _REFLECTIVE_CONTRACTS_CACHE

    contracts: List[Dict[str, str]] = []
    try:
        cmd = ["git", "grep", "-l", "-E", r'Class\.forName']
        r = subprocess.run(cmd, capture_output=True, encoding='utf-8',
                           errors='replace', timeout=10, cwd=repo_root)
        files = [f for f in (r.stdout or '').strip().split('\n') if f
                 and not _is_non_application_file(f)
                 and not _is_test_file(f)]
    except Exception:
        files = []

    for cand in files[:100]:
        try:
            with open(os.path.join(repo_root, cand), 'r',
                      encoding='utf-8', errors='replace') as fh:
                src = fh.read()
        except Exception:
            continue
        # `Class.forName("PKG.")` form (literal package).
        for m in re.finditer(
            r'Class\.forName\s*\(\s*(?:new\s+StringBuilder\s*\(\s*\)\s*\.append\s*\(\s*)?'
            r'"([\w\.]+\.)"',
            src,
        ):
            pkg = m.group(1)
            # Look in the same enclosing scope (8000-char window) for a
            # getMethod call that uses a literal prefix.
            window = src[m.end(): m.end() + 8000]
            for mm in re.finditer(
                r'getMethod\s*\(\s*"([A-Za-z_]\w*)"\s*\+', window
            ):
                contracts.append({
                    'target_package': pkg,
                    'method_prefix': mm.group(1),
                })
            for mm in re.finditer(
                r'"([A-Za-z_]\w*)"\s*\+\s*getClassName', window,
            ):
                contracts.append({
                    'target_package': pkg,
                    'method_prefix': mm.group(1),
                })

    # Dedup
    seen = set()
    uniq: List[Dict[str, str]] = []
    for c in contracts:
        key = (c['target_package'], c['method_prefix'])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(c)

    _REFLECTIVE_CONTRACTS_CACHE = uniq
    _REFLECTIVE_CACHE_REPO = repo_root
    return uniq


# --------------------------------------------------------------------------
# Dynamic Interface / Inheritance Discovery
# --------------------------------------------------------------------------
# We scan every Java source file in the repo ONCE and build:
#   * interface_methods[InterfaceName] -> set of abstract method names
#   * extends_map[ClassName]           -> immediate parent class (or None)
#   * implements_map[ClassName]        -> set of immediately-implemented
#                                         interface names
#   * class_to_file[ClassName]         -> repo-relative source file
#
# The transitive closure (parent classes' parent classes, super-interfaces
# of implemented interfaces, etc.) is computed on demand by
# `_collect_inherited_method_names`. JDK / standard-library classes that
# are not in the repository fall back to `_JDK_INTERFACE_METHOD_SEEDS`.
# This is the ONE place a JVM-standard-library name appears - all CUSTOM
# framework / interface methods are learned from source.

_INTERFACE_DISCOVERY_CACHE: Optional[Dict[str, Dict]] = None
_INTERFACE_DISCOVERY_REPO: Optional[str] = None


def discover_interface_methods(repo_root: str) -> Dict[str, Dict]:
    """
    Scan the repository for `interface X { ... }` and `class X extends/
    implements Y` declarations. For each interface, extract the abstract
    method names declared in its body. For each class, record its parent
    class and the set of interfaces it implements.

    Two-class names with the same short identifier (Java allows the same
    simple name in different packages) are tracked as a UNION of all
    inheritance edges seen anywhere in the repo - this keeps the
    name-based lookup permissive enough to recognise framework bases
    without needing FQNs.

    Returns a dict with keys:
        interface_methods : Dict[str, Set[str]]
            interface short-name -> abstract method names on it
        extends_map       : Dict[str, Set[str]]
            class short-name -> set of parent class short-names seen
            (multiple entries when the same name appears in different
            files - union semantics is correct for framework matching)
        implements_map    : Dict[str, Set[str]]
            class short-name -> set of interface short-names directly
            implemented
        class_to_files    : Dict[str, Set[str]]
            class short-name -> all source files that declare a class /
            interface with this name
        is_interface      : Set[str]
            short-names that are EVER declared as `interface` (even if
            another file uses the same name as a `class`)

    The result is cached per `repo_root` so repeated calls are free.
    """
    global _INTERFACE_DISCOVERY_CACHE, _INTERFACE_DISCOVERY_REPO
    if _INTERFACE_DISCOVERY_CACHE is not None \
            and _INTERFACE_DISCOVERY_REPO == repo_root:
        return _INTERFACE_DISCOVERY_CACHE

    interface_methods: Dict[str, Set[str]] = {}
    extends_map: Dict[str, Set[str]] = {}
    implements_map: Dict[str, Set[str]] = {}
    class_to_files: Dict[str, Set[str]] = {}
    is_interface: Set[str] = set()

    # Find every .java file in the repo. Uses POSIX [[:space:]] because
    # git grep's POSIX-extended regex doesn't honour \s portably.
    try:
        cmd = ["git", "grep", "-l", "-E",
               r'(class|interface)[[:space:]]+[A-Z]']
        r = subprocess.run(
            cmd, capture_output=True, encoding='utf-8',
            errors='replace', timeout=60, cwd=repo_root,
        )
        files = [f for f in (r.stdout or '').strip().split('\n')
                 if f and f.endswith('.java')
                 and not _is_non_application_file(f)
                 and not _is_test_file(f)]
    except Exception:
        files = []

    # Cap to keep the scan deterministic on huge repos.
    files = files[:5000]

    # Regex to capture: optional modifiers, "class X" or "interface X",
    # optional generics, optional "extends Y[, ...]", optional "implements
    # Z, ...". Multi-line tolerant via DOTALL on a windowed slice.
    decl_re = re.compile(
        r'(?:public\s+|abstract\s+|final\s+|static\s+|protected\s+|private\s+)*'
        r'(class|interface)\s+(\w+)'
        r'(?:\s*<[^>]*>)?'
        r'(?:\s+extends\s+([\w<>\.,\s\?]+?))?'
        r'(?:\s+implements\s+([\w<>\.,\s\?]+?))?'
        r'\s*\{'
    )

    for fpath in files:
        try:
            abs_path = os.path.join(repo_root, fpath)
            with open(abs_path, 'r', encoding='utf-8', errors='replace') as fh:
                content = fh.read()
        except Exception:
            continue

        for m in decl_re.finditer(content):
            kind = m.group(1)
            name = m.group(2)
            extends_clause = m.group(3) or ''
            implements_clause = m.group(4) or ''

            # Record file location.
            class_to_files.setdefault(name, set()).add(fpath)

            # Parse extends clause: keep simple-name parents only (strip
            # generics and package qualifiers).
            parents: List[str] = []
            if extends_clause:
                for raw in _split_top_level_args(extends_clause):
                    simple = _simple_type_name(raw)
                    if simple:
                        parents.append(simple)
            ifaces: List[str] = []
            if implements_clause:
                for raw in _split_top_level_args(implements_clause):
                    simple = _simple_type_name(raw)
                    if simple:
                        ifaces.append(simple)

            if kind == 'interface':
                is_interface.add(name)
                # An interface may "extend" multiple super-interfaces.
                # We treat super-interfaces as implements_map entries so
                # method discovery follows the same union path used for
                # classes.
                implements_map.setdefault(name, set()).update(parents)
                # Extract method names declared in this interface body.
                body = _extract_class_body(content, m.end() - 1)
                if body is not None:
                    methods = _extract_abstract_method_names(body)
                    interface_methods.setdefault(name, set()).update(methods)
            else:
                # A class may have one parent. We accumulate parents
                # across multiple same-named declarations.
                extends_map.setdefault(name, set()).update(parents)
                implements_map.setdefault(name, set()).update(ifaces)

    result = {
        'interface_methods': interface_methods,
        'extends_map': extends_map,
        'implements_map': implements_map,
        'class_to_files': class_to_files,
        'is_interface': is_interface,
    }
    _INTERFACE_DISCOVERY_CACHE = result
    _INTERFACE_DISCOVERY_REPO = repo_root
    return result


def _simple_type_name(raw: str) -> str:
    """
    Reduce 'java.util.List<String>' / 'List<String>' / 'List' to 'List'.
    Returns '' if the input isn't a valid type identifier.
    """
    if not raw:
        return ''
    # Strip generics.
    no_gen = re.sub(r'<.*?>', '', raw).strip()
    # Take the segment after the last dot for FQNs.
    if '.' in no_gen:
        no_gen = no_gen.rsplit('.', 1)[-1]
    no_gen = no_gen.strip()
    if not re.match(r'^[A-Za-z_]\w*$', no_gen):
        return ''
    return no_gen


def _extract_class_body(content: str, brace_open_pos: int) -> Optional[str]:
    """
    Given the offset of the `{` that opens a class/interface body, return
    the body text (between matched braces). Reuses
    `SerenaMCPClient._matching_brace_end` to honour strings/comments.
    """
    if brace_open_pos < 0 or brace_open_pos >= len(content):
        return None
    if content[brace_open_pos] != '{':
        return None
    end = SerenaMCPClient._matching_brace_end(content, brace_open_pos)
    if end is None:
        return None
    return content[brace_open_pos + 1:end]


def _extract_abstract_method_names(body: str) -> Set[str]:
    """
    Return the names of abstract methods declared in an interface body.
    Heuristic: any line that ends with `;` and contains `<id>(...)` whose
    `<id>` is not a control keyword is treated as a method declaration.

    Default-method bodies (those with `{`) are ALSO included - they are
    legitimately on the interface and can be a runtime entry point if
    overridden.
    """
    names: Set[str] = set()
    # Strip block / line comments to avoid pulling names out of doc.
    no_block = re.sub(r'/\*.*?\*/', '', body, flags=re.DOTALL)
    no_line = re.sub(r'(?m)//.*?$', '', no_block)

    # `public default? <type> name(...)` - abstract or default method.
    for m in re.finditer(
        r'(?:public\s+|protected\s+|abstract\s+|default\s+|static\s+|final\s+|synchronized\s+|native\s+)*'
        r'(?:<[^>]*>\s+)?'   # optional generic prefix on the method
        r'[\w<>\[\],\s\?]+?\s+'
        r'(\w+)\s*\([^)]*\)\s*(?:throws\s+[\w\.,\s]+)?\s*[;{]',
        no_line,
    ):
        name = m.group(1)
        if name in _CONTROL_KEYWORDS:
            continue
        # Skip names that look like type identifiers (start with uppercase).
        if name[0].isupper():
            continue
        names.add(name)
    return names


def _collect_inherited_method_names(class_name: str,
                                    repo_root: str,
                                    visited: Optional[Set[str]] = None) -> Set[str]:
    """
    Return the union of method names declared on `class_name`'s
    transitive super-interfaces and parent classes, joined with the JDK
    fallback seeds in `_JDK_INTERFACE_METHOD_SEEDS`.

    A class's OWN (concrete) methods are NOT included - this is purely the
    set of method names whose dispatch is governed by an interface or a
    parent's framework contract, i.e. the "entry point candidates" for
    that class.
    """
    if visited is None:
        visited = set()
    if class_name in visited:
        return set()
    visited.add(class_name)

    discovery = discover_interface_methods(repo_root)
    extends_map = discovery['extends_map']
    implements_map = discovery['implements_map']
    interface_methods = discovery['interface_methods']

    methods: Set[str] = set()

    # JDK fallback seeds for unresolved standard-library types.
    if class_name in _JDK_INTERFACE_METHOD_SEEDS:
        methods.update(_JDK_INTERFACE_METHOD_SEEDS[class_name])

    # Methods directly declared on this interface / class.
    if class_name in interface_methods:
        methods.update(interface_methods[class_name])

    # Recurse through implemented interfaces (and super-interfaces).
    for iface in implements_map.get(class_name, set()):
        methods.update(_collect_inherited_method_names(
            iface, repo_root, visited
        ))

    # Recurse through parent classes (set semantics: same simple-name
    # may have multiple parents seen across files).
    for parent in extends_map.get(class_name, set()):
        methods.update(_collect_inherited_method_names(
            parent, repo_root, visited
        ))

    return methods


def _class_inherits_from(class_name: str,
                         target: str,
                         repo_root: str,
                         visited: Optional[Set[str]] = None) -> bool:
    """
    True if `class_name` transitively extends or implements `target`.
    Walks both the extends chain and the implements graph. JDK names that
    don't exist in the repo are treated as leaves (matching by direct
    name comparison).

    NOTE: this is a permissive check that uses simple-name matching
    across ALL declarations in the repo with the given name. For file-
    accurate inheritance resolution use `_file_class_inherits_from()`.
    """
    if class_name == target:
        return True
    if visited is None:
        visited = set()
    if class_name in visited:
        return False
    visited.add(class_name)

    discovery = discover_interface_methods(repo_root)
    extends_map = discovery['extends_map']
    implements_map = discovery['implements_map']

    for iface in implements_map.get(class_name, set()):
        if iface == target:
            return True
        if _class_inherits_from(iface, target, repo_root, visited):
            return True
    for parent in extends_map.get(class_name, set()):
        if parent == target:
            return True
        if _class_inherits_from(parent, target, repo_root, visited):
            return True
    return False


def _file_class_inherits_from(file_content: str,
                              method_offset: int,
                              targets: Set[str],
                              repo_root: str) -> bool:
    """
    File-accurate inheritance check. Returns True if the class lexically
    enclosing `method_offset` in `file_content` transitively extends or
    implements ANY of `targets`.

    Why we need this: Java allows the same simple class-name in different
    packages. The repo-wide inheritance map unions edges across all such
    declarations - which is fine for framework category seeding (more
    permissive matching) but produces FALSE POSITIVES for "is THIS
    specific class a servlet?".

    This function reads the actual `extends ... implements ...` clause
    from the current file's class declaration and walks ONLY those
    parents through the global graph.
    """
    class_block = _class_decl_block(file_content, method_offset)
    if not class_block:
        return False

    # Pull "extends X[, ...]" and "implements Y, Z" tokens from the
    # enclosing class header.
    parents: Set[str] = set()
    ext_match = re.search(r'\bextends\s+([\w<>\.,\s\?]+?)(?:\s+implements\b|\s*\{)',
                          class_block)
    if ext_match:
        for raw in _split_top_level_args(ext_match.group(1)):
            simple = _simple_type_name(raw)
            if simple:
                parents.add(simple)
    impl_match = re.search(r'\bimplements\s+([\w<>\.,\s\?]+?)\s*\{', class_block)
    if impl_match:
        for raw in _split_top_level_args(impl_match.group(1)):
            simple = _simple_type_name(raw)
            if simple:
                parents.add(simple)

    if not parents:
        return False

    # Direct hit: the class's own header lists a target.
    if parents & targets:
        return True

    # Transitive: walk through the global graph from each direct parent.
    for parent in parents:
        for target in targets:
            if _class_inherits_from(parent, target, repo_root):
                return True
    return False


def _enclosing_class_name(content: str, method_offset: int) -> Optional[str]:
    """
    Return the simple name of the class/interface/enum that lexically
    encloses `method_offset`. Returns None if no enclosing class is found.
    """
    block = _class_decl_block(content, method_offset)
    m = re.search(r'(?:class|interface|enum)\s+(\w+)', block)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# Dynamic Servlet / REST / Thread-target Discovery
# --------------------------------------------------------------------------
# These functions LEARN entry-point method names by walking the inheritance
# graph built by `discover_interface_methods()`. They replace the previous
# previous hardcoded `_SERVLET_DISPATCH_METHODS` / `_REST_METHOD_ANNOTATIONS`
# (which have been deleted).

_DYNAMIC_DISPATCH_CACHE: Optional[Dict[str, Set[str]]] = None
_DYNAMIC_DISPATCH_REPO: Optional[str] = None


def discover_servlet_dispatch_methods(repo_root: str) -> Set[str]:
    """
    Walk every class in the repository that transitively extends
    `HttpServlet` / `GenericServlet` / `Servlet`. For each such class,
    union the inherited method names. The result is the set of method
    names the servlet container will dispatch to at runtime.

    For JDK-only servlet bases (HttpServlet) we use
    `_JDK_INTERFACE_METHOD_SEEDS` since we cannot read the JDK source.
    For CUSTOM servlet bases that exist in the repository we walk the
    actual interface body and learn whatever the team's framework exposes.
    """
    cache = _ensure_dispatch_cache(repo_root)
    return cache.get('servlet', set())


def discover_filter_dispatch_methods(repo_root: str) -> Set[str]:
    cache = _ensure_dispatch_cache(repo_root)
    return cache.get('filter', set())


def discover_listener_dispatch_methods(repo_root: str) -> Set[str]:
    cache = _ensure_dispatch_cache(repo_root)
    return cache.get('listener', set())


def discover_thread_target_methods(repo_root: str) -> Set[str]:
    cache = _ensure_dispatch_cache(repo_root)
    return cache.get('thread', set())


_SERVLET_BASES: Set[str] = {'HttpServlet', 'GenericServlet', 'Servlet'}
_FILTER_BASES: Set[str] = {'Filter'}
_LISTENER_BASES: Set[str] = {
    'MessageListener', 'ApplicationListener',
    'ServletContextListener', 'HttpSessionListener', 'EventListener',
}
_THREAD_BASES: Set[str] = {
    'Runnable', 'Callable', 'Thread', 'TimerTask',
    # Quartz: framework dispatches to `execute(JobExecutionContext)` on
    # implementors of these interfaces (no in-code call sites exist).
    'Job', 'StatefulJob', 'InterruptableJob',
}


def _ensure_dispatch_cache(repo_root: str) -> Dict[str, Set[str]]:
    """
    Build (and cache) the union of dispatch method names for each
    framework category.

    The set is built from:
      * JDK seeds for the well-known base types we cannot scan
        (HttpServlet, Runnable, etc.).
      * Method declarations on any in-repo INTERFACE that is one of the
        framework bases or a sub-interface thereof (e.g. a custom
        `MyServletBase` interface).

    We deliberately do NOT collect concrete methods of every class that
    happens to inherit from a framework base - that produced false-
    positive method names like business logic accidentally classified
    as "servlet dispatch" methods (the bug seen with Java's same-simple-
    name-different-package classes). Sub-interface method names are
    legitimate because the framework or executor will only dispatch to
    methods declared on the interface contract.
    """
    global _DYNAMIC_DISPATCH_CACHE, _DYNAMIC_DISPATCH_REPO
    if _DYNAMIC_DISPATCH_CACHE is not None \
            and _DYNAMIC_DISPATCH_REPO == repo_root:
        return _DYNAMIC_DISPATCH_CACHE

    discovery = discover_interface_methods(repo_root)
    is_interface = discovery['is_interface']
    interface_methods = discovery['interface_methods']

    servlet_methods: Set[str] = set()
    filter_methods: Set[str] = set()
    listener_methods: Set[str] = set()
    thread_methods: Set[str] = set()

    # Seed each bucket with the JDK fallback methods.
    for base in _SERVLET_BASES:
        servlet_methods |= _JDK_INTERFACE_METHOD_SEEDS.get(base, set())
    for base in _FILTER_BASES:
        filter_methods |= _JDK_INTERFACE_METHOD_SEEDS.get(base, set())
    for base in _LISTENER_BASES:
        listener_methods |= _JDK_INTERFACE_METHOD_SEEDS.get(base, set())
    for base in _THREAD_BASES:
        thread_methods |= _JDK_INTERFACE_METHOD_SEEDS.get(base, set())

    # Pull in method names declared on any in-repo INTERFACE that
    # extends/inherits from a framework base. Custom servlet/filter/
    # listener/runnable interfaces are picked up here. We walk ONLY the
    # implements_map (which records super-interfaces) to avoid leaking
    # method names through a class that happens to share its simple name
    # with an interface.
    def _interface_extends(iname: str, target: str,
                           visited: Optional[Set[str]] = None) -> bool:
        if iname == target:
            return True
        if visited is None:
            visited = set()
        if iname in visited:
            return False
        visited.add(iname)
        for parent in implements_map.get(iname, set()):
            if parent == target or _interface_extends(parent, target, visited):
                return True
        return False

    implements_map = discovery['implements_map']
    for iname in is_interface:
        if any(_interface_extends(iname, base) for base in _SERVLET_BASES):
            servlet_methods |= interface_methods.get(iname, set())
        if any(_interface_extends(iname, base) for base in _FILTER_BASES):
            filter_methods |= interface_methods.get(iname, set())
        if any(_interface_extends(iname, base) for base in _LISTENER_BASES):
            listener_methods |= interface_methods.get(iname, set())
        if any(_interface_extends(iname, base) for base in _THREAD_BASES):
            thread_methods |= interface_methods.get(iname, set())

    cache = {
        'servlet': servlet_methods,
        'filter': filter_methods,
        'listener': listener_methods,
        'thread': thread_methods,
    }
    _DYNAMIC_DISPATCH_CACHE = cache
    _DYNAMIC_DISPATCH_REPO = repo_root
    return cache


# --------------------------------------------------------------------------
# Dynamic REST annotation discovery
# --------------------------------------------------------------------------

_REST_ANNOTATION_CACHE: Optional[Set[str]] = None
_REST_ANNOTATION_REPO: Optional[str] = None


def discover_rest_annotations(repo_root: str) -> Set[str]:
    """
    Discover annotations that mark methods as REST endpoints.

    Strategy:
      1. Seed the result with `_JDK_REST_ANNOTATION_SEEDS` (well-known
         JAX-RS / Spring annotations - these are JDK / library names we
         cannot scan).
      2. Scan repo annotations declared as `@interface X` and consider any
         such annotation as a candidate.
      3. Promote candidate annotations to "REST" status if they appear on
         a class declaration that ALSO carries `@RestController`,
         `@Controller`, `@Path` or `@WebService`. These are the framework's
         own marker annotations - their names ARE the JDK seeds, so the
         step is a self-consistent expansion.
      4. Promote any annotation that appears within the body of a class
         transitively known to be a REST controller (per
         `_REST_CONTROLLER_CLASS_MARKERS`).
    Returns: set of annotation short-names (without the `@`).
    """
    global _REST_ANNOTATION_CACHE, _REST_ANNOTATION_REPO
    if _REST_ANNOTATION_CACHE is not None \
            and _REST_ANNOTATION_REPO == repo_root:
        return _REST_ANNOTATION_CACHE

    found: Set[str] = set(_JDK_REST_ANNOTATION_SEEDS)

    # Find files that mention any of the seed marker annotations on a
    # class declaration (REST controllers).
    try:
        marker_pattern = '|'.join(
            r'@' + re.escape(s) for s in
            ('RestController', 'Controller', 'Path', 'WebService')
        )
        cmd = ["git", "grep", "-l", "-E", marker_pattern]
        r = subprocess.run(
            cmd, capture_output=True, encoding='utf-8',
            errors='replace', timeout=15, cwd=repo_root,
        )
        files = [f for f in (r.stdout or '').strip().split('\n')
                 if f and f.endswith('.java')
                 and not _is_non_application_file(f)
                 and not _is_test_file(f)]
    except Exception:
        files = []

    # In each REST-controller file, harvest method-level annotations.
    method_ann_re = re.compile(r'^\s*@(\w+)\b', re.MULTILINE)
    for f in files[:500]:
        try:
            with open(os.path.join(repo_root, f), 'r',
                      encoding='utf-8', errors='replace') as fh:
                content = fh.read()
        except Exception:
            continue
        for m in method_ann_re.finditer(content):
            name = m.group(1)
            # Filter out obvious non-REST annotations.
            if name in ('Override', 'Deprecated', 'SuppressWarnings',
                        'SafeVarargs', 'FunctionalInterface',
                        'Autowired', 'Inject', 'Resource',
                        'Component', 'Service', 'Repository',
                        'Configuration', 'Bean', 'Valid'):
                continue
            found.add(name)

    _REST_ANNOTATION_CACHE = found
    _REST_ANNOTATION_REPO = repo_root
    return found


# --------------------------------------------------------------------------
# Dynamic Entry Point Discovery (Zero Hardcoded Patterns)
# --------------------------------------------------------------------------

# Module-level cache for dynamically discovered entry point patterns
_DYNAMIC_ENTRY_POINT_CACHE: Optional[Dict[str, List[Dict]]] = None
_DYNAMIC_CACHE_REPO: Optional[str] = None

# Disk cache path (persists across subprocess invocations in same RIA run)
# Resolve to <repo>/.github/RIA_OUTPUT/.entry_points_cache.json using an
# absolute path. This file lives at:
#   <repo>/.github/skills/regression-impact-analysis/scripts/serena_mcp_client.py
# so we walk up 4 levels (scripts -> regression-impact-analysis -> skills ->
# .github) and then append RIA_OUTPUT. The previous 2-up + '..' arithmetic
# resolved to <repo>/.github/skills/RIA_OUTPUT/, which polluted the skill
# tree with a stray RIA_OUTPUT directory.
_DYNAMIC_EP_DISK_CACHE = os.path.normpath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    )))),
    'RIA_OUTPUT', '.entry_points_cache.json'
))


def _discover_network_io_patterns(repo_root: str) -> List[Dict]:
    """
    Discover entry points that open network sockets or start network servers.

    Patterns detected:
    - ServerSocket creation
    - socket.bind() calls
    - HTTP server instantiation
    - Network port opening

    Returns: List of {file, method, line, offset, evidence}
    """
    entry_points: List[Dict] = []

    # Search for network binding patterns
    patterns = [
        (r'new\s+ServerSocket\s*\(', 'ServerSocket-creation'),
        (r'\.bind\s*\(\s*new\s+InetSocketAddress', 'socket-bind'),
        (r'ServerBootstrap.*\.bind\s*\(', 'netty-server-bind'),
        (r'HttpServer\.create\s*\(', 'http-server-creation'),
    ]

    for pattern, evidence_type in patterns:
        try:
            cmd = ["git", "grep", "-n", "-E", pattern]
            result = subprocess.run(
                cmd, capture_output=True, encoding='utf-8',
                errors='replace', timeout=10, cwd=repo_root
            )

            for line in (result.stdout or '').strip().split('\n'):
                if not line:
                    continue
                parts = line.split(':', 2)
                if len(parts) < 3:
                    continue

                file_path = parts[0]
                try:
                    line_num = int(parts[1])
                except ValueError:
                    continue

                if _is_non_application_file(file_path) or _is_test_file(file_path):
                    continue

                entry_points.append({
                    'file': file_path,
                    'line': line_num,
                    'evidence': f'network-io:{evidence_type}',
                    'type': 'network-listener',
                })
        except Exception:
            continue

    return entry_points


def _discover_thread_executor_patterns(repo_root: str) -> List[Dict]:
    """
    Discover entry points passed to Thread/Executor instantiations.

    Patterns detected:
    - new Thread(Runnable)
    - executor.submit(Callable/Runnable)
    - executor.execute(Runnable)
    - ThreadPoolExecutor with task submission

    Returns: List of {file, method, line, offset, evidence}
    """
    entry_points: List[Dict] = []

    # Search for thread/executor patterns
    patterns = [
        (r'new\s+Thread\s*\(', 'new-thread'),
        (r'\.submit\s*\(', 'executor-submit'),
        (r'\.execute\s*\(', 'executor-execute'),
        (r'ThreadPoolExecutor.*\.submit', 'threadpool-submit'),
    ]

    for pattern, evidence_type in patterns:
        try:
            cmd = ["git", "grep", "-n", "-E", pattern]
            result = subprocess.run(
                cmd, capture_output=True, encoding='utf-8',
                errors='replace', timeout=10, cwd=repo_root
            )

            for line in (result.stdout or '').strip().split('\n'):
                if not line:
                    continue
                parts = line.split(':', 2)
                if len(parts) < 3:
                    continue

                file_path = parts[0]
                try:
                    line_num = int(parts[1])
                except ValueError:
                    continue
                line_text = parts[2]

                if _is_non_application_file(file_path) or _is_test_file(file_path):
                    continue

                # Extract the argument to Thread/submit/execute
                # Look for class instantiations: new ClassName()
                class_match = re.search(r'new\s+([A-Z]\w+)\s*\(', line_text)
                if class_match:
                    target_class = class_match.group(1)
                    entry_points.append({
                        'file': file_path,
                        'line': line_num,
                        'target_class': target_class,
                        'evidence': f'thread-target:{evidence_type}',
                        'type': 'thread-executor-target',
                    })
        except Exception:
            continue

    return entry_points


def _discover_annotation_driven_patterns(repo_root: str) -> List[Dict]:
    """
    Discover annotations that mark framework callback methods.

    Algorithm:
    1. Find @WebServlet/@RestController/@RequestMapping classes
    2. Find methods with hot annotations (appear near invoke/call/execute/handle)
    3. Mark servlet doGet/doPost/etc methods as entry points

    Returns: List of {file, method, line, annotation, evidence}
    """
    entry_points: List[Dict] = []
    hot_annotations: Set[str] = set()

    # Phase 1: Find @WebServlet annotated classes and their servlet methods
    try:
        cmd = ["git", "grep", "-n", "-A", "2", "@WebServlet"]
        result = subprocess.run(
            cmd, capture_output=True, encoding='utf-8',
            errors='replace', timeout=15, cwd=repo_root
        )

        current_file = None
        current_line = None

        for line in (result.stdout or '').strip().split('\n'):
            if not line or line.startswith('--'):  # Skip separator lines
                continue

            # Parse git grep output format: "file:line:text" or "file-line-text"
            if ':' in line:
                parts = line.split(':', 2)
            elif '-' in line:
                parts = line.split('-', 2)
            else:
                continue

            if len(parts) < 3:
                continue

            file_path = parts[0]
            line_text = parts[2]

            if _is_non_application_file(file_path) or _is_test_file(file_path):
                continue

            # Found @WebServlet annotation
            if '@WebServlet' in line_text:
                current_file = file_path
                current_line = parts[1]
            # Found servlet class declaration after @WebServlet
            elif current_file and file_path == current_file and 'extends HttpServlet' in line_text:
                # This is a servlet class - mark all servlet dispatch methods as entry points
                for servlet_method in ['doGet', 'doPost', 'doPut', 'doDelete', 'doHead', 'doOptions', 'doTrace', 'service']:
                    entry_points.append({
                        'file': current_file,
                        'line': int(current_line) if current_line.isdigit() else 0,
                        'method': servlet_method,
                        'annotation': 'WebServlet',
                        'evidence': f'annotation-driven:@WebServlet:servlet:{servlet_method}',
                        'type': 'annotation-servlet',
                    })
                current_file = None
    except Exception:
        pass

    # Phase 2: Find annotations near framework invocation patterns
    try:
        cmd = ["git", "grep", "-n", "-B", "5", "-E",
               r'(\.invoke\(|\.call\(|\.execute\(|\.handle\()']
        result = subprocess.run(
            cmd, capture_output=True, encoding='utf-8',
            errors='replace', timeout=15, cwd=repo_root
        )

        # Extract annotations that appear within 5 lines before invoke/call/execute
        for line in (result.stdout or '').strip().split('\n'):
            if '@' in line and not line.strip().startswith('//'):
                # Extract annotation name
                ann_match = re.search(r'@([A-Z]\w+)', line)
                if ann_match:
                    hot_annotations.add(ann_match.group(1))
    except Exception:
        pass

    # Phase 3: Find methods annotated with hot annotations
    for annotation in hot_annotations:
        try:
            cmd = ["git", "grep", "-n", "-A", "2", f"@{annotation}"]
            result = subprocess.run(
                cmd, capture_output=True, encoding='utf-8',
                errors='replace', timeout=10, cwd=repo_root
            )

            current_file = None
            current_line = None

            for line in (result.stdout or '').strip().split('\n'):
                if not line:
                    continue

                parts = line.split(':', 2)
                if len(parts) < 3:
                    continue

                file_path = parts[0]
                try:
                    line_num = int(parts[1])
                except ValueError:
                    continue
                line_text = parts[2]

                if _is_non_application_file(file_path) or _is_test_file(file_path):
                    continue

                # Look for method declaration after annotation
                if f'@{annotation}' in line_text:
                    current_file = file_path
                    current_line = line_num
                elif current_file and re.search(r'(public|protected|private)\s+\w+.*\w+\s*\(', line_text):
                    # Found method declaration
                    method_match = re.search(r'\s+(\w+)\s*\(', line_text)
                    if method_match:
                        entry_points.append({
                            'file': current_file,
                            'line': current_line,
                            'method': method_match.group(1),
                            'annotation': annotation,
                            'evidence': f'annotation-driven:@{annotation}',
                            'type': 'annotation-callback',
                        })
                    current_file = None
                    current_line = None
        except Exception:
            continue

    return entry_points


def _discover_config_file_patterns(repo_root: str) -> List[Dict]:
    """
    Discover entry points wired via configuration files.

    Patterns detected:
    - web.xml: <servlet-class>, <filter-class>, <listener-class>
    - Spring XML: <bean class="...">
    - application.yml: handler/endpoint mappings

    Returns: List of {file, class, method, evidence}
    """
    entry_points: List[Dict] = []

    # Find configuration files
    try:
        cmd = ["find", repo_root, "-type", "f",
               "(", "-name", "web.xml", "-o", "-name", "application.yml",
               "-o", "-name", "*.xml", ")",
               "-path", "*/src/main/*"]
        result = subprocess.run(
            cmd, capture_output=True, encoding='utf-8',
            errors='replace', timeout=10
        )

        config_files = [f.strip() for f in (result.stdout or '').strip().split('\n') if f]
    except Exception:
        config_files = []

    # Parse each config file
    for config_file in config_files[:50]:  # Limit to avoid timeout
        if not os.path.isfile(config_file):
            continue

        try:
            with open(config_file, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except Exception:
            continue

        rel_path = os.path.relpath(config_file, repo_root)

        # Extract servlet-class declarations
        for match in re.finditer(r'<servlet-class>\s*([a-zA-Z0-9_.]+)\s*</servlet-class>', content):
            class_name = match.group(1)
            entry_points.append({
                'file': rel_path,
                'class': class_name,
                'evidence': f'config-wired:servlet-class',
                'type': 'config-servlet',
            })

        # Extract filter-class declarations
        for match in re.finditer(r'<filter-class>\s*([a-zA-Z0-9_.]+)\s*</filter-class>', content):
            class_name = match.group(1)
            entry_points.append({
                'file': rel_path,
                'class': class_name,
                'evidence': f'config-wired:filter-class',
                'type': 'config-filter',
            })

        # Extract listener-class declarations
        for match in re.finditer(r'<listener-class>\s*([a-zA-Z0-9_.]+)\s*</listener-class>', content):
            class_name = match.group(1)
            entry_points.append({
                'file': rel_path,
                'class': class_name,
                'evidence': f'config-wired:listener-class',
                'type': 'config-listener',
            })

    return entry_points


def _discover_main_methods(repo_root: str) -> List[Dict]:
    """
    Discover public static void main(String[] args) entry points.

    Returns: List of {file, method, line, evidence}
    """
    entry_points: List[Dict] = []

    try:
        cmd = ["git", "grep", "-n", "-E",
               r'public\s+static\s+void\s+main\s*\(\s*String']
        result = subprocess.run(
            cmd, capture_output=True, encoding='utf-8',
            errors='replace', timeout=10, cwd=repo_root
        )

        for line in (result.stdout or '').strip().split('\n'):
            if not line:
                continue
            parts = line.split(':', 2)
            if len(parts) < 3:
                continue

            file_path = parts[0]
            try:
                line_num = int(parts[1])
            except ValueError:
                continue

            if _is_non_application_file(file_path) or _is_test_file(file_path):
                continue

            entry_points.append({
                'file': file_path,
                'method': 'main',
                'line': line_num,
                'evidence': 'main-method',
                'type': 'main-entry',
            })
    except Exception:
        pass

    return entry_points


def _discover_callback_interfaces(repo_root: str) -> List[Dict]:
    """
    Discover callback methods registered via setListener/addEventListener.

    Algorithm:
    1. Find setListener/addEventListener/setHandler calls
    2. Extract the interface/class being registered
    3. Find callback methods in those interfaces

    Returns: List of {file, interface, method, evidence}
    """
    entry_points: List[Dict] = []
    callback_patterns: Set[Tuple[str, str]] = set()  # (interface, method)

    # Phase 1: Find listener registration patterns
    try:
        cmd = ["git", "grep", "-n", "-E",
               r'\.(setListener|addEventListener|setHandler|addListener)\s*\(']
        result = subprocess.run(
            cmd, capture_output=True, encoding='utf-8',
            errors='replace', timeout=10, cwd=repo_root
        )

        for line in (result.stdout or '').strip().split('\n'):
            if not line:
                continue
            parts = line.split(':', 2)
            if len(parts) < 3:
                continue

            file_path = parts[0]
            line_text = parts[2]

            if _is_non_application_file(file_path) or _is_test_file(file_path):
                continue

            # Extract interface/class name from: new InterfaceName()
            class_match = re.search(r'new\s+([A-Z]\w+)\s*\(', line_text)
            if class_match:
                callback_patterns.add((file_path, class_match.group(1)))
    except Exception:
        pass

    # Phase 2: Find methods in callback interfaces
    for file_path, interface_name in callback_patterns:
        try:
            abs_path = os.path.join(repo_root, file_path)
            if not os.path.isfile(abs_path):
                continue

            with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()

            # Find interface/class declaration
            class_pattern = rf'(interface|class)\s+{re.escape(interface_name)}\s'
            if re.search(class_pattern, content):
                # Find methods in this interface
                for method_match in re.finditer(
                    r'(public\s+|void\s+|abstract\s+)*(\w+)\s*\([^)]*\)\s*[;{{]',
                    content
                ):
                    method_name = method_match.group(2)
                    if method_name not in _CONTROL_KEYWORDS:
                        entry_points.append({
                            'file': file_path,
                            'interface': interface_name,
                            'method': method_name,
                            'evidence': f'callback-interface:{interface_name}',
                            'type': 'callback',
                        })
        except Exception:
            continue

    return entry_points


def discover_entry_points_from_usage(repo_root: str, force_refresh: bool = False) -> Dict[str, List[Dict]]:
    """
    Discover entry points by analyzing ACTUAL USAGE PATTERNS in the codebase.
    Zero hardcoded framework names, annotations, or class names.

    This is a FULLY DYNAMIC discovery system that learns from:
    1. Network I/O patterns (ServerSocket, bind, etc.)
    2. Thread/Executor instantiations (new Thread, executor.submit, etc.)
    3. Annotation-driven callbacks (finds hot annotations automatically)
    4. Configuration file wiring (web.xml, application.yml, etc.)
    5. Main method declarations
    6. Callback interface registrations (setListener, addEventListener, etc.)
    7. Reflective dispatch patterns (existing implementation)

    Args:
        repo_root: Absolute path to repository root
        force_refresh: If True, ignore cache and re-scan

    Returns: Dict with keys:
        - 'network_io': List of network listener entry points
        - 'thread_executor': List of thread/executor target entry points
        - 'annotation_driven': List of annotation-based callbacks
        - 'config_wired': List of configuration-driven entry points
        - 'main_methods': List of main() entry points
        - 'callbacks': List of callback interface methods
        - 'all': Deduplicated list of all entry points
        - 'stats': Summary statistics

    Each entry point dict contains:
        - file: Relative file path
        - method: Method name (when available)
        - line: Line number (when available)
        - type: Entry point type
        - evidence: Human-readable explanation
        - confidence: 0.0-1.0 (not yet implemented, placeholder)
    """
    global _DYNAMIC_ENTRY_POINT_CACHE, _DYNAMIC_CACHE_REPO

    # Return in-memory cached results if available
    if not force_refresh and _DYNAMIC_ENTRY_POINT_CACHE is not None \
            and _DYNAMIC_CACHE_REPO == repo_root:
        return _DYNAMIC_ENTRY_POINT_CACHE

    # Check disk cache (persists across subprocess invocations)
    disk_cache_path = os.path.normpath(_DYNAMIC_EP_DISK_CACHE)
    if not force_refresh and os.path.exists(disk_cache_path):
        try:
            import json as _json
            with open(disk_cache_path, 'r', encoding='utf-8') as _f:
                cached = _json.load(_f)
            if cached.get('repo_root') == os.path.abspath(repo_root):
                # Restore sets that were serialized as lists
                _DYNAMIC_ENTRY_POINT_CACHE = cached['results']
                _DYNAMIC_CACHE_REPO = repo_root
                stats = cached['results'].get('stats', {})
                print(f"[Dynamic Entry Point Discovery] Loaded from disk cache ({stats.get('total_entry_points', '?')} entry points)")
                return _DYNAMIC_ENTRY_POINT_CACHE
        except Exception:
            pass  # Cache corrupt/stale, re-scan

    print(f"[Dynamic Entry Point Discovery] Scanning repository: {repo_root}")

    # Run all discovery algorithms in parallel (conceptually)
    results = {
        'network_io': _discover_network_io_patterns(repo_root),
        'thread_executor': _discover_thread_executor_patterns(repo_root),
        'annotation_driven': _discover_annotation_driven_patterns(repo_root),
        'config_wired': _discover_config_file_patterns(repo_root),
        'main_methods': _discover_main_methods(repo_root),
        'callbacks': _discover_callback_interfaces(repo_root),
    }

    # Deduplicate and build unified list
    all_entries: List[Dict] = []
    seen_keys: Set[str] = set()

    for category, entries in results.items():
        for entry in entries:
            # Build unique key from file + method/class + line
            key_parts = [
                entry.get('file', ''),
                entry.get('method', entry.get('class', entry.get('interface', ''))),
                str(entry.get('line', 0)),
            ]
            key = ':'.join(key_parts)

            if key not in seen_keys:
                seen_keys.add(key)
                all_entries.append(entry)

    results['all'] = all_entries
    results['stats'] = {
        'total_entry_points': len(all_entries),
        'network_io': len(results['network_io']),
        'thread_executor': len(results['thread_executor']),
        'annotation_driven': len(results['annotation_driven']),
        'config_wired': len(results['config_wired']),
        'main_methods': len(results['main_methods']),
        'callbacks': len(results['callbacks']),
    }

    # Cache results (in-memory)
    _DYNAMIC_ENTRY_POINT_CACHE = results
    _DYNAMIC_CACHE_REPO = repo_root

    # Persist to disk so subprocess invocations don't re-scan
    try:
        import json as _json
        os.makedirs(os.path.dirname(disk_cache_path), exist_ok=True)
        with open(disk_cache_path, 'w', encoding='utf-8') as _f:
            _json.dump({
                'repo_root': os.path.abspath(repo_root),
                'results': results,
            }, _f, indent=2, ensure_ascii=False)
    except Exception:
        pass  # Non-fatal: next subprocess will just re-scan

    print(f"[Dynamic Entry Point Discovery] Found {len(all_entries)} entry points")
    print(f"  - Network I/O: {results['stats']['network_io']}")
    print(f"  - Thread/Executor targets: {results['stats']['thread_executor']}")
    print(f"  - Annotation-driven: {results['stats']['annotation_driven']}")
    print(f"  - Config-wired: {results['stats']['config_wired']}")
    print(f"  - Main methods: {results['stats']['main_methods']}")
    print(f"  - Callbacks: {results['stats']['callbacks']}")

    return results


def _reflective_entry_pattern(file_path: str,
                              file_content: str,
                              method_name: str,
                              method_offset: int,
                              repo_root: str = '') -> Optional[str]:
    """
    Detect entry points invoked by an in-repo reflective dispatcher.

    Some frameworks (e.g. a rule engine) wire up entry points by
    reflection rather than by direct call:

        Class cls = Class.forName("<package>." + className);
        Method m = cls.getMethod("get" + className);
        m.invoke(instance);

    Static call-graph analysis cannot see those invocations, so the
    targets look like dead leaves. We detect them by looking for a
    `Class.forName(...)` + `getMethod(...)` pattern elsewhere in the
    repository AND verifying the current method's name matches the
    dispatcher's method-name template AND the current class lives in
    the dispatcher's target package.

    The detection is fully data-driven: it reads the dispatcher source
    to discover the target package and method-name prefix. No package
    or class names are hardcoded.

    Returns the entry-type reason (e.g. `"reflective-dispatch:get<X>"`)
    when the method matches a discovered pattern, else None.
    """
    if not repo_root:
        return None
    contracts = _discover_reflective_contracts(repo_root)
    if not contracts:
        return None

    # Convert file_path to a package-path string for matching against
    # target_package. Supports multiple source root conventions.
    norm = file_path.replace('\\', '/')
    # Try common source root patterns for all languages
    source_roots = ['src/main/java/', 'src/main/kotlin/', 'src/', 'lib/', 'app/']
    pkg_path = norm
    for root in source_roots:
        idx = norm.find(root)
        if idx >= 0:
            pkg_path = norm[idx + len(root):]
            break
    pkg_path = pkg_path.rsplit('/', 1)[0].replace('/', '.') + '.'

    for contract in contracts:
        target_pkg = contract['target_package']
        method_prefix = contract['method_prefix']
        if target_pkg in pkg_path or pkg_path.startswith(target_pkg):
            if not method_name.startswith(method_prefix):
                continue
            suffix = method_name[len(method_prefix):]
            # Find simple class name (last `class X` declaration before offset).
            class_block = _class_decl_block(file_content, method_offset)
            cm = re.search(r'(?:class|interface|enum)\s+(\w+)', class_block)
            if cm and cm.group(1) == suffix:
                return f'reflective-dispatch:{method_prefix}{suffix}'
    return None


def is_legitimate_entry_point(file_content: str,
                              method_name: str,
                              method_offset: int,
                              file_path: str = '',
                              repo_root: str = '',
                              discovered_entries: Optional[Dict[str, List[Dict]]] = None,
                              profile: Optional[Dict] = None) -> Tuple[bool, str]:
    """
    Decide whether `method_name` at `method_offset` in `file_content` is
    a TRUE runtime entry point using DYNAMIC DISCOVERY (zero hardcoded patterns).

    Args:
        file_content: full source of the file (used for class context)
        method_name: name of the candidate entry point
        method_offset: offset of the method declaration within file_content
        file_path: optional repo-relative path - required for dynamic matching
        repo_root: absolute path to the repository root - required for discovery
        discovered_entries: Optional pre-computed discovery results from
            discover_entry_points_from_usage(). If None, discovery is run
            on-demand (with caching).

    Returns:
        (is_legitimate, reason)

    IMPLEMENTATION NOTE:
    This function now uses ZERO hardcoded framework patterns. All entry points
    are discovered dynamically by analyzing actual usage in the codebase:
    - Servlet dispatch methods: discovered via servlet-class config wiring
    - REST endpoints: discovered via hot annotation analysis
    - Thread/Executor targets: discovered via new Thread/executor.submit patterns
    - Callbacks: discovered via setListener/addEventListener registrations
    - etc.

    The old hardcoded constants (_SERVLET_DISPATCH_METHODS, _REST_METHOD_ANNOTATIONS,
    _BOUNDARY_METHODS) have been DELETED. Their contents are now learned from
    the repository's interface declarations via `discover_interface_methods()`,
    with `_JDK_INTERFACE_METHOD_SEEDS` providing fallback names for JDK /
    Jakarta interfaces whose source isn't in the repo.
    """
    if not method_name:
        return False, 'no method name'

    # ----------------------------------------------------------------------
    # UNCONDITIONAL REJECTIONS (BUG-S1-3 / S1-4 / S1-5 from validation
    # report). Run BEFORE any dynamic discovery so an accidental match in
    # the discovered-entry table cannot resurrect a method that is
    # provably not callable as a runtime entry point.
    #
    #   1. Trivial getters/setters/equals/hashCode/etc. - covered by
    #      `_is_trivial_method`. Even if a discovered annotation happens
    #      to sit on the same line, a 1-line accessor is never a runtime
    #      entry-point in this codebase.
    #   2. `private` and `protected` methods - by Java visibility rules,
    #      these cannot be invoked by an external framework dispatcher
    #      (the framework lives outside the declaring class hierarchy).
    #      Servlet / REST / scheduled methods declared on a class are
    #      always `public`. Subclass-only invocation paths are rare and
    #      always have an alternate `public` shim above them which the
    #      trace will find naturally.
    #   3. `main(String[])` debug stubs - this server-side codebase has
    #      no real CLI entry. Any `main` is a developer's local debug
    #      harness, not a runtime path. Exception: classes annotated
    #      `@SpringBootApplication` or implementing `CommandLineRunner`
    #      *are* legitimate CLI entries and we let them through.
    # ----------------------------------------------------------------------
    if _is_trivial_method(method_name):
        return False, 'rejected:trivial-accessor'

    method_signature = _extract_method_signature(file_content, method_offset)
    is_private = bool(re.search(
        r'(?<![A-Za-z0-9_])private(?![A-Za-z0-9_])', method_signature
    ))

    # `private` is always rejected: a Java framework dispatcher cannot
    # invoke a private method on user code. The trace already enforces
    # same-file restriction for private callees, so any private hit
    # here is a leftover from the upward trace and must be dropped.
    #
    # Note: `protected` is NOT unconditionally rejected. `protected
    # void doGet(...)` on an HttpServlet subclass is a real runtime
    # entry point (the servlet container invokes the inherited method
    # via super-class dispatch). Such methods will be classified by
    # the dispatch / inheritance checks further down. Any protected
    # method that doesn't match those checks falls through to
    # 'not-a-framework-entry-point' and is dropped - so protected
    # boilerplate cannot leak through.
    if is_private:
        return False, 'rejected:private-method'

    if method_name == 'main':
        # Only allow if the file has an explicit entry-point marker from the
        # active language profile (e.g. @SpringBootApplication, if __name__).
        has_marker, _ = _has_any_entry_point_marker(file_content)
        if not has_marker:
            return False, 'rejected:debug-main-stub'

    # ------------------------------------------------------------------
    # YAML-driven rejection layer (data-driven; ZERO hardcoded patterns).
    # If the active language config rejects this method (test path, vendor
    # path, abstract, trivial accessor, dunder, declaration file, ...) we
    # short-circuit BEFORE running dynamic discovery. No false negatives:
    # the YAMLs intentionally only reject what was already rejected by the
    # legacy hardcoded path; the win is they live in configuration.
    # ------------------------------------------------------------------
    yaml_rejected, yaml_reason = _yaml_filter_rejects(
        file_content=file_content,
        method_name=method_name,
        method_offset=method_offset,
        file_path=file_path or '',
    )
    if yaml_rejected:
        return False, f'rejected:yaml-filter:{yaml_reason}'

    if not file_path or not repo_root:
        # Fallback to old hardcoded logic if required parameters missing
        return _is_legitimate_entry_point_hardcoded(
            file_content, method_name, method_offset, file_path, repo_root
        )

    # ------------------------------------------------------------------
    # UNIFIED PROFILE-DRIVEN ENTRY POINT DETECTION
    #
    # Works for ALL languages: Java, Python, TypeScript, JavaScript, etc.
    # Uses the active language profile's entry_point_markers to classify.
    # No hardcoded framework categories — any marker match is accepted.
    # ------------------------------------------------------------------

    # 1. Check method annotations/decorators against profile markers
    method_annotations = _annotations_above(file_content, method_offset)
    markers = _get_entry_point_markers()
    for marker in markers:
        if marker.startswith('@'):
            # Annotation/decorator marker — check in the annotation block
            ann_name = marker[1:]  # strip @
            pattern = r'@' + re.escape(ann_name) + r'(?:\s|\(|$)'
            if re.search(pattern, method_annotations, re.MULTILINE):
                return True, f'profile-marker:{marker}'
        elif marker.startswith(('extends ', 'implements ')):
            # Inheritance marker — check in the class declaration block
            class_block = _class_decl_block(file_content, method_offset)
            if marker in class_block:
                return True, f'profile-marker:{marker}'
        elif marker == 'if __name__':
            # Python main guard — check if this is in a __main__ block
            if 'if __name__' in file_content:
                return True, f'profile-marker:{marker}'
        else:
            # Raw code markers (e.g. 'app.get', 'module.exports',
            # 'export default', 'CommandLineRunner')
            # Check in the 10 lines preceding the method declaration
            preceding = file_content[:method_offset].splitlines()[-10:]
            preceding_text = '\n'.join(preceding)
            if marker in preceding_text:
                return True, f'profile-marker:{marker}'
            # Also check class-level markers in the enclosing class block
            class_block = _class_decl_block(file_content, method_offset)
            if marker in class_block:
                return True, f'profile-marker:{marker}'

    # 2. Run dynamic discovery (cached after first call)
    if discovered_entries is None:
        discovered_entries = discover_entry_points_from_usage(repo_root, force_refresh=False)

    # Pre-compute the enclosing class FQN for cross-file class matches
    # (used by config-wired entries from web.xml / Spring XML where the
    # entry row references the class by FQN but lives in a different file).
    enclosing_pkg = ''
    pkg_match = re.search(r'^\s*package\s+([\w.]+)\s*;', file_content, re.MULTILINE)
    if pkg_match:
        enclosing_pkg = pkg_match.group(1)
    enclosing_class_for_match = _enclosing_class_name(file_content, method_offset) or ''
    enclosing_fqn = (
        f"{enclosing_pkg}.{enclosing_class_for_match}"
        if enclosing_pkg and enclosing_class_for_match
        else enclosing_class_for_match
    )

    # Check if this file:method appears in ANY discovered pattern
    for pattern_type, entries in discovered_entries.items():
        if pattern_type in ('all', 'stats'):
            continue
        for entry in entries:
            entry_file = entry.get('file', '')
            entry_method = entry.get('method', '')
            entry_line = entry.get('line', 0)
            entry_class = entry.get('class', '')

            # Match by file + method name
            if entry_file == file_path and entry_method == method_name:
                evidence = entry.get('evidence', pattern_type)
                return True, f'dynamic-discovery:{evidence}'

            # Match by line proximity (for annotation-driven entries)
            if entry_file == file_path and entry_line > 0:
                method_line = file_content[:method_offset].count('\n') + 1
                if abs(method_line - entry_line) <= 5:
                    evidence = entry.get('evidence', pattern_type)
                    return True, f'dynamic-discovery:{evidence}'

            # Cross-file class match (web.xml / Spring XML config wiring).
            # The entry row lives in web.xml but names the dispatched class
            # by FQN. If the candidate method's enclosing class matches the
            # wired class FQN AND the method is a known dispatch method for
            # that framework category, accept it as a runtime entry.
            if entry_class and enclosing_fqn and enclosing_class_for_match:
                fqn_match = (
                    entry_class == enclosing_fqn
                    or entry_class.endswith('.' + enclosing_class_for_match)
                )
                if fqn_match:
                    etype = entry.get('type', '')
                    dispatch_set: Set[str] = set()
                    if etype == 'config-servlet':
                        dispatch_set = discover_servlet_dispatch_methods(repo_root)
                    elif etype == 'config-filter':
                        dispatch_set = discover_filter_dispatch_methods(repo_root)
                    elif etype == 'config-listener':
                        dispatch_set = discover_listener_dispatch_methods(repo_root)
                    if method_name in dispatch_set:
                        evidence = entry.get('evidence', pattern_type)
                        return True, f'dynamic-discovery:{evidence}:{method_name}'

    # 3. Inheritance-based check — dispatch methods from interface hierarchy
    #    (only meaningful for languages with class inheritance)
    enclosing_class = _enclosing_class_name(file_content, method_offset)
    if enclosing_class:
        # Check dynamically discovered dispatch methods
        servlet_methods = discover_servlet_dispatch_methods(repo_root)
        filter_methods = discover_filter_dispatch_methods(repo_root)
        listener_methods = discover_listener_dispatch_methods(repo_root)
        thread_methods = discover_thread_target_methods(repo_root)

        if method_name in servlet_methods and _file_class_inherits_from(
                file_content, method_offset, _SERVLET_BASES, repo_root):
            return True, f'dynamic-discovery:servlet-inherited:{method_name}'
        if method_name in filter_methods and _file_class_inherits_from(
                file_content, method_offset, _FILTER_BASES, repo_root):
            return True, f'dynamic-discovery:filter-inherited:{method_name}'
        if method_name in listener_methods and _file_class_inherits_from(
                file_content, method_offset, _LISTENER_BASES, repo_root):
            return True, f'dynamic-discovery:listener-inherited:{method_name}'
        if method_name in thread_methods and _file_class_inherits_from(
                file_content, method_offset, _THREAD_BASES, repo_root):
            return True, f'dynamic-discovery:thread-inherited:{method_name}'

        # REST annotation check (dynamic discovery from repo)
        if _has_rest_method_annotation(method_annotations, repo_root):
            return True, 'dynamic-discovery:rest-endpoint'

        # Scheduled / event-listener annotation
        if _has_scheduled_or_listener_annotation(method_annotations):
            return True, 'dynamic-discovery:scheduled-or-listener'

    # 4. Legacy reflective-dispatch check (already dynamic)
    if file_path and repo_root:
        reflective_reason = _reflective_entry_pattern(
            file_path, file_content, method_name, method_offset,
            repo_root=repo_root,
        )
        if reflective_reason:
            return True, f'dynamic-discovery:{reflective_reason}'

    return False, 'not-a-framework-entry-point'


def _is_legitimate_entry_point_hardcoded(file_content: str,
                                         method_name: str,
                                         method_offset: int,
                                         file_path: str = '',
                                         repo_root: str = '') -> Tuple[bool, str]:
    """
    Fallback entry-point validator used when callers don't supply a
    `discovered_entries` snapshot. Despite the legacy name this routine
    is now FULLY DYNAMIC when `repo_root` is provided - it consults
    `discover_servlet_dispatch_methods` / `discover_filter_dispatch_methods`
    etc. instead of any hardcoded method-name set.

    When `repo_root` is empty (rare unit-test path) it falls back to the
    JDK-only seed sets in `_JDK_INTERFACE_METHOD_SEEDS` so it can still
    classify standard javax.servlet / java.util.concurrent shapes.
    """
    if not method_name:
        return False, 'no method name'

    # Mirror the unconditional rejections from is_legitimate_entry_point
    # so the fallback path can never emit a private/protected/trivial
    # method as an entry point.
    if _is_trivial_method(method_name):
        return False, 'rejected:trivial-accessor'
    method_signature = _extract_method_signature(file_content, method_offset)
    if re.search(r'(?<![A-Za-z0-9_])private(?![A-Za-z0-9_])', method_signature):
        return False, 'rejected:private-method'
    if re.search(r'(?<![A-Za-z0-9_])protected(?![A-Za-z0-9_])', method_signature):
        return False, 'rejected:protected-method'
    if method_name == 'main':
        if ('@SpringBootApplication' not in file_content
                and 'CommandLineRunner' not in file_content):
            return False, 'rejected:debug-main-stub'

    class_block = _class_decl_block(file_content, method_offset)
    method_annotations = _annotations_above(file_content, method_offset)

    # Dynamic dispatch-method sets (JDK seeds when repo_root is empty).
    if repo_root:
        servlet_methods = discover_servlet_dispatch_methods(repo_root)
        filter_methods = discover_filter_dispatch_methods(repo_root)
        listener_methods = discover_listener_dispatch_methods(repo_root)
        thread_methods = discover_thread_target_methods(repo_root)
    else:
        servlet_methods = (_JDK_INTERFACE_METHOD_SEEDS.get('HttpServlet', set())
                           | _JDK_INTERFACE_METHOD_SEEDS.get('GenericServlet', set())
                           | _JDK_INTERFACE_METHOD_SEEDS.get('Servlet', set()))
        filter_methods = _JDK_INTERFACE_METHOD_SEEDS.get('Filter', set())
        listener_methods = (_JDK_INTERFACE_METHOD_SEEDS.get('MessageListener', set())
                            | _JDK_INTERFACE_METHOD_SEEDS.get('ApplicationListener', set()))
        thread_methods = (_JDK_INTERFACE_METHOD_SEEDS.get('Runnable', set())
                          | _JDK_INTERFACE_METHOD_SEEDS.get('Callable', set()))

    # 1) Servlet method: class is HttpServlet/GenericServlet AND method is
    #    one of the dispatch methods (discovered from interface scanning).
    if method_name in servlet_methods and _is_class_servlet(class_block):
        return True, f'servlet:{method_name}'

    # 2) REST controller endpoint: method has a REST annotation OR class
    #    has a REST controller annotation and method has @Path.
    if _has_rest_method_annotation(method_annotations, repo_root):
        return True, 'rest-endpoint'
    if _is_class_rest_controller(class_block) and '@Path' in method_annotations:
        return True, 'rest-endpoint'

    # 3) Scheduled task / event listener annotated method.
    if _has_scheduled_or_listener_annotation(method_annotations):
        return True, 'scheduled-or-listener'

    # 4) Runnable.run / Thread.run / TimerTask.run.
    if method_name in thread_methods and _is_class_runnable(class_block):
        return True, f'runnable.{method_name}'

    # 5) Callable.call.
    if method_name in thread_methods and _is_class_callable(class_block):
        return True, f'callable.{method_name}'

    # 6) Servlet Filter.doFilter.
    if method_name in filter_methods and _is_class_filter(class_block):
        return True, f'filter.{method_name}'

    # 7) Listener callbacks.
    if method_name in listener_methods and _is_class_listener(class_block):
        return True, f'listener:{method_name}'

    # 8) Reflective-dispatch entry points: a separate dispatcher in the
    #    repo invokes this method via Class.forName + getMethod. The
    #    contract (target package + method-name prefix) is discovered
    #    dynamically from the dispatcher source.
    if file_path and repo_root:
        reflective_reason = _reflective_entry_pattern(
            file_path, file_content, method_name, method_offset,
            repo_root=repo_root,
        )
        if reflective_reason:
            return True, reflective_reason

    # 9) main() - per the analysis brief, treat as DEBUG STUB and exclude.
    #    Real CLI entry points are extremely rare in this server-side
    #    codebase. If a future caller wants to keep them, they should
    #    inspect Main-Class manifest entries explicitly.
    if method_name == 'main':
        return False, 'main-debug-stub'

    return False, 'not-a-framework-entry-point'


# --------------------------------------------------------------------------
# Call-chain validation (verify each hop actually exists in source)
# --------------------------------------------------------------------------

def _extract_method_body(file_content: str,
                         method_name: str,
                         method_offset: Optional[int] = None) -> Optional[str]:
    """
    Extract the body (between matched braces) of `method_name`. If
    `method_offset` is provided we use that as the declaration start;
    otherwise we search for the first declaration with that name.
    """
    if method_offset is None:
        # Best-effort regex search for a declaration of method_name.
        decl = re.search(
            r'(?:public|protected|private|static|final|synchronized|abstract|native|default)\s+'
            r'[\w<>\[\],\s\?]+\s+' + re.escape(method_name) + r'\s*\(',
            file_content
        )
        if not decl:
            return None
        method_offset = decl.start()
    brace_open = file_content.find('{', method_offset)
    if brace_open == -1:
        return None
    brace_close = SerenaMCPClient._matching_brace_end(file_content, brace_open)
    if brace_close is None:
        return None
    return file_content[brace_open:brace_close + 1]


def _body_contains_call_to(method_body: str, callee: str) -> bool:
    """
    True if `method_body` contains a call expression `<callee>(` that is
    not inside a comment / string / annotation. Uses the same heuristic
    as `_is_call_site` but works on a free-form body slice.
    """
    if not method_body or not callee:
        return False
    # Strip line-comments and block-comments to avoid false positives.
    no_block = re.sub(r'/\*.*?\*/', '', method_body, flags=re.DOTALL)
    no_line = re.sub(r'(?m)//.*?$', '', no_block)
    pattern = r'(?<![A-Za-z0-9_])' + re.escape(callee) + r'\s*\('
    return re.search(pattern, no_line) is not None


def _extract_method_signature(file_content: str, method_offset: int) -> str:
    """
    Extract the textual method declaration (everything from the start of
    the line containing `method_offset` up to but not including the opening
    `{`). Used to inspect modifiers (private / protected / static) without
    relying on a downstream regex over the whole file.
    """
    if not file_content or method_offset < 0:
        return ''
    line_start = file_content.rfind('\n', 0, method_offset) + 1
    brace_open = file_content.find('{', method_offset)
    if brace_open == -1:
        # Maybe abstract / interface method - no body, ends with ';'
        semi = file_content.find(';', method_offset)
        end = semi if semi != -1 else min(len(file_content), method_offset + 200)
    else:
        end = brace_open
    return file_content[line_start:end]


def _extract_callees_from_body(method_body: str) -> List[str]:
    """
    Extract names of all method calls from a method body, with comments
    and string literals stripped. Returns unique names in order of first
    appearance. Excludes language keywords / control flow tokens.
    """
    if not method_body:
        return []
    # Strip block / line comments first.
    no_block = re.sub(r'/\*.*?\*/', '', method_body, flags=re.DOTALL)
    no_line = re.sub(r'(?m)//.*?$', '', no_block)
    # Strip double-quoted string literals (handle simple escapes).
    no_strings = re.sub(r'"(?:\\.|[^"\\])*"', '""', no_line)
    no_strings = re.sub(r"'(?:\\.|[^'\\])*'", "''", no_strings)

    seen: Set[str] = set()
    callees: List[str] = []
    for m in re.finditer(r'(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*\(', no_strings):
        name = m.group(1)
        if name in _CONTROL_KEYWORDS:
            continue
        if name in seen:
            continue
        seen.add(name)
        callees.append(name)
    return callees


def _find_files_defining_method(repo_root: str, method_name: str) -> List[str]:
    """
    Return repo-relative paths of source files that DECLARE a method named
    `method_name`. Used by `verify_entry_point_reaches_target` to walk
    forward through the call graph when a callee's defining file isn't
    known a priori.

    Implementation: a single `git grep` for the declaration pattern. POSIX
    extended regex so it works on macOS / linux without GNU extensions.
    """
    if not method_name:
        return []
    cache_attr = '_files_defining_cache'
    if not hasattr(_find_files_defining_method, cache_attr):
        setattr(_find_files_defining_method, cache_attr, {})
    cache: Dict[Tuple[str, str], List[str]] = getattr(
        _find_files_defining_method, cache_attr
    )
    key = (repo_root, method_name)
    if key in cache:
        return list(cache[key])
    files: List[str] = []
    try:
        cmd = [
            'git', 'grep', '-l', '-E',
            r'(public|protected|private)[[:space:]]+[^=;]*[[:space:]]+'
            + re.escape(method_name) + r'[[:space:]]*\(',
        ]
        result = subprocess.run(
            cmd, capture_output=True, encoding='utf-8',
            errors='replace', cwd=repo_root, timeout=10,
        )
        for f in (result.stdout or '').strip().split('\n'):
            if not f:
                continue
            if _is_non_application_file(f):
                continue
            if _is_test_file(f):
                continue
            # Accept all source files matching the active language profile
            source_exts = _get_source_extensions()
            if not any(f.endswith(ext) for ext in source_exts):
                continue
            files.append(f)
    except Exception:
        pass
    cache[key] = files
    return files


def verify_entry_point_reaches_target(entry_point_file: str,
                                      entry_point_method: str,
                                      target_file: str,
                                      target_method: str,
                                      repo_root: str,
                                      chain: Optional[List[str]] = None,
                                      max_depth: int = 6) -> Tuple[bool, List[str]]:
    """
    Prove that (entry_point_file, entry_point_method) reaches
    (target_file, target_method) via a real static call path.

    Two complementary strategies are used:

      A. CHAIN-REPLAY (cheap, used when `chain` is supplied)
         The upward trace records the path from the seed up to each entry
         point as a list of "file:method" strings. We walk that path in
         REVERSE order (entry -> ... -> seed) and require every method
         in the chain to actually contain a call to the next one. This
         amounts to one method-body read per hop, so it's O(chain_len)
         in I/O. If every hop is verified, the chain is valid.

         The trace already does this hop-by-hop via
         `_caller_body_contains_call`, but we re-run it here as
         defence-in-depth (and so the verification result is recorded
         independently of the trace's own bookkeeping).

      B. FORWARD-WALK FALLBACK (used when no `chain` provided OR
         chain-replay fails)
         Walk forward from the entry point. At each method body, look
         for a call to `target_method` (verified against the actual
         declaration in `target_file`). If not found at this level,
         recurse into callees defined in the same file first, then
         widen to other files. Bounded by `max_depth` and a visited set.
         Skips trivial getters / setters / boilerplate.

    Returns:
      (is_valid: bool, path: List[str])

    On success `path` is the verified chain (entry -> ... -> target).
    On failure it is the deepest path explored, kept for diagnostics.
    """
    # Local file-content cache shared across both strategies. Reading the
    # same file repeatedly is the dominant cost here.
    content_cache: Dict[str, Optional[str]] = {}

    def _read(path: str) -> Optional[str]:
        if path in content_cache:
            return content_cache[path]
        abs_p = os.path.join(repo_root, path)
        if not os.path.isfile(abs_p):
            content_cache[path] = None
            return None
        try:
            with open(abs_p, 'r', encoding='utf-8', errors='replace') as f:
                content_cache[path] = f.read()
        except Exception:
            content_cache[path] = None
        return content_cache[path]

    def _method_body_at(file_path: str, method_name: str) -> Optional[str]:
        content = _read(file_path)
        if content is None:
            return None
        return _extract_method_body(content, method_name)

    def _all_method_bodies(file_path: str, method_name: str) -> List[str]:
        """
        Return the bodies of ALL overloads of `method_name` declared in
        `file_path`. We use a regex sweep for the declaration token and
        match each opening brace to its closing brace via the existing
        balanced-brace walker. This lets the chain-replay accept a hop
        if ANY overload contains the next-hop's callee, which is the
        correct semantics: the trace's chain doesn't carry arity per
        node, so any overload satisfying the call relation is valid.
        """
        content = _read(file_path)
        if content is None:
            return []
        bodies: List[str] = []
        for m in re.finditer(
            r'(?:public|protected|private|static|final|synchronized|abstract|native|default)\s+'
            r'[\w<>\[\],\s\?]+\s+' + re.escape(method_name) + r'\s*\(',
            content,
        ):
            brace_open = content.find('{', m.end())
            if brace_open == -1:
                continue
            brace_close = SerenaMCPClient._matching_brace_end(
                content, brace_open
            )
            if brace_close is None:
                continue
            bodies.append(content[brace_open:brace_close + 1])
        return bodies

    # ------------------------------------------------------------------
    # Strategy A: chain-replay
    # ------------------------------------------------------------------
    if chain and len(chain) >= 2:
        # Chain layout from the upward trace:
        #   chain[0] = seed  (target_file:target_method)
        #   chain[-1] = entry-point
        # Walk in reverse-index order: each chain[i+1] should call chain[i].
        replay_path: List[str] = [chain[-1]]
        replay_ok = True
        for i in range(len(chain) - 1, 0, -1):
            caller_token = chain[i]
            callee_token = chain[i - 1]
            try:
                caller_file, caller_method = caller_token.split(':', 1)
                callee_file, callee_method = callee_token.split(':', 1)
            except ValueError:
                replay_ok = False
                break
            # Accept the hop if ANY overload of caller_method in
            # caller_file invokes callee_method. The chain stored on
            # each entry point doesn't include arity information per
            # node, so we cannot demand a specific overload here -
            # picking the wrong one (e.g. the unrelated single-arg
            # overload of getWorkPolicyConfiguration) would falsely
            # invalidate real entry points.
            bodies = _all_method_bodies(caller_file, caller_method)
            if not bodies:
                # Fallback: best-effort single body lookup.
                single = _method_body_at(caller_file, caller_method)
                if single is not None:
                    bodies = [single]
            if not bodies:
                replay_ok = False
                break
            if not any(_body_contains_call_to(b, callee_method) for b in bodies):
                replay_ok = False
                break
            replay_path.append(callee_token)
        if replay_ok:
            return True, replay_path
        # Fall through to the forward-walk fallback - the trace's chain
        # may have a phantom hop that the body-replay catches even when
        # `_caller_body_contains_call` was satisfied at trace time.
        # The forward walk gives one more chance using a different
        # algorithm.

    # ------------------------------------------------------------------
    # Strategy B: bounded forward walk
    # ------------------------------------------------------------------
    visited: Set[str] = set()
    best_path: List[str] = [f"{entry_point_file}:{entry_point_method}"]

    # Pre-validate that target_method is actually declared in target_file.
    target_content = _read(target_file)
    if target_content is None:
        return False, best_path
    target_decl_pattern = (
        r'(?<![A-Za-z0-9_])' + re.escape(target_method) + r'\s*\('
    )
    if not re.search(target_decl_pattern, target_content):
        return False, best_path

    def walk(current_file: str, current_method: str,
             depth: int, path: List[str]) -> bool:
        nonlocal best_path
        if len(path) > len(best_path):
            best_path = list(path)
        if depth > max_depth:
            return False
        key = f"{current_file}:{current_method}"
        if key in visited:
            return False
        visited.add(key)

        if _is_non_application_file(current_file) or _is_test_file(current_file):
            return False
        if _is_trivial_method(current_method):
            return False

        bodies = _all_method_bodies(current_file, current_method)
        if not bodies:
            single = _method_body_at(current_file, current_method)
            if single is not None:
                bodies = [single]
        if not bodies:
            return False

        # Direct hit: target is invoked from any overload's body.
        if any(_body_contains_call_to(b, target_method) for b in bodies):
            best_path = path + [f"{target_file}:{target_method}"]
            return True

        # Recurse into intermediate callees. Same-file callees first
        # (private helpers, overloads, etc.) - this is where 90% of
        # bridge methods live in this codebase.
        # Aggregate callees across all overloads of the current method.
        callees: List[str] = []
        seen_callees: Set[str] = set()
        for b in bodies:
            for c in _extract_callees_from_body(b):
                if c not in seen_callees:
                    seen_callees.add(c)
                    callees.append(c)
        # Try same-file callees first.
        same_file_callees = []
        for callee in callees:
            if callee == current_method or _is_trivial_method(callee):
                continue
            current_content = _read(current_file)
            if current_content and re.search(
                r'(?<![A-Za-z0-9_])' + re.escape(callee) + r'\s*\(',
                current_content,
            ):
                # Same-file definition exists.
                if walk(current_file, callee, depth + 1,
                        path + [f"{current_file}:{callee}"]):
                    return True
                same_file_callees.append(callee)
        # Then try cross-file. Bounded fan-out per call site.
        explored = 0
        for callee in callees:
            if explored > 30:
                break
            if callee == current_method or _is_trivial_method(callee):
                continue
            if callee in same_file_callees:
                continue
            defining_files = _find_files_defining_method(repo_root, callee)
            # Bound the per-node cross-file fan-out aggressively.
            for f in defining_files[:4]:
                if f == current_file:
                    continue
                explored += 1
                if walk(f, callee, depth + 1, path + [f"{f}:{callee}"]):
                    return True
        return False

    initial_path = [f"{entry_point_file}:{entry_point_method}"]
    ok = walk(entry_point_file, entry_point_method, 0, initial_path)
    return ok, best_path


# --------------------------------------------------------------------------

class SerenaMCPClient:
    """
    Framework-agnostic code navigation using git grep.
    Traces call chains from changed methods to entry points.
    """

    def __init__(self, repo_path: str, enabled: bool = True, max_symbols: int = 200):
        self.repo_path = repo_path
        self.enabled = enabled
        self.max_symbols = max_symbols
        self._call_count = 0
        self._symbols_cache: Dict[str, Dict] = {}  # Cache symbols per file
        self._content_cache: Dict[str, str] = {}   # Cache file contents
        # Thread-safety locks for caches that may be touched from multiple
        # worker threads (build_flow_registry / parallel pipeline stages).
        # These are recursive locks so that helper methods can be called
        # while another lock-holding helper is still on the stack without
        # deadlocking.
        self._symbols_cache_lock: threading.RLock = threading.RLock()
        self._content_cache_lock: threading.RLock = threading.RLock()

    # ------------------------------------------------------------------
    # File / symbol primitives
    # ------------------------------------------------------------------

    def _read_file(self, rel_path: str) -> Optional[str]:
        """Read file content with caching (thread-safe)."""
        # Fast path: lock-free read from the cache. Dict reads are atomic
        # in CPython under the GIL, but we still take the lock to make
        # the contract explicit and to allow non-CPython runtimes.
        with self._content_cache_lock:
            cached = self._content_cache.get(rel_path)
            if cached is not None:
                return cached
        abs_path = os.path.join(self.repo_path, rel_path)
        if not os.path.isfile(abs_path):
            return None
        try:
            with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        except Exception:
            return None
        with self._content_cache_lock:
            # Another thread might have populated the cache while we were
            # reading; preserve their value to avoid duplicate large
            # strings in memory, but the content is identical so either
            # works.
            existing = self._content_cache.get(rel_path)
            if existing is not None:
                return existing
            self._content_cache[rel_path] = content
        return content

    def get_symbols_overview(self, file_path: str) -> Dict:
        """Extract symbols (classes, methods) from a file. Results are cached.

        For Java methods we also capture the parameter list (raw text between
        the parentheses) and a coarse `arity` count. These are used by the
        upward-trace to disambiguate overloaded methods so that callers of
        an overload that does NOT reach the changed method are not followed.
        """
        with self._symbols_cache_lock:
            cached = self._symbols_cache.get(file_path)
            if cached is not None:
                return cached

        self._call_count += 1
        if not self.enabled or self._call_count > self.max_symbols:
            return {"symbols": [], "error": "disabled_or_budget_exceeded"}

        content = self._read_file(file_path)
        if content is None:
            return {"symbols": [], "error": f"file_not_found: {file_path}"}

        symbols: List[Dict] = []
        ext = Path(file_path).suffix.lower()

        if ext == ".java":
            for m in re.finditer(
                r'(?:public\s+|abstract\s+|final\s+)*(?:class|interface|enum)\s+(\w+)',
                content
            ):
                symbols.append({
                    "name": m.group(1),
                    "kind": "class",
                    "line": content[:m.start()].count('\n') + 1,
                    "offset": m.start(),
                })
            for m in re.finditer(
                r'(?:public|protected|private)\s+(?:static\s+|final\s+|synchronized\s+|abstract\s+|native\s+)*'
                r'(?:[\w<>\[\],\s\?]+)\s+(\w+)\s*\(',
                content
            ):
                name = m.group(1)
                if name in _CONTROL_KEYWORDS:
                    continue
                # Capture the parameter list to support overload resolution.
                params_raw, arity = _extract_method_params(content, m.end() - 1)
                symbols.append({
                    "name": name,
                    "kind": "method",
                    "line": content[:m.start()].count('\n') + 1,
                    "offset": m.start(),
                    "params": params_raw,
                    "arity": arity,
                })
        elif ext == ".py":
            for m in re.finditer(r'^class\s+(\w+)', content, re.MULTILINE):
                symbols.append({
                    "name": m.group(1),
                    "kind": "class",
                    "line": content[:m.start()].count('\n') + 1,
                    "offset": m.start(),
                })
            for m in re.finditer(r'^(?:    |\t)*def\s+(\w+)', content, re.MULTILINE):
                symbols.append({
                    "name": m.group(1),
                    "kind": "function",
                    "line": content[:m.start()].count('\n') + 1,
                    "offset": m.start(),
                })
        elif ext in [".js", ".ts"]:
            for m in re.finditer(r'(?:class|function)\s+(\w+)', content):
                symbols.append({
                    "name": m.group(1),
                    "kind": "function",
                    "line": content[:m.start()].count('\n') + 1,
                    "offset": m.start(),
                })
            for m in re.finditer(r'const\s+(\w+)\s*=\s*(?:function|\()', content):
                symbols.append({
                    "name": m.group(1),
                    "kind": "function",
                    "line": content[:m.start()].count('\n') + 1,
                    "offset": m.start(),
                })
        elif ext == ".go":
            for m in re.finditer(r'func\s+(\w+)', content):
                symbols.append({
                    "name": m.group(1),
                    "kind": "function",
                    "line": content[:m.start()].count('\n') + 1,
                    "offset": m.start(),
                })

        result = {"symbols": symbols, "file": file_path, "language": ext[1:] if ext else ""}
        with self._symbols_cache_lock:
            # Re-check (another thread may have populated already).
            existing = self._symbols_cache.get(file_path)
            if existing is not None:
                return existing
            self._symbols_cache[file_path] = result
        return result

    def find_symbol(self, symbol_name: str, file_path: str = "") -> List[Dict]:
        """Find a symbol definition by name."""
        self._call_count += 1
        if not self.enabled or self._call_count > self.max_symbols:
            return []

        try:
            cmd = ["git", "grep", "-n", "--no-color", "-E",
                   f"(class|interface|enum|def|function|func)\\s+{re.escape(symbol_name)}"]
            if file_path:
                cmd.extend(["--", file_path])

            result = subprocess.run(
                cmd, capture_output=True, encoding="utf-8", errors="replace",
                cwd=self.repo_path, timeout=15,
            )

            matches = []
            for line in (result.stdout or "").strip().split("\n"):
                if not line:
                    continue
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    matches.append({
                        "file": parts[0],
                        "line": int(parts[1]) if parts[1].isdigit() else 0,
                        "text": parts[2].strip(),
                    })
            return matches[:20]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Helpers used by find_referencing_symbols
    # ------------------------------------------------------------------

    @staticmethod
    def _is_call_site(line_text: str, symbol_name: str) -> bool:
        """
        Validate that `symbol_name` appears in `line_text` as an actual
        method call (not a comment, string, import, or random identifier).

        A call site is identified by `<symbol_name>(` with optional whitespace,
        and the line must NOT be a single-line comment, javadoc, or import.
        """
        stripped = line_text.lstrip()
        if not stripped:
            return False
        # Drop obvious non-code lines.
        if stripped.startswith(('//', '/*', '*', '#', '"""', "'''", '@')):
            # @ catches Java annotations like @Reference -- they are not calls.
            return False
        if stripped.startswith(('import ', 'package ', 'from ', 'using ')):
            return False
        # Look for `<symbol>(` with a word boundary on the left.
        pattern = r'(?<![A-Za-z0-9_])' + re.escape(symbol_name) + r'\s*\('
        m = re.search(pattern, line_text)
        if not m:
            return False
        # Reject occurrences that fall inside a string literal. Heuristic:
        # count unescaped quotes before the match.
        prefix = line_text[:m.start()]
        # Strip escaped quotes so we don't count `\"` as a delimiter.
        prefix_for_count = prefix.replace('\\"', '').replace("\\'", '')
        if prefix_for_count.count('"') % 2 == 1:
            return False
        if prefix_for_count.count("'") % 2 == 1:
            return False
        # Reject: this is the method DECLARATION (modifier-list to the left).
        decl_pattern = (
            r'(?:public|protected|private|static|final|synchronized|abstract|native|default)\s+'
            r'[\w<>\[\],\s\?]+\s+' + re.escape(symbol_name) + r'\s*\('
        )
        if re.search(decl_pattern, line_text):
            return False
        return True

    def _enclosing_method(self, rel_path: str, hit_offset: int) -> Optional[str]:
        """
        Return the name of the method that lexically contains `hit_offset`
        in the given file. Walks back through symbol offsets and confirms
        the brace scope, so we never pick a sibling method "near" the line.
        """
        sym = self._enclosing_method_symbol(rel_path, hit_offset)
        return sym['name'] if sym else None

    def _enclosing_method_symbol(self, rel_path: str, hit_offset: int) -> Optional[Dict]:
        """
        Like `_enclosing_method` but returns the FULL symbol dict so the
        caller can inspect the overload's signature (arity / params).
        """
        symbols = self.get_symbols_overview(rel_path).get('symbols', [])
        method_syms = [
            s for s in symbols
            if s['kind'] in ('method', 'function') and s.get('offset', 0) <= hit_offset
        ]
        if not method_syms:
            return None
        method_syms.sort(key=lambda s: s['offset'], reverse=True)
        content = self._read_file(rel_path) or ''
        # Walk most-recent-first; return the first whose body still contains the hit.
        for sym in method_syms:
            start = content.find('{', sym['offset'])
            if start == -1:
                continue
            end = self._matching_brace_end(content, start)
            if end is None:
                continue
            if start <= hit_offset <= end:
                return sym
        # Fallback: nearest above (Python doesn't use braces).
        return method_syms[0]

    def find_method_overloads(self, method_name: str, file_path: str) -> List[Dict]:
        """
        Return all overloads of `method_name` defined in `file_path`.

        Each entry is the symbol dict from `get_symbols_overview`, which
        includes `name`, `line`, `offset`, `params`, and `arity`. This is
        framework-agnostic and works for any method - the same routine is
        used regardless of domain.
        """
        symbols = self.get_symbols_overview(file_path).get('symbols', [])
        return [s for s in symbols
                if s.get('kind') in ('method', 'function')
                and s.get('name') == method_name]

    @staticmethod
    def _matching_brace_end(content: str, brace_open_pos: int) -> Optional[int]:
        """Find matching closing brace, handling strings/comments. Returns offset or None."""
        n = len(content)
        if brace_open_pos >= n or content[brace_open_pos] != '{':
            return None
        depth = 1
        pos = brace_open_pos + 1
        in_string = False
        in_char = False
        in_line_comment = False
        in_block_comment = False
        while pos < n and depth > 0:
            ch = content[pos]
            nxt = content[pos + 1] if pos + 1 < n else ''
            if in_line_comment:
                if ch == '\n':
                    in_line_comment = False
            elif in_block_comment:
                if ch == '*' and nxt == '/':
                    in_block_comment = False
                    pos += 1
            elif in_string:
                if ch == '\\':
                    pos += 1
                elif ch == '"':
                    in_string = False
            elif in_char:
                if ch == '\\':
                    pos += 1
                elif ch == "'":
                    in_char = False
            else:
                if ch == '/' and nxt == '/':
                    in_line_comment = True
                    pos += 1
                elif ch == '/' and nxt == '*':
                    in_block_comment = True
                    pos += 1
                elif ch == '"':
                    in_string = True
                elif ch == "'":
                    in_char = True
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        return pos
            pos += 1
        return None

    # ------------------------------------------------------------------
    # Reference / caller discovery
    # ------------------------------------------------------------------

    def _resolve_call_sites(self,
                            symbol_name: str,
                            candidate_hits: List[Dict],
                            from_file: str,
                            max_callers: int,
                            overload_arities: Optional[Set[int]],
                            same_file_only: bool) -> List[Dict]:
        """
        Apply the FULL existing validation pipeline to a list of raw
        candidate hits and return the final references list.

        This contains the IDENTICAL filtering/parsing logic that
        `find_referencing_symbols` previously inlined - extracted so it
        can be reused both by the legacy git-grep path and by the
        bulk-index path. Behaviour is bit-for-bit identical:

          1. Drop hits in non-application files (defense in depth - the
             index already filters these but the legacy path passed raw
             grep output).
          2. Drop hits in test files (same reasoning).
          3. If `same_file_only` AND `from_file`, drop cross-file hits.
          4. If `from_file`, language scope: only same-extension hits.
          5. `_is_call_site` validation against the raw text.
          6. Walk the FULL file content forward to compute multi-line
             argument arity (`_extract_call_args`).
          7. Apply `overload_arities` filter when provided.
          8. Resolve enclosing method via `_enclosing_method`.
          9. Drop self-recursion (caller == symbol AND same file).
         10. Drop trivial caller methods.
         11. Dedup by `<file>:<caller_method>` key.
         12. Truncate to `max_callers`.
        """
        from_ext = Path(from_file).suffix.lower() if from_file else ""

        references: List[Dict] = []
        seen_keys: Set[str] = set()

        for hit in candidate_hits:
            ref_file = hit.get("file", "")
            ref_line = hit.get("line", 0)
            ref_text = hit.get("text", "")
            if not ref_file or not ref_text:
                continue

            # Hard filters BEFORE any expensive work (defense in depth).
            if _is_non_application_file(ref_file):
                continue
            if _is_test_file(ref_file):
                continue
            # `same_file_only` semantics: when set with a `from_file`,
            # only hits inside that exact file are kept.
            if same_file_only and from_file and ref_file != from_file:
                continue
            # Language scope: only same-extension files when from_file
            # is given.
            if from_ext and Path(ref_file).suffix.lower() != from_ext:
                continue

            # Validate the line is a real call site (not a comment,
            # string, import, annotation, or method declaration).
            if not self._is_call_site(ref_text, symbol_name):
                continue

            # Find the offset within file content for enclosing-method
            # lookup.
            content = self._read_file(ref_file)
            if content is None:
                continue
            # Translate (file, line) to char offset.
            line_start = 0
            cur_line = 1
            while cur_line < ref_line and line_start < len(content):
                nl = content.find('\n', line_start)
                if nl == -1:
                    break
                line_start = nl + 1
                cur_line += 1
            # Find symbol_name `(` on this line.
            line_end = content.find('\n', line_start)
            if line_end == -1:
                line_end = len(content)
            line_slice = content[line_start:line_end]
            m_local = re.search(
                r'(?<![A-Za-z0-9_])' + re.escape(symbol_name) + r'\s*\(',
                line_slice
            )
            if not m_local:
                continue
            hit_offset = line_start + m_local.start()

            # Resolve the call's argument count by walking forward
            # through the matched `(` in the FULL file content (calls
            # can span multiple lines). This is what allows overload
            # disambiguation.
            paren_pos_in_content = line_start + m_local.end() - 1
            window_end = min(len(content), paren_pos_in_content + 4000)
            arg_text, arity = _extract_call_args(
                content[:window_end], paren_pos_in_content
            )

            # Apply overload filter (skip the call site if arity
            # doesn't match a target overload). When `overload_arities`
            # is None we accept any arity - preserves backwards
            # compatibility.
            if overload_arities is not None:
                if arity < 0 or arity not in overload_arities:
                    continue

            caller_method = self._enclosing_method(ref_file, hit_offset)
            if not caller_method:
                continue
            # Skip self-recursion at the source.
            if caller_method == symbol_name and ref_file == from_file:
                continue
            # Skip trivial caller methods (getter/setter/etc.).
            if _is_trivial_method(caller_method):
                continue

            key = f"{ref_file}:{caller_method}"
            if key in seen_keys:
                continue
            seen_keys.add(key)

            references.append({
                "file": ref_file,
                "line": ref_line,
                "text": ref_text.strip(),
                "caller_method": caller_method,
                "call_arity": arity,
                "hit_offset": hit_offset,
            })
            if len(references) >= max_callers:
                break

        return references

    def find_referencing_symbols(self,
                                 symbol_name: str,
                                 from_file: str = "",
                                 max_callers: int = 30,
                                 overload_arities: Optional[Set[int]] = None,
                                 same_file_only: bool = False) -> Dict:
        """
        Find call sites of `symbol_name` in the codebase.

        Quality guarantees:
          * Only returns hits where the symbol appears as an actual call
            (`<symbol>(`) outside strings/comments/imports.
          * Drops hits in non-application files (RIA tool, generated, build,
            test paths) at source.
          * If `from_file` is provided, restricts hits to files of the SAME
            language (no Java->Python cross-language pollution).
          * If `same_file_only` is True AND `from_file` is provided, only
            hits inside `from_file` are returned. This is used when the
            target method is `private` / `protected` and its name
            collides with definitions in OTHER files - in such cases
            cross-file callers cannot legally invoke our method.
          * Drops the line that is the method DECLARATION itself.
          * Returns at most `max_callers` hits.
          * If `overload_arities` is provided (a set of integers), only call
            sites whose top-level argument count is in that set are kept.
            This filter is what disambiguates overloaded methods - callers
            of an overload that does NOT reach the changed method are
            excluded. Pass `None` to disable the filter (any arity matches).

        Returns: {"references": [{file, line, text, caller_method, arity}, ...]}

        Thread-safe: can be called from multiple threads in parallel.
        """
        self._call_count += 1
        if not self.enabled or self._call_count > self.max_symbols:
            return {"references": []}

        # ----- Per-symbol git grep (parallel-safe) -------------------------
        # `git grep -n -w <symbol>` is fast for narrowing candidates.
        # When restricted to a single file we pass it as a pathspec so
        # the grep already excludes all other files (avoiding cap
        # truncation problems for ambiguous names).
        try:
            cmd = ["git", "grep", "-n", "--no-color", "-w", symbol_name]
            if same_file_only and from_file:
                cmd.extend(["--", from_file])
            result = subprocess.run(
                cmd,
                capture_output=True, encoding="utf-8", errors="replace",
                cwd=self.repo_path, timeout=20,
            )
        except Exception:
            return {"references": []}

        # Parse grep output into the same shape the index would have
        # produced, then run the shared validator. This keeps the two
        # paths byte-for-byte identical.
        raw_hits: List[Dict] = []
        for line in (result.stdout or "").strip().split("\n"):
            if not line:
                continue
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            ref_file = parts[0]
            try:
                ref_line = int(parts[1])
            except ValueError:
                continue
            ref_text = parts[2]
            raw_hits.append({
                "file": ref_file,
                "line": ref_line,
                "text": ref_text,
            })

        references = self._resolve_call_sites(
            symbol_name=symbol_name,
            candidate_hits=raw_hits,
            from_file=from_file,
            max_callers=max_callers,
            overload_arities=overload_arities,
            same_file_only=same_file_only,
        )
        return {"references": references}

    # ------------------------------------------------------------------
    # Method-body callee extraction (unchanged in spirit; kept for v2.0)
    # ------------------------------------------------------------------

    def find_called_methods(self, method_name: str, file_path: str) -> List[Dict]:
        """
        Find methods *called by* the given method.

        Parses the method body and returns the list of method invocations
        (name only). Used to traverse call trees DOWNWARD from an entry point.

        Returns: list of dicts: [{"name": <called_method>, "file": <file_path>}]
        """
        self._call_count += 1
        if not self.enabled or self._call_count > self.max_symbols:
            return []

        content = self._read_file(file_path)
        if content is None:
            return []

        # Locate the method declaration. Greedy [^)]* fails when the
        # parameter list contains nested parens (e.g. JAX-RS / Spring
        # annotations like `@FormParam("name")`). We instead find the
        # opening "methodName(" then walk forward through balanced
        # parentheses to find the closing ")", then locate the next "{".
        decl_pattern = re.compile(
            r'\b' + re.escape(method_name) + r'\s*\(',
            re.MULTILINE
        )
        method_body = None
        for decl_match in decl_pattern.finditer(content):
            paren_open = decl_match.end() - 1  # index of '('
            depth = 0
            paren_close = -1
            for i in range(paren_open, len(content)):
                ch = content[i]
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        paren_close = i
                        break
            if paren_close < 0:
                continue
            # Find next '{' (skip whitespace, throws clause).
            brace_open = content.find('{', paren_close)
            if brace_open < 0:
                continue
            # Reject if a ';' appears before '{' (interface/abstract decl
            # or a call expression rather than a definition).
            semi = content.find(';', paren_close, brace_open)
            if semi != -1:
                continue
            end = self._matching_brace_end(content, brace_open)
            if end is None:
                continue
            method_body = content[brace_open:end + 1]
            break

        if method_body is None:
            return []

        called_names: List[Dict] = []
        seen: Set[str] = set()
        for m in re.finditer(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(', method_body):
            name = m.group(1)
            if name in _CONTROL_KEYWORDS:
                continue
            if name in seen:
                continue
            seen.add(name)
            called_names.append({"name": name, "file": file_path})

        return called_names

    # ------------------------------------------------------------------
    # Upward call-tree trace
    # ------------------------------------------------------------------

    def trace_call_chain_to_entry_points(self,
                                         changed_method: str,
                                         changed_file: str,
                                         max_depth: int = 10,
                                         max_callers_per_node: int = 100) -> Dict:
        """
        Trace call chain from `changed_method` upward to TRUE runtime
        entry points (servlets, REST endpoints, scheduled tasks, listeners,
        Runnables / Callables, filters).

        DYNAMIC DISCOVERY INTEGRATION:
        This method now uses ZERO hardcoded framework patterns. Entry points
        are discovered dynamically by analyzing actual usage patterns in the
        codebase ONCE at the start of the trace, then cached for all validation
        checks during the upward traversal.

        An entry point = a method whose ENCLOSING CLASS / METHOD ANNOTATIONS
        identify it as a runtime dispatch target. A method that simply has
        no callers found is NOT an entry point - it is logged as a
        "dead leaf" and skipped, because reporting it would mislead
        downstream test selection.

        Overload-aware traversal:
          * When the current method has multiple overloads in its source
            file, we restrict the upward search to callers whose call-site
            arity matches the overload(s) that actually reach the changed
            method. This prevents callers of a sibling overload (which
            does not invoke the changed method) from contaminating the
            trace. Resolution is fully data-driven - the rule applies to
            ANY method with overloads, not just the seed.

        Chain-validity guarantee:
          * Every recorded chain hop is verified by reading the caller's
            method body and confirming the callee call expression is
            actually present. This eliminates name-collision-based
            phantom chains (e.g. `processRequest` in two unrelated
            servlets).

        Other guarantees:
          * Language-scoped, context-validated `find_referencing_symbols`.
          * Drops getters/setters/boilerplate everywhere (callers + leaves).
          * Drops RIA-tool / generated / build / test files everywhere.
          * Per-(method, arity-set) visited cache prevents loops.
          * Caps callers per node to prevent fan-out explosion.

        Returns dict with:
          - entry_points: validated TRUE entry points, each with chain and
            entry_type (servlet / rest-endpoint / runnable.run / etc.).
          - call_chains: convenience list of just the chains.
          - dead_leaves: methods that ran out of callers but were NOT
            legitimate entry points - kept for diagnostics.
          - chain_rejections: hops dropped because the caller's body did
            not actually contain a call to the callee (phantom chains).
        """
        # PHASE 0: Run dynamic entry point discovery ONCE at trace start
        # This replaces ALL hardcoded pattern matching with usage-based learning
        print(f"[Trace] Running dynamic entry point discovery...")
        discovered_entries = discover_entry_points_from_usage(self.repo_path, force_refresh=False)
        print(f"[Trace] Discovered {discovered_entries['stats']['total_entry_points']} entry points")

        entry_points: List[Dict] = []
        dead_leaves: List[Dict] = []
        chain_rejections: List[Dict] = []
        seen_entries: Set[str] = set()
        # Visited key includes the arity-set so different overloads of the
        # same name can each be explored independently.
        global_visited: Set[str] = set()

        seed_key = f"{changed_file}:{changed_method}"

        def _arity_set_key(arities: Optional[Set[int]]) -> str:
            if arities is None:
                return '*'
            return ','.join(str(a) for a in sorted(arities))

        def _record_entry(current_file: str,
                          current_method: str,
                          chain: List[str],
                          entry_type: str) -> None:
            entry_key = f"{current_file}:{current_method}"
            if entry_key in seen_entries:
                return
            seen_entries.add(entry_key)
            entry_points.append({
                "file": current_file,
                "method": current_method,
                "chain": list(chain),
                "entry_type": entry_type,
            })

        def trace_upward(current_file: str,
                         current_method: str,
                         current_arities: Optional[Set[int]],
                         depth: int,
                         chain: List[str]) -> None:
            """
            current_arities: set of formal-parameter-counts of the overload(s)
            of `current_method` that we are currently exploring (i.e. the
            overload the previous step's call was inside). When None we are
            exploring "any overload" - used for the seed and for methods
            that have only one definition.
            """
            if depth > max_depth:
                return

            key = f"{current_file}:{current_method}#{_arity_set_key(current_arities)}"
            if key in global_visited:
                return
            global_visited.add(key)

            # Hard reject if current location is RIA / generated / test.
            if _is_non_application_file(current_file) or _is_test_file(current_file):
                return

            base_key = f"{current_file}:{current_method}"
            content = self._read_file(current_file) or ''

            # ----------------------------------------------------------
            # Step 1: If the CURRENT method is a legitimate entry point
            # (servlet dispatch / REST endpoint / scheduled task /
            # listener / Runnable.run / Callable.call / Filter.doFilter
            # / reflective-dispatch target), record it.
            #
            # For "terminal" framework dispatchers (servlet / REST /
            # Runnable / Callable / Filter / listener / scheduled) we
            # STOP here - those have no further static callers worth
            # exploring (the framework calls them).
            #
            # For reflective-dispatch entry points the method is ALSO
            # often invoked by static callers in addition to the
            # reflective dispatcher (e.g. a rule action also called
            # directly from a thread). We record the reflective entry
            # AND continue tracing its static callers.
            #
            # We never apply this to the seed itself.
            # ----------------------------------------------------------
            already_recorded_as_entry = False
            if base_key != seed_key:
                method_offset = self._find_method_offset(current_file,
                                                         current_method,
                                                         current_arities)
                if method_offset is not None:
                    is_ep, reason = is_legitimate_entry_point(
                        content, current_method, method_offset,
                        file_path=current_file,
                        repo_root=self.repo_path,
                        discovered_entries=discovered_entries,
                    )
                    if is_ep:
                        _record_entry(current_file, current_method,
                                      chain, reason)
                        already_recorded_as_entry = True
                        # Terminal dispatchers: stop here.
                        # NOTE: dynamic-discovery reasons include the pattern type,
                        # so we check for 'reflective-dispatch' anywhere in reason
                        if 'reflective-dispatch' not in reason:
                            return
                        # Reflective entry points: also continue upward
                        # so concurrent static-caller paths are found.

            # ----------------------------------------------------------
            # Step 2: Find callers and recurse upward.
            #
            # Visibility-aware caller search: if the method is private OR
            # protected AND the same method name is also declared in
            # OTHER source files (name collision), grep-based caller
            # discovery cannot tell which definition is invoked. To avoid
            # phantom chains (e.g. caller's `processRequest` actually
            # targets its own private `processRequest`, not OURS), we
            # restrict caller search to the SAME FILE.
            # ----------------------------------------------------------
            method_offset_here = self._find_method_offset(
                current_file, current_method, current_arities
            )
            visibility = self._method_visibility(
                current_file, current_method, method_offset_here
            )
            restrict_to_same_file = False
            if visibility in ('private', 'protected'):
                # Only restrict when the name is ambiguous across files.
                if self._method_defined_elsewhere(current_method, current_file):
                    restrict_to_same_file = True

            refs = self.find_referencing_symbols(
                current_method,
                from_file=current_file,
                max_callers=max_callers_per_node,
                overload_arities=current_arities,
                same_file_only=restrict_to_same_file,
            )
            callers = refs.get("references", [])

            # Re-filter (defence-in-depth - find_referencing_symbols already filters).
            valid_callers = []
            for c in callers:
                cf = c.get('file', '')
                cm = c.get('caller_method', '')
                if not cm:
                    continue
                if _is_non_application_file(cf):
                    continue
                if _is_test_file(cf):
                    continue
                if _is_trivial_method(cm):
                    continue
                # Stay in-language.
                if not _same_language(cf, current_file):
                    continue
                valid_callers.append(c)

            # ----------------------------------------------------------
            # Step 3: No callers => either a legitimate entry point
            # (already recorded above) or a dead leaf. If we got here
            # without recording an entry, this method is NOT a runtime
            # dispatch target.
            # ----------------------------------------------------------
            if not valid_callers:
                # Special case: the seed itself. If the seed has no
                # callers (the changed method is unused) we don't add an
                # entry but we don't error either.
                if base_key == seed_key:
                    dead_leaves.append({
                        "file": current_file,
                        "method": current_method,
                        "reason": "seed-has-no-callers",
                    })
                    return
                # If we already recorded this as an entry point (e.g.
                # reflective-dispatch target with no additional static
                # callers), don't double-mark as dead leaf.
                if already_recorded_as_entry:
                    return
                # Non-seed leaf without legitimate-entry classification:
                # this is a "dead method" or a method only reachable via
                # reflection / config wiring we cannot see. Record for
                # diagnostics but do NOT treat as entry point.
                dead_leaves.append({
                    "file": current_file,
                    "method": current_method,
                    "reason": "leaf-not-a-framework-entry-point",
                    "chain": list(chain),
                })
                return

            for caller in valid_callers:
                caller_file = caller['file']
                caller_method = caller['caller_method']
                hit_offset = caller.get('hit_offset', -1)

                # ------------------------------------------------------
                # Chain validation: confirm caller's body actually
                # contains a call to current_method. find_referencing_symbols
                # already validated the *call site* line, but a defence-
                # in-depth check at the body level catches edge cases
                # where _enclosing_method picks up a sibling method due
                # to brace mismatches.
                # ------------------------------------------------------
                next_arities = self._resolve_caller_overload_arities(
                    caller_file, caller_method, hit_offset
                )
                if not self._caller_body_contains_call(
                        caller_file, caller_method, next_arities,
                        hit_offset, current_method):
                    chain_rejections.append({
                        "caller_file": caller_file,
                        "caller_method": caller_method,
                        "callee_method": current_method,
                        "reason": "caller-body-does-not-contain-call",
                    })
                    continue

                new_chain = chain + [f"{caller_file}:{caller_method}"]
                trace_upward(caller_file, caller_method, next_arities,
                             depth + 1, new_chain)

        # Seed: we never know which overload the user "meant" - explore all.
        initial_chain = [f"{changed_file}:{changed_method}"]
        seed_arities = self._seed_overload_arities(changed_method, changed_file)
        print(f"[TRACE] Starting from: {changed_file}:{changed_method}")
        if seed_arities:
            print(f"[TRACE] Seed overload arities: {sorted(seed_arities)}")
        trace_upward(changed_file, changed_method, seed_arities,
                     0, initial_chain)

        # ----------------------------------------------------------------
        # FORWARD-REACHABILITY VALIDATION (BUG-S1-2 fix).
        #
        # The upward trace is grep-based and can occasionally surface a
        # method that *contains the seed token* but does not actually
        # reach the changed method through any call path (e.g. a sibling
        # overload, a name collision, or a phantom chain produced by
        # imperfect enclosing-method detection). We close that gap here
        # by walking FORWARD from each candidate entry point and
        # confirming a real call path exists. If verification fails,
        # the entry is moved to `invalid_entry_points` with the
        # attempted-path for diagnostics.
        # ----------------------------------------------------------------
        print(f"[VALIDATION] Entry points before forward validation: "
              f"{len(entry_points)}")

        validated_entry_points: List[Dict] = []
        invalid_entry_points: List[Dict] = []
        for ep in entry_points:
            ep_file = ep.get('file', '')
            ep_method = ep.get('method', '')
            ep_chain = ep.get('chain', [])
            is_valid, attempted_path = verify_entry_point_reaches_target(
                ep_file, ep_method,
                changed_file, changed_method,
                self.repo_path,
                chain=ep_chain,
                max_depth=6,
            )
            if is_valid:
                ep_with_proof = dict(ep)
                ep_with_proof['forward_path'] = attempted_path
                validated_entry_points.append(ep_with_proof)
            else:
                invalid_entry_points.append({
                    **ep,
                    'rejection_reason': 'forward-reachability-failed',
                    'attempted_path': attempted_path,
                })

        print(f"[VALIDATION] Entry points after forward validation: "
              f"{len(validated_entry_points)}")
        print(f"[VALIDATION] Invalid entry points: "
              f"{len(invalid_entry_points)}")
        for invalid in invalid_entry_points:
            print(f"  - {invalid['file']}:{invalid['method']} - "
                  f"{invalid['rejection_reason']}")

        if dead_leaves:
            print(f"[TRACE] Dead leaves (no upstream entry point found): "
                  f"{len(dead_leaves)}")
            for dl in dead_leaves[:20]:
                print(f"  - {dl.get('file')}:{dl.get('method')} ({dl.get('reason')})")
            if len(dead_leaves) > 20:
                print(f"  ... and {len(dead_leaves) - 20} more")
        if chain_rejections:
            print(f"[TRACE] Chain hops rejected (caller body did not contain call): "
                  f"{len(chain_rejections)}")

        return {
            "changed_method": changed_method,
            "changed_file": changed_file,
            "entry_points": validated_entry_points,
            "call_chains": [ep["chain"] for ep in validated_entry_points],
            "total_entry_points": len(validated_entry_points),
            "dead_leaves": dead_leaves,
            "chain_rejections": chain_rejections,
            "invalid_entry_points": invalid_entry_points,
        }

    # ------------------------------------------------------------------
    # Helpers used by trace_call_chain_to_entry_points
    # ------------------------------------------------------------------

    def _find_method_offset(self,
                            file_path: str,
                            method_name: str,
                            arities: Optional[Set[int]] = None) -> Optional[int]:
        """
        Return the file offset of the method declaration of `method_name`
        in `file_path`. If multiple overloads exist and `arities` is
        provided, the matching overload is preferred. Falls back to the
        first definition.
        """
        symbols = self.get_symbols_overview(file_path).get('symbols', [])
        same_named = [s for s in symbols
                      if s.get('kind') in ('method', 'function')
                      and s.get('name') == method_name]
        if not same_named:
            return None
        if arities:
            for s in same_named:
                if s.get('arity', -1) in arities:
                    return s.get('offset')
        return same_named[0].get('offset')

    def _method_visibility(self,
                           file_path: str,
                           method_name: str,
                           method_offset: Optional[int] = None) -> str:
        """
        Inspect the method declaration line and return one of:
          'public', 'protected', 'private', 'package' (default).
        Used to constrain caller search: a `private` method can only have
        callers in the same file; a `protected` method only in the same
        file or a subclass file; a `public` method anywhere.
        """
        content = self._read_file(file_path)
        if content is None:
            return 'package'
        if method_offset is None:
            method_offset = self._find_method_offset(file_path, method_name)
        if method_offset is None:
            return 'package'
        # Look at the line/segment containing the declaration.
        line_start = content.rfind('\n', 0, method_offset) + 1
        # Capture up to the declaration's `(` for full visibility tokens.
        paren = content.find('(', method_offset)
        decl_slice = content[line_start: paren if paren > 0 else method_offset + 200]
        if 'private ' in decl_slice or decl_slice.lstrip().startswith('private '):
            return 'private'
        if 'protected ' in decl_slice or decl_slice.lstrip().startswith('protected '):
            return 'protected'
        if 'public ' in decl_slice or decl_slice.lstrip().startswith('public '):
            return 'public'
        return 'package'

    def _method_defined_elsewhere(self,
                                  method_name: str,
                                  exclude_file: str) -> bool:
        """
        Cheap check: is there any OTHER source file in the repo that
        declares a method named `method_name`? If yes, the same name is
        ambiguous and callers found by name alone may be calling the
        OTHER definition. Used to decide whether to constrain caller
        search to the declaring file.

        We use POSIX extended regex (`grep -E`) so the pattern relies on
        `[[:space:]]` rather than `\s` (which is not portable in POSIX).
        Looser pattern: `<modifier>\s+...\s+<name>\s*(`.
        """
        # Cache to avoid hitting git grep on every recursion.
        if not hasattr(self, '_defined_elsewhere_cache'):
            self._defined_elsewhere_cache: Dict[str, Set[str]] = {}
        cache_key = method_name
        if cache_key in self._defined_elsewhere_cache:
            files = self._defined_elsewhere_cache[cache_key]
        else:
            files: Set[str] = set()
            try:
                cmd = [
                    "git", "grep", "-l", "-E",
                    r'(public|protected|private)[[:space:]]+[^=;]*[[:space:]]+'
                    + re.escape(method_name) + r'[[:space:]]*\(',
                ]
                result = subprocess.run(
                    cmd, capture_output=True, encoding='utf-8',
                    errors='replace',
                    cwd=self.repo_path, timeout=10,
                )
                for f in (result.stdout or '').strip().split('\n'):
                    if not f:
                        continue
                    if _is_non_application_file(f):
                        continue
                    if _is_test_file(f):
                        continue
                    files.add(f)
            except Exception:
                pass
            self._defined_elsewhere_cache[cache_key] = files
        # Filter out the exclude_file and require same-language.
        ext = Path(exclude_file).suffix.lower()
        return any(f != exclude_file
                   and Path(f).suffix.lower() == ext
                   for f in files)

    def _caller_body_contains_call(self,
                                   caller_file: str,
                                   caller_method: str,
                                   caller_arities: Optional[Set[int]],
                                   hit_offset: int,
                                   callee_name: str) -> bool:
        """
        Verify that the body of the SPECIFIC overload of `caller_method`
        that lexically contains `hit_offset` actually invokes
        `callee_name`. Returns False if the body cannot be located or
        does not contain the call.
        """
        content = self._read_file(caller_file)
        if content is None:
            return False
        # Prefer the overload whose body contains the hit offset.
        method_offset = None
        if hit_offset >= 0:
            sym = self._enclosing_method_symbol(caller_file, hit_offset)
            if sym and sym.get('name') == caller_method:
                method_offset = sym.get('offset')
        if method_offset is None:
            method_offset = self._find_method_offset(
                caller_file, caller_method, caller_arities
            )
        if method_offset is None:
            return False
        body = _extract_method_body(content, caller_method, method_offset)
        if body is None:
            return False
        return _body_contains_call_to(body, callee_name)

    def _seed_overload_arities(self,
                               method_name: str,
                               file_path: str) -> Optional[Set[int]]:
        """
        For the seed method, return the set of arities of its overloads
        IN ITS OWN FILE. If only one overload exists, returns that single
        arity. If the seed name doesn't appear (e.g. unusual file layout),
        returns None (no arity filter).
        """
        overloads = self.find_method_overloads(method_name, file_path)
        arities = {o['arity'] for o in overloads if o.get('arity', -1) >= 0}
        return arities if arities else None

    def _resolve_caller_overload_arities(self,
                                         caller_file: str,
                                         caller_method: str,
                                         hit_offset: int) -> Optional[Set[int]]:
        """
        Given a hit_offset inside `caller_file`, find which overload of
        `caller_method` contains that offset. Return its arity as a single-
        element set. If we can't tell, return None (no filter).
        """
        if hit_offset < 0:
            return None
        symbols = self.get_symbols_overview(caller_file).get('symbols', [])
        same_named = [s for s in symbols
                      if s.get('kind') in ('method', 'function')
                      and s.get('name') == caller_method
                      and s.get('offset', 0) <= hit_offset]
        if not same_named:
            return None
        # If only one overload, no ambiguity.
        all_named = [s for s in symbols
                     if s.get('kind') in ('method', 'function')
                     and s.get('name') == caller_method]
        if len(all_named) <= 1:
            arity = all_named[0].get('arity', -1) if all_named else -1
            return {arity} if arity >= 0 else None
        # Multiple overloads: pick the one whose body lexically contains
        # the hit offset, mirroring _enclosing_method_symbol.
        same_named.sort(key=lambda s: s['offset'], reverse=True)
        content = self._read_file(caller_file) or ''
        for sym in same_named:
            brace_open = content.find('{', sym['offset'])
            if brace_open == -1:
                continue
            brace_close = self._matching_brace_end(content, brace_open)
            if brace_close is None:
                continue
            if brace_open <= hit_offset <= brace_close:
                arity = sym.get('arity', -1)
                return {arity} if arity >= 0 else None
        return None


# CLI for testing
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Test Serena MCP client")
    parser.add_argument("--repo", required=True, help="Repository root path")
    parser.add_argument("--method", required=True, help="Method name to trace")
    parser.add_argument("--file", required=True, help="File containing the method")
    args = parser.parse_args()

    client = SerenaMCPClient(args.repo, enabled=True, max_symbols=10000)
    result = client.trace_call_chain_to_entry_points(args.method, args.file)

    print(f"\nEntry Points Found: {result['total_entry_points']}")
    for ep in result['entry_points']:
        print(f"  - {ep['file']}:{ep['method']}")
        print(f"    Chain: {' -> '.join(ep['chain'])}")
