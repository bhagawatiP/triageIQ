---
name: scan-automation-repo
description: "One bulk, local, framework-agnostic (Playwright/Cypress/Selenium/JUnit/TestNG/pytest/Cucumber/RSpec/generic) pass over the cloned automation repo, indexing every test block's folder/file/name and any nearby Jira-style ID - from the title, a comment/decorator/tag on the lines directly above, or inherited from an enclosing describe/context block's own ID. Writes the full index to a cache file inside the clone (auto-deleted by clone-automation-repo's cleanup) and prints only a compact per-folder count summary - never dumps thousands of individual test names into the agent's context. Serves both automation-agent modes. Read-only, no network access."
---

# Scan Automation Repo

**This skill exists only to be used by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as one step of their documented workflow - never called ad hoc by a general-purpose assistant piecing together its own duplicate-detection process.** If you are not currently running as one of those two agents, stop and delegate this request to them instead of using this skill directly - they carry critical safety rules (asking before cloning a repo, running in parallel, cross-checking for conflicts) that do not exist anywhere else, and skipping them by using skills piecemeal has previously caused this tool to silently produce wrong or misleading results.

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/scan-automation-repo/scripts/scan_automation_repo.py" --repo-path <path from clone-automation-repo>
```

Returns `scanCachePath`, `filesScanned`, `testsFound`, and `folderSummary` (folder path -> test count) - enough for the agent to judge, from folder **names and counts alone**, whether the repo's structure is genuinely functional (like `EndToEndTest/E2ERegression/{ACD,Coaching,WFM,...}`) or deployment/test-tier-organized (like `SanityTest/`, `FailoverTest/`, `PostValidationTest/`) - without spending tokens reading every test name up front.

**One scan feeds both modes** - downstream scripts read `scanCachePath` directly:
- **Mode A** (bounded source): `match-automation-tests-by-id` intersects the cache's `possibleId` values against the Jira ID list.
- **Mode B** (Test Repository): `save-automation-groups` groups straight from the cache's `folderPath`, ignoring `possibleId`.

**ID detection, in priority order, per test block:** (1) an ID on the test's own declaration line or the few lines directly above it - whichever is closest to that line wins if both an ID and an ancestor's ID both fall in that window; (2) failing that, the ID carried by the nearest enclosing `describe`/`context` block (including `describe.serial`/`.only`/`.skip`/`.parallel` variants), tracked by brace-depth scope so it's inherited correctly no matter how many lines below the block-opening a given `test()`/`it()` sits, and no matter how deeply blocks are nested. A test with no ID found either way simply has `possibleId: null` - never treated as an error, just falls back to name-only matching downstream.
