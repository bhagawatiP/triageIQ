export const meta = {
  name: 'cxone-swarm-triage',
  description: 'CXone Swarm case triage: intake -> classification (P1-P4) -> root cause (adversarial verify) -> customer/swarm/R&D drafts.',
  phases: [
    { title: 'Intake',         detail: 'Pull Jira case if key given; read logs/HAR; consult KB; produce structured summary' },
    { title: 'Classification', detail: 'Assign P1-P4 severity, impact, product area, initial category' },
    { title: 'RootCause',      detail: '7-step framework + two parallel adversarial verifiers (doc-alignment + source-of-truth)' },
    { title: 'TeamAssignment', detail: 'Route the bug to its owning team via the widget/area ownership map; flag code-RCA eligibility' },
    { title: 'Coverage & Code', detail: 'Parallel: test-bed coverage analysis (draft-only) + code-level RCA for Titans/Sapphire/Waves' },
    { title: 'Drafts',         detail: 'Parallel: customer response, internal swarm update, R&D escalation note' },
    { title: 'Render',         detail: 'Compose tabular triage summary (Priority + Severity + team + coverage + code fix + actions) and save to reports/ dir' },
  ],
}

// Portable paths — relative to the repo root (the cwd when you run this workflow from the
// cloned repo). Override any of them via Workflow args: { root, testbedDir, pmnDir }.
const ROOT = (typeof args === 'object' && args && args.root) ? String(args.root).replace(/[\\/]+$/, '') : '.'
const P = rel => `${ROOT}/${rel}`
const KB_PATH = P('cxone-dashboard-kb.md')
const JIRA_SCRIPT = P('.claude/skills/jira-get-issue/scripts/get_jira_issue.py')
const CONFLUENCE_SCRIPT = P('.claude/skills/confluence-get-page/scripts/get_confluence_page.py')
const TEAM_ASSIGNMENT_PATH = P('team-assignment.md')
const TESTCASE_PERSONA_PATH = P('.claude/testcase-creation.prompt1.md')
const TESTBED_DIR = (typeof args === 'object' && args && args.testbedDir) || P('testbed')
const PMN_SHARED_DIR = (typeof args === 'object' && args && args.pmnDir) || P('.external/cxone-cxdvi-pmn-shared')
const CODE_RCA_TEAMS = ['Titans', 'Sapphire', 'Waves']

const PRIORITY_LADDER = `
P1 = Production outage (i.e., NOC outage bridge) or critical lab escalation. Something is down and needs immediate attention to restore.
P2 = Item or feature not functioning as designed, no workaround available.
P3 = Item or feature not functioning as designed, workaround available.
P4 = User annoyances; no business impact.
`.trim()

const GOLDEN_RULES = `
1. Dashboards interpret data; they do NOT generate it.
2. Reports and raw records are higher trust than widgets.
3. Real-Time widgets != Historical widgets.
4. Mixed RT + Historical metrics inherit the SLOWEST refresh cadence.
5. View By compatibility matters. Agent level != Team level != Company level.
6. Average of averages != weighted average.
7. Handle Time must align with Active contact duration only.
8. Automated chat messages should not reset queue waiting time unless documented.
9. Global Views generally evaluate access as a union of permissions.
10. Permission failures should not render generic widget failures unless known behavior.
`.trim()

const INTAKE_SCHEMA = {
  type: 'object',
  required: ['case_key', 'summary', 'observed_vs_expected', 'product_area_guess', 'evidence_paths', 'kb_read'],
  additionalProperties: false,
  properties: {
    case_key: { type: 'string' },
    summary: { type: 'string' },
    observed_vs_expected: {
      type: 'object',
      required: ['observed', 'expected'],
      additionalProperties: false,
      properties: {
        observed: { type: 'string' },
        expected: { type: 'string' },
      },
    },
    product_area_guess: { type: 'string' },
    objects_involved: { type: 'array', items: { type: 'string' } },
    filters_and_config: { type: 'string' },
    date_range: { type: 'string' },
    user_roles: { type: 'string' },
    release_correlation: { type: 'string' },
    reproduction_steps: { type: 'array', items: { type: 'string' } },
    evidence_paths: { type: 'array', items: { type: 'string' } },
    log_excerpts: { type: 'array', items: { type: 'string' } },
    har_findings: { type: 'array', items: { type: 'string' } },
    jira_fetch_status: { type: 'string', enum: ['success', 'failed_auth', 'failed_network', 'not_requested'] },
    kb_read: { type: 'boolean' },
    open_questions: { type: 'array', items: { type: 'string' } },
  },
}

