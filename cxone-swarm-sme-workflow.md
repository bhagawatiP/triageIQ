# CXone Swarm SME — End-to-End Workflow

How the `cxone-swarm-sme` agent and the `cxone-swarm-triage` workflow work together to triage a Swarm case from raw ticket to ready-to-paste drafts — now including **team routing**, **test-coverage analysis**, and **code-level RCA**.

## Pipeline Diagram

```
┌───────────────────────────────────────────────────────────────────────────┐
│                            USER INVOCATION                                │
│                                                                           │
│   Workflow({ name: 'cxone-swarm-triage',                                  │
│              args: { jiraKey, description?, logsPath?, harPath?,          │
│                      screenshots?, priority? } })                         │
└───────────────────────────────┬───────────────────────────────────────────┘
                                │
                                ▼
        ╔═══════════════════════════════════════════════════════╗
        ║   PHASE 1 — INTAKE            (1 agent · Explore)     ║
        ║   Reads KB, fetches Jira (if key), reads logs/HAR;    ║
        ║   extracts symptom, observed vs expected, objects,    ║
        ║   repro, evidence, release correlation, questions.    ║
        ║   OUTPUT: INTAKE_SCHEMA (JSON)                        ║
        ╚═══════════════════════════════════════════════════════╝
                                │
                                ▼
        ╔═══════════════════════════════════════════════════════╗
        ║   PHASE 2 — CLASSIFICATION    (1 agent)               ║
        ║   Assigns severity (P1-P4), impact, product area,     ║
        ║   metric type, 1-of-7 category, confidence.           ║
        ║   OUTPUT: CLASSIFICATION_SCHEMA (JSON)                ║
        ╚═══════════════════════════════════════════════════════╝
                                │
                                ▼
        ╔═══════════════════════════════════════════════════════╗
        ║   PHASE 3 — ROOT CAUSE        (3 agents)              ║
        ║   3a. 7-step framework (WebFetch NICE docs + KB).     ║
        ║        │                                              ║
        ║        ▼  ← PARALLEL FAN-OUT                          ║
        ║   3b. DOC-ALIGNMENT SKEPTIC   3c. SOURCE-OF-TRUTH     ║
        ║   Both refuted=false → keep confidence;               ║
        ║   else downgrade + tag.                               ║
        ║   OUTPUT: ROOTCAUSE + VERIFIERS (JSON)                ║
        ╚═══════════════════════════════════════════════════════╝
                                │
                                ▼
        ╔═══════════════════════════════════════════════════════╗
        ║   PHASE 4 — TEAM ASSIGNMENT   (1 agent)   [NEW]       ║
        ║   Reads team-assignment.md; routes the RCA object     ║
        ║   (widget → Table A, else area → Table B) to its      ║
        ║   owning team. Sets code_rca_eligible = true only     ║
        ║   for Titans / Sapphire / Waves.                      ║
        ║   OUTPUT: TEAM_SCHEMA (JSON)                          ║
        ╚═══════════════════════════════════════════════════════╝
                                │
                                ▼
        ╔═══════════════════════════════════════════════════════╗
        ║   PHASE 5 — COVERAGE & CODE   (2 agents, PARALLEL) [NEW]║
        ║   ┌─────────────────────┐   ┌───────────────────────┐ ║
        ║   │ 5a. TEST COVERAGE   │   │ 5b. CODE RCA          │ ║
        ║   │ Map widget→Xray     │   │ (only if eligible)    │ ║
        ║   │ folder; shortlist   │   │ Read cxone-cxdvi-     │ ║
        ║   │ cached tests; check │   │ pmn-shared (dev);     │ ║
        ║   │ step alignment.     │   │ locate widget code;   │ ║
        ║   │ Covered → test key. │   │ propose fix (diff).   │ ║
        ║   │ Gap → DRAFT case    │   │ READ-ONLY. Else stub. │ ║
        ║   │ (no Jira write).    │   │                       │ ║
        ║   └─────────┬───────────┘   └───────────┬───────────┘ ║
        ║             └───────────────┬───────────┘             ║
        ║   OUTPUT: COVERAGE_SCHEMA + CODE_RCA_SCHEMA (JSON)    ║
        ╚═══════════════════════════════════════════════════════╝
                                │
                                ▼
        ╔═══════════════════════════════════════════════════════╗
        ║   PHASE 6 — DRAFTS           (3 agents, PARALLEL)    ║
        ║   CUSTOMER · SWARM · R&D ESCALATION.                  ║
        ║   Swarm + escalation now cite owning team, coverage   ║
        ║   verdict, and (if any) proposed code fix.            ║
        ║   OUTPUT: DRAFTS bundle (3 JSONs)                     ║
        ╚═══════════════════════════════════════════════════════╝
                                │
                                ▼
        ╔═══════════════════════════════════════════════════════╗
        ║   PHASE 7 — RENDER           (2 agents · Explore save)║
        ║   Compose the tabular triage summary (now incl. Team  ║
        ║   Assignment, Test Coverage, Code RCA sections) and   ║
        ║   save to reports/<case_key>-triage.md.               ║
        ╚═══════════════════════════════════════════════════════╝
                                │
                                ▼
┌───────────────────────────────────────────────────────────────────────────┐
│  FINAL RETURN VALUE                                                       │
│  { intake, classification, rootCause, verifiers,                          │
│    teamAssignment,            ← Phase 4                                   │
│    coverage, codeRCA,         ← Phase 5                                   │
│    drafts: { customer, swarm, escalation },   ← Phase 6                   │
│    rendered, saved }          ← Phase 7                                   │
└───────────────────────────────────────────────────────────────────────────┘
```

## Reading It in One Paragraph

