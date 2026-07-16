#!/usr/bin/env python3
"""
Stage 0c: Build Flow Dependencies (RIA v2 - One-Time)

Analyzes ENTIRE workspace (all flows × all components) to build dependency map.

Dependency Types:
  - DIRECT:   Component methods are IN CALL TREE from entry point
  - INDIRECT: Tests for that flow mention component keywords (but methods
              are not in call tree)
  - NONE:     No relationship (not added to map)

Bugs/Gaps Fixed:
  - BUG-FD2: trace_call_chain now uses find_called_methods() (call-tree
             walk DOWNWARD), not find_symbol() (which returned definitions).
  - BUG-FD3: check_keyword_match_in_tests now actually loads the enriched
             corpus, filters by flow tag, and searches test text with
             word-boundary matching.
  - BUG-FD4: Removed entry_points[:2] cap; we now process all entry points.
  - GAP-FD2: BFS now tracks (method, file, depth) to honour true path depth.

Output: flow_dependencies.json
"""

import argparse
import json
import os
import re
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from serena_mcp_client import SerenaMCPClient
from configs.ria_config import RIA_OUTPUT_DIR, REPO_ROOT


# ---------------------------------------------------------------------------
# DIRECT: call-tree tracing (BUG-FD2 fix)
# ---------------------------------------------------------------------------

def _split_entry_point(ep):
    """Accept either 'file:method' or just 'method' string."""
    if isinstance(ep, dict):
        return ep.get('file', ''), ep.get('method', '')
    if not isinstance(ep, str):
        return '', ''
    if ':' in ep:
        file_path, method = ep.rsplit(':', 1)
        return file_path, method
    return '', ep


def trace_call_chain(entry_point, component_methods, serena, max_depth=5,
                     max_breadth=40, method_to_files=None):
    """
    BFS down the call tree from `entry_point` looking for any method in
    `component_methods`. Returns True on first hit.

    `entry_point` is "file:method" (or a {'file','method'} dict).
    `component_methods` is a set/list of method names belonging to the
    target component.
    `method_to_files` (optional) is a dict: method_name -> list of files
    where that method is declared, built from the component_map. Enables
    cross-file resolution: when an expanded call name matches a method
    declared elsewhere in the codebase, we can hop into that file's body
    and continue the descent. Without it, the trace is single-file and
    cannot follow service-layer dispatches between classes.
    """
    if not component_methods:
        return False
    component_set = set(component_methods)

    ep_file, ep_method = _split_entry_point(entry_point)
    if not ep_method:
        return False

    visited = set()
    # Queue items: (method_name, file_path, depth)
    queue = [(ep_method, ep_file, 0)]
    expansions = 0
    max_expansions = 400  # global ceiling per (flow, component) pair

    while queue and expansions < max_expansions:
        current_method, current_file, depth = queue.pop(0)

        key = f"{current_file}:{current_method}"
        if key in visited:
            continue
        visited.add(key)

        # Hit?
        if current_method in component_set:
            return True

        if depth >= max_depth:
            continue

        # Expand: find what current_method calls, in current_file.
        # If we don't know the file, we can't introspect the method body, skip.
        if not current_file:
            continue

        # serena.find_called_methods is a static-analysis lookup; an empty
        # list is a legitimate result (leaf method). However, exceptions
        # indicate a real failure (e.g. unreadable file, invalid syntax)
        # and must surface to surface KB build problems.
        try:
            called = serena.find_called_methods(current_method, current_file)
        except Exception as e:
            raise RuntimeError(
                f"[build_flow_dependencies] serena.find_called_methods "
                f"failed for method '{current_method}' in "
                f"'{current_file}': {e}\n"
                f"Root cause: file is unreadable, has syntax errors, or "
                f"the parser does not support its language.\n"
                f"Fix: Verify the source file and the active language "
                f"profile in configs/ria_config.py."
            ) from e
        expansions += 1

        for call in called[:max_breadth]:
            cname = call.get('name')
            if not cname or cname in (current_method,):
                continue
            # Quick win: if a called name is a component method, return True.
            if cname in component_set:
                return True
            # Cross-file hop: enqueue the SAME-file body first (covers
            # private helpers and same-class overloads), then enqueue the
            # other-file declarations so the BFS can descend into the
            # service layer / DAO classes that live in different files.
            queue.append((cname, current_file, depth + 1))
            if method_to_files:
                for other_file in method_to_files.get(cname, [])[:3]:
                    if other_file == current_file:
                        continue
                    other_key = f"{other_file}:{cname}"
                    if other_key in visited:
                        continue
                    queue.append((cname, other_file, depth + 1))

    return False