const CLASSIFICATION_SCHEMA = {
  type: 'object',
  required: ['severity', 'impact', 'product_area', 'classification', 'confidence', 'rationale'],
  additionalProperties: false,
  properties: {
    severity: { type: 'string', enum: ['P1', 'P2', 'P3', 'P4'] },
    impact: { type: 'string', enum: ['Informational', 'Reporting Impact', 'Operational Impact', 'Production Impact'] },
    product_area: { type: 'string' },
    metric_type: { type: 'string', enum: ['Real-Time', 'Near Real-Time', 'Historical', 'Calculated', 'Aggregated', 'Unknown'] },
    classification: {
      type: 'string',
      enum: ['Expected Behavior', 'Configuration Issue', 'Known Limitation', 'Documentation Gap', 'Potential Defect', 'Confirmed Defect', 'Regression'],
    },
    confidence: { type: 'string', enum: ['Low', 'Medium', 'High'] },
    rationale: { type: 'string' },
  },
}

const ROOTCAUSE_SCHEMA = {
  type: 'object',
  required: ['seven_step', 'root_cause', 'supporting_evidence', 'documentation_alignment', 'release_regression'],
  additionalProperties: false,
  properties: {
    seven_step: {
      type: 'object',
      required: ['product_area', 'object', 'metric_type', 'source_of_truth', 'dashboard_vs_sot', 'release_correlation', 'final_classification'],
      additionalProperties: false,
      properties: {
        product_area: { type: 'string' },
        object: { type: 'string' },
        metric_type: { type: 'string' },
        source_of_truth: { type: 'string' },
        dashboard_vs_sot: { type: 'string' },
        release_correlation: { type: 'string' },
        final_classification: { type: 'string' },
      },
    },
    root_cause: { type: 'string' },
    supporting_evidence: { type: 'array', items: { type: 'string' } },
    documentation_alignment: { type: 'string' },
    doc_urls_consulted: { type: 'array', items: { type: 'string' } },
    release_regression: { type: 'string' },
    matched_patterns: { type: 'array', items: { type: 'string' } },
  },
}

const VERIFY_SCHEMA = {
  type: 'object',
  required: ['refuted', 'reasoning', 'discrepancies'],
  additionalProperties: false,
  properties: {
    refuted: { type: 'boolean' },
    reasoning: { type: 'string' },
    discrepancies: {
      type: 'array',
      items: {
        type: 'object',
        required: ['claim', 'reality'],
        additionalProperties: false,
        properties: {
          claim: { type: 'string' },
          reality: { type: 'string' },
          evidence: { type: 'string' },
        },
      },
    },
  },
}

const CUSTOMER_DRAFT_SCHEMA = {
  type: 'object',
  required: ['subject', 'body'],
  additionalProperties: false,
  properties: {
    subject: { type: 'string' },
    body: { type: 'string' },
    next_step_for_customer: { type: 'string' },
  },
}

const SWARM_DRAFT_SCHEMA = {
  type: 'object',
  required: ['headline', 'summary', 'evidence', 'recommended_action'],
  additionalProperties: false,
  properties: {
    headline: { type: 'string' },
    summary: { type: 'string' },
    evidence: { type: 'array', items: { type: 'string' } },
    recommended_action: { type: 'string' },
  },
}

const ESCALATION_DRAFT_SCHEMA = {
  type: 'object',
  required: ['title', 'description', 'evidence_bundle', 'engineering_questions', 'reproduction_steps'],
  additionalProperties: false,
  properties: {
    title: { type: 'string' },
    description: { type: 'string' },
    evidence_bundle: { type: 'array', items: { type: 'string' } },
    engineering_questions: { type: 'array', items: { type: 'string' } },
    reproduction_steps: { type: 'array', items: { type: 'string' } },
    suggested_component: { type: 'string' },
  },
}

