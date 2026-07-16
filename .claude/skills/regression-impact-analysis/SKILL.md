---
name: regression-impact-analysis
description: RIA v2 - Intelligent test selection from a code change using the 6-stage Flow x Component pipeline with MULTI-LANGUAGE support (Java, TypeScript, JavaScript, Python).  AUTO-DETECTS language and changed methods from git when the user does not name one.  KB validation is PROMPT-DRIVEN.  Use when the user asks to "Run RIA on my changes", "Run RIA analysis", "Find regression tests for method X", "Recommend tests for a code change", "Which tests should I run?", "RIA for method X", or any variation about regression impact analysis.  The skill executes ria_agent.py which auto-builds the Knowledge Base on first run and then runs Stages 1-6 plus HTML report generation with dark-themed interactive report.
version: 3.0.0
---

# Regression Impact Analysis (RIA) v2

## Phase 2: Dependency-Aware Analysis (NEW)

In addition to method-level source changes, RIA now also handles
**dependency / build-file changes** automatically:

  - `pom.xml` (Maven)
  - `build.gradle` / `build.gradle.kts` (Gradle)
  - `package.json` (npm / Yarn / pnpm)
  - `requirements.txt` / `pyproject.toml` / `setup.py` (pip / Poetry)
  - `*.csproj` (NuGet)

When the working tree contains build-file changes but **no source-method
changes**, the agent:

  1. Detects every changed dependency via plug-in parsers
     (`scripts/parsers/`).
  2. Classifies each dependency dynamically using usage %, import
     centrality, module concentration, API entropy, scope and lexicons
     pulled from `component_map.json`. There are **no hardcoded artifact
     names** anywhere in the classifier.
  3. Picks one of four strategies per dependency:
     `SKIP` / `RIA_ANALYSIS` / `TARGETED_SUITE` / `CRITICAL_SUITE`. The
     run-wide strategy is the most-conservative bucket.
  4. For `RIA_ANALYSIS` runs: greps the codebase for source files that
     import the changed dependency, deduplicates them into unique
     business flows via the call graph + `flow_registry.json`, then
     selects 3 representative tests per flow (smoke, top-correlation,
     edge case). Stages 4-6 then run on the synthesized flow set.
  5. For `CRITICAL_SUITE` runs: writes a recommendation file pointing at
     the project's full critical suite and skips the per-test pipeline.

The HTML report gains a top-of-page "Dependency Changes" section when
the run was triggered by a build-file change. Source-change runs are
unaffected and the HTML layout is unchanged.

Configuration:
  - `configs/tech_stack_mappings.json` - per-stack name -> import-prefix
    overrides (kept short; the default rule covers most artifacts).
  - `configs/classification_rules.json` - signal weights and lexicons.

## What This Skill Does

When invoked, this skill runs a single Python entry point (`ria_agent.py`) that
orchestrates the entire 6-stage RIA pipeline end-to-end.

In **v2** the skill automatically detects:
- **Language**: Java, TypeScript, JavaScript, Python (from repository files)
- **Changed methods**: From git (unstaged + staged + untracked files)

When multiple methods changed, the agent runs the multi-method consolidation
pipeline and writes a UNION of recommended tests.

The skill is designed for **prompt-based execution only**.  The user does NOT
run Python directly; Claude invokes `ria_agent.py` via the Bash tool.

---

## When Claude Should Invoke This Skill

Claude MUST invoke this skill when the user prompt matches any of:

### Auto-Detection (recommended)
- "Run RIA on my changes"
- "Run RIA analysis on my changes"
- "Analyze my code changes"
- "Find regression tests for my changes"
- "Which tests should I run?"
- "Recommend tests for my recent changes"
- "RIA on my changes"

### With JIRA Card (NEW v2)
- "Run RIA on my changes for JIRA card CXWFM-12345"
- "Run RIA for JIRA CXWFM-12345"
- "Analyze regression impact for card CXWFM-12345"
- "RIA on my changes, document in CXWFM-12345"

