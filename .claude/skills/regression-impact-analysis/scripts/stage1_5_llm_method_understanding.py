#!/usr/bin/env python3
"""
Stage 1.5 — LLM Method Understanding

Reads the FULL body of each changed method (not just diff lines),
sends it to an LLM, and produces:
  - What the method does as a whole
  - What the code change affects
  - Test scenarios to validate

Input:  .detect_changes_cache.json (from Stage 1)
Output: method_understanding.json  (consumed by Stage 7 + Report)
"""
import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR / '..' / 'configs'))

import agent_reasoning      # noqa: E402 (Copilot agent answers directly; no mailbox)
import ria_config           # noqa: E402

SYSTEM_PROMPT_TEMPLATE = """You are a senior {language} code analyst doing surgical change analysis.

You will receive:
1. The git diff showing EXACTLY what changed (BEFORE and AFTER for each hunk).
2. The changed lines in context within the method body.
3. The call-chain showing how this method is reached from an entry point.

CRITICAL RULES:
- Focus on the EXACT variables and conditions that were modified in the diff.
- If the change adds a guard like "x != 0 &&" to a compound if-condition,
  the change affects ONLY the clause containing that variable — NOT adjacent
  clauses in the same if-statement (e.g., splitShiftGap checks vs dayShiftGap checks).
- List the EXACT variable names from the diff as controlling_parameters.
- Each test scenario must be tied to the SPECIFIC variable/condition that changed,
  NOT to other conditions in the same if-statement.
- Use business language in scenario descriptions, not code jargon.

Respond ONLY with valid JSON — no markdown fences, no commentary."""


_LANGUAGE_MAP = {
    '.java': 'Java', '.kt': 'Kotlin',
    '.py': 'Python',
    '.ts': 'TypeScript', '.tsx': 'TypeScript/React',
    '.js': 'JavaScript', '.jsx': 'JavaScript/React', '.mjs': 'JavaScript',
    '.go': 'Go',
}


def _detect_language(file_path: str) -> str:
    """Auto-detect language from file extension."""
    ext = os.path.splitext(file_path)[1].lower()
    return _LANGUAGE_MAP.get(ext, 'source code')


def _extract_method_body(file_path: str, line_start: int, line_end: int, repo_root: str) -> str:
    """Read the method source code from the source file."""
    full_path = os.path.join(repo_root, file_path)
    if not os.path.exists(full_path):
        return ""
    with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
    # line_start and line_end are 1-indexed
    start = max(0, line_start - 1)
    end = min(len(lines), line_end)
    return ''.join(lines[start:end])


def _resolve_method_line_range(file_path: str, method_name: str, repo_root: str):
    """
    Best-effort resolution of a method's 1-based (line_start, line_end) by
    scanning the source file for the declaration and brace-matching the body.

    Returns (None, None) when the file is unreadable or the method cannot be
    located. Used as a fallback when explicit/CLI-driven runs carry null line
    numbers in the change cache.
    """
    import re as _re
    if not file_path or not method_name:
        return (None, None)
    full_path = file_path if os.path.isabs(file_path) else os.path.join(repo_root, file_path)
    if not os.path.exists(full_path):
        return (None, None)
    try:
        with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except Exception:
        return (None, None)

    decl_re = _re.compile(r'\b' + _re.escape(method_name) + r'\s*\(')
    start_idx = None
    for i, line in enumerate(lines):
        if decl_re.search(line):
            start_idx = i
            break
    if start_idx is None:
        return (None, None)

    depth = 0
    seen_open = False
    end_idx = None
    for j in range(start_idx, len(lines)):
        for ch in lines[j]:
            if ch == '{':
                depth += 1
                seen_open = True
            elif ch == '}':
                depth -= 1
                if seen_open and depth == 0:
                    end_idx = j
                    break
        if end_idx is not None:
            break

    if end_idx is None:
        return (start_idx + 1, start_idx + 1)
    return (start_idx + 1, end_idx + 1)


def _extract_changed_lines_context(file_path: str, changed_lines: list, repo_root: str) -> str:
    """Extract the changed lines with 3 lines of context around each."""
    full_path = os.path.join(repo_root, file_path)
    if not os.path.exists(full_path):
        return ""
    with open(full_path, 'r', encoding='utf-8', errors='replace') as f:
        all_lines = f.readlines()

    snippets = []
    seen = set()
    for ln in sorted(changed_lines):
        start = max(0, ln - 4)  # 3 lines before
        end = min(len(all_lines), ln + 3)  # 3 lines after (ln is 1-indexed)
        for i in range(start, end):
            if i not in seen:
                seen.add(i)
                marker = " >>>" if (i + 1) in changed_lines else "    "
                snippets.append(f"{marker} L{i+1}: {all_lines[i].rstrip()}")
    return '\n'.join(snippets)