const TEAM_SCHEMA = {
  type: 'object',
  required: ['owning_team', 'match_basis', 'code_rca_eligible', 'confidence'],
  additionalProperties: false,
  properties: {
    owning_team: { type: 'string' },
    match_basis: { type: 'string' },
    matched_row: { type: 'string' },
    code_rca_eligible: { type: 'boolean' },
    confidence: { type: 'string', enum: ['Low', 'Medium', 'High'] },
    rationale: { type: 'string' },
  },
}

const COVERAGE_SCHEMA = {
  type: 'object',
  required: ['coverage_verdict', 'matched_tests', 'proposed_tests', 'creation_status'],
  additionalProperties: false,
  properties: {
    coverage_verdict: { type: 'string', enum: ['Covered', 'Partial', 'Gap', 'Unknown'] },
    xray_folder: { type: 'string' },
    testbed_source: { type: 'string', enum: ['live', 'cached', 'none'] },
    matched_tests: {
      type: 'array',
      items: {
        type: 'object',
        required: ['key', 'alignment'],
        additionalProperties: false,
        properties: {
          key: { type: 'string' },
          summary: { type: 'string' },
          alignment: { type: 'string', enum: ['aligned', 'partial', 'not-aligned'] },
          note: { type: 'string' },
        },
      },
    },
    proposed_tests: {
      type: 'array',
      items: {
        type: 'object',
        required: ['title', 'priority', 'type', 'steps'],
        additionalProperties: false,
        properties: {
          title: { type: 'string' },
          priority: { type: 'string' },
          type: { type: 'string' },
          steps: { type: 'string' },
        },
      },
    },
    creation_status: { type: 'string' },
  },
}

const CODE_RCA_SCHEMA = {
  type: 'object',
  required: ['eligible', 'suspect_paths', 'defect_hypothesis', 'proposed_fix', 'confidence'],
  additionalProperties: false,
  properties: {
    eligible: { type: 'boolean' },
    reason: { type: 'string' },
    suspect_paths: { type: 'array', items: { type: 'string' } },
    defect_hypothesis: { type: 'string' },
    proposed_fix: { type: 'string' },
    confidence: { type: 'string', enum: ['Low', 'Medium', 'High'] },
  },
}

let a = args || {}
if (typeof a === 'string') { try { a = JSON.parse(a) } catch (e) { a = {} } }
const jiraKey = a.jiraKey || ''
log(`workflow args parsed — jiraKey=${jiraKey || '(none)'} logsPath=${a.logsPath || '(none)'} priority=${a.priority || '(none)'}`)
const description = a.description || ''
const logsPath = a.logsPath || ''
const harPath = a.harPath || ''
const screenshots = a.screenshots || []
const priorityHint = a.priority || ''

phase('Intake')

const intake = await agent(
`You are the Ticket Intake stage of the CXone Swarm triage pipeline.

STEP 1 — MANDATORY: Read the knowledge base at ${KB_PATH}. Set kb_read=true in your response after doing this. Do not skip.

STEP 2 — If a Jira key was provided, fetch the live case:
  Jira key: ${jiraKey || '(none)'}
  Command: py ${JIRA_SCRIPT} ${jiraKey || '<KEY>'}
  If the command fails (auth/network), set jira_fetch_status accordingly and continue with the supplied description.

STEP 3 — Read any supplied evidence:
  Description: ${description ? JSON.stringify(description).slice(0, 4000) : '(none)'}
  Logs path: ${logsPath || '(none)'}
  HAR path: ${harPath || '(none)'}
  Screenshots: ${JSON.stringify(screenshots).slice(0, 2000)}
  Priority hint from caller: ${priorityHint || '(none)'}

STEP 4 — Produce a structured intake summary.

For log/HAR analysis: extract errors, timestamps, failed APIs (status, endpoint), permission failures (401/403), server responses. Never assume logs are irrelevant.

Return ONLY JSON matching the schema. Do not invent facts — if a field is unknown, use "unknown" or an empty array as appropriate.`,
  { schema: INTAKE_SCHEMA, agentType: 'Explore', label: 'intake', phase: 'Intake' }
)

phase('Classification')