# ---------------------------------------------------------------------------
# INDIRECT: enriched-corpus keyword search (BUG-FD3 fix)
# ---------------------------------------------------------------------------

def _build_test_text(test):
    step_text_parts = []
    for step in test.get('steps', []) or []:
        step_text_parts.append(
            (step.get('action') or '') + ' ' +
            (step.get('data') or '') + ' ' +
            (step.get('result') or '')
        )
    return ' '.join([
        test.get('summary') or '',
        test.get('description') or '',
        ' '.join(step_text_parts)
    ]).lower()


def check_keyword_match_in_tests(enriched_tests_by_flow, flow_tags,
                                 component_keywords):
    """
    Real INDIRECT detection. For each test associated with the given flow
    tags, do a word-boundary search for any component keyword.

    Args:
        enriched_tests_by_flow: dict mapping flow_tag -> list of pre-built
                                lower-cased test text strings.
        flow_tags: list of flow tags (e.g. ["[WORK_POLICY_GET]"]).
        component_keywords: list of keywords for the component.

    Returns:
        True if any tagged test mentions any keyword.
    """
    if not flow_tags or not component_keywords:
        return False

    # Collect candidate texts (dedupe via set of ids would be costlier; lists
    # are fine because the corpus is bounded).
    texts = []
    for tag in flow_tags:
        texts.extend(enriched_tests_by_flow.get(tag, []))

    if not texts:
        return False

    # Build one combined regex of clean keywords for efficiency.
    cleaned = []
    for kw in component_keywords:
        if not kw:
            continue
        kw = kw.strip().lower()
        if len(kw) < 3:
            continue
        cleaned.append(re.escape(kw))
    if not cleaned:
        return False

    # Word-boundary at both ends. Multi-word keywords (with spaces) still
    # benefit from \b boundaries on outer edges.
    pattern = re.compile(r'\b(?:' + '|'.join(cleaned) + r')\b')
    for text in texts:
        if pattern.search(text):
            return True
    return False


def _index_enriched_tests_by_flow(enriched_tests):
    """Pre-build a {flow_tag: [test_text_lower, ...]} index."""
    index = {}
    for test in enriched_tests:
        text = _build_test_text(test)
        for tag in test.get('auto_tags', []) or []:
            index.setdefault(tag, []).append(text)
    return index


