#!/usr/bin/env python3
"""
Stage 7 — LLM TC Judgment

For each test case shortlisted by Stage 6, uses LLM to judge:
  1. Does this TC exercise the affected flow (entry point → changed method)?
  2. Does this TC validate a test scenario (changed behavior)?

Input:
  - stage6_aggressive_tests.json (shortlisted TCs with steps)
  - method_understanding.json (from Stage 1.5)
  - .detect_changes_cache.json (call-chain data)

Output:
  - stage7_llm_tc_judgment.json (TC verdicts + reasoning + scenario gaps)
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR / '..' / 'configs'))

import agent_reasoning      # noqa: E402 (Copilot agent answers directly; no mailbox)
import ria_config           # noqa: E402

SYSTEM_PROMPT = """You are a test relevance analyst. You must be SURGICALLY PRECISE.

You will receive:
1. The EXACT code change (before/after diff) showing which method/variable/condition was modified.
2. A list of behaviors that are NOT affected by this change (even if in the same method).
3. Test scenarios tied to the specific changed method.
4. Test cases with their steps.

CRITICAL RULES FOR VERDICT (METHOD-LEVEL CRITERIA):
- DIRECT: The test case validates the changed method's primary behavior — i.e. its
  steps drive the exact functional behavior, contract, or output of the modified
  method (assertions/expected results target what the method now does differently).
- INDIRECT: The test case calls the changed method through any execution path
  (entry point → ... → changed method) but does not specifically validate the
  changed behavior. Any test that reaches the changed method during execution —
  even tangentially — is at minimum INDIRECT, never NOT_RELEVANT.
- NOT_RELEVANT: The test case provably does NOT call the changed method through
  any execution path. If there is any plausible call-chain from the test's entry
  point to the changed method, do NOT mark it NOT_RELEVANT — mark it INDIRECT.

COMMON MISTAKE TO AVOID:
If a test exercises an entry point that reaches the changed method (flow_match=true),
it cannot be NOT_RELEVANT. The minimum verdict in that case is INDIRECT. Reserve
NOT_RELEVANT exclusively for tests with no execution path to the changed method.

UPSTREAM SCORING:
Each TC may include Upstream Scores (IDF, Embedding, Keyword) from pipeline stages 4-6.
High scores mean the pipeline found strong lexical/semantic overlap with the code change.
Use these as prior evidence:
- High IDF (>5) + High Embedding (>0.5) = strong prior for DIRECT/INDIRECT — override only if test steps clearly contradict.
- Low scores = weaker prior — rely more on step-by-step analysis.