const intakePayload = JSON.stringify(intake, null, 2).slice(0, 40000)

const classification = await agent(
`You are the Classification Engine stage of the CXone Swarm triage pipeline.

Use the intake below to assign severity (P1-P4), impact, product area, metric type, initial classification, and confidence.

PRIORITY LADDER (use these EXACT labels):
${PRIORITY_LADDER}

Impact scale: Informational / Reporting Impact / Operational Impact / Production Impact.

Classification categories (choose exactly one):
- Expected Behavior
- Configuration Issue
- Known Limitation
- Documentation Gap
- Potential Defect
- Confirmed Defect
- Regression

Confidence: Low / Medium / High.

If the caller supplied a priority hint (${priorityHint || 'none'}), honor it unless the evidence clearly contradicts (e.g., customer said P3 but the case is a production outage — override with justification in rationale).

INTAKE:
${intakePayload}

Return ONLY JSON matching the schema.`,
  { schema: CLASSIFICATION_SCHEMA, label: 'classify', phase: 'Classification' }
)

phase('RootCause')

const classificationPayload = JSON.stringify(classification, null, 2).slice(0, 8000)

const rootCause = await agent(
`You are the Root Cause Analyzer stage of the CXone Swarm triage pipeline.

Execute the 7-STEP ROOT CAUSE FRAMEWORK:
1. Product Area
2. Object (widget/metric/plan/view)
3. Metric Type (Real-Time / Near Real-Time / Historical / Calculated / Aggregated)
4. Source of Truth (Contact History / Plan Monitoring / Interaction Hub / QM Evaluations / Guide Data / Reports / raw states)
5. Compare Dashboard vs Source of Truth
6. Release Correlation (did it start after a release/update/migration?)
7. Final Classification

GOLDEN RULES (apply):
${GOLDEN_RULES}

MANDATORY: Consult NICE documentation via WebFetch against https://help.nicecxone.com before concluding. Key paths:
- /Content/Dashboards/Dashboard-widgets.htm
- /Content/Reporting/Metric-list.htm
- /Content/ACD/ACD-reporting.htm
- /Content/Quality-Management/QM-plan-monitoring.htm
- /Content/Performance-Management/PM-overview.htm
- /Content/Guide/Guide-metrics.htm

If documentation is silent, say: "This behavior is not explicitly documented but appears consistent with current product design."

Also cross-check against the knowledge base at ${KB_PATH} (22 accrued Swarm learnings). List any matched patterns in matched_patterns.

INTAKE:
${intakePayload}

CLASSIFICATION:
${classificationPayload}

Return ONLY JSON matching the schema.`,
  { schema: ROOTCAUSE_SCHEMA, label: 'rootcause', phase: 'RootCause' }
)

const rcPayload = JSON.stringify(rootCause, null, 2).slice(0, 20000)

const [docVerdict, sotVerdict] = await parallel([
  () => agent(
`You are an ADVERSARIAL VERIFIER — DOCUMENTATION ALIGNMENT SKEPTIC.

Your job: try to REFUTE the root-cause analysis by finding places where the claimed documentation alignment does not actually hold. Default to refuted=true if uncertain.

Check:
1. Are the documentation URLs listed in doc_urls_consulted real (fetch them via WebFetch and confirm they respond)?
2. Do the quoted or paraphrased passages actually appear in the NICE docs?
3. Does the "Expected/Config/Defect/Regression" classification actually match what documentation says (or is silent on)?
4. If documentation is silent, did the agent honestly say so — or did it invent a quote?

For each discrepancy, cite the claim and the reality.

ROOT CAUSE UNDER REVIEW:
${rcPayload}

Return ONLY JSON matching the schema.`,
    { schema: VERIFY_SCHEMA, label: 'verify:docs', phase: 'RootCause' }
  ),
  () => agent(
`You are an ADVERSARIAL VERIFIER — SOURCE-OF-TRUTH SKEPTIC.

Your job: try to REFUTE the root-cause analysis by challenging its source-of-truth reasoning. Default to refuted=true if uncertain.

Check:
1. Did the agent correctly identify the source of truth for this metric type? (Dashboard is NEVER the source of truth.)
2. Is the Dashboard-vs-SoT comparison logically valid? Could there be a third data source that better explains the discrepancy?
3. Are the golden rules being violated by the conclusion (e.g., claiming a widget "generates" data)?
4. Is "reproducibility" claimed without evidence?
5. If classified as Regression, is there actual customer confirmation of prior working state?

For each discrepancy, cite the claim and the reality.

ROOT CAUSE UNDER REVIEW:
${rcPayload}

Return ONLY JSON matching the schema.`,
    { schema: VERIFY_SCHEMA, label: 'verify:sot', phase: 'RootCause' }
  ),
])