### Explicit Method
- "Run RIA analysis for method: <NAME>"
- "Run RIA for <NAME>"
- "Find regression tests for <NAME>"
- "Recommend tests for changed method <NAME>"
- "Which tests should I run for <NAME>?"
- "Analyze regression impact of <NAME>"
- "Run regression impact analysis on <NAME>"
- "RIA: <NAME>"

---

## Choosing the KB Strategy

Before invoking ria_agent.py, Claude must decide whether to rebuild the Knowledge Base from scratch.

### Decision Rubric

Pass `--rebuild-kb` if the user's intent is to start from a clean state / rebuild from scratch.
Otherwise (the default), do NOT pass `--rebuild-kb`.

A "rebuild intent" is signalled when the user wants to:
- Start over from scratch, from the beginning, from step 0, or from 0
- Rebuild, regenerate, refresh, recreate, or wipe the KB
- Run from a clean slate, fresh start, or clean run
- Fix issues from a previous run that had wrong/stale results
- Reset or restart the analysis completely

### Few-Shot Examples (Anchors for Understanding)

Use these examples to understand the CONCEPT of rebuild intent, then generalize to ANY similar phrasing:

| User Prompt | Flag | Reasoning |
|-------------|------|-----------|
| "Run RIA on my changes" | (none) | Normal run - no rebuild signal |
| "Run RIA from 0 step" | `--rebuild-kb` | "from 0" signals start over |
| "Start from scratch" | `--rebuild-kb` | Explicit fresh-start intent |
| "Fresh start" / "Clean slate" | `--rebuild-kb` | Rebuild from clean state |
| "Regenerate everything" | `--rebuild-kb` | Rebuild signal |
| "Rebuild knowledge base" | `--rebuild-kb` | Explicit rebuild request |
| "My last run had wrong results" | `--rebuild-kb` | Implies stale KB, rebuild needed |
| "Start over from the beginning" | `--rebuild-kb` | Restart = rebuild |
| "Wipe everything and redo" | `--rebuild-kb` | Wipe = rebuild |
| "Run RIA quickly" | (none) | Speed implies reuse existing KB |
| "Analyze these changes" | (none) | Normal analysis |

### Handling Ambiguous Prompts

If the user's intent is ambiguous or unclear (e.g., "Run RIA properly", "Analyze this correctly", "I need accurate results"):

**DO NOT GUESS.** Instead, ask the user ONE clarifying question:

> "Should I rebuild the knowledge base from scratch (slower, ~13 min longer, but ensures fresh data), or reuse the existing KB (faster)?"

Wait for the user's response, then pass the appropriate flag.

### Cost Model (For Decision Making)

- Passing `--rebuild-kb` adds ~13 minutes (one-time KB build)
- NOT rebuilding when KB is stale can produce incorrect test recommendations
- When confidence in intent is below ~80%, ASK rather than guess

### Default Behavior

If NO rebuild intent is detected in the prompt:
- Do NOT pass `--rebuild-kb`
- Python will auto-build KB if files are missing (first-run safe)
- Python will warn (not fail) if KB is older than 7 days

### Examples of Generalization

These prompts are NOT in the table above, but Claude should understand them by generalizing from the concept:

- "Restart from the very beginning" → `--rebuild-kb` (similar to "start from scratch")
- "Do it again from step 0" → `--rebuild-kb` (similar to "from 0 step")
- "Clean run please" → `--rebuild-kb` (similar to "clean slate")
- "Wipe KB and rerun" → `--rebuild-kb` (wipe = rebuild)
- "Quick analysis of my changes" → (none) - quick = reuse KB

**Claude should understand the INTENT, not match exact strings.**

---

## Execution Steps for Claude

When this skill is invoked, follow these steps **in order**:

