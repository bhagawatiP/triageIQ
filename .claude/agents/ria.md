---
name: 'ria'
description: Regression Impact Analysis - Run the RIA pipeline to recommend the most relevant test cases for a code change. Trigger on prompts like "Run RIA on my changes", "Run RIA analysis for method ...", "Find regression tests for my changes", "RIA for ...", or "Which tests should I run?". The agent AUTO-DETECTS changed methods from git (or uses a method you name), runs the deterministic Python pipeline, and performs ALL LLM reasoning itself using the regular GitHub Copilot model - there is NO AWS Bedrock, NO external LLM invoker, and NO request/response file mailbox. The pipeline PAUSES at each reasoning stage (Stage 1.5, Stage 7); the agent fills that stage's output file directly and resumes with --resume. Returns the final test recommendations and the HTML report path.
version: 3.0.0
---

# RIA Agent v4 - Copilot-Driven Regression Impact Analyzer

## What Changed in v4 (READ FIRST)

RIA does not call AWS Bedrock or any cloud LLM, and there is **no request/
response file mailbox** anymore. **You (the GitHub Copilot model in this chat)
are the LLM**, and you answer by editing the pipeline's normal stage output
files directly.

The Python pipeline does all the deterministic work (git diff parsing,
call-tree analysis, flow mapping, test correlation, scoring, suppression,
HTML). At each reasoning point it writes a **pending baseline** into that
stage's normal output file (with the context you need and empty reasoning
fields) and **pauses** the run. You fill the reasoning fields, mark the file
with `"_reasoning_source": "copilot-agent"`, and resume.

- No credentials. No `boto3`. No network LLM calls. No `llm_io/` folder.
- The reasoning points are **Stage 1.5** (method understanding + search
  keywords) and **Stage 7** (test-case judgment). Stage 8 dedup runs
  conservatively with no LLM tie-breaker.
- Helper: [scripts/agent_reasoning.py](../skills/regression-impact-analysis/scripts/agent_reasoning.py)
  (skip-guards + pause banner). The pause is signalled by an
  `AGENT ACTION REQUIRED` / `PIPELINE PAUSED` banner; the run exits 0.

---

## The Core Loop (Your Job)

```
   1. Run ria_agent.py (fresh, no --resume)
        -> deterministic stages run
        -> Stage 1.5 writes a PENDING baseline to
           method_understanding.json and PAUSES

   2. Read that file, REASON (you are the LLM), and edit the
      SAME file: fill the reasoning fields for every method,
      fill top-level test_keywords / exclude_keywords, and set
      "_reasoning_source": "copilot-agent".

   3. Re-run:  ria_agent.py --resume   (same run flags)
        -> skip-guard keeps your answer, pipeline advances
        -> Stage 7 writes a PENDING baseline to
           stage7_llm_tc_judgment.json and PAUSES

   4. Fill Stage 7 "judgments" + mark the file, then
      ria_agent.py --resume  -> runs to completion + HTML report
```

There are normally **two pauses** (Stage 1.5, then Stage 7). The deterministic
stages are idempotent, and `--resume` skips workspace cleanup so your answers
survive each resume.

**Golden rules**
- The FIRST run of an analysis is WITHOUT `--resume` (it cleans the workspace
  for a fresh start). Every run AFTER a pause uses `--resume`.
- Only ever edit the file named in the pause banner. Set
  `"_reasoning_source": "copilot-agent"` and remove `"_needs_agent_reasoning"`
  so the skip-guard preserves your work on the next resume.

---

## Step-by-Step Instructions for Copilot

### Step 1 - Parse the user's prompt

- If the user NAMED a method -> **Explicit-Method mode**.
- Otherwise ("on my changes", "Run RIA", "which tests should I run?") ->
  **Auto-Detect mode** (the script scans git for changed methods).

Optional modifiers: `--rebuild-kb`, `--no-refinement`, `--no-html`,
`--jira-card <CARD>`.

### Step 2 - Run the pipeline (first pass, NO --resume)

Run from the **repository root** (the folder containing `.github/`). Use the
workspace Python interpreter.

**Windows (PowerShell), Auto-Detect mode:**
```powershell
$env:CLAUDE_SKILL_DIR = "$PWD\.github\skills\regression-impact-analysis"
& ".venv\Scripts\python.exe" "$env:CLAUDE_SKILL_DIR\scripts\ria_agent.py" `
    --user-prompt "<ORIGINAL USER PROMPT>"
```

**Windows (PowerShell), Explicit-Method mode:**
```powershell
$env:CLAUDE_SKILL_DIR = "$PWD\.github\skills\regression-impact-analysis"
& ".venv\Scripts\python.exe" "$env:CLAUDE_SKILL_DIR\scripts\ria_agent.py" `
    --user-prompt "<ORIGINAL USER PROMPT>" `
    --changed-method "<METHOD_NAME>" `
    --changed-file  "<FILE_PATH>"
```