const verifiers = { doc_alignment: docVerdict, source_of_truth: sotVerdict }
const bothPassed = docVerdict && sotVerdict && !docVerdict.refuted && !sotVerdict.refuted
const verifiedClassification = bothPassed
  ? classification
  : { ...classification, confidence: classification.confidence === 'High' ? 'Medium' : 'Low', rationale: `${classification.rationale} [Downgraded: adversarial verifier flagged discrepancies — see verifiers.]` }

log(`RootCause verifiers — doc:${docVerdict?.refuted ? 'REFUTED' : 'ok'}  sot:${sotVerdict?.refuted ? 'REFUTED' : 'ok'}`)

phase('TeamAssignment')

const teamAssignment = await agent(
`You are the TEAM ASSIGNMENT stage of the CXone Swarm triage pipeline.

Read the ownership map at ${TEAM_ASSIGNMENT_PATH} and route this bug to its owning engineering team.

RESOLUTION RULE (ordered — never guess):
1. Match the RCA object/widget (including the aliases/source spellings listed) against Table A.
2. Else match the product area / symptom against Table B.
3. Else owning_team = "Unresolved".
A widget whose Team cell is marked deprecated → owning_team = "N/A (deprecated)".

Set code_rca_eligible = true ONLY if owning_team is exactly one of: ${CODE_RCA_TEAMS.join(', ')}.
match_basis must be 'widget:"<name>"', 'area:"<name>"', or 'unresolved'.

ROOT CAUSE (contains the object/widget and product area to route on):
${rcPayload}

Return ONLY JSON matching the schema.`,
  { schema: TEAM_SCHEMA, label: 'team', phase: 'TeamAssignment' }
)

const eligible = !!(teamAssignment && teamAssignment.code_rca_eligible === true)
log(`TeamAssignment — team=${teamAssignment?.owning_team || 'unknown'} codeRCA=${eligible ? 'eligible' : 'skipped'}`)

phase('Coverage & Code')

const teamPayload = JSON.stringify(teamAssignment, null, 2).slice(0, 4000)

