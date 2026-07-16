---
name: save-manual-groups
description: "Persists the manual agent's functional grouping of manual, non-Removed candidate test cases to manual-groups.toon. Takes the group name -> test keys mapping decided by the agent from actual content (summary + steps + description), sorts groups and tests alphabetically. Step 3 of the manual agent workflow."
---

# Save Manual Groups

**This skill exists only to be used by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as one step of their documented workflow - never called ad hoc by a general-purpose assistant piecing together its own duplicate-detection process.** If you are not currently running as one of those two agents, stop and delegate this request to them instead of using this skill directly - they carry critical safety rules (asking before cloning a repo, running in parallel, cross-checking for conflicts) that do not exist anywhere else, and skipping them by using skills piecemeal has previously caused this tool to silently produce wrong or misleading results.

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/save-manual-groups/scripts/save_manual_groups.py" --input "$env:TEMP/tco_manual_payload.json" --cleanup-input
```

Input: `{ source, key, project, containerSummary, testTypeFilterUsed, tests: [{key,summary}], groups: [{name, keys:[...]}] }`

Writes `test-cases-optimizer-work/manual-agent-work/manual-groups.toon`, sorted alphabetically by group name and by test summary within each group. Returns `candidateTestsCount` and `totalGroups`.

No Removed or Automated tests should ever be in the input `tests` list - they were already excluded by `read-manual-candidates` (JQL-level) or captured separately by `list-removed-tests`.