Respond ONLY with valid JSON — no markdown fences, no commentary."""


def _format_tc_for_prompt(tc: dict, index: int) -> str:
    """Format a test case for inclusion in the LLM prompt."""
    parts = [f"TC {index + 1}:"]
    parts.append(f"  ID: {tc.get('issue_key', 'unknown')}")
    parts.append(f"  Title: {tc.get('summary', '')}")

    desc = tc.get('description', '')
    if desc:
        parts.append(f"  Description: {desc[:300]}")

    steps = tc.get('steps', [])
    if steps:
        parts.append("  Steps:")
        for i, step in enumerate(steps[:15]):  # Limit to 15 steps
            action = step.get('action', '') if isinstance(step, dict) else str(step)
            data = step.get('data', '') if isinstance(step, dict) else ''
            result = step.get('result', '') if isinstance(step, dict) else ''
            parts.append(f"    Step {i+1}: {action[:200]}")
            if data:
                parts.append(f"      Data: {data[:150]}")
            if result:
                parts.append(f"      Expected: {result[:150]}")
    else:
        parts.append("  Steps: (not available)")

    tags = tc.get('matched_flows', [])
    if tags:
        flow_names = [f.get('flow_name', '') for f in tags if isinstance(f, dict)]
        if flow_names:
            parts.append(f"  Matched Flows: {', '.join(flow_names)}")

    # Pass upstream scoring signals so the LLM has prior evidence
    idf = tc.get('idf_score') or (tc.get('score_breakdown', {}) or {}).get('idf_score')
    emb = tc.get('embedding_sim') or (tc.get('score_breakdown', {}) or {}).get('embedding_sim')
    kw = tc.get('keyword_score', 0)
    signals = []
    if idf is not None and idf > 0:
        signals.append(f"IDF={round(float(idf), 2)}")
    if emb is not None and float(emb) > 0:
        signals.append(f"Embedding={round(float(emb), 3)}")
    if kw:
        signals.append(f"Keyword={kw}")
    if signals:
        parts.append(f"  Upstream Scores: {', '.join(signals)}")

    return '\n'.join(parts)


def _build_context_block(method_understanding: dict, call_chains_text: list) -> str:
    """Build the context block from method understanding data."""
    methods = method_understanding.get('methods', [])
    parts = []

    for m in methods:
        if 'error' in m:
            continue
        parts.append(f"CHANGED METHOD: {m.get('class_name', '')}.{m.get('method_name', '')}()")
        parts.append(f"Purpose: {m.get('purpose', 'unknown')}")
        parts.append(f"Change Impact: {m.get('change_impact', 'unknown')}")

        # Include exact change — this is critical for precision
        exact_change = m.get('exact_change', '')
        if exact_change:
            parts.append(f"EXACT CODE CHANGE: {exact_change}")

        # Include git diff hunks if available
        diff_hunks = m.get('diff_hunks', '')
        if diff_hunks and diff_hunks != "(git diff not available)" and diff_hunks != "(no diff hunks in method range)":
            parts.append(f"GIT DIFF (- = removed, + = added):\n{diff_hunks}")

        # Changed variables
        changed_vars = m.get('changed_variables', [])
        if changed_vars:
            parts.append(f"CHANGED VARIABLES: {', '.join(changed_vars)}")

        behaviors = m.get('affected_behaviors', [])
        if behaviors:
            parts.append(f"Affected Behaviors: {', '.join(behaviors)}")

        params = m.get('controlling_parameters', [])
        if params:
            parts.append(f"Controlling Parameters: {', '.join(params)}")

        # NOT affected — critical for distinguishing DIRECT vs INDIRECT
        not_affected = m.get('NOT_affected', [])
        if not_affected:
            parts.append(f"⚠️ NOT AFFECTED by this change (tests for these are INDIRECT, not DIRECT): {', '.join(not_affected)}")

        parts.append("")

    # Scenarios
    all_scenarios = []
    for m in methods:
        for s in agent_reasoning.normalize_scenarios(m.get('test_scenarios', [])):
            all_scenarios.append(s)

    if all_scenarios:
        parts.append("TEST SCENARIOS TO VALIDATE:")
        for s in all_scenarios:
            sid = s.get('id', '?')
            desc = s.get('description', '')
            pri = s.get('priority', '')
            parts.append(f"  {sid} [{pri}]: {desc}")
        parts.append("")

    # Call chains
    if call_chains_text:
        parts.append("CALL-CHAIN (entry point → changed method):")
        for ct in call_chains_text:
            if ct and ct != "(call-chain not available)":
                parts.append(f"  {ct}")
        parts.append("")

    return '\n'.join(parts)


def run(repo_root: str = '.'):
    """Run Stage 7 — TC Judgment.

    Two-phase, agent-driven:
      • PREPARE (no agent answer yet): format the candidate TCs + code-change
        context, write a PENDING baseline to stage7_llm_tc_judgment.json with
        empty judgments, and return the pending result so the pipeline pauses.
      • FINALIZE (agent filled "judgments" + set _reasoning_source=copilot-agent):
        run the deterministic post-processing (hard-rule override, scenario-gap
        analysis, summaries) on the agent verdicts and write the final output.
    """
    output_dir = os.path.join(repo_root, ria_config.RIA_OUTPUT_DIR)
    out_path = os.path.join(output_dir, 'stage7_llm_tc_judgment.json')

    # Load method understanding
    mu_path = os.path.join(output_dir, 'method_understanding.json')
    if not os.path.exists(mu_path):
        print("[Stage 7] ERROR: method_understanding.json not found. Run Stage 1.5 first.")
        return None

    with open(mu_path, 'r') as f:
        method_understanding = json.load(f)

    # Load stage 6 output
    s6_path = os.path.join(output_dir, 'stage6_aggressive_tests.json')
    if not os.path.exists(s6_path):
        print("[Stage 7] ERROR: stage6_aggressive_tests.json not found. Run Stage 6 first.")
        return None

    with open(s6_path, 'r') as f:
        stage6 = json.load(f)

    tcs = stage6.get('aggressive_tests', [])
    if not tcs:
        print("[Stage 7] No test cases from Stage 6.")
        return None

    print(f"[Stage 7] Judging {len(tcs)} test cases from Stage 6")

    # Build context
    call_chains_text = method_understanding.get('call_chains_text', [])
    context_block = _build_context_block(method_understanding, call_chains_text)

    # Collect all scenarios for gap analysis.
    # Fix #6: Scenario IDs (S1, S2, S3) are reused across methods — without
    # disambiguating by method we double-count and the scenario_coverage
    # math breaks (total != covered + gaps). Tag each scenario with its
    # owning method so identity is unique across the corpus.
    all_scenarios = []
    for m in method_understanding.get('methods', []):
        method_name = m.get('method_name', 'unknown')
        for s in agent_reasoning.normalize_scenarios(m.get('test_scenarios', [])):
            tagged = dict(s)
            tagged['_owning_method'] = method_name
            sid = s.get('id', '')
            tagged['_unique_key'] = f"{method_name}:{sid}" if sid else f"{method_name}:{s.get('description', '')}"
            all_scenarios.append(tagged)

    scenario_ids = []
    scenario_labels = []
    for i, s in enumerate(all_scenarios):
        sid = s.get('id', f'S{i+1}')
        desc = s.get('description', sid)
        scenario_ids.append(sid)
        scenario_labels.append(f'{sid}: {desc}')

    all_judgments = []

    # ---- FINALIZE vs PREPARE branch -------------------------------------
    if agent_reasoning.agent_provided(out_path):
        # The Copilot agent has judged these TCs. Load the verdicts and run
        # the deterministic post-processing below (override + gap analysis).
        agent_data = agent_reasoning.load_json(out_path) or {}
        all_judgments = agent_data.get('judgments', []) or []
        # Backfill summaries from Stage 6 where the agent omitted them.
        _summ = {tc.get('issue_key', ''): tc.get('summary', '') for tc in tcs}
        for j in all_judgments:
            jid = j.get('test_id') or j.get('issue_key') or ''
            j.setdefault('issue_key', jid)
            if not j.get('summary'):
                j['summary'] = _summ.get(jid, '')
        print(f"[Stage 7] Using Copilot-provided judgments ({len(all_judgments)} TCs)")
    else:
        # No agent answer yet: write a PENDING baseline with the formatted TCs
        # and code-change context, then pause the pipeline.
        tcs_to_judge = []
        for i, tc in enumerate(tcs):
            tcs_to_judge.append({
                'test_id': tc.get('issue_key', ''),
                'summary': tc.get('summary', ''),
                'formatted': _format_tc_for_prompt(tc, i),
            })
        pending = agent_reasoning.mark_pending({
            'stage': '7',
            'description': 'TC Judgment (Copilot-agent reasoning)',
            'code_change_context': context_block,
            'available_scenarios': scenario_labels or ['(none — use NONE)'],
            'total_tcs': len(tcs),
            'tcs_to_judge': tcs_to_judge,
            'judgments': [],
        })
        agent_reasoning.write_json(out_path, pending)
        agent_reasoning.print_action_required(
            stage="STAGE 7 — Test-Case Judgment",
            reads=[out_path + "  (code_change_context, available_scenarios, tcs_to_judge)"],
            writes=out_path,
            instructions="""
