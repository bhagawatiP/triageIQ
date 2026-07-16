---
name: validate-cross-agent-duplicates
description: "Final cross-check, run once after manual-duplicates.toon, automation-duplicates.toon, and combine-duplicates.toon all exist and before generate-combined-report. Finds any test id claimed in more than one of the three artifacts and resolves it in code, not agent judgment: automation-duplicates.toon always wins, combine-duplicates.toon is second priority, manual-duplicates.toon never wins a conflict. After removing a losing test, re-validates each affected set's size - drops a set that falls below 2 tests, keeps a set that still has 2 (even if it started at 3)."
---

# Validate Cross-Agent Duplicates

**This skill exists only to be used by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as one step of their documented workflow - never called ad hoc by a general-purpose assistant piecing together its own duplicate-detection process.** If you are not currently running as one of those two agents, stop and delegate this request to them instead of using this skill directly - they carry critical safety rules (asking before cloning a repo, running in parallel, cross-checking for conflicts) that do not exist anywhere else, and skipping them by using skills piecemeal has previously caused this tool to silently produce wrong or misleading results.

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/validate-cross-agent-duplicates/scripts/validate_cross_agent_duplicates.py"
```

No input payload - it reads `manual-duplicates.toon`, `automation-duplicates.toon`, and `combine-duplicates.toon` directly from `test-cases-optimizer-work/` and rewrites `manual-duplicates.toon` and `combine-duplicates.toon` in place with any conflicts resolved. `automation-duplicates.toon` is never modified - it never loses a conflict.

Run this **after** `save-combine-duplicates` and **before** `generate-combined-report`, whenever combine-ownership is established (see either agent's workflow) - it is the one point where all three artifacts exist together and can actually be cross-checked.

## Why a test can end up in more than one artifact

The manual and automation agents build their duplicate sets independently and in parallel, with no visibility into each other. The combine step then builds its own sets afterward by comparing the two sides. None of those three steps check the other two artifacts against each other while running - this script is that check, done once, at the end.

## Priority

1. **Automation wins.** A test claimed in `automation-duplicates.toon` is removed from `combine-duplicates.toon` and/or `manual-duplicates.toon` wherever else it appears.
2. **Combine is second.** A test claimed in `combine-duplicates.toon` (after rule 1 already ran) is removed from `manual-duplicates.toon` if it also appears there.
3. **Manual never wins.** It only ever loses tests to rules 1 and 2.

## Set-size revalidation after removal

- A 2-test set that loses a test is left with 1 - not a valid merge candidate, so the **whole set is dropped**.
- A 3-test set that loses a test is left with 2 - still valid (2 is the minimum), so it is **kept** as a 2-test set.
- A group left with zero sets afterward is dropped too.

Returns `changed`, `removed` (each entry: id, group, reason), `manualGroupsRemaining`, `combineGroupsRemaining`.
