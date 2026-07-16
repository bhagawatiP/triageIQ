---
name: save-manual-duplicates
description: "Persists the manual agent's within-group duplicate (merge candidate) analysis to manual-duplicates.toon, reading manual-groups.toon for totals. Validates each set has 2-3 tests, stepDiff <= 2, and a non-empty suggestedName. Step 5 of the manual agent workflow."
---

# Save Manual Duplicates

**This skill exists only to be used by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as one step of their documented workflow - never called ad hoc by a general-purpose assistant piecing together its own duplicate-detection process.** If you are not currently running as one of those two agents, stop and delegate this request to them instead of using this skill directly - they carry critical safety rules (asking before cloning a repo, running in parallel, cross-checking for conflicts) that do not exist anywhere else, and skipping them by using skills piecemeal has previously caused this tool to silently produce wrong or misleading results.

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/save-manual-duplicates/scripts/save_manual_duplicates.py" --input "$env:TEMP/tco_manual_payload.json" --cleanup-input
```

Input: `{ duplicateGroups: [{name, sets: [{stepDiff, criteria, mergeRationale, suggestedName, tests:[{key,summary,stepCount,stepSource}]}]}], noSteps: [{key,summary}] }`

Requires `manual-groups.toon` to already exist (run `save-manual-groups` first). Writes `test-cases-optimizer-work/manual-agent-work/manual-duplicates.toon`. Returns `mergeableSets`, `groupsWithDuplicates`, `noStepsCount`, and `invalidSets` (any set outside 2-3 tests, stepDiff > 2, or missing `suggestedName` - fix and re-run).

`suggestedName` is required on every set, no exceptions. See the `duplicate-detection-guidelines` skill for the full naming rules (professional, ticket-free, grounded in the set's actual content) and the `criteria`/`mergeRationale` grounding rule - apply those before calling this script, not after.
