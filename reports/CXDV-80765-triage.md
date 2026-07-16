## CXDV-80765 — Triage Summary

| Field | Value |
|-------|-------|
| Issue | **Coaching Transactional Report** export coerces free-text strings like "1:1"/"1-1" into DateTime values in CSV and XLSX. |
| Priority | P2 |
| Severity | High |
| Classification | Regression (hypothesis) |
| Confidence | Medium (downgraded by verifier) |
| Product Area | Dashboard & Reporting -> Prebuilt Reports -> Coaching Transactional Report -> Export Engine (server-side) |
| Metric Type | Historical |
| Impact | Reporting Impact |

## Evidence Snapshot

| Signal | Value |
|--------|-------|
| Report / Widget | **Coaching Transactional Report** / Coaching Transaction details |
| Affected Fields | Coaching Type, Session Name, Focus Area |
| Observed | "1:1" -> "7/8/2026 1:01 AM"; "1-1" -> "1/1/2026" in CSV and XLSX |
| Expected | String values preserved verbatim, matching widget UI and legacy BI Reports |
| Control | Non-date-like strings ("Coach The Coach", "Communication Skills") round-trip correctly |
| CSV artifact | Coerced value additionally wrapped as `="7/8/2026 1:01 AM"` |
| Release corr. | Post BI Reports migration; related 26.2 fixes CXDV-75569/75570/75953 (scope unread) |
| Tenant | MERCURY NEW ZEALAND, Cluster A33, Instance AU1, BU 4610704 (single-tenant repro) |

## Justifications

| Axis | Justification |
|------|---------------|
| Priority P2 | Feature is broken with no user-side workaround: coerced rows cannot be reconstructed after export. Dashboard renders correctly, so not P1. |
| Severity High | Silent data corruption in exported coaching records misleads auditing and downstream consumers; deterministic pattern likely affects any tenant with date-shaped strings. |

## RCA (Short)

- Proven: widget renders "1:1"/"1-1" correctly while CSV/XLSX exports emit deterministic DateTime coercions; non-date-shaped strings survive, isolating the defect to pattern-based type inference somewhere in the export path.
- Unproven / downgraded: "Regression from BI migration" lacks legacy baseline evidence; pin to the export engine specifically is over-committed — verifier notes an intermediate report-data/query projection or serializer could be the mutator; cross-tenant reproducibility is predicted, not observed.
- Secondary: CSV `="..."` formula wrapper protects an already-coerced value, indicating the mutation occurs upstream of the CSV writer (independent of Excel client auto-format).

## Info Required

| # | Item |
|---|------|
| 1 | Confirmation from customer or legacy build that identical strings exported cleanly pre-BI-migration (baseline for Regression claim). |
| 2 | Repro on a second tenant/cluster and different date range to validate scope beyond A33/AU1. |
| 3 | Read of CXDV-75569 / CXDV-75570 / CXDV-75953 fix scopes to confirm they did not touch string-column inference. |
| 4 | Inspection of intermediate report-data payload (pre-CSV/XLSX writer) and XLSX cell-type metadata to locate the coercion. |
| 5 | Direct SoT check against coaching records DB (PM/QM store) to confirm strings are intact server-side. |
| 6 | Cross-report audit of other prebuilt reports with free-text colon/hyphen fields (agent notes, category/disposition tags). |

## Next Actions

| Owner | Action |
|-------|--------|
| Engineering | Instrument export pipeline; capture intermediate payload, identify the layer performing DateTime inference, and enforce explicit string schema for Coaching Type, Session Name, Focus Area. |
| Support | Read the three 26.2 fix tickets, request customer confirmation of prior working behavior, and attempt repro on a second tenant/date range before finalizing Regression classification. |
| Customer | Provide any pre-migration export samples if retained; confirm impacted downstream consumers and whether corrupted exports have already been distributed. |

## Risks / Blockers

| # | Risk |
|---|------|
| 1 | No user-side workaround: once exported, coerced rows are unrecoverable without re-export after fix. |
| 2 | Defect location not yet isolated; fix may span serializer, query projection, or export writer. |
| 3 | Same inference likely mis-corrupts other prebuilt reports containing free-text tokens matching date/time shapes. |
| 4 | Public NICE documentation URLs returned HTTP 404, so no external contract to anchor expected behavior. |

## Customer Response (paste-ready)

> Thank you for the detailed repro on the Coaching Transactional Report export. We have confirmed that the widget renders your coaching values correctly and that specific text patterns (such as "1:1" and "1-1") are being reshaped into date/time values only during CSV and XLSX export, while other free-text values export intact. Engineering is investigating the export path and related recent changes. We will follow up with progress updates and any interim guidance as soon as the responsible layer is identified.