### Step 1 - Determine the mode and extract JIRA card

- If the user prompt contains a specific method name -> **Explicit-Method mode**
- Otherwise (prompt mentions "my changes", "my code", "pending changes",
  or just "Run RIA") -> **Auto-Detect mode**

**Extract JIRA card from prompt:**
- Regex pattern: `([A-Z]+-\d+)` or `(JIRA|card|ticket)\s+([A-Z]+-\d+)`
- Examples: 
  - "Run RIA for CXWFM-12345" → extract `CXWFM-12345`
  - "RIA on my changes for card EEM-60885" → extract `EEM-60885`
  - "Run RIA for JIRA EMOB-123" → extract `EMOB-123`
- If JIRA card found → pass via `--jira-card` flag

Optional modifiers in any mode: `--rebuild-kb`, `--no-refinement`, `--no-html`.

### Step 1.5 - KB Validation Strategy (Claude-driven)

Claude (you) decides whether to rebuild the KB by reading the user's
prompt and applying the rubric in the **"Choosing the KB Strategy"**
section above.

- If the user expresses rebuild intent -> pass `--rebuild-kb`
- Otherwise -> do NOT pass the flag; Python defaults to `minimal`
  (verify files exist; auto-build if missing)
- If intent is ambiguous -> ASK the user before invoking the pipeline

Pass the user's exact prompt via `--user-prompt` for audit logging only
(no longer used for keyword-based strategy detection inside Python).
CLI flag `--skip-kb-check` is also supported when the user explicitly
wants to bypass KB validation.

### Step 2 - Run the pipeline via Bash

Execute from the **repository root** (containing `.github/`).

**Auto-Detect mode (no method name needed):**
```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/ria_agent.py" \
    --user-prompt "<ORIGINAL USER PROMPT>" \
    [--jira-card CXWFM-12345] \
    [--rebuild-kb] [--no-refinement] [--no-html]
```

**Explicit-Method mode:**
```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/ria_agent.py" \
    --user-prompt "<ORIGINAL USER PROMPT>" \
    --changed-method "<METHOD_NAME>" \
    [--changed-file "<FILE_PATH>"] \
    [--jira-card CXWFM-12345] \
    [--rebuild-kb] \
    [--no-refinement] \
    [--no-html]
```

**JIRA Integration (NEW v2):**

When user mentions a JIRA card in their prompt (e.g., "Run RIA for CXWFM-12345"):
1. Extract JIRA card number using regex `([A-Z]+-\d+)`
2. Pass via `--jira-card` flag to the pipeline
3. Pipeline runs normally (Stages 0-6 + HTML report)
4. After completion:
   - **DRY-RUN MODE (default)**: Saves report to `./reports/RIA-{CARD}-{timestamp}.md`
   - **LIVE MODE**: Posts to JIRA Quality tab or creates sub-task

**To enable LIVE JIRA posting:**
Remove `'--dry-run',` from line 1393 in `ria_agent.py`. Until then, reports are saved locally for review.

Stream output so the user sees stage progress (KB strategy banner ->
auto-detect summary -> Stage 0 -> 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> HTML).

#### Optional: live in-process reasoning (`--live-agent`)

By DEFAULT the pipeline PAUSES at each reasoning stage (1.5 / 7) by writing a
pending baseline and EXITING 0; the agent fills the file and re-runs with
`--resume` ("chef leaves the kitchen and comes back"). This is the safe default
and works headless.

