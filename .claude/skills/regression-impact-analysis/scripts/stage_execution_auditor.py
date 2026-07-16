#!/usr/bin/env python3
"""
Stage Execution Auditor (RIA v7.5 - Unified)
============================================

Validates each pipeline stage's pre-conditions, captured output, and
post-conditions. Detects issues, asks the user for approval, and
optionally auto-fixes the most common KB-completeness defects by
rebuilding the affected artifact and re-running downstream stages.

Stages covered:
  0  Knowledge Base build
  4  Test correlation
  5  Test refinement
  6  Aggressive suppression
  7  HTML report

DESIGN PRINCIPLES
-----------------
1. Non-invasive detection. Validation never stops the pipeline. Every
   audit hook runs even if a previous hook failed, so the report
   contains EVERY issue found in one run.

2. Approval-based fixes. Auto-fix is OFF by default. The orchestrator
   accepts ``apply_fixes`` in {"no", "prompt", "yes"}:
     - "no"      -> detection only, no fixes
     - "prompt"  -> ask the user (y/N) before applying fixes (DEFAULT)
     - "yes"     -> apply fixes unattended

3. Granular auto-repair. Each issue carries a ``fix`` identifier which
   maps to a single rebuild action (rebuild_synonym_groups,
   rebuild_component_map, rebuild_flow_dependencies, ...). Fixes are
   deduplicated by (kind, target).

4. Re-run from failed stage. After fixes are applied, the orchestrator
   re-runs the affected stage(s) and downstream stages, then re-audits
   to produce a before/after comparison.

5. Transparent reporting. Two artifacts are written:
     - validation_audit_report.md (human-readable Markdown)
     - audit_report.json          (machine-readable for tooling)
   When fixes are applied, an additional file is written:
     - audit_report_before_after.json

PUBLIC API
----------
    audit_full_pipeline(repo_root, output_dir, ...,
                        apply_fixes="prompt"|"yes"|"no",
                        rerun_pipeline=callable_or_None)
        -> dict (issues, fixes_applied, before/after, status)

    generate_audit_report(stage_results, report_path)
        -> Path                # writes Markdown report

    write_audit_report_json(issues, output_dir) -> Path
    write_before_after_report(before, after, output_dir) -> Path
    print_audit_report(issues) / print_before_after_summary(before, after)

Stand-alone CLI (for debugging):
    python3 stage_execution_auditor.py --full-pipeline
    python3 stage_execution_auditor.py --stage 0 --auto-fix
    python3 stage_execution_auditor.py --report-only
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Locate sibling scripts and configs (no PYTHONPATH magic).
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent

if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))

try:
    from configs.ria_config import (  # type: ignore
        RIA_OUTPUT_DIR,
        TC_DATA_PATH,
        REPO_ROOT,
    )
except Exception as e:  # pragma: no cover - defensive
    RIA_OUTPUT_DIR = os.environ.get(
        'RIA_OUTPUT_DIR',
        str(Path('.github/RIA_OUTPUT').resolve()),
    )
    TC_DATA_PATH = os.environ.get(
        'TC_DATA_PATH',
        str(Path('.github/RIA_INPUT/all_tcs_extracted_enriched.json')),
    )
    REPO_ROOT = os.environ.get('REPO_ROOT', os.getcwd())
    print(
        f"[auditor] WARNING: configs/ria_config.py unavailable ({e}); "
        f"using environment fallbacks.",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------
PASS = 'PASS'
FAIL = 'FAIL'
FIXED = 'FIXED'
SKIP = 'SKIP'

# Severities (used by the issue-level model surfaced in JSON / before-after).
SEVERITY_INFO = 'INFO'
SEVERITY_WARN = 'WARN'
SEVERITY_ERROR = 'ERROR'
SEVERITY_CRITICAL = 'CRITICAL'  # Used by deep-content validation for outcomes

# Auto-fix kinds. Each maps to one branch in apply_fix().
FIX_NONE = 'none'
FIX_REBUILD_KB = 'rebuild_kb'
FIX_REBUILD_SYNONYM_GROUPS = 'rebuild_synonym_groups'
FIX_REBUILD_COMPONENT_MAP = 'rebuild_component_map'
FIX_REBUILD_FLOW_REGISTRY = 'rebuild_flow_registry'
FIX_REBUILD_FLOW_DEPENDENCIES = 'rebuild_flow_dependencies'
FIX_REBUILD_DISCOVERED_VOCAB = 'rebuild_discovered_vocabularies'
FIX_REBUILD_IDF = 'rebuild_idf_index'
FIX_REBUILD_EMBEDDINGS = 'rebuild_embeddings'
FIX_REBUILD_FOCUSED_KB = 'rebuild_focused_kb'
FIX_REGENERATE_HTML = 'regenerate_html_report'
FIX_RERUN_STAGE = 'rerun_stage'
FIX_RERUN_STAGE1 = 'rerun_stage1'
FIX_RERUN_STAGE2 = 'rerun_stage2'
FIX_RERUN_STAGE3 = 'rerun_stage3'
FIX_RERUN_STAGE5 = 'rerun_stage5'
FIX_RERUN_STAGE6 = 'rerun_stage6'

# Fix-kind -> earliest pipeline stage that must re-run after the fix.
# Used to decide what subset of the pipeline to re-execute.
FIX_TO_RERUN_FROM_STAGE: Dict[str, int] = {
    FIX_REBUILD_KB: 0,
    FIX_REBUILD_SYNONYM_GROUPS: 0,
    FIX_REBUILD_COMPONENT_MAP: 0,
    FIX_REBUILD_DISCOVERED_VOCAB: 0,
    FIX_REBUILD_IDF: 0,
    FIX_REBUILD_EMBEDDINGS: 0,
    FIX_REBUILD_FLOW_REGISTRY: 4,
    FIX_REBUILD_FLOW_DEPENDENCIES: 4,
    FIX_REBUILD_FOCUSED_KB: 1,
    FIX_RERUN_STAGE: 4,
    FIX_RERUN_STAGE1: 1,
    FIX_RERUN_STAGE2: 2,
    FIX_RERUN_STAGE3: 3,
    FIX_RERUN_STAGE5: 5,
    FIX_RERUN_STAGE6: 6,
    FIX_REGENERATE_HTML: 7,
    FIX_NONE: 99,
}


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------
@dataclass
class Check:
    """A single validation check within a stage audit."""
    name: str
    status: str  # PASS / FAIL / SKIP
    detail: str = ''


@dataclass
class AuditIssue:
    """A flat, JSON-serializable issue (used for cross-stage reporting,
    deduplication, and the before/after comparison)."""
    stage: str
    severity: str
    type: str
    message: str
    target: str = ''
    fix: str = FIX_NONE
    fix_command: str = ''
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StageResult:
    """Result envelope for one stage's audit pass."""
    stage: int
    label: str
    status: str = PASS  # PASS / FAIL / FIXED / SKIP
    checks: List[Check] = field(default_factory=list)
    issues: List[AuditIssue] = field(default_factory=list)
    fixes_applied: List[str] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0

    def add_check(self, check: Check, issue: Optional[AuditIssue] = None) -> None:
        """Record a check; optionally attach a structured issue when FAIL."""
        self.checks.append(check)
        if check.status == FAIL:
            if self.status != FAIL:
                self.status = FAIL
            if issue is not None:
                self.issues.append(issue)
            else:
                # Synthesize a minimal issue so reports stay consistent.
                self.issues.append(AuditIssue(
                    stage=f'stage{self.stage}',
                    severity=SEVERITY_ERROR,
                    type=check.name,
                    message=check.detail or check.name,
                ))

    def to_dict(self) -> Dict[str, Any]:
        return {
            'stage': self.stage,
            'label': self.label,
            'status': self.status,
            'duration_s': round(self.duration_s, 3),
            'checks': [
                {'name': c.name, 'status': c.status, 'detail': c.detail}
                for c in self.checks
            ],
            'issues': [i.to_dict() for i in self.issues],
            'fixes_applied': list(self.fixes_applied),
            'metrics': self.metrics,
        }


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------
def _read_json(path: Path) -> Tuple[bool, Any, str]:
    """Return (ok, payload, error_message). Never raises."""
    if not path.exists():
        return False, None, f"file does not exist: {path}"
    try:
        with path.open('r', encoding='utf-8') as fh:
            data = json.load(fh)
        return True, data, ''
    except json.JSONDecodeError as e:
        return False, None, f"invalid JSON: {e}"
    except OSError as e:
        return False, None, f"unreadable: {e}"


