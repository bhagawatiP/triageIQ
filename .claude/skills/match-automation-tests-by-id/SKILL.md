---
name: match-automation-tests-by-id
description: "Mode A only (Test Set/Plan/Execution): one bulk, local set-intersection between the Jira ID list and the scan-automation-repo cache's detected IDs - no per-ID lookups, no Method Name fallback. Matched tests keep their Jira ID + test name; unmatched tests are reported as a count only, never listed by ID. Read-only, no network access."
---

# Match Automation Tests By ID

**This skill exists only to be used by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as one step of their documented workflow - never called ad hoc by a general-purpose assistant piecing together its own duplicate-detection process.** If you are not currently running as one of those two agents, stop and delegate this request to them instead of using this skill directly - they carry critical safety rules (asking before cloning a repo, running in parallel, cross-checking for conflicts) that do not exist anywhere else, and skipping them by using skills piecemeal has previously caused this tool to silently produce wrong or misleading results.

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/match-automation-tests-by-id/scripts/match_automation_tests_by_id.py" --scan-cache <path> --ids-file <path to JSON array of Jira IDs>
```

Write the Jira ID list (from `read-all-source-tests`) to a small JSON file first, pass its path via `--ids-file`.

Returns `matchedCount`, `notFoundCount` (count only - **never** the list of missing IDs), and `matched` (id, testName, folderPath, filePath, lineNo per hit).

If `notFoundCount > 0`, the report must show exactly one line under the Automation Duplicates section: `"XX test cases from this set were not found in the automation repository."` - no ID list, no separate table.