Add `--live-agent` to keep the process ALIVE across those pauses ("chef stays
in the kitchen"): at each reasoning stage the pipeline BLOCKS in place, prints a
single greppable marker line `RIA_LIVE_WAIT stage="..." file="..."`, and polls
that file until the agent fills it (sets `_reasoning_source: copilot-agent`) —
then continues in the SAME process, no restart. On timeout (default 1800s,
override with `--live-timeout`) it falls back to the default pause/exit path, so
it can never hang.

How Claude drives `--live-agent` (interactive only):
1. Launch it as a BACKGROUND command (so you can act while it blocks).
2. Watch its stdout with the Monitor tool for `RIA_LIVE_WAIT`.
3. On each marker, READ the named file, fill the reasoning fields
   (`_reasoning_source: copilot-agent`), write it back (atomically) — the
   polling loop detects it within ~2s and resumes with no restart.
4. Repeat for the second pause; the run finishes in one process (no `--resume`).

```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/ria_agent.py" \
    --live-agent --user-prompt "<ORIGINAL USER PROMPT>"
```

### Step 3 - Read the result files

After the script exits with code 0, read:
- `.github/RIA_OUTPUT/stage6_aggressive_tests.json` - final test list (single-method mode) OR consolidated UNION (multi-method mode)
- `.github/RIA_OUTPUT/stage6_consolidated_tests.json` - per-method-merged tests (multi-method mode only)
- `.github/RIA_OUTPUT/consolidated_summary.json`     - per-method breakdown (multi-method mode only)
- `.github/RIA_OUTPUT/ria_v7_summary.json`           - per-stage counts
- `.github/RIA_OUTPUT/RIA_Report.html`               - interactive report

### Step 4 - Report to the user

Return a concise summary:

**Without JIRA card:**
```
RIA Analysis Complete

Changed method   : <METHOD_NAME>
Entry points     : <N>
Impacted flows   : <D> DIRECT, <I> INDIRECT
Stage 4 tests    : <N1>
Stage 5 tests    : <N2>
Stage 6 tests    : <N3>  (final recommendations)

Final tests JSON : .github/RIA_OUTPUT/stage6_aggressive_tests.json
HTML Report      : .github/RIA_OUTPUT/RIA_Report.html

Open the report:
  open .github/RIA_OUTPUT/RIA_Report.html
```

**With JIRA card (dry-run mode):**
```
RIA Analysis Complete

Changed methods  : <N>
Impacted flows   : <D>
Recommended tests: <N>

RIA Report saved for review:
  ./reports/RIA-<CARD>-<timestamp>.md

Review the report and confirm before posting to JIRA.
To enable live JIRA posting, update ria_agent.py line 1393.
```

---

## What `ria_agent.py` Does Internally

```
ria_agent.py
  |
  +-- Step -1 (NEW v2): Auto-detect changes from git
  |     - Skipped if --changed-method was supplied AND --auto-detect was not.
  |     - Calls detect_changes.detect_code_changes(REPO_ROOT) which:
  |         * runs `git diff` (unstaged) + `git diff --cached` (staged)
  |           + `git ls-files --others` (untracked)
  |         * filters to .java files (excluding tests / generated / build dirs)
  |         * parses each file to extract method declarations
  |         * matches changed line numbers to method bodies
  |     - 0 methods   -> abort with friendly message
  |     - 1 method    -> sets --changed-method/--changed-file and continues
  |     - 2+ methods  -> dispatches to multi-method consolidation pipeline
  |
  +-- Step 0: Intelligent KB validation (kb_strategy.py)
  |     - Picks strategy from --user-prompt:
  |         skip / minimal (default) / standard / rebuild
  |     - 'minimal'  -> verify the 3 ONE-TIME KB files exist; no age check
  |     - 'standard' -> existence + 7-day staleness warning (never aborts)
  |     - 'skip'     -> bypass all KB checks
  |     - 'rebuild'  -> force a full Stage-0 rebuild
  |     - --rebuild-kb / --skip-kb-check flags still override the prompt
  |
  +-- Stage 0 (only if --rebuild-kb or KB missing):
  |     1. build_synonym_groups.py    -> synonym_groups.json
  |     2. build_component_map.py     -> component_map.json
  |     3. build_flow_registry.py     -> flow_registry.json
  |     4. build_flow_dependencies.py -> flow_dependencies.json
  |     (test corpus must be produced separately by tc_extractor.py)
  |
  +-- Stages 1-4: ria_v7_orchestrator.py --mode analyze --atomic
  |     1: stage1_call_tree_analysis.py     -> stage1_entry_points.json
  |     2: stage2_flow_mapping.py           -> stage2_impacted_flows.json
  |     3: stage3_indirect_flows.py         -> stage3_indirect_flows.json
  |     4: stage4_test_correlation.py       -> stage4_recommended_tests.json
  |
  +-- Stage 5: stage5_refine_tests.py             -> stage5_refined_tests.json
  +-- Stage 6: stage6_aggressive_suppression.py   -> stage6_aggressive_tests.json
  +-- HTML  : generate_html_report.py             -> RIA_Report.html
```

### Multi-method mode (NEW v2)

When git auto-detection finds 2+ changed methods, the agent runs the
single-method pipeline once *per method* and snapshots each per-method output
to `.github/RIA_OUTPUT/multi_method/method_<N>_<name>/`.  After all per-method
runs complete, the agent writes:

- `stage6_consolidated_tests.json` - UNION of all per-method final test lists,
  with each test annotated with `triggered_by_methods` (the list of changed
  methods that recommended it).
- `stage6_aggressive_tests.json`   - same UNION, written here so the HTML
  report and downstream consumers naturally pick it up.
- `consolidated_summary.json`      - per-method breakdown (status, snapshot
  path, test count) plus aggregate counts.

---

## Files Layout

```
.github/skills/regression-impact-analysis/
├── SKILL.md                                  (this file)
├── configs/
│   ├── ria_config.py                         (scoring + thresholds)
│   ├── ria_config.env                        (environment overrides)
│   └── ria_config.env.template               (template)
└── scripts/
    ├── ria_agent.py                          (MAIN ENTRY POINT - prompt invokes this)
    ├── detect_changes.py                     (NEW v7.3 - git auto-detection)
    ├── ria_v7_orchestrator.py                (Stages 1-4 driver)
    ├── stage1_call_tree_analysis.py
    ├── stage2_flow_mapping.py
    ├── stage3_indirect_flows.py
    ├── stage4_test_correlation.py
    ├── stage5_refine_tests.py
    ├── stage6_aggressive_suppression.py
    ├── generate_html_report.py               (HTML report)
    ├── build_synonym_groups.py               (KB builder)
    ├── build_component_map.py                (KB builder)
    ├── build_flow_registry.py                (KB builder)
    ├── build_flow_dependencies.py            (KB builder)
    ├── tc_extractor.py                       (Jira test-corpus extractor)
    ├── serena_mcp_client.py                  (MCP integration)
    └── utils.py                              (shared helpers)
```

Outputs are written to: `.github/RIA_OUTPUT/`

---

## Inputs and Outputs

### Inputs

| Input             | Source                                              |
|-------------------|-----------------------------------------------------|
| Changed method    | Parsed from user prompt                             |
| Changed file path | Parsed from user prompt (or script default)         |
| Knowledge Base    | `.github/RIA_OUTPUT/knowledge_base/*.json`          |
| Test corpus       | `.../knowledge_base/all_tcs_extracted_enriched.json`|
| Source code       | Repository on disk (scanned by stage 1)             |

### Outputs

| Output                                | Path                                                |
|---------------------------------------|-----------------------------------------------------|
| Entry points                          | `.github/RIA_OUTPUT/stage1_entry_points.json`       |
| Impacted DIRECT flows                 | `.github/RIA_OUTPUT/stage2_impacted_flows.json`     |
| Impacted INDIRECT flows               | `.github/RIA_OUTPUT/stage3_indirect_flows.json`     |
| Stage 4 raw recommendations           | `.github/RIA_OUTPUT/stage4_recommended_tests.json`  |
| Stage 5 refined tests                 | `.github/RIA_OUTPUT/stage5_refined_tests.json`      |
| **Stage 6 final tests**               | `.github/RIA_OUTPUT/stage6_aggressive_tests.json`   |
| Summary metrics                       | `.github/RIA_OUTPUT/ria_v7_summary.json`            |
| **HTML report**                       | `.github/RIA_OUTPUT/RIA_Report.html`                |

---

## Expected Result

For a typical changed method:

| Metric                     | Value                       |
|----------------------------|-----------------------------|
| Final test count           | 40-50 (typically 48)        |
| Precision                  | 95%+                        |
| Reduction from corpus      | 99.5% (9,825 -> ~48)        |
| Pipeline runtime           | 1-3 minutes                 |
| Knowledge Base build       | ~26 seconds (one-time)      |

---

## Troubleshooting

### KB files missing

```
[ERROR] KB incomplete. Missing: synonym_groups.json, ...
```

Re-run with a "rebuild" prompt (preferred) or the explicit flag:
```bash
# Prompt-driven (recommended):
python3 "${CLAUDE_SKILL_DIR}/scripts/ria_agent.py" \
    --user-prompt "Rebuild RIA KB and analyze <NAME>" \
    --changed-method <NAME>

# Explicit flag (back-compat):
python3 "${CLAUDE_SKILL_DIR}/scripts/ria_agent.py" \
    --rebuild-kb --changed-method <NAME>
```

### Test corpus missing

```
[ERROR] Missing test corpus: all_tcs_extracted_enriched.json
```

Run `tc_extractor.py` first (requires Jira credentials in `configs/ria_config.env`):
```bash
python3 "${CLAUDE_SKILL_DIR}/scripts/tc_extractor.py"
```

### 0 entry points found

The method name may be misspelled, or the method isn't called by any controller
endpoint. Verify spelling and try the fully-qualified name.

### Too many or too few tests

Stages 5 and 6 self-tune to the 40-50 band. If the result is consistently off,
adjust thresholds at the top of `stage5_refine_tests.py` /
`stage6_aggressive_suppression.py`.

### Pipeline hangs / partial output

Cancel (Ctrl-C) and re-run with `--rebuild-kb`. Inspect
`.github/RIA_OUTPUT/run.log` for clues.

---

## Related Documentation

- **Prompt usage**           : `PROMPT_USAGE.md`
- **Agent definition**       : `../../agents/ria.agent.md`
- **Configuration reference**: `configs/ria_config.py`

## Internal Modules

The following files are internal modules imported by `ria_agent.py` and `tc_extractor.py`. They are not invoked directly.

**Core modules** (`core/`): `__init__.py`, `call_graph.py`, `config_adapter.py`, `detector.py`, `language_adapter.py`

**Pipeline stages and utilities** (`scripts/`): `agent_reasoning.py`, `build_discovered_vocabularies.py`, `build_embeddings.py`, `discover_reserved_words.py`, `discover_test_patterns.py`, `extract_diff_concepts.py`, `flow_discovery.py`, `jira_extension.py`, `stage1_5_llm_method_understanding.py`, `stage7_llm_tc_judgment.py`, `stage8_semantic_deduplication.py`, `stage_execution_auditor.py`, `term_idf.py`

> **LLM reasoning is performed by the GitHub Copilot agent**, not AWS Bedrock.
> There is NO request/response file mailbox. The reasoning stages (Stage 1.5
> and Stage 7) write a *pending baseline* into their normal output file and
> PAUSE the pipeline; the Copilot agent fills the reasoning fields directly
> (setting `"_reasoning_source": "copilot-agent"`) and resumes with `--resume`
> (see `scripts/agent_reasoning.py` and `../../agents/ria.agent.md`). Stage 8
> dedup is conservative (no LLM tie-breaker). There is NO `boto3`, NO AWS
> Bedrock, and NO cloud LLM credential requirement.

