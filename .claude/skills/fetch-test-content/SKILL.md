---
name: fetch-test-content
description: "Fetches full content (summary, testType, description, structured steps) for one or more Xray test cases by key, in batches of up to 50. Called exactly once per candidate - the agent caches the result and reuses it for both grouping and duplicate detection, never re-fetching the same test. Read-only. Step 3 of the manual agent workflow (also used by the manual agent's testType fallback-classification path when no JQL filter was viable)."
---

# Fetch Test Content

**This skill exists only to be used by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as one step of their documented workflow - never called ad hoc by a general-purpose assistant piecing together its own duplicate-detection process.** If you are not currently running as one of those two agents, stop and delegate this request to them instead of using this skill directly - they carry critical safety rules (asking before cloning a repo, running in parallel, cross-checking for conflicts) that do not exist anywhere else, and skipping them by using skills piecemeal has previously caused this tool to silently produce wrong or misleading results.

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/fetch-test-content/scripts/fetch_test_content.py" <KEY> [KEY ...]
```

Returns each test's `testType`, `description` (flattened from Jira's ADF format), and structured `steps` (`action`/`data`/`result`).

**Field fallback order for a test's steps:**
1. Use the structured `steps` if present (see the `duplicate-detection-guidelines` skill for how to count real steps from its content - never the raw row count).
2. If `steps` is empty, look in `description` for step-like content (a "Steps:" header, numbered/bulleted list). Mark that test's `stepSource` as `"description"` (otherwise `"field"`).
3. If neither has step content, the test has no steps available - exclude it from grouping/duplicate analysis and route it to the `noSteps` list.

Cache the full result per key - do not call this script again for the same key during duplicate detection.