def _extract_git_diff(file_path: str, repo_root: str) -> str:
    """Extract the actual git diff for this file (before/after hunks)."""
    import subprocess
    try:
        result = subprocess.run(
            ['git', 'diff', 'HEAD', '--', file_path],
            capture_output=True, text=True, cwd=repo_root, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        # Try unstaged
        result = subprocess.run(
            ['git', 'diff', '--', file_path],
            capture_output=True, text=True, cwd=repo_root, timeout=10
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _extract_diff_hunks_for_method(diff_text: str, line_start: int, line_end: int) -> str:
    """Extract only the diff hunks that overlap with the method's line range."""
    if not diff_text:
        return "(git diff not available)"
    hunks = []
    current_hunk = []
    in_hunk = False
    hunk_start = 0

    for line in diff_text.split('\n'):
        if line.startswith('@@'):
            # Parse hunk header like @@ -1398,8 +1398,8 @@
            import re
            match = re.search(r'\+(\d+)', line)
            if match:
                hunk_start = int(match.group(1))
                # Check if this hunk overlaps with our method
                if current_hunk and in_hunk:
                    hunks.append('\n'.join(current_hunk))
                current_hunk = [line]
                in_hunk = (hunk_start >= line_start - 10 and hunk_start <= line_end + 10)
        elif in_hunk:
            current_hunk.append(line)

    if current_hunk and in_hunk:
        hunks.append('\n'.join(current_hunk))

    return '\n\n'.join(hunks) if hunks else "(no diff hunks in method range)"


def _build_call_chain_text(call_chains: list, method_name: str) -> str:
    """Find call chains that include this method and format them."""
    matching = []
    for chain in call_chains:
        if isinstance(chain, list):
            # Each item is a string like "path/File.java:methodName"
            names = []
            for item in chain:
                if isinstance(item, str):
                    # Extract method name from "file:method" format
                    parts = item.split(':')
                    name = parts[-1] if len(parts) > 1 else item
                    names.append(name)
                elif isinstance(item, dict):
                    names.append(item.get('method_name', item.get('name', str(item))))
            if any(method_name.lower() in n.lower() for n in names):
                matching.append(' → '.join(names))
        elif isinstance(chain, str):
            if method_name.lower() in chain.lower():
                matching.append(chain)
    if matching:
        return '\n'.join(matching)
    return "(call-chain not available)"


def _deduplicate_methods(methods: list) -> list:
    """Group methods by unique (method_name) and merge changed_lines."""
    unique = {}
    for m in methods:
        name = m['method_name']
        if name not in unique:
            unique[name] = {
                'method_name': name,
                'instances': [],
                'all_changed_lines': [],
            }
        unique[name]['instances'].append(m)
        unique[name]['all_changed_lines'].extend(m.get('changed_lines', []))
    return list(unique.values())


def run(repo_root: str = '.'):
    """Run Stage 1.5 — Method Understanding.

    Reasoning is performed by the Copilot agent. On the first pass this stage
    extracts the deterministic context (method body, git diff, call chains) for
    every changed method and writes a *pending baseline* to
    ``method_understanding.json`` with empty reasoning fields, then prints an
    AGENT ACTION REQUIRED banner. Once the agent fills the reasoning fields and
    sets ``_reasoning_source: copilot-agent``, subsequent runs preserve that
    answer (skip-guard below) and never regenerate it.
    """
    output_dir = os.path.join(repo_root, ria_config.RIA_OUTPUT_DIR)
    cache_path = os.path.join(output_dir, '.detect_changes_cache.json')

    out_path = os.path.join(output_dir, 'method_understanding.json')
    # Skip-guard: preserve the agent's answer across re-runs.
    if agent_reasoning.agent_provided(out_path):
        existing = agent_reasoning.load_json(out_path)
        n = sum(1 for m in (existing.get('methods') or []) if not m.get('error'))
        print(f"[Stage 1.5] Using Copilot-provided method understanding "
              f"({n} method(s)) — skipping regeneration")
        return existing

    if not os.path.exists(cache_path):
        print("[Stage 1.5] ERROR: .detect_changes_cache.json not found. Run Stage 1 first.")
        return None

    with open(cache_path, 'r') as f:
        cache = json.load(f)

    # Parse methods from changed_files[].changed_methods[] structure
    all_methods = []
    call_chains = []

    if isinstance(cache, dict):
        # Primary structure: changed_files -> changed_methods (with file_path on parent)
        for cf in cache.get('changed_files', []):
            file_path = cf.get('file_path', '')
            for m in cf.get('changed_methods', []):
                method = dict(m)
                method['file_path'] = file_path
                all_methods.append(method)

        # Fallback: try direct methods list
        if not all_methods:
            for key in ['methods', 'changed_methods']:
                if key in cache:
                    items = cache[key]
                    if isinstance(items, list):
                        all_methods = items
                    break

    # Load call chains from stage1_entry_points.json
    stage1_path = os.path.join(output_dir, 'stage1_entry_points.json')
    if os.path.exists(stage1_path):
        with open(stage1_path, 'r') as f:
            stage1 = json.load(f)
        call_chains = stage1.get('call_chains', stage1.get('call_trees', []))

    if not all_methods:
        print("[Stage 1.5] No changed methods found in cache.")
        return None

    # Deduplicate by method name
    unique_methods = _deduplicate_methods(all_methods)
    print(f"[Stage 1.5] Found {len(all_methods)} method instances → {len(unique_methods)} unique methods")

    results = []

    # Cache file reads: when multiple methods live in the same file, avoid
    # reading the file once per method. Lock-protected for thread safety
    # because methods are now analyzed concurrently.
    _file_cache: dict = {}
    _file_cache_lock = threading.Lock()

    def _cached_read(file_path: str) -> list:
        with _file_cache_lock:
            if file_path in _file_cache:
                return _file_cache[file_path]
        # Read the file outside the lock so a slow disk read does not block
        # other threads — but write back under the lock.
        full_path = os.path.join(repo_root, file_path)
        if not os.path.exists(full_path):
            with _file_cache_lock:
                _file_cache[file_path] = []
            return []
        with open(full_path, 'r', encoding='utf-8', errors='replace') as fh:
            data = fh.readlines()
        with _file_cache_lock:
            _file_cache[file_path] = data
        return data

    # Cache git diff per file (subprocess git call) - same file diff is
    # identical regardless of which method we're analyzing.
    _diff_cache: dict = {}
    _diff_cache_lock = threading.Lock()

    def _cached_diff(file_path: str) -> str:
        with _diff_cache_lock:
            if file_path in _diff_cache:
                return _diff_cache[file_path]
        diff = _extract_git_diff(file_path, repo_root)
        with _diff_cache_lock:
            _diff_cache[file_path] = diff
        return diff

    def _analyze_one_method(group):
        """Analyze a single deduplicated method group end-to-end.

        Pure function (apart from the shared file/diff caches, which are
        lock-protected) so it is safe to invoke from multiple threads.
        Returns the result dict to be appended to `results`, or None when
        the method body could not be read.
        """
        method_name = group['method_name']
        # Pick the instance with the most changed lines for the primary body
        primary = max(group['instances'], key=lambda m: len(m.get('changed_lines', [])))
        file_path = primary.get('file_path', '')
        line_start = primary.get('line_start', 0)
        line_end = primary.get('line_end', 0)
        changed_lines = primary.get('changed_lines', [])
        class_name = primary.get('class_name', '')

        # Defensive coercion: explicit/CLI-driven runs may carry null line
        # numbers. Resolve them from the file when possible, else fall back to
        # 0 so arithmetic below never raises 'NoneType - NoneType'.
        if line_start is None or line_end is None:
            r_start, r_end = _resolve_method_line_range(file_path, method_name, repo_root)
            if line_start is None:
                line_start = r_start if r_start is not None else 0
            if line_end is None:
                line_end = r_end if r_end is not None else (line_start or 0)

        print(f"[Stage 1.5] Analyzing: {class_name}.{method_name}() [{line_end - line_start + 1} lines]")

        # Extract full method body (cached per-file to avoid re-reading the
        # same file when several methods in it changed).
        cached_lines = _cached_read(file_path)
        if not cached_lines:
            print(f"  WARNING: Could not read method body from {file_path}")
            return None
        s_idx = max(0, line_start - 1)
        e_idx = min(len(cached_lines), line_end)
        method_body = ''.join(cached_lines[s_idx:e_idx])

        # Truncate very long methods to ~800 lines to fit context
        body_lines = method_body.split('\n')
        if len(body_lines) > 800:
            # Keep first 200, last 200, and lines around changes
            keep = set(range(200))
            keep.update(range(len(body_lines) - 200, len(body_lines)))
            for cl in changed_lines:
                idx = cl - line_start
                keep.update(range(max(0, idx - 20), min(len(body_lines), idx + 20)))
            kept = []
            prev_i = -2
            for i in sorted(keep):
                if i < len(body_lines):
                    if i > prev_i + 1:
                        kept.append(f"    ... [{i - prev_i - 1} lines omitted] ...")
                    kept.append(f"L{line_start + i}: {body_lines[i]}")
                    prev_i = i
            method_body = '\n'.join(kept)

        # Extract changed lines with context using the cached file lines.
        snippets = []
        seen = set()
        for ln in sorted(changed_lines):
            start_ctx = max(0, ln - 4)
            end_ctx = min(len(cached_lines), ln + 3)
            for i in range(start_ctx, end_ctx):
                if i not in seen:
                    seen.add(i)
                    marker = " >>>" if (i + 1) in changed_lines else "    "
                    snippets.append(f"{marker} L{i+1}: {cached_lines[i].rstrip()}")
        changed_context = '\n'.join(snippets)

        # Extract actual git diff (before/after) - cached per file
        git_diff = _cached_diff(file_path)
        diff_hunks = _extract_diff_hunks_for_method(git_diff, line_start, line_end)

        # Build call-chain text
        chain_text = _build_call_chain_text(call_chains, method_name)

        # Build a CONTEXT-ONLY record for the Copilot agent to reason over.
        # The deterministic extraction (call chain, git diff, changed lines in
        # context) lives here; the agent fills the reasoning fields below by
        # reading this same artifact. No LLM/network call happens in-process.
        language = _detect_language(file_path)
        record = {
            'method_name': method_name,
            'class_name': class_name,
            'file_path': file_path,
            'language': language,
            'line_range': [line_start, line_end],
            'changed_lines': sorted(set(group['all_changed_lines'])),
            'instances': len(group['instances']),
            'diff_hunks': diff_hunks,
            # Context the agent uses to reason (inputs, not answers):
            '_context': {
                'call_chain': chain_text,
                'git_diff': diff_hunks,
                'changed_lines_in_context': changed_context,
            },
            # Reasoning fields — EMPTY until the Copilot agent fills them:
            'purpose': '',
            'exact_change': '',
            'change_impact': '',
            'changed_variables': [],
            'affected_behaviors': [],
            'controlling_parameters': [],
            'test_scenarios': [],
            'NOT_affected': [],
            '_needs_agent_reasoning': True,
        }
        print(f"  • prepared context for {class_name}.{method_name}()")
        return record

    # Extract context for every method in parallel. executor.map preserves the
    # input order, so `results` ends up in the same order as `unique_methods`,
    # keeping output deterministic.
    print(f"[Stage 1.5] Preparing context for {len(unique_methods)} method(s) (max_workers=6)")
    with ThreadPoolExecutor(max_workers=6) as executor:
        for r in executor.map(_analyze_one_method, unique_methods):
            if r is not None:
                results.append(r)

    # Write a PENDING baseline: deterministic context is filled, reasoning
    # fields are empty and awaiting the Copilot agent.
    output = agent_reasoning.mark_pending({
        'stage': '1.5',
        'description': 'Method Understanding (Copilot-agent reasoning)',
        'total_methods': len(unique_methods),
        'successful': 0,  # counts methods with agent reasoning; 0 until filled
        'call_chains_text': [_build_call_chain_text(call_chains, g['method_name']) for g in unique_methods],
        # Agent-provided search keywords (consumed by diff-concept refinement).
        'test_keywords': [],
        'exclude_keywords': [],
        'methods': results,
    })

    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2)

    agent_reasoning.print_action_required(
        stage="STAGE 1.5 — Method Understanding",
        reads=[out_path + "  (each method's _context: git_diff, call_chain, changed lines)"],
        writes=out_path,
        instructions="""
For EACH method in "methods", read its "_context" and fill these fields:
  purpose, exact_change, change_impact, changed_variables[],
  affected_behaviors[], controlling_parameters[], test_scenarios[], NOT_affected[].
Follow the surgical-change rules in .github/agents/ria.agent.md (only the
clause that actually changed matters; adjacent clauses are NOT_affected).
Also fill top-level "test_keywords" and "exclude_keywords" (QA/business search
terms derived from the changes; exclude terms tied to NOT_affected behavior).
Set top-level "_reasoning_source" to "copilot-agent" and remove
"_needs_agent_reasoning". Then re-run ria_agent.py to continue.
""",
    )

    print(f"\n[Stage 1.5] Pending baseline written: {out_path}")
    print(f"[Stage 1.5] {len(unique_methods)} method(s) awaiting Copilot-agent reasoning")
    return output


if __name__ == '__main__':
    repo_root = sys.argv[1] if len(sys.argv) > 1 else '.'
    run(repo_root)
