# CXDV Xray Test Repository — Analysis

Synced 2026-07-15 from the Jira/Xray Cloud project **CXDV** (CXone Data Visualization / CXCV Dashboard), via the Xray Cloud GraphQL API. This snapshot is bundled with TriageIQ so the test-bed lookup works offline.

## Files

- `widget-reference.md` — all 37 CXCV Dashboard widgets (ACD, QM, Coaching, Metrics, PM, IA) cross-referenced against their Xray Test Repository folders, with test counts and sample scenario titles/keys.
- `report-reference.md` — prebuilt report test coverage (Report Queries, ACD/QM Reports, Custom Reporting) mapped to the `dv-report-template` suite structure.
- `lessons-learned.md` — how the Xray Cloud GraphQL API works in this tenant (auth flow, folder-by-path querying, project ID resolution) and CXDV project scope notes.
- `raw-data/` — raw JSON API responses backing the above:
  - `cxdv-full-folder-tree.json` — full Test Repository folder tree (`getFolder(path:"/")`, recursive) with per-folder test counts.
  - `widget-folders-raw.json` — per-widget-folder `getTests` results (key + summary).
  - `report-folders-raw.json` — per-report/functional-folder `getTests` results.
  - `cxdv-first-100-tests.json` — first 100 tests project-wide (`project = CXDV`), used to resolve the internal project ID (`10095`).

## Credentials

Xray Cloud API credentials (`client_id`/`client_secret`) are **not** stored here — TriageIQ reads them from `webapp/config.env` at runtime. See `lessons-learned.md` for the auth flow if you need to re-query or refresh this data.
