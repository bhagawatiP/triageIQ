#!/usr/bin/env python3
"""
Stage 6: Aggressive Suppression

Reduces Stage 5 output to a tight final set using:
  - High keyword precision threshold (default >= 4 keyword groups matched).
  - Reduced per-flow diversity quotas (default 3 per flow).

Post-pipeline-simplification: Stage 6 now reads flows directly from the
focused KB (flow_registry.json + flow_dependencies.json) - the legacy
stage2_impacted_flows.json shim has been removed.

  - Method-specific keywords are derived at runtime from --changed-method.
  - Flow quotas are derived dynamically from --flow-registry JSON.
  - changed_components are passed explicitly via --changed-components.
  - Stage 4 input count is read from stage4_recommended_tests.json (not
    hard-coded).
  - Input/output paths are CLI-driven (no hard-coded absolute paths).
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Import component filtering functions from stage5.
# FAIL-FAST: a missing/broken stage5 module is a hard installation bug, NOT
# a recoverable condition. The previous ImportError catch installed empty
# stub functions that silently passed every test through with no filtering,
# masking the real failure. We now propagate the ImportError so the user
# sees the actual cause (missing module, syntax error, etc.).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stage5_refine_tests import (
    derive_method_keywords,
    extract_component_keywords,
    check_component_match,
    _build_tier_pattern,
    compute_tier_bonus,
    get_tier_bonus,
    _derive_idf_bypass_threshold,
    load_flows_from_registry,
    calculate_keyword_score,
)

# ---------------------------------------------------------------------------
# Path defaults
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
GITHUB_DIR = SKILL_DIR.parent.parent
REPO_ROOT = GITHUB_DIR.parent
DEFAULT_OUTPUT_DIR = GITHUB_DIR / 'RIA_OUTPUT'

DEFAULT_INPUT = DEFAULT_OUTPUT_DIR / 'stage5_refined_tests.json'
DEFAULT_OUTPUT = DEFAULT_OUTPUT_DIR / 'stage6_aggressive_tests.json'
DEFAULT_STAGE4 = DEFAULT_OUTPUT_DIR / 'stage4_recommended_tests.json'
DEFAULT_KB_DIR = DEFAULT_OUTPUT_DIR / 'knowledge_base'
DEFAULT_FLOW_REGISTRY = DEFAULT_KB_DIR / 'flow_registry.json'
DEFAULT_FLOW_DEPENDENCIES = DEFAULT_KB_DIR / 'flow_dependencies.json'

# Algorithm defaults
DEFAULT_MIN_KEYWORD_SCORE = 1   # same as Stage 5 (was 4, then 2, now 1)
DEFAULT_REDUCED_QUOTA = 3       # per-flow quota for diversity boost
DEFAULT_TOP_N = 45              # only used when --strategy=top_n
# Target final test count for keyword_plus_diversity strategy. Stage 6 enforces
# a global top-N cap (by composite/keyword/signal3 score) so the union of
# keyword-precision tests + flow-diversity tests stays in the recommended
# 20-30 test range regardless of how many flows the change touches.
DEFAULT_FINAL_CAP = 30


def _adaptive_final_cap(default, n_flows, n_input_tests):
    """Scale the final cap with the actual evidence available.

    Logic:
      - Tiny input (<= cap): no scaling needed.
      - Many flows (cross-cutting): +3 per extra flow above 5, ceiling 60.
      - Few input tests / sparse evidence: never exceed input.
    """
    cap = default or DEFAULT_FINAL_CAP
    if n_flows > 5:
        cap = min(cap + (n_flows - 5) * 3, 60)
    if n_input_tests and n_input_tests < cap:
        cap = max(1, n_input_tests)
    return cap


def _adaptive_embedding_gates(kb_dir=None):
    """Return (low, high, calltree) embedding similarity gates.

    Reads the optional embeddings_stats.json that Stage 0c may produce.
    When available, uses corpus percentiles:
      - low      = p25 of test/test similarities
      - high     = p75 of diff/test similarities
      - calltree = p50 of diff/test similarities
    Fallbacks: 0.20 / 0.40 / 0.35 (validated EEM defaults).
    """
    low, high, calltree = 0.20, 0.40, 0.35
    if not kb_dir:
        return low, high, calltree
    stats_path = os.path.join(str(kb_dir), 'embeddings_stats.json')
    if not os.path.isfile(stats_path):
        return low, high, calltree
    try:
        with open(stats_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        p25 = data.get('p25_test_similarity')
        p75 = data.get('p75_diff_similarity')
        p50 = data.get('p50_diff_similarity')
        if p25 and 0.0 < float(p25) < 1.0:
            low = max(0.10, float(p25))
        if p75 and 0.0 < float(p75) < 1.0:
            high = max(low + 0.05, float(p75))
        if p50 and 0.0 < float(p50) < 1.0:
            calltree = max(low, min(high - 0.05, float(p50)))
    except (json.JSONDecodeError, IOError, OSError, KeyError):
        pass
    return low, high, calltree


# ---------------------------------------------------------------------------
# UPGRADE 3: Reciprocal Rank Fusion (RRF) — replaces the magic-formula
# `_hybrid_score` previously used for the global cap.
#
# RRF(d) = sum over rankers i of  1 / (k + rank_i(d))
#
# Cormack et al. (SIGIR 2009) show RRF matches or beats learned fusion
# without tuning, and k=60 is the standard constant proposed in that
# paper.  We fuse two rankers:
#   - IDF score  (lexical specificity of matched diff phrases)
#   - Embedding cosine similarity (semantic alignment with the diff)
#
# Items missing from a ranker (e.g. no embedding because the test is
# brand-new and not in the embedding KB) are placed at the
# `len(tests)+1` rank so they receive the smallest possible
# contribution from that signal — no arbitrary penalty, no NaN.
#
# Ties are handled with DENSE ranking (1, 1, 2, 3, ...): equal scores
# share a rank.  This avoids artificially penalising the second copy of
# an identical-score test.
# ---------------------------------------------------------------------------
RRF_K_DEFAULT = 60


def _dense_rank_map(tests: List[Dict[str, Any]], score_fn) -> Dict[str, int]:
    """Return {issue_key: rank} where rank is 1-based dense rank by `score_fn`
    (descending). Tests with the same score share a rank.

    Tests missing an issue_key are skipped so the map's size never exceeds
    the number of uniquely keyed tests in the input.
    """
    keyed = [(t.get('issue_key', '') or '', score_fn(t)) for t in tests
             if (t.get('issue_key') or '') != '']
    if not keyed:
        return {}
    # Sort descending, then dense-rank: items with equal scores share a rank.
    keyed.sort(key=lambda kv: -kv[1])
    rank_map: Dict[str, int] = {}
    prev_score = None
    rank = 0
    for key, score in keyed:
        if score != prev_score:
            rank += 1
            prev_score = score
        rank_map[key] = rank
    return rank_map


def _build_rrf_score_map(tests: List[Dict[str, Any]],
                          k: int = RRF_K_DEFAULT,
                          flow_tag_boost: float = 1.5
                          ) -> Dict[str, float]:
    """Compute Reciprocal Rank Fusion scores for every test.

    Args:
        tests: candidate tests (already filtered by upstream stages).
        k: RRF smoothing constant. The standard value from Cormack 2009.
        flow_tag_boost: multiplicative factor applied to RRF when the test
            has matched_flows[]. This preserves the call-tree structural
            prior — flow-tagged tests exercise the changed code path and
            must rank above keyword-only tests that merely share
            vocabulary, all else being equal.

    Returns:
        {issue_key: rrf_score}. Tests with no issue_key are absent.

    The function is pure: same input -> same output. It does NOT mutate
    `tests` or rely on any module-level state.
    """
    if not tests:
        return {}

    def _idf_of(t):
        return t.get('score_breakdown', {}).get('idf_score', 0) or 0.0

    def _emb_of(t):
        return t.get('score_breakdown', {}).get('embedding_sim') or 0.0

    idf_ranks = _dense_rank_map(tests, _idf_of)
    emb_ranks = _dense_rank_map(tests, _emb_of)

    # "Missing" rank (worst possible) is len(tests) + 1, so 1/(k + missing)
    # is strictly smaller than any in-corpus contribution.
    miss_rank = len(tests) + 1

    rrf: Dict[str, float] = {}
    for t in tests:
        key = t.get('issue_key', '') or ''
        if not key:
            continue
        ri = idf_ranks.get(key, miss_rank)
        re_ = emb_ranks.get(key, miss_rank)
        score = 1.0 / (k + ri) + 1.0 / (k + re_)
        if t.get('matched_flows'):
            score *= flow_tag_boost
        rrf[key] = score
    return rrf


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_max_signal3(test: Dict[str, Any]) -> int:
    flows = test.get('matched_flows') or []
    if not flows:
        return 0
    return max((flow.get('signal3_score', 0) or 0) for flow in flows)


def get_primary_flow(test: Dict[str, Any]) -> str:
    flows = test.get('matched_flows') or []
    return flows[0].get('flow_name', '') if flows else ''


def _get_primary_concept(test: Dict[str, Any]) -> Optional[str]:
    """Return the highest-contribution lexical diff phrase this test matched.

    Used by concept diversity to ensure each unique code-change concept
    (e.g. "min daily shift gap") has representation in the final cap,
    even when other concepts dominate the rankings by sheer match count.
    """
    matches = test.get('score_breakdown', {}).get('idf_matches', [])
    best = None
    best_score = -1.0
    for m in matches:
        layer = m.get('match_layer', '')
        if layer == 'embedding':  # skip pure-embedding entries
            continue
        contrib = m.get('contribution', 0)
        if contrib > best_score:
            best_score = contrib
            best = m.get('phrase')
    return best


def derive_flow_quotas(flows: List[Dict[str, Any]], quota_per_flow: int,
                       kb_dir: str = None,
                       enriched_corpus_path: str = None) -> Dict[str, int]:
    """
    Build {flow_name: adaptive_quota} from a list of flow records loaded
    via load_flows_from_registry().

    Adaptive quota: hub flows (high corpus coverage) get reduced quotas.
    - Normal flow (10% corpus): full quota (e.g. 3 tests)
    - Hub flow (65% corpus): reduced quota (e.g. 1 test)

    This keeps trade tests from [FETCH_TRADE_AGENT_LIST] but limits noise.

    Args:
        enriched_corpus_path: Optional explicit path to the enriched test
            corpus. Option A (separate enriched corpus per pipeline) uses
            this to route Stage 6 to the matching pipeline's corpus.
            When omitted the legacy
            `<kb_dir>/all_tcs_extracted_enriched.json` is used.
    """
    if not flows:
        return {}

    # Load corpus for coverage analysis
    corpus_coverage = {}
    corpus_size = 0
    if kb_dir:
        corpus_path = (enriched_corpus_path
                       if enriched_corpus_path
                       else os.path.join(kb_dir, 'all_tcs_extracted_enriched.json'))
        if os.path.isfile(corpus_path):
            try:
                with open(corpus_path, 'r', encoding='utf-8') as f:
                    corpus = json.load(f)
                corpus_size = len(corpus)
                for test in corpus:
                    for tag in test.get('auto_tags', []):
                        corpus_coverage[tag] = corpus_coverage.get(tag, 0) + 1
            except Exception:
                pass

    quotas: Dict[str, int] = {}

    # Derive hub-flow thresholds dynamically from corpus distribution.
    # A "hub flow" is one covering a disproportionate share of the corpus
    # (4x mean coverage). Adapts to any corpus size and flow distribution.
    if corpus_coverage and corpus_size > 0:
        coverage_values = [c / corpus_size for c in corpus_coverage.values() if c > 0]
        if coverage_values:
            mean_coverage = sum(coverage_values) / len(coverage_values)
        else:
            mean_coverage = 0.05
        hub_threshold = min(0.60, mean_coverage * 4)
        medium_threshold = min(0.40, mean_coverage * 2)
    else:
        hub_threshold = 0.40
        medium_threshold = 0.20

    for flow in flows:
        name = flow.get('flow_name') or flow.get('flow')
        flow_tag = flow.get('flow_tag')
        if not name:
            continue

        # Adaptive quota based on corpus coverage AND impact type
        if flow_tag and corpus_coverage and corpus_size > 0:
            test_count = corpus_coverage.get(flow_tag, 0)
            coverage_pct = test_count / corpus_size
            impact_type = flow.get('impact_type', 'DIRECT')
            classification = flow.get('classification', 'PRIMARY')

            # Hub flows: distinguish functional vs infrastructure
            if coverage_pct >= hub_threshold:
                # DIRECT + PRIMARY hub flow = legitimate functional impact
                if impact_type == 'DIRECT' and classification == 'PRIMARY':
                    quotas[name] = max(2, (quota_per_flow * 2) // 3)
                else:
                    quotas[name] = max(1, quota_per_flow // 3)
            # Medium coverage: 2/3 quota
            elif coverage_pct >= medium_threshold:
                quotas[name] = max(1, (quota_per_flow * 2) // 3)
            # Low coverage: full quota
            else:
                quotas[name] = quota_per_flow
        else:
            quotas[name] = quota_per_flow

    return quotas


def read_stage4_count(stage4_path: str) -> int:
    """Return total tests in Stage 4 output.

    FAIL-FAST: a missing or unreadable Stage 4 output is a pipeline-order
    bug, not a recoverable condition.
    """
    if not os.path.isfile(stage4_path):
        raise FileNotFoundError(
            f"[Stage 6] Stage 4 output not found at: {stage4_path}\n"
            f"Root cause: Stage 4 did not run before Stage 6.\n"
            f"Fix: Run Stage 4 (or full pipeline) before invoking "
            f"Stage 6."
        )
    with open(stage4_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    val = data.get('total_recommended')
    if val is not None:
        return val
    val = data.get('output_tests')
    if val is not None:
        return val
    tests = data.get('recommended_tests') or []
    if not tests:
        raise RuntimeError(
            f"[Stage 6] Stage 4 output at {stage4_path} has no "
            f"'total_recommended', 'output_tests', or "
            f"'recommended_tests' fields.\n"
            f"Fix: Re-run Stage 4."
        )
    return len(tests)


# ---------------------------------------------------------------------------
# Suppression strategies
# ---------------------------------------------------------------------------
def suppress_keyword_plus_diversity(tests: List[Dict[str, Any]],
                                     min_keyword_score: int,
                                     flow_quotas: Dict[str, int],
                                     component_keywords: List[str] = None,
                                     final_cap: int = DEFAULT_FINAL_CAP,
                                     kb_dir: str = None,
                                     anchor_concepts: List[str] = None
                                     ) -> List[Dict[str, Any]]:
    """High-keyword-precision tests + top-N-per-flow, with component filtering.

    The combined set is finally capped to `final_cap` tests using composite
    tier_bonus + keyword + signal3 score so the recommendation stays in the
    recommended 20-30 test range and prioritises tests that mention the
    component in their summary over tests that mention it only in steps.
    """
    print("\n" + "=" * 80)
    print(f"STRATEGY: KEYWORD >= {min_keyword_score} + COMPONENT MATCH + TOP {flow_quotas} PER FLOW")
    print("=" * 80)

    # Build tier-bonus pattern so we can refresh tier_bonus on tests that
    # came from stages without it (back-compat) and use it as the primary
    # sort key in the global cap.
    tier_pattern = _build_tier_pattern(component_keywords) if component_keywords else None

    # PRECISION CONTRACT (v7.4): when component_keywords is non-empty, a
    # test must mention at least one compound phrase to survive. There is
    # NO fallback to keyword-only filtering - the previous fallback was
    # too lenient and produced false positives. Producing fewer highly-
    # relevant tests is preferable to producing many tests with mixed
    # relevance.
    #
    # EXCEPTION (v8.0 IDF override): tests with high IDF scores bypass
    # the component keyword requirement because they have DIRECT textual
    # evidence of mentioning the changed business concepts.
    # Threshold is auto-derived from IDF corpus statistics (specificity_3pct).
    IDF_BYPASS_THRESHOLD = _derive_idf_bypass_threshold(kb_dir)
    # Embedding gates (corpus-percentile-derived; validated defaults
    # 0.20 / 0.40 / 0.35 used as fallback when no stats are available).
    _EMB_LOW_GATE, _EMB_HIGH_GATE, _CALLTREE_EMB_GATE_VAL = _adaptive_embedding_gates(kb_dir)

    keyword_pass = [t for t in tests if t.get('keyword_score', 0) >= min_keyword_score
                    or t.get('score_breakdown', {}).get('idf_score', 0) >= IDF_BYPASS_THRESHOLD]
    high_precision: List[Dict[str, Any]] = []
    idf_bypassed = 0
    calltree_filtered = 0
    anchor_filtered = 0
    if anchor_concepts:
        print(f"\n[stage6] Method-name anchor concepts: {anchor_concepts}")
    for t in keyword_pass:
        comp_match = t.get('component_match')
        # Recompute when missing (e.g. the test did not pass through Stage 5
        # in this run) so Stage 6 can stand alone.
        if comp_match is None:
            comp_match = check_component_match(t, component_keywords) if component_keywords else True
        # Strict gate: drop tests that don't match the component when
        # component keywords are configured.
        # EXCEPTION: high IDF score bypasses this requirement.
        idf_score = t.get('score_breakdown', {}).get('idf_score', 0)
        if component_keywords and not comp_match:
            if idf_score < IDF_BYPASS_THRESHOLD:
                continue
            else:
                # CALL-TREE COHERENCE GATE (v9.0): keyword-only tests
                # (no matched_flows, i.e. no call-tree validation) that
                # bypass the component filter via IDF must also show
                # semantic evidence of relevance.  Without this gate,
                # tests from non-impacted flows (e.g. Extra Hours, AO
                # Simulation) leak through because they share vocabulary
                # with diff context variables but are architecturally
                # unrelated to the changed code path.
                _CALLTREE_EMB_GATE = _CALLTREE_EMB_GATE_VAL
                if not t.get('matched_flows'):
                    emb_sim = (t.get('score_breakdown', {})
                               .get('embedding_sim') or 0.0)
                    if emb_sim < _CALLTREE_EMB_GATE:
                        calltree_filtered += 1
                        continue
                idf_bypassed += 1
        # --- METHOD-NAME ANCHOR RELEVANCE GATE ---
        # Keyword-only tests (no matched_flows) that bypass component match
        # via IDF must also mention at least one anchor concept derived from
        # the changed method names.  This prevents tests about unrelated
        # features (Extra Hours, Time Off) from leaking in just because they
        # share diff-concept variables (e.g. 'split shift', 'shift gap')
        # with the changed code.  Flow-tagged tests are exempt — they are
        # validated by the call-tree.  Uses word-boundary matching to avoid
        # substring false positives (e.g. 'call' matching 'callback').
        if anchor_concepts and not t.get('matched_flows'):
            step_text_anchor = ' '.join(
                (s.get('action') or '') + ' ' + (s.get('data') or '') + ' ' + (s.get('result') or '')
                for s in t.get('steps', [])
            )
            t_text = (t.get('summary', '') + ' '
                      + str(t.get('description', '')) + ' '
                      + step_text_anchor).lower()
            if not any(re.search(r'\b' + re.escape(ac) + r'\b', t_text)
                       for ac in anchor_concepts):
                anchor_filtered += 1
                continue
        t['component_match'] = comp_match
        # Refresh the field-priority tier bonus when it's missing so the
        # global cap can rank summary-mention tests above steps-only ones.
        if tier_pattern is not None and 'tier_bonus' not in t:
            bonus, where = compute_tier_bonus(t, tier_pattern)
            t['tier_bonus'] = bonus
            t['tier_field'] = where
        high_precision.append(t)

    if component_keywords:
        print(f"\n[stage6] Strict filter: kept {len(high_precision)} of "
              f"{len(keyword_pass)} keyword-passing tests after requiring "
              f"a component compound match.")
        if idf_bypassed:
            print(f"[stage6] IDF override: {idf_bypassed} tests bypassed component "
                  f"filter due to high diff-concept match (IDF >= {IDF_BYPASS_THRESHOLD})")
        if calltree_filtered:
            print(f"[stage6] Call-tree coherence gate: {calltree_filtered} keyword-only "
                  f"tests filtered (no matched_flows + embedding_sim < 0.35)")
        if anchor_filtered:
            print(f"[stage6] Anchor relevance gate: {anchor_filtered} keyword-only "
                  f"tests filtered (no matched_flows + no anchor concept in text)")

    print(f"\nStage 1 (Keyword >= {min_keyword_score} + component match): {len(high_precision)} tests (before quotas)")

    kw_dist = defaultdict(int)
    for t in high_precision:
        kw_dist[t.get('keyword_score', 0)] += 1
    if kw_dist:
        print("  Keyword distribution:")
        for s in sorted(kw_dist.keys(), reverse=True):
            print(f"    Score {s}: {kw_dist[s]} tests")

    # Apply per-flow quotas to keyword filter results
    if flow_quotas:
        by_flow_kw = defaultdict(list)
        for t in high_precision:
            by_flow_kw[get_primary_flow(t)].append(t)

        capped_high_precision = []
        print("\n  Applying per-flow quotas to keyword-filtered tests:")
        # Deterministic flow iteration: sorted by flow name.
        for flow in sorted(by_flow_kw.keys()):
            tests_in_flow = by_flow_kw[flow]
            quota = flow_quotas.get(flow, 999)
            # Allow 15x quota for high-precision keyword matches so
            # flow-tagged tests (call-tree validated) are not prematurely
            # eliminated before the global cap can rank them.
            cap = quota * 15 if quota < 999 else 999
            # Deterministic sort: use a composite score that considers
            # all signals — keyword strength, IDF diff-concept match,
            # embedding similarity, and component tier.  This prevents
            # singular/plural component-keyword mismatches from overriding
            # strong IDF + embedding scores.
            def _per_flow_sort_key(t):
                idf = t.get('score_breakdown', {}).get('idf_score', 0)
                emb = t.get('score_breakdown', {}).get('embedding_sim') or 0.0
                kw = t.get('keyword_score', 0)
                tier = get_tier_bonus(t)
                sig3 = ((t.get('matched_flows') or [{}])[0]
                        .get('signal3_score', 0) or 0)
                # Composite: IDF and embedding carry most weight (they
                # measure actual code-change relevance), keyword and
                # tier are secondary discriminators.
                composite = idf + emb * 50 + kw * 5 + tier + sig3
                return (-composite, t.get('issue_key', '') or '')

            sorted_tests = sorted(tests_in_flow, key=_per_flow_sort_key)
            selected = sorted_tests[:cap]
            capped_high_precision.extend(selected)
            if len(tests_in_flow) > cap:
                print(f"    {flow}: {len(tests_in_flow)} tests -> {len(selected)} (quota={quota}, cap={cap})")
            else:
                print(f"    {flow}: {len(tests_in_flow)} tests (under cap={cap})")
        high_precision = capped_high_precision
        print(f"\n  After quotas: {len(high_precision)} tests")

    hp_ids = {t['issue_key'] for t in high_precision}
    remaining = [t for t in tests if t.get('issue_key') not in hp_ids]

    by_flow = defaultdict(list)
    for t in remaining:
        by_flow[get_primary_flow(t)].append(t)

    diversity_tests: List[Dict[str, Any]] = []
    print("\nStage 2 (Flow diversity with reduced quotas + component filter):")
    if not flow_quotas:
        print("  WARNING: No flow quotas available - skipping diversity boost.")
    # Deterministic flow iteration: sorted by flow name.
    for flow in sorted(flow_quotas.keys()):
        quota = flow_quotas[flow]
        flow_tests = by_flow.get(flow, [])
        # Sort flow tests by tier_bonus first so summary-mention tests are
        # picked before steps-only tests in the diversity boost.
        sorted_flow = sorted(
            flow_tests,
            key=lambda t: (
                -get_tier_bonus(t),
                -get_max_signal3(t),
                t.get('issue_key', '') or '',
            ),
        )
        # Apply STRICT component filter: only consider candidates that
        # actually match the component, then take top-N from that set.
        # Drop candidates that don't match - no fallback.
        # EXCEPTION: high IDF tests bypass component filter.
        candidates = []
        for t in sorted_flow:
            comp_match = t.get('component_match')
            if comp_match is None:
                comp_match = check_component_match(t, component_keywords) if component_keywords else True
            idf_score = t.get('score_breakdown', {}).get('idf_score', 0)
            if component_keywords and not comp_match:
                if idf_score < IDF_BYPASS_THRESHOLD:
                    continue
            t['component_match'] = comp_match
            # Refresh tier_bonus for back-compat with older Stage 5 outputs.
            if tier_pattern is not None and 'tier_bonus' not in t:
                bonus, where = compute_tier_bonus(t, tier_pattern)
                t['tier_bonus'] = bonus
                t['tier_field'] = where
            candidates.append(t)
        diversity_tests.extend(candidates[:quota])
        print(f"  {flow}: {len([t for t in diversity_tests if get_primary_flow(t) == flow])} tests (quota: {quota})")

    for t in high_precision:
        t['stage6_selection'] = 'high_keyword_precision'
    for t in diversity_tests:
        t['stage6_selection'] = 'flow_diversity_reduced'

    final_tests = high_precision + diversity_tests
    print(f"\nTotal: {len(high_precision)} + {len(diversity_tests)} = {len(final_tests)} tests")

    # Final global cap: trim to top `final_cap` tests using
    # Reciprocal Rank Fusion (RRF) of two complementary rankers — IDF
    # (lexical specificity) and embedding similarity (semantic alignment).
    #
    # UPGRADE 3: We replaced the hand-tuned hybrid formula
    #     idf * (1 + max(0, emb - 0.30) * 3) * 1.5_if_flow_tagged
    # with the parameter-free RRF score
    #     RRF(d) = Σ 1 / (k + rank_i(d))
    # over the IDF and embedding rankings.  Cormack et al. ("Reciprocal
    # Rank Fusion outperforms Condorcet and individual Rank Learning
    # Methods", SIGIR 2009) show RRF matches or beats learned fusion
    # without tuning, and k=60 is the standard constant from that paper.
    #
    # Why RRF here?
    #   - It is rank-based, so it does not care about the WIDELY different
    #     scales of IDF (0–~30) and cosine similarity (0–1) — the magic
    #     `(emb - 0.30) * 3` factor is no longer needed.
    #   - It is monotonic in each input ranker, so an item that's better
    #     by either signal cannot move down.
    #   - It is robust to ties (multiple items at rank N split the contribution
    #     evenly via dense ranking — see _rrf_rank_map below).
    #
    # The structural prior (flow-tagged TCs exercise the changed code path)
    # is preserved as a multiplicative boost (×1.5) on the RRF score —
    # this matches the previous behaviour and stays consistent with the
    # Step 0 reservation that follows.
    #
    # Sort key (descending priority):
    #   1. rrf_score (corpus-wide; precomputed once via _build_rrf_score_map)
    #   2. tier_bonus  (summary +50, description +10, steps 0)
    #   3. keyword_score
    #   4. max signal3_score
    #   5. issue_key (asc) - stable tie-break for determinism
    if final_cap and len(final_tests) > final_cap:
        rrf_score_map = _build_rrf_score_map(final_tests, k=RRF_K_DEFAULT,
                                              flow_tag_boost=1.5)

        sorted_tests = sorted(
            final_tests,
            key=lambda t: (
                -rrf_score_map.get(t.get('issue_key', '') or '', 0.0),
                -get_tier_bonus(t),
                -t.get('keyword_score', 0),
                -get_max_signal3(t),
                t.get('issue_key', '') or '',
            ),
        )

        # Step 0: RESERVE slots for flow-tagged TCs (call-tree validated).
        # Flow-tagged TCs exercise the changed code path — they must not be
        # squeezed out by keyword-only TCs that merely share vocabulary.
        # Reserve at least 50% of the cap for them.
        flow_tagged_sorted = [t for t in sorted_tests if t.get('matched_flows')]
        # Reserve 70% of cap for flow-tagged (call-tree validated) TCs.
        # These exercise the changed code path — they are higher-confidence
        # than keyword-only TCs that merely share vocabulary.
        flow_reserve = min(len(flow_tagged_sorted), max(final_cap * 7 // 10, 1))

        selected: List[Dict[str, Any]] = []
        seen_keys: set = set()
        for t in flow_tagged_sorted[:flow_reserve]:
            selected.append(t)
            seen_keys.add(t.get('issue_key'))
        if flow_reserve:
            print(f"  [flow-tagged reserve] {flow_reserve} flow-tagged TCs reserved "
                  f"(of {len(flow_tagged_sorted)} available)")

        # Step 1: take top-1 from each flow not already represented (for
        # diversity), in score-rank order.
        flows_seen: set = set()
        for t in selected:
            flows_seen.add(get_primary_flow(t))
        for t in sorted_tests:
            if len(selected) >= final_cap:
                break
            flow = get_primary_flow(t)
            if flow in flows_seen:
                continue
            if t.get('issue_key') in seen_keys:
                continue
            selected.append(t)
            seen_keys.add(t.get('issue_key'))
            flows_seen.add(flow)

        # Step 1b: Concept diversity — ensure every unique primary diff
        # concept (the longest/highest-IDF phrase each test matched) has at
        # least one representative.  This prevents the cap from being
        # dominated by tests matching the same context phrases (e.g.
        # "split shift") while excluding tests matching the primary code-
        # change concept (e.g. "min daily shift gap").
        concepts_seen: set = set()
        for t in selected:
            pc = _get_primary_concept(t)
            if pc:
                concepts_seen.add(pc)
        for t in sorted_tests:
            if len(selected) >= final_cap:
                break
            if t.get('issue_key') in seen_keys:
                continue
            pc = _get_primary_concept(t)
            if not pc or pc in concepts_seen:
                continue
            selected.append(t)
            seen_keys.add(t.get('issue_key'))
            concepts_seen.add(pc)

        # Step 2: fill remaining slots by global score rank.
        for t in sorted_tests:
            if len(selected) >= final_cap:
                break
            if t.get('issue_key') in seen_keys:
                continue
            selected.append(t)
            seen_keys.add(t.get('issue_key'))

        # Step 3: Semantic quality swap — replace tests with very low
        # embedding similarity (false positive keyword matches) with
        # candidates that have high semantic relevance to the code change.
        # Gates are corpus-percentile-derived (see _adaptive_embedding_gates);
        # validated defaults 0.20 / 0.40 are used when stats are absent.
        _EMB_LOW = _EMB_LOW_GATE
        _EMB_HIGH = _EMB_HIGH_GATE
        # Adaptive swap budget: more swaps for larger sets, capped at 10.
        _MAX_SWAPS = max(3, min(10, len(selected) // 6))
        swaps_done = 0
        candidate_pool = [t for t in sorted_tests
                          if t.get('issue_key') not in seen_keys]
        candidate_pool.sort(
            key=lambda t: -(t.get('score_breakdown', {}).get('embedding_sim') or 0.0))
        for candidate in candidate_pool:
            if swaps_done >= _MAX_SWAPS:
                break
            cand_emb = (candidate.get('score_breakdown', {})
                        .get('embedding_sim') or 0.0)
            if cand_emb < _EMB_HIGH:
                break  # no more high-quality candidates
            # Find the lowest-embedding test currently selected
            # Never evict flow-tagged TCs — they are call-tree validated
            evictable = [i for i in range(len(selected))
                         if not selected[i].get('matched_flows')]
            if not evictable:
                break  # only flow-tagged TCs remain — nothing to evict
            worst_idx = min(
                evictable,
                key=lambda i: (selected[i].get('score_breakdown', {})
                               .get('embedding_sim') or 0.0))
            worst_emb = (selected[worst_idx].get('score_breakdown', {})
                         .get('embedding_sim') or 0.0)
            if worst_emb >= _EMB_LOW:
                break  # no more low-quality tests to evict
            evicted = selected[worst_idx]
            selected[worst_idx] = candidate
            seen_keys.discard(evicted.get('issue_key'))
            seen_keys.add(candidate.get('issue_key'))
            swaps_done += 1
            print(f"  [semantic swap] Evicted {evicted.get('issue_key')} "
                  f"(emb={worst_emb:.3f}) -> {candidate.get('issue_key')} "
                  f"(emb={cand_emb:.3f})")
        if swaps_done:
            print(f"  [semantic swap] {swaps_done} swap(s) applied")

        print(f"\nApplied global cap: {len(final_tests)} -> {len(selected)} tests "
              f"(target {final_cap})")
        final_tests = selected

    # Final deterministic ordering of the surviving tests so output JSON
    # contains the same ranking on every run. Preserve the diversity-first
    # selection above by using the same key ordering.
    final_tests = sorted(
        final_tests,
        key=lambda t: (
            -get_tier_bonus(t),
            -t.get('keyword_score', 0),
            -get_max_signal3(t),
            t.get('issue_key', '') or '',
        ),
    )

    return final_tests


def suppress_top_n(tests: List[Dict[str, Any]], top_n: int) -> List[Dict[str, Any]]:
    """Take the top N tests by composite_score (already sorted in Stage 5)."""
    print("\n" + "=" * 80)
    print(f"STRATEGY: TOP {top_n} BY COMPOSITE SCORE")
    print("=" * 80)
    final_tests = tests[:top_n]
    for t in final_tests:
        t['stage6_selection'] = 'top_composite_score'
    if final_tests:
        print(f"\nSelected top {len(final_tests)} tests by composite score")
        print(f"  Score range: {final_tests[-1]['composite_score']} - "
              f"{final_tests[0]['composite_score']}")
    return final_tests


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def aggressive_suppression(input_file: str,
                            output_file: str,
                            flow_registry_file: str,
                            flow_dependencies_file: str,
                            stage4_file: str,
                            changed_method: str,
                            changed_methods: List[str],
                            changed_components: List[str],
                            strategy: str,
                            min_keyword_score: int,
                            reduced_quota: int,
                            top_n: int,
                            kb_dir: str = None,
                            final_cap: int = DEFAULT_FINAL_CAP,
                            anchor_concepts_file: str = None,
                            enriched_corpus_path: str = None) -> Dict[str, Any]:
    print("=" * 80)
    print("STAGE 6: AGGRESSIVE SUPPRESSION")
    print("=" * 80)
    print(f"\nInput:                {input_file}")
    print(f"Output:               {output_file}")
    print(f"Flow registry:        {flow_registry_file}")
    print(f"Flow dependencies:    {flow_dependencies_file}")
    print(f"Stage 4 source:       {stage4_file}")
    print(f"Changed method:       {changed_method}")
    if changed_methods:
        print(f"Changed methods:      {changed_methods}")
    if changed_components:
        print(f"Changed components:   {changed_components}")
    print(f"Strategy:             {strategy}")

    # Resolve final list of changed methods.
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

    # Resolve owning component(s) explicitly from CLI (no stage2 lookup).
    component_names: List[str] = [c for c in (changed_components or []) if c]

    component_keywords = extract_component_keywords(
        method_keywords, kb_dir=kb_dir, component_names=component_names,
    )
    if component_names:
        print(f"\nOwning component(s): {component_names}")
    print(f"\nComponent-level keywords (required for match): {len(component_keywords)}")
    print(f"  {component_keywords}")

    # Load flows from focused KB (replaces stage2 shim).
    flows = load_flows_from_registry(
        flow_registry_file,
        flow_dependencies_file,
        changed_components=component_names,
    )
    # Derive flow quotas dynamically with corpus-coverage adaptation
    flow_quotas = derive_flow_quotas(flows, reduced_quota, kb_dir=kb_dir,
                                     enriched_corpus_path=enriched_corpus_path)

    # Scale final_cap adaptively: cross-cutting changes (many flows) earn
    # extra slots; sparse evidence (few input tests) shrinks the cap to
    # match what's actually available so we never report empty rows.
    n_flows = len(flow_quotas)
    if not os.path.isfile(input_file):
        raise FileNotFoundError(
            f"[Stage 6] Stage 5 output not found at: {input_file}\n"
            f"Root cause: Stage 5 did not run before Stage 6.\n"
            f"Fix: Run Stage 5 (or full pipeline) before Stage 6."
        )
    with open(input_file, 'r', encoding='utf-8') as f:
        _peek = json.load(f) or {}
    n_input_tests = len(_peek.get('refined_tests') or [])
    if n_input_tests == 0:
        raise RuntimeError(
            f"[Stage 6] Stage 5 output at {input_file} contains zero "
            f"refined_tests. Stage 6 has nothing to suppress.\n"
            f"Root cause: Stage 5 filtered out every test.\n"
            f"Fix: Inspect Stage 5 logs and KB completeness."
        )
    scaled_cap = _adaptive_final_cap(final_cap, n_flows, n_input_tests)
    if scaled_cap != final_cap:
        print(f"\n[Stage 6] Adaptive cap: {final_cap} -> {scaled_cap} "
              f"(flows={n_flows}, input_tests={n_input_tests})")
        final_cap = scaled_cap

    print(f"\nDerived adaptive flow quotas from flow_registry ({len(flow_quotas)} flows):")
    for flow_name, quota in flow_quotas.items():
        print(f"  - {flow_name}: quota={quota}")

    # Load anchor concepts (method-name domain terms) for relevance gating.
    # FAIL-FAST when an anchor_concepts_file is supplied but unreadable.
    anchor_concepts: List[str] = []
    if anchor_concepts_file:
        if not os.path.isfile(anchor_concepts_file):
            raise FileNotFoundError(
                f"[Stage 6] anchor_concepts_file not found at: "
                f"{anchor_concepts_file}\n"
                f"Fix: Verify the path or omit --anchor-concepts."
            )
        with open(anchor_concepts_file, 'r', encoding='utf-8') as f:
            ac_data = json.load(f)
        anchor_concepts = ac_data.get('anchor_concepts', []) or []
        if not anchor_concepts:
            # An empty anchor list is legitimate when the changed method/class
            # names decompose only to generic, high-frequency words (e.g.
            # createObjectMapper / BatchUtils -> create, object, mapper, batch,
            # utils). Anchor gating is an optional relevance filter; proceed
            # with no gating instead of aborting (same behavior as when no
            # anchor file is supplied at all).
            print(
                f"\n[Stage 6] WARNING: anchor_concepts_file at "
                f"{anchor_concepts_file} contains no anchor_concepts "
                f"(method/class names are all generic). Proceeding with no "
                f"anchor-concept gating."
            )
        else:
            print(f"\nAnchor concepts (from method names): {anchor_concepts}")

    # Read actual Stage 4 count.
    stage4_count = read_stage4_count(stage4_file)
    print(f"\nStage 4 produced {stage4_count} tests")

    # Reuse the already-loaded peek of refined_tests (validated above).
    data = _peek
    all_tests = data.get('refined_tests') or []
    print(f"Stage 5 input: {len(all_tests)} tests")

    # Self-heal: in multi-method (consolidated) mode, ria_agent skips the
    # Stage 5 refinement and copies Stage 4 output directly to the Stage 5
    # path (see _run_multi_method_analysis_body in ria_agent.py), so
    # `keyword_score` / `matched_keywords` / `component_match` were never
    # computed. Stage 6 must be able to stand alone, so we backfill these
    # fields on any test that lacks `keyword_score`. This mirrors the
    # equivalent self-healing pattern already used for `tier_bonus` and
    # `component_match` elsewhere in this file.
    missing_kw = sum(1 for t in all_tests if 'keyword_score' not in t)
    if missing_kw:
        print(f"\n[stage6] Backfilling keyword_score on {missing_kw} test(s) "
              f"that came through Stage 5 without enrichment "
              f"(multi-method copy-through mode).")
        for t in all_tests:
            if 'keyword_score' in t:
                continue
            score, kws, comp_match = calculate_keyword_score(
                t, method_keywords, component_keywords,
            )
            t['keyword_score'] = score
            t['matched_keywords'] = kws
            # Only set component_match when not already populated upstream
            # so we don't clobber a stricter Stage 5 decision.
            if t.get('component_match') is None:
                t['component_match'] = comp_match

    if strategy == 'keyword_plus_diversity':
        final_tests = suppress_keyword_plus_diversity(
            all_tests, min_keyword_score, flow_quotas, component_keywords,
            final_cap=final_cap, kb_dir=kb_dir,
            anchor_concepts=anchor_concepts,
        )
    elif strategy == 'top_n':
        final_tests = suppress_top_n(all_tests, top_n)
    else:
        raise ValueError(f"Unknown strategy: {strategy}")

    # Stats
    print("\n" + "=" * 80)
    print("FINAL RESULTS")
    print("=" * 80)
    print(f"\nTotal tests: {len(final_tests)}")
    if all_tests:
        pct = ((len(all_tests) - len(final_tests)) / len(all_tests)) * 100
        print(f"Reduction from Stage 5: "
              f"{len(all_tests) - len(final_tests)} tests removed ({pct:.1f}%)")
    if stage4_count > 0:
        pct4 = ((stage4_count - len(final_tests)) / stage4_count) * 100
        print(f"Reduction from Stage 4: "
              f"{stage4_count - len(final_tests)} tests removed ({pct4:.1f}%)")

    # Fix P0: Strip unreliable impact_type from test output. The Stage 7 LLM
    # provides accurate DIRECT/INDIRECT classifications; the per-test labels
    # carried through Stages 2-6 are heuristic and should not be displayed.
    for test in final_tests:
        if 'impact_type' in test:
            del test['impact_type']
        # Also strip from matched_flows if present so nested labels don't
        # leak into the HTML report.
        if 'matched_flows' in test:
            for flow in test.get('matched_flows', []):
                if isinstance(flow, dict) and 'impact_type' in flow:
                    del flow['impact_type']

    # Persist.
    output_data = {
        'stage': 6,
        'description': 'Aggressive suppression for maximum precision',
        'run_id': data.get('run_id'),
        'generated_at': data.get('generated_at'),
        'changed_method': changed_method,
        'strategy': strategy,
        'parameters': {
            'min_keyword_score': min_keyword_score if strategy == 'keyword_plus_diversity' else None,
            'flow_quotas': flow_quotas if strategy == 'keyword_plus_diversity' else None,
            'final_cap': final_cap if strategy == 'keyword_plus_diversity' else None,
            'top_n': top_n if strategy == 'top_n' else None,
        },
        'input_tests': len(all_tests),
        'output_tests': len(final_tests),
        'reduction_from_stage5': len(all_tests) - len(final_tests),
        'reduction_from_stage4': (stage4_count - len(final_tests)) if stage4_count > 0 else None,
        'stage4_input_count': stage4_count if stage4_count > 0 else None,
        'aggressive_tests': final_tests,
    }

    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, indent=2)

    print(f"\n{'=' * 80}")
    print(f"Aggressive suppression complete: {output_file}")
    print(f"{'=' * 80}\n")
    return output_data


def _split_csv(value: str) -> List[str]:
    if not value:
        return []
    return [v.strip() for v in value.split(',') if v.strip()]


def main():
    parser = argparse.ArgumentParser(
        description='Stage 6: dynamic, method-agnostic aggressive suppression '
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
                        help='Path to stage5_refined_tests.json')
    parser.add_argument('--output-file', default=str(DEFAULT_OUTPUT),
                        help='Path to write stage6_aggressive_tests.json')
    parser.add_argument('--flow-registry', default=str(DEFAULT_FLOW_REGISTRY),
                        help='Path to flow_registry.json (for flow quotas)')
    parser.add_argument('--flow-dependencies',
                        default=str(DEFAULT_FLOW_DEPENDENCIES),
                        help='Path to flow_dependencies.json '
                             '(for DIRECT/INDIRECT classification)')
    parser.add_argument('--stage4-tests', default=str(DEFAULT_STAGE4),
                        help='Path to stage4_recommended_tests.json '
                             '(for accurate reduction reporting)')
    parser.add_argument('--strategy', default='keyword_plus_diversity',
                        choices=['keyword_plus_diversity', 'top_n'],
                        help='Suppression strategy')
    parser.add_argument('--min-keyword-score', type=int,
                        default=DEFAULT_MIN_KEYWORD_SCORE,
                        help='Minimum keyword score for keyword_plus_diversity')
    parser.add_argument('--reduced-quota', type=int, default=DEFAULT_REDUCED_QUOTA,
                        help='Per-flow quota for diversity boost')
    parser.add_argument('--top-n', type=int, default=DEFAULT_TOP_N,
                        help='Top-N count when --strategy=top_n')
    parser.add_argument('--final-cap', type=int, default=DEFAULT_FINAL_CAP,
                        help='Global cap on final tests for keyword_plus_diversity '
                             '(target ~20-30 tests). 0 disables the cap.')
    parser.add_argument('--kb-dir', default=str(DEFAULT_KB_DIR),
                        help='Knowledge base directory for corpus-coverage analysis')
    parser.add_argument('--anchor-concepts', default=None,
                        help='Path to anchor_concepts.json (method-name domain terms)')
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
        # Last-resort: try Stage 5 input file
        try:
            with open(args.input_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            cms = data.get('changed_methods', [])
            if cms:
                changed_methods = cms
                changed_method = cms[0]
            else:
                changed_method = data.get('changed_method')
        except Exception:
            pass
    if not changed_method:
        print("ERROR: --changed-method (or --changed-methods) not provided "
              "and could not be inferred from the Stage 5 input file.")
        sys.exit(2)
    if not changed_methods:
        changed_methods = [changed_method]

    changed_components = _split_csv(args.changed_components)

    aggressive_suppression(
        input_file=args.input_file,
        output_file=args.output_file,
        flow_registry_file=args.flow_registry,
        flow_dependencies_file=args.flow_dependencies,
        stage4_file=args.stage4_tests,
        changed_method=changed_method,
        changed_methods=changed_methods,
        changed_components=changed_components,
        strategy=args.strategy,
        min_keyword_score=args.min_keyword_score,
        reduced_quota=args.reduced_quota,
        top_n=args.top_n,
        kb_dir=args.kb_dir,
        final_cap=args.final_cap,
        anchor_concepts_file=args.anchor_concepts,
        enriched_corpus_path=args.enriched_corpus_path,
    )


if __name__ == '__main__':
    main()
