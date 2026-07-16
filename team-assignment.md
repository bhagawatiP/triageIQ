# CXCV Dashboard — Widget / Area → Team Assignment

Ownership map used by the `cxone-swarm-sme` agent (§16) and the `cxone-swarm-triage`
workflow (`TeamAssignment` phase) to route a triaged bug to its owning engineering team.

## Team roster

`Waves` · `Agni` · `Sapphire` · `Titans` · `Dragonfly` · `Hornet`

## Code-RCA-eligible teams

Only bugs owned by **Titans**, **Sapphire**, or **Waves** trigger the code-level RCA
step (§18 / `CodeRCA` phase) against `C:\Code\cxone-cxdvi-pmn-shared`. For any other
team (Agni, Dragonfly, Hornet) the triage stops at team routing — no code inspection.

## Resolution rule (apply in order — never guess)

1. Match the RCA **object / widget** against **Table A** (widget name or a listed alias).
2. Else match the product **area / symptom** against **Table B**.
3. Else emit `Team: Unresolved — needs manual routing` and set `code_rca_eligible = false`.

A widget whose Team cell reads *deprecated* is not owned by a live team — report it as
`Team: N/A (widget deprecated)` and do not run code RCA.

## Table A — Widget → Team

| Domain | Widget | Aliases / source spelling | Team |
|--------|--------|---------------------------|------|
| ACD | Agent State Counter | | Waves |
| ACD | Agent List | | Agni |
| ACD | Agent State Summary | | Sapphire |
| ACD | Contact States by Skill | | Sapphire |
| ACD | Contact List | | Titans |
| ACD | Service Level | | Agni |
| ACD | Queue Counter | | Waves |
| ACD | Callback Requests | Callback Request | Sapphire |
| ACD | Report | Reports | Titans |
| ACD | Call Arrival | Call Arrival Widget | Titans |
| ACD | Disposition | Dispostition, Dispositions | Waves |
| ACD | Agent Contact View | | Agni |
| ACD | Interaction Summary | Interactions Summary | Agni |
| QM | Category Over Time | Category OverTime | Waves |
| QM | Evaluations and Coaching Events | Evaluation and Coaching Events | *deprecated* |
| QM | Evaluations and Coaching Trend | Evaluation and Coaching Trend | Agni |
| QM | Evaluator Calibration | | Sapphire |
| QM | Evaluator Performance | | Titans |
| QM | Forms Calibrations | Forms Calibration, Form Calibration | Waves |
| QM | Quality Score | Quality Score Widget | Sapphire |
| QM | Top Categories | | Titans |
| QM | Plan Status | Plan Status Widget | Sapphire |
| QM | Quality Evaluations | Quality Evaluation Widget | Titans |
| Analytics | Frustration | Frustation | Agni |
| Coaching | Coaching Status | Coaching Status Widget | Waves |
| Metrics | KPI | KPI Widget | Agni |
| Metrics | KPI Trend | | Waves |
| Metrics | Metrics Interval | Metric Interval | Waves |
| Metrics | Metrics Summary | Metric Summary | Agni |
| Metrics | Gauge | Gauge widget | Titans |
| Metrics | Leaderboard | LeaderBoard | Sapphire |
| Metrics | Metric Breakdown | | Sapphire |
| Metrics | Metric Review | Metrics Review | Titans |
| WFM | Out of Adherence | Out of Adherence Cause, OOA Cause | Agni |
| WFM | Agent Adherence | Agent Adherence Variance | Titans |

## Table B — Area / Symptom → Team (fallback)

| Area / Symptom | Aliases | Team |
|----------------|---------|------|
| Manage Dashboard & Share by Roles | dashboard sharing, share by roles, manage dashboard | Agni |
| Reports | prebuilt reports, report export, report template, report queries | Titans |
| Web Apps | webapp, web-app, cxcv-dashboard webapp | Waves |
| New tenant / Tenant spin-up | tenant provisioning, new tenant, tenant creation, tenant onboarding, tenant segmentation setup | Waves |
| Data mismatch issue | value mismatch, widget vs source-of-truth mismatch, wrong numbers | Dragonfly |
| Data not loading or no data flowing | no data, empty widget, endless loading, refreshing forever | Hornet |

## Provenance & re-sync

- **Source of truth:** `C:\Users\bhagawatip\OneDrive - NICE Ltd\Work\Testing\Sparkathon.xlsx`
  (`Sheet1`; Table 1 = rows 2–34 widget→team, Table 2 = rows 37–43 area→team).
- **Jira Team fields:** *read* from `customfield_10040`; *written* by `xray-create-test --team` to `customfield_10098`. Pass the Table-A/B team name as `--team` when creating coverage.
- **Cleaning applied vs source:** normalized casing (`waves`→`Waves`), trimmed trailing
  spaces on `Agni`, preserved original misspellings as aliases (`Frustation`,
  `Dispostition`). Row 13 (`Evaluations and Coaching Events`) has no team in source — its
  cell literally reads "Widget is deprecated"; recorded here as *deprecated*.
- **Manual additions (NOT in the source xlsx — preserve on re-sync):** Table A WFM rows
  `Out of Adherence → Agni` and `Agent Adherence → Titans`; Table B row
  `New tenant / Tenant spin-up → Waves`. Added 2026-07-15 by user request.
- **To re-sync:** the workbook is normally exclusive-locked by Excel/OneDrive; read a
  Volume Shadow Copy snapshot (`esentutl /y /vss "<path>" <dest>`) rather than the live
  file, then regenerate Tables A/B above — and re-apply the manual additions listed above.
