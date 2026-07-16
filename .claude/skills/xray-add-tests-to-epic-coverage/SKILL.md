---
name: xray-add-tests-to-epic-coverage
description: Add one or more test issues to a Jira Epic's Test Coverage section via the Jira REST API issue-link approach (mirrors epicTestcoverage.py). Uses "Test" link type with test as inwardIssue and epic as outwardIssue. No MCP server required.
---

# Xray Add Tests to Epic Coverage

## When to Use This Skill

Use this skill to associate test cases with a Jira Epic so they appear in the Epic's **Test Coverage** panel — not under child issues, not under linked work items, and not inside a test plan.

**Trigger conditions:**
- User wants to "add test(s) to epic test coverage"
- Tests have already been created and need to be visible in an Epic's Test Coverage section
- User says "link test to epic" or "associate test with epic" (for coverage tracking)
- MCP `epic_add_test_coverage` or `epic_add_multiple_test_coverage` tools are unavailable

> **Do NOT use this skill** if the goal is to add tests to a test plan — use `xray-add-tests-to-plan` for that instead.

---

## How to Execute

```bash
# Single test
python "${CLAUDE_PLUGIN_ROOT}/scripts/xray_add_tests_to_epic_coverage.py" --epic <EPIC-KEY> --tests <TEST-KEY>

# Multiple tests (space-separated)
python "${CLAUDE_PLUGIN_ROOT}/scripts/xray_add_tests_to_epic_coverage.py" --epic <EPIC-KEY> --tests <TEST-KEY-1> <TEST-KEY-2> <TEST-KEY-3>
```

**Examples:**
```bash
# Add one test to epic
python "${CLAUDE_PLUGIN_ROOT}/scripts/xray_add_tests_to_epic_coverage.py" --epic CXREC-12345 --tests CXREC-50001

# Add multiple tests to epic
python "${CLAUDE_PLUGIN_ROOT}/scripts/xray_add_tests_to_epic_coverage.py" --epic CXREC-12345 --tests CXREC-50001 CXREC-50002 CXREC-50003
```

---

## What It Does

1. Creates a Jira issue link using the `"Test"` link type with **test as `inwardIssue`** and **epic as `outwardIssue`** — this is the exact direction used by the confirmed-working `epicTestcoverage.py` implementation
2. If that fails, retries with `"Test"` outward, then `"Relates"`, `"Blocks"`, `"Covers"`
3. As a final fallback, sets the Epic Link custom field on the test issue (`customfield_10014` and alternatives)
4. Returns JSON with method used and success/failure per test

---

## Link Type Attempt Order

| # | Link Type | Direction | Notes |
|---|-----------|-----------|-------|
| 1 | `Test` | inward (is tested by) | Preferred — populates Xray Test Coverage |
| 2 | `Test` | outward (tests) | Fallback direction |
| 3 | `Relates` | outward | Generic fallback |
| 4 | `Blocks` | outward | Generic fallback |
| 5 | `Covers` | outward | Generic fallback |
| 6 | Epic Link custom field | — | Last resort (`customfield_10014`, etc.) |

---

## Output Format

### Single test
```json
{
  "success": true,
  "epicKey": "CXREC-12345",
  "testKey": "CXREC-50001",
  "method": "Test (inward)",
  "message": "Linked test CXREC-50001 to epic CXREC-12345 using Test (inward)"
}
```

### Multiple tests
```json
{
  "success": true,
  "epicKey": "CXREC-12345",
  "totalTests": 3,
  "successfullyAdded": 3,
  "failed": 0,
  "addedTestKeys": ["CXREC-50001", "CXREC-50002", "CXREC-50003"],
  "results": [...]
}
```

### Failure
```json
{
  "success": false,
  "epicKey": "CXREC-12345",
  "testKey": "CXREC-50001",
  "error": "All linking methods failed."
}
```

---

## Required Environment Variables

| Variable | Purpose |
|----------|---------|
| `CONFLUENCE_USERNAME` | Atlassian username (email) |
| `CONFLUENCE_TOKEN` | Atlassian API token |

---

## Requirements

- **Python 3** installed with `requests` library (`pip install requests`)
- This skill uses a standalone script in this skill folder
- Skill folder: `.github/skills/xray-add-tests-to-epic-coverage/`