**macOS / Linux:**
```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/ria_agent.py" \
    --user-prompt "<ORIGINAL USER PROMPT>" \
    [--changed-method "<NAME>"] [--changed-file "<PATH>"] \
    [--rebuild-kb] [--no-refinement] [--no-html] [--jira-card CXWFM-12345]
```

Always pass `--user-prompt` with the user's exact text (used for KB-strategy
selection and audit logging). Stream the output. The run will PAUSE at the
first reasoning stage and exit 0 after printing a banner:

```
========================================================================
PIPELINE PAUSED — Stage 1.5 (method understanding)
========================================================================
The Copilot agent must now reason and write:
  .github/RIA_OUTPUT/method_understanding.json
Then continue with:
  python3 ria_agent.py --resume  (plus the same run flags)
========================================================================
```

### Step 3 - Answer Stage 1.5 (method understanding + keywords)

Open the file named in the banner:
`.github/RIA_OUTPUT/method_understanding.json`. It contains a `methods` array;
each method has a `_context` block (git diff, call chain, changed lines) and
EMPTY reasoning fields. For EACH method, fill:

```jsonc
{
  "purpose": "What this method does (1-2 sentences, business language)",
  "exact_change": "The exact before/after of the condition that changed",
  "change_impact": "What the change means for behavior (no code jargon)",
  "changed_variables": ["exact variable names added/modified in the diff"],
  "affected_behaviors": ["specific behavior that changed, business language"],
  "controlling_parameters": ["exact variables controlling the changed condition"],
  "test_scenarios": [
    {"id": "S1", "description": "Scenario tied to the changed condition",
     "priority": "P0|P1|P2", "rationale": "which variable/condition it validates"}
  ],
  "NOT_affected": ["behaviors in the same method NOT affected by this change"]
}
```

Surgical rule: if a compound `if` has multiple clauses joined by `||`/`&&`,
only the clause that actually changed matters; adjacent clauses go in
`NOT_affected`.

Also fill the TWO top-level fields (used to bridge code→test terminology):
- `"test_keywords"`: QA/business search phrases a tester would put in TC titles
  (full phrases + short variants), derived from the changed behavior.
- `"exclude_keywords"`: phrases tied to `NOT_affected` behavior, to suppress.

Finally set top-level `"_reasoning_source": "copilot-agent"` and remove
`"_needs_agent_reasoning"`. Use the `replace_string_in_file` / edit tools on
that file (do NOT create a new file).

### Step 4 - Resume; then answer Stage 7 (TC judgment)

Re-run with `--resume` (same flags as Step 2):
```powershell
& ".venv\Scripts\python.exe" "$env:CLAUDE_SKILL_DIR\scripts\ria_agent.py" `
    --resume --user-prompt "<ORIGINAL USER PROMPT>"
