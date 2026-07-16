#!/usr/bin/env python3
"""
Stage 4: Test Correlation with 2-Layer Matching (No Xray Dependency)

Applies the 2-layer scoring algorithm. Priority multipliers are NOT used
(no Xray dependency); the per-flow 3-signal score is retained for
diagnostics ONLY (not added to total_score).

  Layer 1: Flow Match    (0 pts - redundant after tag filter at line 726)
  Layer 2: Component Match (40 pts)
  Base Score = Layer 1 + Layer 2 = 40 (flow-lane tests)

  Note: Flow match points removed because Step 6 (tag filter) already
  proves the flow match. No need to reward it again in scoring.

Criticality is derived from impact_type ONLY:
  DIRECT   -> CRITICAL
  INDIRECT -> HIGH
  Other    -> MEDIUM

Output schema:
  - matched_flows[] (list of objects with flow_id, flow_name, match_score,
                     signal3_score)
  - total_score (base 40 + IDF boost for flow-lane tests)
  - criticality (CRITICAL/HIGH/MEDIUM)

Stage 4 reads flow_registry.json (focused mode) + flow_dependencies.json
DIRECTLY from the Knowledge Base. There are no longer any Stage 1/2/3
trace-and-match scripts: flow_registry (focused mode) already contains the
impacted flows for the changed code, and flow_dependencies provides the
per-(flow, component) DIRECT/INDIRECT label used for criticality.

CLI:
    --flow-registry      <path/to/flow_registry.json>   (REQUIRED)
    --changed-file       <changed file path>            (used to derive
                                                         component when
                                                         --changed-component
                                                         is not supplied)
    --changed-component  <component name>               (single-method mode)
    --changed-components <comma list>                   (multi-method mode)

Input:
  - flow_registry.json (focused) + flow_dependencies.json
  - enriched test corpus
Output: stage4_recommended_tests.json
"""

import argparse
import json
import os
import sys
import re
import time
from concurrent.futures import ProcessPoolExecutor

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from configs.ria_config import (
    RIA_OUTPUT_DIR, TC_DATA_PATH,
    FLOW_MATCH_POINTS, COMPONENT_MATCH_POINTS,
    INCLUSION_THRESHOLD as _CONFIG_INCLUSION_THRESHOLD,
)
INCLUSION_THRESHOLD = _CONFIG_INCLUSION_THRESHOLD


def _compute_adaptive_inclusion_threshold(scores_list, default=None):
    """Pick an inclusion threshold from the actual score distribution.

    Algorithm:
      - If we have <20 scores, fall back to the configured default
        (insufficient data for percentile estimation).
      - Otherwise compute the 25th percentile of NON-ZERO scores. If
        that percentile is below the configured floor we keep the
        floor (so we never silently weaken the gate); otherwise we
        use the percentile as a corpus-aware threshold.
    """
    if default is None:
        default = INCLUSION_THRESHOLD
    nonzero = sorted(s for s in scores_list if s and s > 0)
    if len(nonzero) < 20:
        return default
    p25 = nonzero[max(0, int(len(nonzero) * 0.25) - 1)]
    return max(default, p25)

# IDF scoring integration (loaded lazily)
_idf_index = None
_diff_phrases = None
_embeddings_index = None   # pre-computed test embeddings (ndarray + key map)
_diff_embedding = None     # single embedding vector for all diff phrases

# Global context for SECF (Semantic Entity Coherence Filtering)
# DISABLED (2026-06-06): _SECF_CHANGED_FILE = None


def _load_idf_and_concepts(kb_dir, diff_concepts_path):
    """Load IDF index, embedding index and diff concepts for scoring."""
    global _idf_index, _diff_phrases, _embeddings_index, _diff_embedding

    # Reset for each run_stage4 call
    _idf_index = None
    _diff_phrases = None
    _embeddings_index = None
    _diff_embedding = None

    # Load IDF index
    idf_path = os.path.join(kb_dir, 'idf_index.json') if kb_dir else None
    if idf_path and os.path.isfile(idf_path):
        try:
            from term_idf import load_idf_index
            _idf_index = load_idf_index(idf_path)
            print(f"[Stage4] Loaded IDF index: {len(_idf_index.get('idf', {}))} terms")
        except Exception as e:
            print(f"[Stage4] WARNING: Could not load IDF index: {e}")
            _idf_index = {}
    else:
        _idf_index = {}

    # Load embedding index (Layer 5 — semantic similarity)
    emb_path = os.path.join(kb_dir, 'embeddings_index.npz') if kb_dir else None
    if emb_path and os.path.isfile(emb_path):
        try:
            from build_embeddings import load_embeddings_index
            _embeddings_index = load_embeddings_index(emb_path)
            n_tests = len(_embeddings_index.get('test_keys', []))
            print(f"[Stage4] Loaded embedding index: {n_tests} test vectors")
        except Exception as e:
            print(f"[Stage4] WARNING: Could not load embedding index: {e}")
            _embeddings_index = None
    else:
        _embeddings_index = None

    # Load diff concepts
    if diff_concepts_path and os.path.isfile(diff_concepts_path):
        try:
            with open(diff_concepts_path, 'r', encoding='utf-8') as f:
                concepts_data = json.load(f)
            _diff_phrases = concepts_data.get('all_phrases', [])
            print(f"[Stage4] Loaded diff concepts: {len(_diff_phrases)} phrases")
        except Exception as e:
            print(f"[Stage4] WARNING: Could not load diff concepts: {e}")
            _diff_phrases = []
    else:
        _diff_phrases = []

    # Pre-compute a single embedding for ALL diff phrases (done once)
    if _embeddings_index and _diff_phrases:
        try:
            from build_embeddings import embed_text
            combined_diff_text = ' '.join(_diff_phrases)
            _diff_embedding = embed_text(combined_diff_text)
            print(f"[Stage4] Computed diff embedding ({len(_diff_phrases)} phrases → 1 vector)")
        except Exception as e:
            print(f"[Stage4] WARNING: Could not compute diff embedding: {e}")
            _diff_embedding = None


# ProcessPool workers: globals populated by initializer once per worker
_W_IDF_INDEX = None
_W_DIFF_PHRASES = None
_W_EMB_INDEX = None
_W_DIFF_EMB = None


def _stage4_worker_init(idf_index, diff_phrases, emb_index, diff_emb):
    """Called once per worker process to load read-only globals."""
    global _W_IDF_INDEX, _W_DIFF_PHRASES, _W_EMB_INDEX, _W_DIFF_EMB
    _W_IDF_INDEX = idf_index
    _W_DIFF_PHRASES = diff_phrases
    _W_EMB_INDEX = emb_index
    _W_DIFF_EMB = diff_emb


def _stage4_worker_score(test):
    """Worker function: score one test using process-local globals."""
    global _idf_index, _diff_phrases, _embeddings_index, _diff_embedding
    _idf_index = _W_IDF_INDEX
    _diff_phrases = _W_DIFF_PHRASES
    _embeddings_index = _W_EMB_INDEX
    _diff_embedding = _W_DIFF_EMB
    return _compute_idf_score(test)