const [coverage, codeRCA] = await parallel([
  () => agent(
`You are the TEST-CASE COVERAGE stage of the CXone Swarm triage pipeline.

Test bed = CXDV Xray Test Repository (Jira project CXDV, internal id 10095).

STEP 1 — Map the widget to its Xray Test Repository folder using ${TESTBED_DIR}/widget-reference.md.
STEP 2 — Shortlist existing tests from the cached per-folder lists ${TESTBED_DIR}/raw-data/widget-folders-raw.json and ${TESTBED_DIR}/raw-data/report-folders-raw.json (each entry has key + summary). Set testbed_source="cached" (or "none" if nothing relevant is cached). Match on the widget name AND metric synonyms, case-insensitively and typo-tolerantly — the test bed may label a metric differently from the bug (e.g. "Longest Wait" is filed as "Longest delay") and summaries contain typos (e.g. "Lengest"). Searching the bug's exact phrase alone risks a FALSE Gap; cross-check metric aliases before concluding no coverage exists.
STEP 3 — Alignment: compare each candidate's summary/steps against the bug's observed-vs-expected and reproduction. Mark each aligned / partial / not-aligned.
STEP 4 — Verdict:
  - "Covered": an aligned existing test exists → list it in matched_tests; leave proposed_tests empty; creation_status="n/a".
  - "Gap"/"Partial": no aligned test → author ONLY 1-2 JIRA-ready scenario(s) in proposed_tests that DIRECTLY reproduce/cover THIS bug (one primary reproduction path, plus at most one for a distinct facet of the same bug) following the persona at ${TESTCASE_PERSONA_PATH} (mandatory navigation steps, JIRA schema). Do NOT emit a broad functional/negative/edge/accessibility matrix — scope strictly to this defect. Put the full description + numbered steps + expected results in each proposed_tests[].steps. creation_status="draft-only (awaiting confirm)".

IMPORTANT: This pipeline is DRAFT-ONLY. Do NOT create, update, approve, or otherwise write anything to Jira/Xray. Creation happens later through the interactive agent's confirm gate.

INTAKE:
${intakePayload}

ROOT CAUSE:
${rcPayload}

Return ONLY JSON matching the schema.`,
    { schema: COVERAGE_SCHEMA, label: 'coverage', phase: 'Coverage & Code' }
  ),
  () => eligible
    ? agent(
`You are the CODE-LEVEL RCA stage of the CXone Swarm triage pipeline.

The owning team (${teamAssignment?.owning_team}) is code-RCA eligible. Inspect the ALREADY-CLONED repo at ${PMN_SHARED_DIR} (branch develop) — the ClearView Core .NET 8 / Angular monorepo — to locate the defect and propose a fix.

READ-ONLY GUARANTEE: never edit, stage, commit, or branch in that repo. Output a *suggested* fix for R&D, not an applied change.

1. Orient using ${PMN_SHARED_DIR}/AGENTS.md and ${PMN_SHARED_DIR}/docs/ARCHITECTURE.md.
2. Locate the widget's code across tiers:
   - Back-end / BLL:  ClearView Shared Framework/Dashboard/Widgets
   - Data Visualization API:  Data Visualization API/Controllers/Dashboard/Widgets
   - Front-end:  ClearView Source/src/app/dashboard
   - Models / DAL:  ClearView Data Models (+ Mongo/SQL framework implementations)
   Grep/Glob on the widget name, metric, endpoint, or the symptom keywords from the bug.
3. Provide suspect_paths, defect_hypothesis, and a concise proposed_fix (a diff or a precise change description) with a confidence rating.
If the repo is unreadable, set eligible=true but explain the blocker in reason and keep proposed_fix conservative — do NOT fabricate code paths.

ROOT CAUSE:
${rcPayload}

OWNING TEAM:
${teamPayload}

Return ONLY JSON matching the schema.`,
        { schema: CODE_RCA_SCHEMA, label: 'coderca', phase: 'Coverage & Code' }
      )
    : Promise.resolve({
        eligible: false,
        reason: `Owned by ${teamAssignment?.owning_team || 'an unresolved team'}; code RCA only runs for ${CODE_RCA_TEAMS.join('/')}.`,
        suspect_paths: [],
        defect_hypothesis: '',
        proposed_fix: '',
        confidence: 'Low',
      }),
])

log(`Coverage — verdict=${coverage?.coverage_verdict || 'unknown'}  proposed=${coverage?.proposed_tests?.length || 0}  |  CodeRCA — ${codeRCA?.eligible ? 'analyzed' : 'skipped'}`)

phase('Drafts')

const finalPayload = JSON.stringify({
  intake,
  classification: verifiedClassification,
  rootCause,
  verifiers,
  teamAssignment,
  coverage,
  codeRCA,
}, null, 2).slice(0, 80000)