**Intake** pulls the case (Jira + logs + KB) → **Classification** stamps it P1–P4 and picks a 1-of-7 bucket → **Root Cause** runs the 7-step framework, then two skeptics in parallel try to refute it against NICE docs and source-of-truth rules → **Team Assignment** routes the affected widget/area to its owning team and decides whether code RCA applies → **Coverage & Code** (parallel) checks the CXDV Xray test bed for existing coverage (drafting a new case on a gap, never writing to Jira) and, for Titans/Sapphire/Waves, inspects the `cxone-cxdvi-pmn-shared` source to propose a fix → **Drafts** fans out three tailored write-ups → **Render** composes and saves the tabular report. Every phase's output is JSON-schema-constrained.

## Phase-by-Phase Detail

### Phase 1 — Intake
- 1 agent (Explore). Reads KB at `cxone-dashboard-kb.md`; runs `get_jira_issue.py` if a Jira key was supplied; reads logs/HAR/screenshots. Output: `INTAKE_SCHEMA`.

### Phase 2 — Classification
- 1 agent. Assigns severity (P1–P4), impact, product area, metric type, 1-of-7 classification, confidence. Output: `CLASSIFICATION_SCHEMA`.

### Phase 3 — Root Cause (with adversarial verify)
- 3 agents: the 7-step analyzer + two parallel skeptics (doc-alignment, source-of-truth). If either refutes, confidence is auto-downgraded. Output: `ROOTCAUSE_SCHEMA` + `verifiers`.

### Phase 4 — Team Assignment *(new)*
- 1 agent. Reads `team-assignment.md` and applies the ordered rule: widget (Table A, incl. aliases) → area/symptom (Table B) → `Unresolved`. A deprecated widget → `N/A (deprecated)`. Sets `code_rca_eligible = true` only for **Titans / Sapphire / Waves**. Output: `TEAM_SCHEMA` — `owning_team`, `match_basis`, `matched_row`, `code_rca_eligible`, `confidence`.

### Phase 5 — Coverage & Code *(new, parallel)*
- **5a. Test Coverage:** maps the widget to its Xray folder via `cxdv-test-repository/widget-reference.md`, shortlists candidate tests from the cached `raw-data/*-folders-raw.json`, and checks step alignment against the bug. `Covered` → returns the aligned test key(s). `Gap`/`Partial` → drafts JIRA-ready scenario(s) per the `testcase-creation` persona. **Draft-only — the pipeline never writes to Jira/Xray**; creation happens through the interactive agent's confirm gate. Output: `COVERAGE_SCHEMA`.
- **5b. Code RCA:** runs only when `code_rca_eligible`. Reads the cloned `cxone-cxdvi-pmn-shared` (branch `develop`), orients via its `AGENTS.md`/`docs/ARCHITECTURE.md`, locates the widget's back-end/API/front-end code, and proposes a fix (diff + rationale). **Read-only — never modifies that repo.** Not eligible → a stub `{eligible:false, reason}`. Output: `CODE_RCA_SCHEMA`.

### Phase 6 — Drafts
- 3 agents in parallel. Customer response (blame-free, no fix ETA); internal swarm update (now names the owning team + coverage verdict + any code pointer); R&D escalation note (`suggested_component` = owning team; evidence bundle includes suspect paths, proposed fix, and test keys).

### Phase 7 — Render
- 2 agents (compose + Explore save). Produces the tabular report — now with **Team Assignment**, **Test Coverage**, and **Code RCA & Suggested Fix** sections — and writes it to `reports/<case_key>-triage.md`.

## Two Ways to Invoke

| Mode | Call | Best for |
|------|------|----------|
| **Interactive (single agent)** | `Task({ subagent_type: 'cxone-swarm-sme', prompt: '<case>' })` | Quick triage, chat-style back-and-forth; runs the same team/coverage/code steps and **owns the test-creation confirm gate**. |
| **Pipeline (this workflow)** | `Workflow({ name: 'cxone-swarm-triage', args: {...} })` | Deep triage with adversarial verify + all three drafts ready to paste; coverage is draft-only. |

## Key Design Choices

- **Pipeline (not full parallel):** each phase feeds the next; Team Assignment depends on the RCA object, Code RCA depends on team eligibility.
- **Fan-out only where independent:** two skeptics in Phase 3, Coverage ∥ Code in Phase 5, three drafts in Phase 6.
- **JSON-schema-constrained outputs** at every phase.
- **No silent writes:** test-case *creation* is gated behind explicit confirmation (interactive agent); the pipeline only drafts. Code RCA is strictly read-only against pmn-shared.
- **Single sources of truth:** `cxone-dashboard-kb.md` (learnings), `team-assignment.md` (ownership) — one file each to update.

## Reference Paths

| Artifact | Path |
|----------|------|
| SME agent | `.claude/agents/cxone-swarm-sme.md` |
| Workflow script | `.claude/workflows/cxone-swarm-triage.js` |
| Knowledge base | `cxone-dashboard-kb.md` |
| Team ownership map | `team-assignment.md` |
| Test-case persona | `.claude/testcase-creation.prompt1.md` |
| Test-creation agent | `.claude/agents/auto-tp-gen.md` |
| Test-bed snapshot | `testbed/` (bundled in the repo) |
| pmn-shared source (code RCA) | `.external/cxone-cxdvi-pmn-shared/` — auto-cloned from `https://github.com/nice-cxone/cxone-cxdvi-pmn-shared` |
| Sample reports | `reports/` |
| Jira skill *(not bundled)* | `.claude/skills/jira-get-issue/scripts/get_jira_issue.py` |
| Confluence skill *(not bundled)* | `.claude/skills/confluence-get-page/scripts/get_confluence_page.py` |