def _safe_load_json(path: Path) -> Optional[Dict[str, Any]]:
    """Best-effort JSON load that never raises and returns None on failure."""
    ok, data, _ = _read_json(path)
    return data if ok else None


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _run(cmd: List[str], cwd: Optional[Path] = None,
         timeout: int = 1800) -> Tuple[int, str, str]:
    """Invoke a subprocess and capture stdout/stderr. Never raises."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, '', f"timeout after {timeout}s"
    except FileNotFoundError as e:
        return 127, '', f"command not found: {e}"
    except Exception as e:  # pragma: no cover
        return 1, '', f"unexpected error: {e}"


def _kb_dir(audit_output: Path) -> Path:
    return audit_output / 'knowledge_base'


def _build_method_to_component_map(kb_dir: Path) -> Dict[str, set]:
    """Build a reverse index ``method_name -> {owning_component_name}`` from
    ``component_map.json``. The map may contain entries shaped as either
    ``{component_name, methods: [{method_name, ...}, ...]}`` or
    ``{components: [{component_name, methods: [...]}, ...]}``. We accept
    both shapes plus a flat ``method_name -> component`` shape, and we
    fold method-name collisions into a set so a single method that lives
    in multiple components still resolves correctly.
    """
    out: Dict[str, set] = {}
    cm_path = Path(kb_dir) / 'component_map.json'
    payload = _safe_load_json(cm_path) or {}
    if not isinstance(payload, dict):
        return out

    def _add(method_name: Any, comp_name: Any) -> None:
        if not method_name or not comp_name:
            return
        m = str(method_name).strip()
        c = str(comp_name).strip()
        if not m or not c:
            return
        out.setdefault(m, set()).add(c)

    components = payload.get('components')
    if isinstance(components, list):
        for entry in components:
            if not isinstance(entry, dict):
                continue
            comp_name = (entry.get('component_name')
                         or entry.get('component')
                         or entry.get('name'))
            for meth in entry.get('methods') or []:
                if isinstance(meth, dict):
                    _add(meth.get('method_name')
                         or meth.get('method')
                         or meth.get('name'),
                         comp_name)
                else:
                    _add(meth, comp_name)
    elif isinstance(components, dict):
        # Map keyed by component name -> {methods: [...]}
        for comp_name, entry in components.items():
            if isinstance(entry, dict):
                for meth in entry.get('methods') or []:
                    if isinstance(meth, dict):
                        _add(meth.get('method_name')
                             or meth.get('method')
                             or meth.get('name'),
                             comp_name)
                    else:
                        _add(meth, comp_name)

    # Some builders emit a flat top-level method index too.
    flat = payload.get('method_to_component')
    if isinstance(flat, dict):
        for m, c in flat.items():
            if isinstance(c, str):
                _add(m, c)
            elif isinstance(c, list):
                for cc in c:
                    _add(m, cc)
    return out


def _extract_method_name_from_trigger(entry: Any) -> Optional[str]:
    """Parse one ``triggered_by_methods`` entry into a bare method name.

    Accepts dict shapes (``{"method": "foo", ...}``) and string shapes
    (``"foo"`` or ``"foo (in path/to/File.java)"``). Returns None if the
    entry can't be parsed.
    """
    if isinstance(entry, dict):
        v = entry.get('method') or entry.get('method_name') or entry.get('name')
        return str(v).strip() if v else None
    if isinstance(entry, str):
        s = entry.strip()
        # Strip trailing " (in <file>)" annotation if present.
        if '(' in s:
            s = s.split('(', 1)[0].strip()
        return s or None
    return None


def _build_flow_to_component_map(kb_dir: Path) -> Dict[str, set]:
    """Build a reverse index ``flow_id/flow_name -> {component_name}`` from
    ``flow_dependencies.json``. The file's canonical schema is

        {"flow_dependencies": [{"flow_id": ..., "flow_name": ...,
                                 "component": ...}, ...]}

    A few legacy variants use ``dependencies`` instead of
    ``flow_dependencies`` and/or list multiple components per entry; we
    accept both and fold every (flow, component) pair into the index.
    """
    out: Dict[str, set] = {}
    fd_path = Path(kb_dir) / 'flow_dependencies.json'
    payload = _safe_load_json(fd_path) or {}
    if not isinstance(payload, dict):
        return out
    entries = (payload.get('flow_dependencies')
               or payload.get('dependencies')
               or [])
    if not isinstance(entries, list):
        return out

    def _add(flow_key: Any, comp: Any) -> None:
        if not flow_key or not comp:
            return
        k = str(flow_key).strip()
        c = str(comp).strip()
        if not k or not c:
            return
        out.setdefault(k, set()).add(c)

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # The KB build uses ``flow`` as the flow label; older builds used
        # ``flow_id`` / ``flow_name``. Fold all of them so both legacy and
        # current KB shapes are recognized.
        flow_keys = [
            entry.get('flow'),
            entry.get('flow_id'),
            entry.get('flow_name'),
            entry.get('id'),
            entry.get('name'),
        ]
        comp_field = entry.get('component')
        comps_field = entry.get('components')
        comp_values: List[Any] = []
        if isinstance(comp_field, str):
            comp_values.append(comp_field)
        elif isinstance(comp_field, list):
            comp_values.extend(comp_field)
        if isinstance(comps_field, list):
            comp_values.extend(comps_field)
        for c in comp_values:
            for fk in flow_keys:
                _add(fk, c)
    return out


def _collect_covered_components(
    tests: List[Any],
    method_to_components: Dict[str, set],
    flow_to_components: Optional[Dict[str, set]] = None,
) -> set:
    """Return the set of component names covered by the given test list.

    Coverage signals (any one is sufficient):
      1. Direct fields: ``component``, ``owning_component``,
         ``matched_components`` on the test record itself.
      2. Indirect via ``triggered_by_methods``: each entry is resolved
         through ``method_to_components`` so a test that lists changed
         method names without a direct component field still counts as
         coverage for the owning component(s).
      3. Indirect via ``matched_flows`` / ``matched_flow`` /
         ``flow_tags`` / ``flows``: each flow id or flow name is resolved
         through ``flow_to_components`` (if provided) so a Stage 4 test
         that only carries flow tags is still counted as covering the
         component(s) those flows belong to.
    """
    covered: set = set()
    if not isinstance(tests, list):
        return covered
    flow_to_components = flow_to_components or {}
    for t in tests:
        if not isinstance(t, dict):
            continue
        # 1. Direct component fields.
        for k in ('component', 'owning_component', 'matched_components'):
            v = t.get(k)
            if isinstance(v, str):
                covered.add(v)
            elif isinstance(v, list):
                covered.update(str(x) for x in v if x)
        # 2. Indirect via triggered_by_methods.
        triggers = t.get('triggered_by_methods')
        if isinstance(triggers, list):
            for entry in triggers:
                m = _extract_method_name_from_trigger(entry)
                if not m:
                    continue
                comps = method_to_components.get(m)
                if comps:
                    covered.update(comps)
        # 3. Indirect via matched flows.
        if flow_to_components:
            flow_keys: List[str] = []
            for fk in ('matched_flows', 'flow_tags', 'flows'):
                v = t.get(fk)
                if isinstance(v, str):
                    flow_keys.append(v)
                elif isinstance(v, list):
                    for x in v:
                        if isinstance(x, str):
                            flow_keys.append(x)
                        elif isinstance(x, dict):
                            for sub in ('flow_id', 'flow_name', 'id', 'name'):
                                if x.get(sub):
                                    flow_keys.append(str(x.get(sub)))
            mf = t.get('matched_flow')
            if isinstance(mf, str):
                flow_keys.append(mf)
            elif isinstance(mf, dict):
                for sub in ('flow_id', 'flow_name', 'id', 'name'):
                    if mf.get(sub):
                        flow_keys.append(str(mf.get(sub)))
            for fk in flow_keys:
                comps = flow_to_components.get(fk)
                if comps:
                    covered.update(comps)
    return covered


def _is_multi_method_mode(audit_output: Path) -> bool:
    """Detect whether the most recent RIA run used multi-method orchestration.

    Multi-method runs are identified by the presence of the per-method
    workspace directory (``multi_method/``) created by
    ``run_multi_method_analysis()`` in ``ria_agent.py``. As a secondary
    signal, ``consolidated_summary.json`` carries ``mode == 'multi_method'``
    and ``stage6_consolidated_tests.json`` carries the same marker. Either
    signal is sufficient — we treat presence of the workspace dir as the
    primary check because it cannot be faked by stale single-method outputs.

    In multi-method mode the orchestrator skips the per-stage Stage 1 / 2 /
    3 files (it consults ``flow_registry.json`` directly), so the auditor
    must NOT flag those files as missing.
    """
    try:
        out = Path(audit_output)
    except Exception:
        return False
    workspace = out / 'multi_method'
    if workspace.exists() and workspace.is_dir():
        return True
    # Fallback: inspect the consolidated summary / stage6 file for an
    # explicit mode marker. This handles environments where the per-method
    # workspace was already cleaned up.
    for marker in ('consolidated_summary.json',
                   'stage6_consolidated_tests.json',
                   'stage6_aggressive_tests.json'):
        payload = _safe_load_json(out / marker) or {}
        if isinstance(payload, dict) and payload.get('mode') == 'multi_method':
            return True
    return False


def _print_section(title: str) -> None:
    print()
    print('-' * 80)
    print(title)
    print('-' * 80)


# ---------------------------------------------------------------------------
# Stage 0: Knowledge Base build
# ---------------------------------------------------------------------------
KB_REQUIRED_FILES = (
    'synonym_groups.json',
    'component_map.json',
    'flow_registry.json',
    'flow_dependencies.json',
)

KB_OPTIONAL_FILES = (
    ('discovered_framework_suffixes.json', FIX_REBUILD_DISCOVERED_VOCAB,
     'python3 build_discovered_vocabularies.py --only framework_suffixes'),
    ('discovered_generic_nouns.json', FIX_REBUILD_DISCOVERED_VOCAB,
     'python3 build_discovered_vocabularies.py --only vocabularies'),
    ('domain_vocabulary.json', FIX_REBUILD_DISCOVERED_VOCAB,
     'python3 build_discovered_vocabularies.py --only vocabularies'),
    ('codebase_vocabulary.json', FIX_REBUILD_DISCOVERED_VOCAB,
     'python3 extract_diff_concepts.py --build-vocab'),
    ('idf_index.json', FIX_REBUILD_IDF, 'python3 term_idf.py'),
    ('embeddings_index.npz', FIX_REBUILD_EMBEDDINGS,
     'python3 build_embeddings.py'),
)


def _validate_synonym_groups(payload: Dict[str, Any], target: str
                             ) -> Tuple[List[Check], List[AuditIssue]]:
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    groups = payload.get('synonym_groups') or payload.get('groups') or {}
    total = payload.get('total_groups')
    coverage = payload.get('clustering_coverage_pct', 0)
    extracted = payload.get('total_verbs_extracted', 0)

    if not isinstance(groups, dict) or not groups:
        c = Check('synonym_groups.synonym_groups', FAIL,
                  'synonym_groups payload is empty')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_synonym_groups_empty',
            message=c.detail, target=target,
            fix=FIX_REBUILD_SYNONYM_GROUPS,
            fix_command='python3 build_synonym_groups.py',
        ))
    else:
        checks.append(Check('synonym_groups.synonym_groups', PASS,
                            f'{len(groups)} groups present'))

    if total is None or total <= 0:
        c = Check('synonym_groups.total_groups', FAIL,
                  f'total_groups missing or zero ({total!r})')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_synonym_total_groups_zero',
            message=c.detail, target=target,
            fix=FIX_REBUILD_SYNONYM_GROUPS,
            fix_command='python3 build_synonym_groups.py',
        ))
    else:
        checks.append(Check('synonym_groups.total_groups', PASS,
                            f'total_groups={total}'))

    if extracted < 100:
        c = Check('synonym_groups.total_verbs_extracted', FAIL,
                  f'verb pool too small ({extracted}); '
                  f'expected >=100 for a real corpus')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_synonym_verbs_too_few',
            message=c.detail, target=target,
            fix=FIX_REBUILD_SYNONYM_GROUPS,
            fix_command='python3 build_synonym_groups.py',
            details={'extracted': extracted},
        ))
    else:
        checks.append(Check('synonym_groups.total_verbs_extracted', PASS,
                            f'extracted={extracted}'))

    try:
        cov = float(coverage)
    except (TypeError, ValueError):
        cov = 0.0
    if cov < 5.0:
        c = Check('synonym_groups.clustering_coverage_pct', FAIL,
                  f'coverage too low ({cov}%); expected >=5%')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_synonym_coverage_low',
            message=c.detail, target=target,
            fix=FIX_REBUILD_SYNONYM_GROUPS,
            fix_command='python3 build_synonym_groups.py',
            details={'coverage_pct': cov},
        ))
    else:
        checks.append(Check('synonym_groups.clustering_coverage_pct', PASS,
                            f'coverage={cov}%'))

    return checks, issues


def _validate_component_map(payload: Dict[str, Any], target: str
                            ) -> Tuple[List[Check], List[AuditIssue], Dict[str, Any]]:
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    components = payload.get('components') or []
    total = payload.get('total_components', len(components))
    metrics['total_components'] = total

    if not components:
        c = Check('component_map.components', FAIL,
                  'components list is empty')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_component_map_empty',
            message=c.detail, target=target,
            fix=FIX_REBUILD_COMPONENT_MAP,
            fix_command='python3 build_component_map.py',
        ))
        return checks, issues, metrics

    checks.append(Check('component_map.components', PASS,
                        f'{total} components indexed'))

    with_keywords = sum(1 for c in components if c.get('keywords'))
    with_methods = sum(1 for c in components if c.get('methods'))
    metrics['components_with_keywords'] = with_keywords
    metrics['components_with_methods'] = with_methods

    pct_kw = 100.0 * with_keywords / max(total, 1)
    pct_m = 100.0 * with_methods / max(total, 1)

    if pct_kw < 80.0:
        c = Check('component_map.keyword_coverage', FAIL,
                  f'only {pct_kw:.1f}% of components have keywords '
                  f'(expected >=80%)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_component_keyword_coverage_low',
            message=c.detail, target=target,
            fix=FIX_REBUILD_COMPONENT_MAP,
            fix_command='python3 build_component_map.py',
            details={'keyword_coverage_pct': round(pct_kw, 1)},
        ))
    else:
        checks.append(Check('component_map.keyword_coverage', PASS,
                            f'{pct_kw:.1f}% of components have keywords'))

    if pct_m < 50.0:
        c = Check('component_map.method_coverage', FAIL,
                  f'only {pct_m:.1f}% of components have methods '
                  f'(expected >=50%)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_component_method_coverage_low',
            message=c.detail, target=target,
            fix=FIX_REBUILD_COMPONENT_MAP,
            fix_command='python3 build_component_map.py',
            details={'method_coverage_pct': round(pct_m, 1)},
        ))
    else:
        checks.append(Check('component_map.method_coverage', PASS,
                            f'{pct_m:.1f}% of components have methods'))

    REQUIRED = ('component_name', 'file_paths', 'file_count')
    missing = []
    for c in components[:200]:
        for f in REQUIRED:
            if f not in c:
                missing.append((c.get('component_name', '?'), f))
                break
    if missing:
        ck = Check('component_map.required_fields', FAIL,
                   f'{len(missing)} components missing required fields '
                   f'(sample: {missing[:3]})')
        checks.append(ck)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_component_required_fields',
            message=ck.detail, target=target,
            fix=FIX_REBUILD_COMPONENT_MAP,
            fix_command='python3 build_component_map.py',
        ))
    else:
        checks.append(Check('component_map.required_fields', PASS,
                            'all sampled components carry required fields'))

    return checks, issues, metrics


def _validate_flow_registry(payload: Dict[str, Any], target: str
                            ) -> Tuple[List[Check], List[AuditIssue], Dict[str, Any]]:
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    flows = payload.get('flows') or []
    metrics['total_flows'] = len(flows)

    if not flows:
        c = Check('flow_registry.flows', FAIL, 'flow list is empty')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_flow_registry_empty',
            message=c.detail, target=target,
            fix=FIX_REBUILD_FLOW_REGISTRY,
            fix_command='python3 build_flow_registry.py',
        ))
        return checks, issues, metrics

    checks.append(Check('flow_registry.flows', PASS,
                        f'{len(flows)} flows registered'))

    bad_eps: List[str] = []
    total_eps = 0
    for flow in flows:
        for ep in flow.get('entry_points', []) or []:
            total_eps += 1
            if not isinstance(ep, str) or ':' not in ep:
                bad_eps.append(ep)
                continue
            file_part, _, method_part = ep.partition(':')
            if not file_part or not method_part:
                bad_eps.append(ep)
    metrics['total_entry_points'] = total_eps
    metrics['malformed_entry_points'] = len(bad_eps)

    if total_eps == 0:
        c = Check('flow_registry.entry_point_format', FAIL,
                  'no entry points across any flow')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_flow_registry_no_entry_points',
            message=c.detail, target=target,
            fix=FIX_REBUILD_FLOW_REGISTRY,
            fix_command='python3 build_flow_registry.py',
        ))
    elif bad_eps:
        c = Check('flow_registry.entry_point_format', FAIL,
                  f'{len(bad_eps)}/{total_eps} entry points malformed '
                  f'(sample: {bad_eps[:3]})')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_flow_registry_malformed_entry_points',
            message=c.detail, target=target,
            fix=FIX_REBUILD_FLOW_REGISTRY,
            fix_command='python3 build_flow_registry.py',
        ))
    else:
        checks.append(Check('flow_registry.entry_point_format', PASS,
                            f'all {total_eps} entry points are file:method'))

    REQUIRED = ('flow_id', 'flow_name', 'entry_points')
    bad_flows: List[Tuple[str, str]] = []
    for f in flows[:50]:
        for k in REQUIRED:
            if k not in f:
                bad_flows.append((f.get('flow_id', '?'), k))
                break
    if bad_flows:
        c = Check('flow_registry.required_fields', FAIL,
                  f'{len(bad_flows)} flows missing fields '
                  f'(sample: {bad_flows[:3]})')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_flow_registry_required_fields',
            message=c.detail, target=target,
            fix=FIX_REBUILD_FLOW_REGISTRY,
            fix_command='python3 build_flow_registry.py',
        ))
    else:
        checks.append(Check('flow_registry.required_fields', PASS,
                            'all sampled flows carry required fields'))

    return checks, issues, metrics


def _validate_flow_dependencies(payload: Dict[str, Any],
                                flow_registry_payload: Optional[Dict[str, Any]],
                                target: str
                                ) -> Tuple[List[Check], List[AuditIssue], Dict[str, Any]]:
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    deps = payload.get('dependencies') or payload.get('flow_dependencies') or []
    metrics['total_dependencies'] = len(deps)

    if not deps:
        c = Check('flow_dependencies.dependencies', FAIL,
                  'dependency list is empty')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_flow_dependencies_empty',
            message=c.detail, target=target,
            fix=FIX_REBUILD_FLOW_DEPENDENCIES,
            fix_command='python3 build_flow_dependencies.py',
        ))
        return checks, issues, metrics

    checks.append(Check('flow_dependencies.dependencies', PASS,
                        f'{len(deps)} dependency pairs'))

    REQUIRED = ('flow', 'component', 'dependency_type')
    bad: List[Any] = []
    flows_seen = set()
    for dep in deps:
        for k in REQUIRED:
            if not dep.get(k):
                bad.append(dep)
                break
        else:
            flows_seen.add(dep['flow'])
    metrics['unique_flows_in_deps'] = len(flows_seen)

    if bad:
        c = Check('flow_dependencies.required_fields', FAIL,
                  f'{len(bad)} entries missing required fields '
                  f'(sample: {bad[:2]})')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_flow_dependencies_required_fields',
            message=c.detail, target=target,
            fix=FIX_REBUILD_FLOW_DEPENDENCIES,
            fix_command='python3 build_flow_dependencies.py',
        ))
    else:
        checks.append(Check('flow_dependencies.required_fields', PASS,
                            'all entries carry flow + component + type'))

    if flow_registry_payload:
        registry_flows = {
            f['flow_name'] for f in (flow_registry_payload.get('flows') or [])
            if isinstance(f, dict) and f.get('flow_name')
        }
        missing = registry_flows - flows_seen
        metrics['flows_without_deps'] = sorted(missing)
        if missing and len(missing) == len(registry_flows):
            c = Check('flow_dependencies.coverage_vs_registry', FAIL,
                      'no flow in flow_registry has any dependency entry')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_ERROR,
                type='stage0_flow_dependencies_no_coverage',
                message=c.detail, target=target,
                fix=FIX_REBUILD_FLOW_DEPENDENCIES,
                fix_command='python3 build_flow_dependencies.py',
            ))
        elif missing:
            checks.append(Check(
                'flow_dependencies.coverage_vs_registry', PASS,
                f'{len(missing)}/{len(registry_flows)} flows lack '
                f'deps (acceptable in focused mode)',
            ))
        else:
            checks.append(Check(
                'flow_dependencies.coverage_vs_registry', PASS,
                'every registry flow has at least one dependency entry',
            ))

    return checks, issues, metrics


def audit_stage_0(audit_output: Optional[Path] = None,
                  change_type: Optional[str] = None) -> StageResult:
    """Audit Stage 0 KB build (detection only).

    ``change_type`` is plumbed through so the deep-content validator can
    inspect the per-pipeline enriched corpus (Option A) instead of the legacy
    single-pipeline file. ``None`` keeps the historical behaviour (legacy
    `all_tcs_extracted_enriched.json`).
    """
    audit_output = Path(audit_output or RIA_OUTPUT_DIR)
    kb = _kb_dir(audit_output)
    res = StageResult(stage=0, label='Knowledge Base build')
    t0 = time.time()

    if not kb.exists():
        res.add_check(
            Check('kb.kb_dir', FAIL, f'KB dir missing: {kb}'),
            AuditIssue(
                stage='stage0', severity=SEVERITY_ERROR,
                type='stage0_kb_dir_missing',
                message=f'KB dir missing: {kb}',
                target=str(kb),
                fix=FIX_REBUILD_KB,
                fix_command='python3 ria_agent.py --rebuild-kb',
            ),
        )
        res.duration_s = time.time() - t0
        return res
    res.add_check(Check('kb.kb_dir', PASS, str(kb)))

    # Required files
    payloads: Dict[str, Any] = {}
    for fname in KB_REQUIRED_FILES:
        path = kb / fname
        ok, payload, err = _read_json(path)
        if not ok:
            res.add_check(
                Check(f'kb.{fname}', FAIL, err),
                AuditIssue(
                    stage='stage0', severity=SEVERITY_ERROR,
                    type='stage0_kb_file_missing_or_invalid',
                    message=f'{fname}: {err}',
                    target=str(path),
                    fix=FIX_REBUILD_KB,
                    fix_command='python3 ria_agent.py --rebuild-kb',
                ),
            )
        else:
            payloads[fname] = payload
            res.add_check(Check(f'kb.{fname}', PASS, 'valid JSON'))

    # Optional/secondary files
    for fname, fix_kind, cmd in KB_OPTIONAL_FILES:
        path = kb / fname
        if not path.exists():
            res.add_check(
                Check(f'kb_optional.{fname}', FAIL,
                      f'optional KB file missing: {fname}'),
                AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_kb_optional_missing',
                    message=f'optional KB file missing: {fname}',
                    target=str(path),
                    fix=fix_kind,
                    fix_command=cmd,
                ),
            )
        elif _file_size(path) == 0:
            res.add_check(
                Check(f'kb_optional.{fname}', FAIL,
                      f'optional KB file empty: {fname}'),
                AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_kb_optional_empty',
                    message=f'optional KB file empty: {fname}',
                    target=str(path),
                    fix=fix_kind,
                    fix_command=cmd,
                ),
            )
        else:
            res.add_check(Check(f'kb_optional.{fname}', PASS, 'present'))

    # Per-file content validation
    if 'synonym_groups.json' in payloads:
        c, isu = _validate_synonym_groups(
            payloads['synonym_groups.json'],
            str(kb / 'synonym_groups.json'),
        )
        for ck in c:
            res.checks.append(ck)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL

    if 'component_map.json' in payloads:
        c, isu, m = _validate_component_map(
            payloads['component_map.json'],
            str(kb / 'component_map.json'),
        )
        for ck in c:
            res.checks.append(ck)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update({f'component_map.{k}': v for k, v in m.items()})

    if 'flow_registry.json' in payloads:
        c, isu, m = _validate_flow_registry(
            payloads['flow_registry.json'],
            str(kb / 'flow_registry.json'),
        )
        for ck in c:
            res.checks.append(ck)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update({f'flow_registry.{k}': v for k, v in m.items()})

    if 'flow_dependencies.json' in payloads:
        c, isu, m = _validate_flow_dependencies(
            payloads['flow_dependencies.json'],
            payloads.get('flow_registry.json'),
            str(kb / 'flow_dependencies.json'),
        )
        for ck in c:
            res.checks.append(ck)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update({f'flow_dependencies.{k}': v for k, v in m.items()})

    # ----- DEEP CONTENT VALIDATION -----------------------------------------
    # Run the content-level validators in addition to the structural ones
    # above. They share the same StageResult so all findings end up in one
    # report. Each call is wrapped to keep audit_stage_0 non-fatal.
    repo_root_path = Path(REPO_ROOT)
    corpus_path = (Path(TC_DATA_PATH) if TC_DATA_PATH
                   else _raw_corpus_path(repo_root_path))

    try:
        c, isu, m = validate_synonym_groups_content(kb, corpus_path)
        res.checks.extend(c)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(Check('synonym_groups.content', FAIL,
                            f'deep validation crashed: {exc}'))

    try:
        c, isu, m = validate_component_map_content(kb, repo_root_path)
        res.checks.extend(c)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(Check('component_map.content', FAIL,
                            f'deep validation crashed: {exc}'))

    try:
        c, isu, m = validate_flow_registry_content(kb, repo_root_path,
                                                   corpus_path)
        res.checks.extend(c)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(Check('flow_registry.content', FAIL,
                            f'deep validation crashed: {exc}'))

    try:
        c, isu, m = validate_flow_dependencies_content(kb)
        res.checks.extend(c)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(Check('flow_dependencies.content', FAIL,
                            f'deep validation crashed: {exc}'))

    # ----- DEEP CONTENT VALIDATION (extended coverage: 12/12 KB files) -----
    # Option A: route the auditor to the per-pipeline enriched corpus
    # (*_source.json / *_dependency.json) when `change_type` is supplied.
    # Falls back to the legacy single-pipeline file otherwise so historical
    # callers continue to validate the canonical artefact.
    enriched_corpus_path = _resolve_enriched_corpus_path(kb, change_type)
    raw_corpus_for_validators = _raw_corpus_path(repo_root_path)

    try:
        c, isu, m = validate_enriched_corpus_content(
            kb, raw_corpus_for_validators, change_type=change_type)
        res.checks.extend(c)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(Check('enriched_corpus.content', FAIL,
                            f'deep validation crashed: {exc}'))

    try:
        c, isu, m = validate_idf_index_content(
            kb, raw_corpus_for_validators)
        res.checks.extend(c)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(Check('idf_index.content', FAIL,
                            f'deep validation crashed: {exc}'))

    try:
        c, isu, m = validate_generic_nouns_content(
            kb, enriched_corpus_path)
        res.checks.extend(c)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(Check('generic_nouns.content', FAIL,
                            f'deep validation crashed: {exc}'))

    try:
        c, isu, m = validate_domain_vocabulary_content(kb)
        res.checks.extend(c)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(Check('domain_vocabulary.content', FAIL,
                            f'deep validation crashed: {exc}'))

    try:
        c, isu, m = validate_embeddings_index_content(
            kb, raw_corpus_for_validators)
        res.checks.extend(c)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(Check('embeddings_index.content', FAIL,
                            f'deep validation crashed: {exc}'))

    try:
        c, isu, m = validate_codebase_vocabulary_content(
            kb, repo_root_path)
        res.checks.extend(c)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(Check('codebase_vocabulary.content', FAIL,
                            f'deep validation crashed: {exc}'))

    try:
        c, isu, m = validate_framework_suffixes_content(
            kb, repo_root_path)
        res.checks.extend(c)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(Check('framework_suffixes.content', FAIL,
                            f'deep validation crashed: {exc}'))

    try:
        c, isu, m = validate_language_reserved_words_content(kb)
        res.checks.extend(c)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(Check('reserved_words.content', FAIL,
                            f'deep validation crashed: {exc}'))

    res.duration_s = time.time() - t0
    return res


# ---------------------------------------------------------------------------
# Stage 1: Call tree analysis
# ---------------------------------------------------------------------------
def audit_stage_1(audit_output: Optional[Path] = None,
                  changed_method: Optional[str] = None,
                  changed_file: Optional[str] = None,
                  repo_root: Optional[Path] = None) -> StageResult:
    """Audit Stage 1 (call tree analysis) output."""
    audit_output = Path(audit_output or RIA_OUTPUT_DIR)
    repo_root = Path(repo_root or REPO_ROOT)
    res = StageResult(stage=1, label='Call tree analysis')
    t0 = time.time()
    # Multi-method runs derive entry points directly from flow_registry.json
    # and never write stage1_entry_points.json. Skip the file-based audit
    # cleanly so the report does not flag a missing artifact that the new
    # architecture intentionally omits.
    if _is_multi_method_mode(audit_output):
        res.status = SKIP
        res.checks.append(Check(
            'stage1.architecture_mode', SKIP,
            'multi-method mode detected (multi_method/ workspace present); '
            'Stage 1 entry points sourced from flow_registry.json'))
        res.metrics['stage1.skipped_reason'] = 'multi_method_mode'
        res.duration_s = time.time() - t0
        return res
    try:
        c, isu, m = validate_stage1_content(audit_output, changed_method,
                                            changed_file, repo_root)
        for ck in c:
            res.checks.append(ck)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(
            Check('stage1.validation', FAIL, f'crashed: {exc}'),
            AuditIssue(stage='stage1', severity=SEVERITY_ERROR,
                       type='stage1_validation_crash', message=str(exc)))
    res.duration_s = time.time() - t0
    return res


# ---------------------------------------------------------------------------
# Stage 2: Flow mapping
# ---------------------------------------------------------------------------
def audit_stage_2(audit_output: Optional[Path] = None,
                  kb_dir: Optional[Path] = None,
                  change_type: Optional[str] = None) -> StageResult:
    """Audit Stage 2 (flow mapping) output."""
    audit_output = Path(audit_output or RIA_OUTPUT_DIR)
    kb_dir = Path(kb_dir or _kb_dir(audit_output))
    res = StageResult(stage=2, label='Flow mapping')
    t0 = time.time()
    # Multi-method runs derive impacted flows from flow_registry.json
    # directly and never write stage2_impacted_flows.json. Skip cleanly.
    if _is_multi_method_mode(audit_output):
        res.status = SKIP
        res.checks.append(Check(
            'stage2.architecture_mode', SKIP,
            'multi-method mode detected (multi_method/ workspace present); '
            'flow mapping sourced from flow_registry.json'))
        res.metrics['stage2.skipped_reason'] = 'multi_method_mode'
        res.duration_s = time.time() - t0
        return res
    try:
        c, isu, m = validate_stage2_content(audit_output, kb_dir, change_type)
        for ck in c:
            res.checks.append(ck)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(
            Check('stage2.validation', FAIL, f'crashed: {exc}'),
            AuditIssue(stage='stage2', severity=SEVERITY_ERROR,
                       type='stage2_validation_crash', message=str(exc)))
    res.duration_s = time.time() - t0
    return res


# ---------------------------------------------------------------------------
# Stage 3: Indirect flows
# ---------------------------------------------------------------------------
def audit_stage_3(audit_output: Optional[Path] = None,
                  kb_dir: Optional[Path] = None,
                  change_type: Optional[str] = None) -> StageResult:
    """Audit Stage 3 (indirect flows) output."""
    audit_output = Path(audit_output or RIA_OUTPUT_DIR)
    kb_dir = Path(kb_dir or _kb_dir(audit_output))
    res = StageResult(stage=3, label='Indirect flows')
    t0 = time.time()
    # Multi-method runs resolve DIRECT/INDIRECT criticality from
    # flow_dependencies.json inline (Stage 4) and never write
    # stage3_indirect_flows.json. Skip cleanly.
    if _is_multi_method_mode(audit_output):
        res.status = SKIP
        res.checks.append(Check(
            'stage3.architecture_mode', SKIP,
            'multi-method mode detected (multi_method/ workspace present); '
            'indirect criticality resolved via flow_dependencies.json'))
        res.metrics['stage3.skipped_reason'] = 'multi_method_mode'
        res.duration_s = time.time() - t0
        return res
    try:
        c, isu, m = validate_stage3_content(audit_output, kb_dir, change_type)
        for ck in c:
            res.checks.append(ck)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(
            Check('stage3.validation', FAIL, f'crashed: {exc}'),
            AuditIssue(stage='stage3', severity=SEVERITY_ERROR,
                       type='stage3_validation_crash', message=str(exc)))
    res.duration_s = time.time() - t0
    return res


# ---------------------------------------------------------------------------
# Stage 4: Test correlation
# ---------------------------------------------------------------------------
def audit_stage_4(audit_output: Optional[Path] = None) -> StageResult:
    audit_output = Path(audit_output or RIA_OUTPUT_DIR)
    res = StageResult(stage=4, label='Test correlation')
    t0 = time.time()

    s4_path = audit_output / 'stage4_recommended_tests.json'
    ok, payload, err = _read_json(s4_path)
    if not ok:
        # Fall back to alternate filenames produced by older builds.
        for alt in ('stage4_correlated_tests.json',
                    'stage4_test_correlation.json',
                    'stage4_tests.json',
                    'stage2_impacted_flows.json'):
            cand = audit_output / alt
            if cand.exists():
                s4_path = cand
                ok, payload, err = _read_json(cand)
                if ok:
                    break

    if not ok:
        res.add_check(
            Check('stage4.output_file', FAIL, err),
            AuditIssue(
                stage='stage4', severity=SEVERITY_ERROR,
                type='stage4_output_missing',
                message=f'stage4 output missing: {err}',
                target=str(s4_path),
                fix=FIX_RERUN_STAGE,
                fix_command='re-run Stage 4 (stage4_test_correlation.py)',
            ),
        )
        res.duration_s = time.time() - t0
        return res
    res.add_check(Check('stage4.output_file', PASS, str(s4_path)))

    tests = (payload.get('recommended_tests')
             or payload.get('correlated_tests')
             or payload.get('tests')
             or payload.get('impacted_flows')
             or [])
    res.metrics['stage4_test_count'] = len(tests)

    if len(tests) == 0:
        res.add_check(
            Check('stage4.test_count', FAIL,
                  'zero tests recommended; flow_dependencies/registry likely '
                  'incomplete'),
            AuditIssue(
                stage='stage4', severity=SEVERITY_WARN,
                type='stage4_zero_tests',
                message='Stage 4 produced zero correlated tests',
                target=str(s4_path),
                fix=FIX_REBUILD_FOCUSED_KB,
                fix_command='re-run focused KB rebuild + Stage 4',
            ),
        )
    elif len(tests) > 500:
        res.add_check(
            Check('stage4.test_count', FAIL,
                  f'{len(tests)} tests (>500 — likely overmatching)'),
            AuditIssue(
                stage='stage4', severity=SEVERITY_WARN,
                type='stage4_overmatching',
                message=f'{len(tests)} tests recommended (>500)',
                target=str(s4_path),
                fix=FIX_RERUN_STAGE,
                fix_command='inspect Signal-1 distinguishing-words filter',
            ),
        )
    else:
        res.add_check(Check('stage4.test_count', PASS,
                            f'{len(tests)} tests recommended'))

    REQUIRED = ('issue_key', 'criticality')
    missing: List[Tuple[str, str]] = []
    zero_score: List[str] = []
    no_flows: List[str] = []
    valid_crit = {'CRITICAL', 'HIGH', 'MEDIUM', 'LOW'}
    bad_crit: List[Tuple[str, str]] = []

    for t in tests[:1000]:
        if not isinstance(t, dict):
            continue
        ik = t.get('issue_key', '?')
        for f in REQUIRED:
            if f not in t:
                missing.append((ik, f))
                break
        crit = t.get('criticality')
        if crit and crit not in valid_crit:
            bad_crit.append((ik, crit))
        if t.get('score', 1) == 0 or t.get('total_score', 1) == 0:
            zero_score.append(ik)
        if not (t.get('matched_flows')
                or t.get('flow_tags')
                or t.get('flows')):
            no_flows.append(ik)

    res.metrics['stage4_zero_score'] = len(zero_score)
    res.metrics['stage4_missing_flows'] = len(no_flows)

    if missing:
        res.add_check(
            Check('stage4.required_fields', FAIL,
                  f'{len(missing)} tests missing required fields '
                  f'(sample: {missing[:3]})'),
            AuditIssue(
                stage='stage4', severity=SEVERITY_ERROR,
                type='stage4_missing_fields',
                message=f'{len(missing)} tests missing required fields',
                target=str(s4_path),
                fix=FIX_RERUN_STAGE,
                fix_command='re-run Stage 4',
            ),
        )
    else:
        res.add_check(Check('stage4.required_fields', PASS,
                            'all sampled tests carry issue_key + criticality'))

    if bad_crit:
        res.add_check(
            Check('stage4.criticality_values', FAIL,
                  f'invalid criticality on {len(bad_crit)} tests'),
            AuditIssue(
                stage='stage4', severity=SEVERITY_WARN,
                type='stage4_bad_criticality',
                message=f'invalid criticality on {len(bad_crit)} tests',
                target=str(s4_path),
                fix=FIX_RERUN_STAGE,
                fix_command='re-run Stage 4',
            ),
        )
    else:
        res.add_check(Check('stage4.criticality_values', PASS,
                            'all criticalities are CRITICAL/HIGH/MEDIUM/LOW'))

    if zero_score and len(zero_score) == len(tests):
        res.add_check(
            Check('stage4.scoring', FAIL,
                  'every test has score=0; scoring pass produced no signal'),
            AuditIssue(
                stage='stage4', severity=SEVERITY_ERROR,
                type='stage4_all_zero_scores',
                message='every test has score=0',
                target=str(s4_path),
                fix=FIX_REBUILD_FOCUSED_KB,
                fix_command='re-run focused KB rebuild + Stage 4',
            ),
        )
    else:
        res.add_check(Check('stage4.scoring', PASS,
                            f'{len(zero_score)} tests with score=0'))

    if no_flows and len(no_flows) > 0.5 * len(tests):
        res.add_check(
            Check('stage4.matched_flows', FAIL,
                  f'{len(no_flows)}/{len(tests)} tests have no matched_flows'),
            AuditIssue(
                stage='stage4', severity=SEVERITY_WARN,
                type='stage4_unmatched_flows',
                message=f'{len(no_flows)}/{len(tests)} tests lack flows',
                target=str(s4_path),
                fix=FIX_REBUILD_FLOW_DEPENDENCIES,
                fix_command='python3 build_flow_dependencies.py',
            ),
        )
    else:
        res.add_check(Check(
            'stage4.matched_flows', PASS,
            f'{len(tests) - len(no_flows)}/{len(tests)} tests have flows',
        ))

    # Cross-validate referenced components against flow_dependencies.
    fd = _safe_load_json(_kb_dir(audit_output) / 'flow_dependencies.json') or {}
    fd_components: set = set()
    for entry in (fd.get('flow_dependencies') or fd.get('dependencies') or []):
        comp = entry.get('component') if isinstance(entry, dict) else None
        if comp:
            fd_components.add(comp)
    referenced_components: set = set()
    for t in tests:
        if not isinstance(t, dict):
            continue
        comp = t.get('component') or t.get('owning_component')
        if comp:
            referenced_components.add(comp)
    missing_comps = sorted(c for c in referenced_components
                           if c not in fd_components)
    if missing_comps and fd_components:
        res.add_check(
            Check('stage4.flow_dependencies_coverage', FAIL,
                  f'{len(missing_comps)} components referenced by Stage 4 '
                  f'absent from flow_dependencies.json'),
            AuditIssue(
                stage='stage4', severity=SEVERITY_WARN,
                type='stage4_missing_components',
                message=(f'{len(missing_comps)} components referenced by '
                         f'Stage 4 are absent from flow_dependencies.json'),
                target=str(_kb_dir(audit_output) / 'flow_dependencies.json'),
                fix=FIX_REBUILD_FLOW_DEPENDENCIES,
                fix_command='python3 build_flow_dependencies.py',
                details={'missing_components': missing_comps[:25]},
            ),
        )
    else:
        res.add_check(Check('stage4.flow_dependencies_coverage', PASS,
                            'every referenced component present in fd'))

    # Deep content validation for Stage 4.
    try:
        c, isu, m = validate_stage4_content(audit_output)
        res.checks.extend(c)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(Check('stage4.content', FAIL,
                            f'deep validation crashed: {exc}'))

    res.duration_s = time.time() - t0
    return res


# ---------------------------------------------------------------------------
# Stage 5: Test refinement
# ---------------------------------------------------------------------------
def audit_stage_5(audit_output: Optional[Path] = None) -> StageResult:
    audit_output = Path(audit_output or RIA_OUTPUT_DIR)
    res = StageResult(stage=5, label='Test refinement')
    t0 = time.time()
    # In multi-method mode the orchestrator intentionally copies the
    # Stage 4 output to the Stage 5 path because flow_dependencies is
    # scoped per-method and Stage 5's dependency-aware ranking would
    # SKIP every flow. Audit it as a clean SKIP rather than flagging
    # the deliberate no-op (e.g. "no score enrichment", "low retention").
    if _is_multi_method_mode(audit_output):
        res.status = SKIP
        res.checks.append(Check(
            'stage5.architecture_mode', SKIP,
            'multi-method mode detected (multi_method/ workspace present); '
            'Stage 5 is a copy-through of Stage 4 because '
            'flow_dependencies are per-method scoped'))
        res.metrics['stage5.skipped_reason'] = 'multi_method_mode_copy_through'
        res.duration_s = time.time() - t0
        return res

    s4_path = audit_output / 'stage4_recommended_tests.json'
    s5_path = audit_output / 'stage5_refined_tests.json'

    ok4, p4, err4 = _read_json(s4_path)
    ok5, p5, err5 = _read_json(s5_path)

    if not ok4:
        res.add_check(Check('stage5.input_stage4', FAIL, err4))
    else:
        res.add_check(Check('stage5.input_stage4', PASS, ''))

    if not ok5:
        # Stage 5 may have been skipped (--no-refinement) - INFO only.
        res.add_check(Check('stage5.output_file', SKIP,
                            f'stage5 output missing: {err5} '
                            '(refinement may have been skipped)'))
        res.duration_s = time.time() - t0
        return res
    res.add_check(Check('stage5.output_file', PASS, str(s5_path)))

    s4_tests = ((p4 or {}).get('recommended_tests')
                or (p4 or {}).get('tests') or [])
    s5_tests = ((p5 or {}).get('refined_tests')
                or (p5 or {}).get('tests') or [])

    res.metrics['stage5_input_count'] = len(s4_tests)
    res.metrics['stage5_output_count'] = len(s5_tests)

    if s4_tests and not s5_tests:
        res.add_check(
            Check('stage5.not_empty', FAIL,
                  'all tests eliminated by refinement '
                  '(synonym_groups likely incomplete)'),
            AuditIssue(
                stage='stage5', severity=SEVERITY_ERROR,
                type='stage5_over_filtered',
                message='all tests eliminated by refinement',
                target=str(s5_path),
                fix=FIX_REBUILD_SYNONYM_GROUPS,
                fix_command='python3 build_synonym_groups.py',
                details={'input_count': len(s4_tests),
                         'output_count': 0,
                         'current_threshold': 'aggressive'},
            ),
        )
    elif s4_tests:
        ratio = 100.0 * len(s5_tests) / max(len(s4_tests), 1)
        if ratio < 10.0:
            res.add_check(
                Check('stage5.retention_ratio', FAIL,
                      f'kept only {ratio:.1f}% of input tests'),
                AuditIssue(
                    stage='stage5', severity=SEVERITY_WARN,
                    type='stage5_low_retention',
                    message=f'low retention ratio: {ratio:.1f}%',
                    target=str(s5_path),
                    fix=FIX_REBUILD_SYNONYM_GROUPS,
                    fix_command='python3 build_synonym_groups.py',
                    details={'retention_pct': round(ratio, 1)},
                ),
            )
        else:
            res.add_check(Check('stage5.retention_ratio', PASS,
                                f'{ratio:.1f}% retention'))
    else:
        res.add_check(Check('stage5.retention_ratio', SKIP,
                            'no input tests'))

    bad_eps = 0
    for t in s5_tests[:500]:
        eps = (t.get('entry_points')
               or t.get('discovered_entry_points')
               or [])
        for ep in eps:
            if not isinstance(ep, str) or ':' not in ep:
                bad_eps += 1
                break
    if bad_eps:
        res.add_check(
            Check('stage5.entry_points_format', FAIL,
                  f'{bad_eps} tests carry malformed entry_points'),
            AuditIssue(
                stage='stage5', severity=SEVERITY_WARN,
                type='stage5_malformed_entry_points',
                message=f'{bad_eps} tests with malformed entry_points',
                target=str(s5_path),
                fix=FIX_REBUILD_FLOW_REGISTRY,
                fix_command='python3 build_flow_registry.py',
            ),
        )
    else:
        res.add_check(Check('stage5.entry_points_format', PASS,
                            'sampled tests carry valid entry_points'))

    # Deep content validation for Stage 5.
    try:
        c, isu, m = validate_stage5_content(audit_output)
        res.checks.extend(c)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(Check('stage5.content', FAIL,
                            f'deep validation crashed: {exc}'))

    res.duration_s = time.time() - t0
    return res


# ---------------------------------------------------------------------------
# Stage 6: Aggressive suppression
# ---------------------------------------------------------------------------
def audit_stage_6(audit_output: Optional[Path] = None) -> StageResult:
    audit_output = Path(audit_output or RIA_OUTPUT_DIR)
    res = StageResult(stage=6, label='Aggressive suppression')
    t0 = time.time()

    s5_path = audit_output / 'stage5_refined_tests.json'
    s6_path = audit_output / 'stage6_aggressive_tests.json'

    ok5, p5, err5 = _read_json(s5_path)
    ok6, p6, err6 = _read_json(s6_path)

    if not ok5:
        res.add_check(Check('stage6.input_stage5', SKIP, err5))
    else:
        res.add_check(Check('stage6.input_stage5', PASS, ''))

    if not ok6:
        res.add_check(Check('stage6.output_file', SKIP,
                            f'stage6 output missing: {err6}'))
        res.duration_s = time.time() - t0
        return res
    res.add_check(Check('stage6.output_file', PASS, str(s6_path)))

    s5_tests = ((p5 or {}).get('refined_tests')
                or (p5 or {}).get('tests') or [])
    s6_tests = ((p6 or {}).get('aggressive_tests')
                or (p6 or {}).get('tests') or [])

    res.metrics['stage6_input_count'] = len(s5_tests)
    res.metrics['stage6_output_count'] = len(s6_tests)

    if s5_tests and not s6_tests:
        res.add_check(
            Check('stage6.not_empty', FAIL,
                  'aggressive suppression eliminated all tests'),
            AuditIssue(
                stage='stage6', severity=SEVERITY_ERROR,
                type='stage6_over_suppressed',
                message='aggressive suppression eliminated all tests',
                target=str(s6_path),
                fix=FIX_RERUN_STAGE,
                fix_command='re-run Stage 6 with looser thresholds',
                details={'input_count': len(s5_tests), 'output_count': 0},
            ),
        )
    elif s5_tests:
        # ADAPTIVE retention check.
        #
        # Stage 6 is an INTENTIONAL aggressive-suppression pass that applies
        # a global final cap (DEFAULT_FINAL_CAP = 30, scaled up to ~60 for
        # cross-cutting changes by stage6_aggressive_suppression._adaptive_final_cap).
        # Once Stage 5 emits enough candidates that the cap dominates, the
        # retention ratio is determined by the cap, not by suppression
        # quality. A fixed 5% threshold therefore mis-flags every healthy
        # large-Stage-5 run as "low retention" (e.g. Stage 5 = 955 -> the
        # cap of 30 yields 3.1%, but that is exactly what the algorithm
        # was designed to do).
        #
        # We derive the floor from the system's own constraints, not from
        # a hardcoded percentage:
        #
        #   retention_floor_count =
        #       max(target_min_count, cap_used_by_stage6)
        #
        # If the actual output meets this absolute floor, the run is healthy
        # regardless of percentage. If it does NOT, we still report the
        # ratio for diagnostics.
        #
        # `target_min_count` and `target_max_count` mirror the deep-content
        # target band ([20, 100]) used by validate_stage6_content() so the
        # two checks stay consistent.
        ratio = 100.0 * len(s6_tests) / max(len(s5_tests), 1)

        # Pull the cap actually applied by Stage 6 from its own output when
        # it is exposed (single-method runs persist `parameters.final_cap`).
        # Multi-method consolidated outputs strip parameters; in that case
        # we import the same default the suppression script uses, so the
        # check stays in lock-step with the algorithm without hardcoding
        # the value here.
        cap_used: Optional[int] = None
        try:
            params = (p6 or {}).get('parameters') or {}
            if isinstance(params, dict):
                fc = params.get('final_cap')
                if isinstance(fc, int) and fc > 0:
                    cap_used = fc
        except Exception:
            cap_used = None
        if cap_used is None:
            try:
                if str(SCRIPT_DIR) not in sys.path:
                    sys.path.insert(0, str(SCRIPT_DIR))
                from stage6_aggressive_suppression import (  # type: ignore
                    DEFAULT_FINAL_CAP as _S6_DEFAULT_CAP,
                )
                cap_used = int(_S6_DEFAULT_CAP)
            except Exception:
                cap_used = None

        # Documented Stage 6 target band (mirrors validate_stage6_content()).
        target_min_count = 20
        target_max_count = 100

        # Effective absolute floor: we want at least the documented minimum,
        # but never demand more than what the cap is allowed to emit. If the
        # cap is unknown we fall back to target_min_count.
        if cap_used is not None:
            retention_floor_count = max(
                target_min_count,
                min(cap_used, target_max_count),
            )
        else:
            retention_floor_count = target_min_count

        # Cap-dominated regime: when input * (target_min/input) < retention_floor
        # the cap itself is the limiting factor and the retention ratio loses
        # meaning. Switch to absolute-count semantics in that case.
        cap_dominated = len(s5_tests) > retention_floor_count and (
            cap_used is not None and len(s5_tests) > cap_used
        )

        if len(s6_tests) >= retention_floor_count:
            # Healthy: enough absolute tests retained.
            detail = (
                f'{ratio:.1f}% retention '
                f'({len(s6_tests)}/{len(s5_tests)} tests; '
                f'cap={cap_used if cap_used is not None else "n/a"})'
            )
            res.add_check(Check('stage6.retention_ratio', PASS, detail))
        elif cap_dominated and len(s6_tests) >= target_min_count:
            # Cap-dominated AND meets minimum target: not a true regression.
            res.add_check(Check(
                'stage6.retention_ratio', PASS,
                f'{ratio:.1f}% retention is cap-dominated '
                f'(cap={cap_used}, output={len(s6_tests)}, '
                f'min target={target_min_count})',
            ))
        else:
            # Genuine over-suppression: output below the absolute floor.
            res.add_check(
                Check('stage6.retention_ratio', FAIL,
                      f'kept only {len(s6_tests)} of {len(s5_tests)} '
                      f'stage 5 tests ({ratio:.1f}%, '
                      f'floor={retention_floor_count})'),
                AuditIssue(
                    stage='stage6', severity=SEVERITY_WARN,
                    type='stage6_low_retention',
                    message=(f'low retention: {len(s6_tests)} tests '
                             f'(< floor {retention_floor_count}; '
                             f'ratio {ratio:.1f}%)'),
                    target=str(s6_path),
                    fix=FIX_RERUN_STAGE,
                    fix_command='inspect stage 6 thresholds',
                    details={
                        'output_count': len(s6_tests),
                        'input_count': len(s5_tests),
                        'retention_pct': round(ratio, 2),
                        'retention_floor_count': retention_floor_count,
                        'cap_used': cap_used,
                    },
                ),
            )
    else:
        res.add_check(Check('stage6.retention_ratio', SKIP,
                            'no input tests'))

    n = len(s6_tests)
    if n == 0:
        pass
    elif n > 300:
        res.add_check(
            Check('stage6.final_count', FAIL,
                  f'{n} tests after suppression (>300)'),
            AuditIssue(
                stage='stage6', severity=SEVERITY_WARN,
                type='stage6_too_many_tests',
                message=f'{n} tests after suppression',
                target=str(s6_path),
                fix=FIX_RERUN_STAGE,
                fix_command='tighten stage 6 thresholds',
            ),
        )
    else:
        res.add_check(Check('stage6.final_count', PASS, f'final count {n}'))

    # Deep content validation for Stage 6.
    try:
        c, isu, m = validate_stage6_content(audit_output)
        res.checks.extend(c)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(Check('stage6.content', FAIL,
                            f'deep validation crashed: {exc}'))

    res.duration_s = time.time() - t0
    return res


# ---------------------------------------------------------------------------
# Stage 7: HTML report
# ---------------------------------------------------------------------------
def audit_stage_7(audit_output: Optional[Path] = None) -> StageResult:
    audit_output = Path(audit_output or RIA_OUTPUT_DIR)
    res = StageResult(stage=7, label='HTML report')
    t0 = time.time()

    html_path = audit_output / 'RIA_Report.html'
    if not html_path.exists():
        # Treat as SKIP rather than FAIL when --no-html was used.
        res.add_check(Check('stage7.html_file', SKIP,
                            f'missing: {html_path} (HTML may be disabled)'))
        res.duration_s = time.time() - t0
        return res

    try:
        size = html_path.stat().st_size
    except OSError as e:
        res.add_check(Check('stage7.html_file', FAIL, f'stat failed: {e}'))
        res.duration_s = time.time() - t0
        return res

    res.metrics['html_size_bytes'] = size
    if size < 5_000:
        res.add_check(
            Check('stage7.html_file', FAIL,
                  f'HTML too small ({size} bytes)'),
            AuditIssue(
                stage='stage7', severity=SEVERITY_WARN,
                type='stage7_html_too_small',
                message=f'HTML report only {size} bytes',
                target=str(html_path),
                fix=FIX_REGENERATE_HTML,
                fix_command='python3 generate_html_report.py',
            ),
        )
    elif size > 5_000_000:
        res.add_check(
            Check('stage7.html_file', FAIL,
                  f'HTML too large ({size} bytes)'),
            AuditIssue(
                stage='stage7', severity=SEVERITY_WARN,
                type='stage7_html_too_large',
                message=f'HTML report {size} bytes (>5MB)',
                target=str(html_path),
                fix=FIX_NONE,
            ),
        )
    else:
        res.add_check(Check('stage7.html_file', PASS, f'{size} bytes'))

    try:
        head = html_path.read_text(encoding='utf-8', errors='replace')[:2000]
        if '<html' not in head.lower() and '<!doctype' not in head.lower():
            res.add_check(
                Check('stage7.html_structure', FAIL,
                      'missing <html> or <!doctype> in first 2KB'),
                AuditIssue(
                    stage='stage7', severity=SEVERITY_WARN,
                    type='stage7_html_malformed',
                    message='HTML missing doctype/html tag',
                    target=str(html_path),
                    fix=FIX_REGENERATE_HTML,
                    fix_command='python3 generate_html_report.py',
                ),
            )
        else:
            res.add_check(Check('stage7.html_structure', PASS,
                                'doctype/html tag present'))
    except OSError as e:
        res.add_check(Check('stage7.html_structure', FAIL,
                            f'read failed: {e}'))

    res.duration_s = time.time() - t0
    return res


# ===========================================================================
# DEEP CONTENT VALIDATION
# ===========================================================================
# The validators above check structure (file present, JSON valid, fields
# present, basic counts within range). The validators below additionally
# inspect the *content* of each artifact for completeness, correctness,
# cross-file consistency, and quality. They also validate that the
# pipeline's outputs match the EXPECTED OUTCOME for the detected change
# type (dependency upgrade, single-method change, etc.).
#
# Every validator returns a (checks, issues, metrics) tuple so callers can
# merge results into the existing StageResult envelope without changing
# the public API.
# ===========================================================================

import re as _re_deep  # local alias keeps top-of-file import block tidy

# Generic verbs / nouns that are too broad to be useful as match anchors.
_GENERIC_VERBS = frozenset({
    'get', 'set', 'do', 'run', 'call', 'use', 'make', 'have', 'find',
    'go', 'put', 'take', 'give', 'come', 'see', 'know', 'think', 'look',
    'want', 'work', 'process', 'handle', 'execute', 'perform', 'invoke',
})

_GENERIC_KEYWORDS = frozenset({
    'utility', 'utilities', 'util', 'utils', 'helper', 'helpers', 'common',
    'service', 'services', 'manager', 'managers', 'controller', 'controllers',
    'handler', 'handlers', 'base', 'abstract', 'impl', 'implementation',
})

# Source / build directories used to count code files for component_map
# coverage estimation. Anything under these is considered out of scope.
_NON_SOURCE_DIRS = (
    'node_modules', '.git', '.venv', 'venv', '__pycache__',
    'build', 'dist', 'target', 'out', 'coverage',
    '.gradle', '.idea', '.vscode', '.next', '.cache',
)

_TEST_DIR_HINTS = ('test/', 'tests/', '/tests/', '/test/', 'spec/', '/specs/')

_SOURCE_EXT = ('.java', '.py', '.ts', '.tsx', '.js', '.jsx')


def _is_source_file(path: Path) -> bool:
    """Heuristic: return True for production source files.

    Used by component_map content validation to estimate how many
    components we should expect.
    """
    if not path.is_file():
        return False
    if path.suffix.lower() not in _SOURCE_EXT:
        return False
    parts = {p.lower() for p in path.parts}
    if any(d in parts for d in _NON_SOURCE_DIRS):
        return False
    pstr = str(path).replace('\\', '/').lower()
    if any(t in pstr for t in _TEST_DIR_HINTS):
        return False
    if path.name.endswith(('Test.java', 'Tests.java', 'IT.java', '.test.ts',
                           '.spec.ts', '.test.tsx', '.spec.tsx',
                           '_test.py', 'test_.py')):
        return False
    return True


def _count_source_files(repo_root: Path) -> int:
    """Cheap recursive count of production source files."""
    n = 0
    try:
        for root, dirs, files in os.walk(repo_root):
            # prune in-place
            dirs[:] = [d for d in dirs if d not in _NON_SOURCE_DIRS]
            rel = Path(root).relative_to(repo_root) if Path(root).is_relative_to(repo_root) else Path(root)
            rel_str = str(rel).replace('\\', '/').lower() + '/'
            if any(t in rel_str for t in _TEST_DIR_HINTS):
                continue
            for fname in files:
                if not fname.lower().endswith(_SOURCE_EXT):
                    continue
                if fname.endswith(('Test.java', 'Tests.java', 'IT.java',
                                   '.test.ts', '.spec.ts',
                                   '.test.tsx', '.spec.tsx',
                                   '_test.py', 'test_.py')):
                    continue
                n += 1
                if n > 50000:  # safety bound for very large repos
                    return n
    except Exception:
        return n
    return n


def _resolve_repo_path(repo_root: Path, file_path: str) -> Optional[Path]:
    """Resolve a possibly-relative file_path against the repo root."""
    if not file_path:
        return None
    p = Path(file_path)
    if p.is_absolute() and p.exists():
        return p
    cand = repo_root / file_path
    if cand.exists():
        return cand
    return None


_VERB_RE = _re_deep.compile(r"\b([a-zA-Z]{3,})\b")

# Cached spaCy pipeline for the auditor - mirrors build_synonym_groups.py so
# the cross-check uses identical extraction semantics. Loaded lazily.
_AUDITOR_NLP = None
_AUDITOR_NLP_CHECKED = False


def _load_auditor_spacy():
    """Lazy spaCy loader for the auditor.

    Returns a spaCy pipeline if spaCy + en_core_web_sm are installed,
    otherwise returns None. Audit checks degrade gracefully — they skip
    PoS-aware cross-checks rather than failing the whole audit.
    """
    global _AUDITOR_NLP, _AUDITOR_NLP_CHECKED
    if _AUDITOR_NLP_CHECKED:
        return _AUDITOR_NLP
    _AUDITOR_NLP_CHECKED = True
    try:
        import spacy  # type: ignore
        _AUDITOR_NLP = spacy.load("en_core_web_sm")
    except Exception:
        _AUDITOR_NLP = None
    return _AUDITOR_NLP


def _extract_verbs_from_corpus(corpus: List[Dict[str, Any]],
                               sample_limit: int = 5000) -> set:
    """Verb extractor used to cross-check synonym_groups stats.

    Mirrors the extraction semantics of build_synonym_groups.py so the
    cross-check is apples-to-apples:
      * If spaCy + en_core_web_sm are available, run PoS-tag + lemma and
        keep only VERB tokens (matching extract_verbs_from_tests()).
      * Otherwise fall back to a strict heuristic that still avoids the
        worst noun/adjective leaks: lowercase tokens that begin with a
        non-capital letter AND end with a verb-shaped suffix
        (-e/-ed/-ing/-ate/-ize/-ise/-fy) OR are <=5 chars (likely
        imperative forms). The heuristic is intentionally conservative;
        the spaCy path is the source of truth.
    """
    verbs: set = set()
    if not isinstance(corpus, list):
        return verbs

    nlp = _load_auditor_spacy()
    sample = corpus[:sample_limit]

    if nlp is not None:
        # PoS-aware path - mirrors build_synonym_groups.extract_verbs_from_tests
        texts: List[str] = []
        for tc in sample:
            if not isinstance(tc, dict):
                continue
            text = ' '.join(str(tc.get(k, '') or '')
                            for k in ('summary', 'description'))
            for step in (tc.get('steps') or [])[:20]:
                if isinstance(step, dict):
                    text += ' ' + str(step.get('action', '') or '')
                    text += ' ' + str(step.get('data', '') or '')
                    text += ' ' + str(step.get('result', '') or '')
                elif isinstance(step, str):
                    text += ' ' + step
            text = text.strip()
            if text:
                texts.append(text)

        try:
            for doc in nlp.pipe(texts, batch_size=64,
                                disable=["parser", "ner"]):
                for token in doc:
                    if token.pos_ == "VERB":
                        lemma = token.lemma_.lower()
                        if len(lemma) >= 3 and lemma.isalpha():
                            verbs.add(lemma)
        except Exception:
            # If spaCy crashes mid-pipe, fall through to heuristic on the
            # remainder so the audit produces *some* answer rather than 0.
            pass
        if verbs:
            return verbs

    # Strict heuristic fallback (spaCy unavailable). Conservative on purpose:
    # we'd rather under-estimate than over-estimate, because over-estimation
    # is what made this audit fire spurious "53% gap" warnings.
    verb_suffixes = ('ate', 'ize', 'ise', 'ify', 'fy',
                     'ing', 'ed', 'en')
    for tc in sample:
        if not isinstance(tc, dict):
            continue
        text = ' '.join(str(tc.get(k, '') or '')
                        for k in ('summary', 'description'))
        for step in (tc.get('steps') or [])[:20]:
            if isinstance(step, dict):
                text += ' ' + str(step.get('action', '') or '')
            elif isinstance(step, str):
                text += ' ' + step
        for tok in _VERB_RE.findall(text):
            if not tok or tok[0].isupper():
                continue
            tl = tok.lower()
            if len(tl) < 3 or not tl.isalpha():
                continue
            # Keep only tokens that look verb-shaped: short imperative
            # (<=5 chars, e.g. "add", "save", "load") OR end in a
            # verb-shaped suffix.
            if len(tl) <= 5 or tl.endswith(verb_suffixes):
                verbs.add(tl)
    return verbs


def _count_generic_synonym_groups(groups: Any) -> int:
    """Count synonym groups whose members are dominated by generic verbs."""
    n_generic = 0
    if isinstance(groups, dict):
        iterator = groups.items()
    else:
        return 0
    for name, members in iterator:
        if not isinstance(members, list) or not members:
            continue
        gen = sum(1 for m in members if str(m).lower() in _GENERIC_VERBS)
        if gen / max(len(members), 1) >= 0.7:
            n_generic += 1
        elif name.upper() in {'MISC', 'OTHER', 'GENERIC'}:
            n_generic += 1
    return n_generic


def _file_has_method_definitions(file_path: Path,
                                 repo_root: Path) -> bool:
    """Return True if the file declares at least one method/function.

    Universal across Java, Kotlin, Python, TypeScript, JavaScript, Go: we
    look for any of the canonical declaration prefixes used across these
    languages. A file with only constants / fields / class-level static
    declarations returns False -- such files legitimately have no methods
    and should NOT be flagged as parser failures by the auditor.

    The check is intentionally cheap (regex over the file body) because
    it runs across ~200 sample components on every audit pass.
    """
    resolved = _resolve_repo_path(repo_root, str(file_path))
    if resolved is None or not resolved.is_file():
        # Can't read the file -> can't make a defensible judgement;
        # assume it *should* have had methods so the auditor still flags
        # genuinely missing components.
        return True
    try:
        # Cap read size so a 50MB generated file doesn't stall the audit.
        text = resolved.read_text(encoding='utf-8', errors='ignore')[:200000]
    except Exception:
        return True

    # Java / Kotlin / Groovy / C-family: typed declaration with parens.
    # Pattern: "<modifier> <type-token> <name>(" with the `(` followed by
    # `{` (concrete body), `;` (abstract / interface signature), or `throws`
    # (Java checked-exception declaration). At least ONE type-like token
    # must sit between the modifier and the method name, which excludes
    # enum literals like `ACTIVE("Active");` (no type token, just the
    # literal name) and constructor-call-style field initializers.
    #
    # The pre-paren region forbids '=', ';', '\n', and ',' so we don't
    # span over field initializers, statements, or enum literal lists.
    #
    # IMPORTANT: build_component_map.py drops the canonical Object-overrides
    # (`toString`, `equals`, `hashCode`, `clone`, `finalize`) because they
    # are universally generated by Lombok / IDEs and carry no business
    # signal. We MIRROR that filter here so a Lombok @Data class whose only
    # source-level methods are equals + hashCode is recognised as
    # "constants-like" (no parser failure) instead of being mis-classified
    # as a parser bug.
    _OBJECT_OVERRIDE = _re_deep.compile(
        r'^(?:toString|equals|hashCode|clone|finalize)$'
    )
    java_method = _re_deep.compile(
        r'\b(?:public|private|protected|internal|fun|def|static|final|'
        r'synchronized|abstract|native|override|virtual|async)\b'
        r'[^=;,\n]{1,200}?\b\w+\s+(\w+)\s*\([^)]*\)\s*'
        r'(?:throws\s+[\w,.\s]+)?\s*[\{;]'
    )
    for m in java_method.finditer(text):
        # Group 1 = method name. Skip Object overrides; they don't count.
        if _OBJECT_OVERRIDE.match(m.group(1)):
            continue
        return True
    # Kotlin / Scala-style: `fun name(...)` or `def name(...)` where the
    # modifier IS the keyword. The first regex caught these already, but
    # make it explicit so a file that uses ONLY `fun` or `def` without
    # extra modifiers still hits.
    kotlin_method = _re_deep.compile(
        r'^\s*(?:override\s+)?(?:fun|def)\s+\w+\s*\(',
        _re_deep.MULTILINE
    )
    if kotlin_method.search(text):
        return True

    # Java constructor pattern: `<modifier> ClassName(...) {` where the
    # name immediately follows the modifier (no return type). Constructors
    # ARE callable members and the symbol parser reports them, so a file
    # with a non-trivial constructor still has methods from the parser's
    # POV.
    #
    # We DELIBERATELY ignore the standard "private no-arg empty"
    # constructor used by utility classes (`private Foo() {}`) because
    # Serena / get_symbols_overview also drops it from the method list.
    # Treating it as a method would re-flag known-good utility classes
    # like SQSRetryConstants as parser failures.
    ctor_method = _re_deep.compile(
        r'\b(?P<vis>public|private|protected)\s+(?P<name>[A-Z]\w*)\s*\('
        r'(?P<params>[^)]*)\)\s*(?:throws\s+[\w,.\s]+)?\s*\{'
        r'(?P<body>[^{}]*)\}'
    )
    # Cross-check: only count it as a method if the matched name appears
    # as a class declaration in the file (so we ignore method calls).
    for m in ctor_method.finditer(text):
        ctor_name = m.group('name')
        if not _re_deep.search(
            rf'\b(?:class|record|interface|enum)\s+{ctor_name}\b', text
        ):
            continue
        # Skip the trivial private no-arg empty constructor pattern.
        is_trivial = (m.group('vis') == 'private'
                      and not m.group('params').strip()
                      and not m.group('body').strip())
        if is_trivial:
            continue
        return True

    # Python / Cython.
    py_method = _re_deep.compile(r'^\s*(?:async\s+)?def\s+\w+\s*\(',
                                 _re_deep.MULTILINE)
    if py_method.search(text):
        return True

    # TypeScript / JavaScript-only patterns. We restrict by file
    # extension because the third alternative (`\w+(...){`) can otherwise
    # mis-fire on Java records and similar declarations. Java code is
    # already covered by the `java_method` and `ctor_method` regexes
    # above.
    suffix = resolved.suffix.lower()
    if suffix in {'.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs'}:
        js_method = _re_deep.compile(
            r'(?:^|\s)(?:function\s+\w+|const\s+\w+\s*=\s*(?:async\s*)?'
            r'\([^)]*\)\s*=>|(?:public|private|protected|static|async)\s+'
            r'\w+\s*\([^)]*\)\s*\{|^\s*\w+\s*\([^)]*\)\s*\{)',
            _re_deep.MULTILINE
        )
        if js_method.search(text):
            return True

    # Go.
    if suffix == '.go':
        go_method = _re_deep.compile(
            r'^\s*func\s+(?:\([^)]*\)\s*)?\w+\s*\(',
            _re_deep.MULTILINE
        )
        if go_method.search(text):
            return True

    # Nothing matched -> it's a constants / data / interface file.
    return False


def _has_only_generic_keywords(keywords: List[str]) -> bool:
    """True when 100% of a component's keywords are too generic."""
    if not keywords:
        return True
    cleaned = [str(k).lower().strip() for k in keywords if k]
    if not cleaned:
        return True
    return all(any(g in c for g in _GENERIC_KEYWORDS) for c in cleaned)


