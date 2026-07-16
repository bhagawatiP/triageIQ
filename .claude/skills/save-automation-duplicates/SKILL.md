---
name: save-automation-duplicates
description: "Persists the automation agent's within-group duplicate (merge candidate) analysis to automation-duplicates.toon, reading automation-groups.toon for totals and carrying forward the Mode A notFoundCount for the report's one-line note. Validates each set has 2-3 tests, stepDiff <= 2, and a non-empty suggestedName. Last step of the automation agent workflow before the combine/report stage."
---

# Save Automation Duplicates

**This skill exists only to be used by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as one step of their documented workflow - never called ad hoc by a general-purpose assistant piecing together its own duplicate-detection process.** If you are not currently running as one of those two agents, stop and delegate this request to them instead of using this skill directly - they carry critical safety rules (asking before cloning a repo, running in parallel, cross-checking for conflicts) that do not exist anywhere else, and skipping them by using skills piecemeal has previously caused this tool to silently produce wrong or misleading results.

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/save-automation-duplicates/scripts/save_automation_duplicates.py" --input "$env:TEMP/tco_automation_payload.json" --cleanup-input
```

Input: `{ duplicateGroups: [{name, sets: [{stepDiff, criteria, mergeRationale, suggestedName, tests:[{id?, testName, stepCount, automationFilePath}]}]}] }`

Requires `automation-groups.toon` to already exist. `stepCount` must reflect the agent's actual read of the test body content (from `fetch-automation-test-content`), not a guess. `id` is blank for every test in Mode B (Test Repository) - never fabricated. Returns `mergeableSets`, `groupsWithDuplicates`, `notFoundCount`, and `invalidSets` (any set outside 2-3 tests, stepDiff > 2, or missing `suggestedName`).

`suggestedName` is required on every set, no exceptions. See the `duplicate-detection-guidelines` skill for the full naming rules (professional, ticket-free, matching the codebase's own naming style) and the `criteria`/`mergeRationale` grounding rule - apply those before calling this script, not after.