For EACH entry in "tcs_to_judge", append one object to top-level "judgments":
  {test_id, flow_match(bool), scenario_match(full description or "NONE"),
   verdict("DIRECT"|"INDIRECT"|"NOT_RELEVANT"), reasoning, confidence(0..1)}.
Verdict rules (see .github/agents/ria.agent.md): DIRECT = validates the changed
method's primary behavior; INDIRECT = reaches the changed method via some path
(if flow_match is true, minimum verdict is INDIRECT); NOT_RELEVANT = no path to
the changed method. Set top-level "_reasoning_source" to "copilot-agent" and
remove "_needs_agent_reasoning". Then re-run: python3 ria_agent.py --resume
""",
        )
        print(f"[Stage 7] Pending baseline written: {out_path}")
        return pending

    # Sort judgments by test_id so output is deterministic regardless of the
    # order in which parallel futures completed.
    all_judgments.sort(key=lambda x: x.get('test_id', '') or x.get('issue_key', ''))

    # ------------------------------------------------------------------
    # HARD RULE OVERRIDE: flow_match=True implies at minimum INDIRECT.
    # ------------------------------------------------------------------
    # Rationale: a test that the upstream pipeline has confirmed exercises
    # the changed method's flow (entry point → ... → changed method) cannot
    # be NOT_RELEVANT by definition — its execution path reaches the
    # changed code. The LLM occasionally mislabels such tests as
    # NOT_RELEVANT when surface-level lexical overlap is weak; this
    # post-hoc rule corrects the verdict so downstream consumers (HTML
    # report, suppression, scenario coverage) treat the test as impacted.
    #
    # Edge cases handled:
    #   - flow_match missing / None / non-bool truthy: only the strict
    #     boolean True triggers the override. Anything else is left alone.
    #   - verdict is already DIRECT or INDIRECT: untouched (override only
    #     promotes NOT_RELEVANT → INDIRECT, never demotes).
    #   - verdict is UNJUDGED: untouched (we don't fabricate a verdict
    #     when the LLM call failed; that signal is meaningful).
    override_count = 0
    for j in all_judgments:
        flow_match = j.get('flow_match')
        verdict = j.get('verdict')
        if flow_match is True and verdict == 'NOT_RELEVANT':
            tc_key = j.get('issue_key') or j.get('test_id') or '?'
            print(
                f"[Stage 7] HARD RULE OVERRIDE: {tc_key} "
                f"flow_match=True -> promoting NOT_RELEVANT to INDIRECT"
            )
            j['verdict'] = 'INDIRECT'
            # Annotate reasoning so the report shows why the verdict was
            # changed without losing the LLM's original explanation.
            original_reason = j.get('reasoning', '') or ''
            j['reasoning'] = (
                "[Auto-corrected: flow_match=True so test reaches the changed "
                "method; promoted NOT_RELEVANT -> INDIRECT.] " + original_reason
            ).strip()
            j['verdict_overridden'] = True
            j['original_verdict'] = 'NOT_RELEVANT'
            override_count += 1
    if override_count:
        print(
            f"[Stage 7] Applied {override_count} hard-rule override(s) "
            f"(flow_match=True suppression-bypass)"
        )

    # Scenario gap analysis — compare both ID and description since
    # the LLM may return either form depending on prompt version.
    # Fix #6: Track coverage and gaps using the per-method unique keys
    # built above so that S1/S2/S3 across different methods don't
    # collide. This guarantees total == covered + gaps.
    raw_covered = set()
    for j in all_judgments:
        sm = j.get('scenario_match', 'NONE')
        if sm and sm != 'NONE':
            raw_covered.add(sm)

    covered_unique_keys = set()
    gaps = []
    seen_unique_keys = set()
    for s in all_scenarios:
        unique_key = s.get('_unique_key') or s.get('id', '')
        if unique_key in seen_unique_keys:
            # Defensive: skip exact duplicates so total == covered + gaps.
            continue
        seen_unique_keys.add(unique_key)

        sid = s.get('id', '')
        desc = s.get('description', '')
        if sid in raw_covered or (desc and desc in raw_covered):
            covered_unique_keys.add(unique_key)
        else:
            # Fix #6: Persist the globally unique scenario id (method:S#)
            # alongside the per-method `local_id`. Without this, two
            # different methods both reporting an "S1" gap would look
            # like one row in any consumer that keys on `scenario_id`.
            gaps.append({
                'scenario_id': unique_key or sid,
                'local_id': sid,
                'owning_method': s.get('_owning_method', ''),
                'description': desc,
                'priority': s.get('priority', ''),
                'status': 'NO_COVERAGE',
                'recommendation': f"Create a test case that validates: {desc}",
            })

    # Coverage set used for scenario_coverage["covered"] count below.
    covered_scenarios = covered_unique_keys

    # Fix #6: Persist a globally unique scenario id of the form
    # `method_name:scenario_id` in the serialised output so downstream
    # consumers can disambiguate between the S1/S2/S3 emitted by each
    # method. The original per-method `id` is preserved as `local_id` for
    # back-compat (existing reports group by `id`). Owning method is
    # surfaced explicitly so reports can show provenance without having to
    # carry the helper underscore-prefixed keys.
    sanitized_scenarios = []
    for s in all_scenarios:
        clean = {k: v for k, v in s.items() if not k.startswith('_')}
        unique_key = s.get('_unique_key') or ''
        owning_method = s.get('_owning_method') or ''
        local_id = clean.get('id', '')
        if unique_key:
            # `id` becomes the globally unique key; expose `local_id` for
            # readers that previously grouped on the bare S1/S2/S3 form.
            clean['id'] = unique_key
            clean['local_id'] = local_id
        if owning_method:
            clean['method_name'] = owning_method
        sanitized_scenarios.append(clean)

    # Build summary
    verdicts_summary = {
        'DIRECT': sum(1 for j in all_judgments if j.get('verdict') == 'DIRECT'),
        'INDIRECT': sum(1 for j in all_judgments if j.get('verdict') == 'INDIRECT'),
        'NOT_RELEVANT': sum(1 for j in all_judgments if j.get('verdict') == 'NOT_RELEVANT'),
        'UNJUDGED': sum(1 for j in all_judgments if j.get('verdict') == 'UNJUDGED'),
    }

    output = {
        'stage': '7',
        'description': 'TC Judgment (Copilot-agent reasoning)',
        # Preserve the agent-source marker so re-runs skip re-preparing this
        # stage and keep the finalized judgments intact.
        '_reasoning_source': agent_reasoning.AGENT_SOURCE,
        'total_tcs_judged': len(all_judgments),
        'verdicts_summary': verdicts_summary,
        'scenario_coverage': {
            # Fix #6: total uses the de-duplicated unique-key count so
            # total == covered + gaps holds exactly.
            'total_scenarios': len(seen_unique_keys),
            'covered': len(covered_scenarios),
            'gaps': len(gaps),
        },
        'judgments': all_judgments,
        'scenarios': sanitized_scenarios,
        'scenario_gaps': gaps,
        'method_understanding_summary': {
            m.get('method_name', ''): {
                'purpose': m.get('purpose', ''),
                'change_impact': m.get('change_impact', ''),
            }
            for m in method_understanding.get('methods', []) if 'error' not in m
        },
        'call_chains': call_chains_text,
    }

    # Fix #3: Renamed from stage7_llm_synthesis.json to stage7_llm_tc_judgment.json
    # for consistency with the script name and audit documentation.
    out_path = os.path.join(output_dir, 'stage7_llm_tc_judgment.json')
    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"\n[Stage 7] Output: {out_path}")
    print(f"[Stage 7] Results: {verdicts_summary}")
    print(f"[Stage 7] Scenario Gaps: {len(gaps)}")
    return output


if __name__ == '__main__':
    repo_root = sys.argv[1] if len(sys.argv) > 1 else '.'
    run(repo_root)