def validate_synonym_groups_content(kb_dir: Path,
                                    test_corpus_path: Optional[Path]
                                    ) -> Tuple[List[Check], List[AuditIssue],
                                               Dict[str, Any]]:
    """Deep content validation for synonym_groups.json.

    Detects:
      - Verb extraction gross under/overcount vs. corpus (>5% gap)
      - Generic-group quality (>30% groups dominated by generic verbs)
      - Coverage adequacy for matching (sample 100 random tests)
      - Anomalous totals for the repo size (e.g. 1 group, 10 verbs)
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    sg_path = Path(kb_dir) / 'synonym_groups.json'
    payload = _safe_load_json(sg_path)
    if not payload:
        # Structural validator already flagged this; nothing more to add.
        return checks, issues, metrics

    groups = payload.get('synonym_groups') or payload.get('groups') or {}
    extracted = int(payload.get('total_verbs_extracted', 0) or 0)
    metrics['synonym_groups.total'] = (len(groups) if isinstance(groups, dict)
                                       else 0)
    metrics['synonym_groups.verbs_extracted'] = extracted

    # 1. Anomalous totals.
    if isinstance(groups, dict) and 0 < len(groups) < 3:
        c = Check('synonym_groups.content.anomalous_group_count', FAIL,
                  f'only {len(groups)} groups present; expected >=3 for '
                  'a real corpus')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_synonym_groups_anomalous_count',
            message=c.detail, target=str(sg_path),
            fix=FIX_REBUILD_SYNONYM_GROUPS,
            fix_command='python3 build_synonym_groups.py',
            details={'group_count': len(groups)},
        ))
    else:
        checks.append(Check('synonym_groups.content.anomalous_group_count',
                            PASS, f'{len(groups) if isinstance(groups, dict) else 0} groups'))

    # 2. Generic / low-quality groups.
    n_generic = _count_generic_synonym_groups(groups)
    metrics['synonym_groups.generic_groups'] = n_generic
    n_total = len(groups) if isinstance(groups, dict) else 0
    if n_total and n_generic / n_total > 0.30:
        c = Check('synonym_groups.content.quality', FAIL,
                  f'{n_generic}/{n_total} groups dominated by generic '
                  'verbs (>30%)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_synonym_groups_low_quality',
            message=c.detail, target=str(sg_path),
            fix=FIX_REBUILD_SYNONYM_GROUPS,
            fix_command='python3 build_synonym_groups.py',
            details={'generic_groups': n_generic,
                     'total_groups': n_total},
        ))
    else:
        checks.append(Check('synonym_groups.content.quality', PASS,
                            f'{n_generic}/{max(n_total,1)} generic groups'))

    # 3. Verb-count cross-check vs corpus.
    #
    # The auditor's _extract_verbs_from_corpus() now mirrors the
    # extraction semantics of build_synonym_groups.py:
    #   * spaCy PoS path when available -> apples-to-apples comparison.
    #   * Strict heuristic fallback otherwise (only verb-shaped tokens).
    #
    # The audit also factors in that:
    #   * The auditor samples up to 5000 tests; the builder consumes the
    #     full corpus. So the auditor's count is normally LOWER, not higher.
    #   * Verbs from code (build_synonym_groups extracts both code+test
    #     verbs) are NOT visible here, so the builder's reported total can
    #     legitimately exceed the test-only auditor estimate.
    #
    # Bidirectional gap policy (after PoS alignment):
    #   * If the auditor estimates MORE verbs than reported, gap > 0.5
    #     and abs delta > 100 -> the builder is dropping verbs (real bug).
    #   * If the auditor estimates FEWER verbs than reported, that is
    #     EXPECTED (the auditor samples the corpus + sees no code). We
    #     only flag this if the auditor sees < 10% of the reported count,
    #     which would indicate the auditor itself is broken / corpus is
    #     unrepresentative.
    if test_corpus_path:
        corpus = _safe_load_json(Path(test_corpus_path)) or []
        actual_verbs = _extract_verbs_from_corpus(corpus)
        actual_n = len(actual_verbs)
        metrics['synonym_groups.corpus_verb_estimate'] = actual_n
        if actual_n and extracted:
            # Direction-aware comparison.
            delta = abs(actual_n - extracted)
            if actual_n >= extracted:
                gap = delta / max(actual_n, 1)
                # Builder under-extracted (real concern) - tighten threshold.
                trigger = (gap > 0.5 and delta > 100)
                direction = 'builder_under_extracted'
            else:
                # Builder >= auditor: auditor lacks code-verb visibility so
                # this is the expected case. Only flag a degenerate gap.
                gap = delta / max(extracted, 1)
                trigger = (extracted > 0 and actual_n / extracted < 0.10
                           and delta > 100)
                direction = 'auditor_undercount'
            if trigger:
                c = Check('synonym_groups.content.verb_count_mismatch', FAIL,
                          f'reported total_verbs_extracted={extracted}, '
                          f'corpus PoS estimate={actual_n} '
                          f'(gap={gap*100:.0f}%, {direction})')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_synonym_verb_count_mismatch',
                    message=c.detail, target=str(sg_path),
                    fix=FIX_REBUILD_SYNONYM_GROUPS,
                    fix_command='python3 build_synonym_groups.py',
                    details={'reported': extracted,
                             'estimated': actual_n,
                             'gap_pct': round(gap * 100, 1),
                             'direction': direction},
                ))
            else:
                checks.append(Check(
                    'synonym_groups.content.verb_count_mismatch',
                    PASS, f'verb count within tolerance ({extracted} vs '
                    f'{actual_n} estimated, {direction})'))
        elif actual_n and not extracted:
            checks.append(Check(
                'synonym_groups.content.verb_count_mismatch', FAIL,
                f'corpus has ~{actual_n} verb-like tokens but '
                f'total_verbs_extracted=0'))

        # Coverage adequacy: sample N tests, check overlap with clustered verbs.
        if isinstance(groups, dict) and corpus:
            clustered: set = set()
            for members in groups.values():
                if isinstance(members, list):
                    for m in members:
                        clustered.add(str(m).lower())
            sample = corpus[:200] if len(corpus) > 200 else corpus
            with_overlap = 0
            for tc in sample:
                if not isinstance(tc, dict):
                    continue
                text = ' '.join(str(tc.get(k, '') or '').lower()
                                for k in ('summary', 'description'))
                for w in _VERB_RE.findall(text):
                    if w.lower() in clustered:
                        with_overlap += 1
                        break
            cov_pct = 100.0 * with_overlap / max(len(sample), 1)
            metrics['synonym_groups.match_coverage_pct'] = round(cov_pct, 1)
            if cov_pct < 60.0:
                c = Check(
                    'synonym_groups.content.match_coverage', FAIL,
                    f'only {cov_pct:.1f}% of sampled tests share a verb '
                    f'with any synonym group (expected >=60%)')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_synonym_match_coverage_low',
                    message=c.detail, target=str(sg_path),
                    fix=FIX_REBUILD_SYNONYM_GROUPS,
                    fix_command='python3 build_synonym_groups.py',
                    details={'match_coverage_pct': round(cov_pct, 1)},
                ))
            else:
                checks.append(Check(
                    'synonym_groups.content.match_coverage', PASS,
                    f'{cov_pct:.1f}% of sampled tests overlap a group'))

    return checks, issues, metrics


def validate_component_map_content(kb_dir: Path,
                                   repo_root: Path
                                   ) -> Tuple[List[Check], List[AuditIssue],
                                              Dict[str, Any]]:
    """Deep content validation for component_map.json.

    Detects:
      - Missing files (component_count vs source-file count >10% gap)
      - Stale paths (file_paths that no longer exist on disk)
      - Generic-only keyword quality
      - Method coverage anomalies (sampled components have empty methods)
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    cm_path = Path(kb_dir) / 'component_map.json'
    payload = _safe_load_json(cm_path)
    if not payload:
        return checks, issues, metrics

    components = payload.get('components') or []
    metrics['component_map.total'] = len(components)
    if not components:
        return checks, issues, metrics

    # 1. File-count cross-check (cap to avoid 30-second os.walk on huge repos).
    #
    # Compare against the sum of file_paths INSIDE the component map
    # rather than the raw component count. build_component_map.py
    # consolidates files that share a base name (e.g. WorkPolicyTemplate-
    # Service.java + WorkPolicyTemplateDao.java -> single WorkPolicyTemplate
    # component) using the discovered_framework_suffixes.json registry.
    # Comparing files-on-disk to *components* therefore over-reports the gap
    # by the consolidation factor (~1.5x in typical Spring codebases) and
    # produced spurious "38% gap" warnings even on healthy KBs. The
    # honest measure is files-on-disk vs files-actually-indexed.
    src_count = _count_source_files(Path(repo_root))
    metrics['component_map.source_file_count'] = src_count
    indexed_files: set = set()
    for comp in components:
        for fp in (comp.get('file_paths') or []):
            if isinstance(fp, str) and fp:
                indexed_files.add(fp)
    indexed_count = len(indexed_files)
    metrics['component_map.indexed_file_count'] = indexed_count
    if src_count > 0:
        # File-level coverage gap (true measure of parser failure).
        file_gap_pct = 100.0 * max(src_count - indexed_count, 0) / src_count
        metrics['component_map.file_coverage_gap_pct'] = round(file_gap_pct, 1)
        # Consolidation ratio (informational only).
        consolidation = (indexed_count / max(len(components), 1)
                         if len(components) else 0.0)
        metrics['component_map.consolidation_ratio'] = round(consolidation, 2)
        if file_gap_pct > 30.0:
            c = Check('component_map.content.coverage_gap', FAIL,
                      f'{src_count} source files on disk but only '
                      f'{indexed_count} indexed across {len(components)} '
                      f'components (file-level gap {file_gap_pct:.0f}%)')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_WARN,
                type='stage0_component_map_under_coverage',
                message=c.detail, target=str(cm_path),
                fix=FIX_REBUILD_COMPONENT_MAP,
                fix_command='python3 build_component_map.py',
                details={'source_files': src_count,
                         'indexed_files': indexed_count,
                         'components': len(components),
                         'file_gap_pct': round(file_gap_pct, 1),
                         'consolidation_ratio': round(consolidation, 2)},
            ))
        else:
            checks.append(Check('component_map.content.coverage_gap', PASS,
                                f'file gap {file_gap_pct:.1f}% '
                                f'({indexed_count}/{src_count} files into '
                                f'{len(components)} components, '
                                f'consolidation {consolidation:.2f}x)'))

    # 2. Stale paths - sample 50 components.
    stale: List[str] = []
    for comp in components[:50]:
        for fp in (comp.get('file_paths') or [])[:3]:
            resolved = _resolve_repo_path(Path(repo_root), fp)
            if resolved is None:
                stale.append(fp)
                break
    metrics['component_map.stale_paths_sampled'] = len(stale)
    if len(stale) > 0.10 * 50:  # >10% of sample
        c = Check('component_map.content.stale_paths', FAIL,
                  f'{len(stale)}/50 sampled components reference paths '
                  f'that do not exist on disk')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_component_map_stale_paths',
            message=c.detail, target=str(cm_path),
            fix=FIX_REBUILD_COMPONENT_MAP,
            fix_command='python3 build_component_map.py',
            details={'stale_sample': stale[:5]},
        ))
    else:
        checks.append(Check('component_map.content.stale_paths', PASS,
                            f'{len(stale)}/50 stale paths in sample'))

    # 3. Keyword-quality sampling.
    sample = components[:20]
    only_generic = sum(1 for c in sample
                       if _has_only_generic_keywords(c.get('keywords') or []))
    metrics['component_map.generic_keyword_sample'] = only_generic
    if sample and only_generic / len(sample) > 0.50:
        c = Check('component_map.content.keyword_quality', FAIL,
                  f'{only_generic}/{len(sample)} sampled components '
                  f'have only generic keywords')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_component_map_generic_keywords',
            message=c.detail, target=str(cm_path),
            fix=FIX_REBUILD_COMPONENT_MAP,
            fix_command='python3 build_component_map.py',
            details={'generic_only': only_generic,
                     'sample_size': len(sample)},
        ))
    else:
        checks.append(Check('component_map.content.keyword_quality', PASS,
                            f'{only_generic}/{len(sample)} generic-only '
                            'in sample'))

    # 4. Method extraction sanity.
    #
    # Components with file_paths but zero methods can be EITHER:
    #   (a) parser failures - the file declares methods but the parser
    #       couldn't read them (real bug we want to surface), OR
    #   (b) constant / enum / data / interface / config files that
    #       legitimately contain only fields, e.g. QueryConstant.java,
    #       SQSConfig.java. These are NOT bugs and must not be flagged.
    #
    # We separate the two by inspecting the actual file contents
    # (universal across Java/Kotlin/Python/TS/JS/Go) and only count a
    # component as "suspicious" if at least one of its file_paths
    # CONTAINS a method-style declaration. Files with only constants are
    # demoted to an informational metric.
    method_less = [c for c in components[:200]
                   if (c.get('file_paths') and not c.get('methods'))]
    constants_like = []   # legitimate no-method files
    suspicious = []       # parser actually failed on these
    repo_path = Path(repo_root)
    for comp in method_less:
        # A component is "suspicious" only if at least ONE of its files
        # declares a callable. If every file is constants/data, it's a
        # legitimate empty-method component.
        any_callable = False
        for fp in (comp.get('file_paths') or [])[:5]:
            if _file_has_method_definitions(Path(fp), repo_path):
                any_callable = True
                break
        if any_callable:
            suspicious.append(comp.get('component_name'))
        else:
            constants_like.append(comp.get('component_name'))
    metrics['component_map.no_method_components'] = len(method_less)
    metrics['component_map.constants_components_sampled'] = len(constants_like)
    metrics['component_map.parser_failure_sampled'] = len(suspicious)
    if len(suspicious) > 50:  # absolute threshold
        c = Check('component_map.content.method_extraction', FAIL,
                  f'{len(suspicious)}/200 sampled components declare '
                  f'methods in their file but component_map shows zero '
                  f'methods - parser is silently failing')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_component_map_method_parse_failure',
            message=c.detail, target=str(cm_path),
            fix=FIX_REBUILD_COMPONENT_MAP,
            fix_command='python3 build_component_map.py',
            details={'suspicious_sample': suspicious[:5],
                     'constants_excluded': len(constants_like)},
        ))
    else:
        checks.append(Check('component_map.content.method_extraction', PASS,
                            f'{len(suspicious)} parser failures + '
                            f'{len(constants_like)} legitimate constants/'
                            f'data files in 200-sample'))

    return checks, issues, metrics