const [customer, swarm, escalation] = await parallel([
  () => agent(
`You are the CUSTOMER RESPONSE DRAFTER for a CXone Swarm case.

Tone: professional, blame-free, calm, authoritative. No speculation presented as fact. Acknowledge the customer's report, explain findings in plain language, and give a concrete next step.

If the classification is Expected Behavior or Configuration — explain why and give the fix.
If Documentation Gap — acknowledge the docs are silent and give the design rationale.
If Potential/Confirmed Defect or Regression — acknowledge escalation to engineering; give an expected next update cadence but never a fix ETA unless one is in evidence.

CONTEXT:
${finalPayload}

Return ONLY JSON matching the schema.`,
    { schema: CUSTOMER_DRAFT_SCHEMA, label: 'draft:customer', phase: 'Drafts' }
  ),
  () => agent(
`You are the INTERNAL SWARM UPDATE DRAFTER for a CXone Swarm case.

Audience: fellow Swarm engineers and TAMs. Concise, evidence-based, ready to paste into the Swarm thread. Include: headline (one line), summary (2-3 sentences), evidence bullets, and one clear recommended action.

Reflect the routing in your update: name the owning team (from teamAssignment) and state the test-coverage verdict (covered → cite the test key; gap → note a new test is drafted). If a code-level fix was proposed (codeRCA.eligible), mention the suspect path in one evidence bullet.

CONTEXT:
${finalPayload}

Return ONLY JSON matching the schema.`,
    { schema: SWARM_DRAFT_SCHEMA, label: 'draft:swarm', phase: 'Drafts' }
  ),
  () => agent(
`You are the R&D ESCALATION NOTE DRAFTER for a CXone Swarm case.

Audience: R&D engineers who did not attend the swarm. Include: title, description, evidence bundle (paths / log excerpts / API failures / doc URLs), engineering questions that would sharpen the diagnosis, reproduction steps, and a suggested component / squad if inferable.

Use the routing context: set suggested_component to the owning team (teamAssignment.owning_team). When codeRCA.eligible is true, add its suspect_paths and proposed_fix to the evidence_bundle and reference them in the description. Add any aligned existing test key(s) and drafted proposed-test titles (from coverage) to the evidence_bundle so R&D can see the coverage state.

Only produce a full escalation note if classification is Potential Defect / Confirmed Defect / Regression. Otherwise return a stub with title="No escalation recommended" and description explaining why.

CONTEXT:
${finalPayload}

Return ONLY JSON matching the schema.`,
    { schema: ESCALATION_DRAFT_SCHEMA, label: 'draft:escalation', phase: 'Drafts' }
  ),
])

phase('Render')

const REPORTS_DIR = P('reports')

const RENDER_SCHEMA = {
  type: 'object',
  required: ['markdown', 'case_key'],
  additionalProperties: false,
  properties: {
    markdown: { type: 'string' },
    case_key: { type: 'string' },
  },
}

