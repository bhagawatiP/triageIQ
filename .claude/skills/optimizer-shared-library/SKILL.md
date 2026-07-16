---
name: optimizer-shared-library
description: "Shared Xray Cloud GraphQL client, verified JQL builder, TOON reader/writer, and working-folder path helpers imported by the manual and automation test-cases-optimizer skill scripts. Library module - not invoked directly."
---

# Optimizer Shared Library

**This library exists only to be imported by the other skills' own scripts, on behalf of the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent - it has no action of its own to invoke directly.** Not a runnable skill on its own - every other skill in this plugin imports from here.

| File | Purpose |
|------|---------|
| `${CLAUDE_SKILL_DIR}/scripts/xray_client.py` | Auth + GraphQL call against Xray Cloud. Reads `XRAY_CLIENT_ID`/`XRAY_CLIENT_SECRET` from the environment. |
| `${CLAUDE_SKILL_DIR}/scripts/jql_builder.py` | The single source of truth for JQL against a Test Plan/Set/Execution/Repository, including the verified `issueFunction in testPlanTests/testSetTests/testExecutionTests(...)` pattern (the nested `tests()` connection does not accept a `jql` argument - this was confirmed against the live API) and the positive-allow-list `testType` clause convention. |
| `${CLAUDE_SKILL_DIR}/scripts/toon_io.py` | Generic TOON (Token-Oriented Object Notation) reader/writer. No domain schema knowledge. |
| `${CLAUDE_SKILL_DIR}/scripts/report_paths.py` | Resolves the `test-cases-optimizer-work/` folder (at the git repo root, or cwd if not in a repo) and every fixed artifact path inside it. |

No test-taking or classification logic lives here - only Xray access, JQL construction, serialization, and paths.