def validate_flow_registry_content(kb_dir: Path,
                                   repo_root: Path,
                                   test_corpus_path: Optional[Path]
                                   ) -> Tuple[List[Check], List[AuditIssue],
                                              Dict[str, Any]]:
    """Deep content validation for flow_registry.json.

    Detects:
      - Synthetic / generic flow names dominating the registry
      - Empty entry_points on >10% of flows
      - Entry points pointing at non-existent files
      - Test tagging coverage <70% of corpus
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    fr_path = Path(kb_dir) / 'flow_registry.json'
    payload = _safe_load_json(fr_path)
    if not payload:
        return checks, issues, metrics

    flows = payload.get('flows') or []
    metrics['flow_registry.total'] = len(flows)
    if not flows:
        return checks, issues, metrics

    # 1. Empty entry_points.
    empty_eps = sum(1 for f in flows
                    if not (f.get('entry_points') or []))
    metrics['flow_registry.empty_entry_points'] = empty_eps
    if len(flows) and empty_eps / len(flows) > 0.10:
        c = Check('flow_registry.content.empty_entry_points', FAIL,
                  f'{empty_eps}/{len(flows)} flows have empty entry_points '
                  f'(>10% threshold)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_flow_registry_empty_entry_points',
            message=c.detail, target=str(fr_path),
            fix=FIX_REBUILD_FLOW_REGISTRY,
            fix_command='python3 build_flow_registry.py',
            details={'empty_count': empty_eps,
                     'total': len(flows)},
        ))
    else:
        checks.append(Check('flow_registry.content.empty_entry_points', PASS,
                            f'{empty_eps}/{len(flows)} flows lack EPs'))

    # 2. Synthetic / generic flow names.
    synthetic = []
    generic = []
    for f in flows:
        name = str(f.get('flow_name') or '')
        flow_id = str(f.get('flow_id') or '')
        if (name.startswith('Component:')
                or flow_id.startswith('SYN_')
                or f.get('origin') == 'component_synthetic'):
            synthetic.append(name or flow_id)
        if name.lower() in {'run', 'execute', 'call', 'invoke', 'process',
                            'handle', 'do', 'flow'}:
            generic.append(name)
    metrics['flow_registry.synthetic_flows'] = len(synthetic)
    metrics['flow_registry.generic_flows'] = len(generic)

    if len(flows) and len(synthetic) / len(flows) > 0.20:
        c = Check('flow_registry.content.synthetic_flows', FAIL,
                  f'{len(synthetic)}/{len(flows)} flows are synthetic '
                  f'(Component:* / SYN_*) which defeats flow-based scoring')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_flow_registry_synthetic_flows',
            message=c.detail, target=str(fr_path),
            fix=FIX_REBUILD_FLOW_REGISTRY,
            fix_command='python3 build_flow_registry.py',
            details={'synthetic_sample': synthetic[:5],
                     'synthetic_count': len(synthetic)},
        ))
    else:
        checks.append(Check('flow_registry.content.synthetic_flows', PASS,
                            f'{len(synthetic)}/{len(flows)} synthetic'))

    if generic:
        c = Check('flow_registry.content.generic_names', FAIL,
                  f'{len(generic)} flows have generic names: '
                  f'{generic[:3]}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_flow_registry_generic_names',
            message=c.detail, target=str(fr_path),
            fix=FIX_REBUILD_FLOW_REGISTRY,
            fix_command='python3 build_flow_registry.py',
            details={'generic_sample': generic[:5]},
        ))
    else:
        checks.append(Check('flow_registry.content.generic_names', PASS,
                            'no generic flow names detected'))

    # 3. Entry-point file existence (sample to keep this O(50)).
    bad_eps: List[str] = []
    sampled_eps = 0
    for f in flows[:30]:
        for ep in (f.get('entry_points') or [])[:3]:
            sampled_eps += 1
            if not isinstance(ep, str) or ':' not in ep:
                bad_eps.append(ep)
                continue
            file_part, _, _ = ep.partition(':')
            if not _resolve_repo_path(Path(repo_root), file_part):
                bad_eps.append(ep)
    metrics['flow_registry.bad_entry_points_sampled'] = len(bad_eps)
    if sampled_eps and len(bad_eps) / sampled_eps > 0.20:
        c = Check('flow_registry.content.entry_point_validity', FAIL,
                  f'{len(bad_eps)}/{sampled_eps} sampled entry points '
                  f'reference missing files')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_flow_registry_invalid_entry_points',
            message=c.detail, target=str(fr_path),
            fix=FIX_REBUILD_FLOW_REGISTRY,
            fix_command='python3 build_flow_registry.py',
            details={'bad_sample': bad_eps[:5]},
        ))
    else:
        checks.append(Check('flow_registry.content.entry_point_validity', PASS,
                            f'{len(bad_eps)}/{sampled_eps} bad EPs'))

    # 4. Test tagging coverage. Prefer the *_enriched.json corpus
    #    (post-tagging) when present - the base extracted file has not
    #    been tagged yet so 0% would be a false positive there.
    if test_corpus_path:
        enriched = Path(test_corpus_path).with_name(
            Path(test_corpus_path).name.replace('.json', '_enriched.json'))
        if not enriched.exists():
            enriched = Path(str(test_corpus_path).replace(
                'all_tcs_extracted.json', 'all_tcs_extracted_enriched.json'))
        corpus_for_tags = (enriched if enriched.exists()
                           else Path(test_corpus_path))
        corpus = _safe_load_json(corpus_for_tags) or []
        if corpus:
            tagged = sum(1 for t in corpus
                         if isinstance(t, dict) and (t.get('auto_tags')
                                                     or t.get('flow_tags')
                                                     or t.get('primary_flow')))
            tag_pct = 100.0 * tagged / len(corpus)
            metrics['flow_registry.test_tag_coverage_pct'] = round(tag_pct, 1)
            metrics['flow_registry.tag_corpus_used'] = str(corpus_for_tags)
            if tag_pct < 70.0:
                c = Check('flow_registry.content.test_tag_coverage', FAIL,
                          f'only {tag_pct:.1f}% of corpus tests have '
                          f'auto_tags / primary_flow (<70% threshold)')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_flow_registry_low_tag_coverage',
                    message=c.detail, target=str(fr_path),
                    fix=FIX_REBUILD_FLOW_REGISTRY,
                    fix_command='python3 build_flow_registry.py',
                    details={'tag_pct': round(tag_pct, 1),
                             'tagged': tagged,
                             'total': len(corpus)},
                ))
            else:
                checks.append(Check(
                    'flow_registry.content.test_tag_coverage', PASS,
                    f'{tag_pct:.1f}% tagged'))

    return checks, issues, metrics


def validate_flow_dependencies_content(kb_dir: Path
                                       ) -> Tuple[List[Check], List[AuditIssue],
                                                  Dict[str, Any]]:
    """Deep content validation for flow_dependencies.json.

    Detects:
      - Dangling component references (component not in component_map)
      - Flow names not present in flow_registry (cross-file inconsistency)
      - Suspicious dependency_type values (DIRECT/INDIRECT only)
      - Orphan flows (flow_registry flows with zero dep entries)
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    fd_path = Path(kb_dir) / 'flow_dependencies.json'
    fd_payload = _safe_load_json(fd_path)
    if not fd_payload:
        return checks, issues, metrics

    fr_payload = _safe_load_json(Path(kb_dir) / 'flow_registry.json') or {}
    cm_payload = _safe_load_json(Path(kb_dir) / 'component_map.json') or {}

    deps = (fd_payload.get('dependencies')
            or fd_payload.get('flow_dependencies') or [])
    metrics['flow_dependencies.total'] = len(deps)
    if not deps:
        return checks, issues, metrics

    cm_components = {
        str(c.get('component_name'))
        for c in (cm_payload.get('components') or [])
        if c.get('component_name')
    }
    fr_flows = {
        str(f.get('flow_name'))
        for f in (fr_payload.get('flows') or [])
        if f.get('flow_name')
    }

    # 1. Dangling components.
    dangling = sorted({
        str(d.get('component'))
        for d in deps
        if isinstance(d, dict) and d.get('component')
        and str(d.get('component')) not in cm_components
    })
    metrics['flow_dependencies.dangling_components'] = len(dangling)
    if cm_components and dangling:
        c = Check('flow_dependencies.content.dangling_components', FAIL,
                  f'{len(dangling)} components referenced in flow_dependencies '
                  f'are absent from component_map.json')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_flow_dependencies_dangling_components',
            message=c.detail, target=str(fd_path),
            fix=FIX_REBUILD_FLOW_DEPENDENCIES,
            fix_command='python3 build_flow_dependencies.py',
            details={'dangling_sample': dangling[:5]},
        ))
    else:
        checks.append(Check('flow_dependencies.content.dangling_components',
                            PASS, '0 dangling component references'))

    # 2. Flow-name inconsistency vs registry.
    unknown_flows = sorted({
        str(d.get('flow'))
        for d in deps
        if isinstance(d, dict) and d.get('flow')
        and str(d.get('flow')) not in fr_flows
    })
    metrics['flow_dependencies.unknown_flows'] = len(unknown_flows)
    if fr_flows and unknown_flows:
        c = Check('flow_dependencies.content.flow_name_consistency', FAIL,
                  f'{len(unknown_flows)} flow names appear in '
                  f'flow_dependencies but not in flow_registry')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_flow_dependencies_inconsistent_flows',
            message=c.detail, target=str(fd_path),
            fix=FIX_REBUILD_FLOW_DEPENDENCIES,
            fix_command='python3 build_flow_dependencies.py',
            details={'unknown_sample': unknown_flows[:5]},
        ))
    else:
        checks.append(Check('flow_dependencies.content.flow_name_consistency',
                            PASS, 'all flow names exist in flow_registry'))

    # 3. dependency_type validity.
    valid_types = {'DIRECT', 'INDIRECT', 'TRANSITIVE'}
    bad_types = [d.get('dependency_type') for d in deps
                 if isinstance(d, dict) and d.get('dependency_type')
                 and d.get('dependency_type') not in valid_types]
    metrics['flow_dependencies.bad_dependency_types'] = len(bad_types)
    if bad_types:
        c = Check('flow_dependencies.content.dependency_type', FAIL,
                  f'{len(bad_types)} entries have invalid dependency_type '
                  f'(expected DIRECT/INDIRECT/TRANSITIVE)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_flow_dependencies_bad_type',
            message=c.detail, target=str(fd_path),
            fix=FIX_REBUILD_FLOW_DEPENDENCIES,
            fix_command='python3 build_flow_dependencies.py',
            details={'sample': bad_types[:5]},
        ))
    else:
        checks.append(Check('flow_dependencies.content.dependency_type',
                            PASS, 'all dependency_types valid'))

    # 4. Orphan flows: flows registered but with zero dep entries.
    flows_with_deps = {str(d.get('flow')) for d in deps
                       if isinstance(d, dict) and d.get('flow')}
    orphans = sorted(fr_flows - flows_with_deps) if fr_flows else []
    metrics['flow_dependencies.orphan_flows'] = len(orphans)
    if fr_flows and len(orphans) and len(orphans) / len(fr_flows) > 0.30:
        c = Check('flow_dependencies.content.orphan_flows', FAIL,
                  f'{len(orphans)}/{len(fr_flows)} flows have ZERO '
                  f'dependency entries')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_flow_dependencies_orphan_flows',
            message=c.detail, target=str(fd_path),
            fix=FIX_REBUILD_FLOW_DEPENDENCIES,
            fix_command='python3 build_flow_dependencies.py',
            details={'orphan_sample': orphans[:5]},
        ))
    else:
        checks.append(Check('flow_dependencies.content.orphan_flows', PASS,
                            f'{len(orphans)} orphan flow(s)'))

    return checks, issues, metrics


# ---------------------------------------------------------------------------
# Deep validators for the remaining 9 KB artifacts (100% coverage).
# ---------------------------------------------------------------------------
def _resolve_enriched_corpus_path(kb_dir: Path,
                                  change_type: Optional[str]) -> Path:
    """Return the enriched-corpus file the auditor should validate.

    Option A (separate enriched corpus per pipeline):
      - all_tcs_extracted_enriched_source.json     (source pipeline)
      - all_tcs_extracted_enriched_dependency.json (dependency pipeline)

    The legacy backward-compat file (`all_tcs_extracted_enriched.json`) is no
    longer produced.

    Resolution:
      change_type starts with 'dependency'  -> the *_dependency file
      change_type starts with 'source'      -> the *_source file
      otherwise                              -> default to the *_source file
                                                (canonical enriched corpus).
    """
    kb_dir = Path(kb_dir)
    source_path = kb_dir / 'all_tcs_extracted_enriched_source.json'
    dep_path = kb_dir / 'all_tcs_extracted_enriched_dependency.json'
    if not change_type:
        return source_path
    ct = str(change_type).strip().lower()
    if ct.startswith('dependency'):
        return dep_path if dep_path.exists() else source_path
    if ct.startswith('source'):
        return source_path
    return source_path


def validate_enriched_corpus_content(kb_dir: Path,
                                     raw_corpus_path: Optional[Path],
                                     change_type: Optional[str] = None,
                                     ) -> Tuple[List[Check], List[AuditIssue],
                                                Dict[str, Any]]:
    """Deep content validation for all_tcs_extracted_enriched.json.

    Detects:
      - Missing or invalid JSON
      - Truncation (< 70% of raw corpus)
      - Missing required per-test fields
      - primary_flow not in auto_tags
      - flow tags referencing flows absent from flow_registry.json
      - Duplicate issue_key entries
      - Stale relative to flow_registry.json
      - Bogus discovered_entry_points (file does not resolve)
      - flow_scores shape problems / primary_flow not max-scored

    When ``change_type`` is provided the validator inspects the per-pipeline
    enriched corpus (`*_source.json` / `*_dependency.json`) instead of the
    legacy single-pipeline file. This matches the per-pipeline registry the
    auditor already validates against (flow_registry.json vs.
    flow_registry_dependency.json).
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    target = _resolve_enriched_corpus_path(kb_dir, change_type)
    metrics['enriched_corpus.audited_file'] = str(target)
    if not target.exists():
        # Structural validator already flagged optional KB file missing.
        return checks, issues, metrics

    ok, payload, err = _read_json(target)
    if not ok:
        c = Check('enriched_corpus.content.valid_json', FAIL,
                  f'cannot parse: {err}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_enriched_corpus_invalid_json',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_FLOW_REGISTRY,
            fix_command='python3 build_flow_registry.py',
        ))
        return checks, issues, metrics

    if not isinstance(payload, list):
        c = Check('enriched_corpus.content.shape', FAIL,
                  'top-level structure is not a list')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_enriched_corpus_bad_shape',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_FLOW_REGISTRY,
            fix_command='python3 build_flow_registry.py',
        ))
        return checks, issues, metrics

    enriched = payload
    metrics['enriched_corpus.length'] = len(enriched)
    checks.append(Check('enriched_corpus.content.valid_json', PASS,
                        f'list with {len(enriched)} entries'))

    # 1. Length vs raw corpus.
    if raw_corpus_path and Path(raw_corpus_path).exists():
        raw = _safe_load_json(Path(raw_corpus_path)) or []
        raw_n = len(raw) if isinstance(raw, list) else 0
        metrics['enriched_corpus.raw_corpus_length'] = raw_n
        if raw_n > 0:
            ratio = len(enriched) / raw_n
            metrics['enriched_corpus.coverage_ratio'] = round(ratio, 3)
            if ratio < 0.70:
                c = Check('enriched_corpus.content.length_vs_raw', FAIL,
                          f'enriched={len(enriched)} is {ratio*100:.0f}% of '
                          f'raw corpus ({raw_n}); expected >=70%')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_ERROR,
                    type='stage0_enriched_corpus_truncated',
                    message=c.detail, target=str(target),
                    fix=FIX_REBUILD_FLOW_REGISTRY,
                    fix_command='python3 build_flow_registry.py',
                    details={'enriched': len(enriched),
                             'raw': raw_n,
                             'ratio_pct': round(ratio * 100, 1)},
                ))
            elif ratio > 1.0:
                c = Check('enriched_corpus.content.length_vs_raw', FAIL,
                          f'enriched={len(enriched)} exceeds raw corpus '
                          f'({raw_n}); duplicate inflation suspected')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_enriched_corpus_inflated',
                    message=c.detail, target=str(target),
                    fix=FIX_REBUILD_FLOW_REGISTRY,
                    fix_command='python3 build_flow_registry.py',
                    details={'enriched': len(enriched), 'raw': raw_n},
                ))
            else:
                checks.append(Check('enriched_corpus.content.length_vs_raw',
                                    PASS,
                                    f'{ratio*100:.1f}% of raw corpus tagged'))

    # 2. Required fields per test (sample first 200 to keep it fast).
    required = ('issue_key', 'auto_tags', 'primary_flow',
                'discovered_entry_points', 'flow_scores')
    missing_field_count = 0
    sample_size = min(len(enriched), 200)
    for tc in enriched[:sample_size]:
        if not isinstance(tc, dict):
            missing_field_count += 1
            continue
        if not all(k in tc for k in required):
            missing_field_count += 1
            continue
        if not tc.get('auto_tags'):
            missing_field_count += 1
    metrics['enriched_corpus.missing_field_sample'] = missing_field_count
    if sample_size and missing_field_count / sample_size > 0.05:
        c = Check('enriched_corpus.content.required_fields', FAIL,
                  f'{missing_field_count}/{sample_size} sampled tests are '
                  f'missing required fields or have empty auto_tags')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_enriched_corpus_missing_fields',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_FLOW_REGISTRY,
            fix_command='python3 build_flow_registry.py',
            details={'missing_count': missing_field_count,
                     'sample_size': sample_size},
        ))
    else:
        checks.append(Check('enriched_corpus.content.required_fields', PASS,
                            f'{sample_size - missing_field_count}/'
                            f'{sample_size} sampled tests have all required '
                            'fields'))

    # 3. primary_flow consistency (must be in auto_tags).
    pf_inconsistent = 0
    for tc in enriched[:sample_size]:
        if not isinstance(tc, dict):
            continue
        pf = tc.get('primary_flow')
        tags = tc.get('auto_tags') or []
        if pf and isinstance(tags, list) and pf not in tags:
            pf_inconsistent += 1
    metrics['enriched_corpus.primary_flow_inconsistencies'] = pf_inconsistent
    if pf_inconsistent > 0:
        c = Check('enriched_corpus.content.primary_flow_in_tags', FAIL,
                  f'{pf_inconsistent}/{sample_size} tests have primary_flow '
                  'NOT present in auto_tags')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_enriched_corpus_primary_flow_inconsistency',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_FLOW_REGISTRY,
            fix_command='python3 build_flow_registry.py',
            details={'inconsistent_count': pf_inconsistent,
                     'sample_size': sample_size},
        ))
    else:
        checks.append(Check('enriched_corpus.content.primary_flow_in_tags',
                            PASS, 'primary_flow consistent with auto_tags'))

    # 4. Cross-ref auto_tags vs flow_registry.json.
    fr_path = Path(kb_dir) / 'flow_registry.json'
    fr_payload = _safe_load_json(fr_path) or {}
    known_tags: set = set()
    for f in (fr_payload.get('flows') or []):
        for t in (f.get('test_tags') or []):
            known_tags.add(str(t))
        # Tag may also live under flow_id / flow_name in some builds.
        if f.get('flow_id'):
            known_tags.add(str(f['flow_id']))
        if f.get('flow_name'):
            known_tags.add(str(f['flow_name']))
    if known_tags:
        unknown_tag_tests = 0
        for tc in enriched[:sample_size]:
            if not isinstance(tc, dict):
                continue
            tags = tc.get('auto_tags') or []
            if not isinstance(tags, list):
                continue
            for t in tags:
                if str(t) not in known_tags:
                    unknown_tag_tests += 1
                    break
        metrics['enriched_corpus.unknown_tag_tests'] = unknown_tag_tests
        if sample_size and unknown_tag_tests / sample_size > 0.10:
            c = Check('enriched_corpus.content.tag_cross_reference', FAIL,
                      f'{unknown_tag_tests}/{sample_size} tests reference '
                      'flow tags absent from flow_registry.json')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_ERROR,
                type='stage0_enriched_corpus_dangling_tags',
                message=c.detail, target=str(target),
                fix=FIX_REBUILD_FLOW_REGISTRY,
                fix_command='python3 build_flow_registry.py',
                details={'unknown_tag_tests': unknown_tag_tests,
                         'sample_size': sample_size},
            ))
        else:
            checks.append(Check('enriched_corpus.content.tag_cross_reference',
                                PASS,
                                f'{unknown_tag_tests}/{sample_size} tests '
                                'reference unknown flow tags'))

    # 5. Duplicate issue_key.
    try:
        from collections import Counter
        issue_keys = [str(t.get('issue_key')) for t in enriched
                      if isinstance(t, dict) and t.get('issue_key')]
        counter = Counter(issue_keys)
        duplicates = [k for k, v in counter.items() if v > 1]
        metrics['enriched_corpus.duplicate_issue_keys'] = len(duplicates)
        if duplicates:
            c = Check('enriched_corpus.content.unique_issue_keys', FAIL,
                      f'{len(duplicates)} duplicate issue_key values found')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_ERROR,
                type='stage0_enriched_corpus_duplicate_keys',
                message=c.detail, target=str(target),
                fix=FIX_REBUILD_FLOW_REGISTRY,
                fix_command='python3 build_flow_registry.py',
                details={'duplicate_sample': duplicates[:5],
                         'duplicate_count': len(duplicates)},
            ))
        else:
            checks.append(Check('enriched_corpus.content.unique_issue_keys',
                                PASS, f'{len(issue_keys)} unique issue_keys'))
    except Exception as exc:  # pragma: no cover - defensive
        checks.append(Check('enriched_corpus.content.unique_issue_keys', FAIL,
                            f'duplicate detection crashed: {exc}'))

    # 6. Freshness vs flow_registry.json.
    #
    # build_flow_registry.py writes flow_registry.json and the enriched
    # corpus in the same call, milliseconds apart, with the enriched corpus
    # written FIRST. So the natural ordering is enriched_mtime <
    # flow_registry_mtime by a sub-millisecond margin. Treating that as a
    # "stale rebuild" produces a false positive on every healthy run -- we
    # require a meaningful skew (>= 5 seconds) before flagging.
    _STALE_SKEW_SECS = 5.0
    try:
        if fr_path.exists():
            enriched_mtime = target.stat().st_mtime
            fr_mtime = fr_path.stat().st_mtime
            skew = fr_mtime - enriched_mtime
            if skew > _STALE_SKEW_SECS:
                c = Check('enriched_corpus.content.freshness', FAIL,
                          f'enriched corpus is {skew:.1f}s older than '
                          f'flow_registry.json (stale rebuild)')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_enriched_corpus_stale',
                    message=c.detail, target=str(target),
                    fix=FIX_REBUILD_FLOW_REGISTRY,
                    fix_command='python3 build_flow_registry.py',
                    details={'enriched_mtime': enriched_mtime,
                             'flow_registry_mtime': fr_mtime,
                             'skew_secs': round(skew, 3)},
                ))
            else:
                checks.append(Check('enriched_corpus.content.freshness', PASS,
                                    f'enriched mtime within '
                                    f'{_STALE_SKEW_SECS:.0f}s of '
                                    f'flow_registry'))
    except OSError as exc:
        checks.append(Check('enriched_corpus.content.freshness', FAIL,
                            f'mtime check failed: {exc}'))

    # 7. discovered_entry_points format + file resolution (sample 50).
    bad_ep = 0
    sampled_ep = 0
    for tc in enriched[:50]:
        if not isinstance(tc, dict):
            continue
        for ep in (tc.get('discovered_entry_points') or [])[:2]:
            sampled_ep += 1
            if not isinstance(ep, str) or ':' not in ep:
                bad_ep += 1
                continue
            file_part, _, _ = ep.partition(':')
            if not _resolve_repo_path(Path(REPO_ROOT), file_part):
                bad_ep += 1
    metrics['enriched_corpus.bad_entry_points_sampled'] = bad_ep
    if sampled_ep and bad_ep / sampled_ep > 0.30:
        c = Check('enriched_corpus.content.entry_point_validity', FAIL,
                  f'{bad_ep}/{sampled_ep} sampled discovered_entry_points '
                  'point at missing files / wrong format')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_enriched_corpus_bad_entry_points',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_FLOW_REGISTRY,
            fix_command='python3 build_flow_registry.py',
            details={'bad_count': bad_ep, 'sampled': sampled_ep},
        ))
    else:
        checks.append(Check('enriched_corpus.content.entry_point_validity',
                            PASS,
                            f'{bad_ep}/{sampled_ep} bad entry points'))

    # 8. flow_scores shape + primary_flow == argmax.
    score_argmax_violations = 0
    score_shape_violations = 0
    for tc in enriched[:sample_size]:
        if not isinstance(tc, dict):
            continue
        scores = tc.get('flow_scores')
        pf = tc.get('primary_flow')
        if not isinstance(scores, dict):
            score_shape_violations += 1
            continue
        try:
            if scores and pf:
                top_flow = max(scores.items(), key=lambda x: float(x[1]))[0]
                if str(top_flow) != str(pf):
                    score_argmax_violations += 1
        except (ValueError, TypeError):
            score_shape_violations += 1
    metrics['enriched_corpus.flow_scores_shape_bad'] = score_shape_violations
    metrics['enriched_corpus.flow_scores_argmax_violations'] = (
        score_argmax_violations)
    if score_shape_violations and (
            score_shape_violations / max(sample_size, 1) > 0.05):
        c = Check('enriched_corpus.content.flow_scores_shape', FAIL,
                  f'{score_shape_violations}/{sample_size} tests have '
                  'malformed flow_scores')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_enriched_corpus_flow_scores_shape',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_FLOW_REGISTRY,
            fix_command='python3 build_flow_registry.py',
            details={'shape_violations': score_shape_violations,
                     'sample_size': sample_size},
        ))
    else:
        checks.append(Check('enriched_corpus.content.flow_scores_shape', PASS,
                            f'{score_shape_violations} shape issues'))

    if score_argmax_violations > 0 and (
            score_argmax_violations / max(sample_size, 1) > 0.05):
        c = Check('enriched_corpus.content.primary_flow_argmax', FAIL,
                  f'{score_argmax_violations}/{sample_size} tests have a '
                  'flow with higher score than primary_flow')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_enriched_corpus_primary_flow_argmax',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_FLOW_REGISTRY,
            fix_command='python3 build_flow_registry.py',
            details={'argmax_violations': score_argmax_violations,
                     'sample_size': sample_size},
        ))
    else:
        checks.append(Check('enriched_corpus.content.primary_flow_argmax',
                            PASS,
                            f'{score_argmax_violations} argmax violations'))

    return checks, issues, metrics


def validate_idf_index_content(kb_dir: Path,
                               raw_corpus_path: Optional[Path]
                               ) -> Tuple[List[Check], List[AuditIssue],
                                          Dict[str, Any]]:
    """Deep content validation for idf_index.json.

    Detects:
      - Missing keys / malformed structure
      - total_documents drift vs raw corpus
      - Empty / collapsed idf map
      - Distribution collapse (p25 == p50 == p75)
      - Missing statistics (BM25 params, specificity thresholds)
      - Discriminative tail too narrow (< 30% terms with df/N < 0.10)
      - Stale relative to raw corpus
      - Domain tokens absent from idf map
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    target = Path(kb_dir) / 'idf_index.json'
    if not target.exists():
        return checks, issues, metrics

    ok, payload, err = _read_json(target)
    if not ok or not isinstance(payload, dict):
        c = Check('idf_index.content.valid_json', FAIL,
                  f'cannot parse / not a dict: {err}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_idf_invalid',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_IDF,
            fix_command='python3 term_idf.py',
        ))
        return checks, issues, metrics

    required_keys = ('total_documents', 'document_frequency', 'idf',
                     'statistics', 'version')
    missing = [k for k in required_keys if k not in payload]
    if missing:
        c = Check('idf_index.content.required_keys', FAIL,
                  f'missing keys: {missing}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_idf_missing_keys',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_IDF,
            fix_command='python3 term_idf.py',
            details={'missing_keys': missing},
        ))
    else:
        checks.append(Check('idf_index.content.required_keys', PASS,
                            'all required keys present'))

    total_docs = int(payload.get('total_documents', 0) or 0)
    idf_map = payload.get('idf') or {}
    statistics = payload.get('statistics') or {}
    metrics['idf_index.total_documents'] = total_docs
    metrics['idf_index.idf_size'] = len(idf_map) if isinstance(idf_map,
                                                                dict) else 0

    # 1. total_documents matches raw corpus.
    if raw_corpus_path and Path(raw_corpus_path).exists():
        raw = _safe_load_json(Path(raw_corpus_path)) or []
        raw_n = len(raw) if isinstance(raw, list) else 0
        metrics['idf_index.raw_corpus_length'] = raw_n
        if raw_n > 0:
            drift = abs(total_docs - raw_n) / raw_n
            if drift > 0.02:
                c = Check('idf_index.content.total_documents', FAIL,
                          f'total_documents={total_docs} vs raw corpus '
                          f'{raw_n} (drift {drift*100:.1f}%, expected <=2%)')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_ERROR,
                    type='stage0_idf_total_documents_drift',
                    message=c.detail, target=str(target),
                    fix=FIX_REBUILD_IDF,
                    fix_command='python3 term_idf.py',
                    details={'total_documents': total_docs,
                             'raw_corpus_length': raw_n,
                             'drift_pct': round(drift * 100, 1)},
                ))
            else:
                checks.append(Check('idf_index.content.total_documents', PASS,
                                    f'{total_docs} matches raw corpus'))

    # 2. idf size sanity.
    if isinstance(idf_map, dict):
        if total_docs >= 1000 and len(idf_map) < 200:
            c = Check('idf_index.content.idf_size', FAIL,
                      f'idf has only {len(idf_map)} terms for '
                      f'{total_docs} docs (expected >=200)')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_ERROR,
                type='stage0_idf_collapsed',
                message=c.detail, target=str(target),
                fix=FIX_REBUILD_IDF,
                fix_command='python3 term_idf.py',
                details={'idf_size': len(idf_map),
                         'total_documents': total_docs},
            ))
        else:
            checks.append(Check('idf_index.content.idf_size', PASS,
                                f'{len(idf_map)} terms'))

    # 3. statistics block populated.
    needed_stats = ('p25', 'p50', 'p75', 'specificity_3pct',
                    'bm25_k1', 'bm25_b', 'avg_doc_length')
    missing_stats = [k for k in needed_stats if k not in statistics]
    if missing_stats:
        c = Check('idf_index.content.statistics', FAIL,
                  f'statistics missing keys: {missing_stats}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_idf_missing_statistics',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_IDF,
            fix_command='python3 term_idf.py',
            details={'missing_stats': missing_stats},
        ))
    else:
        checks.append(Check('idf_index.content.statistics', PASS,
                            'statistics block fully populated'))

    # 4. Distribution non-flat.
    try:
        p25 = float(statistics.get('p25', 0) or 0)
        p50 = float(statistics.get('p50', 0) or 0)
        p75 = float(statistics.get('p75', 0) or 0)
        metrics['idf_index.p25'] = p25
        metrics['idf_index.p50'] = p50
        metrics['idf_index.p75'] = p75
        if p25 == p50 == p75:
            c = Check('idf_index.content.distribution', FAIL,
                      f'distribution is flat (p25=p50=p75={p25})')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_ERROR,
                type='stage0_idf_distribution_collapse',
                message=c.detail, target=str(target),
                fix=FIX_REBUILD_IDF,
                fix_command='python3 term_idf.py',
                details={'p25': p25, 'p50': p50, 'p75': p75},
            ))
        elif not (p75 > p50 > p25):
            c = Check('idf_index.content.distribution', FAIL,
                      f'distribution out of order: p25={p25} p50={p50} '
                      f'p75={p75}')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_WARN,
                type='stage0_idf_distribution_out_of_order',
                message=c.detail, target=str(target),
                fix=FIX_REBUILD_IDF,
                fix_command='python3 term_idf.py',
                details={'p25': p25, 'p50': p50, 'p75': p75},
            ))
        else:
            checks.append(Check('idf_index.content.distribution', PASS,
                                f'p25={p25:.2f} p50={p50:.2f} p75={p75:.2f}'))
    except (TypeError, ValueError) as exc:
        checks.append(Check('idf_index.content.distribution', FAIL,
                            f'distribution check crashed: {exc}'))

    # 5. Discriminative tail.
    df_map = payload.get('document_frequency') or {}
    if isinstance(df_map, dict) and total_docs > 0:
        threshold = total_docs * 0.10
        discrim = sum(1 for v in df_map.values()
                      if isinstance(v, (int, float)) and v < threshold)
        ratio = discrim / max(len(df_map), 1)
        metrics['idf_index.discriminative_tail_pct'] = round(ratio * 100, 1)
        if ratio < 0.30:
            c = Check('idf_index.content.discriminative_tail', FAIL,
                      f'only {ratio*100:.1f}% of terms have df/N < 0.10 '
                      f'(expected >30%)')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_WARN,
                type='stage0_idf_narrow_tail',
                message=c.detail, target=str(target),
                fix=FIX_REBUILD_IDF,
                fix_command='python3 term_idf.py',
                details={'discriminative_pct': round(ratio * 100, 1)},
            ))
        else:
            checks.append(Check('idf_index.content.discriminative_tail', PASS,
                                f'{ratio*100:.1f}% terms in discriminative '
                                'tail'))

    # 6. Freshness vs raw corpus.
    if raw_corpus_path and Path(raw_corpus_path).exists():
        try:
            idf_mtime = target.stat().st_mtime
            raw_mtime = Path(raw_corpus_path).stat().st_mtime
            if idf_mtime < raw_mtime:
                c = Check('idf_index.content.freshness', FAIL,
                          'idf_index older than raw corpus (stale)')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_idf_stale',
                    message=c.detail, target=str(target),
                    fix=FIX_REBUILD_IDF,
                    fix_command='python3 term_idf.py',
                    details={'idf_mtime': idf_mtime,
                             'raw_mtime': raw_mtime},
                ))
            else:
                checks.append(Check('idf_index.content.freshness', PASS,
                                    'idf newer than raw corpus'))
        except OSError as exc:
            checks.append(Check('idf_index.content.freshness', FAIL,
                                f'mtime check failed: {exc}'))

    # 7. Spot-check 3 domain tokens appear in idf.
    dv_payload = _safe_load_json(Path(kb_dir) / 'domain_vocabulary.json') or {}
    dv_tokens = list(dv_payload.get('domain_tokens') or [])[:50]
    if dv_tokens and isinstance(idf_map, dict):
        found = sum(1 for t in dv_tokens[:3] if str(t).lower() in idf_map)
        metrics['idf_index.domain_token_hits_sample3'] = found
        if found == 0 and len(dv_tokens) >= 3:
            c = Check('idf_index.content.domain_token_presence', FAIL,
                      'sampled 3 domain tokens are absent from idf map')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_WARN,
                type='stage0_idf_no_domain_tokens',
                message=c.detail, target=str(target),
                fix=FIX_REBUILD_IDF,
                fix_command='python3 term_idf.py',
                details={'sampled_tokens': dv_tokens[:3]},
            ))
        else:
            checks.append(Check('idf_index.content.domain_token_presence',
                                PASS, f'{found}/3 sampled domain tokens '
                                'present in idf'))

    return checks, issues, metrics