def _load_enriched_tests(output_dir, explicit_path=None):
    """Find enriched corpus from preferred KB location, then RIA_INPUT.

    When `explicit_path` is supplied (Option A: per-pipeline enriched corpus
    routing), it is checked FIRST so the dependency pipeline picks up
    `all_tcs_extracted_enriched_dependency.json` rather than the
    source-pipeline-tagged file.

    The legacy backward-compat file (`all_tcs_extracted_enriched.json`) is no
    longer produced; only the per-pipeline files are read here.
    """
    candidates = []
    if explicit_path:
        candidates.append(explicit_path)
    # Prefer the per-pipeline source file; fall back to the dependency file
    # so this loader continues to work in dependency-only invocations that
    # do not pass an explicit path. The RIA_INPUT mirror is checked last as
    # a safety net for legacy callers.
    candidates.extend([
        os.path.join(output_dir, 'all_tcs_extracted_enriched_source.json'),
        os.path.join(output_dir, 'all_tcs_extracted_enriched_dependency.json'),
        os.path.join(REPO_ROOT, '.github', 'RIA_INPUT', 'all_tcs_extracted_enriched.json'),
    ])
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f), path
            except Exception:
                continue
    return [], None


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_flow_dependencies(flow_registry_path, component_map_path, repo_root,
                            output_path, changed_components=None,
                            enriched_corpus_path=None):
    """
    Build flow dependencies map for flows × CHANGED components only (per-run).

    Args:
        changed_components: List of component names that changed (e.g., ['AgentTrade']).
                           If None, analyzes ALL components (legacy behavior).
        enriched_corpus_path: Optional explicit path to the enriched test
                           corpus. Option A (separate enriched corpus per
                           pipeline) uses this to route the dependency
                           pipeline at
                           `all_tcs_extracted_enriched_dependency.json`. When
                           None, the loader falls back to the per-pipeline
                           source file
                           (`<output_dir>/all_tcs_extracted_enriched_source.json`)
                           then the per-pipeline dependency file. The legacy
                           single-pipeline file is no longer produced or
                           read from `<output_dir>`.
    """
    mode = "FOCUSED (changed components only)" if changed_components else "FULL (all components)"
    print(f"\n{'=' * 80}")
    print(f"STAGE 0c: Build Flow Dependencies (Per-Run, {mode})")
    print(f"{'=' * 80}")
    if changed_components:
        print(f"Processing flows × {len(changed_components)} changed component(s): {changed_components}")
    else:
        print("Processing ENTIRE workspace (all flows x all components)")

    # Initialize Serena MCP
    serena = SerenaMCPClient(repo_path=repo_root, enabled=True, max_symbols=100000)

    # Load flow registry
    print("\nLoading flow registry...")
    with open(flow_registry_path, 'r', encoding='utf-8') as f:
        flow_registry = json.load(f)
    flows = flow_registry.get('flows', [])
    print(f"  Loaded {len(flows)} flows")

    # Load component map
    print("\nLoading component map...")
    with open(component_map_path, 'r', encoding='utf-8') as f:
        component_map = json.load(f)
    all_components = component_map.get('components', [])
    print(f"  Loaded {len(all_components)} components")

    # Filter to changed components only if specified
    if changed_components:
        components = [c for c in all_components
                     if c.get('component_name') in changed_components
                     or any(rc in changed_components
                           for rc in c.get('raw_class_names', []))]
        print(f"  Filtered to {len(components)} changed components: {[c.get('component_name') for c in components]}")
    else:
        components = all_components

    # Build cross-file method index: method_name -> [file_paths] from the
    # FULL component_map (not just the filtered subset). This lets
    # trace_call_chain hop across class boundaries when descending the
    # call tree from an entry point.
    method_to_files = {}
    for c in all_components:
        for fp in (c.get('file_paths') or []):
            for m in (c.get('methods') or []):
                method_to_files.setdefault(m, []).append(fp)

    # Load enriched test corpus once for INDIRECT checks (BUG-FD3 fix).
    # FAIL-FAST: missing enriched corpus = Stage 0 ordering bug.
    output_dir = os.path.dirname(output_path)
    print("\nLoading enriched test corpus for INDIRECT detection...")
    enriched_tests, enriched_path = _load_enriched_tests(
        output_dir, explicit_path=enriched_corpus_path)
    if not enriched_tests:
        raise FileNotFoundError(
            f"[build_flow_dependencies] No enriched test corpus found "
            f"under {output_dir}. INDIRECT dependency detection cannot "
            f"run without it.\n"
            f"Root cause: Stage 0 (build_flow_registry.py) did not "
            f"produce all_tcs_extracted_enriched.json.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb' so the "
            f"enriched corpus is built before flow_dependencies."
        )
    print(f"  Loaded {len(enriched_tests)} enriched tests from {enriched_path}")
    enriched_index = _index_enriched_tests_by_flow(enriched_tests)
    print(f"  Indexed tests across {len(enriched_index)} flow tags")

    # Build dependencies
    print(f"\nAnalyzing {len(flows)} flows x {len(components)} components...")
    dependencies = []
    direct_count = 0
    indirect_count = 0

    for flow_idx, flow in enumerate(flows, 1):
        flow_name = flow.get('flow_name', '')
        entry_points = flow.get('entry_points', []) or []
        flow_tags = flow.get('test_tags', []) or [
            f"[{flow_name.upper().replace(' ', '_')}]"
        ]
        flow_test_count = flow.get('test_count', 0)

        if flow_idx % 5 == 0 or flow_idx == 1:
            print(f"\n  [{flow_idx}/{len(flows)}] Flow: {flow_name} "
                  f"({len(entry_points)} entry points)")

        for component in components:
            comp_name = component.get('component_name', '')
            comp_methods = component.get('methods', []) or []
            comp_keywords = component.get('keywords', []) or []

            # ---- DIRECT (BUG-FD4 fix: walk ALL entry points, not [:2]) ----
            is_direct = False
            for ep in entry_points:
                if trace_call_chain(ep, comp_methods, serena, max_depth=5,
                                    method_to_files=method_to_files):
                    is_direct = True
                    break

            if is_direct:
                dependencies.append({
                    "flow": flow_name,
                    "component": comp_name,
                    "dependency_type": "DIRECT"
                })
                direct_count += 1
                continue  # skip INDIRECT once DIRECT is established

            # ---- INDIRECT (BUG-FD3 fix: real keyword search in flow tests) ----
            if flow_test_count > 0 and check_keyword_match_in_tests(
                enriched_index, flow_tags, comp_keywords
            ):
                dependencies.append({
                    "flow": flow_name,
                    "component": comp_name,
                    "dependency_type": "INDIRECT"
                })
                indirect_count += 1
            # else: NONE (not added)

    # Fix #5: Validate dependencies against the registry before writing.
    # Every dependency MUST reference a flow that exists in the
    # registry, otherwise we will produce an inconsistent KB where
    # flow_dependencies.json names a flow flow_registry.json does not
    # know about (the symptom Fix #4 is meant to eliminate at source).
    registry_flow_names = {f.get('flow_name', '') for f in flows if f.get('flow_name')}
    orphan_deps = [
        dep for dep in dependencies
        if dep.get('flow') and dep['flow'] not in registry_flow_names
    ]
    if orphan_deps:
        print(f"\n[WARN] {len(orphan_deps)} dependency record(s) reference flows "
              f"not present in flow_registry.json:")
        for dep in orphan_deps[:10]:
            print(f"     - flow='{dep.get('flow')}' component='{dep.get('component')}' "
                  f"type={dep.get('dependency_type')}")
        # Drop orphans so the two files stay in sync.
        dependencies = [
            dep for dep in dependencies
            if not dep.get('flow') or dep['flow'] in registry_flow_names
        ]
        print(f"[OK] Dropped {len(orphan_deps)} orphan dependency record(s); "
              f"{len(dependencies)} remain in sync with registry")
        # Recompute counts to match the filtered list.
        direct_count = sum(1 for d in dependencies if d.get('dependency_type') == 'DIRECT')
        indirect_count = sum(1 for d in dependencies if d.get('dependency_type') == 'INDIRECT')

    # Save dependencies
    output_data = {
        "dependencies": dependencies,
        "total_dependencies": len(dependencies),
        "direct_count": direct_count,
        "indirect_count": indirect_count,
        "flows_analyzed": len(flows),
        "components_analyzed": len(components),
        "source": "call-tree tracing (find_called_methods) + word-boundary keyword matching"
    }

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)

    print(f"\n{'=' * 80}")
    print(f"STAGE 0c COMPLETE")
    print(f"{'=' * 80}")
    print(f"Dependencies found: {len(dependencies)}")
    print(f"  DIRECT:   {direct_count}")
    print(f"  INDIRECT: {indirect_count}")
    print(f"Output saved: {output_path}")

    return output_data


