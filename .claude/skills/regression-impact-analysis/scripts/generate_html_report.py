#!/usr/bin/env python3
"""
RIA HTML Report Generator - Combined Source + Dependency Edition.

Produces a single HTML report with two clearly-separated sections that
cover BOTH analysis pipelines (source code changes and dependency
changes):

  Summary Box           - Totals for both pipelines at a glance.

  SECTION 1 - CODE CHANGE ANALYSIS (source pipeline)
    1a. Changed Methods       - methods + file/line refs from method_understanding.json
    1b. Funnel                - Pipeline progression (Stage 4 -> 5 -> 6 -> 7).
    1c. Flows & Scenarios     - Per-flow scenarios with test counts.
    1d. Test Distribution     - How the final tests split across flows / scenarios.
    1e. Recommended Tests     - Every source-pipeline test with score and rationale.

  SECTION 2 - DEPENDENCY CHANGE ANALYSIS (dependency pipeline)
    2a. Changed Dependencies  - artifact, old/new versions, classification
    2b. Affected Components   - production files impacted by the dependency upgrade
    2c. Impacted Flows        - DIRECT/INDIRECT flow counts
    2d. Recommended Tests     - Every dependency-pipeline test with score / flow

  Final Recommendations - actionable mvn test command + validation checklist.

Sources (read from <output_dir>):
  Combined / both:
    - combined_pipeline_tests.json                  (UNION of both pipelines)

  Source-pipeline only:
    - stage6_aggressive_tests_source_pipeline.json  (source tests, post-restore)
    - stage6_aggressive_tests.json                  (legacy / single-pipeline mode)
    - method_understanding.json                     (changed methods + line ranges)
    - stage2_impacted_flows.json                    (direct vs indirect flow counts)
    - ria_v7_summary.json                           (funnel counts)
    - stage7_llm_tc_judgment.json                   (verdicts, reasoning)
    - consolidated_summary.json                     (changed methods context)

  Dependency-pipeline only:
    - stage6_aggressive_tests_dependency_pipeline.json (dependency tests)
    - dependency_change_summary.json                (per-stack dep classification)
    - flow_registry.dependency.json                 (dep flows + component_changes)
    - critical_suite_recommendation.json            (CRITICAL_SUITE strategy)

Backward compatible: if the dependency pipeline did not run (no
combined_pipeline_tests.json and no dependency_change_summary.json),
Section 2 is hidden and the report behaves exactly like the
pre-combined source-only version.

Public entry point: generate_html_report(output_dir, output_file='RIA_Report.html')
"""

from __future__ import annotations

import html as _html
import json
import os
import shutil
from collections import OrderedDict, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _esc(value: Any) -> str:
    """HTML-escape any value, treating None as empty string."""
    if value is None:
        return ''
    return _html.escape(str(value), quote=True)


def _read_json(path: str) -> Optional[Any]:
    """Load a JSON file or return None if missing / unreadable."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[WARN] Failed to read {path}: {exc}")
        return None


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_inputs(output_dir: str) -> Dict[str, Any]:
    """Load the JSON inputs needed by the combined two-section report.

    Pipeline test sets are loaded from three possible files. Files are
    looked up in this priority order to stay backward compatible with
    runs that produced only a subset of them:

      Source tests:
        1. stage6_aggressive_tests_source_pipeline.json   (post-restore copy)
        2. stage6_aggressive_tests.json                   (legacy / source-only run)

      Dependency tests:
        1. stage6_aggressive_tests_dependency_pipeline.json (post-restore copy)
        2. (none)  -> Section 2 is hidden

      Combined (union) tests:
        1. combined_pipeline_tests.json                   (preferred)
        2. fallback: source tests when no dependency pipeline ran
    """
    summary = _read_json(os.path.join(output_dir, 'ria_v7_summary.json')) or {}

    # ---- Source pipeline stage6 -----------------------------------------
    source_stage6 = (
        _read_json(os.path.join(
            output_dir, 'stage6_aggressive_tests_source_pipeline.json'))
        or _read_json(os.path.join(output_dir, 'stage6_aggressive_tests.json'))
        or {}
    )

    # ---- Dependency pipeline stage6 -------------------------------------
    dep_stage6 = _read_json(
        os.path.join(output_dir, 'stage6_aggressive_tests_dependency_pipeline.json'),
    ) or {}

    # ---- Combined (union) pipeline tests --------------------------------
    combined = _read_json(
        os.path.join(output_dir, 'combined_pipeline_tests.json'),
    ) or {}

    stage7 = _read_json(os.path.join(output_dir, 'stage7_llm_tc_judgment.json'))
    if stage7 is None:  # legacy filename fallback
        stage7 = _read_json(os.path.join(output_dir, 'stage7_llm_synthesis.json')) or {}
    consolidated = _read_json(os.path.join(output_dir, 'consolidated_summary.json')) or {}

    # ---- Source pipeline metadata ---------------------------------------
    method_understanding = _read_json(
        os.path.join(output_dir, 'method_understanding.json'),
    ) or {}
    impacted_flows = _read_json(
        os.path.join(output_dir, 'stage2_impacted_flows.json'),
    ) or {}

    # ---- Dependency pipeline metadata -----------------------------------
    # RIA Phase 2: dependency-change summary (only present when the run
    # was triggered by a pom.xml / package.json / requirements.txt change).
    dep_summary = _read_json(
        os.path.join(output_dir, 'dependency_change_summary.json'),
    ) or {}
    critical_rec = _read_json(
        os.path.join(output_dir, 'critical_suite_recommendation.json'),
    ) or {}
    dep_flow_registry = _read_json(
        os.path.join(output_dir, 'flow_registry.dependency.json'),
    ) or {}

    return {
        'summary': summary,
        # Stage 6 records — source pipeline view used by the existing
        # four-section breakdown (funnel/flows/distribution/tests).
        'stage6': source_stage6,
        # Raw per-pipeline records for the combined report sections.
        'source_stage6': source_stage6,
        'dep_stage6': dep_stage6,
        'combined': combined,
        'stage7': stage7,
        'consolidated': consolidated,
        'method_understanding': method_understanding,
        'impacted_flows': impacted_flows,
        'dep_summary': dep_summary,
        'critical_rec': critical_rec,
        'dep_flow_registry': dep_flow_registry,
    }


# ---------------------------------------------------------------------------
# Pipeline test extraction helpers
# ---------------------------------------------------------------------------

def _aggressive_tests(stage6: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return the list of aggressive_tests records from a stage6 payload."""
    if not isinstance(stage6, dict):
        return []
    tests = stage6.get('aggressive_tests') or stage6.get('tests') or []
    return [t for t in tests if isinstance(t, dict)]