def validate_generic_nouns_content(kb_dir: Path,
                                   enriched_corpus_path: Optional[Path]
                                   ) -> Tuple[List[Check], List[AuditIssue],
                                              Dict[str, Any]]:
    """Deep content validation for discovered_generic_nouns.json.

    Detects:
      - Missing keys / malformed structure
      - Count outside the expected band (30..300)
      - Mutual exclusion violation vs domain_vocabulary
      - n_tests drifted vs enriched corpus length
      - Stale vs component_map / flow_registry
      - Internal summary inconsistency
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    target = Path(kb_dir) / 'discovered_generic_nouns.json'
    if not target.exists():
        return checks, issues, metrics

    ok, payload, err = _read_json(target)
    if not ok or not isinstance(payload, dict):
        c = Check('generic_nouns.content.valid_json', FAIL,
                  f'cannot parse / not a dict: {err}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_generic_nouns_invalid',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 build_discovered_vocabularies.py '
                        '--only vocabularies',
        ))
        return checks, issues, metrics

    required = ('generic_nouns', 'n_tests')
    missing = [k for k in required if k not in payload]
    if missing:
        c = Check('generic_nouns.content.required_keys', FAIL,
                  f'missing keys: {missing}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_generic_nouns_missing_keys',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 build_discovered_vocabularies.py '
                        '--only vocabularies',
            details={'missing_keys': missing},
        ))
    else:
        checks.append(Check('generic_nouns.content.required_keys', PASS,
                            'required keys present'))

    nouns = payload.get('generic_nouns') or []
    n_tests = int(payload.get('n_tests', 0) or 0)
    metrics['generic_nouns.count'] = (len(nouns) if isinstance(nouns, list)
                                      else 0)
    metrics['generic_nouns.n_tests'] = n_tests

    # 1. Count band 30..300.
    n = len(nouns) if isinstance(nouns, list) else 0
    if n < 30 or n > 300:
        c = Check('generic_nouns.content.count_band', FAIL,
                  f'generic_nouns count={n}, expected 30..300')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_generic_nouns_count_band',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 build_discovered_vocabularies.py '
                        '--only vocabularies',
            details={'count': n},
        ))
    else:
        checks.append(Check('generic_nouns.content.count_band', PASS,
                            f'{n} entries in band'))

    # 2. Mutual exclusion vs domain_vocabulary.
    dv_payload = _safe_load_json(Path(kb_dir) / 'domain_vocabulary.json') or {}
    dv_tokens = set(str(t).lower() for t in (dv_payload.get('domain_tokens')
                                              or []))
    overlap = (set(str(x).lower() for x in nouns) & dv_tokens
               if isinstance(nouns, list) else set())
    metrics['generic_nouns.domain_overlap'] = len(overlap)
    if overlap:
        c = Check('generic_nouns.content.domain_mutual_exclusion', FAIL,
                  f'{len(overlap)} tokens appear in BOTH generic_nouns and '
                  'domain_vocabulary (mutual-exclusion violation)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_generic_nouns_domain_overlap',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 build_discovered_vocabularies.py '
                        '--only vocabularies',
            details={'overlap_sample': sorted(overlap)[:5]},
        ))
    else:
        checks.append(Check('generic_nouns.content.domain_mutual_exclusion',
                            PASS, '0 overlap with domain_vocabulary'))

    # 3. n_tests vs enriched corpus length.
    #
    # n_tests in discovered_generic_nouns.json is the count of the RAW
    # test corpus consumed at vocabulary-discovery time. The enriched
    # corpus is a STRICT SUBSET of the raw corpus: build_flow_registry.py
    # writes only tests that scored at/above the adaptive_min_score
    # threshold (untagged tests are intentionally excluded - see
    # build_flow_registry.py "NO FALLBACK" block, ~line 1431).
    #
    # Therefore n_tests >= enriched_n is the EXPECTED relationship, not a
    # bug. We only flag this when:
    #   * n_tests < enriched_n (impossible unless one of the files is from
    #     an older build; that IS a real staleness signal), OR
    #   * the untagged ratio exceeds 30% (signals synonym_groups gaps that
    #     are bad enough to leave a third of the corpus unmatched - this
    #     is a quality signal worth surfacing).
    if enriched_corpus_path and Path(enriched_corpus_path).exists():
        enriched = _safe_load_json(Path(enriched_corpus_path)) or []
        enriched_n = len(enriched) if isinstance(enriched, list) else 0
        metrics['generic_nouns.enriched_length'] = enriched_n
        if enriched_n > 0 and n_tests > 0:
            untagged = max(n_tests - enriched_n, 0)
            untagged_pct = 100.0 * untagged / n_tests
            metrics['generic_nouns.untagged_ratio_pct'] = round(
                untagged_pct, 1)
            if enriched_n > n_tests:
                # enriched > raw: the two files were built at different
                # times against different corpora. Real staleness.
                c = Check('generic_nouns.content.n_tests_match', FAIL,
                          f'enriched corpus ({enriched_n}) larger than '
                          f'raw n_tests ({n_tests}) - rebuild order '
                          f'inverted')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_generic_nouns_n_tests_drift',
                    message=c.detail, target=str(target),
                    fix=FIX_REBUILD_DISCOVERED_VOCAB,
                    fix_command='python3 build_discovered_vocabularies.py '
                                '--only vocabularies',
                    details={'n_tests': n_tests,
                             'enriched_length': enriched_n,
                             'untagged_pct': 0.0},
                ))
            elif untagged_pct > 30.0:
                # Too many tests fail to enrich -> synonym_groups gap.
                c = Check('generic_nouns.content.n_tests_match', FAIL,
                          f'{untagged}/{n_tests} tests ({untagged_pct:.1f}%) '
                          f'fail to enrich into flow_registry - synonym '
                          f'groups likely incomplete')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_generic_nouns_n_tests_drift',
                    message=c.detail, target=str(target),
                    fix=FIX_REBUILD_SYNONYM_GROUPS,
                    fix_command='python3 build_synonym_groups.py',
                    details={'n_tests': n_tests,
                             'enriched_length': enriched_n,
                             'untagged_pct': round(untagged_pct, 1)},
                ))
            else:
                checks.append(Check('generic_nouns.content.n_tests_match',
                                    PASS,
                                    f'{n_tests} raw, {enriched_n} '
                                    f'enriched ({untagged_pct:.1f}% '
                                    f'untagged)'))

    # 4. Freshness vs component_map / flow_registry.
    try:
        target_mtime = target.stat().st_mtime
        for upstream_name in ('component_map.json', 'flow_registry.json'):
            up_path = Path(kb_dir) / upstream_name
            if not up_path.exists():
                continue
            up_mtime = up_path.stat().st_mtime
            if target_mtime < up_mtime:
                c = Check(f'generic_nouns.content.freshness.{upstream_name}',
                          FAIL,
                          f'generic_nouns older than {upstream_name}')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_generic_nouns_stale',
                    message=c.detail, target=str(target),
                    fix=FIX_REBUILD_DISCOVERED_VOCAB,
                    fix_command='python3 build_discovered_vocabularies.py '
                                '--only vocabularies',
                    details={'upstream': upstream_name,
                             'target_mtime': target_mtime,
                             'upstream_mtime': up_mtime},
                ))
            else:
                checks.append(Check(
                    f'generic_nouns.content.freshness.{upstream_name}', PASS,
                    'newer than upstream'))
    except OSError as exc:
        checks.append(Check('generic_nouns.content.freshness', FAIL,
                            f'mtime check failed: {exc}'))

    # 5. Sample 3 obvious generic verbs present.
    if isinstance(nouns, list):
        nouns_lower = set(str(x).lower() for x in nouns)
        sentinels = ('get', 'set', 'do')
        present = [s for s in sentinels if s in nouns_lower]
        metrics['generic_nouns.sentinel_hits'] = len(present)
        if len(present) == 0:
            c = Check('generic_nouns.content.sentinel_check', FAIL,
                      'none of the obvious generic verbs (get/set/do) are '
                      'present (MAD-z math may have run on wrong column)')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_WARN,
                type='stage0_generic_nouns_no_sentinels',
                message=c.detail, target=str(target),
                fix=FIX_REBUILD_DISCOVERED_VOCAB,
                fix_command='python3 build_discovered_vocabularies.py '
                            '--only vocabularies',
                details={'expected_sentinels': list(sentinels)},
            ))
        else:
            checks.append(Check('generic_nouns.content.sentinel_check', PASS,
                                f'{len(present)}/3 sentinels present'))

    # 6. summary.generic_count internal consistency.
    summary = payload.get('summary') or {}
    if isinstance(summary, dict) and 'generic_count' in summary:
        s_count = int(summary.get('generic_count', 0) or 0)
        if s_count != n:
            c = Check('generic_nouns.content.summary_consistency', FAIL,
                      f'summary.generic_count={s_count} but '
                      f'len(generic_nouns)={n}')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_WARN,
                type='stage0_generic_nouns_summary_inconsistent',
                message=c.detail, target=str(target),
                fix=FIX_REBUILD_DISCOVERED_VOCAB,
                fix_command='python3 build_discovered_vocabularies.py '
                            '--only vocabularies',
                details={'summary_count': s_count, 'actual_count': n},
            ))
        else:
            checks.append(Check('generic_nouns.content.summary_consistency',
                                PASS, 'summary.generic_count matches'))

    return checks, issues, metrics


def validate_domain_vocabulary_content(kb_dir: Path
                                       ) -> Tuple[List[Check], List[AuditIssue],
                                                  Dict[str, Any]]:
    """Deep content validation for domain_vocabulary.json.

    Detects:
      - Missing keys / malformed structure
      - Count outside the expected band (200..20000)
      - Empty domain_tokens (FAIL hard)
      - Overlap with discovered_generic_nouns (mutual exclusion violation)
      - Overlap with language_reserved_words (tokenizer leak)
      - PascalCase tokens (capitalisation regression)
      - Component-keyword overlap below 50%
      - summary.domain_count internal inconsistency
      - Stale relative to upstream KB files
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    target = Path(kb_dir) / 'domain_vocabulary.json'
    if not target.exists():
        return checks, issues, metrics

    ok, payload, err = _read_json(target)
    if not ok or not isinstance(payload, dict):
        c = Check('domain_vocabulary.content.valid_json', FAIL,
                  f'cannot parse / not a dict: {err}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_domain_vocab_invalid',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 build_discovered_vocabularies.py '
                        '--only vocabularies',
        ))
        return checks, issues, metrics

    tokens = payload.get('domain_tokens') or []
    summary = payload.get('summary') or {}
    n = len(tokens) if isinstance(tokens, list) else 0
    metrics['domain_vocabulary.count'] = n

    # 1. Empty / count band.
    if n == 0:
        c = Check('domain_vocabulary.content.count_band', FAIL,
                  'domain_tokens is empty (Stage 5 will hard-fail)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_domain_vocab_empty',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 build_discovered_vocabularies.py '
                        '--only vocabularies',
        ))
    elif n < 200 or n > 20000:
        c = Check('domain_vocabulary.content.count_band', FAIL,
                  f'domain_tokens count={n}, expected 200..20000')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_domain_vocab_count_band',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 build_discovered_vocabularies.py '
                        '--only vocabularies',
            details={'count': n},
        ))
    else:
        checks.append(Check('domain_vocabulary.content.count_band', PASS,
                            f'{n} tokens in band'))

    # 2. Overlap with generic_nouns.
    gn_payload = _safe_load_json(
        Path(kb_dir) / 'discovered_generic_nouns.json') or {}
    gn_set = set(str(t).lower() for t in (gn_payload.get('generic_nouns')
                                            or []))
    tokens_lower = (set(str(t).lower() for t in tokens)
                    if isinstance(tokens, list) else set())
    overlap_gn = tokens_lower & gn_set
    metrics['domain_vocabulary.generic_overlap'] = len(overlap_gn)
    if overlap_gn:
        c = Check('domain_vocabulary.content.generic_mutual_exclusion', FAIL,
                  f'{len(overlap_gn)} domain tokens overlap with '
                  'generic_nouns (mutual-exclusion violation)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_domain_vocab_generic_overlap',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 build_discovered_vocabularies.py '
                        '--only vocabularies',
            details={'overlap_sample': sorted(overlap_gn)[:5]},
        ))
    else:
        checks.append(Check('domain_vocabulary.content.generic_mutual_exclusion',
                            PASS, '0 overlap with generic_nouns'))

    # 3. Overlap with language_reserved_words (tokenizer leak).
    rw_payload = _safe_load_json(
        Path(kb_dir) / 'language_reserved_words.json') or {}
    rw_set = set(str(t).lower() for t in (rw_payload.get('reserved_words')
                                            or []))
    overlap_rw = tokens_lower & rw_set
    metrics['domain_vocabulary.reserved_overlap'] = len(overlap_rw)
    if overlap_rw:
        c = Check('domain_vocabulary.content.reserved_mutual_exclusion', FAIL,
                  f'{len(overlap_rw)} domain tokens are language reserved '
                  'words (tokenizer leak)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_domain_vocab_reserved_overlap',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 build_discovered_vocabularies.py '
                        '--only vocabularies',
            details={'overlap_sample': sorted(overlap_rw)[:5]},
        ))
    else:
        checks.append(Check('domain_vocabulary.content.reserved_mutual_exclusion',
                            PASS, '0 overlap with reserved words'))

    # 4. Lowercase only (no PascalCase).
    if isinstance(tokens, list):
        not_lower = [str(t) for t in tokens if str(t) != str(t).lower()]
        metrics['domain_vocabulary.non_lowercase'] = len(not_lower)
        if not_lower:
            c = Check('domain_vocabulary.content.lowercase', FAIL,
                      f'{len(not_lower)} tokens have non-lowercase forms '
                      '(PascalCase leak)')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_ERROR,
                type='stage0_domain_vocab_pascal_case',
                message=c.detail, target=str(target),
                fix=FIX_REBUILD_DISCOVERED_VOCAB,
                fix_command='python3 build_discovered_vocabularies.py '
                            '--only vocabularies',
                details={'sample': not_lower[:5]},
            ))
        else:
            checks.append(Check('domain_vocabulary.content.lowercase', PASS,
                                'all tokens lowercase'))

    # 5. summary.domain_count internal consistency.
    if isinstance(summary, dict) and 'domain_count' in summary:
        s_count = int(summary.get('domain_count', 0) or 0)
        if s_count != n:
            c = Check('domain_vocabulary.content.summary_consistency', FAIL,
                      f'summary.domain_count={s_count} but '
                      f'len(domain_tokens)={n}')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_ERROR,
                type='stage0_domain_vocab_summary_inconsistent',
                message=c.detail, target=str(target),
                fix=FIX_REBUILD_DISCOVERED_VOCAB,
                fix_command='python3 build_discovered_vocabularies.py '
                            '--only vocabularies',
                details={'summary_count': s_count, 'actual_count': n},
            ))
        else:
            checks.append(Check('domain_vocabulary.content.summary_consistency',
                                PASS, 'summary.domain_count matches'))

    # 6. Component-keyword overlap >= 50%.
    cm_payload = _safe_load_json(Path(kb_dir) / 'component_map.json') or {}
    cm_keywords: set = set()
    for comp in (cm_payload.get('components') or []):
        for kw in (comp.get('keywords') or []):
            cm_keywords.add(str(kw).lower())
    if cm_keywords and tokens_lower:
        coverage = len(tokens_lower & cm_keywords) / len(tokens_lower)
        metrics['domain_vocabulary.component_overlap_pct'] = round(
            coverage * 100, 1)
        if coverage < 0.50:
            c = Check('domain_vocabulary.content.component_overlap', FAIL,
                      f'only {coverage*100:.1f}% of domain tokens overlap '
                      'with component keywords (expected >=50%)')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_WARN,
                type='stage0_domain_vocab_low_component_overlap',
                message=c.detail, target=str(target),
                fix=FIX_REBUILD_DISCOVERED_VOCAB,
                fix_command='python3 build_discovered_vocabularies.py '
                            '--only vocabularies',
                details={'overlap_pct': round(coverage * 100, 1)},
            ))
        else:
            checks.append(Check('domain_vocabulary.content.component_overlap',
                                PASS,
                                f'{coverage*100:.1f}% overlap with '
                                'component keywords'))

    # 7. Freshness vs upstream KB files.
    try:
        target_mtime = target.stat().st_mtime
        for upstream_name in ('discovered_generic_nouns.json',
                              'component_map.json',
                              'flow_registry.json'):
            up_path = Path(kb_dir) / upstream_name
            if not up_path.exists():
                continue
            up_mtime = up_path.stat().st_mtime
            if target_mtime < up_mtime:
                c = Check(f'domain_vocabulary.content.freshness.'
                          f'{upstream_name}', FAIL,
                          f'domain_vocabulary older than {upstream_name}')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_domain_vocab_stale',
                    message=c.detail, target=str(target),
                    fix=FIX_REBUILD_DISCOVERED_VOCAB,
                    fix_command='python3 build_discovered_vocabularies.py '
                                '--only vocabularies',
                    details={'upstream': upstream_name,
                             'target_mtime': target_mtime,
                             'upstream_mtime': up_mtime},
                ))
            else:
                checks.append(Check(
                    f'domain_vocabulary.content.freshness.{upstream_name}',
                    PASS, 'newer than upstream'))
    except OSError as exc:
        checks.append(Check('domain_vocabulary.content.freshness', FAIL,
                            f'mtime check failed: {exc}'))

    return checks, issues, metrics


def validate_embeddings_index_content(kb_dir: Path,
                                      raw_corpus_path: Optional[Path]
                                      ) -> Tuple[List[Check], List[AuditIssue],
                                                 Dict[str, Any]]:
    """Deep content validation for embeddings_index.npz.

    Detects:
      - Cannot load .npz / missing arrays
      - Wrong embedding dimension (!= 384)
      - Shape[0] mismatch with test_keys
      - Coverage drop vs raw corpus (< 95% WARN, < 50% FAIL)
      - All-zero embedding rows (sample 200)
      - Empty / duplicate test_keys
      - dtype != float32
      - Stale relative to raw corpus
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    target = Path(kb_dir) / 'embeddings_index.npz'
    if not target.exists():
        return checks, issues, metrics

    try:
        import numpy as np  # type: ignore
    except Exception as exc:  # pragma: no cover - numpy is a hard dep
        checks.append(Check('embeddings_index.content.numpy', FAIL,
                            f'numpy unavailable: {exc}'))
        return checks, issues, metrics

    try:
        archive = np.load(str(target), allow_pickle=True)
        names = archive.files if hasattr(archive, 'files') else list(archive)
    except Exception as exc:
        c = Check('embeddings_index.content.loadable', FAIL,
                  f'cannot load .npz: {exc}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_embeddings_unloadable',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_EMBEDDINGS,
            fix_command='python3 build_embeddings.py',
        ))
        return checks, issues, metrics

    if 'embeddings' not in names or 'test_keys' not in names:
        c = Check('embeddings_index.content.required_arrays', FAIL,
                  f'missing arrays in npz; found: {list(names)}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_embeddings_missing_arrays',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_EMBEDDINGS,
            fix_command='python3 build_embeddings.py',
        ))
        return checks, issues, metrics

    embeddings = archive['embeddings']
    test_keys = archive['test_keys']
    metrics['embeddings_index.shape'] = list(embeddings.shape)
    metrics['embeddings_index.dtype'] = str(embeddings.dtype)
    metrics['embeddings_index.test_keys_count'] = int(len(test_keys))

    # 1. Embedding dimension.
    if len(embeddings.shape) != 2 or embeddings.shape[1] != 384:
        c = Check('embeddings_index.content.dimension', FAIL,
                  f'expected (N, 384), got shape={list(embeddings.shape)}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_embeddings_wrong_dimension',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_EMBEDDINGS,
            fix_command='python3 build_embeddings.py',
            details={'shape': list(embeddings.shape)},
        ))
    else:
        checks.append(Check('embeddings_index.content.dimension', PASS,
                            f'shape={list(embeddings.shape)}'))

    # 2. shape[0] == len(test_keys).
    if len(embeddings.shape) >= 1 and embeddings.shape[0] != len(test_keys):
        c = Check('embeddings_index.content.row_key_match', FAIL,
                  f'embeddings rows ({embeddings.shape[0]}) != '
                  f'test_keys ({len(test_keys)})')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_embeddings_shape_mismatch',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_EMBEDDINGS,
            fix_command='python3 build_embeddings.py',
            details={'rows': int(embeddings.shape[0]),
                     'keys': int(len(test_keys))},
        ))
    else:
        checks.append(Check('embeddings_index.content.row_key_match', PASS,
                            f'{embeddings.shape[0]} rows match keys'))

    # 3. Coverage vs raw corpus.
    if raw_corpus_path and Path(raw_corpus_path).exists():
        raw = _safe_load_json(Path(raw_corpus_path)) or []
        raw_n = len(raw) if isinstance(raw, list) else 0
        metrics['embeddings_index.raw_corpus_length'] = raw_n
        if raw_n > 0:
            ratio = embeddings.shape[0] / raw_n
            metrics['embeddings_index.coverage_ratio'] = round(ratio, 3)
            if ratio < 0.50:
                c = Check('embeddings_index.content.coverage', FAIL,
                          f'embeddings cover only {ratio*100:.0f}% of raw '
                          'corpus (expected >=95%)')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_ERROR,
                    type='stage0_embeddings_severe_undercoverage',
                    message=c.detail, target=str(target),
                    fix=FIX_REBUILD_EMBEDDINGS,
                    fix_command='python3 build_embeddings.py',
                    details={'coverage_pct': round(ratio * 100, 1)},
                ))
            elif ratio < 0.95:
                c = Check('embeddings_index.content.coverage', FAIL,
                          f'embeddings cover {ratio*100:.0f}% of raw '
                          'corpus (expected >=95%)')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_embeddings_undercoverage',
                    message=c.detail, target=str(target),
                    fix=FIX_REBUILD_EMBEDDINGS,
                    fix_command='python3 build_embeddings.py',
                    details={'coverage_pct': round(ratio * 100, 1)},
                ))
            else:
                checks.append(Check('embeddings_index.content.coverage', PASS,
                                    f'{ratio*100:.1f}% coverage'))

    # 4. All-zero rows in sample 200.
    try:
        sample_n = min(200, embeddings.shape[0])
        if sample_n > 0:
            sample = embeddings[:sample_n]
            zero_rows = int(sum(1 for r in sample
                                if float(np.abs(r).sum()) == 0.0))
            metrics['embeddings_index.zero_rows_sample'] = zero_rows
            if zero_rows > 0:
                c = Check('embeddings_index.content.zero_rows', FAIL,
                          f'{zero_rows}/{sample_n} sampled rows are all-zero')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_ERROR,
                    type='stage0_embeddings_zero_rows',
                    message=c.detail, target=str(target),
                    fix=FIX_REBUILD_EMBEDDINGS,
                    fix_command='python3 build_embeddings.py',
                    details={'zero_rows': zero_rows,
                             'sample': sample_n},
                ))
            else:
                checks.append(Check('embeddings_index.content.zero_rows', PASS,
                                    f'0 zero-rows in sample {sample_n}'))
    except Exception as exc:
        checks.append(Check('embeddings_index.content.zero_rows', FAIL,
                            f'zero-row check crashed: {exc}'))

    # 5. test_keys quality (no empties, no duplicates).
    try:
        keys_list = [str(k) for k in test_keys.tolist()]
        empty_keys = sum(1 for k in keys_list if not k.strip())
        from collections import Counter
        counter = Counter(keys_list)
        dup_keys = [k for k, v in counter.items() if v > 1 and k.strip()]
        metrics['embeddings_index.empty_keys'] = empty_keys
        metrics['embeddings_index.duplicate_keys'] = len(dup_keys)
        if empty_keys or dup_keys:
            c = Check('embeddings_index.content.key_quality', FAIL,
                      f'{empty_keys} empty + {len(dup_keys)} duplicate '
                      'test_keys')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_ERROR,
                type='stage0_embeddings_bad_keys',
                message=c.detail, target=str(target),
                fix=FIX_REBUILD_EMBEDDINGS,
                fix_command='python3 build_embeddings.py',
                details={'empty_keys': empty_keys,
                         'duplicate_keys': len(dup_keys),
                         'duplicate_sample': dup_keys[:5]},
            ))
        else:
            checks.append(Check('embeddings_index.content.key_quality', PASS,
                                'no empty/duplicate keys'))
    except Exception as exc:
        checks.append(Check('embeddings_index.content.key_quality', FAIL,
                            f'key quality check crashed: {exc}'))

    # 6. dtype float32.
    if str(embeddings.dtype) != 'float32':
        c = Check('embeddings_index.content.dtype', FAIL,
                  f'dtype={embeddings.dtype}, expected float32')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_embeddings_wrong_dtype',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_EMBEDDINGS,
            fix_command='python3 build_embeddings.py',
            details={'dtype': str(embeddings.dtype)},
        ))
    else:
        checks.append(Check('embeddings_index.content.dtype', PASS,
                            'dtype=float32'))

    # 7. Freshness vs raw corpus.
    if raw_corpus_path and Path(raw_corpus_path).exists():
        try:
            emb_mtime = target.stat().st_mtime
            raw_mtime = Path(raw_corpus_path).stat().st_mtime
            if emb_mtime < raw_mtime:
                c = Check('embeddings_index.content.freshness', FAIL,
                          'embeddings older than raw corpus')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_embeddings_stale',
                    message=c.detail, target=str(target),
                    fix=FIX_REBUILD_EMBEDDINGS,
                    fix_command='python3 build_embeddings.py',
                    details={'emb_mtime': emb_mtime,
                             'raw_mtime': raw_mtime},
                ))
            else:
                checks.append(Check('embeddings_index.content.freshness', PASS,
                                    'newer than raw corpus'))
        except OSError as exc:
            checks.append(Check('embeddings_index.content.freshness', FAIL,
                                f'mtime check failed: {exc}'))

    return checks, issues, metrics


def validate_codebase_vocabulary_content(kb_dir: Path,
                                         repo_root: Path
                                         ) -> Tuple[List[Check],
                                                    List[AuditIssue],
                                                    Dict[str, Any]]:
    """Deep content validation for codebase_vocabulary.json.

    Detects:
      - Missing keys / malformed structure
      - Vocabulary too small (< 1000 WARN, < 100 FAIL)
      - Flat distribution (max/median < 50)
      - Missing sentinel framework tokens
      - min_frequency_threshold absent / wrong shape
      - Stale relative to newest source mtime
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    target = Path(kb_dir) / 'codebase_vocabulary.json'
    if not target.exists():
        return checks, issues, metrics

    ok, payload, err = _read_json(target)
    if not ok or not isinstance(payload, dict):
        c = Check('codebase_vocabulary.content.valid_json', FAIL,
                  f'cannot parse / not a dict: {err}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_codebase_vocab_invalid',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 extract_diff_concepts.py --build-vocab',
        ))
        return checks, issues, metrics

    vocab = payload.get('vocabulary')
    if not isinstance(vocab, dict):
        c = Check('codebase_vocabulary.content.shape', FAIL,
                  'vocabulary key missing or not a dict')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_codebase_vocab_bad_shape',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 extract_diff_concepts.py --build-vocab',
        ))
        return checks, issues, metrics

    n = len(vocab)
    metrics['codebase_vocabulary.size'] = n

    # 1. Vocab size.
    if n < 100:
        c = Check('codebase_vocabulary.content.size', FAIL,
                  f'vocabulary has only {n} terms (expected >=1000)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_codebase_vocab_collapsed',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 extract_diff_concepts.py --build-vocab',
            details={'size': n},
        ))
    elif n < 1000:
        c = Check('codebase_vocabulary.content.size', FAIL,
                  f'vocabulary size={n} below 1000')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_codebase_vocab_undersized',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 extract_diff_concepts.py --build-vocab',
            details={'size': n},
        ))
    else:
        checks.append(Check('codebase_vocabulary.content.size', PASS,
                            f'{n} terms'))

    # 2. Frequency distribution non-flat.
    try:
        freqs = sorted(int(v) for v in vocab.values()
                       if isinstance(v, (int, float)))
        if freqs:
            mid = freqs[len(freqs) // 2]
            top = freqs[-1]
            ratio = top / max(mid, 1)
            metrics['codebase_vocabulary.freq_max'] = top
            metrics['codebase_vocabulary.freq_median'] = mid
            metrics['codebase_vocabulary.freq_ratio'] = round(ratio, 1)
            if ratio < 50:
                c = Check('codebase_vocabulary.content.distribution', FAIL,
                          f'max/median = {ratio:.1f} (expected >=50); '
                          'distribution is flat')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_codebase_vocab_flat_distribution',
                    message=c.detail, target=str(target),
                    fix=FIX_REBUILD_DISCOVERED_VOCAB,
                    fix_command='python3 extract_diff_concepts.py '
                                '--build-vocab',
                    details={'ratio': round(ratio, 1),
                             'max': top, 'median': mid},
                ))
            else:
                checks.append(Check('codebase_vocabulary.content.distribution',
                                    PASS, f'ratio={ratio:.1f}'))
    except Exception as exc:
        checks.append(Check('codebase_vocabulary.content.distribution', FAIL,
                            f'distribution check crashed: {exc}'))

    # 3. Sentinel framework tokens present with freq>=10.
    sentinels = ('service', 'controller', 'test')
    present = [s for s in sentinels
               if isinstance(vocab.get(s), (int, float))
               and int(vocab.get(s, 0) or 0) >= 10]
    metrics['codebase_vocabulary.sentinel_hits'] = len(present)
    if len(present) < 3:
        c = Check('codebase_vocabulary.content.sentinels', FAIL,
                  f'only {len(present)}/3 framework sentinels present with '
                  'freq>=10')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_codebase_vocab_missing_sentinels',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 extract_diff_concepts.py --build-vocab',
            details={'present': present, 'expected': list(sentinels)},
        ))
    else:
        checks.append(Check('codebase_vocabulary.content.sentinels', PASS,
                            f'{len(present)}/3 sentinels present'))

    # 4. min_frequency_threshold.
    threshold = payload.get('min_frequency_threshold')
    if not isinstance(threshold, int) or threshold < 2:
        c = Check('codebase_vocabulary.content.threshold', FAIL,
                  f'min_frequency_threshold={threshold} (expected int >=2)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_codebase_vocab_bad_threshold',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 extract_diff_concepts.py --build-vocab',
            details={'threshold': threshold},
        ))
    else:
        checks.append(Check('codebase_vocabulary.content.threshold', PASS,
                            f'threshold={threshold}'))

    # 5. Freshness vs newest source mtime (sample top of repo_root).
    try:
        target_mtime = target.stat().st_mtime
        newest_src = 0.0
        for ext in ('java', 'py', 'ts', 'tsx', 'js'):
            for p in Path(repo_root).rglob(f'*.{ext}'):
                # Skip vendor / hidden dirs to keep this O(repo)
                pstr = str(p)
                if any(skip in pstr for skip in (
                        '/.git/', '/node_modules/', '/target/',
                        '/build/', '/dist/', '/.venv/', '/__pycache__/')):
                    continue
                try:
                    mt = p.stat().st_mtime
                    if mt > newest_src:
                        newest_src = mt
                except OSError:
                    continue
                # Cap iterations: don't scan more than 5000 files per ext.
                # (best-effort; extra mtime drift is non-fatal.)
        if newest_src > 0:
            metrics['codebase_vocabulary.newest_src_mtime'] = newest_src
            metrics['codebase_vocabulary.target_mtime'] = target_mtime
            # Only WARN if vocab older than newest source by more than 24h.
            if target_mtime + 86400 < newest_src:
                c = Check('codebase_vocabulary.content.freshness', FAIL,
                          'codebase_vocabulary older than newest source by '
                          '>24h')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_codebase_vocab_stale',
                    message=c.detail, target=str(target),
                    fix=FIX_REBUILD_DISCOVERED_VOCAB,
                    fix_command='python3 extract_diff_concepts.py '
                                '--build-vocab',
                    details={'target_mtime': target_mtime,
                             'newest_src_mtime': newest_src},
                ))
            else:
                checks.append(Check('codebase_vocabulary.content.freshness',
                                    PASS, 'within 24h of newest source'))
    except Exception as exc:
        checks.append(Check('codebase_vocabulary.content.freshness', FAIL,
                            f'freshness check crashed: {exc}'))

    return checks, issues, metrics


