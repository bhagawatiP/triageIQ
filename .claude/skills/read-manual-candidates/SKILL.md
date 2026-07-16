---
name: read-manual-candidates
description: "Reads the manual agent's candidate tests directly via a JQL-filtered Xray query (issueFunction in testPlanTests/testSetTests/testExecutionTests, or project=<KEY> for a Test Repository), always excluding Jira status Removed and, when probe-source-convention found a reliable signal on testType, labels, or status, also filtering by that field at the query level - so Removed and Automated tests are never fetched at all. Read-only. Step 2 of the manual agent workflow."
---

# Read Manual Candidates

**This skill exists only to be used by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as one step of their documented workflow - never called ad hoc by a general-purpose assistant piecing together its own duplicate-detection process.** If you are not currently running as one of those two agents, stop and delegate this request to them instead of using this skill directly - they carry critical safety rules (asking before cloning a repo, running in parallel, cross-checking for conflicts) that do not exist anywhere else, and skipping them by using skills piecemeal has previously caused this tool to silently produce wrong or misleading results.

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/read-manual-candidates/scripts/read_manual_candidates.py" --source <plan|testset|execution|repository> [--key <KEY>] [--project <PROJECT>] [--filter-field testType|labels|status] [--filter-value "<value>"] [--filter-positive]
```

- `--filter-field` (default `testType`) + `--filter-value` + `--filter-positive`: allow-list (`field = "<value>"`) - use when probe-source-convention found the Manual value directly, on whichever field carries the signal for this project.
- `--filter-field` + `--filter-value` alone (no `--filter-positive`): exclude (`field != "<value>"`) - use when probe-source-convention found the automated value and cross-checked it as trustworthy.
- Neither `--filter-value` nor `--filter-field`: only the Removed exclusion applies (fallback mode) - the caller must then classify each candidate's `testType` client-side via `fetch-test-content`.

Returns `candidatesReturned` and the `tests` list (key + summary only - no content yet). The `jqlUsed` field is echoed back for transparency/debugging.