def _combined_tests(combined: Dict[str, Any],
                    source_tests: List[Dict[str, Any]],
                    dep_tests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return the union of source + dependency tests.

    Prefer the persisted combined_pipeline_tests.json file (which has
    been deduplicated by the orchestrator). Fall back to a local merge
    keyed on issue_key when that file is unavailable.
    """
    if isinstance(combined, dict):
        tests = combined.get('tests') or []
        if tests:
            return [t for t in tests if isinstance(t, dict)]

    merged: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for t in dep_tests:
        key = t.get('issue_key') or t.get('test_id') or id(t)
        merged[str(key)] = t
    for t in source_tests:
        key = t.get('issue_key') or t.get('test_id') or id(t)
        merged[str(key)] = t  # source wins on overlap
    return list(merged.values())


# ---------------------------------------------------------------------------
# Combined report — summary, code-change, dependency-change, final recs
# ---------------------------------------------------------------------------

def _format_method_location(m: Dict[str, Any]) -> str:
    """Format a method record as 'name (file.java:start-end)'."""
    name = m.get('method_name') or '(unknown)'
    file_path = m.get('file_path') or ''
    file_short = file_path.rsplit('/', 1)[-1] if file_path else ''
    line_range = m.get('line_range') or []
    if isinstance(line_range, (list, tuple)) and len(line_range) >= 2:
        loc = f"{line_range[0]}-{line_range[1]}"
    elif isinstance(line_range, (list, tuple)) and len(line_range) == 1:
        loc = f"{line_range[0]}"
    else:
        loc = ''
    if file_short and loc:
        return f"{name} ({file_short}:{loc})"
    if file_short:
        return f"{name} ({file_short})"
    return name


def _build_summary_box(source_tests: List[Dict[str, Any]],
                       dep_tests: List[Dict[str, Any]],
                       combined_tests: List[Dict[str, Any]],
                       method_understanding: Dict[str, Any],
                       dep_summary: Dict[str, Any],
                       has_dependency_pipeline: bool) -> str:
    """Top-of-report summary covering both pipelines."""
    num_methods = len(method_understanding.get('methods') or [])
    num_source_tests = len(source_tests)
    num_dep_tests = len(dep_tests)
    total_tests = len(combined_tests)

    # Count of dependency artefacts. Sum the per-stack counts when
    # available, else the union of coordinates from all stacks.
    num_deps = 0
    per_stack = (dep_summary or {}).get('per_stack') or {}
    if per_stack:
        for info in per_stack.values():
            if isinstance(info, dict):
                num_deps += _safe_int(info.get('count'), 0)

    items: List[str] = [
        f'<li>Code Changes: <strong>{num_methods}</strong> method(s) '
        f'&rarr; <strong>{num_source_tests}</strong> test(s)</li>',
    ]
    if has_dependency_pipeline:
        items.append(
            f'<li>Dependency Changes: <strong>{num_deps}</strong> '
            f'librar{"y" if num_deps == 1 else "ies"} '
            f'&rarr; <strong>{num_dep_tests}</strong> test(s)</li>'
        )
    else:
        items.append(
            '<li class="muted">Dependency Changes: '
            '<em>not applicable for this run</em></li>'
        )

    return (
        '<section id="summary" class="summary-box">'
        '<h2>Summary</h2>'
        f'<p><strong>Total tests recommended:</strong> '
        f'<span class="big-num">{total_tests}</span></p>'
        f'<ul class="summary-list">{"".join(items)}</ul>'
        '</section>'
    )


def _build_changed_methods_section(method_understanding: Dict[str, Any],
                                   impacted_flows: Dict[str, Any]) -> str:
    """List the changed methods (with file:line) and direct/indirect flow counts.

    This is the *sub-block* for Section 1 (Code Change Analysis); the
    funnel / flows / distribution / tests sub-blocks are produced by the
    existing builders and concatenated below.
    """
    methods = method_understanding.get('methods') or []

    # Method list -----------------------------------------------------------
    method_rows: List[str] = []
    for idx, m in enumerate(methods, 1):
        if not isinstance(m, dict):
            continue
        loc = _format_method_location(m)
        cls = m.get('class_name') or ''
        purpose = m.get('purpose') or ''
        method_rows.append(
            '<tr>'
            f'<td class="num">{idx}</td>'
            f'<td class="mono">{_esc(cls)}</td>'
            f'<td class="mono">{_esc(loc)}</td>'
            f'<td>{_esc(purpose)}</td>'
            '</tr>'
        )

    if method_rows:
        methods_table = (
            '<table class="data-table">'
            '<thead><tr>'
            '<th class="num">#</th>'
            '<th>Class</th>'
            '<th>Method (file:line)</th>'
            '<th>Purpose</th>'
            '</tr></thead>'
            f'<tbody>{"".join(method_rows)}</tbody>'
            '</table>'
        )
    else:
        methods_table = '<p class="muted">No method-level changes detected.</p>'

    # Impacted flows --------------------------------------------------------
    flows_list = impacted_flows.get('impacted_flows') or []
    direct = sum(
        1 for f in flows_list
        if isinstance(f, dict)
        and (f.get('impact_type') or '').upper() == 'DIRECT'
    )
    indirect = sum(
        1 for f in flows_list
        if isinstance(f, dict)
        and (f.get('impact_type') or '').upper() == 'INDIRECT'
    )
    total_flows = len(flows_list)

    flows_block = (
        '<p class="hint">'
        f'Impacted flows: <strong>{total_flows}</strong> '
        f'(<span class="verdict-pill verdict-direct">{direct} DIRECT</span> '
        f'<span class="verdict-pill verdict-indirect">{indirect} INDIRECT</span>)'
        '</p>'
    )

    return (
        '<div class="subsection">'
        f'<h3>Changed Methods ({len(methods)})</h3>'
        + methods_table
        + flows_block +
        '</div>'
    )


def _build_test_table(tests: List[Dict[str, Any]],
                      table_id: str,
                      empty_msg: str = 'No tests') -> str:
    """Render a stage6-style test list as a sortable table.

    Used by both Section 1 (source tests) and Section 2 (dependency
    tests) so the two pipelines look visually consistent.
    """
    if not tests:
        return f'<p class="muted">{_esc(empty_msg)}</p>'

    # Sort by score desc so the most relevant tests surface first.
    def _score(t: Dict[str, Any]) -> float:
        return _safe_float(
            t.get('total_score'),
            _safe_float(t.get('composite_score'),
                        _safe_float(t.get('score'), 0.0)),
        )
    sorted_tests = sorted(tests, key=_score, reverse=True)

    rows: List[str] = []
    for t in sorted_tests:
        flow_names: List[str] = []
        for f in t.get('matched_flows') or []:
            if isinstance(f, dict):
                fn = f.get('flow_name')
                if fn and fn not in flow_names:
                    flow_names.append(fn)
        if not flow_names:
            single = t.get('matched_flow')
            if single:
                flow_names.append(single)
        flows_txt = ', '.join(flow_names) if flow_names else '-'

        relevance = (
            t.get('criticality_source')
            or t.get('dependency_type')
            or t.get('criticality')
            or '-'
        )
        rows.append(
            '<tr>'
            f'<td class="mono">{_esc(t.get("issue_key") or t.get("test_id") or "")}</td>'
            f'<td>{_esc(t.get("summary") or "")}</td>'
            f'<td>{_esc(flows_txt)}</td>'
            f'<td>{_esc(relevance)}</td>'
            f'<td class="num">{_score(t):.2f}</td>'
            '</tr>'
        )

    return (
        f'<table class="data-table tests-table" id="{_esc(table_id)}">'
        '<thead><tr>'
        '<th>Test Case ID</th>'
        '<th>Summary</th>'
        '<th>Matched Flows</th>'
        '<th>Relevance</th>'
        '<th class="num">Score</th>'
        '</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody>'
        '</table>'
    )


def _build_changed_dependencies_section(
        dep_summary: Dict[str, Any],
        dep_flow_registry: Dict[str, Any]) -> str:
    """Section 2 dep-changes block: artefacts + affected components."""
    per_stack = (dep_summary or {}).get('per_stack') or {}
    rows: List[str] = []
    artefact_count = 0
    for stack, info in per_stack.items():
        if not isinstance(info, dict):
            continue
        for d in info.get('dependencies') or []:
            if not isinstance(d, dict):
                continue
            artefact_count += 1
            coord = d.get('coordinate') or ''
            vc = d.get('version_change') or []
            if isinstance(vc, (list, tuple)) and len(vc) >= 2:
                version_str = f"{vc[0]} &rarr; {vc[1]}"
            else:
                version_str = '-'
            rows.append(
                '<tr>'
                f'<td>{artefact_count}</td>'
                f'<td>{_esc(stack)}</td>'
                f'<td class="mono">{_esc(coord)}</td>'
                f'<td>{version_str}</td>'
                f'<td>{_esc(d.get("strategy") or "")}</td>'
                f'<td class="num">{_esc(d.get("score"))}</td>'
                '</tr>'
            )

    if rows:
        deps_table = (
            '<table class="data-table">'
            '<thead><tr>'
            '<th class="num">#</th>'
            '<th>Stack</th>'
            '<th>Artifact</th>'
            '<th>Version Change</th>'
            '<th>Strategy</th>'
            '<th class="num">Score</th>'
            '</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
    else:
        deps_table = (
            '<p class="muted">No per-dependency classification available.</p>'
        )

    # Affected components (production files impacted by the dep upgrade) ---
    components = dep_flow_registry.get('component_changes') or []
    if components:
        comp_items = ''.join(
            f'<li class="mono">{_esc(c)}</li>' for c in components
        )
        components_block = (
            f'<h3>Affected Components ({len(components)})</h3>'
            f'<ul class="dist-list">{comp_items}</ul>'
        )
    else:
        components_block = ''

    # Impacted dep flows ---------------------------------------------------
    dep_flows = dep_flow_registry.get('flows') or []
    flows_block = ''
    if dep_flows:
        flow_rows: List[str] = []
        for f in dep_flows:
            if not isinstance(f, dict):
                continue
            tags = ', '.join(f.get('test_tags') or []) or '-'
            flow_rows.append(
                '<tr>'
                f'<td>{_esc(f.get("flow_id") or "")}</td>'
                f'<td>{_esc(f.get("flow_name") or "")}</td>'
                f'<td class="mono">{_esc(tags)}</td>'
                f'<td class="num">{_safe_int(f.get("test_count"), 0)}</td>'
                '</tr>'
            )
        flows_block = (
            f'<h3>Impacted Flows ({len(dep_flows)})</h3>'
            '<table class="data-table">'
            '<thead><tr>'
            '<th>Flow ID</th>'
            '<th>Flow Name</th>'
            '<th>Test Tags</th>'
            '<th class="num">Test Count</th>'
            '</tr></thead>'
            f'<tbody>{"".join(flow_rows)}</tbody>'
            '</table>'
        )

    return (
        '<div class="subsection">'
        f'<h3>Changed Dependencies ({artefact_count})</h3>'
        + deps_table
        + components_block
        + flows_block +
        '</div>'
    )


def _extract_test_classes(tests: List[Dict[str, Any]]) -> List[str]:
    """Best-effort: derive likely Maven test-class names from test records.

    Tests recorded by RIA do not always include a class FQN; when they
    do, prefer it. Otherwise pull from `test_class`, `class_name`, or
    a path-shaped `file` field. Duplicates are removed; empties dropped.
    """
    classes: List[str] = []
    seen: set = set()
    for t in tests:
        cls = (
            t.get('test_class')
            or t.get('class_name')
            or t.get('class')
            or ''
        )
        if not cls:
            file_path = t.get('file') or t.get('test_file') or ''
            if isinstance(file_path, str) and file_path.endswith('.java'):
                cls = file_path.rsplit('/', 1)[-1].rsplit('.', 1)[0]
        if cls and cls not in seen:
            seen.add(cls)
            classes.append(cls)
    return classes


def _build_final_recommendations_section(
        combined_tests: List[Dict[str, Any]],
        method_understanding: Dict[str, Any],
        dep_summary: Dict[str, Any],
        has_dependency_pipeline: bool) -> str:
    """Closing actionable section: validation checklist + mvn command."""
    total = len(combined_tests)

    # Build the checklist items
    checklist: List[str] = []
    methods = method_understanding.get('methods') or []
    if methods:
        method_classes: List[str] = []
        for m in methods:
            cls = m.get('class_name')
            if cls and cls not in method_classes:
                method_classes.append(cls)
        if method_classes:
            checklist.append(
                f'<li>&#10003; Code changes in <strong>'
                f'{_esc(", ".join(method_classes))}</strong></li>'
            )

    if has_dependency_pipeline:
        artefacts: List[str] = []
        per_stack = (dep_summary or {}).get('per_stack') or {}
        for info in per_stack.values():
            if not isinstance(info, dict):
                continue
            for d in info.get('dependencies') or []:
                coord = d.get('coordinate') if isinstance(d, dict) else None
                if coord and coord not in artefacts:
                    artefacts.append(coord)
        if artefacts:
            checklist.append(
                f'<li>&#10003; Library upgrade compatibility: <strong>'
                f'{_esc(", ".join(artefacts[:6]))}'
                f'{("..." if len(artefacts) > 6 else "")}</strong></li>'
            )

    checklist_html = (
        '<ul class="dist-list">' + ''.join(checklist) + '</ul>'
        if checklist else ''
    )

    # mvn command (only emit when we found at least one Java-looking class)
    classes = _extract_test_classes(combined_tests)
    mvn_block = ''
    if classes:
        joined = ','.join(classes)
        mvn_block = (
            '<h3>Test Execution Command</h3>'
            f'<pre>mvn test -Dtest="{_esc(joined)}"</pre>'
            '<p class="hint">Class names derived from test records. '
            'Adjust the module / profile flags for your build.</p>'
        )

    return (
        '<section id="final-recommendations" class="final-recommendations">'
        '<h2>Final Recommendations</h2>'
        f'<p>Execute all <strong>{total}</strong> recommended tests to validate:</p>'
        + checklist_html
        + mvn_block +
        '</section>'
    )


# ---------------------------------------------------------------------------
# Dependency-change section (Phase 2)
# ---------------------------------------------------------------------------

def _build_dependency_section(dep_summary: Dict[str, Any],
                               critical_rec: Dict[str, Any]) -> str:
    """
    Render the optional 'Dependency Changes' section. Returns an empty
    string when neither file is present (so source-change runs look
    identical to before).
    """
    if not dep_summary and not critical_rec:
        return ''

    lines: List[str] = ['<section id="dep-changes">'
                        '<h2>0. Dependency Changes</h2>']

    if critical_rec and critical_rec.get('strategy') == 'CRITICAL_SUITE':
        lines.append(
            '<div class="dep-critical">'
            '<strong>CRITICAL_SUITE recommended.</strong> '
            f"{_esc(critical_rec.get('reason') or '')}"
            '</div>'
        )

    if dep_summary:
        overall = _esc(dep_summary.get('overall_strategy') or '')
        unique_flows_count = _safe_int(dep_summary.get('unique_flows_count'), 0)
        affected = _safe_int(dep_summary.get('affected_files_count'), 0)
        selected = _safe_int(dep_summary.get('selected_tests_count'), 0)
        lines.append(
            '<div class="dep-summary">'
            f'<div><strong>Strategy:</strong> {overall}</div>'
            f'<div><strong>Affected source files:</strong> {affected:,}</div>'
            f'<div><strong>Unique flows:</strong> {unique_flows_count:,}</div>'
            f'<div><strong>Selected tests (3-per-flow):</strong> {selected:,}</div>'
            '</div>'
        )

        # Per-stack table
        per_stack = dep_summary.get('per_stack') or {}
        if per_stack:
            rows: List[str] = []
            rows.append(
                '<thead><tr><th>Stack</th><th>Count</th>'
                '<th>Top dependencies</th></tr></thead>'
            )
            body: List[str] = []
            for stack, info in per_stack.items():
                if not isinstance(info, dict):
                    continue
                count = _safe_int(info.get('count'), 0)
                deps = info.get('dependencies') or []
                top_lines = []
                for d in deps[:8]:
                    if not isinstance(d, dict):
                        continue
                    coord = _esc(d.get('coordinate') or '')
                    strat = _esc(d.get('strategy') or '')
                    score = _esc(d.get('score'))
                    top_lines.append(
                        f"<div class='dep-row'>"
                        f"<code>{coord}</code> "
                        f"<span class='dep-strat'>[{strat}]</span> "
                        f"<span class='dep-score'>score={score}</span>"
                        f"</div>"
                    )
                body.append(
                    f'<tr><td>{_esc(stack)}</td><td>{count}</td>'
                    f'<td>{"".join(top_lines) or "(none)"}</td></tr>'
                )
            rows.append(f'<tbody>{"".join(body)}</tbody>')
            lines.append(f'<table class="dep-table">{"".join(rows)}</table>')

        # Flow breakdown
        flows = dep_summary.get('unique_flows') or []
        if flows:
            flow_rows: List[str] = [
                '<thead><tr><th>Flow ID</th><th>Flow Name</th>'
                '<th>Tags</th><th>Affected files</th><th>Origin</th></tr></thead>'
            ]
            body2: List[str] = []
            for f in flows:
                if not isinstance(f, dict):
                    continue
                tag_html = ', '.join(
                    f"<code>{_esc(t)}</code>"
                    for t in (f.get('test_tags') or [])
                )
                body2.append(
                    '<tr>'
                    f'<td>{_esc(f.get("flow_id"))}</td>'
                    f'<td>{_esc(f.get("flow_name"))}</td>'
                    f'<td>{tag_html or "(none)"}</td>'
                    f'<td>{_safe_int(f.get("sources_count"), 0)}</td>'
                    f'<td>{_esc(f.get("origin"))}</td>'
                    '</tr>'
                )
            flow_rows.append(f'<tbody>{"".join(body2)}</tbody>')
            lines.append(
                '<h3 class="dep-subhead">Unique business flows</h3>'
                f'<table class="dep-table">{"".join(flow_rows)}</table>'
            )

    lines.append('</section>')
    return ''.join(lines)


def _index_stage6(stage6: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Build issue_key -> stage6 record map for fast lookup."""
    index: Dict[str, Dict[str, Any]] = {}
    for rec in stage6.get('aggressive_tests', []) or []:
        key = rec.get('issue_key') or rec.get('test_id')
        if key:
            index[key] = rec
    return index


def _selected_judgments(stage7: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Stage 7 judgments where verdict is DIRECT or INDIRECT (final selection)."""
    selected = []
    for j in stage7.get('judgments', []) or []:
        verdict = (j.get('verdict') or '').upper()
        if verdict in ('DIRECT', 'INDIRECT'):
            selected.append(j)
    return selected


# ---------------------------------------------------------------------------
# Row enrichment
# ---------------------------------------------------------------------------

def _flow_names_for(stage6_rec: Optional[Dict[str, Any]]) -> List[str]:
    """Extract the list of matched flow names for a stage6 record."""
    if not stage6_rec:
        return []
    flows: List[str] = []
    for f in stage6_rec.get('matched_flows', []) or []:
        name = f.get('flow_name')
        if name and name not in flows:
            flows.append(name)
    if not flows:
        single = stage6_rec.get('matched_flow')
        if single:
            flows.append(single)
    return flows


def _build_rows(judgments: List[Dict[str, Any]],
                stage6_index: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Enrich each Stage 7 judgment with Stage 6 metadata for the table."""
    rows: List[Dict[str, Any]] = []
    for j in judgments:
        issue_key = j.get('issue_key') or j.get('test_id') or ''
        s6 = stage6_index.get(issue_key, {})

        flows = _flow_names_for(s6)
        scenarios: List[str] = []
        scen = j.get('scenario_match')
        if scen and scen != 'NONE':
            scenarios.append(scen)

        composite = _safe_float(s6.get('composite_score'),
                                _safe_float(s6.get('total_score'), 0.0))
        verdict = (j.get('verdict') or '').upper()
        confidence = _safe_float(j.get('confidence'), 0.0)
        reasoning = (j.get('reasoning') or '').strip()
        flow_match = bool(j.get('flow_match'))

        rationale_parts = [
            f"{verdict} verdict (confidence: {confidence:.2f})",
            f"Flow match: {'yes' if flow_match else 'no'}",
        ]
        if reasoning:
            rationale_parts.append(f"Reason: {reasoning}")
        rationale = ' - '.join(rationale_parts)

        summary = j.get('summary') or s6.get('summary') or ''

        rows.append({
            'issue_key': issue_key,
            'summary': summary,
            'flows': flows,
            'scenarios': scenarios,
            'composite_score': composite,
            'verdict': verdict,
            'confidence': confidence,
            'reasoning': reasoning,
            'flow_match': flow_match,
            'rationale': rationale,
        })
    rows.sort(key=lambda r: r['composite_score'], reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _build_funnel_section(summary: Dict[str, Any],
                          selected_count: int) -> str:
    """Section 1: linear funnel from full corpus through Stage 7."""
    stages = summary.get('stages', {}) or {}
    tests = summary.get('tests', {}) or {}

    full_corpus = _safe_int(tests.get('full_corpus_count'), 0)
    s4 = _safe_int(stages.get('stage4_recommended_tests'), 0)
    s5 = _safe_int(stages.get('stage5_refined_tests'), 0)
    s6 = _safe_int(stages.get('stage6_final_tests'), 0)

    s7_summary = stages.get('stage7_judged_tests', {}) or {}
    s7_direct = _safe_int(s7_summary.get('DIRECT'), 0)
    s7_indirect = _safe_int(s7_summary.get('INDIRECT'), 0)
    s7_total = s7_direct + s7_indirect
    if s7_total == 0:
        s7_total = selected_count  # fall back to enumerated rows

    steps: List[Tuple[str, int, str]] = []
    if full_corpus:
        steps.append(('Full Corpus', full_corpus, 'Total tests in IDF library'))
    steps.append(('Stage 4', s4, 'Flow correlation'))
    steps.append(('Stage 5', s5, 'Refinement'))
    steps.append(('Stage 6', s6, 'Aggressive suppression'))
    steps.append((
        'Stage 7',
        s7_total,
        f'LLM judgment (DIRECT + INDIRECT only) - {s7_direct} DIRECT, {s7_indirect} INDIRECT',
    ))

    items_html: List[str] = []
    for idx, (label, count, note) in enumerate(steps):
        items_html.append(
            f'<li class="funnel-step">'
            f'<div class="funnel-label">{_esc(label)}</div>'
            f'<div class="funnel-count">{count:,} tests</div>'
            f'<div class="funnel-note">{_esc(note)}</div>'
            f'</li>'
        )
        if idx != len(steps) - 1:
            items_html.append('<li class="funnel-arrow" aria-hidden="true">&darr;</li>')

    return (
        '<section id="funnel">'
        '<h2>1. Funnel</h2>'
        '<ol class="funnel">'
        + ''.join(items_html) +
        '</ol>'
        '</section>'
    )


def _build_flows_section(rows: List[Dict[str, Any]]) -> Tuple[str, Dict[str, int], Dict[str, int]]:
    """Section 2: nested table of flows -> scenarios -> test counts.

    A test that maps to multiple flows is counted under each of its flows
    (intentional - it exercises every flow it touches). Per-flow totals
    deduplicate tests that match multiple scenarios within the same flow.
    """
    flow_buckets: "OrderedDict[str, OrderedDict[str, List[str]]]" = OrderedDict()

    for row in rows:
        flows = row['flows'] or ['(no matched flow)']
        scenarios = row['scenarios'] or ['(no scenario)']
        for flow in flows:
            if flow not in flow_buckets:
                flow_buckets[flow] = OrderedDict()
            for scen in scenarios:
                flow_buckets[flow].setdefault(scen, []).append(row['issue_key'])

    flow_totals: Dict[str, int] = {}
    for flow, scen_map in flow_buckets.items():
        unique_tests = set()
        for scen, keys in scen_map.items():
            unique_tests.update(keys)
        flow_totals[flow] = len(unique_tests)

    # Distribution-level scenario totals: count DISTINCT tests per scenario
    # across the whole row set (not per flow). Without this dedup, a test
    # tagged to N flows would inflate its scenario by N.
    scenario_test_keys: Dict[str, set] = defaultdict(set)
    for row in rows:
        scenarios = row['scenarios'] or ['(no scenario)']
        for scen in scenarios:
            scenario_test_keys[scen].add(row['issue_key'])
    scenario_totals: Dict[str, int] = {k: len(v) for k, v in scenario_test_keys.items()}

    sorted_flows = sorted(flow_buckets.items(),
                          key=lambda kv: flow_totals.get(kv[0], 0),
                          reverse=True)

    rows_html: List[str] = []
    for flow, scen_map in sorted_flows:
        sorted_scenarios = sorted(scen_map.items(),
                                  key=lambda kv: len(set(kv[1])),
                                  reverse=True)
        flow_total = flow_totals.get(flow, 0)
        rowspan = len(sorted_scenarios) + 1  # +1 for the total row
        first = True
        for scen, keys in sorted_scenarios:
            count = len(set(keys))
            if first:
                rows_html.append(
                    f'<tr class="flow-first">'
                    f'<td rowspan="{rowspan}" class="flow-name">{_esc(flow)}</td>'
                    f'<td>{_esc(scen)}</td>'
                    f'<td class="num">{count}</td>'
                    f'</tr>'
                )
                first = False
            else:
                rows_html.append(
                    f'<tr>'
                    f'<td>{_esc(scen)}</td>'
                    f'<td class="num">{count}</td>'
                    f'</tr>'
                )
        rows_html.append(
            f'<tr class="flow-total">'
            f'<td colspan="2">Total for {_esc(flow)}</td>'
            f'<td class="num"><strong>{flow_total}</strong></td>'
            f'</tr>'
        )

    table_html = (
        '<table class="data-table">'
        '<thead><tr>'
        '<th>Flow Name</th>'
        '<th>Scenario</th>'
        '<th class="num">Test Count</th>'
        '</tr></thead>'
        '<tbody>' + ''.join(rows_html) + '</tbody>'
        '</table>'
    )

    section = (
        '<section id="flows">'
        '<h2>2. Flows &amp; Scenarios</h2>'
        '<p class="hint">A test that maps to multiple flows is counted under each. '
        'Per-flow totals deduplicate tests that match multiple scenarios within the same flow.</p>'
        + table_html +
        '</section>'
    )
    return section, flow_totals, dict(scenario_totals)


def _build_distribution_section(rows: List[Dict[str, Any]],
                                flow_totals: Dict[str, int],
                                scenario_totals: Dict[str, int]) -> str:
    """Section 3: cost / distribution breakdown."""
    total_tests = len(rows)

    flow_items: List[str] = []
    for flow, count in sorted(flow_totals.items(), key=lambda kv: kv[1], reverse=True):
        pct = (count / total_tests * 100.0) if total_tests else 0.0
        flow_items.append(
            f'<li>{_esc(flow)}: <strong>{count}</strong> tests '
            f'<span class="muted">({pct:.1f}%)</span></li>'
        )

    scenario_items: List[str] = []
    for scen, count in sorted(scenario_totals.items(), key=lambda kv: kv[1], reverse=True):
        pct = (count / total_tests * 100.0) if total_tests else 0.0
        scenario_items.append(
            f'<li>{_esc(scen)}: <strong>{count}</strong> tests '
            f'<span class="muted">({pct:.1f}%)</span></li>'
        )

    return (
        '<section id="distribution">'
        '<h2>3. Test Distribution</h2>'
        f'<p>Total recommended tests: <strong>{total_tests}</strong></p>'
        '<div class="dist-grid">'
        '<div>'
        '<h3>By Flow</h3>'
        '<ul class="dist-list">' + (''.join(flow_items) or '<li class="muted">No flows</li>') + '</ul>'
        '</div>'
        '<div>'
        '<h3>By Scenario</h3>'
        '<ul class="dist-list">' + (''.join(scenario_items) or '<li class="muted">No scenarios</li>') + '</ul>'
        '</div>'
        '</div>'
        '</section>'
    )


def _build_test_table_section(rows: List[Dict[str, Any]]) -> str:
    """Section 4: full test cases table sorted by composite_score desc."""
    body_rows: List[str] = []
    for row in rows:
        flows_txt = ', '.join(row['flows']) if row['flows'] else '-'
        scenarios_txt = ', '.join(row['scenarios']) if row['scenarios'] else '-'
        verdict_class = 'verdict-direct' if row['verdict'] == 'DIRECT' else 'verdict-indirect'
        body_rows.append(
            '<tr>'
            f'<td class="mono">{_esc(row["issue_key"])}</td>'
            f'<td>{_esc(row["summary"])}</td>'
            f'<td>{_esc(flows_txt)}</td>'
            f'<td>{_esc(scenarios_txt)}</td>'
            f'<td class="num">{row["composite_score"]:.2f}</td>'
            f'<td><span class="verdict-pill {verdict_class}">{_esc(row["verdict"])}</span> '
            f'{_esc(row["rationale"])}</td>'
            '</tr>'
        )

    table_html = (
        '<table class="data-table tests-table">'
        '<thead><tr>'
        '<th>Test Case ID</th>'
        '<th>Test Case Summary</th>'
        '<th>Functional Flows</th>'
        '<th>Scenarios</th>'
        '<th class="num">Total Score</th>'
        '<th>Rationale</th>'
        '</tr></thead>'
        '<tbody>' + (''.join(body_rows) or '<tr><td colspan="6" class="muted">No tests</td></tr>') +
        '</tbody>'
        '</table>'
    )

    return (
        '<section id="tests">'
        f'<h2>4. Test Cases ({len(rows)})</h2>'
        '<p class="hint">All DIRECT and INDIRECT tests from Stage 7, sorted by composite score (descending).</p>'
        + table_html +
        '</section>'
    )


# ---------------------------------------------------------------------------
# CSS / page shell
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg: #0d1117;
  --bg-alt: #161b22;
  --card: #161b22;
  --card2: #1c2129;
  --border: #30363d;
  --fg: #e6edf3;
  --text: #e6edf3;
  --muted: #8b949e;
  --accent: #58a6ff;
  --green: #3fb950;
  --yellow: #d29922;
  --red: #f85149;
  --orange: #f0883e;
  --purple: #bc8cff;
  --cyan: #39d2c0;
  --indirect: #f0883e;
  --direct: #3fb950;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  font-family: 'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, Helvetica, Arial, sans-serif;
  color: var(--fg);
  background: var(--bg);
  line-height: 1.5;
  font-size: 14px;
}

header.report-header {
  padding: 28px 32px;
  border-bottom: 1px solid var(--border);
  background: linear-gradient(135deg, #161b22 0%, #0d1117 100%);
}

header.report-header h1 {
  margin: 0 0 6px 0;
  font-size: 1.75rem;
  color: var(--accent);
}

header.report-header .meta {
  color: var(--muted);
  font-size: 13px;
  margin-top: 2px;
}

main {
  max-width: 1200px;
  margin: 0 auto;
  padding: 28px 32px 56px;
}

section {
  margin-bottom: 36px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 22px 24px;
}

h2 {
  margin: 0 0 14px 0;
  font-size: 1.25rem;
  color: var(--accent);
  border-bottom: 1px solid var(--border);
  padding-bottom: 8px;
}

h3 {
  margin: 0 0 8px 0;
  font-size: 15px;
  color: var(--text);
}

p.hint {
  color: var(--muted);
  margin: 4px 0 12px;
  font-size: 13px;
}

/* Funnel */
.funnel {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}

.funnel-step {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 18px;
  width: 100%;
  max-width: 480px;
  background: var(--card2);
  text-align: center;
  color: var(--text);
}

.funnel-step .funnel-label {
  font-weight: 600;
  color: var(--accent);
}

.funnel-step .funnel-count {
  font-size: 18px;
  margin-top: 4px;
  font-weight: 700;
  color: var(--text);
}

.funnel-step .funnel-note {
  color: var(--muted);
  font-size: 12px;
  margin-top: 4px;
}

.funnel-arrow {
  list-style: none;
  font-size: 18px;
  color: var(--muted);
}

/* Tables */
.data-table {
  border-collapse: collapse;
  width: 100%;
  font-size: 13px;
  background: var(--card);
  color: var(--text);
}

.data-table th, .data-table td {
  border: 1px solid var(--border);
  padding: 8px 12px;
  vertical-align: top;
  text-align: left;
}

.data-table th {
  background: var(--card2);
  color: var(--accent);
  font-weight: 600;
  text-transform: uppercase;
  font-size: 12px;
  letter-spacing: 0.4px;
}

.data-table td.num, .data-table th.num {
  text-align: right;
  white-space: nowrap;
}

/* Zebra striping */
.data-table tbody tr:nth-child(even) td {
  background: rgba(255, 255, 255, 0.02);
}

.data-table tbody tr:hover td {
  background: rgba(88, 166, 255, 0.06);
}

.data-table tr.flow-total td {
  background: rgba(88, 166, 255, 0.08) !important;
  font-weight: 600;
  color: var(--accent);
}

.data-table .flow-name {
  background: var(--card2) !important;
  color: var(--text);
  font-weight: 600;
  vertical-align: top;
}

.tests-table td:nth-child(2) {
  min-width: 220px;
}

.tests-table td:nth-child(6) {
  min-width: 280px;
}

.mono {
  font-family: 'Fira Code', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  white-space: nowrap;
  color: var(--cyan);
}

.muted {
  color: var(--muted);
}

/* Distribution */
.dist-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
}

.dist-list {
  margin: 0;
  padding-left: 20px;
  color: var(--text);
}

.dist-list li {
  margin: 4px 0;
}

.dist-list li strong {
  color: var(--accent);
}

/* Verdict pills */
.verdict-pill {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 700;
  margin-right: 4px;
}

.verdict-pill.verdict-direct {
  background: #063a1f;
  color: #3fb950;
  border: 1px solid #1a7f37;
}

.verdict-pill.verdict-indirect {
  background: #3a2a00;
  color: #f0883e;
  border: 1px solid #b08800;
}

@media (max-width: 720px) {
  main { padding: 16px; }
  .dist-grid { grid-template-columns: 1fr; }
  header.report-header { padding: 18px; }
  .data-table { font-size: 12px; }
  section { padding: 16px; }
}

/* RIA Phase 2 - Dependency-changes section */
#dep-changes .dep-summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
  margin: 12px 0;
}
#dep-changes .dep-summary > div {
  background: var(--card2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 14px;
}
#dep-changes .dep-critical {
  background: rgba(248, 81, 73, 0.12);
  border: 1px solid var(--red);
  border-radius: 6px;
  padding: 10px 14px;
  margin: 8px 0;
}
#dep-changes table.dep-table {
  width: 100%;
  border-collapse: collapse;
  margin: 8px 0 12px;
  font-size: 13px;
}
#dep-changes table.dep-table th,
#dep-changes table.dep-table td {
  border: 1px solid var(--border);
  padding: 8px 10px;
  text-align: left;
  vertical-align: top;
}
#dep-changes table.dep-table th {
  background: var(--card2);
  color: var(--muted);
  font-weight: 600;
}
#dep-changes .dep-row {
  margin-bottom: 4px;
}
#dep-changes .dep-strat {
  color: var(--accent);
  font-weight: 600;
  margin-left: 4px;
}
#dep-changes .dep-score {
  color: var(--muted);
  margin-left: 4px;
}
#dep-changes h3.dep-subhead {
  margin-top: 16px;
  color: var(--muted);
  font-size: 14px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

