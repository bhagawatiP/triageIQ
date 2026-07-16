---
name: list-removed-tests
description: "Collects just the keys of every Removed-status test in a source, scoped via the same verified JQL pattern as read-manual-candidates, and persists them to removed-tests.toon - key-only, no content, even if there are hundreds of them. This is the only Removed-ID table in the whole design; nothing automation-related uses this treatment. Read-only. Runs alongside read-manual-candidates in the manual agent workflow."
---

# List Removed Tests

**This skill exists only to be used by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as one step of their documented workflow - never called ad hoc by a general-purpose assistant piecing together its own duplicate-detection process.** If you are not currently running as one of those two agents, stop and delegate this request to them instead of using this skill directly - they carry critical safety rules (asking before cloning a repo, running in parallel, cross-checking for conflicts) that do not exist anywhere else, and skipping them by using skills piecemeal has previously caused this tool to silently produce wrong or misleading results.

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/list-removed-tests/scripts/list_removed_tests.py" --source <plan|testset|execution|repository> [--key <KEY>] [--project <PROJECT>]
```

Writes `test-cases-optimizer-work/manual-agent-work/removed-tests.toon` directly (no separate save step - the data is a flat key list with no agent judgment involved). Returns `removedCount`.