const rendered = await agent(
`You are the FINAL RENDERING stage of the CXone Swarm triage pipeline.

Synthesize the intake, classification, root cause, verifiers, team assignment, coverage, code RCA, and drafts below into a SHORT, TABULAR report. Aim for ~60-90 lines total. Use ONLY tables and short bullets — no prose paragraphs outside tables and the one blockquote at the end.

RULES:
- Priority uses the P1-P4 ladder from Classification (customer/workaround axis).
- Severity is a SEPARATE axis (Critical / High / Medium / Low) describing engineering blast radius. A P3 with a workaround can still be High severity.
- Keep every table cell under 30 words.
- Preserve adversarial-verifier caveats: if verifiers downgraded confidence or refuted a claim, reflect it in the RCA short-bullets (e.g. "hypothesis fails arithmetic", "not yet determined").
- Team Assignment: fill from teamAssignment (owning_team, match_basis, code_rca_eligible, confidence).
- Test Coverage: fill from coverage — verdict, existing aligned test key(s), and (on Gap/Partial) the drafted proposed-test titles. Never claim a test was created — this pipeline is draft-only.
- Code RCA & Suggested Fix: if codeRCA.eligible, give suspect path(s) + defect + proposed fix; else one line stating the owning team and that code RCA is out of scope.
- Customer Response blockquote: <=80 words, blame-free, no fix ETA, no square-bracket placeholders.
- Bold key object names on first mention.
- Never invent facts — only condense what is in the source below.
- The case_key field must match intake.case_key.

EXACT TEMPLATE (fill in, do not restructure):

## <CASE_KEY> — Triage Summary

| Field | Value |
|-------|-------|
| Issue | <one line, <=25 words> |
| Priority | <P1 | P2 | P3 | P4> |
| Severity | <Critical | High | Medium | Low> |
| Classification | <one of the 7 buckets, optionally "(hypothesis)" if verifiers downgraded> |
| Confidence | <Low | Medium | High> <"(downgraded by verifier)" if applicable> |
| Product Area | <from rootCause.seven_step.product_area> |
| Metric Type | <RT | NRT | Historical | Calculated | Aggregated | N/A> |
| Impact | <Informational | Reporting Impact | Operational Impact | Production Impact> |

## Evidence Snapshot

| Signal | Value |
|--------|-------|
| <e.g. Endpoint>       | <value> |
| <e.g. Observed>       | <value> |
| <e.g. Expected>       | <value> |
| <e.g. Server signal>  | <value> |
| <e.g. Log excerpt>    | <value> |
| <e.g. Retries>        | <value> |
| <e.g. Release corr.>  | <value> |

## Justifications

| Axis | Justification |
|------|---------------|
| Priority <P#> | <=30 words |
| Severity <level> | <=30 words |

## RCA (Short)

- <=3 bullets. First: what is PROVEN from evidence. Second: what hypothesis fails / what remains unproven. Third: any secondary defect confirmed independently. If RCA is genuinely unknown, first bullet must start "Root cause not yet determined".

## Team Assignment

| Field | Value |
|-------|-------|
| Owning Team | <owning_team> |
| Match Basis | <match_basis> |
| Code-RCA Eligible | <Yes | No> |
| Confidence | <Low | Medium | High> |

## Test Coverage

| Field | Value |
|-------|-------|
| Verdict | <Covered | Partial | Gap | Unknown> |
| Existing Test(s) | <CXDV-##### (aligned) | -> |
| Xray Folder | <folder path or -> |
| Action | <None - covered | Draft new (awaiting confirm) | Update-by-new> |

- On Gap/Partial: list proposed JIRA-ready case title(s) + priority as bullets. (Draft only — not created.)

## Code RCA & Suggested Fix

- If eligible: bullet 1 = suspect path(s) in cxone-cxdvi-pmn-shared; bullet 2 = defect mechanism; bullet 3 = proposed fix + confidence. Else: one bullet — "Owned by <team>; code RCA out of scope (only Titans/Sapphire/Waves)."

## Info Required

| # | Item |
|---|------|
| 1 | ... |
| 2 | ... |
| 3 | ... |
| 4 | ... |
| 5 | ... |
| 6 | ... |

## Next Actions

| Owner | Action |
|-------|--------|
| Engineering | <specific R&D action, <=40 words> |
| Support | <specific Swarm/TSE/TAM action, <=40 words> |
| Customer | <specific customer action, <=40 words> |

## Risks / Blockers

| # | Risk |
|---|------|
| 1 | ... |
| 2 | ... |
| 3 | ... |
| 4 | ... |

## Customer Response (paste-ready)

> <=80 words, blame-free, no fix ETA. Single blockquote, no line breaks within the quote unless natural sentence breaks.

--- END TEMPLATE ---

CONTEXT:
${finalPayload}

Return ONLY JSON matching the schema. The "markdown" field must contain the fully-rendered template (with the case key substituted into the H2 header). The "case_key" field must be a filesystem-safe token (letters/digits/hyphens only — replace any others with '-').`,
  { schema: RENDER_SCHEMA, label: 'render', phase: 'Render' }
)

const saved = await agent(
`Save the following markdown report to disk. Use this exact procedure — do not deviate.

1. Ensure the directory exists: run \`mkdir -p "${REPORTS_DIR}"\`.
2. Write the markdown to: \`${REPORTS_DIR}/${rendered.case_key}-triage.md\`. Use the Write tool with the exact contents below (no wrapping, no code fences, no extra prefix/suffix).
3. Run \`ls -la "${REPORTS_DIR}/${rendered.case_key}-triage.md"\` to confirm the file exists and report the path back.
4. Return ONLY a JSON object matching the schema: {"saved_path": "<absolute path of the written file>"}.

MARKDOWN CONTENT TO WRITE (verbatim, do not modify):
---BEGIN---
${rendered.markdown}
---END---`,
  {
    schema: {
      type: 'object',
      required: ['saved_path'],
      additionalProperties: false,
      properties: { saved_path: { type: 'string' } },
    },
    agentType: 'Explore',
    label: 'save',
    phase: 'Render',
  }
)

log(`report saved to ${saved.saved_path}`)

return {
  intake,
  classification: verifiedClassification,
  rootCause,
  verifiers,
  teamAssignment,
  coverage,
  codeRCA,
  drafts: { customer, swarm, escalation },
  rendered,
  saved,
}