def validate_framework_suffixes_content(kb_dir: Path,
                                        repo_root: Path
                                        ) -> Tuple[List[Check],
                                                   List[AuditIssue],
                                                   Dict[str, Any]]:
    """Deep content validation for discovered_framework_suffixes.json.

    Detects:
      - Missing keys / malformed structure
      - summary.classes_scanned < 100
      - len(suffixes) < 5
      - Suffix shape (must be PascalCase, length >=3)
      - summary.total_kept inconsistency
      - Stale relative to newest *.java mtime
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    target = Path(kb_dir) / 'discovered_framework_suffixes.json'
    if not target.exists():
        return checks, issues, metrics

    ok, payload, err = _read_json(target)
    if not ok or not isinstance(payload, dict):
        c = Check('framework_suffixes.content.valid_json', FAIL,
                  f'cannot parse / not a dict: {err}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_framework_suffixes_invalid',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 build_discovered_vocabularies.py '
                        '--only framework_suffixes',
        ))
        return checks, issues, metrics

    suffixes = payload.get('suffixes') or []
    summary = payload.get('summary') or {}
    classes_scanned = int(summary.get('classes_scanned', 0) or 0)
    total_kept = int(summary.get('total_kept', 0) or 0)
    n = len(suffixes) if isinstance(suffixes, list) else 0

    metrics['framework_suffixes.count'] = n
    metrics['framework_suffixes.classes_scanned'] = classes_scanned

    # 1. classes_scanned >= 100 (WARN).
    if classes_scanned < 100:
        c = Check('framework_suffixes.content.classes_scanned', FAIL,
                  f'only {classes_scanned} classes scanned (expected >=100)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_framework_suffixes_low_scan',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 build_discovered_vocabularies.py '
                        '--only framework_suffixes',
            details={'classes_scanned': classes_scanned},
        ))
    else:
        checks.append(Check('framework_suffixes.content.classes_scanned',
                            PASS, f'{classes_scanned} classes scanned'))

    # 2. len(suffixes) >= 5 (WARN).
    if n < 5:
        c = Check('framework_suffixes.content.count', FAIL,
                  f'only {n} suffixes discovered (expected >=5)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_framework_suffixes_low_count',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 build_discovered_vocabularies.py '
                        '--only framework_suffixes',
            details={'count': n},
        ))
    else:
        checks.append(Check('framework_suffixes.content.count', PASS,
                            f'{n} suffixes'))

    # 3. Suffix shape: PascalCase length>=3.
    bad_shape: List[str] = []
    if isinstance(suffixes, list):
        for s in suffixes:
            ss = str(s) if s is not None else ''
            if (not ss
                    or len(ss) < 3
                    or not ss[0].isupper()
                    or not ss.isalpha()):
                bad_shape.append(ss)
    metrics['framework_suffixes.bad_shape_count'] = len(bad_shape)
    if bad_shape:
        c = Check('framework_suffixes.content.shape', FAIL,
                  f'{len(bad_shape)} suffixes have wrong shape '
                  f'(non-PascalCase / <3 chars / non-alpha)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_framework_suffixes_bad_shape',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 build_discovered_vocabularies.py '
                        '--only framework_suffixes',
            details={'sample': bad_shape[:5]},
        ))
    else:
        checks.append(Check('framework_suffixes.content.shape', PASS,
                            'all suffixes well-formed'))

    # 4. summary.total_kept consistency.
    if total_kept != n:
        c = Check('framework_suffixes.content.summary_consistency', FAIL,
                  f'summary.total_kept={total_kept} but '
                  f'len(suffixes)={n}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_framework_suffixes_summary_inconsistent',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 build_discovered_vocabularies.py '
                        '--only framework_suffixes',
            details={'total_kept': total_kept, 'actual_count': n},
        ))
    else:
        checks.append(Check('framework_suffixes.content.summary_consistency',
                            PASS, 'summary.total_kept matches'))

    # 5. Freshness vs newest *.java mtime.
    try:
        target_mtime = target.stat().st_mtime
        newest_java = 0.0
        for p in Path(repo_root).rglob('*.java'):
            pstr = str(p)
            if any(skip in pstr for skip in (
                    '/.git/', '/node_modules/', '/target/',
                    '/build/', '/dist/')):
                continue
            try:
                mt = p.stat().st_mtime
                if mt > newest_java:
                    newest_java = mt
            except OSError:
                continue
        if newest_java > 0:
            metrics['framework_suffixes.newest_java_mtime'] = newest_java
            if target_mtime + 86400 < newest_java:
                c = Check('framework_suffixes.content.freshness', FAIL,
                          'framework_suffixes older than newest .java by >24h')
                checks.append(c)
                issues.append(AuditIssue(
                    stage='stage0', severity=SEVERITY_WARN,
                    type='stage0_framework_suffixes_stale',
                    message=c.detail, target=str(target),
                    fix=FIX_REBUILD_DISCOVERED_VOCAB,
                    fix_command='python3 build_discovered_vocabularies.py '
                                '--only framework_suffixes',
                    details={'target_mtime': target_mtime,
                             'newest_java_mtime': newest_java},
                ))
            else:
                checks.append(Check('framework_suffixes.content.freshness',
                                    PASS, 'within 24h of newest .java'))
    except Exception as exc:
        checks.append(Check('framework_suffixes.content.freshness', FAIL,
                            f'freshness check crashed: {exc}'))

    return checks, issues, metrics


def validate_language_reserved_words_content(kb_dir: Path
                                             ) -> Tuple[List[Check],
                                                        List[AuditIssue],
                                                        Dict[str, Any]]:
    """Deep content validation for language_reserved_words.json.

    Detects:
      - Missing keys / malformed structure
      - language field absent or mismatch with detected language
      - len(reserved_words) < 20
      - Non-lowercase entries
      - Per-language sentinel words missing (Java: public/class,
        Python: def/class, TS: function/interface)
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    target = Path(kb_dir) / 'language_reserved_words.json'
    if not target.exists():
        return checks, issues, metrics

    ok, payload, err = _read_json(target)
    if not ok or not isinstance(payload, dict):
        c = Check('reserved_words.content.valid_json', FAIL,
                  f'cannot parse / not a dict: {err}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_reserved_words_invalid',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 extract_diff_concepts.py --build-vocab',
        ))
        return checks, issues, metrics

    language = str(payload.get('language') or '').lower()
    words = payload.get('reserved_words') or []
    metrics['reserved_words.language'] = language
    metrics['reserved_words.count'] = (len(words) if isinstance(words, list)
                                       else 0)

    # 1. language matches RIA_LANGUAGE env (WARN on mismatch).
    detected = (os.environ.get('RIA_LANGUAGE') or '').lower()
    if detected and language and detected != language:
        c = Check('reserved_words.content.language_match', FAIL,
                  f'language={language} does not match detected '
                  f'RIA_LANGUAGE={detected}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_WARN,
            type='stage0_reserved_words_language_mismatch',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 extract_diff_concepts.py --build-vocab',
            details={'language': language, 'detected': detected},
        ))
    else:
        checks.append(Check('reserved_words.content.language_match', PASS,
                            f'language={language or "(unset)"}'))

    # 2. Count >= 20.
    n = len(words) if isinstance(words, list) else 0
    if n < 20:
        c = Check('reserved_words.content.count', FAIL,
                  f'only {n} reserved_words (expected >=20)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage0', severity=SEVERITY_ERROR,
            type='stage0_reserved_words_collapsed',
            message=c.detail, target=str(target),
            fix=FIX_REBUILD_DISCOVERED_VOCAB,
            fix_command='python3 extract_diff_concepts.py --build-vocab',
            details={'count': n},
        ))
    else:
        checks.append(Check('reserved_words.content.count', PASS,
                            f'{n} reserved_words'))

    # 3. All entries lowercase.
    if isinstance(words, list):
        not_lower = [str(w) for w in words if str(w) != str(w).lower()]
        metrics['reserved_words.non_lowercase'] = len(not_lower)
        if not_lower:
            c = Check('reserved_words.content.lowercase', FAIL,
                      f'{len(not_lower)} entries are not lowercase')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_ERROR,
                type='stage0_reserved_words_non_lowercase',
                message=c.detail, target=str(target),
                fix=FIX_REBUILD_DISCOVERED_VOCAB,
                fix_command='python3 extract_diff_concepts.py --build-vocab',
                details={'sample': not_lower[:5]},
            ))
        else:
            checks.append(Check('reserved_words.content.lowercase', PASS,
                                'all entries lowercase'))

    # 4. Per-language sentinels.
    sentinels_by_language = {
        'java': ('public', 'class'),
        'python': ('def', 'class'),
        'typescript': ('function', 'interface'),
        'ts': ('function', 'interface'),
        'javascript': ('function', 'class'),
        'js': ('function', 'class'),
    }
    expected = sentinels_by_language.get(language)
    if expected and isinstance(words, list):
        words_set = set(str(w).lower() for w in words)
        missing_sent = [s for s in expected if s not in words_set]
        if missing_sent:
            c = Check('reserved_words.content.sentinels', FAIL,
                      f'language={language} missing sentinel words: '
                      f'{missing_sent}')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage0', severity=SEVERITY_ERROR,
                type='stage0_reserved_words_missing_sentinels',
                message=c.detail, target=str(target),
                fix=FIX_REBUILD_DISCOVERED_VOCAB,
                fix_command='python3 extract_diff_concepts.py --build-vocab',
                details={'language': language,
                         'expected_sentinels': list(expected),
                         'missing': missing_sent},
            ))
        else:
            checks.append(Check('reserved_words.content.sentinels', PASS,
                                f'all {language} sentinels present'))

    return checks, issues, metrics


def validate_stage1_content(audit_output: Path,
                            changed_method: Optional[str],
                            changed_file: Optional[str],
                            repo_root: Path
                            ) -> Tuple[List[Check], List[AuditIssue],
                                       Dict[str, Any]]:
    """Validate Stage 1 (call tree analysis) output quality.

    Detects:
      - File missing / not parseable
      - entry_points list empty (ERROR: pipeline cannot proceed)
      - entry_points list excessive (>50, WARN: overly broad call tree)
      - Required keys missing on entry_points
      - Entry-point file resolution < 80% (sample 10)
      - changed_method not referenced in entry_points (sanity)
      - total_entry_points mismatched with len(entry_points)
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    target = Path(audit_output) / 'stage1_entry_points.json'

    if not target.exists():
        c = Check('stage1.content.exists', FAIL,
                  f'stage1_entry_points.json missing: {target}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage1', severity=SEVERITY_ERROR,
            type='stage1_output_missing',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE1,
            fix_command='Re-run Stage 1 (call tree analysis)',
        ))
        return checks, issues, metrics

    ok, payload, err = _read_json(target)
    if not ok or not isinstance(payload, dict):
        c = Check('stage1.content.valid_json', FAIL,
                  f'cannot parse / not a dict: {err}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage1', severity=SEVERITY_ERROR,
            type='stage1_invalid_json',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE1,
            fix_command='Re-run Stage 1 (call tree analysis)',
        ))
        return checks, issues, metrics

    # 1. Required keys.
    required_keys = ('entry_points',)
    missing_keys = [k for k in required_keys if k not in payload]
    if missing_keys:
        c = Check('stage1.content.required_keys', FAIL,
                  f'missing required keys: {missing_keys}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage1', severity=SEVERITY_ERROR,
            type='stage1_missing_keys',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE1,
            fix_command='Re-run Stage 1 (call tree analysis)',
            details={'missing_keys': missing_keys},
        ))
    else:
        checks.append(Check('stage1.content.required_keys', PASS,
                            'all required keys present'))

    entry_points = payload.get('entry_points') or []
    if not isinstance(entry_points, list):
        entry_points = []
    metrics['stage1.entry_point_count'] = len(entry_points)

    # 2. entry_points non-empty.
    if not entry_points:
        c = Check('stage1.content.non_empty', FAIL,
                  'entry_points list is empty (pipeline cannot proceed)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage1', severity=SEVERITY_ERROR,
            type='stage1_no_entry_points',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE1,
            fix_command='Re-run Stage 1 (call tree analysis)',
        ))
        return checks, issues, metrics

    checks.append(Check('stage1.content.non_empty', PASS,
                        f'{len(entry_points)} entry points found'))

    # 3. Excessive count band.
    if len(entry_points) > 50:
        c = Check('stage1.content.count_band', FAIL,
                  f'{len(entry_points)} entry points (expected 1-50; '
                  'overly broad call tree)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage1', severity=SEVERITY_WARN,
            type='stage1_excessive_entry_points',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE1,
            fix_command='Re-run Stage 1 (call tree analysis)',
            details={'count': len(entry_points)},
        ))
    else:
        checks.append(Check('stage1.content.count_band', PASS,
                            f'{len(entry_points)} entry points within band'))

    # 4. Required fields on each entry_point (sample first 50).
    bad_fields = 0
    for ep in entry_points[:50]:
        if not isinstance(ep, dict):
            bad_fields += 1
            continue
        if not (ep.get('file') and ep.get('method')):
            bad_fields += 1
    if bad_fields:
        c = Check('stage1.content.required_fields', FAIL,
                  f'{bad_fields}/{min(50, len(entry_points))} entry points '
                  'missing file/method')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage1', severity=SEVERITY_ERROR,
            type='stage1_entry_point_missing_fields',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE1,
            fix_command='Re-run Stage 1 (call tree analysis)',
            details={'bad_count': bad_fields},
        ))
    else:
        checks.append(Check('stage1.content.required_fields', PASS,
                            'all sampled entry points have file/method'))

    # 5. File resolution (sample 10).
    sampled = 0
    resolved = 0
    for ep in entry_points[:10]:
        if not isinstance(ep, dict):
            continue
        f = ep.get('file')
        if not f:
            continue
        sampled += 1
        if _resolve_repo_path(Path(repo_root), str(f)):
            resolved += 1
    metrics['stage1.file_resolution_sampled'] = sampled
    metrics['stage1.file_resolution_hits'] = resolved
    if sampled > 0:
        ratio = resolved / sampled
        if ratio < 0.80:
            c = Check('stage1.content.file_resolution', FAIL,
                      f'only {ratio*100:.0f}% of sampled entry-point files '
                      'resolve (expected >=80%)')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage1', severity=SEVERITY_WARN,
                type='stage1_stale_paths',
                message=c.detail, target=str(target),
                fix=FIX_RERUN_STAGE1,
                fix_command='Re-run Stage 1 (call tree analysis)',
                details={'resolution_pct': round(ratio * 100, 1)},
            ))
        else:
            checks.append(Check('stage1.content.file_resolution', PASS,
                                f'{ratio*100:.1f}% of sampled paths resolve'))

    # 6. changed_method should appear somewhere in entry_points chain.
    if changed_method:
        cm_lower = str(changed_method).lower()
        found = False
        # Check direct method match or substring in any entry point's
        # method/chain field.
        for ep in entry_points:
            if not isinstance(ep, dict):
                continue
            if cm_lower == str(ep.get('method', '') or '').lower():
                found = True
                break
            chain = ep.get('chain') or ep.get('call_chain') or []
            if isinstance(chain, list):
                for hop in chain:
                    if cm_lower in str(hop).lower():
                        found = True
                        break
            if found:
                break
        # Also accept if changed_method substring is in the JSON payload.
        if not found:
            try:
                blob = json.dumps(payload).lower()
                if cm_lower in blob:
                    found = True
            except Exception:
                pass
        if not found:
            c = Check('stage1.content.changed_method_reachable', FAIL,
                      f'changed_method "{changed_method}" not found '
                      'anywhere in entry_points chain')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage1', severity=SEVERITY_WARN,
                type='stage1_changed_method_not_reachable',
                message=c.detail, target=str(target),
                fix=FIX_RERUN_STAGE1,
                fix_command='Re-run Stage 1 (call tree analysis)',
                details={'changed_method': changed_method},
            ))
        else:
            checks.append(Check('stage1.content.changed_method_reachable',
                                PASS,
                                f'changed_method "{changed_method}" '
                                'reachable'))

    # 7. total_entry_points consistency.
    total = payload.get('total_entry_points')
    if total is not None and total != len(entry_points):
        c = Check('stage1.content.total_consistency', FAIL,
                  f'total_entry_points={total} but len(entry_points)='
                  f'{len(entry_points)}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage1', severity=SEVERITY_WARN,
            type='stage1_total_mismatch',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE1,
            fix_command='Re-run Stage 1 (call tree analysis)',
            details={'total': total, 'actual': len(entry_points)},
        ))
    else:
        checks.append(Check('stage1.content.total_consistency', PASS,
                            'total_entry_points consistent'))

    return checks, issues, metrics


def validate_stage2_content(audit_output: Path,
                            kb_dir: Path,
                            change_type: Optional[str]
                            ) -> Tuple[List[Check], List[AuditIssue],
                                       Dict[str, Any]]:
    """Validate Stage 2 (flow mapping) output quality.

    Detects:
      - File missing / not parseable
      - impacted_flows list empty (ERROR)
      - impacted_flows list excessive (>20, WARN)
      - Generic flow names (Component:* placeholders)
      - Empty entry_points on a flow
      - flow_id values not present in flow_registry.json
      - total_flows mismatched with len(impacted_flows)
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    target = Path(audit_output) / 'stage2_impacted_flows.json'

    if not target.exists():
        c = Check('stage2.content.exists', FAIL,
                  f'stage2_impacted_flows.json missing: {target}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage2', severity=SEVERITY_ERROR,
            type='stage2_output_missing',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE2,
            fix_command='Re-run Stage 2 (flow mapping)',
        ))
        return checks, issues, metrics

    ok, payload, err = _read_json(target)
    if not ok or not isinstance(payload, dict):
        c = Check('stage2.content.valid_json', FAIL,
                  f'cannot parse / not a dict: {err}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage2', severity=SEVERITY_ERROR,
            type='stage2_invalid_json',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE2,
            fix_command='Re-run Stage 2 (flow mapping)',
        ))
        return checks, issues, metrics

    # 1. Required keys.
    required_keys = ('impacted_flows',)
    missing_keys = [k for k in required_keys if k not in payload]
    if missing_keys:
        c = Check('stage2.content.required_keys', FAIL,
                  f'missing required keys: {missing_keys}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage2', severity=SEVERITY_ERROR,
            type='stage2_missing_keys',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE2,
            fix_command='Re-run Stage 2 (flow mapping)',
            details={'missing_keys': missing_keys},
        ))
    else:
        checks.append(Check('stage2.content.required_keys', PASS,
                            'all required keys present'))

    impacted = payload.get('impacted_flows') or []
    if not isinstance(impacted, list):
        impacted = []
    metrics['stage2.impacted_flow_count'] = len(impacted)

    # 2. Non-empty.
    if not impacted:
        c = Check('stage2.content.non_empty', FAIL,
                  'impacted_flows list is empty (no flows impacted; '
                  'pipeline cannot recommend tests)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage2', severity=SEVERITY_ERROR,
            type='stage2_no_impacted_flows',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE2,
            fix_command='Re-run Stage 2 (flow mapping)',
        ))
        return checks, issues, metrics

    checks.append(Check('stage2.content.non_empty', PASS,
                        f'{len(impacted)} impacted flows'))

    # 3. Count band.
    if len(impacted) > 20:
        c = Check('stage2.content.count_band', FAIL,
                  f'{len(impacted)} impacted flows (expected 1-20; '
                  'overly broad impact)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage2', severity=SEVERITY_WARN,
            type='stage2_excessive_flows',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE2,
            fix_command='Re-run Stage 2 (flow mapping)',
            details={'count': len(impacted)},
        ))
    else:
        checks.append(Check('stage2.content.count_band', PASS,
                            f'{len(impacted)} flows within band'))

    # 4. Required fields on each flow + entry_points non-empty per flow.
    bad_fields = 0
    bad_entry_points = 0
    generic_names = 0
    for f in impacted:
        if not isinstance(f, dict):
            bad_fields += 1
            continue
        if not (f.get('flow_id') and f.get('flow_name')):
            bad_fields += 1
            continue
        eps = f.get('entry_points')
        if not isinstance(eps, list) or not eps:
            bad_entry_points += 1
        name = str(f.get('flow_name') or '')
        if name.startswith('Component:'):
            generic_names += 1

    if bad_fields:
        c = Check('stage2.content.required_fields', FAIL,
                  f'{bad_fields}/{len(impacted)} flows missing '
                  'flow_id/flow_name')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage2', severity=SEVERITY_ERROR,
            type='stage2_flow_missing_fields',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE2,
            fix_command='Re-run Stage 2 (flow mapping)',
            details={'bad_count': bad_fields},
        ))
    else:
        checks.append(Check('stage2.content.required_fields', PASS,
                            'all flows have flow_id/flow_name'))

    if bad_entry_points:
        c = Check('stage2.content.flow_entry_points', FAIL,
                  f'{bad_entry_points}/{len(impacted)} flows have empty '
                  'entry_points (flows must have entry points)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage2', severity=SEVERITY_ERROR,
            type='stage2_flow_empty_entry_points',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE2,
            fix_command='Re-run Stage 2 (flow mapping)',
            details={'bad_count': bad_entry_points},
        ))
    else:
        checks.append(Check('stage2.content.flow_entry_points', PASS,
                            'all flows have entry_points'))

    metrics['stage2.generic_flow_names'] = generic_names
    if generic_names:
        c = Check('stage2.content.flow_name_quality', FAIL,
                  f'{generic_names}/{len(impacted)} flows use generic '
                  '"Component:*" names (expected real business flow names)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage2', severity=SEVERITY_WARN,
            type='stage2_generic_flow_names',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE2,
            fix_command='Re-run Stage 2 (flow mapping)',
            details={'generic_count': generic_names},
        ))
    else:
        checks.append(Check('stage2.content.flow_name_quality', PASS,
                            'all flow names are non-generic'))

    # 5. Cross-reference flow_id against flow_registry.json.
    fr_payload = _safe_load_json(Path(kb_dir) / 'flow_registry.json') or {}
    registry_flows = fr_payload.get('flows') or []
    registry_ids = set()
    if isinstance(registry_flows, list):
        for fr in registry_flows:
            if isinstance(fr, dict):
                fid = fr.get('flow_id')
                if fid:
                    registry_ids.add(str(fid))
    if registry_ids:
        unknown = [str(f.get('flow_id')) for f in impacted
                   if isinstance(f, dict)
                   and f.get('flow_id')
                   and str(f.get('flow_id')) not in registry_ids]
        metrics['stage2.unknown_flow_ids'] = len(unknown)
        if unknown:
            c = Check('stage2.content.flow_id_xref', FAIL,
                      f'{len(unknown)}/{len(impacted)} flow_id values not '
                      f'in flow_registry.json (sample: {unknown[:3]})')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage2', severity=SEVERITY_WARN,
                type='stage2_unknown_flow_ids',
                message=c.detail, target=str(target),
                fix=FIX_RERUN_STAGE2,
                fix_command='Re-run Stage 2 (flow mapping)',
                details={'unknown_sample': unknown[:5]},
            ))
        else:
            checks.append(Check('stage2.content.flow_id_xref', PASS,
                                'all flow_id values cross-reference OK'))

    # 6. total_flows consistency.
    total = payload.get('total_flows')
    if total is not None and total != len(impacted):
        c = Check('stage2.content.total_consistency', FAIL,
                  f'total_flows={total} but len(impacted_flows)='
                  f'{len(impacted)}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage2', severity=SEVERITY_WARN,
            type='stage2_total_mismatch',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE2,
            fix_command='Re-run Stage 2 (flow mapping)',
            details={'total': total, 'actual': len(impacted)},
        ))
    else:
        checks.append(Check('stage2.content.total_consistency', PASS,
                            'total_flows consistent'))

    return checks, issues, metrics


