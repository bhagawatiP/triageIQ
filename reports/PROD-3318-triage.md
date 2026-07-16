## PROD-3318 — Triage Summary

| Field | Value |
|-------|-------|
| Issue | **Monthly revenue export** for June 2026 returns HTTP 200 with 0 rows; outgoing range carries invalid month=13. |
| Priority | P3 |
| Severity | High |
| Classification | Regression (hypothesis) |
| Confidence | Medium (downgraded by verifier) |
| Product Area | Reporting / Report Export — Monthly Revenue Export (date-range handling in POST /v1/reports/export) |
| Metric Type | Historical |
| Impact | Reporting Impact |

## Evidence Snapshot

| Signal | Value |
|--------|-------|
| Endpoint | **POST /v1/reports/export** (rep_5521, rep_5522) |
| Observed | HTTP 200, 0 rows in CSV; outgoing range `2026-13-01..2026-13-31` |
| Expected | Range `2026-06-01..2026-06-30` returning June revenue rows, or 4xx on invalid filter |
| Server signal | WARN `reports date filter parsed to invalid month=13, coercing to empty range` |
| Log excerpt | `INFO api request rep_5521 succeeded status=200 rows=0` |
| Retries | Two client retries (~68s apart) yielded identical invalid range |
| Release corr. | **PROD-3301** date-picker change MM/DD/YYYY -> ISO YYYY-MM-DD (~2 weeks prior) |

## Justifications

| Axis | Justification |
|------|---------------|
| Priority P3 | Single internal FinOps user impacted; manual workaround exists; no external customer impact; needed before month-end close. |
| Severity High | Export path unusable via UI; API silently returns 200 on invalid filter masking failures — engineering blast radius across report exports. |

## RCA (Short)

- Proven from logs: client sends range `2026-13-01..2026-13-31`; reports service parses invalid month=13 and coerces to empty range, yielding 200/0 rows; deterministic across two retries.
- Hypothesis (specific mechanism) fails arithmetic: JS `getMonth()+1` double-increment on June (5) yields 7, not 13; month=13 is the December fingerprint. End-day was also mutated (30 -> 31), so corruption is broader than "month-only" — exact formatter defect not yet determined.
- Secondary defect confirmed independently: API returns 200 for unparseable date filter instead of 4xx validation error; regression tie to PROD-3301 is a strong correlation, not yet confirmed by diff or prior-working-state evidence.

## Info Required

| # | Item |
|---|------|
| 1 | Git diff / blame of PROD-3301 date-picker and ISO formatter code paths |
| 2 | Prior successful export log entry (or customer confirmation) establishing prior working state |
| 3 | Browser, timezone, and locale of the FinOps user at time of repro |
| 4 | Whether other report types (weekly, quarterly) or other months (Dec 2025, May 2026) also emit malformed ranges |
| 5 | Raw revenue dataset query with valid June 2026 range to confirm data-store integrity |
| 6 | HAR / network capture of the export request showing the exact client-sent payload |

## Next Actions

| Owner | Action |
|-------|--------|
| Engineering | Diff PROD-3301 for date formatter defect; add 4xx validation in reports service for unparseable date filters; add unit tests covering month boundaries. |
| Support | Reproduce in second browser/timezone; capture HAR; confirm scope across report types; document manual workaround for FinOps user through month-end close. |
| Customer | Use manual workaround (e.g., date entry via API or alternate flow) to complete June month-end close; share browser and locale details with support. |

## Risks / Blockers

| # | Risk |
|---|------|
| 1 | Month-end close deadline pressure if workaround is not sustainable |
| 2 | Silent 200/empty response may be masking similar defects in other report exports |
| 3 | Specific root-cause mechanism unproven — fix may miss the true defect surface |
| 4 | Prior-working-state not evidenced; classification could shift from Regression to latent defect on further review |

## Customer Response (paste-ready)

> Thanks for flagging the empty June 2026 revenue export. Our logs show the export request is reaching the reports service with an unrecognized date range, so it returns successfully but with no rows. We are actively investigating the request-construction path and the missing validation error, and we will keep you updated. In the meantime, please continue with the manual workaround so your month-end close is not blocked, and share your browser and timezone so we can accelerate reproduction.