def _compute_idf_score(test):
    """
    Compute IDF-weighted score for a test based on diff concept matches.

    Returns (score, details, embedding_sim):
      - score >= 0. Higher = more diff-derived phrases matched.
      - details: list of per-phrase match info.
      - embedding_sim: cosine similarity (0–1) between diff text and
        this test's pre-computed embedding, or None if unavailable.

    PRECISION RULES (to avoid false positives):
      - Effective match weight: each matched phrase contributes
        (word_count - 1) to the weight.  A 4-gram counts as 3, a trigram
        as 2, a bigram as 1.  This means a single highly-specific 4-gram
        match ("min daily shift gap") is as strong as three bigram matches.
      - A trigram (3+ words) is already fairly specific, so when present
        the minimum effective weight drops to 2; otherwise it stays at 3.
      - Without any trigram, we need at least 3 effective weight points
        (e.g. three distinct bigrams) to accept the match.
      - If neither condition is met, score is 0 (no IDF credit).
    """
    if not _idf_index or not _diff_phrases:
        return 0.0, [], None

    try:
        from term_idf import score_phrases_against_test

        # Compute embedding similarity for this test (Layer 5)
        embedding_sim = None
        if _embeddings_index is not None and _diff_embedding is not None:
            key = test.get('issue_key')
            key_to_idx = _embeddings_index.get('key_to_idx', {})
            if key and key in key_to_idx:
                from build_embeddings import semantic_similarity
                idx = key_to_idx[key]
                test_emb = _embeddings_index['embeddings'][idx:idx+1]
                sims = semantic_similarity(_diff_embedding, test_emb)
                embedding_sim = float(sims[0])

        # UPGRADE 1: weight matched phrases with BM25 (Okapi) instead of
        # raw IDF.  BM25 adds TF saturation (a rare phrase appearing 10x
        # is not 10x more relevant — diminishing returns) and document
        # length normalisation (long tests don't get a free boost from
        # text volume).  See term_idf.compute_bm25_score for the formula.
        score, details = score_phrases_against_test(
            _diff_phrases, test, _idf_index,
            embedding_sim=embedding_sim, use_bm25=True)

        # Auto-derived precision gate: adapt to diff size.
        # Embedding-only matches bypass the lexical precision gate —
        # they already passed their own cosine threshold (≥0.40).
        is_embedding_only = (len(details) == 1
                             and details[0].get('match_layer') == 'embedding')
        if is_embedding_only:
            return score, details, embedding_sim

        # Effective weight counts phrase specificity: a 4-gram ("min daily
        # shift gap") contributes 3, a trigram 2, a bigram 1.  This avoids
        # rejecting a test that has one very specific long-phrase match
        # just because the overlap detector collapsed sub-phrases into it.
        lexical_details = [d for d in details
                           if d.get('match_layer') != 'embedding']
        effective_weight = sum(len(d['phrase'].split()) - 1
                               for d in lexical_details)
        has_trigram = any(len(d['phrase'].split()) >= 3
                          for d in lexical_details)

        # A trigram is already fairly specific evidence, so lower the bar.
        # Without a trigram we need stronger cumulative evidence (3+).
        min_effective = 2 if has_trigram else 3

        if effective_weight < min_effective:
            return 0.0, [], embedding_sim

        # ------------------------------------------------------------------
        # UPGRADE 2: Semantic Entity Coherence Filtering (SECF)
        # STATUS: TEMPORARILY DISABLED (2026-06-06)
        # REASON: Bugs identified, pending fix (see RIA_SECF_SESSION_CONTEXT.md)
        # TO RE-ENABLE: Uncomment lines below
        # ------------------------------------------------------------------
        # try:
        #     from entity_coherence_filter import compute_entity_coherence
        #
        #     # Get changed file from global context (set by caller)
        #     changed_file = globals().get('_SECF_CHANGED_FILE', '')
        #
        #     if changed_file and score > 0:
        #         coherence = compute_entity_coherence(changed_file, test)
        #
        #         # Threshold: 0.30 (empirically validated on EEM corpus)
        #         if coherence < 0.30:
        #             # Entity mismatch - reject as false positive
        #             return 0.0, [], embedding_sim
        # except Exception as e:
        #     # Fail-safe: if SECF filter errors, continue without it
        #     # (prefer false positives over pipeline crash)
        #     pass

        # Without any trigram, the match relies on common short phrases —
        # require enough of them (already enforced by min_effective=3).
        if not has_trigram and effective_weight < 3:
            return 0.0, [], embedding_sim

        return score, details, embedding_sim
    except Exception as e:
        # FAIL-FAST: previously this swallowed every exception so a single
        # malformed test silently zeroed out IDF scoring for the whole
        # corpus. Surface the offending test so the data issue is visible.
        raise RuntimeError(
            f"[Stage 4] _compute_idf_score failed for test "
            f"{test.get('issue_key', '<unknown>')!r}: {e}\n"
            f"Root cause: malformed test record, corrupt IDF index, or "
            f"embedding lookup error.\n"
            f"Fix: validate the enriched corpus and rebuild the KB."
        ) from e


def extract_flow_tags(test):
    """
    Extract flow tags from enriched test.

    Uses auto_tags field if available (from Stage 0a enrichment).
    Falls back to extracting [FLOW] tags from text if auto_tags not present.
    """
    if 'auto_tags' in test and test['auto_tags']:
        return test['auto_tags']

    step_text = ' '.join([
        (step.get('action') or '') + ' ' +
        (step.get('data') or '') + ' ' +
        (step.get('result') or '')
        for step in test.get('steps', [])
    ])

    combined_text = ' '.join([
        test.get('summary') or '',
        test.get('description') or '',
        step_text
    ])

    tags = re.findall(r'\[([^\]]+)\]', combined_text)
    return [f"[{tag.upper().strip()}]" for tag in tags]


# Adaptive minimum keyword length: 3 chars for single tokens, but allow
# 2-char abbreviations when the keyword is a multi-word phrase (since
# multi-word phrases are inherently specific). Domain abbreviations like
# "ct" (client team), "id" (identifier), "db" (database) often legitimately
# appear in 2-token compounds like "ct configuration".
_MIN_SINGLE_KW_LEN = 3
_MIN_COMPOUND_TOKEN_LEN = 2


def _accepts_keyword(kw_lower):
    """Return True if a keyword passes the length floor.

    Multi-word phrases are accepted as long as every token is >= 2 chars,
    so legitimate "ct configuration" / "db schema" survive while pure
    single-character noise ("a", "i") is filtered out.
    """
    if not kw_lower:
        return False
    if ' ' in kw_lower:
        for tok in kw_lower.split():
            if len(tok) < _MIN_COMPOUND_TOKEN_LEN:
                return False
        return True
    return len(kw_lower) >= _MIN_SINGLE_KW_LEN


def check_component_mention(test, component_keywords):
    """
    Check if test mentions ANY component keyword.

    Uses word-boundary matching, NOT raw substring matching, to avoid
    false positives like "work" substring-matching "workforcemanager",
    "workflow", "workplan" etc. Aligns with
    build_flow_dependencies.check_keyword_match_in_tests which already
    uses \\b boundaries.

    Length policy (data-driven, not hardcoded English):
      - Single tokens: must be >= 3 chars (avoid "a", "is", etc.).
      - Multi-word phrases: each token must be >= 2 chars so legitimate
        domain abbreviations like "ct" / "id" inside "ct configuration"
        survive.
    """
    step_text = ' '.join([
        (step.get('action') or '') + ' ' +
        (step.get('data') or '') + ' ' +
        (step.get('result') or '')
        for step in test.get('steps', [])
    ])

    searchable_text = ' '.join([
        test.get('summary') or '',
        test.get('description') or '',
        step_text
    ]).lower()

    # Build a single combined regex of all valid keywords (word-boundary
    # delimited). This is both faster and consistent with Stage 0c.
    cleaned = []
    for kw in component_keywords or []:
        if not kw:
            continue
        kw_lower = kw.strip().lower()
        if not _accepts_keyword(kw_lower):
            continue
        cleaned.append(re.escape(kw_lower))

    if not cleaned:
        return False

    pattern = re.compile(r'\b(?:' + '|'.join(cleaned) + r')\b')
    return bool(pattern.search(searchable_text))


# Tier-bonus constants (production-ready field-priority scoring).
# Tests that mention component keywords in the highest-signal field
# (summary) outrank tests that mention them only in description, which in
# turn outrank tests that mention them only in the steps.
TIER_BONUS_SUMMARY = 50
TIER_BONUS_DESCRIPTION = 10
TIER_BONUS_STEPS = 0


def _build_keyword_pattern(component_keywords):
    """Build a compiled word-boundary regex over the supplied keywords.

    Returns None when no usable keywords are supplied (matches behaviour
    of `check_component_mention`). Length policy mirrors
    `_accepts_keyword`.
    """
    cleaned = []
    for kw in component_keywords or []:
        if not kw:
            continue
        kw_lower = kw.strip().lower()
        if not _accepts_keyword(kw_lower):
            continue
        cleaned.append(re.escape(kw_lower))
    if not cleaned:
        return None
    return re.compile(r'\b(?:' + '|'.join(cleaned) + r')\b')


def compute_tier_bonus(test, component_keywords, pattern=None):
    """Compute the field-priority tier bonus for a test.

    Returns (bonus, where) where `where` is one of 'summary', 'description',
    'steps', or 'none'. Looks for any component keyword (word-boundary)
    first in summary, then description, then steps - first hit wins.

    The pattern argument lets callers compile the regex once outside the
    per-test loop.
    """
    if pattern is None:
        pattern = _build_keyword_pattern(component_keywords)
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


