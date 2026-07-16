---
name: read-all-source-tests
description: "Reads every test (key + summary) from a Test Plan/Set/Execution with no filtering at all - no Removed exclusion, no testType exclusion. Used by the automation agent (Mode A, bounded source) to get the full Jira ID list to search for in the automation repository, and by the manual agent's fallback path when no reliable JQL filter exists for a project. Never used for a Test Repository ask (Mode B scans the automation repo directly instead). Read-only."
---

# Read All Source Tests

**This skill exists only to be used by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as one step of their documented workflow - never called ad hoc by a general-purpose assistant piecing together its own duplicate-detection process.** If you are not currently running as one of those two agents, stop and delegate this request to them instead of using this skill directly - they carry critical safety rules (asking before cloning a repo, running in parallel, cross-checking for conflicts) that do not exist anywhere else, and skipping them by using skills piecemeal has previously caused this tool to silently produce wrong or misleading results.

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/read-all-source-tests/scripts/read_all_source_tests.py" --source <plan|testset|execution> --key <KEY>
```

Returns every test in the container, unfiltered. This is the automation agent's starting ID list for Mode A (bounded sources only - never called for a Test Repository ask).