def main():
    parser = argparse.ArgumentParser(description="Build Flow Dependencies (Stage 0c)")
    parser.add_argument("--flow-registry",
                        default=os.path.join(RIA_OUTPUT_DIR, "knowledge_base", "flow_registry.json"),
                        help="Flow registry path")
    parser.add_argument("--component-map",
                        default=os.path.join(RIA_OUTPUT_DIR, "knowledge_base", "component_map.json"),
                        help="Component map path")
    parser.add_argument("--repo-root", default=REPO_ROOT, help="Repository root")
    parser.add_argument("--output",
                        default=os.path.join(RIA_OUTPUT_DIR, "knowledge_base", "flow_dependencies.json"),
                        help="Output path")
    parser.add_argument("--changed-components",
                        help="Comma-separated list of changed component names (e.g., 'AgentTrade,TargetAgentsCallable')")
    parser.add_argument("--changed-component",
                        help="Single changed component name (alias for --changed-components)")
    parser.add_argument("--enriched-corpus-path", default=None,
                        help="Optional explicit path to enriched test corpus JSON (per-pipeline override). "
                             "If not supplied, falls back to the legacy file inside output_dir.")

    args = parser.parse_args()

    # Parse changed components
    changed_components = None
    if args.changed_components:
        changed_components = [c.strip() for c in args.changed_components.split(',') if c.strip()]
    elif args.changed_component:
        changed_components = [args.changed_component.strip()]

    try:
        build_flow_dependencies(args.flow_registry, args.component_map, args.repo_root, args.output,
                               changed_components=changed_components,
                               enriched_corpus_path=args.enriched_corpus_path)
        sys.exit(0)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
