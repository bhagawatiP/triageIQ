---
name: xray-add-tests-to-plan
description: Add existing tests to an Xray Test Plan via the Xray Cloud GraphQL API. Automatically resolves Jira keys to internal Xray IDs. No MCP server required.
---

# Xray Add Tests to Plan

## When to Use This Skill

Use this skill to link existing test issues to a test plan in Xray. Mirrors the `xray_add_tests_to_plan` / `update_test_plan` MCP tools.

**Trigger conditions:**
- After creating tests with `jira-add-test`, link them to an existing test plan
- After `test_plan_create`, associate tests with the new plan
- MCP `xray_add_tests_to_plan` or `update_test_plan` tools are unavailable

---

## How to Execute

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/xray_add_tests_to_plan.py" <TEST-PLAN-KEY> <TEST-KEY-1> [TEST-KEY-2] ...
```

**Example:**
```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/xray_add_tests_to_plan.py" CXREC-107937 CXREC-107916 CXREC-107917
```

---

## What It Does

1. Resolves the test plan Jira key to an internal Xray issue ID
2. Resolves each test Jira key to internal Xray issue IDs (batched, 10 at a time)
3. Executes the `addTestsToTestPlan` GraphQL mutation
4. Returns JSON with added counts and any warnings

---

## Output Format

```json
{
  "success": true,
  "testPlanKey": "CXREC-107937",
  "addedCount": 2,
  "addedTestIds": ["12345", "54321"],
  "warning": null,
  "resolvedTests": [
    { "key": "CXREC-107916", "issueId": "12345" },
    { "key": "CXREC-107917", "issueId": "54321" }
  ]
}
```

---

## Required Environment Variables

| Variable | Purpose |
|----------|---------|
| `XRAY_CLIENT_ID` | Xray Cloud API client ID |
| `XRAY_CLIENT_SECRET` | Xray Cloud API client secret |

---

## Requirements

- **Python 3** installed with `requests` library (`pip install requests`)
- Shared `skills/atlassian-api-clients/scripts/xray_client.py` module