/* Combined report — summary box, section wrappers, final recs */
section.summary-box {
  background: linear-gradient(135deg, rgba(88, 166, 255, 0.08) 0%, rgba(88, 166, 255, 0.02) 100%);
  border: 2px solid var(--accent);
}

section.summary-box h2 {
  border-bottom-color: var(--accent);
}

section.summary-box .big-num {
  color: var(--accent);
  font-size: 1.6rem;
  font-weight: 700;
}

section.summary-box ul.summary-list {
  margin: 8px 0 0;
  padding-left: 20px;
}

section.summary-box ul.summary-list li {
  margin: 4px 0;
}

section.code-changes-section h2,
section.dep-changes-section h2 {
  color: var(--accent);
}

section.code-changes-section {
  border-left: 4px solid var(--green);
}

section.dep-changes-section {
  border-left: 4px solid var(--orange);
}

.subsection {
  margin: 12px 0 18px;
}

.subsection h3 {
  color: var(--accent);
  font-size: 14px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-top: 18px;
}

section.final-recommendations {
  background: rgba(240, 136, 62, 0.08);
  border: 2px solid var(--orange);
}

section.final-recommendations h2 {
  color: var(--orange);
  border-bottom-color: var(--orange);
}

section.final-recommendations pre {
  background: #0a0d12;
  color: var(--green);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 14px 16px;
  margin: 8px 0 6px;
  overflow-x: auto;
  font-family: 'Fira Code', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-all;
}
"""


def _render_html(title: str,
                 generated_at: str,
                 method_summary: str,
                 summary_box_html: str,
                 code_section_html: str,
                 dep_section_html: str,
                 final_rec_html: str,
                 version: str = '') -> str:
    """Stitch the combined report into a complete HTML document.

    Layout (top to bottom):
      header -> summary box -> Section 1 (code changes) ->
      Section 2 (dependency changes; empty in single-pipeline mode) ->
      final recommendations.
    """
    return (
        '<!DOCTYPE html>\n'
        '<html lang="en">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{_esc(title)}</title>\n'
        f'<style>{_CSS}</style>\n'
        '</head>\n'
        '<body>\n'
        '<header class="report-header">'
        f'<h1>{_esc(title)}</h1>'
        f'<div class="meta">Generated: {_esc(generated_at)}</div>'
        + (f'<div class="meta">Version: {_esc(version)}</div>' if version else '')
        + f'<div class="meta">{_esc(method_summary)}</div>'
        + '</header>\n'
        '<main>\n'
        + summary_box_html
        + code_section_html
        + dep_section_html
        + final_rec_html +
        '</main>\n'
        '</body>\n'
        '</html>\n'
    )


def _method_summary(consolidated: Dict[str, Any], summary: Dict[str, Any]) -> str:
    """Compose a single-line description of the analyzed methods."""
    per_method = consolidated.get('per_method') or summary.get('per_method') or []
    parts: List[str] = []
    for m in per_method:
        cls = m.get('class_name') or ''
        name = m.get('method_name') or ''
        if cls and name:
            parts.append(f"{cls}.{name}()")
        elif name:
            parts.append(f"{name}()")
    if not parts:
        return 'Methods analyzed: (none)'
    return f"Methods analyzed ({len(parts)}): " + ', '.join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_html_report(output_dir: str,
                         output_file: str = 'RIA_Report.html') -> str:
    """Generate the four-section RIA HTML report and write it to disk.

    Returns the absolute path to the written report.
    """
    print('=' * 80)
    print('GENERATING HTML REPORT (combined source + dependency layout)')
    print('=' * 80)
    print(f'Output directory: {output_dir}')

    inputs = _load_inputs(output_dir)
    summary = inputs['summary']
    stage6 = inputs['stage6']  # source pipeline view (or single-pipeline view)
    stage7 = inputs['stage7']
    consolidated = inputs['consolidated']
    dep_summary = inputs.get('dep_summary') or {}
    critical_rec = inputs.get('critical_rec') or {}
    method_understanding = inputs.get('method_understanding') or {}
    impacted_flows = inputs.get('impacted_flows') or {}
    dep_flow_registry = inputs.get('dep_flow_registry') or {}

    # Per-pipeline test sets ------------------------------------------------
    source_tests = _aggressive_tests(inputs.get('source_stage6') or {})
    dep_tests = _aggressive_tests(inputs.get('dep_stage6') or {})
    combined_tests = _combined_tests(
        inputs.get('combined') or {}, source_tests, dep_tests,
    )

    has_dependency_pipeline = (
        bool(dep_tests)
        or bool(dep_summary)
        or bool(critical_rec)
        or bool(dep_flow_registry.get('flows'))
    )

    print(f'Source pipeline tests   : {len(source_tests)}')
    print(f'Dependency pipeline tests: {len(dep_tests)}')
    print(f'Combined union tests     : {len(combined_tests)}')
    print(f'Dependency pipeline run  : {has_dependency_pipeline}')

    # ---- Section 1 inner blocks (existing four-section layout) -----------
    stage6_index = _index_stage6(stage6)
    judgments = _selected_judgments(stage7)
    rows = _build_rows(judgments, stage6_index)

    verdicts_summary = (stage7.get('verdicts_summary') or {}) if isinstance(stage7, dict) else {}
    expected_direct = _safe_int(verdicts_summary.get('DIRECT'), 0)
    expected_indirect = _safe_int(verdicts_summary.get('INDIRECT'), 0)
    expected_total = expected_direct + expected_indirect
    actual_direct = sum(1 for r in rows if r['verdict'] == 'DIRECT')
    actual_indirect = sum(1 for r in rows if r['verdict'] == 'INDIRECT')
    print(f'Stage 7 expected: DIRECT={expected_direct}, INDIRECT={expected_indirect}, '
          f'total={expected_total}')
    print(f'Rows in table  : DIRECT={actual_direct}, INDIRECT={actual_indirect}, '
          f'total={len(rows)}')
    if expected_total and expected_total != len(rows):
        print(f'[WARN] Row count mismatch: expected {expected_total}, got {len(rows)}')

    funnel_html = _build_funnel_section(summary, len(rows))
    flows_html, flow_totals, scenario_totals = _build_flows_section(rows)
    distribution_html = _build_distribution_section(rows, flow_totals, scenario_totals)
    tests_html = _build_test_table_section(rows)
    changed_methods_html = _build_changed_methods_section(
        method_understanding, impacted_flows,
    )

    # ---- Top-of-report summary box --------------------------------------
    summary_box_html = _build_summary_box(
        source_tests=source_tests,
        dep_tests=dep_tests,
        combined_tests=combined_tests,
        method_understanding=method_understanding,
        dep_summary=dep_summary,
        has_dependency_pipeline=has_dependency_pipeline,
    )

    # ---- Section 1: Code Change Analysis (source pipeline) ---------------
    code_section_html = (
        '<section class="code-changes-section" id="code-changes">'
        '<h2>Section 1: Code Change Analysis</h2>'
        + changed_methods_html
        + funnel_html
        + flows_html
        + distribution_html
        + tests_html +
        '</section>'
    )

    # ---- Section 2: Dependency Change Analysis ---------------------------
    if has_dependency_pipeline:
        dep_changed_block = _build_changed_dependencies_section(
            dep_summary, dep_flow_registry,
        )
        # Optional CRITICAL_SUITE banner from the legacy section helper.
        legacy_dep_html = _build_dependency_section(dep_summary, critical_rec)
        dep_tests_table = _build_test_table(
            dep_tests, table_id='dep-tests-table',
            empty_msg='No dependency-pipeline tests recommended.',
        )
        dep_section_html = (
            '<section class="dep-changes-section" id="dependency-changes">'
            '<h2>Section 2: Dependency Change Analysis</h2>'
            + dep_changed_block
            + legacy_dep_html
            + f'<h3>Recommended Tests ({len(dep_tests)})</h3>'
            + dep_tests_table +
            '</section>'
        )
    else:
        dep_section_html = (
            '<section class="dep-changes-section" id="dependency-changes">'
            '<h2>Section 2: Dependency Change Analysis</h2>'
            '<p class="muted"><em>No dependency changes detected in this analysis. '
            'This run was triggered by source-code changes only.</em></p>'
            '</section>'
        )

    # ---- Final recommendations ------------------------------------------
    final_rec_html = _build_final_recommendations_section(
        combined_tests=combined_tests,
        method_understanding=method_understanding,
        dep_summary=dep_summary,
        has_dependency_pipeline=has_dependency_pipeline,
    )

    # Document ----------------------------------------------------------------
    title = 'Regression Impact Analysis Report'
    generated_at = summary.get('generated_at') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    method_summary = _method_summary(consolidated, summary)

    html_doc = _render_html(
        title=title,
        generated_at=generated_at,
        method_summary=method_summary,
        summary_box_html=summary_box_html,
        code_section_html=code_section_html,
        dep_section_html=dep_section_html,
        final_rec_html=final_rec_html,
        version=summary.get('version', ''),
    )

    # Always overwrite the report cleanly. We deliberately do NOT create a
    # `RIA_Report_old.html` backup here: concurrent pipeline runs are
    # prevented by the PID-based lock file (acquire_ria_lock /
    # release_ria_lock in ria_agent.py, lock file at
    # OUTPUT_DIR/.ria_lock), so a stale previous report has no value and
    # was a frequent source of user confusion (two near-identical HTML
    # files in RIA_OUTPUT/). Any RIA_Report_old.html left behind by
    # earlier versions is removed at agent startup by
    # cleanup_legacy_report_backup() in ria_agent.py.
    output_path = os.path.join(output_dir, output_file)
    with open(output_path, 'w', encoding='utf-8') as fh:
        fh.write(html_doc)

    print(f'\nHTML report generated: {output_path}')
    print(f'   Source-pipeline tests    : {len(source_tests)}')
    print(f'   Dependency-pipeline tests: {len(dep_tests)}')
    print(f'   Combined (union)         : {len(combined_tests)}')
    print(f'   Stage 7 rows (Section 1) : {len(rows)}')
    print(f'   Flows / Scenarios        : {len(flow_totals)} / {len(scenario_totals)}')

    return output_path


if __name__ == '__main__':
    import sys
    from pathlib import Path

    _default_output_dir = (
        Path(__file__).resolve().parent.parent.parent.parent / '.github' / 'RIA_OUTPUT'
    )
    if not _default_output_dir.exists():
        _default_output_dir = (
            Path(__file__).resolve().parent.parent.parent.parent / 'RIA_OUTPUT'
        )
    out_dir = sys.argv[1] if len(sys.argv) > 1 else str(_default_output_dir)
    out_file = sys.argv[2] if len(sys.argv) > 2 else 'RIA_Report.html'
    generate_html_report(out_dir, out_file)