def assign_criticality(impact_type):
    """
    Assign criticality based on impact type ONLY (no priority).
    impact_type determines criticality.

    Test Priority (P1/P2/P3) is IGNORED - No Xray dependency!
        DIRECT   -> CRITICAL
        INDIRECT -> HIGH
        Other    -> MEDIUM
    """
    if impact_type == "DIRECT":
        return "CRITICAL"
    elif impact_type == "INDIRECT":
        return "HIGH"
    else:
        return "MEDIUM"


def assign_criticality_from_deps(flow_name, changed_components,
                                  flow_deps_map, fallback_impact_type=None):
    """
    Assign criticality using the (flow, changed_component) lookup table.

    FAIL-FAST CONTRACT (no fallbacks):
        - flow_deps_map MUST be non-empty (flow_dependencies.json exists
          and was loaded successfully).
        - changed_components MUST be non-empty (at least one canonical
          component name resolved).
        - At least one (flow_name, component) pair MUST exist in the map.
        Any of these violations indicates a Knowledge Base problem; we
        raise RuntimeError so the bug surfaces immediately instead of
        being masked with a default DIRECT/CRITICAL label.

    Args:
        flow_name: The matched flow name (string).
        changed_components: list of changed component names. Must be
            non-empty.
        flow_deps_map: dict {(flow_name, component_name): dependency_type}
            built from flow_dependencies.json. Must be non-empty.
        fallback_impact_type: kept for backward-compat in the call sites
            but NO LONGER USED for fallback decisions. Pass-through only.

    Returns:
        Tuple (criticality, dependency_type, source) where source is
        always "flow_deps".
    """
    if not flow_deps_map:
        raise RuntimeError(
            f"[Stage 4] flow_dependencies.json missing/empty while resolving "
            f"flow '{flow_name}'. Cannot assign criticality without (flow, "
            f"component) dependency map.\n"
            f"Root cause: Stage 0 (build_flow_dependencies.py) did not "
            f"produce flow_dependencies.json, or the file is empty.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb' to regenerate "
            f"the knowledge base."
        )
    if not changed_components:
        raise RuntimeError(
            f"[Stage 4] changed_components is empty while resolving flow "
            f"'{flow_name}'.\n"
            f"Root cause: Upstream stage did not provide a canonical "
            f"component name for the change (component_map.json missing "
            f"the component, or stem-derivation produced an empty string).\n"
            f"Fix: Verify component_map.json contains the changed file's "
            f"component, then run 'python3 ria_agent.py --rebuild-kb'."
        )

    # Rank: DIRECT (0) is most critical, INDIRECT (1), other (2).
    rank_map = {"DIRECT": 0, "INDIRECT": 1}
    best_rank = 99
    best_dep = None

    for comp in changed_components:
        if not comp:
            raise RuntimeError(
                f"[Stage 4] changed_components contains an empty/None entry "
                f"for flow '{flow_name}'. Components must all be canonical "
                f"non-empty strings."
            )
        dep = flow_deps_map.get((flow_name, comp))
        if dep is None:
            continue
        r = rank_map.get(dep, 2)
        if r < best_rank:
            best_rank = r
            best_dep = dep

    if best_dep is None:
        raise RuntimeError(
            f"[Stage 4] flow '{flow_name}' has no dependency entry for any "
            f"of components: {changed_components}.\n"
            f"This means flow_dependencies.json is incomplete - the flow "
            f"appears in flow_registry but Stage 0 did not record any "
            f"DIRECT/INDIRECT relationship for these components.\n"
            f"Root cause: build_flow_dependencies.py did not index the "
            f"changed components, or the components were filtered out.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb' to regenerate "
            f"the knowledge base. If the problem persists, inspect "
            f"build_flow_dependencies.py for component-skipping filters."
        )

    return (assign_criticality(best_dep), best_dep, "flow_deps")


