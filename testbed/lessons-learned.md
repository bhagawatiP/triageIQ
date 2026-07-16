# Lessons Learned

Read this file FIRST before consulting widget-reference.md or metrics-reference.md — entries here may override/refine those references.

### [2026-07] — Xray Cloud API access for test-scenario lookups
- Tried: Accessing the CXDV Xray Test Repository directly via the Jira web URL (`nice-ce-cxone-prod.atlassian.net/projects/CXDV...testing-board...`).
- Failed: `WebFetch` cannot authenticate to the Atlassian-Connect Xray plugin UI — it's behind login, no MCP Atlassian connector was available in-session.
- Worked: Authenticated directly against the **Xray Cloud REST/GraphQL API** (`https://xray.cloud.getxray.app`) using `client_id`/`client_secret` (Xray API keys, distinct from a Jira user account) via `POST /api/v1/authenticate`, then queried `POST /api/v2/graphql` with a bearer token for `getTests`, `getFolder`, and folder-scoped `getTests(folder: {path, includeDescendants})`.
- Rule: Xray Cloud GraphQL's `getTests` folder filter takes a `folder: {path: String!, includeDescendants: Boolean}` argument requiring `projectId` (the Jira **internal numeric** project ID, e.g. `10095` for CXDV — not the key). Resolve it once via `getTests(jql: "project = CXDV", limit: 1) { results { jira(fields: ["project"]) } } }`. The JQL function name for folder scoping (e.g. `testRepositoryFolderTests(...)`) does **not** exist in this Xray Cloud tenant/version — use the `folder` argument on `getTests` instead, not JQL.
- Rule: `getFolder(projectId, path)` takes a folder **path** string (e.g. `"/Dashboards/ACD"`), NOT the numeric folder ID shown in the Xray UI URL (`selectedFolder=...` hash) — that UI ID isn't queryable via this GraphQL schema. Walk down from `path: "/"` and match folder names instead.
- Rule: Credentials are stored in `configuration/local.properties` (gitignored) as `JIRA_AUTH_CREDS={"client_id":"...","client_secret":"..."}` — this is the exact format `getJiraAuth()` in `secretManagementUtils/secretParser.ts` already parses (see `e2e/utils/reporter/xrayJiraReporter.ts` for the consumer). Reuse this key rather than inventing a new env var name.
- Tags: xray, jira, graphql, api-access, test-repository, credentials

### [2026-07] — CXDV project scope is broader than this repo
- Tried: Enumerating the full CXDV Test Repository folder tree (`getFolder(path:"/")`) to find widget/dashboard/report test coverage.
- Worked: The relevant folders for this repo's product (CXCV Dashboard) are `/CXDV_20Mar2026/Dashboards/*`, `/CXDV_20Mar2026/New widgets- Waves & Technocrats/*`, `/CXDV_20Mar2026/Dragonfly/*` (per-release QA + Report Queries), `/CXDV_20Mar2026/Custom Reporting`, `/CXDV_20Mar2026/Reporting Template ACD Permissions-Waves`, and the legacy `/CXDV_20Mar2026/Guardians (Only for Reference)/*` tree.
- Failed/Noise: CXDV also contains folders for unrelated sub-teams sharing the same Jira project — `/Gamification`, `/Application Analytics`, `/ETL`, `/CXCV IL`, `/CXDV_20Mar2026/BI Reports MSTR` (841 tests — appears to be a separate MicroStrategy-based reporting product, not the CXCV Dashboard "Reports" this repo automates). Filter these out when searching broadly.
- Rule: When querying CXDV for test scenarios, always scope to the folders above — a bare `jql: "project = CXDV"` returns 12,700+ tests spanning many unrelated products/teams.
- Tags: xray, scope, cxdv, test-repository

## Distillation Log

| Date | Distilled into | Source lessons |
|------|-----------------|-----------------|
| — | — | (none yet — fewer than 3 lessons per widget/feature so far) |