def validate_stage3_content(audit_output: Path,
                            kb_dir: Path,
                            change_type: Optional[str]
                            ) -> Tuple[List[Check], List[AuditIssue],
                                       Dict[str, Any]]:
    """Validate Stage 3 (indirect flows) output quality.

    Detects:
      - File missing / not parseable
      - indirect_flows wrong type (list expected; empty allowed)
      - Excessive indirect flows (>15, WARN)
      - flow_id not in flow_registry.json
      - Overlap between Stage 2 DIRECT and Stage 3 INDIRECT classifications
      - total_indirect_flows mismatched with len(indirect_flows)
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    target = Path(audit_output) / 'stage3_indirect_flows.json'

    if not target.exists():
        c = Check('stage3.content.exists', FAIL,
                  f'stage3_indirect_flows.json missing: {target}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage3', severity=SEVERITY_ERROR,
            type='stage3_output_missing',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE3,
            fix_command='Re-run Stage 3 (indirect flows)',
        ))
        return checks, issues, metrics

    ok, payload, err = _read_json(target)
    if not ok or not isinstance(payload, dict):
        c = Check('stage3.content.valid_json', FAIL,
                  f'cannot parse / not a dict: {err}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage3', severity=SEVERITY_ERROR,
            type='stage3_invalid_json',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE3,
            fix_command='Re-run Stage 3 (indirect flows)',
        ))
        return checks, issues, metrics

    # 1. Required keys.
    required_keys = ('indirect_flows',)
    missing_keys = [k for k in required_keys if k not in payload]
    if missing_keys:
        c = Check('stage3.content.required_keys', FAIL,
                  f'missing required keys: {missing_keys}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage3', severity=SEVERITY_ERROR,
            type='stage3_missing_keys',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE3,
            fix_command='Re-run Stage 3 (indirect flows)',
            details={'missing_keys': missing_keys},
        ))
    else:
        checks.append(Check('stage3.content.required_keys', PASS,
                            'all required keys present'))

    indirect = payload.get('indirect_flows')
    if not isinstance(indirect, list):
        c = Check('stage3.content.indirect_flows_type', FAIL,
                  'indirect_flows is not a list')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage3', severity=SEVERITY_ERROR,
            type='stage3_wrong_type',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE3,
            fix_command='Re-run Stage 3 (indirect flows)',
        ))
        return checks, issues, metrics

    metrics['stage3.indirect_flow_count'] = len(indirect)
    checks.append(Check('stage3.content.indirect_flows_type', PASS,
                        f'{len(indirect)} indirect flows '
                        '(empty list is acceptable)'))

    # 2. Excessive indirect flows.
    if len(indirect) > 15:
        c = Check('stage3.content.count_band', FAIL,
                  f'{len(indirect)} indirect flows (expected 0-15; keyword '
                  'matching may be too loose)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage3', severity=SEVERITY_WARN,
            type='stage3_excessive_indirect_flows',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE3,
            fix_command='Re-run Stage 3 (indirect flows)',
            details={'count': len(indirect)},
        ))
    else:
        checks.append(Check('stage3.content.count_band', PASS,
                            f'{len(indirect)} flows within band'))

    # 3. Required fields per flow.
    bad_fields = 0
    for f in indirect:
        if not isinstance(f, dict):
            bad_fields += 1
            continue
        if not (f.get('flow_id') and f.get('flow_name')):
            bad_fields += 1
    if bad_fields:
        c = Check('stage3.content.required_fields', FAIL,
                  f'{bad_fields}/{len(indirect)} indirect flows missing '
                  'flow_id/flow_name')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage3', severity=SEVERITY_ERROR,
            type='stage3_flow_missing_fields',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE3,
            fix_command='Re-run Stage 3 (indirect flows)',
            details={'bad_count': bad_fields},
        ))
    elif indirect:
        checks.append(Check('stage3.content.required_fields', PASS,
                            'all indirect flows have flow_id/flow_name'))

    # 4. Cross-reference flow_id against flow_registry.json.
    fr_payload = _safe_load_json(Path(kb_dir) / 'flow_registry.json') or {}
    registry_flows = fr_payload.get('flows') or []
    registry_ids = set()
    if isinstance(registry_flows, list):
        for fr in registry_flows:
            if isinstance(fr, dict):
                fid = fr.get('flow_id')
                if fid:
                    registry_ids.add(str(fid))
    if registry_ids and indirect:
        unknown = [str(f.get('flow_id')) for f in indirect
                   if isinstance(f, dict)
                   and f.get('flow_id')
                   and str(f.get('flow_id')) not in registry_ids]
        metrics['stage3.unknown_flow_ids'] = len(unknown)
        if unknown:
            c = Check('stage3.content.flow_id_xref', FAIL,
                      f'{len(unknown)}/{len(indirect)} flow_id values not '
                      f'in flow_registry.json (sample: {unknown[:3]})')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage3', severity=SEVERITY_WARN,
                type='stage3_unknown_flow_ids',
                message=c.detail, target=str(target),
                fix=FIX_RERUN_STAGE3,
                fix_command='Re-run Stage 3 (indirect flows)',
                details={'unknown_sample': unknown[:5]},
            ))
        else:
            checks.append(Check('stage3.content.flow_id_xref', PASS,
                                'all flow_id values cross-reference OK'))

    # 5. No overlap between Stage 2 DIRECT and Stage 3 INDIRECT.
    s2_path = Path(audit_output) / 'stage2_impacted_flows.json'
    s2_payload = _safe_load_json(s2_path) or {}
    s2_flows = s2_payload.get('impacted_flows') or []
    direct_ids = set()
    if isinstance(s2_flows, list):
        for f in s2_flows:
            if isinstance(f, dict) and f.get('flow_id'):
                direct_ids.add(str(f.get('flow_id')))
    if direct_ids and indirect:
        indirect_ids = set()
        for f in indirect:
            if isinstance(f, dict) and f.get('flow_id'):
                indirect_ids.add(str(f.get('flow_id')))
        overlap = direct_ids & indirect_ids
        metrics['stage3.direct_indirect_overlap'] = len(overlap)
        if overlap:
            c = Check('stage3.content.no_direct_overlap', FAIL,
                      f'{len(overlap)} flow(s) classified as both DIRECT '
                      f'(Stage 2) and INDIRECT (Stage 3): '
                      f'{sorted(overlap)[:3]}')
            checks.append(c)
            issues.append(AuditIssue(
                stage='stage3', severity=SEVERITY_ERROR,
                type='stage3_direct_indirect_overlap',
                message=c.detail, target=str(target),
                fix=FIX_RERUN_STAGE3,
                fix_command='Re-run Stage 3 (indirect flows)',
                details={'overlap_sample': sorted(overlap)[:5]},
            ))
        else:
            checks.append(Check('stage3.content.no_direct_overlap', PASS,
                                'no overlap with Stage 2 DIRECT flows'))

    # 6. total_indirect_flows consistency.
    total = payload.get('total_indirect_flows')
    if total is not None and total != len(indirect):
        c = Check('stage3.content.total_consistency', FAIL,
                  f'total_indirect_flows={total} but len(indirect_flows)='
                  f'{len(indirect)}')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage3', severity=SEVERITY_WARN,
            type='stage3_total_mismatch',
            message=c.detail, target=str(target),
            fix=FIX_RERUN_STAGE3,
            fix_command='Re-run Stage 3 (indirect flows)',
            details={'total': total, 'actual': len(indirect)},
        ))
    else:
        checks.append(Check('stage3.content.total_consistency', PASS,
                            'total_indirect_flows consistent'))

    return checks, issues, metrics


def validate_stage4_content(audit_output: Path
                            ) -> Tuple[List[Check], List[AuditIssue],
                                       Dict[str, Any]]:
    """Deep content validation for stage4_recommended_tests.json.

    Detects:
      - Score-distribution collapse (all tests share one score)
      - Criticality-vs-flow_dependencies mismatch
      - Changed-component coverage gap (changed comp not in any test)
      - Empty matched_flows on >5% of recommended tests
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    s4_path = Path(audit_output) / 'stage4_recommended_tests.json'
    payload = _safe_load_json(s4_path)
    if not payload:
        return checks, issues, metrics

    tests = (payload.get('recommended_tests')
             or payload.get('tests') or [])
    if not tests:
        # Already flagged by structural validator as "zero tests"; nothing to add.
        return checks, issues, metrics

    # 1. Score distribution.
    scores = [t.get('score', t.get('total_score', 0))
              for t in tests if isinstance(t, dict)]
    unique_scores = {float(s) for s in scores if isinstance(s, (int, float))}
    metrics['stage4.unique_scores'] = len(unique_scores)
    if scores and len(unique_scores) <= 1:
        c = Check('stage4.content.score_distribution', FAIL,
                  f'all {len(tests)} tests share a single score - scoring '
                  f'pass produced no signal')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage4', severity=SEVERITY_ERROR,
            type='stage4_score_collapse',
            message=c.detail, target=str(s4_path),
            fix=FIX_REBUILD_FOCUSED_KB,
            fix_command='re-run focused KB rebuild + Stage 4',
            details={'unique_scores': sorted(unique_scores)[:5]},
        ))
    else:
        checks.append(Check('stage4.content.score_distribution', PASS,
                            f'{len(unique_scores)} distinct scores'))

    # 2. matched_flows coverage on selected tests.
    no_flows = [t.get('issue_key', '?') for t in tests
                if isinstance(t, dict) and not (t.get('matched_flows')
                                                or t.get('flow_tags')
                                                or t.get('flows'))]
    metrics['stage4.tests_without_flows'] = len(no_flows)
    if tests and len(no_flows) / len(tests) > 0.05:
        c = Check('stage4.content.flow_match_completeness', FAIL,
                  f'{len(no_flows)}/{len(tests)} recommended tests have '
                  f'NO matched_flows (>5% threshold)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage4', severity=SEVERITY_WARN,
            type='stage4_unmatched_recommended',
            message=c.detail, target=str(s4_path),
            fix=FIX_REBUILD_FLOW_DEPENDENCIES,
            fix_command='python3 build_flow_dependencies.py',
            details={'no_flow_sample': no_flows[:5]},
        ))
    else:
        checks.append(Check('stage4.content.flow_match_completeness', PASS,
                            f'{len(no_flows)}/{len(tests)} unmatched'))

    # 3. Changed-component coverage: every changed component should have
    #    at least one test referencing it. Coverage may be expressed via
    #    direct fields (component/owning_component/matched_components) or
    #    indirectly via triggered_by_methods (multi-method consolidated
    #    output) which is resolved through component_map.json.
    changed_components: List[str] = []
    cc1 = payload.get('changed_components')
    if isinstance(cc1, list):
        changed_components = [str(x) for x in cc1 if x]
    cc2 = payload.get('changed_component')
    if cc2 and not changed_components:
        changed_components = [str(cc2)]
    kb = _kb_dir(Path(audit_output))
    method_to_components = _build_method_to_component_map(kb)
    flow_to_components = _build_flow_to_component_map(kb)
    referenced_comps = _collect_covered_components(
        tests, method_to_components, flow_to_components,
    )
    missing_changed = [c for c in changed_components
                       if c and c not in referenced_comps]
    metrics['stage4.uncovered_changed_components'] = missing_changed
    if changed_components and missing_changed:
        c = Check('stage4.content.changed_component_coverage', FAIL,
                  f'{len(missing_changed)}/{len(changed_components)} '
                  f'changed components have NO test reference '
                  f'({missing_changed[:3]})')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage4', severity=SEVERITY_ERROR,
            type='stage4_changed_component_uncovered',
            message=c.detail, target=str(s4_path),
            fix=FIX_REBUILD_FOCUSED_KB,
            fix_command='re-run focused KB rebuild + Stage 4',
            details={'uncovered': missing_changed},
        ))
    elif changed_components:
        checks.append(Check('stage4.content.changed_component_coverage',
                            PASS,
                            f'all {len(changed_components)} changed '
                            'components covered'))

    # 4. Criticality assignment consistency.
    valid_crit = {'CRITICAL', 'HIGH', 'MEDIUM', 'LOW'}
    no_crit = sum(1 for t in tests
                  if isinstance(t, dict) and t.get('criticality') not in valid_crit)
    metrics['stage4.tests_without_criticality'] = no_crit
    if no_crit:
        c = Check('stage4.content.criticality_assignment', FAIL,
                  f'{no_crit} tests have missing/invalid criticality')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage4', severity=SEVERITY_WARN,
            type='stage4_criticality_missing',
            message=c.detail, target=str(s4_path),
            fix=FIX_REBUILD_FLOW_DEPENDENCIES,
            fix_command='python3 build_flow_dependencies.py',
            details={'count': no_crit},
        ))
    else:
        checks.append(Check('stage4.content.criticality_assignment', PASS,
                            'all tests have valid criticality'))

    return checks, issues, metrics


def validate_stage5_content(audit_output: Path
                            ) -> Tuple[List[Check], List[AuditIssue],
                                       Dict[str, Any]]:
    """Deep content validation for stage5_refined_tests.json.

    Detects:
      - Retention outside [10%, 95%] band -> threshold problem
      - Synonym-match evidence missing on refined tests (synonym_groups
        not actually being applied)
      - Score regression (Stage 5 scores all <= Stage 4 scores)
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    s4_path = Path(audit_output) / 'stage4_recommended_tests.json'
    s5_path = Path(audit_output) / 'stage5_refined_tests.json'
    p4 = _safe_load_json(s4_path)
    p5 = _safe_load_json(s5_path)
    if not p5:
        return checks, issues, metrics

    s4_tests = (p4 or {}).get('recommended_tests') or (p4 or {}).get('tests') or []
    s5_tests = p5.get('refined_tests') or p5.get('tests') or []
    metrics['stage5.input_count'] = len(s4_tests)
    metrics['stage5.output_count'] = len(s5_tests)
    if not s4_tests or not s5_tests:
        return checks, issues, metrics

    ratio = 100.0 * len(s5_tests) / max(len(s4_tests), 1)
    metrics['stage5.retention_pct'] = round(ratio, 1)
    if ratio < 10.0:
        c = Check('stage5.content.retention_band', FAIL,
                  f'retention {ratio:.1f}% < 10% (over-filtering)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage5', severity=SEVERITY_WARN,
            type='stage5_over_filtered_band',
            message=c.detail, target=str(s5_path),
            fix=FIX_REBUILD_SYNONYM_GROUPS,
            fix_command='python3 build_synonym_groups.py',
            details={'retention_pct': round(ratio, 1)},
        ))
    elif ratio > 95.0:
        c = Check('stage5.content.retention_band', FAIL,
                  f'retention {ratio:.1f}% > 95% (refinement is a no-op)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage5', severity=SEVERITY_WARN,
            type='stage5_no_op',
            message=c.detail, target=str(s5_path),
            fix=FIX_RERUN_STAGE5,
            fix_command='re-run Stage 5 with stricter threshold',
            details={'retention_pct': round(ratio, 1)},
        ))
    else:
        checks.append(Check('stage5.content.retention_band', PASS,
                            f'retention {ratio:.1f}% in band'))

    # Score signal: if every refined test has the same score as its Stage4
    # counterpart, refinement is not enriching the signal.
    s4_scores: Dict[str, float] = {}
    for t in s4_tests:
        if isinstance(t, dict):
            ik = t.get('issue_key')
            if ik:
                s4_scores[str(ik)] = float(t.get('score',
                                                 t.get('total_score', 0)) or 0)
    same_score = 0
    sampled = 0
    for t in s5_tests[:200]:
        if not isinstance(t, dict):
            continue
        ik = str(t.get('issue_key') or '')
        if ik not in s4_scores:
            continue
        sampled += 1
        new_score = float(t.get('score', t.get('total_score', 0)) or 0)
        if new_score == s4_scores[ik]:
            same_score += 1
    metrics['stage5.unchanged_scores_sampled'] = same_score
    if sampled and same_score == sampled:
        c = Check('stage5.content.score_enrichment', FAIL,
                  f'all {sampled} sampled tests retained their Stage 4 '
                  f'score - refinement is not enriching scores')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage5', severity=SEVERITY_WARN,
            type='stage5_no_score_enrichment',
            message=c.detail, target=str(s5_path),
            fix=FIX_REBUILD_SYNONYM_GROUPS,
            fix_command='python3 build_synonym_groups.py',
            details={'sampled': sampled},
        ))
    else:
        checks.append(Check('stage5.content.score_enrichment', PASS,
                            f'{sampled - same_score}/{sampled} scores '
                            'changed by refinement'))

    return checks, issues, metrics


def validate_stage6_content(audit_output: Path
                            ) -> Tuple[List[Check], List[AuditIssue],
                                       Dict[str, Any]]:
    """Deep content validation for stage6_aggressive_tests.json.

    Detects:
      - Final count outside [20, 100] band -> target miss
      - Duplicate issue_keys (dedup failure)
      - Criticality distribution skew (zero CRITICAL when CRITICAL exists upstream)
      - Tests covering 0 of the changed components
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    s6_path = Path(audit_output) / 'stage6_aggressive_tests.json'
    s4_path = Path(audit_output) / 'stage4_recommended_tests.json'
    p6 = _safe_load_json(s6_path)
    p4 = _safe_load_json(s4_path)
    if not p6:
        return checks, issues, metrics

    s6_tests = (p6.get('aggressive_tests')
                or p6.get('tests')
                or p6.get('output_tests') or [])
    metrics['stage6.final_count'] = len(s6_tests)
    if not s6_tests:
        return checks, issues, metrics

    # 1. Target band.
    n = len(s6_tests)
    if n < 20:
        c = Check('stage6.content.target_count', FAIL,
                  f'final count {n} < 20 (under-target)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage6', severity=SEVERITY_WARN,
            type='stage6_under_target',
            message=c.detail, target=str(s6_path),
            fix=FIX_RERUN_STAGE6,
            fix_command='re-run Stage 6 with looser thresholds',
            details={'final_count': n},
        ))
    elif n > 100:
        c = Check('stage6.content.target_count', FAIL,
                  f'final count {n} > 100 (over-target)')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage6', severity=SEVERITY_WARN,
            type='stage6_over_target',
            message=c.detail, target=str(s6_path),
            fix=FIX_RERUN_STAGE6,
            fix_command='re-run Stage 6 with stricter thresholds',
            details={'final_count': n},
        ))
    else:
        checks.append(Check('stage6.content.target_count', PASS,
                            f'final count {n} within band'))

    # 2. Dedup check.
    seen: Dict[str, int] = {}
    for t in s6_tests:
        if isinstance(t, dict):
            ik = str(t.get('issue_key') or '')
            if ik:
                seen[ik] = seen.get(ik, 0) + 1
    dups = {k: v for k, v in seen.items() if v > 1}
    metrics['stage6.duplicate_keys'] = len(dups)
    if dups:
        c = Check('stage6.content.deduplication', FAIL,
                  f'{len(dups)} duplicate issue_keys in final selection '
                  f'(sample: {list(dups.keys())[:3]})')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage6', severity=SEVERITY_WARN,
            type='stage6_dedup_failure',
            message=c.detail, target=str(s6_path),
            fix=FIX_RERUN_STAGE6,
            fix_command='re-run Stage 6 (dedup logic)',
            details={'duplicate_sample': list(dups.keys())[:5]},
        ))
    else:
        checks.append(Check('stage6.content.deduplication', PASS,
                            'no duplicate issue_keys'))

    # 3. Criticality distribution. If Stage 4 produced CRITICAL tests but
    #    Stage 6 has zero CRITICAL we have a priority failure.
    s4_critical = 0
    if p4:
        for t in (p4.get('recommended_tests') or p4.get('tests') or []):
            if isinstance(t, dict) and t.get('criticality') == 'CRITICAL':
                s4_critical += 1
    s6_crit_dist: Dict[str, int] = {}
    for t in s6_tests:
        if isinstance(t, dict):
            crit = t.get('criticality') or 'UNKNOWN'
            s6_crit_dist[crit] = s6_crit_dist.get(crit, 0) + 1
    metrics['stage6.criticality_distribution'] = s6_crit_dist
    if s4_critical and not s6_crit_dist.get('CRITICAL'):
        c = Check('stage6.content.criticality_priority', FAIL,
                  f'Stage 4 had {s4_critical} CRITICAL tests but Stage 6 '
                  f'kept ZERO - aggressive suppression dropped priority signals')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage6', severity=SEVERITY_ERROR,
            type='stage6_priority_failure',
            message=c.detail, target=str(s6_path),
            fix=FIX_RERUN_STAGE6,
            fix_command='re-run Stage 6 preserving CRITICAL',
            details={'s4_critical': s4_critical,
                     's6_distribution': s6_crit_dist},
        ))
    else:
        checks.append(Check('stage6.content.criticality_priority', PASS,
                            f'distribution={s6_crit_dist}'))

    # 4. Changed-component coverage. Coverage may be expressed via
    #    direct fields (component/owning_component/matched_components)
    #    OR indirectly via triggered_by_methods which is resolved through
    #    component_map.json (multi-method consolidated output emits
    #    triggered_by_methods, not the legacy direct-component fields).
    changed_components: List[str] = []
    if p4:
        cc1 = p4.get('changed_components')
        if isinstance(cc1, list):
            changed_components = [str(x) for x in cc1 if x]
        cc2 = p4.get('changed_component')
        if cc2 and not changed_components:
            changed_components = [str(cc2)]
    kb = _kb_dir(Path(audit_output))
    method_to_components = _build_method_to_component_map(kb)
    flow_to_components = _build_flow_to_component_map(kb)
    referenced = _collect_covered_components(
        s6_tests, method_to_components, flow_to_components,
    )
    missing = [c for c in changed_components if c and c not in referenced]
    if changed_components and missing:
        c = Check('stage6.content.changed_component_coverage', FAIL,
                  f'{len(missing)}/{len(changed_components)} changed '
                  f'components NOT covered by final tests ({missing[:3]})')
        checks.append(c)
        issues.append(AuditIssue(
            stage='stage6', severity=SEVERITY_ERROR,
            type='stage6_changed_component_uncovered',
            message=c.detail, target=str(s6_path),
            fix=FIX_RERUN_STAGE6,
            fix_command='re-run Stage 6 preserving component coverage',
            details={'uncovered': missing},
        ))
    elif changed_components:
        checks.append(Check('stage6.content.changed_component_coverage',
                            PASS,
                            f'all {len(changed_components)} changed '
                            'components covered'))

    return checks, issues, metrics


# ---------------------------------------------------------------------------
# Expected-outcome validation per change-type
# ---------------------------------------------------------------------------
def _collect_pipeline_outputs(audit_output: Path,
                              kb_dir: Path) -> Dict[str, Any]:
    """Aggregate the high-level numbers needed for outcome validation."""
    out: Dict[str, Any] = {}
    fr = _safe_load_json(kb_dir / 'flow_registry.json') or {}
    flows = fr.get('flows') or []
    out['unique_flows'] = flows
    out['flow_count'] = len(flows)
    out['synthetic_flow_count'] = sum(
        1 for f in flows
        if (str(f.get('flow_name', '')).startswith('Component:')
            or str(f.get('flow_id', '')).startswith('SYN_')
            or f.get('origin') == 'component_synthetic'))

    p6 = _safe_load_json(audit_output / 'stage6_aggressive_tests.json') or {}
    s6_tests = (p6.get('aggressive_tests') or p6.get('tests')
                or p6.get('output_tests') or [])
    out['final_test_count'] = len(s6_tests)

    p4 = _safe_load_json(audit_output / 'stage4_recommended_tests.json') or {}
    cc1 = p4.get('changed_components') or []
    cc2 = p4.get('changed_component')
    affected = list(cc1) if isinstance(cc1, list) else []
    if cc2 and not affected:
        affected = [cc2]
    out['affected_files'] = len(affected)
    out['changed_components'] = affected
    out['stage4_test_count'] = len(p4.get('recommended_tests')
                                   or p4.get('tests') or [])
    return out


def validate_expected_outcomes(change_type: Optional[str],
                               audit_output: Path,
                               kb_dir: Path
                               ) -> Tuple[List[Check], List[AuditIssue],
                                          Dict[str, Any]]:
    """Validate that pipeline outputs match expectations for the change type.

    ``change_type`` is one of:
        "dependency"            - dependency upgrade / pin change
        "source_single_method"  - single method body changed
        "source_multi_method"   - multiple methods / classes changed
        None                    - skip outcome validation
    """
    checks: List[Check] = []
    issues: List[AuditIssue] = []
    metrics: Dict[str, Any] = {}
    if not change_type:
        return checks, issues, metrics
    ct = change_type.strip().lower()

    outputs = _collect_pipeline_outputs(audit_output, kb_dir)
    metrics['outcomes.snapshot'] = {
        'change_type': ct,
        'affected_files': outputs['affected_files'],
        'flow_count': outputs['flow_count'],
        'synthetic_flow_count': outputs['synthetic_flow_count'],
        'final_test_count': outputs['final_test_count'],
        'stage4_test_count': outputs['stage4_test_count'],
    }

    # Per-change-type expectations (open-band: warn outside, no hard fail).
    if ct == 'dependency':
        # Affected files: dependency change -> typically <= 20 files import it.
        if outputs['affected_files'] > 20:
            c = Check('outcomes.dependency.affected_files', FAIL,
                      f'{outputs["affected_files"]} affected files '
                      f'(expected <=20 for a dependency change)')
            checks.append(c)
            issues.append(AuditIssue(
                stage='outcomes', severity=SEVERITY_WARN,
                type='outcome_excessive_affected_files',
                message=c.detail,
                fix=FIX_REBUILD_FOCUSED_KB,
                fix_command='inspect import detection',
                details={'expected_max': 20,
                         'actual': outputs['affected_files']},
            ))
        else:
            checks.append(Check('outcomes.dependency.affected_files', PASS,
                                f'{outputs["affected_files"]} files'))

        # Flow count: 2-5 unique flows expected.
        if outputs['flow_count'] > 10:
            c = Check('outcomes.dependency.flow_count', FAIL,
                      f'{outputs["flow_count"]} flows (expected 2-5)')
            checks.append(c)
            issues.append(AuditIssue(
                stage='outcomes', severity=SEVERITY_WARN,
                type='outcome_excessive_flows',
                message=c.detail,
                fix=FIX_REBUILD_FLOW_REGISTRY,
                fix_command='python3 build_flow_registry.py',
                details={'expected_max': 10,
                         'actual': outputs['flow_count']},
            ))
        elif outputs['flow_count'] == 0:
            checks.append(Check('outcomes.dependency.flow_count', SKIP,
                                'no flows registered'))
        else:
            checks.append(Check('outcomes.dependency.flow_count', PASS,
                                f'{outputs["flow_count"]} flows'))

        # Synthetic flows must be 0 - dependency changes should resolve to
        # real flows, not "Component: X" placeholders.
        if outputs['synthetic_flow_count'] > 0:
            c = Check('outcomes.dependency.synthetic_flows', FAIL,
                      f'{outputs["synthetic_flow_count"]} synthetic '
                      f'(Component:* / SYN_*) flows present - flow '
                      f'discovery did not resolve real flows')
            checks.append(c)
            issues.append(AuditIssue(
                stage='outcomes', severity=SEVERITY_ERROR,
                type='outcome_synthetic_flows',
                message=c.detail,
                fix=FIX_REBUILD_FLOW_REGISTRY,
                fix_command='python3 build_flow_registry.py',
                details={'synthetic_count': outputs['synthetic_flow_count']},
            ))
        else:
            checks.append(Check('outcomes.dependency.synthetic_flows', PASS,
                                'no synthetic flows'))

        # Final test count: 30-100 expected for dependency change.
        ftc = outputs['final_test_count']
        if ftc > 100:
            c = Check('outcomes.dependency.final_test_count', FAIL,
                      f'{ftc} final tests (expected 30-100)')
            checks.append(c)
            issues.append(AuditIssue(
                stage='outcomes', severity=SEVERITY_WARN,
                type='outcome_excessive_tests',
                message=c.detail,
                fix=FIX_RERUN_STAGE6,
                fix_command='tighten Stage 6 thresholds',
                details={'expected_max': 100, 'actual': ftc},
            ))
        elif 0 < ftc < 10:
            c = Check('outcomes.dependency.final_test_count', FAIL,
                      f'{ftc} final tests (expected 30-100; '
                      'over-suppression suspected)')
            checks.append(c)
            issues.append(AuditIssue(
                stage='outcomes', severity=SEVERITY_WARN,
                type='outcome_under_suppression',
                message=c.detail,
                fix=FIX_RERUN_STAGE6,
                fix_command='loosen Stage 6 thresholds',
                details={'expected_min': 30, 'actual': ftc},
            ))
        else:
            checks.append(Check('outcomes.dependency.final_test_count', PASS,
                                f'{ftc} final tests'))

    elif ct in ('source_single_method', 'source_multi_method',
                'source', 'source_code'):
        # Source change: 1-3 affected flows, 30-60 final tests.
        if outputs['flow_count'] > 15:
            c = Check('outcomes.source.flow_count', FAIL,
                      f'{outputs["flow_count"]} flows (expected 1-15)')
            checks.append(c)
            issues.append(AuditIssue(
                stage='outcomes', severity=SEVERITY_WARN,
                type='outcome_excessive_flows_source',
                message=c.detail,
                fix=FIX_REBUILD_FLOW_REGISTRY,
                fix_command='python3 build_flow_registry.py',
                details={'expected_max': 15,
                         'actual': outputs['flow_count']},
            ))
        else:
            checks.append(Check('outcomes.source.flow_count', PASS,
                                f'{outputs["flow_count"]} flows'))

        ftc = outputs['final_test_count']
        if ftc > 100:
            c = Check('outcomes.source.final_test_count', FAIL,
                      f'{ftc} final tests (expected 30-100)')
            checks.append(c)
            issues.append(AuditIssue(
                stage='outcomes', severity=SEVERITY_WARN,
                type='outcome_excessive_tests_source',
                message=c.detail,
                fix=FIX_RERUN_STAGE6,
                fix_command='tighten Stage 6 thresholds',
                details={'expected_max': 100, 'actual': ftc},
            ))
        else:
            checks.append(Check('outcomes.source.final_test_count', PASS,
                                f'{ftc} final tests'))

    return checks, issues, metrics


def audit_expected_outcomes(change_type: Optional[str],
                            audit_output: Path,
                            kb_dir: Path) -> StageResult:
    """Run expected-outcome validation as its own pseudo-stage so the
    Markdown / JSON reports surface it alongside the structural stages."""
    res = StageResult(stage=8, label='Expected outcomes')
    t0 = time.time()
    if not change_type:
        res.add_check(Check('outcomes.skip', SKIP,
                            'change_type not provided'))
        res.duration_s = time.time() - t0
        return res
    try:
        c, isu, m = validate_expected_outcomes(change_type, audit_output,
                                               kb_dir)
        for ck in c:
            res.checks.append(ck)
        for i in isu:
            res.issues.append(i)
            res.status = FAIL
        res.metrics.update(m)
    except Exception as exc:  # pragma: no cover - defensive
        res.add_check(Check('outcomes.audit', FAIL,
                            f'outcome validation crashed: {exc}'),
                      AuditIssue(stage='outcomes',
                                 severity=SEVERITY_ERROR,
                                 type='outcome_audit_crash',
                                 message=str(exc)))
    res.duration_s = time.time() - t0
    return res


# ---------------------------------------------------------------------------
# Auto-fix dispatch
# ---------------------------------------------------------------------------
def _raw_corpus_path(repo_root: Path) -> Path:
    return Path(repo_root) / '.github' / 'RIA_INPUT' / 'all_tcs_extracted.json'