def derive_component_from_file(changed_file, component_map_path=None):
    """
    Derive the canonical component name from the changed file path.

    FAIL-FAST CONTRACT (no fallbacks):
      1. `changed_file` MUST be a non-empty string.
      2. Filename stem (after stripping the language extension) MUST be
         non-empty.
      3. `component_map_path` MUST point to an existing component_map.json.
      4. The stem MUST resolve to a canonical component_name via
         component_map.json (either as a direct match or via
         raw_class_names[]).
      Any violation raises RuntimeError so KB issues surface immediately
      instead of silently degrading to a filename-stem guess that may not
      exist in flow_dependencies.json.
    """
    if not changed_file:
        raise RuntimeError(
            "[Stage 4] derive_component_from_file: changed_file is empty.\n"
            "Root cause: Caller did not supply --changed-file or the "
            "detected change set is empty.\n"
            "Fix: Re-run with an explicit --changed-file or verify "
            "git diff produced changes."
        )

    # ---- Step 1: filename stem (drop directory + known extensions) -----
    # Extensions discovered from the active language profile; falls back to
    # the legacy hard list if the profile lookup fails (offline tests, etc.)
    base = os.path.basename(changed_file)
    stem = base
    try:
        from configs.ria_config import get_active_profile
        _exts = tuple(get_active_profile().get('source_extensions') or ())
    except Exception:
        _exts = ()
    if not _exts:
        _exts = ('.java', '.kt', '.py', '.ts', '.tsx', '.js', '.jsx', '.mjs')
    for ext in _exts:
        if stem.lower().endswith(ext):
            stem = stem[:-len(ext)]
            break
    if not stem:
        raise RuntimeError(
            f"[Stage 4] derive_component_from_file: filename stem is empty "
            f"after stripping extensions for '{changed_file}'.\n"
            f"Root cause: File has no recognizable source extension or "
            f"is empty.\n"
            f"Fix: Verify '{changed_file}' is a real source file."
        )

    # ---- Step 2: REQUIRE component_map for canonical name -------------
    if not component_map_path or not os.path.isfile(component_map_path):
        raise FileNotFoundError(
            f"[Stage 4] component_map.json not found at: "
            f"{component_map_path}\n"
            f"Root cause: Stage 0 (build_component_map.py) did not "
            f"produce the component map.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    with open(component_map_path, 'r', encoding='utf-8') as f:
        component_map = json.load(f)
    components = component_map.get('components', []) or []
    if not components:
        raise RuntimeError(
            f"[Stage 4] component_map.json at {component_map_path} "
            f"contains zero components.\n"
            f"Root cause: build_component_map.py produced an empty map.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    for component in components:
        comp_name = component.get('component_name', '') or ''
        if comp_name == stem:
            return comp_name
        raw_classes = component.get('raw_class_names') or []
        if stem in raw_classes:
            return comp_name or stem

    # No match found in component_map - this is a KB completeness bug.
    raise RuntimeError(
        f"[Stage 4] No component in component_map.json matches stem "
        f"'{stem}' (from changed file '{changed_file}').\n"
        f"Root cause: build_component_map.py did not index this file's "
        f"class, or the file's class name does not match its filename "
        f"(pre-consolidation).\n"
        f"Fix: Run 'python3 ria_agent.py --rebuild-kb'. If the problem "
        f"persists, inspect build_component_map.py for class-skipping "
        f"filters."
    )


def _load_component_name_map(kb_dir):
    """
    Build a normalization map: raw_class_name -> canonical component_name.

    FAIL-FAST CONTRACT (no fallbacks):
      - kb_dir MUST be provided.
      - component_map.json MUST exist and be valid JSON.
      - At least one component MUST be present.

    Resolution rules:
      1. Each canonical component_name maps to itself (idempotent).
      2. Each entry in raw_class_names[] maps to its canonical component_name.

    Returns:
        Dict[str, str] mapping any known raw or canonical name to its
        canonical component_name.
    """
    if not kb_dir:
        raise RuntimeError(
            "[Stage 4] _load_component_name_map: kb_dir is empty.\n"
            "Fix: Pass --kb-dir explicitly or run via ria_agent.py."
        )
    component_map_path = os.path.join(kb_dir, "component_map.json")
    if not os.path.isfile(component_map_path):
        raise FileNotFoundError(
            f"[Stage 4] component_map.json not found at: "
            f"{component_map_path}\n"
            f"Root cause: Stage 0 (build_component_map.py) did not run "
            f"or did not write the file.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    with open(component_map_path, 'r', encoding='utf-8') as f:
        component_map = json.load(f)
    components = component_map.get('components', []) or []
    if not components:
        raise RuntimeError(
            f"[Stage 4] component_map.json at {component_map_path} "
            f"contains zero components.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    mapping: dict = {}
    for component in components:
        comp_name = component.get('component_name') or ''
        if not comp_name:
            raise RuntimeError(
                f"[Stage 4] component_map.json contains an entry without "
                f"'component_name': {component!r}.\n"
                f"Fix: Re-run 'python3 ria_agent.py --rebuild-kb'."
            )
        # Identity mapping (canonical -> canonical) so callers can
        # blindly normalize without checking whether a name is
        # already canonical.
        mapping[comp_name] = comp_name
        for raw in component.get('raw_class_names') or []:
            if raw and isinstance(raw, str):
                # Do NOT overwrite an existing canonical mapping —
                # if a canonical name happens to also appear in some
                # other component's raw_class_names, the canonical
                # wins (preserves identity invariant).
                mapping.setdefault(raw, comp_name)
    if not mapping:
        raise RuntimeError(
            f"[Stage 4] component_map.json at {component_map_path} "
            f"produced an empty normalization map.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    return mapping


def _normalize_component_name(name, name_map):
    """
    Normalize a (possibly raw) component name to its canonical form
    using the raw_class_name -> component_name map produced by
    _load_component_name_map().

    FAIL-FAST CONTRACT (no fallbacks):
      - name MUST be non-empty.
      - name_map MUST be non-empty (component_map.json was loaded).
      - name MUST appear in the map either as a canonical name or a raw
        class name. An unknown name is a KB completeness bug.

    Returns:
        The canonical component_name.
    """
    if not name:
        raise RuntimeError(
            "[Stage 4] _normalize_component_name: name is empty.\n"
            "Fix: Caller must pass a non-empty component name."
        )
    if not name_map:
        raise RuntimeError(
            "[Stage 4] _normalize_component_name: name_map is empty.\n"
            "Root cause: _load_component_name_map() returned an empty "
            "map, indicating component_map.json is missing or empty.\n"
            "Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    if name not in name_map:
        raise RuntimeError(
            f"[Stage 4] Component '{name}' not in component_map.json "
            f"(neither canonical nor raw class name).\n"
            f"Root cause: build_component_map.py did not index the class "
            f"or it was filtered out.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'. If the "
            f"problem persists, inspect build_component_map.py."
        )
    return name_map[name]


def get_component_keywords(changed_component, kb_dir):
    """
    Get component keywords from component_map.json.

    FAIL-FAST CONTRACT (no fallbacks):
      - changed_component MUST be a non-empty string.
      - kb_dir MUST be supplied and component_map.json MUST exist.
      - The component MUST resolve via canonical name OR raw_class_names.

    Resolution order (post-component consolidation):
      1. Exact match on `component_name` (canonical PascalCase base name).
      2. Match on `raw_class_names[]` (the original per-file class names
         that were merged into a base component).
    Any miss raises RuntimeError so KB drift surfaces immediately.
    """
    if not changed_component:
        raise RuntimeError(
            "[Stage 4] get_component_keywords: changed_component is empty.\n"
            "Fix: Caller must pass a non-empty component name."
        )
    if not kb_dir:
        raise RuntimeError(
            "[Stage 4] get_component_keywords: kb_dir is empty.\n"
            "Fix: Pass --kb-dir explicitly or run via ria_agent.py."
        )
    component_map_path = os.path.join(kb_dir, "component_map.json")
    if not os.path.isfile(component_map_path):
        raise FileNotFoundError(
            f"[Stage 4] component_map.json not found at: "
            f"{component_map_path}\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    with open(component_map_path, 'r', encoding='utf-8') as f:
        component_map = json.load(f)

    components = component_map.get('components', []) or []
    if not components:
        raise RuntimeError(
            f"[Stage 4] component_map.json at {component_map_path} "
            f"contains zero components.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )

    # Tier 1: canonical match
    for component in components:
        if component.get('component_name', '') == changed_component:
            keywords = component.get('keywords', []) or []
            if not keywords:
                raise RuntimeError(
                    f"[Stage 4] Component '{changed_component}' has no "
                    f"keywords in component_map.json.\n"
                    f"Root cause: build_component_map.py did not extract "
                    f"keywords for this component.\n"
                    f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
                )
            return keywords

    # Tier 2: raw_class_names match (handles WorkPolicyTemplateHelper
    # -> WorkPolicyTemplate consolidation transparently).
    for component in components:
        raw_classes = component.get('raw_class_names') or []
        if changed_component in raw_classes:
            keywords = component.get('keywords', []) or []
            if not keywords:
                raise RuntimeError(
                    f"[Stage 4] Component matching raw class "
                    f"'{changed_component}' has no keywords in "
                    f"component_map.json.\n"
                    f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
                )
            return keywords

    # No match anywhere - this is a KB completeness bug.
    raise RuntimeError(
        f"[Stage 4] Component '{changed_component}' not found in "
        f"component_map.json (neither as canonical component_name nor in "
        f"raw_class_names).\n"
        f"Root cause: build_component_map.py did not index this class.\n"
        f"Fix: Run 'python3 ria_agent.py --rebuild-kb'. If the problem "
        f"persists, inspect build_component_map.py for filters that may "
        f"exclude this class."
    )


def _signal3_score_for_flow(test, flow_tag):
    """
    Get the per-test 3-signal score for a given flow tag from the enriched
    corpus. Returns 0 if absent.

    Stage 0a enrichment writes test['flow_scores'] = {flow_tag: int}.
    """
    flow_scores = test.get('flow_scores') or {}
    if not isinstance(flow_scores, dict):
        return 0
    raw = flow_scores.get(flow_tag, 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def run_stage4(flow_registry_path, test_corpus_path,
               kb_dir, output_dir,
               changed_component=None, changed_components=None,
               changed_file=None,
               run_id=None,
               diff_concepts_path=None, full_corpus_path=None,
               flow_deps_path=None):
    """
    Run Stage 4: Test correlation with 2-layer matching + IDF-weighted
    diff scoring.

    Inputs:
      - flow_registry_path: focused flow_registry.json (REQUIRED). Every
        flow in the registry is impacted by the change; DIRECT/INDIRECT
        labels are resolved per (flow, component) via
        flow_dependencies.json (loaded from kb_dir).
      - changed_components / changed_component: explicit component name(s)
        whose (flow, component) DIRECT/INDIRECT mapping should be looked
        up in flow_dependencies.json. Falls back to the canonical
        component derived from `changed_file`.
      - test_corpus_path: enriched test corpus (auto_tags + flow_scores).
      - full_corpus_path (optional): COMPLETE test corpus
        (e.g. all_tcs_extracted.json). When supplied, a second
        "keyword lane" IDF-scans that full corpus to catch tests that
        are relevant by content even if they are not on a discovered
        flow.
    """
    print(f"\n{'=' * 80}")
    print(f"STAGE 4: Test Correlation (2-Layer Matching + IDF Diff Scoring)")
    print(f"{'=' * 80}")
    print(f"Flow registry:    {flow_registry_path}")
    print(f"Test corpus:      {test_corpus_path}")
    print(f"Full corpus:      {full_corpus_path or '(same as test corpus)'}")
    print(f"Changed component: {changed_component or '(none - skip component filter)'}")
    print(f"Changed file:     {changed_file or '(not provided)'}")
    print(f"Diff concepts:    {diff_concepts_path or '(none)'}")

    # Load IDF index and diff concepts for scoring
    _load_idf_and_concepts(kb_dir, diff_concepts_path)

    # Set global context for SECF filter
    # DISABLED (2026-06-06): global _SECF_CHANGED_FILE
    # DISABLED (2026-06-06): _SECF_CHANGED_FILE = changed_file or ''

    # Read flow_registry directly. Every flow in the registry is impacted
    # (the focused KB rebuild in Step 0b already filtered to flows reachable
    # from the change).
    if not flow_registry_path or not os.path.isfile(flow_registry_path):
        print(f"\nERROR: flow_registry.json not found: {flow_registry_path}")
        print("       Stage 4 cannot proceed without an impacted-flows source.")
        raise FileNotFoundError(flow_registry_path)
    with open(flow_registry_path, 'r', encoding='utf-8') as f:
        flow_registry_data = json.load(f)
    registry_flows = flow_registry_data.get('flows', []) or []
    if not registry_flows:
        print(f"\nERROR: flow_registry.json is empty (no flows). "
              f"Stage 4 cannot proceed.")
        print(f"       File: {flow_registry_path}")
        raise ValueError("flow_registry.json contains no flows")

    # Load flow_dependencies.json for (flow, component) -> DIRECT/INDIRECT
    # mapping. This map drives criticality assignment: rather than using a
    # flow-level impact_type (which paints the whole flow with a single
    # label), we look up the EXACT (flow, changed_component) pair so the
    # same flow can produce DIRECT criticality for one changed component
    # and INDIRECT criticality for another.
    #
    # Two paths to choose the dependency map:
    #   1. Caller-supplied `flow_deps_path` argument (used by the dependency
    #      pipeline so its `flow_dependencies_dependency.json` is loaded
    #      instead of the source pipeline's canonical file).
    #   2. Environment variable `RIA_FLOW_DEPS_OVERRIDE` (back-compat hook).
    #   3. Default: `<kb_dir>/flow_dependencies.json` (source pipeline).
    if not flow_deps_path:
        flow_deps_path = os.environ.get('RIA_FLOW_DEPS_OVERRIDE') or None
    if not flow_deps_path:
        flow_deps_path = os.path.join(kb_dir, 'flow_dependencies.json')
    print(f"Flow dependencies: {flow_deps_path}")
    if not os.path.isfile(flow_deps_path):
        raise FileNotFoundError(
            f"[Stage 4] flow_dependencies.json not found at: {flow_deps_path}\n"
            f"Root cause: Stage 0 (build_flow_dependencies.py) did not "
            f"produce the dependency map.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb' to regenerate "
            f"the knowledge base."
        )
    flow_deps_map = {}
    with open(flow_deps_path, 'r', encoding='utf-8') as f:
        flow_deps_data = json.load(f)
    # Build lookup: {(flow_name, component_name): dependency_type}
    for dep in flow_deps_data.get('dependencies', []):
        flow_name = dep.get('flow', '')
        component = dep.get('component', '')
        dep_type = dep.get('dependency_type', '')
        if not flow_name or not component:
            raise RuntimeError(
                f"[Stage 4] Malformed dependency entry in "
                f"flow_dependencies.json: {dep!r}.\n"
                f"Every dependency must have non-empty 'flow' and "
                f"'component' fields.\n"
                f"Fix: Re-run 'python3 ria_agent.py --rebuild-kb'."
            )
        flow_deps_map[(flow_name, component)] = dep_type
    if not flow_deps_map:
        raise RuntimeError(
            f"[Stage 4] flow_dependencies.json contains zero valid "
            f"(flow, component) pairs.\n"
            f"Path: {flow_deps_path}\n"
            f"Root cause: build_flow_dependencies.py produced an empty "
            f"dependency list.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb'."
        )
    print(f"Loaded flow_dependencies.json: {len(flow_deps_map)} "
          f"(flow, component) pairs")

    # Get component keywords. Two filtering paths:
    #   - filter_keywords: required-mention list (component filter is ON
    #     when --changed-component is supplied; OFF for multi-method mode).
    #   - tier_keywords: keywords used to compute the field-priority tier
    #     bonus. ALWAYS populated (multi-method falls back to the explicit
    #     `changed_components` list). The tier bonus does NOT exclude tests;
    #     it only re-ranks them so summary-mention tests beat steps-only
    #     tests at the final cap.
    if changed_component:
        component_keywords = get_component_keywords(changed_component, kb_dir)
        print(f"Component keywords (filter): {len(component_keywords)}")
    else:
        component_keywords = []
        print(f"Component keywords (filter): 0 (component filtering disabled)")

    # Build the list of changed components for both tier bonus and
    # flow_deps lookup. Resolution order:
    #   1. explicit `changed_component` arg (single-method mode)
    #   2. explicit `changed_components` arg (multi-method consolidated)
    #   3. derive from `changed_file` via component_map.json
    if changed_component:
        resolved_components = [changed_component]
    elif changed_components and isinstance(changed_components, list):
        resolved_components = [c for c in changed_components if c]
    elif changed_file:
        comp_map_path = os.path.join(kb_dir or '', 'component_map.json')
        derived = derive_component_from_file(changed_file, comp_map_path)
        resolved_components = [derived] if derived else []
    else:
        resolved_components = []

    # ------------------------------------------------------------------
    # ROOT-CAUSE FIX: Normalize raw class names -> canonical component
    # names BEFORE any flow_deps_map lookup.
    #
    # flow_dependencies.json is keyed by canonical (consolidated) names
    # such as "WorkPolicyTemplate", but upstream callers (stages 0-3,
    # CLI args, derive_component_from_file's stem fallback) may pass raw
    # per-file class names like "WorkPolicyTemplateHelper" that were
    # merged into the canonical component during consolidation. Without
    # this normalization, every (flow, raw_name) lookup misses, every
    # test falls into the DIRECT/CRITICAL fallback path, and downstream
    # stages mark the entire recommendation set as CRITICAL.
    #
    # The normalization is idempotent (canonical -> canonical) and
    # gracefully degrades when component_map.json is unavailable
    # (returns the input verbatim, preserving legacy behaviour).
    # Order is preserved and duplicates produced by collapse are
    # de-duplicated to keep downstream loops fast and deterministic.
    # ------------------------------------------------------------------
    _component_name_map = _load_component_name_map(kb_dir)
    if _component_name_map and resolved_components:
        _seen_canonical: set = set()
        _normalized: list = []
        for _raw in resolved_components:
            _canonical = _normalize_component_name(_raw, _component_name_map)
            if not _canonical:
                continue
            if _canonical in _seen_canonical:
                continue
            _seen_canonical.add(_canonical)
            _normalized.append(_canonical)
            if _canonical != _raw:
                print(f"[Stage4] Normalized component name: "
                      f"{_raw!r} -> {_canonical!r}")
        resolved_components = _normalized

    # Build tier keywords from resolved components
    tier_keywords: list = []
    seen_tier_kws: set = set()
    for comp in resolved_components:
        for kw in get_component_keywords(comp, kb_dir):
            if not isinstance(kw, str):
                continue
            kw_l = kw.strip().lower()
            if kw_l and kw_l not in seen_tier_kws:
                seen_tier_kws.add(kw_l)
                tier_keywords.append(kw_l)
    tier_pattern = _build_keyword_pattern(tier_keywords)
    print(f"Tier-bonus keywords: {len(tier_keywords)} "
          f"(components: {resolved_components or 'none'})")
    print(f"Changed components for flow_deps lookup: "
          f"{resolved_components or 'none'}")

    # ------------------------------------------------------------------
    # Build the list of impacted flows from flow_registry.
    #
    # Every flow in flow_registry.json is impacted by definition (the
    # focused KB rebuild already filtered to flows reachable from the
    # change). The DIRECT vs INDIRECT label is resolved per
    # (flow, changed_component) via flow_deps_map. When the map has no
    # entry, we fall back to "DIRECT" because focused mode only emits
    # reachable flows.
    # ------------------------------------------------------------------
    all_flows: list = []
    for rf in registry_flows:
        flow_name = rf.get('flow_name') or ''
        flow_id = rf.get('flow_id') or ''
        test_tags = rf.get('test_tags') or []
        flow_tag = test_tags[0] if test_tags else (
            f"[{flow_name.upper().replace(' ', '_')}]" if flow_name else ''
        )
        if not flow_tag:
            continue
        # Determine impact_type from flow_dependencies (per changed
        # component). Use the BEST (most-critical) label across all
        # changed components for this flow.
        best_dep = None
        last_missing_comp = None
        for comp in resolved_components:
            if not comp:
                continue
            dep = flow_deps_map.get((flow_name, comp))
            if dep is None:
                last_missing_comp = comp
                continue
            if dep == 'DIRECT':
                best_dep = 'DIRECT'
                break  # DIRECT is best, stop early
            if dep == 'INDIRECT' and best_dep is None:
                best_dep = 'INDIRECT'
        if best_dep is None:
            # In FOCUSED-KB mode the registry only covers flows reachable from
            # the changed method, so many (flow, component) pairs will be absent
            # from flow_dependencies.json.  Treat a missing entry as "no
            # dependency" and skip this flow rather than aborting the pipeline.
            print(
                f"[Stage 4] SKIP flow '{flow_name}': no dependency entry for "
                f"component '{last_missing_comp}' in flow_dependencies.json "
                f"(focused KB - expected in multi-method mode)"
            )
            continue
        impact_type = best_dep
        all_flows.append({
            'flow_id': flow_id,
            'flow_name': flow_name,
            'flow_tag': flow_tag,
            'impact_type': impact_type,
        })
    print(f"Total impacted flows: {len(all_flows)}")

    # Build a quick lookup: flow_tag -> dict (for downstream tests with
    # multiple matches)
    flow_lookup_by_tag = {}
    for f in all_flows:
        tag = f.get('flow_tag') or ''
        if tag and tag not in flow_lookup_by_tag:
            flow_lookup_by_tag[tag] = {
                'flow_id': f.get('flow_id') or '',
                'flow_name': f.get('flow_name') or '',
                'flow_tag': tag,
                'impact_type': f.get('impact_type', 'DIRECT')
            }

    # Load test corpus - FAIL-FAST: a missing or malformed corpus is an
    # upstream KB problem, not a recoverable edge case.
    if not test_corpus_path or not os.path.isfile(test_corpus_path):
        raise FileNotFoundError(
            f"[Stage 4] test corpus not found: {test_corpus_path}\n"
            f"Root cause: Stage 0 did not produce the enriched test "
            f"corpus (all_tcs_extracted_enriched.json).\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb' to regenerate."
        )
    with open(test_corpus_path, 'r', encoding='utf-8') as f:
        all_tests = json.load(f)
    if not isinstance(all_tests, list):
        raise RuntimeError(
            f"[Stage 4] test corpus at {test_corpus_path} is not a JSON "
            f"list (got {type(all_tests).__name__}).\n"
            f"Fix: Re-run 'python3 ria_agent.py --rebuild-kb'."
        )
    if not all_tests:
        raise RuntimeError(
            f"[Stage 4] test corpus at {test_corpus_path} is empty.\n"
            f"Root cause: Stage 0 produced no enriched tests, indicating "
            f"the source corpus was empty or scoring rejected every test.\n"
            f"Fix: Run 'python3 ria_agent.py --rebuild-kb' and verify "
            f"the source corpus (all_tcs_extracted.json) is non-empty."
        )
    print(f"Total tests in corpus: {len(all_tests)}")

    # Per-test correlation: collect ALL flow matches per test
    print(f"\n{'=' * 80}")
    print(f"CORRELATION PHASE: Per-test scoring with all matched flows")
    print(f"{'=' * 80}")

    impacted_flow_tags = set(flow_lookup_by_tag.keys())

    # ------------------------------------------------------------------
    # Parallel pre-pass: collect flow-lane candidates (tests that pass
    # the tag + component filters) and score them in parallel via
    # ProcessPoolExecutor. The scoring loop below performs a simple
    # dict lookup instead of calling _compute_idf_score sequentially.
    # The fallback to sequential scoring (small corpus or worker
    # failure) preserves identical behaviour.
    # ------------------------------------------------------------------
    flow_lane_candidates = []
    for _t in all_tests:
        _tags = set(extract_flow_tags(_t))
        if not (_tags & impacted_flow_tags):
            continue
        if component_keywords and not check_component_mention(_t, component_keywords):
            continue
        flow_lane_candidates.append(_t)

    idf_results_by_key = {}
    _PARALLEL_THRESHOLD = 500
    if len(flow_lane_candidates) >= _PARALLEL_THRESHOLD:
        try:
            _workers = min(8, max(1, (os.cpu_count() or 2)))
            with ProcessPoolExecutor(
                max_workers=_workers,
                initializer=_stage4_worker_init,
                initargs=(_idf_index, _diff_phrases,
                          _embeddings_index, _diff_embedding),
            ) as _pool:
                _scored = list(_pool.map(_stage4_worker_score,
                                          flow_lane_candidates))
            for _cand, _res in zip(flow_lane_candidates, _scored):
                _k = _cand.get('issue_key')
                if _k is not None:
                    idf_results_by_key[_k] = _res
        except Exception as _e:
            print(f"[Stage4] WARNING: parallel flow-lane scoring "
                  f"failed ({_e}); falling back to sequential.")
            idf_results_by_key = {}

    test_results = []
    for test in all_tests:
        test_tags = set(extract_flow_tags(test))
        matched_tags = test_tags & impacted_flow_tags
        if not matched_tags:
            continue

        # Component check: skip only if component_keywords is non-empty
        # and test doesn't match. In consolidated multi-method mode,
        # component_keywords may be empty to disable this filter.
        if component_keywords:
            component_matched = check_component_mention(test, component_keywords)
            if not component_matched:
                # Per spec, component match is required when keywords
                # provided. Skip if missing.
                continue

        # Build matched_flows[] with per-flow match scores
        matched_flows = []
        per_flow_signal3 = []
        criticality_priority = 99  # lower = more critical (CRITICAL=0)
        chosen_impact_type = None
        chosen_dep_type = None         # resolved DIRECT/INDIRECT from flow_deps
        # chosen_crit_source: source label for diagnostics; expected to be
        # overwritten with "flow_deps" by the inner loop. Initial value is
        # only kept for the unlikely case where matched_flows is empty,
        # which is filtered out below.
        chosen_crit_source = "uninitialized"

        # Iterate matched_tags in a deterministic order. Without sorting
        # we would walk a set in hash order, which can change the
        # `chosen_impact_type` tie-break across runs.
        for tag in sorted(matched_tags):
            flow_info = flow_lookup_by_tag.get(tag)
            if not flow_info:
                # matched_tags is the intersection of test_tags and
                # impacted_flow_tags (== flow_lookup_by_tag.keys()), so
                # this is impossible unless flow_lookup_by_tag was
                # mutated mid-loop. Surface as a hard bug.
                raise RuntimeError(
                    f"[Stage 4] Internal invariant violated: matched tag "
                    f"'{tag}' missing from flow_lookup_by_tag. This "
                    f"indicates a programming error in stage4."
                )

            # signal3 is retained per-flow for diagnostics only.
            # signal3 is NOT added to total_score.
            signal3 = _signal3_score_for_flow(test, tag)
            per_flow_signal3.append(signal3)

            # Per-flow match score = layer1 (flow match) + signal3
            # (diagnostic only - stored in matched_flows[] for JSON output).
            # Component score is global (per test) and added once at the end.
            # Note: FLOW_MATCH_POINTS is now 0 (redundant after tag filter).
            match_score = FLOW_MATCH_POINTS + signal3

            # Resolve criticality via flow_dependencies.json lookup.
            # Instead of trusting a flow-level impact_type, we look up
            # the EXACT (flow_name, changed_component) pair so the
            # criticality matches what the dependency analysis actually
            # found for THIS change. There is NO fallback: missing
            # entries in flow_deps_map raise RuntimeError so KB drift
            # is surfaced immediately.
            flow_name_for_lookup = flow_info['flow_name']
            per_flow_crit, per_flow_dep, per_flow_src = (
                assign_criticality_from_deps(
                    flow_name_for_lookup,
                    resolved_components,
                    flow_deps_map,
                    fallback_impact_type=flow_info['impact_type']))

            matched_flows.append({
                "flow_id": flow_info['flow_id'],
                "flow_name": flow_info['flow_name'],
                "flow_tag": tag,
                "impact_type": flow_info['impact_type'],
                "dependency_type": per_flow_dep,
                "criticality_source": per_flow_src,
                "match_score": match_score,
                "signal3_score": signal3
            })

            # Track best (most-critical) impact for this test across
            # all matched flows. DIRECT (CRITICAL) beats INDIRECT (HIGH).
            crit_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}.get(
                per_flow_crit, 3)
            if crit_rank < criticality_priority:
                criticality_priority = crit_rank
                chosen_impact_type = flow_info['impact_type']
                chosen_dep_type = per_flow_dep
                chosen_crit_source = per_flow_src

        if not matched_flows:
            continue

        # Total score components:
        #   layer1 = FLOW_MATCH_POINTS (0 - flow match already proven by tag
        #            filter at line 726-728, no need to reward it again)
        #   layer2 = COMPONENT_MATCH_POINTS (component mentioned in test)
        #   idf_score = IDF-weighted diff concept match score
        #               (0..unbounded)
        # The IDF score provides the DIFFERENTIATION signal — tests that
        # mention the actual changed business concepts (e.g. "daily
        # shift gap") score higher than tests that merely share the same
        # flow tag.
        layer1 = FLOW_MATCH_POINTS  # 0 (redundant after tag filter)
        layer2 = COMPONENT_MATCH_POINTS  # 40
        layer3 = max(per_flow_signal3) if per_flow_signal3 else 0
        base_score = layer1 + layer2  # 40 for flow-lane tests

        # IDF-weighted diff concept scoring (uses parallel-precomputed
        # result if available; otherwise falls back to sequential call).
        _cached = idf_results_by_key.get(test.get('issue_key'))
        if _cached is not None:
            idf_score, idf_details, emb_sim = _cached
        else:
            idf_score, idf_details, emb_sim = _compute_idf_score(test)
        total_score = base_score + idf_score

        # Inclusion threshold check. Flow-lane tests have base_score=40,
        # so they need IDF boost to cross INCLUSION_THRESHOLD (40).
        # Keyword-lane tests have base_score=0, need even more IDF.
        if total_score < INCLUSION_THRESHOLD:
            continue

        # Final criticality is the flow_deps_map result resolved per
        # (flow, changed_component). FALLBACK DELETED: previously this
        # branched to `assign_criticality(chosen_impact_type or "DIRECT")`
        # when chosen_dep_type was None, but assign_criticality_from_deps
        # now raises RuntimeError before chosen_dep_type can stay None,
        # so the else branch is unreachable. We assert the invariant so
        # any future regression surfaces immediately.
        if not chosen_dep_type:
            raise RuntimeError(
                f"[Stage 4] Internal invariant violated: chosen_dep_type "
                f"is None for test {test.get('issue_key')!r} despite "
                f"matched_flows being non-empty. This indicates a "
                f"programming error in stage4."
            )
        criticality = assign_criticality(chosen_dep_type)

        # Sort matched_flows deterministically: by match_score desc,
        # then signal3 desc, then flow_tag for stable tie-breaking.
        matched_flows.sort(key=lambda f: (-f['match_score'],
                                           -f.get('signal3_score', 0),
                                           f.get('flow_tag', '')))

        # Field-priority tier bonus: summary > description > steps. This
        # is the lever Stages 5/6 use at final ranking to make sure
        # tests that name the component in their summary outrank tests
        # that only mention it deep in steps. Bonus is ALWAYS computed
        # when tier keywords exist - it does not exclude any test.
        tier_bonus, tier_field = compute_tier_bonus(
            test, tier_keywords, tier_pattern)

        test_results.append({
            "issue_key": test.get('issue_key'),
            "summary": test.get('summary'),
            # Preserve description + steps so downstream refinement
            # stages (Stage 5 keyword/component filters, Stage 6
            # suppression) can search the full test text instead of
            # relying on summary alone. Without these, compound-phrase
            # component filters silently kill 99%+ of tests because
            # most tests reference the modified domain in steps/
            # description rather than in the (terse) summary.
            "description": test.get('description'),
            "steps": test.get('steps'),
            "matched_flows": matched_flows,
            "matched_flow": matched_flows[0]['flow_name'],   # back-compat
            "impact_type": chosen_impact_type or "DIRECT",
            "dependency_type": chosen_dep_type or (chosen_impact_type or "DIRECT"),
            "criticality_source": chosen_crit_source,
            "score_breakdown": {
                "flow_match": layer1,
                "component_match": layer2,
                "signal3": layer3,  # diagnostic only
                "idf_score": idf_score,
                "idf_matches": idf_details[:5],  # top 5 matches for diagnostics
                "base_score": base_score,
                "tier_bonus": tier_bonus,
                "tier_field": tier_field,
                "embedding_sim": round(emb_sim, 4) if emb_sim is not None else None
            },
            "total_score": total_score,
            "score": total_score,                            # back-compat
            "tier_bonus": tier_bonus,
            "tier_field": tier_field,
            "criticality": criticality,
            "priority": test.get('priority')
        })

    print(f"Per-test matches (flow lane): {len(test_results)}")

    # ------------------------------------------------------------------
    # KEYWORD LANE: Corpus-wide IDF scan (independent of flow matching)
    #
    # The flow lane above only scores tests that share a flow tag with
    # the impacted flows.  When shared code serves multiple business
    # flows (e.g. Trade AND Swap both call the same validation logic),
    # the call-tree tracer may discover only ONE path, leaving the
    # other flow's tests invisible.
    #
    # The keyword lane fixes this by IDF-scoring EVERY test in the
    # corpus against the diff-derived phrases.  Tests that pass the
    # same precision gates (min_matches + trigram requirement) are
    # added with impact_type="KEYWORD" and criticality="MEDIUM".
    #
    # Deduplication: tests already matched via the flow lane are skipped.
    # Stage 5/6 IDF bypass logic already handles KEYWORD-lane tests
    # (they have high idf_score, which bypasses the component filter).
    # ------------------------------------------------------------------
    keyword_lane_count = 0
    if _diff_phrases:
        flow_matched_keys = {t['issue_key'] for t in test_results}

        # Load the FULL corpus for keyword lane scanning. If a separate
        # full_corpus_path was provided, use it; otherwise fall back to
        # the same corpus used for flow matching (enriched subset).
        if full_corpus_path and os.path.isfile(full_corpus_path):
            with open(full_corpus_path, 'r', encoding='utf-8') as f:
                keyword_corpus = json.load(f)
        else:
            keyword_corpus = all_tests

        print(f"\n{'=' * 80}")
        print(f"KEYWORD LANE: Corpus-wide IDF scan ({len(keyword_corpus)} tests, "
              f"{len(_diff_phrases)} diff phrases)")
        print(f"{'=' * 80}")

        # Parallel pre-pass for keyword lane: score every test that is
        # not already covered by the flow lane. Same fall-back rules as
        # the flow lane (sequential when corpus is small or workers fail).
        keyword_lane_candidates = [
            _t for _t in keyword_corpus
            if _t.get('issue_key') not in flow_matched_keys
        ]
        keyword_idf_by_key = {}
        if len(keyword_lane_candidates) >= _PARALLEL_THRESHOLD:
            try:
                _workers = min(8, max(1, (os.cpu_count() or 2)))
                with ProcessPoolExecutor(
                    max_workers=_workers,
                    initializer=_stage4_worker_init,
                    initargs=(_idf_index, _diff_phrases,
                              _embeddings_index, _diff_embedding),
                ) as _pool:
                    _scored = list(_pool.map(_stage4_worker_score,
                                              keyword_lane_candidates))
                for _cand, _res in zip(keyword_lane_candidates, _scored):
                    _k = _cand.get('issue_key')
                    if _k is not None:
                        keyword_idf_by_key[_k] = _res
            except Exception as _e:
                print(f"[Stage4] WARNING: parallel keyword-lane scoring "
                      f"failed ({_e}); falling back to sequential.")
                keyword_idf_by_key = {}

        for test in keyword_corpus:
            key = test.get('issue_key')
            if key in flow_matched_keys:
                continue  # already scored via flow lane

            _cached = keyword_idf_by_key.get(key)
            if _cached is not None:
                idf_score, idf_details, emb_sim = _cached
            else:
                idf_score, idf_details, emb_sim = _compute_idf_score(test)
            if idf_score <= 0:
                continue  # precision gates (min_matches + trigram) failed

            # Keyword lane has base_score=0 (no flow tag, no component
            # match). It must still cross INCLUSION_THRESHOLD via IDF
            # alone — tests that only weakly mention diff phrases must
            # not pollute the recommendation set.
            if idf_score < INCLUSION_THRESHOLD:
                continue

            tier_bonus, tier_field = compute_tier_bonus(
                test, tier_keywords, tier_pattern)

            test_results.append({
                "issue_key": key,
                "summary": test.get('summary'),
                "description": test.get('description'),
                "steps": test.get('steps'),
                "matched_flows": [],
                "matched_flow": None,
                "impact_type": "KEYWORD",
                "score_breakdown": {
                    "flow_match": 0,
                    "component_match": 0,
                    "signal3": 0,
                    "idf_score": idf_score,
                    "idf_matches": idf_details[:5],
                    "base_score": 0,
                    "tier_bonus": tier_bonus,
                    "tier_field": tier_field,
                    "embedding_sim": round(emb_sim, 4) if emb_sim is not None else None
                },
                "total_score": idf_score,
                "score": idf_score,
                "tier_bonus": tier_bonus,
                "tier_field": tier_field,
                "criticality": "MEDIUM",
                "priority": test.get('priority')
            })
            keyword_lane_count += 1

        print(f"Keyword lane: {keyword_lane_count} additional tests "
              f"from corpus-wide IDF scan")
    else:
        print(f"\nKeyword lane: skipped (no diff phrases available)")

    print(f"\nTotal per-test matches (flow + keyword): {len(test_results)}")

    # Sort deterministically by:
    #   1. criticality asc (CRITICAL first)
    #   2. tier_bonus desc (summary-mentions first)
    #   3. total_score desc
    #   4. signal3_score desc
    #   5. issue_key asc (stable tie-break)
    criticality_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}
    test_results.sort(key=lambda t: (
        criticality_order.get(t['criticality'], 3),
        -t.get('tier_bonus', 0),
        -t['total_score'],
        -t.get('score_breakdown', {}).get('signal3', 0),
        t.get('issue_key', '') or '',
    ))

    # Score variability stats (for visibility / sanity)
    if test_results:
        scores = [t['total_score'] for t in test_results]
        score_min, score_max = min(scores), max(scores)
        unique_scores = sorted(set(scores), reverse=True)
        print(f"Score range: {score_min} .. {score_max}  (unique values: {len(unique_scores)})")
    else:
        score_min = score_max = 0
        unique_scores = []

    # Build output
    output = {
        "stage": 4,
        "description": "Test correlation with 2-layer + 3-signal scoring",
        "run_id": run_id,
        "generated_at": time.strftime('%Y-%m-%d %H:%M:%S'),
        "algorithm": {
            "flow_match_points": FLOW_MATCH_POINTS,
            "component_match_points": COMPONENT_MATCH_POINTS,
            "inclusion_threshold": INCLUSION_THRESHOLD
        },
        "changed_component": changed_component,
        "changed_components": resolved_components,
        "total_flows_analyzed": len(all_flows),
        # Fix #4: Rename `total_tests_analyzed` -> `flow_lane_tests` to make
        # the metric self-describing. The previous name implied the count
        # represented the entire corpus that was analyzed, but Stage 4 only
        # iterates the flow-corpus subset (enriched corpus with flow tags),
        # while the keyword lane scans a separate full corpus and emits
        # additional matches. With the new name it is obvious why
        # `total_recommended` (flow + keyword lanes) can exceed
        # `flow_lane_tests` (flow-lane corpus size). The legacy key is kept
        # as an alias so downstream consumers (e.g. older HTML report
        # checkpoints) keep working.
        "flow_lane_tests": len(all_tests),
        "total_tests_analyzed": len(all_tests),  # back-compat alias
        "recommended_tests": test_results,
        # Clamp at 0. The keyword lane can introduce tests that are not in
        # the flow corpus, so len(test_results) may exceed len(all_tests).
        # Negative exclusion counts are nonsensical.
        "excluded_tests_count": max(0, len(all_tests) - len(test_results)),
        "total_recommended": len(test_results),
        "score_variability": {
            "min": score_min,
            "max": score_max,
            "unique_values": len(unique_scores)
        },
        "breakdown_by_criticality": {
            "CRITICAL": len([t for t in test_results if t['criticality'] == 'CRITICAL']),
            "HIGH":     len([t for t in test_results if t['criticality'] == 'HIGH']),
            "MEDIUM":   len([t for t in test_results if t['criticality'] == 'MEDIUM'])
        },
        "breakdown_by_lane": {
            "flow":    len([t for t in test_results if t.get('impact_type') != 'KEYWORD']),
            "keyword": len([t for t in test_results if t.get('impact_type') == 'KEYWORD'])
        }
    }

    # Save output
    output_path = os.path.join(output_dir, "stage4_recommended_tests.json")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n{'=' * 80}")
    print(f"STAGE 4 COMPLETE")
    print(f"{'=' * 80}")
    print(f"Recommended tests: {len(test_results)}")
    print(f"  CRITICAL:  {output['breakdown_by_criticality']['CRITICAL']}")
    print(f"  HIGH:      {output['breakdown_by_criticality']['HIGH']}")
    print(f"  MEDIUM:    {output['breakdown_by_criticality']['MEDIUM']}")
    print(f"  Flow lane: {output['breakdown_by_lane']['flow']}")
    print(f"  Keyword lane: {output['breakdown_by_lane']['keyword']}")
    print(f"Excluded tests:    {output['excluded_tests_count']}")
    print(f"Output saved to:   {output_path}")

    if test_results:
        print(f"\nTop 10 Recommended Tests:")
        for i, t in enumerate(test_results[:10], 1):
            print(f"  {i}. [{t['criticality']}] {t['issue_key']} - "
                  f"{(t['summary'] or '')[:60]}... "
                  f"(score: {t['total_score']}, flows: {len(t['matched_flows'])})")

    return output


def main():
    parser = argparse.ArgumentParser(
        description="Stage 4: Test Correlation with 2-Layer + 3-Signal Scoring"
    )
    # ---- Input (KB-direct mode, the only supported path) ----
    parser.add_argument(
        "--flow-registry", required=True,
        help="Path to flow_registry.json (focused mode). Stage 4 reads "
             "impacted flows directly from flow_registry."
    )
    parser.add_argument(
        "--changed-file",
        help="Changed file path (used to derive component when "
             "--changed-component is not supplied)."
    )
    parser.add_argument(
        "--changed-components",
        default=None,
        help="Comma-separated list of changed component names "
             "(multi-method consolidated mode)."
    )
    # ---- Common args ----
    # Both flags below are equivalent. The `--enriched-corpus-path` alias
    # is kept for explicit per-pipeline routing under Option A (separate
    # enriched corpus per pipeline). Pipeline-aware call sites use the
    # explicit alias because it self-documents (e.g. the dependency-pipeline
    # orchestrator passes
    # `--enriched-corpus-path all_tcs_extracted_enriched_dependency.json`).
    # `argparse` does not allow two flags to share `dest` AND each have a
    # separate default, so both flags map to `test_corpus` and the orchestrator
    # passes only one. The default lives on `--test-corpus`.
    parser.add_argument("--test-corpus", "--enriched-corpus-path",
                        dest="test_corpus",
                        default=TC_DATA_PATH.replace('.json', '_enriched.json'),
                        help="Path to enriched test corpus. Use "
                             "all_tcs_extracted_enriched_source.json or "
                             "all_tcs_extracted_enriched_dependency.json "
                             "to scope Stage 4 to a specific pipeline's "
                             "enriched corpus.")
    parser.add_argument("--changed-component",
                        help="Component that was changed (single-method mode)")
    parser.add_argument("--diff-concepts", default=None,
                        help="Path to diff_concepts.json (from extract_diff_concepts)")
    parser.add_argument("--full-corpus", default=None,
                        help="Path to full (un-enriched) test corpus for keyword lane")
    parser.add_argument("--kb-dir",
                        default=os.path.join(RIA_OUTPUT_DIR, "knowledge_base"),
                        help="Knowledge base directory")
    parser.add_argument("--flow-deps-path", default=None,
                        help="Override path to flow_dependencies.json. Used by the "
                             "dependency-change pipeline to load "
                             "flow_dependencies_dependency.json so source-pipeline "
                             "KB is not required to contain dependency-only components.")
    parser.add_argument("--output-dir", default=RIA_OUTPUT_DIR,
                        help="Output directory")
    parser.add_argument("--run-id", default=None,
                        help="Optional run id to stamp on output")

    args = parser.parse_args()

    # Parse comma-separated changed_components (multi-method mode)
    changed_components_list = None
    if args.changed_components:
        changed_components_list = [
            c.strip() for c in args.changed_components.split(',') if c.strip()
        ]

    try:
        result = run_stage4(
            flow_registry_path=args.flow_registry,
            test_corpus_path=args.test_corpus,
            kb_dir=args.kb_dir,
            output_dir=args.output_dir,
            changed_component=args.changed_component,
            changed_components=changed_components_list,
            changed_file=args.changed_file,
            run_id=args.run_id,
            diff_concepts_path=args.diff_concepts,
            full_corpus_path=args.full_corpus,
            flow_deps_path=args.flow_deps_path,
        )

        if result['total_recommended'] == 0:
            print("\nWARNING: No tests recommended.")
            sys.exit(1)

        sys.exit(0)

    except Exception as e:
        print(f"\nERROR: Stage 4 failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