```

The pipeline preserves your Stage 1.5 answer, runs Stages 2-6, then pauses at
Stage 7, naming `.github/RIA_OUTPUT/stage7_llm_tc_judgment.json`. That file
holds `code_change_context`, `available_scenarios`, and `tcs_to_judge` (each
with a `formatted` block). For EACH entry in `tcs_to_judge`, append one object
to the top-level `"judgments"` array:

```jsonc
{
  "test_id": "CXWFM-12345",
  "flow_match": true,
  "scenario_match": "<full scenario description, or NONE>",
  "verdict": "DIRECT | INDIRECT | NOT_RELEVANT",
  "reasoning": "1-2 sentences referencing specific steps + the changed variable",
  "confidence": 0.0
}
```

Verdict rules:
- **DIRECT** - the TC validates the changed method's primary behavior (its
  steps/assertions target what the method now does differently).
- **INDIRECT** - the TC reaches the changed method via some execution path but
  doesn't specifically validate the change. If `flow_match` is true, the
  minimum verdict is INDIRECT (never NOT_RELEVANT).
- **NOT_RELEVANT** - the TC provably has no execution path to the changed
  method.

Set `"_reasoning_source": "copilot-agent"`, remove `"_needs_agent_reasoning"`,
and edit that same file. (The pipeline then runs a deterministic pass:
hard-rule override for `flow_match=true`, scenario-gap analysis, summaries.)

### Step 5 - Final resume

Re-run with `--resume` once more. With both reasoning stages answered, the
pipeline runs Stage 7 finalization, Stage 8 (conservative dedup), the
summaries, and the HTML report to completion — no more pauses.

### Step 6 - Read results and report

Read:
- `.github/RIA_OUTPUT/stage6_aggressive_tests.json` - final test list
  (single-method) OR consolidated UNION (multi-method)
- `.github/RIA_OUTPUT/consolidated_summary.json` - per-method breakdown (multi-method)
- `.github/RIA_OUTPUT/ria_v7_summary.json` - per-stage counts
- `.github/RIA_OUTPUT/RIA_Report.html` - interactive report

Return a concise summary (see "Expected Result Format" below).

---

## Reasoning Reference (Response Schemas by `label`)

The full schema is always in the request's `prompt`; this is a quick reference
so you know what each handoff expects.

### `method_understanding` (Stage 1.5) - `response_format: json`
Surgical analysis of ONE changed method. Return:
```json
{
  "purpose": "what the method does (business language)",
  "exact_change": "the before/after of the condition that changed",
  "change_impact": "what the change means for behavior",
  "changed_variables": ["exact variable names from the diff"],
  "affected_behaviors": ["behavior that changed, business language"],
  "controlling_parameters": ["variables controlling the changed condition"],
  "test_scenarios": [
    {"id": "S1", "description": "...", "priority": "P0", "rationale": "..."}
  ],
  "NOT_affected": ["behaviors in the same method NOT affected by this change"]
}
```
Be SURGICAL: only the clause that actually changed is affected, not adjacent
clauses in the same `if`.

### `test_keywords` (diff-concept refinement) - `response_format: json`
Generate QA-language search keywords for the change. Return:
```json
{ "test_keywords": ["phrase1", "phrase2"], "exclude_keywords": ["NOT-affected phrase"] }
```
Use BUSINESS / module language (not code variable names). Do not include
keywords for behaviors listed as NOT affected.

### `tc_judgment` (Stage 7) - `response_format: json`
Judge a batch of test cases against the change. Return:
```json
{
  "judgments": [
    {
      "test_id": "EEM-XXXXX",
      "flow_match": true,
      "scenario_match": "full scenario description text, or NONE",
      "verdict": "DIRECT | INDIRECT | NOT_RELEVANT",
      "reasoning": "1-2 sentences referencing specific steps",
      "confidence": 0.0
    }
  ]
}
```
Verdict rules: if `flow_match` is true the verdict is at minimum `INDIRECT`;
reserve `NOT_RELEVANT` for tests with no execution path to the changed method.

### `dedup_judgment` (Stage 8) - `response_format: text`
Decide whether two near-duplicate tests exercise the SAME behavior. Return
the wrapped text answer whose value is exactly one of:
`SAME_BEHAVIOR` or `DIFFERENT_BEHAVIOR`.

---

## Pipeline Stages (Deterministic vs Copilot)

| Stage | Purpose | Who does it | Output |
|-------|---------|-------------|--------|
| 0     | Build / validate Knowledge Base | Python | `knowledge_base/*.json` |
| 1     | Call-tree analysis (entry points) | Python | `stage1_entry_points.json` |
| 1.5   | **Method understanding** | **Copilot** (`method_understanding`) | `method_understanding.json` |
| 2     | Flow mapping (DIRECT flows) | Python | `stage2_impacted_flows.json` |
| 3     | Indirect flow discovery | Python | `stage3_indirect_flows.json` |
| 3.5   | **Test-keyword generation** | **Copilot** (`test_keywords`) | `diff_concepts.json` |
| 4     | Test correlation | Python | `stage4_recommended_tests.json` |
| 5     | Refinement | Python | `stage5_refined_tests.json` |
| 6     | Aggressive suppression | Python | `stage6_aggressive_tests.json` (FINAL) |
| 7     | **TC judgment** | **Copilot** (`tc_judgment`) | `stage7_llm_tc_judgment.json` |
| 8     | **Semantic dedup judge** | **Copilot** (`dedup_judgment`) | dedup output |
| HTML  | Interactive report | Python | `RIA_Report.html` |

---

## Trigger Phrases

### Auto-Detect (no method name needed)
- "Run RIA on my changes" / "RIA on my changes" / "Run RIA"
- "Analyze my code changes"
- "Find regression tests for my changes"
- "Which tests should I run?"
- "Recommend tests for my recent changes"

### Explicit Method
- "Run RIA for `<NAME>`" / "RIA: `<NAME>`"
- "Find regression tests for `<NAME>`"
- "Analyze impact of `<NAME>`"
- "Run RIA for method `<NAME>` in file `<PATH>`"

`<NAME>` may be a bare method (`createObjectMapper`), `Class.method`, or a
fully-qualified name.

### Modifiers
- "Rebuild the KB ..." -> add `--rebuild-kb`
- "... stop at Stage 4" -> add `--no-refinement`
- "... no HTML" -> add `--no-html`
- "... for JIRA CXWFM-12345" -> add `--jira-card CXWFM-12345`

---

## Intelligent KB Validation

Decide `--rebuild-kb` from the prompt:

| Prompt signal | Flag |
|---------------|------|
| "rebuild", "from scratch", "fresh start", "stale", "regenerate", "wipe" | `--rebuild-kb` |
| "quick", "fast" | (none - reuse KB) |
| default | (none - minimal validation; auto-builds if missing) |

If intent is genuinely ambiguous, ask ONE clarifying question before running.
Always pass `--user-prompt "<exact text>"`.

---

## Example Walkthrough (Auto-Detect)

**User:** `Run RIA on my changes`

1. Run the pipeline (Step 2, no `--resume`). It detects changed methods, runs
   Stages 0-1.5, writes a PENDING `method_understanding.json`, and PAUSES.
2. Open `method_understanding.json`, read each method's `_context`, fill the
   reasoning fields + top-level `test_keywords`/`exclude_keywords`, set
   `_reasoning_source: copilot-agent`.
3. Re-run with `--resume`. It refines diff concepts with your keywords, runs
   Stages 2-6, writes a PENDING `stage7_llm_tc_judgment.json`, and PAUSES.
4. Fill the `judgments` array (DIRECT/INDIRECT/NOT_RELEVANT per TC), mark the
   file, and re-run with `--resume`.
5. The pipeline finalizes Stage 7, runs Stage 8, writes summaries + HTML.
6. Read `stage6_aggressive_tests.json` + `ria_v7_summary.json` and report.

---

## Expected Result Format (return to user)

```
RIA Analysis Complete

Changed method(s) : <NAME(s)>
Entry points      : <N>
Impacted flows    : <D> DIRECT, <I> INDIRECT
Stage 4 tests     : <N1>
Stage 5 tests     : <N2>
Stage 6 tests     : <N3>  (final recommendations)
Copilot reasoning : Stage 1.5 + Stage 7 answered over <ITER> resume(s)

Final tests JSON  : .github/RIA_OUTPUT/stage6_aggressive_tests.json
HTML Report       : .github/RIA_OUTPUT/RIA_Report.html
```

---

## Output Files

```
.github/RIA_OUTPUT/
├── knowledge_base/                  (KB files - one-time build)
├── stage1_entry_points.json
├── method_understanding.json        (Stage 1.5 - YOU fill reasoning + keywords)
├── stage2_impacted_flows.json
├── stage3_indirect_flows.json
├── diff_concepts.json               (refined by your Stage 1.5 keywords)
├── stage4_recommended_tests.json
├── stage5_refined_tests.json
├── stage6_aggressive_tests.json     (FINAL)
├── stage7_llm_tc_judgment.json      (Stage 7 - YOU fill judgments)
├── ria_v7_summary.json
└── RIA_Report.html
```

There is no `llm_io/` folder — reasoning lives directly in the stage files.

---

## Error Handling

| Symptom | Action |
|---------|--------|
| `KB incomplete. Missing: ...` | Re-run with `--rebuild-kb` |
| `Missing test corpus: all_tcs_extracted_enriched.json` | Run `tc_extractor.py` first (needs Xray creds in `ria_config.env`) |
| `0 entry points found` | Method name may be misspelled or not in any call tree |
| `PIPELINE PAUSED` banner | Expected: fill the named file, then re-run with `--resume` |
| Pipeline keeps pausing at the same stage | You didn't set `"_reasoning_source": "copilot-agent"` in that file, or edited the wrong file |
| Answer lost after a re-run | You re-ran WITHOUT `--resume` (that cleans the workspace); use `--resume` after a pause |

---

## Related Files

- Skill definition          : [SKILL.md](../skills/regression-impact-analysis/SKILL.md)
- Agent reasoning helper     : [scripts/agent_reasoning.py](../skills/regression-impact-analysis/scripts/agent_reasoning.py)
- Main entry-point script   : [scripts/ria_agent.py](../skills/regression-impact-analysis/scripts/ria_agent.py)
- Configuration             : [configs/ria_config.py](../skills/regression-impact-analysis/configs/ria_config.py)

---

## Version History

- **v4** (2026-07-08): Removed the request/response file mailbox
  (`copilot_llm_bridge` + `llm_io/`). LLM reasoning is still performed by the
  GitHub Copilot model, but now via a pause/resume model: the pipeline writes a
  pending baseline into each reasoning stage's normal output file and pauses;
  the agent fills the file (`_reasoning_source: copilot-agent`) and resumes with
  `--resume`. Reasoning points are Stage 1.5 (understanding + keywords) and
  Stage 7 (TC judgment); Stage 8 dedup is conservative (no LLM tie-breaker).
- **v3** (2026-06-24): Removed AWS Bedrock entirely. LLM reasoning performed by
  the GitHub Copilot model via the `copilot_llm_bridge` request/response file
  handoff. No credentials, no `boto3`, no network LLM.
- **v2** (2026-05-12): Auto git change detection, multi-method consolidation,
  prompt-driven KB validation.
- **v7.0-7.4**: Agnostic zero-hardcoding architecture, 6-stage refinement,
  HTML report (Bedrock-era).