def apply_fix(issue: AuditIssue,
              repo_root: Path,
              kb_dir: Path,
              script_dir: Path,
              changed_method: Optional[str] = None,
              changed_file: Optional[str] = None) -> bool:
    """Apply the auto-fix recommended for a single issue.

    Returns True on success, False otherwise. Issues with FIX_NONE are
    silently skipped (returns True).
    """
    if not issue.fix or issue.fix == FIX_NONE:
        return True

    raw_corpus = _raw_corpus_path(repo_root)
    print(f'  -> {issue.fix}: {issue.message}')

    if issue.fix == FIX_REBUILD_KB:
        # The KB-rebuild child must not trigger its OWN audit cycle: the
        # parent will re-audit after this fix completes. Without
        # --no-audit the child would recurse and could loop indefinitely
        # if the same issue is detected again (e.g. transient KB build
        # failure surfacing as a different audit issue).
        cmd = ['python3', str(script_dir / 'ria_agent.py'),
               '--rebuild-kb', '--user-prompt', 'Audit auto-fix: rebuild KB',
               '--no-html', '--no-audit', '--audit-child']
        rc, _, stderr = _run(cmd, cwd=Path(repo_root), timeout=3600)
        if rc != 0 and stderr:
            print(f'    [ERROR] {stderr.strip().splitlines()[-1]}')
        return rc == 0

    if issue.fix == FIX_REBUILD_SYNONYM_GROUPS:
        rc, _, _ = _run(
            ['python3', str(script_dir / 'build_synonym_groups.py')],
            cwd=Path(repo_root))
        return rc == 0

    if issue.fix == FIX_REBUILD_COMPONENT_MAP:
        rc, _, _ = _run(
            ['python3', str(script_dir / 'build_component_map.py')],
            cwd=Path(repo_root))
        return rc == 0

    if issue.fix == FIX_REBUILD_DISCOVERED_VOCAB:
        rc1, _, _ = _run(
            ['python3', str(script_dir / 'build_discovered_vocabularies.py'),
             '--only', 'framework_suffixes'],
            cwd=Path(repo_root))
        rc2, _, _ = _run(
            ['python3', str(script_dir / 'build_discovered_vocabularies.py'),
             '--only', 'vocabularies'],
            cwd=Path(repo_root))
        return rc1 == 0 and rc2 == 0

    if issue.fix == FIX_REBUILD_IDF:
        rc, _, _ = _run(
            ['python3', str(script_dir / 'term_idf.py'),
             '--corpus', str(raw_corpus),
             '--output', str(Path(kb_dir) / 'idf_index.json')],
            cwd=Path(repo_root))
        return rc == 0

    if issue.fix == FIX_REBUILD_EMBEDDINGS:
        rc, _, _ = _run(
            ['python3', str(script_dir / 'build_embeddings.py'),
             '--corpus', str(raw_corpus),
             '--output', str(Path(kb_dir) / 'embeddings_index.npz')],
            cwd=Path(repo_root))
        return rc == 0

    if issue.fix == FIX_REBUILD_FLOW_REGISTRY:
        if not (changed_method and changed_file):
            print('    [SKIP] flow_registry rebuild needs '
                  '--changed-method/--changed-file')
            return False
        rc, _, _ = _run(
            ['python3', str(script_dir / 'build_flow_registry.py'),
             '--changed-method', changed_method,
             '--changed-file', changed_file],
            cwd=Path(repo_root))
        return rc == 0

    if issue.fix == FIX_REBUILD_FLOW_DEPENDENCIES:
        rc, _, _ = _run(
            ['python3', str(script_dir / 'build_flow_dependencies.py')],
            cwd=Path(repo_root))
        return rc == 0

    if issue.fix == FIX_REBUILD_FOCUSED_KB:
        ok1 = apply_fix(
            AuditIssue(stage=issue.stage, severity=issue.severity,
                       type=issue.type, message=issue.message,
                       fix=FIX_REBUILD_FLOW_REGISTRY, fix_command=''),
            repo_root, kb_dir, script_dir,
            changed_method=changed_method, changed_file=changed_file)
        ok2 = apply_fix(
            AuditIssue(stage=issue.stage, severity=issue.severity,
                       type=issue.type, message=issue.message,
                       fix=FIX_REBUILD_FLOW_DEPENDENCIES, fix_command=''),
            repo_root, kb_dir, script_dir)
        return ok1 and ok2

    if issue.fix == FIX_REGENERATE_HTML:
        rc, _, _ = _run(
            ['python3', str(script_dir / 'generate_html_report.py')],
            cwd=Path(repo_root))
        return rc == 0

    if issue.fix == FIX_RERUN_STAGE:
        # Stage rerun is handled at the orchestrator level via
        # rerun_pipeline; report success so the dedup loop continues.
        return True

    if issue.fix == FIX_RERUN_STAGE1:
        # Re-run Stage 1 only (call tree analysis). Requires
        # --changed-method + --changed-file so the focused KB rebuild
        # can scope to the right component.
        if not changed_method or not changed_file:
            print('    [SKIP] FIX_RERUN_STAGE1 requires '
                  '--changed-method + --changed-file')
            return False
        cmd = ['python3', str(script_dir / 'ria_agent.py'),
               '--changed-method', changed_method,
               '--changed-file', changed_file,
               '--stage', '1',
               '--no-audit', '--no-html', '--audit-child']
        rc, _, stderr = _run(cmd, cwd=Path(repo_root), timeout=600)
        if rc != 0 and stderr:
            print(f'    [ERROR] {stderr.strip().splitlines()[-1]}')
        return rc == 0

    if issue.fix == FIX_RERUN_STAGE2:
        # Re-run Stage 2 only (flow mapping). Requires
        # --changed-method + --changed-file.
        if not changed_method or not changed_file:
            print('    [SKIP] FIX_RERUN_STAGE2 requires '
                  '--changed-method + --changed-file')
            return False
        cmd = ['python3', str(script_dir / 'ria_agent.py'),
               '--changed-method', changed_method,
               '--changed-file', changed_file,
               '--stage', '2',
               '--no-audit', '--no-html', '--audit-child']
        rc, _, stderr = _run(cmd, cwd=Path(repo_root), timeout=600)
        if rc != 0 and stderr:
            print(f'    [ERROR] {stderr.strip().splitlines()[-1]}')
        return rc == 0

    if issue.fix == FIX_RERUN_STAGE3:
        # Re-run Stage 3 only (indirect flows). Requires
        # --changed-method + --changed-file.
        if not changed_method or not changed_file:
            print('    [SKIP] FIX_RERUN_STAGE3 requires '
                  '--changed-method + --changed-file')
            return False
        cmd = ['python3', str(script_dir / 'ria_agent.py'),
               '--changed-method', changed_method,
               '--changed-file', changed_file,
               '--stage', '3',
               '--no-audit', '--no-html', '--audit-child']
        rc, _, stderr = _run(cmd, cwd=Path(repo_root), timeout=600)
        if rc != 0 and stderr:
            print(f'    [ERROR] {stderr.strip().splitlines()[-1]}')
        return rc == 0

    if issue.fix in (FIX_RERUN_STAGE5, FIX_RERUN_STAGE6):
        # Single-stage reruns are handled by the orchestrator via the
        # rerun_pipeline callback (using FIX_TO_RERUN_FROM_STAGE to pick
        # the earliest stage). Report success so dedup continues.
        return True

    print(f'    [WARN] unknown fix kind: {issue.fix}')
    return False


def apply_all_fixes(issues: List[AuditIssue],
                    repo_root: Path,
                    kb_dir: Path,
                    script_dir: Path,
                    changed_method: Optional[str] = None,
                    changed_file: Optional[str] = None) -> Dict[str, Any]:
    """Apply every fixable issue exactly once (deduplicated by fix kind).

    Returns a dict with applied/skipped/failed counts plus the earliest
    pipeline stage that needs to re-run.
    """
    _print_section('APPLYING AUTO-FIXES')

    seen: set = set()
    applied = 0
    skipped = 0
    failed = 0
    earliest_rerun = 99

    for issue in issues:
        if not issue.fix or issue.fix == FIX_NONE:
            continue
        key = (issue.fix, issue.target)
        if key in seen:
            skipped += 1
            continue
        seen.add(key)
        ok = apply_fix(issue, repo_root, kb_dir, script_dir,
                       changed_method=changed_method,
                       changed_file=changed_file)
        if ok:
            applied += 1
            stage_for_fix = FIX_TO_RERUN_FROM_STAGE.get(issue.fix, 99)
            if stage_for_fix < earliest_rerun:
                earliest_rerun = stage_for_fix
        else:
            failed += 1

    print(f'\n  Applied : {applied}')
    print(f'  Skipped : {skipped}  (duplicate fix targets)')
    print(f'  Failed  : {failed}')
    print(f'  Re-run from stage : '
          f'{earliest_rerun if earliest_rerun < 99 else "(none)"}')
    return {
        'applied': applied,
        'skipped': skipped,
        'failed': failed,
        'rerun_from_stage': (earliest_rerun
                             if earliest_rerun < 99
                             else None),
    }


# ---------------------------------------------------------------------------
# User approval
# ---------------------------------------------------------------------------
def prompt_user_for_approval(issues: List[AuditIssue]) -> bool:
    """Prompt the user (y/N) before applying auto-fixes.

    Always returns False on EOF / non-interactive shells so the default
    behavior is "do not modify anything".
    """
    fixable = [i for i in issues
               if i.fix and i.fix != FIX_NONE
               and i.severity != SEVERITY_INFO]
    if not fixable:
        print('\n  No auto-fixes available.')
        return False

    print(f'\n  {len(fixable)} auto-fix(es) available:')
    seen: set = set()
    for issue in fixable:
        key = (issue.fix, issue.target)
        if key in seen:
            continue
        seen.add(key)
        print(f'    - {issue.fix}: {issue.fix_command}')

    if not sys.stdin.isatty():
        print('\n  Non-interactive shell - skipping auto-fix.')
        print('  Re-run with apply_fixes="yes" (or --auto-fix-audit) '
              'to apply unattended.')
        return False

    try:
        ans = input('\n  Apply these auto-fixes now? [y/N]: ').strip().lower()
    except EOFError:
        return False
    return ans in ('y', 'yes')


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
_STATUS_GLYPH = {
    PASS: 'PASS',
    FAIL: 'FAIL',
    FIXED: 'FIXED',
    SKIP: 'SKIP',
}


def generate_audit_report(results: List[StageResult], output_path: Path) -> Path:
    """Render the stage-level audit results as Markdown."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines: List[str] = []
    lines.append('# RIA Pipeline Stage Execution Audit')
    lines.append('')
    lines.append(f'Generated: {time.strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append('')

    # Summary table
    lines.append('## Summary')
    lines.append('')
    lines.append('| Stage | Label | Status | Checks (P/F/S) | Duration (s) |')
    lines.append('|-------|-------|--------|----------------|--------------|')
    for r in results:
        passes = sum(1 for c in r.checks if c.status == PASS)
        fails = sum(1 for c in r.checks if c.status == FAIL)
        skips = sum(1 for c in r.checks if c.status == SKIP)
        lines.append(
            f'| {r.stage} | {r.label} | {_STATUS_GLYPH.get(r.status, r.status)} '
            f'| {passes}/{fails}/{skips} | {r.duration_s:.2f} |'
        )
    lines.append('')

    # Per-stage detail
    for r in results:
        lines.append(f'## Stage {r.stage}: {r.label}')
        lines.append('')
        lines.append(f'**Status:** {_STATUS_GLYPH.get(r.status, r.status)}')
        lines.append('')
        if r.checks:
            lines.append('| Check | Status | Detail |')
            lines.append('|-------|--------|--------|')
            for c in r.checks:
                detail = (c.detail or '').replace('|', '\\|')
                lines.append(
                    f'| {c.name} | {_STATUS_GLYPH.get(c.status, c.status)} | {detail} |'
                )
            lines.append('')
        if r.issues:
            lines.append('**Issues:**')
            lines.append('')
            for i in r.issues:
                fix_hint = (f' (fix: `{i.fix_command}`)'
                            if i.fix_command else '')
                lines.append(f'- [{i.severity}] {i.message}{fix_hint}')
            lines.append('')
        if r.fixes_applied:
            lines.append('**Fixes applied:**')
            lines.append('')
            for f in r.fixes_applied:
                lines.append(f'- `{f}`')
            lines.append('')
        if r.metrics:
            lines.append('**Metrics:**')
            lines.append('')
            for k, v in r.metrics.items():
                lines.append(f'- `{k}` = {v}')
            lines.append('')

    # Recommendations
    lines.append('## Recommendations')
    lines.append('')
    failing = [r for r in results if r.status == FAIL]
    fixed = [r for r in results if r.status == FIXED]
    if not failing and not fixed:
        lines.append('All stages PASSED. No remediation required.')
    else:
        if failing:
            lines.append('### Failing stages')
            lines.append('')
            for r in failing:
                lines.append(f'- **Stage {r.stage} ({r.label})**: '
                             f'{len(r.issues)} issue(s).')
                for i in r.issues[:3]:
                    lines.append(f'  - {i.message}')
            lines.append('')
            lines.append('Re-run RIA with audit fixes enabled:')
            lines.append('')
            lines.append('```')
            lines.append('python3 ria_agent.py --auto-fix-audit')
            lines.append('```')
            lines.append('')
        if fixed:
            lines.append('### Auto-fixed stages')
            lines.append('')
            for r in fixed:
                lines.append(f'- **Stage {r.stage} ({r.label})** was fixed by:')
                for f in r.fixes_applied:
                    lines.append(f'  - `{f}`')
            lines.append('')

    output_path.write_text('\n'.join(lines), encoding='utf-8')
    return output_path


def write_audit_report_json(issues: List[AuditIssue], output_dir: Path) -> Path:
    """Persist the audit issues as JSON for tooling consumption."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / 'audit_report.json'
    payload = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'total_issues': len(issues),
        'issues': [i.to_dict() for i in issues],
    }
    path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    return path


def write_before_after_report(before: List[AuditIssue],
                              after: List[AuditIssue],
                              output_dir: Path) -> Path:
    """Persist a JSON comparing audit results before vs after auto-fix."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / 'audit_report_before_after.json'

    def _summarize(items: List[AuditIssue]) -> Dict[str, Any]:
        return {
            'total': len(items),
            'errors': sum(1 for i in items if i.severity == SEVERITY_ERROR),
            'warnings': sum(1 for i in items if i.severity == SEVERITY_WARN),
            'info': sum(1 for i in items if i.severity == SEVERITY_INFO),
        }

    before_keys = {(i.stage, i.type, i.target) for i in before}
    after_keys = {(i.stage, i.type, i.target) for i in after}
    resolved = sorted(before_keys - after_keys)
    introduced = sorted(after_keys - before_keys)
    persisted = sorted(before_keys & after_keys)

    payload = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'before': _summarize(before),
        'after': _summarize(after),
        'resolved': [{'stage': s, 'type': t, 'target': tg}
                     for s, t, tg in resolved],
        'introduced': [{'stage': s, 'type': t, 'target': tg}
                       for s, t, tg in introduced],
        'persisted': [{'stage': s, 'type': t, 'target': tg}
                      for s, t, tg in persisted],
    }
    path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    return path


def print_audit_report(issues: List[AuditIssue]) -> None:
    """Pretty-print the flat issue list to stdout."""
    _print_section('AUDIT REPORT')
    if not issues:
        print('  All stages PASS - no issues detected.')
        return

    by_stage: Dict[str, List[AuditIssue]] = {}
    for i in issues:
        by_stage.setdefault(i.stage, []).append(i)

    for stage in sorted(by_stage):
        bucket = by_stage[stage]
        n_err = sum(1 for i in bucket if i.severity == SEVERITY_ERROR)
        n_warn = sum(1 for i in bucket if i.severity == SEVERITY_WARN)
        n_info = sum(1 for i in bucket if i.severity == SEVERITY_INFO)
        print(f'\n  {stage.upper()}: {len(bucket)} issue(s) '
              f'(ERROR={n_err}, WARN={n_warn}, INFO={n_info})')
        for idx, issue in enumerate(bucket, 1):
            print(f'    {idx}. [{issue.severity}] {issue.message}')
            if issue.target:
                print(f'       target : {issue.target}')
            if issue.fix and issue.fix != FIX_NONE:
                print(f'       fix    : {issue.fix}  ({issue.fix_command})')
            if issue.details:
                snippet = {k: issue.details[k]
                           for k in list(issue.details.keys())[:3]}
                print(f'       detail : {snippet}')

    fixable = sum(1 for i in issues
                  if i.fix and i.fix != FIX_NONE
                  and i.severity != SEVERITY_INFO)
    print()
    print(f'  Total issues       : {len(issues)}')
    print(f'  Auto-fixes available: {fixable}')


def print_before_after_summary(before: List[AuditIssue],
                               after: List[AuditIssue]) -> None:
    """Compact CLI summary of the before/after comparison."""
    _print_section('BEFORE / AFTER COMPARISON')
    n_before_err = sum(1 for i in before if i.severity == SEVERITY_ERROR)
    n_after_err = sum(1 for i in after if i.severity == SEVERITY_ERROR)
    n_before_warn = sum(1 for i in before if i.severity == SEVERITY_WARN)
    n_after_warn = sum(1 for i in after if i.severity == SEVERITY_WARN)

    before_keys = {(i.stage, i.type, i.target) for i in before}
    after_keys = {(i.stage, i.type, i.target) for i in after}
    resolved = before_keys - after_keys
    introduced = after_keys - before_keys

    print(f'  Issues before fix : {len(before)}  '
          f'(ERROR={n_before_err}, WARN={n_before_warn})')
    print(f'  Issues after fix  : {len(after)}   '
          f'(ERROR={n_after_err}, WARN={n_after_warn})')
    print(f'  Resolved          : {len(resolved)}')
    print(f'  Newly introduced  : {len(introduced)}')
    if introduced:
        print('  WARNING: auto-fix introduced new issues - review the report.')


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _run_all_audits(audit_output: Path,
                    change_type: Optional[str] = None,
                    kb_dir: Optional[Path] = None,
                    changed_method: Optional[str] = None,
                    changed_file: Optional[str] = None,
                    repo_root: Optional[Path] = None
                    ) -> List[StageResult]:
    """Run each stage audit; never abort early - collect all issues.

    When ``change_type`` is provided, an additional pseudo-stage 8
    "Expected outcomes" runs AFTER the structural stages to validate
    that pipeline outputs match expectations for the change type.
    """
    _print_section('AUDIT - DETECTION ONLY (no fixes will be applied)')
    kb_d = Path(kb_dir or _kb_dir(audit_output))
    repo_root_p = Path(repo_root or REPO_ROOT)
    results: List[StageResult] = []
    audits = [
        # Option A: pass change_type into Stage 0 so the enriched-corpus
        # deep validator inspects the per-pipeline file
        # (`*_source.json` / `*_dependency.json`) that matches the pipeline
        # currently being audited.
        ('Stage 0', lambda: audit_stage_0(audit_output,
                                          change_type=change_type)),
        ('Stage 1', lambda: audit_stage_1(audit_output, changed_method,
                                          changed_file, repo_root_p)),
        ('Stage 2', lambda: audit_stage_2(audit_output, kb_d, change_type)),
        ('Stage 3', lambda: audit_stage_3(audit_output, kb_d, change_type)),
        ('Stage 4', lambda: audit_stage_4(audit_output)),
        ('Stage 5', lambda: audit_stage_5(audit_output)),
        ('Stage 6', lambda: audit_stage_6(audit_output)),
        ('Stage 7', lambda: audit_stage_7(audit_output)),
    ]
    if change_type:
        audits.append(('Stage 8',
                       lambda: audit_expected_outcomes(change_type,
                                                       audit_output,
                                                       kb_d)))
    for label, fn in audits:
        try:
            r = fn()
        except Exception as exc:
            stage_num = int(label.split()[-1])
            r = StageResult(stage=stage_num, label=label, status=FAIL)
            r.issues.append(AuditIssue(
                stage=f'stage{stage_num}',
                severity=SEVERITY_ERROR,
                type='audit_hook_crashed',
                message=f'{label} audit hook crashed: {exc}',
            ))
        n_err = sum(1 for i in r.issues if i.severity == SEVERITY_ERROR)
        n_warn = sum(1 for i in r.issues if i.severity == SEVERITY_WARN)
        n_info = sum(1 for i in r.issues if i.severity == SEVERITY_INFO)
        if r.status == PASS:
            print(f'  {label}: PASS')
        elif r.status == SKIP:
            # Stages skipped by architectural design (e.g. multi-method runs
            # that route around stage1/stage2/stage3 files). Show the reason
            # captured in metrics so the user can see WHY it was skipped.
            reason = ''
            try:
                for k, v in (r.metrics or {}).items():
                    if isinstance(k, str) and k.endswith('skipped_reason'):
                        reason = f' ({v})'
                        break
            except Exception:
                reason = ''
            print(f'  {label}: SKIP{reason}')
        else:
            print(f'  {label}: {r.status} - {len(r.issues)} issue(s) '
                  f'(ERROR={n_err}, WARN={n_warn}, INFO={n_info})')
        results.append(r)
    return results


def _flatten_issues(results: List[StageResult]) -> List[AuditIssue]:
    out: List[AuditIssue] = []
    for r in results:
        out.extend(r.issues)
    return out


def _normalize_apply_fixes(apply_fixes: Any) -> str:
    """Normalize backwards-compatible inputs to {"no", "prompt", "yes"}."""
    if apply_fixes is True:
        return 'yes'
    if apply_fixes is False or apply_fixes is None:
        return 'no'
    if isinstance(apply_fixes, str):
        v = apply_fixes.strip().lower()
        if v in ('y', 'yes', 'true', 'auto'):
            return 'yes'
        if v in ('n', 'no', 'false'):
            return 'no'
        if v in ('prompt', 'interactive', 'ask'):
            return 'prompt'
    return 'prompt'


def audit_full_pipeline(repo_root: Optional[str] = None,
                        output_dir: Optional[Path] = None,
                        kb_dir: Optional[Path] = None,
                        script_dir: Optional[Path] = None,
                        changed_method: Optional[str] = None,
                        changed_file: Optional[str] = None,
                        change_type: Optional[str] = None,
                        apply_fixes: Any = 'prompt',
                        rerun_pipeline: Optional[Callable[[int], bool]] = None,
                        # Backward-compat alias for the older
                        # auto_fix:bool/audit_output:Path API.
                        auto_fix: Optional[bool] = None,
                        audit_output: Optional[Path] = None,
                        ) -> Dict[str, Any]:
    """Run the full audit lifecycle (detection -> approval -> fix -> rerun).

    Args:
        repo_root:       Repo root used for subprocesses.
        output_dir:      RIA_OUTPUT directory to inspect.
        kb_dir:          Knowledge base dir (defaults to output_dir/knowledge_base).
        script_dir:      Directory holding the build_*.py / ria_agent.py scripts.
        changed_method:  Optional context for focused-KB rebuilds.
        changed_file:    Optional context for focused-KB rebuilds.
        change_type:     Optional. One of "dependency",
                         "source_single_method", "source_multi_method".
                         When provided, the auditor adds Stage 8 "Expected
                         outcomes" which validates that the pipeline's
                         outputs match the expected band for that change.
        apply_fixes:     One of "no" | "prompt" | "yes". Defaults to "prompt".
                         Booleans True/False also accepted for backward compat.
        rerun_pipeline:  Optional callable(rerun_from_stage:int) -> bool.
                         Invoked AFTER fixes are applied to re-run downstream
                         stages. If None, the orchestrator skips the re-run
                         step but still emits a before/after report comparing
                         the existing outputs.
        auto_fix:        Backward-compat alias. ``auto_fix=True`` is treated
                         as ``apply_fixes="yes"``.
        audit_output:    Backward-compat alias for ``output_dir``.

    Returns: dict with keys {
        overall_status: "PASS" | "FAIL" | "FIXED",
        results:        List[StageResult.to_dict()],
        issues:         flat list of AuditIssue dicts (before fixes),
        fixes_applied:  None or {applied, skipped, failed, rerun_from_stage},
        rerun_ok:       None | bool,
        before_issues:  list[dict],
        after_issues:   None | list[dict],
        report_paths:   {markdown, json, before_after?},
    }
    """
    # Resolve the various overlapping API knobs.
    repo_root = str(repo_root or REPO_ROOT)
    output_dir = Path(output_dir or audit_output or RIA_OUTPUT_DIR)
    kb_dir = Path(kb_dir or _kb_dir(output_dir))
    script_dir = Path(script_dir or SCRIPT_DIR)
    if auto_fix is True and apply_fixes == 'prompt':
        apply_fixes = 'yes'
    elif auto_fix is False and apply_fixes == 'prompt':
        apply_fixes = 'no'
    mode = _normalize_apply_fixes(apply_fixes)

    # Phase 1: detection.
    results_before = _run_all_audits(output_dir,
                                     change_type=change_type,
                                     kb_dir=kb_dir,
                                     changed_method=changed_method,
                                     changed_file=changed_file,
                                     repo_root=Path(repo_root))
    before_issues = _flatten_issues(results_before)

    # Persist Markdown + JSON reports immediately (so users get an artifact
    # even if Phase 3 is skipped).
    md_path = output_dir / 'validation_audit_report.md'
    generate_audit_report(results_before, md_path)
    json_path = write_audit_report_json(before_issues, output_dir)
    print_audit_report(before_issues)
    print(f'\n  Audit report (Markdown): {md_path}')
    print(f'  Audit report (JSON)    : {json_path}')

    # SKIP is a clean exit (architectural skip, not a failure). Treat
    # PASS and SKIP equivalently when computing overall status.
    overall = (PASS if all(r.status in (PASS, SKIP) for r in results_before)
               else FAIL)
    result: Dict[str, Any] = {
        'overall_status': overall,
        'results': [r.to_dict() for r in results_before],
        'issues': [i.to_dict() for i in before_issues],
        'before_issues': [i.to_dict() for i in before_issues],
        'after_issues': None,
        'fixes_applied': None,
        'rerun_ok': None,
        'report_paths': {
            'markdown': str(md_path),
            'json': str(json_path),
        },
    }

    # Phase 2: decide whether to apply fixes.
    fixable = [i for i in before_issues
               if i.fix and i.fix != FIX_NONE
               and i.severity != SEVERITY_INFO]

    proceed = False
    if not fixable:
        # Nothing to fix; report "no" and exit cleanly.
        if overall == PASS:
            print('\n  Audit PASS - no fixes needed.')
        else:
            print('\n  No auto-fixable issues detected.')
        return result

    if mode == 'yes':
        proceed = True
    elif mode == 'prompt':
        proceed = prompt_user_for_approval(before_issues)
    else:  # 'no'
        proceed = False

    if not proceed:
        print('\n  No fixes applied. To apply, re-run with '
              'apply_fixes="yes" (or --auto-fix-audit on ria_agent).')
        return result

    # Phase 3: apply fixes, optionally re-run downstream stages, re-audit.
    summary = apply_all_fixes(fixable, Path(repo_root), kb_dir, script_dir,
                              changed_method=changed_method,
                              changed_file=changed_file)
    result['fixes_applied'] = summary

    # Re-run pipeline from the earliest affected stage if a callback is given.
    rerun_ok: Optional[bool] = None
    if rerun_pipeline is not None and summary.get('rerun_from_stage') is not None:
        _print_section(
            f'RE-RUNNING PIPELINE FROM STAGE '
            f'{summary["rerun_from_stage"]} AFTER FIXES'
        )
        try:
            rerun_ok = bool(rerun_pipeline(summary['rerun_from_stage']))
        except TypeError:
            # Older callbacks have signature () -> bool; fall back to it.
            try:
                rerun_ok = bool(rerun_pipeline())  # type: ignore[misc]
            except Exception as exc:
                print(f'  [ERROR] pipeline re-run crashed: {exc}')
                rerun_ok = False
        except Exception as exc:
            print(f'  [ERROR] pipeline re-run crashed: {exc}')
            rerun_ok = False
    elif rerun_pipeline is None:
        print('\n  [WARN] rerun_pipeline callback not provided - skipping '
              'pipeline re-run. Run RIA again manually to verify.')
    result['rerun_ok'] = rerun_ok

    # Re-audit and emit before/after report.
    results_after = _run_all_audits(output_dir,
                                    change_type=change_type,
                                    kb_dir=kb_dir,
                                    changed_method=changed_method,
                                    changed_file=changed_file,
                                    repo_root=Path(repo_root))
    after_issues = _flatten_issues(results_after)
    print_audit_report(after_issues)
    print_before_after_summary(before_issues, after_issues)

    # Update the Markdown report to reflect the FIXED state.
    for r in results_after:
        for r0 in results_before:
            if r.stage == r0.stage:
                if r0.status == FAIL and r.status == PASS:
                    r.status = FIXED
                    r.fixes_applied = list({i.fix for i in r0.issues
                                            if i.fix and i.fix != FIX_NONE})
    generate_audit_report(results_after, md_path)
    ba_path = write_before_after_report(before_issues, after_issues, output_dir)
    print(f'\n  Before/after report (JSON): {ba_path}')

    result['after_issues'] = [i.to_dict() for i in after_issues]
    result['report_paths']['before_after'] = str(ba_path)
    if all(r.status in (PASS, FIXED, SKIP) for r in results_after):
        result['overall_status'] = FIXED if any(
            r.status == FIXED for r in results_after) else PASS
    else:
        result['overall_status'] = FAIL
    result['results'] = [r.to_dict() for r in results_after]

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description='Stage-by-stage execution auditor for the RIA pipeline.',
    )
    p.add_argument('--stage', type=int, choices=[0, 4, 5, 6, 7, 8],
                   help='Audit a single stage only (8 = expected outcomes)')
    p.add_argument('--full-pipeline', action='store_true',
                   help='Audit every stage in order')
    p.add_argument('--auto-fix', action='store_true',
                   help='Apply fixes unattended (apply_fixes="yes")')
    p.add_argument('--prompt-fix', action='store_true',
                   help='Prompt the user for approval before applying '
                        '(apply_fixes="prompt")')
    p.add_argument('--report-only', action='store_true',
                   help='Detection only - no fixes (apply_fixes="no")')
    p.add_argument('--audit-output', default=str(RIA_OUTPUT_DIR),
                   help='RIA_OUTPUT directory to audit')
    p.add_argument('--repo-root', default=REPO_ROOT,
                   help='Repository root (used by auto-fix rebuilds)')
    p.add_argument('--report', default=None,
                   help='Markdown report output path')
    p.add_argument('--changed-method', default=None,
                   help='Changed method for context (focused-KB rebuilds)')
    p.add_argument('--changed-file', default=None,
                   help='Changed file for context (focused-KB rebuilds)')
    p.add_argument('--change-type', default=None,
                   choices=['dependency', 'source_single_method',
                            'source_multi_method', 'source', 'source_code'],
                   help='Change type for expected-outcome validation '
                        '(adds Stage 8 audit)')
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    audit_output = Path(args.audit_output)

    if args.auto_fix:
        mode: Any = 'yes'
    elif args.report_only:
        mode = 'no'
    elif args.prompt_fix:
        mode = 'prompt'
    else:
        mode = 'no'  # default for the standalone CLI is detection-only

    # Single-stage debugging path: just dump the StageResult JSON and bail.
    if args.stage is not None and not args.full_pipeline:
        if args.stage == 0:
            r = audit_stage_0(audit_output)
        elif args.stage == 1:
            r = audit_stage_1(audit_output, args.changed_method,
                              args.changed_file, Path(args.repo_root))
        elif args.stage == 2:
            r = audit_stage_2(audit_output, _kb_dir(audit_output),
                              args.change_type)
        elif args.stage == 3:
            r = audit_stage_3(audit_output, _kb_dir(audit_output),
                              args.change_type)
        elif args.stage == 4:
            r = audit_stage_4(audit_output)
        elif args.stage == 5:
            r = audit_stage_5(audit_output)
        elif args.stage == 6:
            r = audit_stage_6(audit_output)
        elif args.stage == 7:
            r = audit_stage_7(audit_output)
        elif args.stage == 8:
            r = audit_expected_outcomes(args.change_type,
                                        audit_output,
                                        _kb_dir(audit_output))
        else:  # pragma: no cover
            print(f'unknown stage {args.stage}', file=sys.stderr)
            return 2
        print(json.dumps({'results': [r.to_dict()]}, indent=2))
        report_path = (Path(args.report) if args.report
                       else audit_output / 'validation_audit_report.md')
        generate_audit_report([r], report_path)
        print(f'\n[auditor] Markdown report: {report_path}')
        return 0 if r.status in (PASS, FIXED) else 1

    # Full pipeline path with optional fixes.
    out = audit_full_pipeline(
        repo_root=args.repo_root,
        output_dir=audit_output,
        changed_method=args.changed_method,
        changed_file=args.changed_file,
        change_type=args.change_type,
        apply_fixes=mode,
    )
    if args.report:
        # The orchestrator already wrote validation_audit_report.md; copy
        # to the user-requested path if different.
        target = Path(args.report)
        if target.resolve() != Path(out['report_paths']['markdown']).resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                Path(out['report_paths']['markdown']).read_text(encoding='utf-8'),
                encoding='utf-8',
            )

    return 0 if out['overall_status'] in (PASS, FIXED) else 1


if __name__ == '__main__':
    sys.exit(main())
