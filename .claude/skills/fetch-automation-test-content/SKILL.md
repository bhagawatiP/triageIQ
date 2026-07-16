---
name: fetch-automation-test-content
description: "Reads the actual body content of specific automation test blocks from the still-cloned repo (brace-balancing for curly-brace languages, indentation for Python, keyword-bounded for Cucumber .feature files) for content-based duplicate judgment. Called once per test and cached by the caller - never re-read for the same test during duplicate detection. Read-only, local disk only, no network access."
---

# Fetch Automation Test Content

**This skill exists only to be used by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as one step of their documented workflow - never called ad hoc by a general-purpose assistant piecing together its own duplicate-detection process.** If you are not currently running as one of those two agents, stop and delegate this request to them instead of using this skill directly - they carry critical safety rules (asking before cloning a repo, running in parallel, cross-checking for conflicts) that do not exist anywhere else, and skipping them by using skills piecemeal has previously caused this tool to silently produce wrong or misleading results.

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/fetch-automation-test-content/scripts/fetch_automation_test_content.py" --repo-path <path> --refs-file <path to JSON array of {filePath, lineNo}>
```

Write the refs for one group's tests to a small JSON file first (from `automation-groups.toon` or the match/scan output), pass its path via `--refs-file`. Returns each ref's extracted body `content`.

Fetch only for the tests inside the group currently being analyzed for duplicates - not the whole repo at once.
