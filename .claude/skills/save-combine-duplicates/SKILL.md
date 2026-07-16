---
name: save-combine-duplicates
description: "Persists the cross-agent (manual + automation) duplicate analysis to combine-duplicates.toon: validates each proposed set's structure (2-3 tests, stepDiff <= 2, suggestedName present). Runs only after both manual-duplicates.toon and automation-duplicates.toon exist (the one synchronization point between the two otherwise-parallel agents). Does NOT check whether a test id is already claimed elsewhere - that cross-agent exclusivity check runs afterward, once, in validate-cross-agent-duplicates."
---

# Save Combine Duplicates

**This skill exists only to be used by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as one step of their documented workflow - never called ad hoc by a general-purpose assistant piecing together its own duplicate-detection process.** If you are not currently running as one of those two agents, stop and delegate this request to them instead of using this skill directly - they carry critical safety rules (asking before cloning a repo, running in parallel, cross-checking for conflicts) that do not exist anywhere else, and skipping them by using skills piecemeal has previously caused this tool to silently produce wrong or misleading results.

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/save-combine-duplicates/scripts/save_combine_duplicates.py" --input "$env:TEMP/tco_combine_payload.json" --cleanup-input
```

## How to build the input

1. Read `manual-groups.toon`'s and `automation-groups.toon`'s group names and full membership (not just the subset already flagged as within-side duplicates in `manual-duplicates.toon`/`automation-duplicates.toon`).
2. Judge semantic equivalence between manual and automation group names (not string equality - "ACD XYZ" vs "acd_xyz_flows" can match). Cap at **one match per group, both directions**.
3. **Before fetching anything**, narrow each matched group pair to its "free" tests: skip any manual test whose key is already in `manual-duplicates.toon` and any automation test whose id is already in `automation-duplicates.toon` - both already have an outcome, so there's nothing to gain from considering them and no reason to spend a fetch on them. The tests left over on both sides are the real candidates: a test that wasn't a duplicate on its own side can still turn out to be the same as a test on the other side - that's the entire reason combine exists.
4. For each matched pair's free tests only, compare step content across the two sides. Whichever agent is performing this comparison reuses its own side's content for free (still cached from its own earlier steps) but must fetch the other side's content fresh, targeted to just the free tests in that matched group - manual and automation run as separate, memory-isolated agents, so neither has the other's cached content. Same stepDiff <= 2, max 3 tests/set rule as within-group duplicates, except sets can mix manual and automation tests.

Input: `{ combineGroups: [{name, sets: [{stepDiff, criteria, mergeRationale, suggestedName, tests:[{source:"manual"|"automation", id, label, stepCount, origin}]}]}] }`

The script validates structure only: a set outside the 2-3 test range, a `stepDiff` over 2, or a missing `suggestedName` is rejected (see `invalidSets` in the response). It does not check for a test id already claimed elsewhere - step 3 above is what keeps an already-claimed test out of the payload in the first place, and `validate-cross-agent-duplicates` is the authoritative backstop afterward for anything that slips through anyway (e.g. two different matched group pairs both grabbing the same free test). Returns `mergeableSets`, `invalidSets`.

`suggestedName` is required on every set, no exceptions. See the `duplicate-detection-guidelines` skill for the full rule - for a combine set specifically, the name is really the *automation test's new name* once extended (read that test's actual naming style and extend it, don't switch to a differently-styled sentence), and `mergeRationale` must explain concretely how the existing automated script would be extended to also cover the manual test's steps.
