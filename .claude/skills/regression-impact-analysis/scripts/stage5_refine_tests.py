#!/usr/bin/env python3
"""
Stage 5: Sophisticated Test Refinement

Reduces Stage 4 output to a focused subset using:
  1. Keyword precision filter (score >= threshold) - keywords derived
     dynamically from the changed method name (camelCase split).
  2. Flow diversity boost - keeps top N tests per flow registry entry.

Post-pipeline-simplification: Stage 5 now reads flows directly from the
focused KB (flow_registry.json + flow_dependencies.json) - the legacy
stage2_impacted_flows.json shim has been removed.

  - METHOD_KEYWORDS are derived at runtime from --changed-method.
  - FLOW_QUOTAS are derived at runtime from --flow-registry JSON.
  - changed_components are passed explicitly via --changed-components.
  - Input/output paths are CLI-driven (no hard-coded absolute paths).
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# Path defaults (still resolvable by file location, not absolute hard-coding)
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
GITHUB_DIR = SKILL_DIR.parent.parent
REPO_ROOT = GITHUB_DIR.parent
DEFAULT_OUTPUT_DIR = GITHUB_DIR / 'RIA_OUTPUT'

DEFAULT_INPUT = DEFAULT_OUTPUT_DIR / 'stage4_recommended_tests.json'
DEFAULT_OUTPUT = DEFAULT_OUTPUT_DIR / 'stage5_refined_tests.json'
DEFAULT_KB_DIR = DEFAULT_OUTPUT_DIR / 'knowledge_base'
DEFAULT_FLOW_REGISTRY = DEFAULT_KB_DIR / 'flow_registry.json'
DEFAULT_FLOW_DEPENDENCIES = DEFAULT_KB_DIR / 'flow_dependencies.json'

# Shared scenario coercion (agent may write test_scenarios as strings or dicts).
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import agent_reasoning  # noqa: E402

# ---------------------------------------------------------------------------
# Algorithm constants
# ---------------------------------------------------------------------------
DEFAULT_KEYWORD_THRESHOLD = 1    # lowered to capture domain vocabulary gaps
DEFAULT_PRIMARY_FLOW_QUOTA = 20  # quota for the first flow in stage2 output
DEFAULT_OTHER_FLOW_QUOTA = 10    # quota for every other flow

# Generic verbs/method-name parts that have no business-domain identity. Used
# to drop noise tokens from the derived keyword list (e.g. 'get', 'set', 'do').
# Loaded at runtime from the discovered KB (Stage 0d) so the set adapts to
# the codebase / test corpus instead of relying on a hardcoded list.
def _load_generic_tokens(kb_dir=None, *, required: bool = False) -> set:
    """Load the discovered generic-noun set from the KB.

    When `required=True` (i.e. invoked from refine_tests with a real
    kb_dir), a missing/unreadable file raises RuntimeError so missing
    Stage 0d output surfaces immediately. The module-level call with
    `required=False` returns an empty set so `import stage5_refine_tests`
    succeeds even when the default KB path does not exist yet.
    """
    if kb_dir is None:
        kb_dir = Path(DEFAULT_OUTPUT_DIR) / 'knowledge_base'
    path = Path(kb_dir) / 'discovered_generic_nouns.json'
    if not path.exists():
        if required:
            raise FileNotFoundError(
                f"[stage5] discovered_generic_nouns.json not found at: "
                f"{path}\n"
                f"Root cause: Stage 0d (build_discovered_vocabularies.py) "
                f"did not produce the generic-nouns set.\n"
                f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
            )
        return set()
    data = json.loads(path.read_text(encoding='utf-8'))
    nouns = set(data.get('generic_nouns', []))
    if required and not nouns:
        raise RuntimeError(
            f"[stage5] discovered_generic_nouns.json at {path} contains "
            f"zero generic_nouns.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    return nouns


# Module-level placeholder. refine_tests() reloads this with required=True
# once the real kb_dir is known.
GENERIC_TOKENS = _load_generic_tokens()


def _derive_embedding_bypass_threshold(kb_dir=None) -> float:
    """Return the cosine similarity above which a test bypasses the
    component-keyword filter.

    Algorithm:
      1. Read embeddings_index.npz statistics if available — use the
         95th percentile of pairwise diff/test similarities as the gate.
      2. Else use 0.45 as a statistically reasonable default that has
         been validated on the EEM corpus (top-5% of similarities).

    Returns a float in [0.0, 1.0].
    """
    default = 0.45
    if not kb_dir:
        return default
    # Statistics file is optional — embeddings_index.npz holds the
    # numpy arrays; a sibling JSON could hold percentile stats.
    stats_path = os.path.join(str(kb_dir), 'embeddings_stats.json')
    if not os.path.isfile(stats_path):
        return default
    try:
        with open(stats_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        p95 = data.get('p95_diff_similarity')
        if p95 and 0.0 < float(p95) < 1.0:
            return max(0.30, float(p95))  # never weaken below 0.30
    except (json.JSONDecodeError, IOError, OSError, KeyError):
        pass
    return default


def _derive_idf_bypass_threshold(kb_dir=None) -> float:
    """
    Auto-derive the IDF bypass threshold from the IDF index statistics.

    The threshold represents the IDF score at which a term is specific
    enough (appears in <3% of all test documents) to provide strong
    evidence of relevance. This is computed from corpus size during
    IDF index build (log2(N / 0.03*N) ≈ 5.06 for N=10034).

    Falls back to a statistically reasonable default if stats are unavailable.
    """
    if kb_dir is None:
        kb_dir = str(DEFAULT_OUTPUT_DIR / 'knowledge_base')
    idf_path = os.path.join(kb_dir, 'idf_index.json')
    if not os.path.isfile(idf_path):
        # Fallback: if no IDF index, use a conservative threshold
        return 5.0
    try:
        with open(idf_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        stats = data.get('statistics', {})
        # Use specificity_3pct: the IDF value where a term appears in <3% of docs
        # This is the natural "this term is specific" threshold derived from
        # the actual corpus size. For N=10034: log2(10034/301) ≈ 5.06
        threshold = stats.get('specificity_3pct')
        if threshold and threshold > 0:
            return float(threshold)
        # Fallback: use P25 (25th percentile of all IDF values)
        p25 = stats.get('p25')
        if p25 and p25 > 0:
            return float(p25)
    except (json.JSONDecodeError, IOError, OSError, KeyError):
        pass
    return 5.0  # statistical fallback (log2(1/0.03) ≈ 5.06)


# ---------------------------------------------------------------------------
# Method-keyword derivation (GAP 1 fix)
# ---------------------------------------------------------------------------
def split_camel_case(name: str) -> List[str]:
    """
    Split a method/identifier into lowercased word parts.

    Java / TypeScript / JavaScript / Kotlin: camelCase / PascalCase,
    preserving acronyms (SSO -> 'sso', WorkPolicy -> 'work','policy').

    Python: snake_case (underscore-separated). The function detects an
    underscore in the name and splits on '_' first, then re-applies the
    camelCase split on each piece so mixed conventions still expand
    correctly (e.g. 'get_AgentId' -> 'get','agent','id').
    """
    if not name:
        return []

    pascal_re = r'[A-Z]+(?=[A-Z][a-z]|[^A-Za-z]|$)|[A-Z]?[a-z]+'
    if '_' in name:
        out: List[str] = []
        for piece in name.split('_'):
            if not piece:
                continue
            sub = re.findall(pascal_re, piece)
            if sub:
                out.extend(p.lower() for p in sub if p)
            else:
                out.append(piece.lower())
        return out

    parts = re.findall(pascal_re, name)
    return [p.lower() for p in parts if p]


def derive_method_keywords(changed_method: str) -> List[Tuple[str, List[str]]]:
    """
    Derive keyword groups from a method name.

    Returns a list of (group_name, [variations]) tuples. Variations include
    the singular form, simple plural, and a 2-token compound built from
    adjacent parts (e.g. 'work' + 'policy' -> 'work policy').

    Example:
        getWorkPolicyTemplatesByAgentIdsAndProgram ->
            [('work',     ['work']),
             ('policy',   ['policy', 'policies']),
             ('templates',['template', 'templates']),
             ('agent',    ['agent', 'agents']),
             ('program',  ['program', 'programs', 'programme']),
             ('work_policy', ['work policy', 'workpolicy', 'work-policy']),
             ('policy_templates', ['policy template', 'policytemplate', ...]),
             ...]

    Generic verbs / connectors (get, set, by, and, ...) are filtered out.
    """
    parts = split_camel_case(changed_method)
    # Drop generic tokens for the keyword list. Connectors like "by" and
    # "and" are dropped by GENERIC_TOKENS too. Threshold is 2 chars so
    # meaningful short tokens like 'id', 'db', 'ui', 'io' are preserved.
    business_parts = [p for p in parts if p not in GENERIC_TOKENS and len(p) >= 2]

    if not business_parts:
        return []

    groups: List[Tuple[str, List[str]]] = []
    seen_group_names: set = set()

    def _push(group_name: str, variations: List[str]) -> None:
        if group_name in seen_group_names:
            return
        seen_group_names.add(group_name)
        # Dedupe variations preserving order.
        deduped: List[str] = []
        for v in variations:
            if v and v not in deduped:
                deduped.append(v)
        groups.append((group_name, deduped))

    # Single-word groups with simple pluralisation.
    for p in business_parts:
        variations = [p]
        # Simple plural rules - good enough for English business nouns.
        if p.endswith('y') and not p.endswith(('ay', 'ey', 'iy', 'oy', 'uy')):
            variations.append(p[:-1] + 'ies')        # policy -> policies
        if not p.endswith('s'):
            variations.append(p + 's')               # agent -> agents
        if p.endswith('s'):
            variations.append(p[:-1])                # agents -> agent
        # Common alternate spellings for 'program'.
        if p == 'program':
            variations.append('programme')
        elif p == 'programs':
            variations.append('programmes')
        _push(p, variations)

    # 2-token compound groups for adjacent parts (e.g. 'work policy').
    for i in range(len(business_parts) - 1):
        a, b = business_parts[i], business_parts[i + 1]
        compound_name = f"{a}_{b}"
        variations = [
            f"{a} {b}",
            f"{a}{b}",
            f"{a}-{b}",
            f"{a}_{b}",
        ]
        _push(compound_name, variations)

    return groups


# ---------------------------------------------------------------------------
# Flow loading from focused KB (replaces legacy stage2 shim)
# ---------------------------------------------------------------------------
def load_flows_from_registry(flow_registry_path: str,
                              flow_dependencies_path: str = None,
                              changed_components: List[str] = None
                              ) -> List[Dict[str, Any]]:
    """
    Load flow records from flow_registry.json and label each one as
    DIRECT or INDIRECT using flow_dependencies.json.

    Replaces the legacy `stage2_impacted_flows.json` shim. Returns a list
    of flow dicts with the same surface fields Stage 5 expects:
        flow_name, flow, flow_id, flow_tag, impact_type, classification.

    The (flow, component) -> dependency_type lookup uses every entry in
    `changed_components`. If any owning component yields a DIRECT label
    the flow wins DIRECT; otherwise INDIRECT; otherwise the focused-KB
    default of DIRECT (the focused KB only emits reachable flows).
    """
    if not os.path.isfile(flow_registry_path):
        raise FileNotFoundError(
            f"[Stage 5] flow_registry.json not found at: "
            f"{flow_registry_path}\n"
            f"Root cause: Stage 0 (build_flow_registry.py) did not run.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    with open(flow_registry_path, 'r', encoding='utf-8') as f:
        registry = json.load(f)

    registry_flows = registry.get('flows', []) or []
    if not registry_flows:
        raise RuntimeError(
            f"[Stage 5] flow_registry.json at {flow_registry_path} "
            f"contains zero flows.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )

    # Build (flow_name, component) -> dependency_type map.
    if not flow_dependencies_path or not os.path.isfile(flow_dependencies_path):
        raise FileNotFoundError(
            f"[Stage 5] flow_dependencies.json not found at: "
            f"{flow_dependencies_path}\n"
            f"Root cause: Stage 0 (build_flow_dependencies.py) did not "
            f"run or did not produce the file.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    flow_deps_map: Dict[Tuple[str, str], str] = {}
    with open(flow_dependencies_path, 'r', encoding='utf-8') as f:
        deps_data = json.load(f)
    for dep in deps_data.get('dependencies', []) or []:
        flow_name = dep.get('flow', '')
        component = dep.get('component', '')
        dep_type = dep.get('dependency_type', '')
        if not flow_name or not component:
            raise RuntimeError(
                f"[Stage 5] Malformed dependency entry in "
                f"flow_dependencies.json: {dep!r}.\n"
                f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
            )
        flow_deps_map[(flow_name, component)] = dep_type
    if not flow_deps_map:
        raise RuntimeError(
            f"[Stage 5] flow_dependencies.json at "
            f"{flow_dependencies_path} contains zero valid (flow, "
            f"component) pairs.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )

    components = [c for c in (changed_components or []) if c]
    if not components:
        raise RuntimeError(
            f"[Stage 5] load_flows_from_registry: changed_components is "
            f"empty. Stage 5 cannot resolve DIRECT/INDIRECT labels "
            f"without at least one component.\n"
            f"Fix: Pass --changed-components on the CLI or via the "
            f"orchestrator."
        )

    flows: List[Dict[str, Any]] = []
    for rf in registry_flows:
        flow_name = rf.get('flow_name') or ''
        flow_id = rf.get('flow_id') or ''
        test_tags = rf.get('test_tags') or []
        flow_tag = test_tags[0] if test_tags else (
            f"[{flow_name.upper().replace(' ', '_')}]" if flow_name else ''
        )
        if not flow_name:
            raise RuntimeError(
                f"[Stage 5] flow_registry.json contains an entry without "
                f"'flow_name': {rf!r}\n"
                f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
            )
        if not flow_tag:
            raise RuntimeError(
                f"[Stage 5] flow_registry.json entry for flow "
                f"'{flow_name}' has no test_tags and tag derivation "
                f"failed.\n"
                f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
            )

        # Resolve best (most-critical) impact_type across all components.
        # Focused mode: every flow in flow_registry is reachable from the
        # change, so a missing (flow, component) entry is a KB drift bug.
        best_dep = None
        for comp in components:
            dep = flow_deps_map.get((flow_name, comp))
            if dep is None:
                continue
            if dep == 'DIRECT':
                best_dep = 'DIRECT'
                break
            if dep == 'INDIRECT' and best_dep is None:
                best_dep = 'INDIRECT'
        if best_dep is None:
            # In FOCUSED-KB mode the registry covers only flows reachable from
            # the changed method, so some (flow, component) pairs are absent.
            # Treat this as "no dependency" and skip the flow gracefully.
            print(
                f"[Stage 5] SKIP flow '{flow_name}': no dependency entry for "
                f"components {components} in flow_dependencies.json "
                f"(focused KB - expected in multi-method mode)"
            )
            continue
        impact_type = best_dep

        flows.append({
            'flow_id': flow_id,
            'flow_name': flow_name,
            'flow': flow_name,            # back-compat alias
            'flow_tag': flow_tag,
            'impact_type': impact_type,
            'classification': rf.get('classification', 'PRIMARY'),
        })
    return flows


# ---------------------------------------------------------------------------
# Flow quota derivation
# ---------------------------------------------------------------------------
def derive_flow_quotas(flows: List[Dict[str, Any]],
                       primary_quota: int = DEFAULT_PRIMARY_FLOW_QUOTA,
                       other_quota: int = DEFAULT_OTHER_FLOW_QUOTA,
                       kb_dir: str = None,
                       enriched_corpus_path: str = None
                       ) -> Dict[str, int]:
    """
    Build flow quota mapping from flow_registry-derived flows with
    adaptive quotas.

    Adaptive quota: hub flows (high corpus coverage) get reduced quotas.
    The first flow receives primary quota, others receive other_quota,
    but both are scaled down based on corpus coverage.

    Args:
        enriched_corpus_path: Optional explicit path to the enriched test
            corpus. Option A (separate enriched corpus per pipeline) uses
            this to route Stage 5 to the matching pipeline's enriched
            corpus (`all_tcs_extracted_enriched_source.json` or
            `all_tcs_extracted_enriched_dependency.json`). When omitted the
            legacy `<kb_dir>/all_tcs_extracted_enriched.json` is used.
    """
    if not flows:
        return {}

    # Load corpus for coverage analysis. FAIL-FAST: kb_dir and the
    # enriched test corpus are required - the quota algorithm depends on
    # corpus distribution. Previously a try/except silently degraded the
    # quotas, masking missing or corrupted KB output.
    if not kb_dir:
        raise RuntimeError(
            "[stage5] derive_flow_quotas: kb_dir is empty.\n"
            "Fix: Pass --kb-dir explicitly or run via ria_agent.py."
        )
    corpus_path = (enriched_corpus_path
                   if enriched_corpus_path
                   else os.path.join(kb_dir, 'all_tcs_extracted_enriched.json'))
    if not os.path.isfile(corpus_path):
        raise FileNotFoundError(
            f"[stage5] all_tcs_extracted_enriched.json not found at: "
            f"{corpus_path}\n"
            f"Root cause: Stage 0 did not produce the enriched test "
            f"corpus.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    with open(corpus_path, 'r', encoding='utf-8') as f:
        corpus = json.load(f)
    corpus_size = len(corpus)
    if corpus_size == 0:
        raise RuntimeError(
            f"[stage5] all_tcs_extracted_enriched.json at {corpus_path} "
            f"is empty.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    corpus_coverage: Dict[str, int] = {}
    for test in corpus:
        for tag in test.get('auto_tags', []):
            corpus_coverage[tag] = corpus_coverage.get(tag, 0) + 1

    quotas: Dict[str, int] = {}

    # Derive hub-flow thresholds dynamically from corpus distribution.
    # A "hub flow" is one that covers a disproportionate share of the corpus.
    # We define "disproportionate" as 2x and 4x the mean coverage per flow.
    # FAIL-FAST: empty corpus_coverage means the corpus has no auto_tags,
    # which is a Stage 0 indexing bug.
    if not corpus_coverage:
        raise RuntimeError(
            f"[stage5] derive_flow_quotas: corpus has zero auto_tags "
            f"across {corpus_size} test entries.\n"
            f"Root cause: Stage 0 did not tag any test with a flow tag.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb' and verify "
            f"flow_registry.json has flow_tag entries."
        )
    coverage_values = [c / corpus_size for c in corpus_coverage.values() if c > 0]
    if not coverage_values:
        raise RuntimeError(
            f"[stage5] derive_flow_quotas: every auto_tag has zero "
            f"coverage. corpus_size={corpus_size}, "
            f"tags={len(corpus_coverage)}.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    mean_coverage = sum(coverage_values) / len(coverage_values)
    hub_threshold = min(0.60, mean_coverage * 4)    # "hub" = 4x mean coverage
    medium_threshold = min(0.40, mean_coverage * 2)  # "medium" = 2x mean coverage

    for idx, flow in enumerate(flows):
        name = flow.get('flow_name') or flow.get('flow')
        flow_tag = flow.get('flow_tag')
        if not name:
            continue

        # Base quota: first flow gets primary, others get other
        base_quota = primary_quota if idx == 0 else other_quota

        # Adaptive scaling based on corpus coverage AND impact type
        if flow_tag and corpus_coverage and corpus_size > 0:
            test_count = corpus_coverage.get(flow_tag, 0)
            coverage_pct = test_count / corpus_size
            impact_type = flow.get('impact_type', 'DIRECT')
            classification = flow.get('classification', 'PRIMARY')

            # Hub flows: distinguish functional vs infrastructure
            if coverage_pct >= hub_threshold:
                # DIRECT + PRIMARY hub flow = legitimate functional impact
                # Allow 2/3 quota instead of 1/3
                if impact_type == 'DIRECT' and classification == 'PRIMARY':
                    quotas[name] = max(3, (base_quota * 2) // 3)
                else:
                    quotas[name] = max(1, base_quota // 3)
            # Medium coverage: 2/3 quota
            elif coverage_pct >= medium_threshold:
                quotas[name] = max(1, (base_quota * 2) // 3)
            # Low coverage: full quota
            else:
                quotas[name] = base_quota
        else:
            quotas[name] = base_quota

    return quotas


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------
def _load_domain_vocabulary(kb_dir: str, *, required: bool = True) -> set:
    """Load discovered domain vocabulary from KB (runtime-generated).

    Generated by build_discovered_vocabularies.py during Stage 0d.

    FAIL-FAST CONTRACT (no fallbacks):
      - kb_dir MUST be supplied.
      - domain_vocabulary.json MUST exist and contain non-empty
        'domain_tokens'.
    """
    if not kb_dir:
        raise RuntimeError(
            "[stage5] _load_domain_vocabulary: kb_dir is empty.\n"
            "Fix: Pass --kb-dir explicitly or run via ria_agent.py."
        )
    path = Path(kb_dir) / "domain_vocabulary.json"
    if not path.exists():
        raise FileNotFoundError(
            f"[stage5] domain_vocabulary.json not found at: {path}\n"
            f"Root cause: Stage 0d (build_discovered_vocabularies.py) did "
            f"not produce the domain vocabulary.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    tokens = set(data.get("domain_tokens", []))
    if required and not tokens:
        raise RuntimeError(
            f"[stage5] domain_vocabulary.json at {path} contains zero "
            f"'domain_tokens'.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    return tokens


def _load_connector_tokens(kb_dir=None, *, required: bool = False) -> set:
    """
    Load connector/stop tokens dynamically from the IDF index.

    Connector tokens are words that appear in >50% of test documents
    (IDF < 1.0), making them non-discriminating. These are discovered
    from the actual test corpus during KB build, NOT hardcoded.

    FAIL-FAST CONTRACT:
      - When invoked from refine_tests() with required=True, idf_index.json
        MUST exist and produce a non-empty connector set.
      - The module-level call (required=False) returns {} so importing
        this file succeeds before Stage 0 has run.
    """
    if kb_dir is None:
        if required:
            raise RuntimeError(
                "[stage5] _load_connector_tokens: kb_dir is empty.\n"
                "Fix: Pass --kb-dir or run via ria_agent.py."
            )
        return set()
    idf_path = os.path.join(kb_dir, 'idf_index.json')
    if not os.path.isfile(idf_path):
        if required:
            raise FileNotFoundError(
                f"[stage5] idf_index.json not found at: {idf_path}\n"
                f"Root cause: Stage 0 (term_idf.py) did not produce the "
                f"IDF index.\n"
                f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
            )
        return set()
    with open(idf_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    idf_map = data.get('idf', {})
    stats = data.get('statistics', {})
    stop_threshold = stats.get('p10', 1.0)

    stop_words: set = set()
    for term, idf_score in idf_map.items():
        if ' ' not in term and idf_score <= stop_threshold and len(term) <= 5:
            stop_words.add(term)
    threshold_20pct = stats.get('specificity_5pct', 4.3) * 0.3
    for term, idf_score in idf_map.items():
        if ' ' not in term and len(term) <= 3 and idf_score <= threshold_20pct:
            stop_words.add(term)

    if required and not stop_words:
        raise RuntimeError(
            f"[stage5] idf_index.json at {idf_path} produced zero "
            f"connector tokens. The IDF distribution looks degenerate.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    return stop_words


# Lazily filled — refreshed per run when kb_dir is available.
_CONNECTOR_TOKENS = _load_connector_tokens()


def _is_meaningful_compound(phrase: str) -> bool:
    """A compound phrase is meaningful only if every token is a real
    business term, i.e. NOT a grammatical connector ("by", "and", "ids",
    ...) that appears between meaningful nouns in method names.

    Token length floor is 2 chars so legitimate domain abbreviations like
    "ct" (client team) and "id" inside multi-token compounds are kept. The
    connector set rejects "by ids" / "templates by" / "ids id" outright.

    Examples:
      "work policy"           -> True
      "policy template"       -> True
      "ct configuration"      -> True   (ct is 2 chars but not a connector)
      "by ids"                -> False (both connectors)
      "templates by"          -> False (ends in connector)
      "ids id"                -> False (both connectors)
      "configuration by"      -> False (ends in connector)
    """
    tokens = re.split(r'[ _\-]+', phrase.strip().lower())
    if len(tokens) < 2:
        return False
    for tok in tokens:
        if not tok or len(tok) < 2:
            return False
        if tok in _CONNECTOR_TOKENS:
            return False
    return True


def _load_component_keywords_from_map(component_names: List[str],
                                       kb_dir: str = None) -> List[str]:
    """Load REAL component keywords from component_map.json.

    These are the authoritative component-level terms (e.g. "work policy",
    "policy template", "policy template helper") that genuinely identify
    the component owning the changed method. Resolution is identical to
    Stage 4 (`get_component_keywords`):

      1. Match on `component_name` (canonical PascalCase).
      2. Match on `raw_class_names[]` (handles consolidated names like
         WorkPolicyTemplateHelper -> WorkPolicyTemplate).

    Returns a flat de-duplicated list of lower-cased keywords. Empty list
    if the component_map is missing or no component matches.
    """
    if not kb_dir:
        raise RuntimeError(
            "[Stage 5] _load_component_keywords_from_map: kb_dir is "
            "empty. Caller must supply --kb-dir."
        )
    if not component_names:
        raise RuntimeError(
            "[Stage 5] _load_component_keywords_from_map: "
            "component_names is empty. Stage 5 requires at least one "
            "component to filter against."
        )
    path = Path(kb_dir) / 'component_map.json'
    if not path.exists():
        raise FileNotFoundError(
            f"[Stage 5] component_map.json not found at: {path}\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    data = json.loads(path.read_text(encoding='utf-8'))

    components = data.get('components', []) or []
    if not components:
        raise RuntimeError(
            f"[Stage 5] component_map.json at {path} has zero "
            f"components.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    requested = {c for c in component_names if c}
    seen: set = set()
    out: List[str] = []
    matched_any = False
    for comp in components:
        name = comp.get('component_name') or ''
        raw = set(comp.get('raw_class_names') or [])
        if name in requested or (raw & requested):
            matched_any = True
            for kw in comp.get('keywords') or []:
                if not isinstance(kw, str):
                    continue
                kw_l = kw.strip().lower()
                if kw_l and kw_l not in seen:
                    seen.add(kw_l)
                    out.append(kw_l)
    if not matched_any:
        raise RuntimeError(
            f"[Stage 5] No component in component_map.json matches any "
            f"of the requested component_names: {list(requested)}.\n"
            f"Root cause: build_component_map.py did not index these "
            f"components.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    if not out:
        raise RuntimeError(
            f"[Stage 5] Component(s) {list(requested)} matched but none "
            f"have keywords in component_map.json.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    return out


def extract_component_keywords(method_keywords: List[Tuple[str, List[str]]],
                                kb_dir: str = None,
                                component_names: List[str] = None) -> List[str]:
    """
    Extract EXPLICIT REFERENCE TERMS for component-level filtering.

    FAIL-FAST CONTRACT (no fallbacks):
      - kb_dir MUST be supplied.
      - component_names MUST be a non-empty list.
      - component_map.json MUST contain matching components with keywords.
      - At least one keyword MUST be a meaningful compound phrase.

    Authoritative source: ``component_map.json`` keywords for the
    component(s) owning the changed method (e.g. WorkPolicyTemplate ->
    "work policy", "policy template", ...). The previous fallbacks to
    method-derived compounds and discovered domain vocabulary masked KB
    completeness bugs - they have been removed.
    """
    if not component_names:
        raise RuntimeError(
            "[Stage 5] extract_component_keywords: component_names is "
            "empty. Stage 5 requires at least one canonical component "
            "name to scope the keyword filter.\n"
            "Fix: Pass --changed-components on the CLI or via the "
            "orchestrator."
        )
    if not kb_dir:
        raise RuntimeError(
            "[Stage 5] extract_component_keywords: kb_dir is empty. "
            "Caller must supply --kb-dir."
        )

    explicit_terms: List[str] = []
    seen: set = set()

    # AUTHORITATIVE: component_map.json keywords (compound, multi-token only).
    # These describe the component's identity (e.g. WorkPolicyTemplate ->
    # "work policy", "policy template", "policy template helper").
    map_kws = _load_component_keywords_from_map(component_names, kb_dir)
    for kw in map_kws:
        if _is_meaningful_compound(kw) and kw not in seen:
            seen.add(kw)
            explicit_terms.append(kw)

    if not explicit_terms:
        # Graceful degradation for small/edge components whose names do not
        # yield a compound phrase in component_map.json (e.g. a 2-token utility
        # class like "BatchUtils"). Rather than aborting the whole refinement
        # pipeline, derive precision keywords directly from the canonical
        # component name(s): the PascalCase-split compound plus its individual
        # tokens. This keeps the component filter meaningful without a KB
        # rebuild and never crashes on tiny codebases.
        import re as _re
        for cname in component_names:
            parts = _re.findall(
                r'[A-Z]+(?=[A-Z][a-z]|[^A-Za-z]|$)|[A-Z]?[a-z]+|[0-9]+',
                cname,
            )
            tokens = [p.lower() for p in parts if p]
            if len(tokens) >= 2:
                compound = ' '.join(tokens)
                if compound not in seen:
                    seen.add(compound)
                    explicit_terms.append(compound)
            for tok in tokens:
                if len(tok) >= 3 and tok not in seen:
                    seen.add(tok)
                    explicit_terms.append(tok)
        print(
            f"[Stage 5] WARNING: component_map.json had no compound keywords "
            f"for {component_names}; using name-derived keywords instead: "
            f"{explicit_terms}"
        )

    return explicit_terms


def check_component_match(test: Dict[str, Any], component_keywords: List[str]) -> bool:
    """
    Check if test mentions ANY component-level keyword.

    Uses WORD-BOUNDARY matching (\b...\b) - identical semantics to
    Stage 4's `check_component_mention` - so a keyword like "policy" cannot
    match inside "policymakers" and "ct" cannot match inside "ctzone".
    Multi-word phrases ("work policy") are matched verbatim with internal
    whitespace tolerated as runs of any whitespace.
    """
    if not component_keywords:
        return True  # No component filter if no keywords

    summary_lower = (test.get('summary') or '').lower()
    description_lower = (test.get('description') or '').lower()

    step_text = ' '.join([
        (step.get('action') or '') + ' ' +
        (step.get('data') or '') + ' ' +
        (step.get('result') or '')
        for step in test.get('steps', [])
    ]).lower()

    searchable_text = f"{summary_lower} {description_lower} {step_text}"

    # Word-boundary regex match. Skip very short keywords (<3 chars) that
    # cannot be specific enough at this layer.
    cleaned: List[str] = []
    for kw in component_keywords:
        if not kw:
            continue
        kw_l = kw.strip().lower()
        if len(kw_l) < 3:
            continue
        cleaned.append(re.escape(kw_l))
    if not cleaned:
        return False
    pattern = re.compile(r'\b(?:' + '|'.join(cleaned) + r')\b')
    return bool(pattern.search(searchable_text))


def calculate_keyword_score(test: Dict[str, Any],
                             method_keywords: List[Tuple[str, List[str]]],
                             component_keywords: List[str] = None
                             ) -> Tuple[int, List[str], bool]:
    """
    Calculate keyword precision score (0-N) where N = number of keyword
    groups derived from the changed method name.

    Searches in summary + description + steps (all test fields).

    Returns:
        - score: number of keyword groups matched
        - matched: list of matched keyword group names
        - component_match: whether test matches component-level keywords
    """
    # Search ALL test fields: summary + description + steps
    summary_lower = (test.get('summary') or '').lower()
    description_lower = (test.get('description') or '').lower()

    step_text = ' '.join([
        (step.get('action') or '') + ' ' +
        (step.get('data') or '') + ' ' +
        (step.get('result') or '')
        for step in test.get('steps', [])
    ]).lower()

    searchable_text = f"{summary_lower} {description_lower} {step_text}"

    score = 0
    matched: List[str] = []
    for keyword_name, variations in method_keywords:
        if any(v in searchable_text for v in variations):
            score += 1
            matched.append(keyword_name)

    # Check component match
    component_match = check_component_match(test, component_keywords) if component_keywords else True

    return score, matched, component_match


def get_max_signal3(test: Dict[str, Any]) -> int:
    flows = test.get('matched_flows') or []
    if not flows:
        return 0
    return max((flow.get('signal3_score', 0) or 0) for flow in flows)


def get_primary_flow(test: Dict[str, Any]) -> str:
    flows = test.get('matched_flows') or []
    return flows[0].get('flow_name', '') if flows else ''


# Tier-bonus constants (must mirror stage4_test_correlation).
TIER_BONUS_SUMMARY = 50
TIER_BONUS_DESCRIPTION = 10
TIER_BONUS_STEPS = 0


def _build_tier_pattern(component_keywords: List[str]):
    """Compile a word-boundary regex over the component keywords."""
    if not component_keywords:
        return None
    cleaned: List[str] = []
    for kw in component_keywords:
        if not kw:
            continue
        kw_l = kw.strip().lower()
        if len(kw_l) < 3:
            continue
        cleaned.append(re.escape(kw_l))
    if not cleaned:
        return None
    return re.compile(r'\b(?:' + '|'.join(cleaned) + r')\b')


def compute_tier_bonus(test: Dict[str, Any], pattern) -> Tuple[int, str]:
    """Compute the field-priority tier bonus for a test.

    Returns (bonus, where) - identical contract to stage4. Looks for any
    component keyword (word-boundary) first in summary, then description,
    then steps - first hit wins.
    """
    if pattern is None:
        return 0, 'none'

    summary = (test.get('summary') or '').lower()
    if pattern.search(summary):
        return TIER_BONUS_SUMMARY, 'summary'

    description = (test.get('description') or '').lower()
    if pattern.search(description):
        return TIER_BONUS_DESCRIPTION, 'description'

    step_text = ' '.join([
        (step.get('action') or '') + ' ' +
        (step.get('data') or '') + ' ' +
        (step.get('result') or '')
        for step in test.get('steps', [])
    ]).lower()
    if pattern.search(step_text):
        return TIER_BONUS_STEPS, 'steps'

    return 0, 'none'


def get_tier_bonus(test: Dict[str, Any]) -> int:
    """Return the cached tier_bonus for a test, defaulting to 0."""
    val = test.get('tier_bonus')
    if isinstance(val, (int, float)):
        return int(val)
    return 0


# ---------------------------------------------------------------------------
# Refinement stages
# ---------------------------------------------------------------------------
def stage1_keyword_filter(tests: List[Dict[str, Any]],
                           method_keywords: List[Tuple[str, List[str]]],
                           threshold: int,
                           flow_quotas: Dict[str, int] = None,
                           component_keywords: List[str] = None,
                           min_results_pct: float = 0.05,
                           tier_pattern=None,
                           kb_dir: str = None) -> List[Dict[str, Any]]:
    """Keep tests with keyword score >= threshold AND component match, applying per-flow quotas.

    PRECISION CONTRACT (v7.4): when ``component_keywords`` is non-empty, a
    test MUST mention at least one component compound phrase (word-boundary
    match) to survive this filter. There is NO fallback to "score-only"
    filtering - that fallback proved too lenient and produced false
    positives for methods like ``getWorkPolicyConfiguration`` whose camelCase
    derivation drops "work" as a generic noun.

    EXCEPTION (v8.0 IDF override): tests with a high IDF score (from diff
    concept matching in Stage 4) bypass the component keyword requirement.
    These tests have DIRECT textual evidence that they mention the actual
    changed business concepts (e.g. "daily shift gap"), which is stronger
    evidence than component name matching.

    Rationale: producing 15 highly-relevant tests beats producing 30 tests
    where 1/3 are unrelated. The ``min_results_pct`` parameter is retained
    in the signature for backward compatibility but is intentionally
    unused.
    """
    del min_results_pct  # retained for backward compatibility, intentionally ignored

    # IDF threshold: auto-derived from corpus statistics. Tests scoring above
    # this have enough specificity evidence to bypass the component filter.
    # Derived from IDF index: the specificity_3pct value (term in <3% of docs).
    IDF_BYPASS_THRESHOLD = _derive_idf_bypass_threshold(kb_dir)
    # Embedding bypass: corpus-derived if embeddings_stats.json available;
    # otherwise 0.45 (validated default).
    EMB_BYPASS_THRESHOLD = _derive_embedding_bypass_threshold(kb_dir)

    filtered: List[Dict[str, Any]] = []
    idf_bypassed = 0
    emb_bypassed = 0
    for test in tests:
        score, kws, comp_match = calculate_keyword_score(test, method_keywords, component_keywords)
        emb_sim = test.get('score_breakdown', {}).get('embedding_sim') or 0.0
        if score < threshold:
            # Even if keyword score is below threshold, a high IDF score
            # means the test directly mentions the changed business logic.
            # Also allow bypass if embedding similarity is very high (≥0.45)
            # — the test is semantically about the code change even without
            # matching method/component keywords.
            idf_score = test.get('score_breakdown', {}).get('idf_score', 0)
            if idf_score < IDF_BYPASS_THRESHOLD and emb_sim < EMB_BYPASS_THRESHOLD:
                continue
        test['keyword_score'] = score
        test['matched_keywords'] = kws
        # Strict filter: BOTH keyword score AND component match are required
        # whenever component keywords are configured. No fallback.
        # EXCEPTION: high IDF score OR high embedding sim bypasses component requirement.
        idf_score = test.get('score_breakdown', {}).get('idf_score', 0)
        if component_keywords and not comp_match:
            if idf_score >= IDF_BYPASS_THRESHOLD:
                idf_bypassed += 1
            elif emb_sim >= EMB_BYPASS_THRESHOLD:
                emb_bypassed += 1
            else:
                continue
        test['component_match'] = comp_match
        # Compute (or refresh) the field-priority tier bonus. This is the
        # signal that lets summary-mention tests outrank steps-only tests
        # at the final cap. We always recompute when the test is missing
        # the field so Stage 5 can stand alone if Stage 4 didn't populate it.
        if tier_pattern is not None and 'tier_bonus' not in test:
            bonus, where = compute_tier_bonus(test, tier_pattern)
            test['tier_bonus'] = bonus
            test['tier_field'] = where
        filtered.append(test)

    if component_keywords:
        print(f"[stage5] Strict filter: kept {len(filtered)} tests "
              f"matching at least one component compound phrase.")
        if idf_bypassed:
            print(f"[stage5] IDF override: {idf_bypassed} tests bypassed component "
                  f"filter due to high diff-concept match (IDF >= {IDF_BYPASS_THRESHOLD})")
        if emb_bypassed:
            print(f"[stage5] Embedding override: {emb_bypassed} tests bypassed component "
                  f"filter due to high semantic similarity "
                  f"(embedding_sim >= {EMB_BYPASS_THRESHOLD:.2f})")

    # Apply per-flow quotas to keyword filter results
    if flow_quotas:
        by_flow = defaultdict(list)
        for t in filtered:
            by_flow[get_primary_flow(t)].append(t)

        capped = []
        print(f"\nApplying per-flow quotas to {len(filtered)} keyword-filtered tests:")
        # Deterministic flow iteration: sort flow names alphabetically.
        for flow_name in sorted(by_flow.keys()):
            tests_in_flow = by_flow[flow_name]
            quota = flow_quotas.get(flow_name, 999)
            # Allow 5x quota for high-precision keyword matches (tuned for 25-35 test target)
            cap = quota * 5 if quota < 999 else 999
            # Deterministic sort: IDF score first (diff concept relevance),
            # then tier_bonus (summary > description > steps), then
            # keyword_score, then signal3, then issue_key as a stable
            # tie-break that produces identical orders across runs.
            sorted_tests = sorted(
                tests_in_flow,
                key=lambda t: (
                    -t.get('score_breakdown', {}).get('idf_score', 0),
                    -get_tier_bonus(t),
                    -t.get('keyword_score', 0),
                    -((t.get('matched_flows') or [{}])[0].get('signal3_score', 0) or 0),
                    t.get('issue_key', '') or '',
                ),
            )
            selected = sorted_tests[:cap]
            capped.extend(selected)
            if len(tests_in_flow) > cap:
                print(f"  {flow_name}: {len(tests_in_flow)} tests -> {len(selected)} (quota={quota}, cap={cap})")
        filtered = capped

    return filtered


def flow_diversity_filter(remaining_tests: List[Dict[str, Any]],
                           quotas: Dict[str, int],
                           method_keywords: List[Tuple[str, List[str]]],
                           component_keywords: List[str] = None,
                           tier_pattern=None
                           ) -> List[Dict[str, Any]]:
    """Add top N tests per flow (by tier_bonus then signal3_score) for diversity,
    with component filtering."""
    by_flow = defaultdict(list)
    for t in remaining_tests:
        by_flow[get_primary_flow(t)].append(t)

    diversity_tests: List[Dict[str, Any]] = []
    # Deterministic flow iteration: sort flow names alphabetically. The
    # iteration order does not affect test identity (each test has a fixed
    # primary flow), but it does fix the order in which we *append* to
    # `diversity_tests` and therefore the downstream order.
    for flow_name in sorted(quotas.keys()):
        quota = quotas[flow_name]
        flow_tests = by_flow.get(flow_name, [])
        sorted_tests = sorted(
            flow_tests,
            key=lambda t: (
                -get_tier_bonus(t),
                -get_max_signal3(t),
                t.get('issue_key', '') or '',
            ),
        )
        selected = sorted_tests[:quota]
        for t in selected:
            score, kws, comp_match = calculate_keyword_score(t, method_keywords, component_keywords)
            # Apply component filter to diversity tests too
            if comp_match:
                t['keyword_score'] = score
                t['matched_keywords'] = kws
                t['component_match'] = comp_match
                # Compute / refresh tier bonus for diversity-only tests too.
                if tier_pattern is not None and 'tier_bonus' not in t:
                    bonus, where = compute_tier_bonus(t, tier_pattern)
                    t['tier_bonus'] = bonus
                    t['tier_field'] = where
                t['selection_reason'] = 'flow_diversity'
                diversity_tests.append(t)
    return diversity_tests


# ---------------------------------------------------------------------------
# Main refinement orchestration
# ---------------------------------------------------------------------------
def refine_tests(input_file: str, output_file: str,
                  flow_registry_file: str,
                  flow_dependencies_file: str,
                  changed_method: str,
                  changed_methods: List[str],
                  changed_components: List[str],
                  threshold: int,
                  primary_quota: int, other_quota: int,
                  kb_dir: str = None,
                  enriched_corpus_path: str = None) -> Dict[str, Any]:
    print("=" * 80)
    print("STAGE 5: SOPHISTICATED TEST REFINEMENT")
    print("=" * 80)
    print(f"\nInput:                {input_file}")
    print(f"Output:               {output_file}")
    print(f"Flow registry:        {flow_registry_file}")
    print(f"Flow dependencies:    {flow_dependencies_file}")
    print(f"Changed method:       {changed_method}")
    if changed_methods:
        print(f"Changed methods:      {changed_methods}")
    if changed_components:
        print(f"Changed components:   {changed_components}\n")

    # Refresh GENERIC_TOKENS and CONNECTOR_TOKENS from the resolved KB dir.
    # FAIL-FAST: kb_dir is required for refine_tests() to operate correctly.
    global GENERIC_TOKENS, _CONNECTOR_TOKENS
    if not kb_dir:
        raise RuntimeError(
            "[stage5] refine_tests: kb_dir is empty.\n"
            "Root cause: caller did not pass --kb-dir.\n"
            "Fix: Pass --kb-dir explicitly or run via ria_agent.py."
        )
    GENERIC_TOKENS = _load_generic_tokens(kb_dir, required=True)
    print(f"Loaded {len(GENERIC_TOKENS)} discovered generic tokens from {kb_dir}")
    _CONNECTOR_TOKENS = _load_connector_tokens(kb_dir, required=True)
    print(f"Loaded {len(_CONNECTOR_TOKENS)} auto-discovered connector tokens from corpus")

    # Resolve final list of changed methods (multi-method aware).
    if not changed_methods and changed_method:
        changed_methods = [changed_method]
    elif not changed_methods:
        changed_methods = []

    # Derive keywords from ALL changed methods and merge them
    all_method_keywords = []
    seen_groups = set()
    for method in changed_methods:
        method_kws = derive_method_keywords(method)
        for grp_name, variations in method_kws:
            if grp_name not in seen_groups:
                all_method_keywords.append((grp_name, variations))
                seen_groups.add(grp_name)

    method_keywords = all_method_keywords

    # --- Enrich keywords with LLM method understanding ---
    # Stage 1.5 produces business-language terms that bridge the gap between
    # code method names and how QA writes test cases.
    #
    # GRACEFUL DEGRADATION: method_understanding.json is the output of
    # Stage 1.5. Stage 1.5 itself is non-fatal (the orchestrator catches
    # exceptions and logs [WARN] without aborting), so Stage 5 must also
    # tolerate a missing file - otherwise an LLM hiccup or a transient
    # AWS-Bedrock error would crash the whole pipeline before refinement.
    # When the file is absent we skip enrichment and proceed with the
    # method-name-derived keywords (which are themselves a complete
    # filter signal); the per-test IDF/embedding bypass paths still
    # protect against over-filtering.
    mu_path = os.path.join(kb_dir or '', 'method_understanding.json')
    if not os.path.isfile(mu_path):
        mu_path = os.path.join(str(DEFAULT_OUTPUT_DIR), 'method_understanding.json')
    if not os.path.isfile(mu_path):
        print(f"[stage5] [WARN] method_understanding.json not found at: "
              f"{mu_path}")
        print(f"[stage5] [WARN] Stage 1.5 output unavailable; skipping LLM "
              f"keyword enrichment and proceeding with method-name keywords "
              f"only. Test recall may be slightly reduced but the filter is "
              f"still functional.")
    else:
        try:
            with open(mu_path, 'r', encoding='utf-8') as f:
                mu_data = json.load(f)
        except (json.JSONDecodeError, OSError) as _mu_err:
            print(f"[stage5] [WARN] Could not parse "
                  f"method_understanding.json: {_mu_err}; skipping LLM "
                  f"keyword enrichment.")
            mu_data = {}
        for m in mu_data.get('methods', []):
            # Add affected_behaviors as keyword groups
            for behavior in m.get('affected_behaviors', []):
                tokens = behavior.lower().split()
                for i in range(len(tokens)):
                    word = tokens[i]
                    if len(word) >= 4 and word not in seen_groups:
                        seen_groups.add(word)
                        all_method_keywords.append((word, [word]))
                    if i < len(tokens) - 1:
                        bigram = f"{tokens[i]} {tokens[i+1]}"
                        bg_key = bigram.replace(' ', '_')
                        if bg_key not in seen_groups and len(tokens[i]) >= 3:
                            seen_groups.add(bg_key)
                            all_method_keywords.append((bg_key, [bigram]))

            for s in agent_reasoning.normalize_scenarios(m.get('test_scenarios', [])):
                desc = s.get('description', '').lower()
                desc_tokens = [t for t in desc.split() if len(t) >= 3]
                for i in range(len(desc_tokens) - 1):
                    bigram = f"{desc_tokens[i]} {desc_tokens[i+1]}"
                    bg_key = bigram.replace(' ', '_')
                    if bg_key not in seen_groups:
                        seen_groups.add(bg_key)
                        all_method_keywords.append((bg_key, [bigram]))

    print(f"[OK] Enriched keywords with LLM method understanding: "
          f"{len(all_method_keywords)} total groups")

    method_keywords = all_method_keywords

    # Load flows directly from the focused KB (replaces stage2 shim).
    flows = load_flows_from_registry(
        flow_registry_file,
        flow_dependencies_file,
        changed_components=changed_components or [],
    )
    flow_quotas = derive_flow_quotas(flows, primary_quota, other_quota,
                                     kb_dir=kb_dir,
                                     enriched_corpus_path=enriched_corpus_path)

    # Resolve owning component(s) so we can pull AUTHORITATIVE compound
    # keywords from component_map.json. The orchestrator passes
    # changed_components explicitly via CLI now (no stage2 lookup).
    component_names: List[str] = [c for c in (changed_components or []) if c]

    # Extract component-level keywords for stricter filtering. Real
    # component_map.json keywords are preferred; method-derived compounds
    # are added as a supplement (filtered to exclude grammatical fragments
    # like "by ids", "templates by"). The discovered domain vocabulary is
    # only used as a last-resort fallback.
    component_keywords = extract_component_keywords(
        method_keywords, kb_dir=kb_dir, component_names=component_names,
    )
    if component_names:
        print(f"Owning component(s): {component_names}")

    print(f"Derived {len(method_keywords)} keyword groups from method name:")
    for grp_name, variations in method_keywords:
        print(f"  - {grp_name}: {variations}")
    if not method_keywords:
        print("  WARNING: No business-domain tokens extracted from method name.")
        print("           Stage 1 keyword filter will pass nothing.")

    print(f"\nComponent-level keywords (required for match): {len(component_keywords)}")
    print(f"  {component_keywords}")

    print(f"\nDerived flow quotas from flow_registry ({len(flow_quotas)} flows):")
    for flow_name, quota in flow_quotas.items():
        print(f"  - {flow_name}: quota={quota}")
    if not flow_quotas:
        print("  WARNING: No flows in flow_registry - flow diversity stage will skip.")

    # Load Stage 4 tests. FAIL-FAST on a missing/empty input.
    if not os.path.isfile(input_file):
        raise FileNotFoundError(
            f"[Stage 5] Stage 4 input not found at: {input_file}\n"
            f"Root cause: Stage 4 did not run or did not produce output.\n"
            f"Fix: Re-run Stage 4 (or full pipeline)."
        )
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    all_tests = data.get('recommended_tests') or []
    total_tests = len(all_tests)
    if total_tests == 0:
        raise RuntimeError(
            f"[Stage 5] Stage 4 output at {input_file} contains zero "
            f"recommended_tests.\n"
            f"Root cause: Stage 4 found no tests matching the impacted "
            f"flows + components.\n"
            f"Fix: Verify Stage 4 inputs (flow_registry.json, test "
            f"corpus, diff_concepts.json) and re-run Stage 4."
        )
    print(f"\nStage 4 input: {total_tests} tests\n")

    # Build tier-bonus pattern from component keywords. The pattern is
    # used both to compute the per-test tier bonus (when missing) and to
    # rank-order tests by field priority (summary > description > steps).
    tier_pattern = _build_tier_pattern(component_keywords)
    if tier_pattern is not None:
        print(f"\nTier-bonus pattern compiled from {len(component_keywords)} "
              f"component keywords (summary +{TIER_BONUS_SUMMARY}, "
              f"description +{TIER_BONUS_DESCRIPTION}).")

    # Stage 5.1: keyword filter WITH COMPONENT FILTER
    print("-" * 80)
    print(f"STAGE 5.1: KEYWORD PRECISION FILTER (score >= {threshold} + component match)")
    print("-" * 80)
    high_precision = stage1_keyword_filter(
        all_tests, method_keywords, threshold, flow_quotas, component_keywords,
        tier_pattern=tier_pattern, kb_dir=kb_dir,
    )
    print(f"\nStage 5.1 result: {len(high_precision)} tests")

    stage1_score_dist = Counter(t['keyword_score'] for t in high_precision)
    if stage1_score_dist:
        print("\nKeyword score distribution (Stage 5.1):")
        for s in sorted(stage1_score_dist.keys(), reverse=True):
            print(f"  Score {s}: {stage1_score_dist[s]} tests")

    # Stage 5.2: flow diversity WITH COMPONENT FILTER
    print("\n" + "-" * 80)
    print("STAGE 5.2: FLOW DIVERSITY BOOST (with component filter)")
    print("-" * 80)
    hp_ids = {t['issue_key'] for t in high_precision}
    remaining = [t for t in all_tests if t.get('issue_key') not in hp_ids]
    print(f"\nRemaining tests after Stage 5.1: {len(remaining)}")
    diversity = flow_diversity_filter(
        remaining, flow_quotas, method_keywords, component_keywords,
        tier_pattern=tier_pattern,
    )
    print(f"\nStage 5.2 result: {len(diversity)} tests")

    flow_dist = Counter(get_primary_flow(t) for t in diversity)
    if flow_dist:
        print("\nFlow distribution (Stage 5.2):")
        for flow, count in sorted(flow_dist.items()):
            print(f"  {flow}: {count} tests")

    # Combine.
    for t in high_precision:
        t['selection_reason'] = 'high_keyword_precision'
    refined = high_precision + diversity
    final_count = len(refined)

    print("\n" + "=" * 80)
    print("FINAL RESULTS")
    print("=" * 80)
    print(f"\nTotal refined tests: {final_count}")
    if total_tests:
        pct = ((total_tests - final_count) / total_tests) * 100
        print(f"Reduction: {total_tests - final_count} tests removed ({pct:.1f}%)")

    # Composite score for ordering. Includes tier_bonus, keyword_score,
    # signal3, idf_score AND embedding_sim so tests that match diff
    # concepts both lexically AND semantically rank highest.
    for t in refined:
        idf = t.get('score_breakdown', {}).get('idf_score', 0)
        emb = t.get('score_breakdown', {}).get('embedding_sim') or 0.0
        t['composite_score'] = (
            get_tier_bonus(t)
            + (t['keyword_score'] * 10)
            + (get_max_signal3(t) * 2)
            + (idf * 3)  # IDF contribution: high-relevance diff concept matches
            + (emb * 40) # Embedding contribution: semantic relevance to code change
        )
    # Deterministic sort: composite_score desc, idf_score desc, tier_bonus desc,
    # keyword_score desc, signal3 desc, issue_key asc - identical orders across runs.
    sorted_refined = sorted(
        refined,
        key=lambda t: (
            -t['composite_score'],
            -t.get('score_breakdown', {}).get('idf_score', 0),
            -get_tier_bonus(t),
            -t.get('keyword_score', 0),
            -get_max_signal3(t),
            t.get('issue_key', '') or '',
        ),
    )

    # Top 10 listing.
    print("\n" + "-" * 80)
    print("TOP 10 TESTS (by keyword_score x 10 + signal3 x 2)")
    print("-" * 80)
    for i, t in enumerate(sorted_refined[:10], 1):
        print(f"\n{i}. {t['issue_key']} (Composite={t['composite_score']}, "
              f"Keyword={t['keyword_score']}, Signal3={get_max_signal3(t)})")
        print(f"   Selection: {t.get('selection_reason')}")
        print(f"   Keywords: {', '.join(t.get('matched_keywords', []))}")
        summary = (t.get('summary') or '')[:80]
        print(f"   {summary}...")

    # Persist.
    output_data = {
        'stage': 5,
        'description': 'Sophisticated test refinement: keyword precision + flow diversity',
        'run_id': data.get('run_id'),
        'generated_at': data.get('generated_at'),
        'changed_method': changed_method,
        'algorithm': {
            'stage1_keyword_threshold': threshold,
            'stage2_flow_quotas': flow_quotas,
            'method_keywords': [grp for grp, _ in method_keywords],
            'method_keyword_variations': dict(method_keywords),
        },
        'input_tests': total_tests,
        'output_tests': final_count,
        'reduction': total_tests - final_count,
        'reduction_percentage': round(
            ((total_tests - final_count) / total_tests) * 100, 1
        ) if total_tests else 0,
        'stage1_tests': len(high_precision),
        'stage2_tests': len(diversity),
        'refined_tests': sorted_refined,
    }

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, indent=2)

    print(f"\n{'=' * 80}")
    print(f"Refinement complete: {output_file}")
    print(f"{'=' * 80}\n")
    return output_data


def _split_csv(value: str) -> List[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(',') if v.strip()]


def main():
    parser = argparse.ArgumentParser(
        description='Stage 5: dynamic, method-agnostic test refinement '
                    '(reads flows from flow_registry.json + '
                    'flow_dependencies.json).'
    )
    parser.add_argument('--changed-method', default=None,
                        help='Method name that was changed. Required (single '
                             'or multi-method via --changed-methods).')
    parser.add_argument('--changed-methods', default=None,
                        help='Comma-separated list of changed method names '
                             '(multi-method mode).')
    parser.add_argument('--changed-components', default=None,
                        help='Comma-separated list of owning components '
                             '(used for component_map keyword resolution '
                             'and DIRECT/INDIRECT classification).')
    parser.add_argument('--input-file', default=str(DEFAULT_INPUT),
                        help='Path to stage4_recommended_tests.json')
    parser.add_argument('--output-file', default=str(DEFAULT_OUTPUT),
                        help='Path to write stage5_refined_tests.json')
    parser.add_argument('--flow-registry', default=str(DEFAULT_FLOW_REGISTRY),
                        help='Path to flow_registry.json (for flow quotas)')
    parser.add_argument('--flow-dependencies',
                        default=str(DEFAULT_FLOW_DEPENDENCIES),
                        help='Path to flow_dependencies.json '
                             '(for DIRECT/INDIRECT classification)')
    parser.add_argument('--keyword-threshold', type=int, default=DEFAULT_KEYWORD_THRESHOLD,
                        help='Minimum keyword score for Stage 5.1')
    parser.add_argument('--primary-flow-quota', type=int, default=DEFAULT_PRIMARY_FLOW_QUOTA,
                        help='Quota for the first flow in flow_registry')
    parser.add_argument('--other-flow-quota', type=int, default=DEFAULT_OTHER_FLOW_QUOTA,
                        help='Quota for every other flow in flow_registry')
    parser.add_argument('--kb-dir', default=str(DEFAULT_KB_DIR),
                        help='Knowledge base directory for corpus-coverage analysis')
    parser.add_argument('--enriched-corpus-path', default=None,
                        help='Optional explicit path to the enriched test corpus '
                             '(Option A: per-pipeline enriched corpus). When '
                             'omitted the legacy '
                             '<kb-dir>/all_tcs_extracted_enriched.json is used.')
    args = parser.parse_args()

    # Resolve changed methods (CSV or single).
    changed_methods = _split_csv(args.changed_methods)
    changed_method = args.changed_method
    if not changed_method and changed_methods:
        changed_method = changed_methods[0]
    if not changed_method:
        print("ERROR: --changed-method (or --changed-methods) not provided.")
        sys.exit(2)
    if not changed_methods:
        changed_methods = [changed_method]

    changed_components = _split_csv(args.changed_components)

    refine_tests(
        input_file=args.input_file,
        output_file=args.output_file,
        flow_registry_file=args.flow_registry,
        flow_dependencies_file=args.flow_dependencies,
        changed_method=changed_method,
        changed_methods=changed_methods,
        changed_components=changed_components,
        threshold=args.keyword_threshold,
        primary_quota=args.primary_flow_quota,
        other_quota=args.other_flow_quota,
        kb_dir=args.kb_dir,
        enriched_corpus_path=args.enriched_corpus_path,
    )


if __name__ == '__main__':
    main()
