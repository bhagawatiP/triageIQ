---
name: xray-create-test-plan
description: Creates a new Xray Test Plan issue in Jira via the Xray Cloud GraphQL API. Optionally links tests and associates an Epic. No MCP server required.
---

# Xray Create Test Plan

## When to Use This Skill

Use this skill to create a new test plan in Jira/Xray. Mirrors the `test_plan_create` MCP tool.

**Trigger conditions:**
- Creating a test plan as part of the `test_plan_create_with_tests` workflow
- User asks to create an empty test plan for manual test linking
- MCP `create_test_plan` or `test_plan_create` tools are unavailable

---

## How to Execute

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/xray_create_test_plan.py" --project <KEY> --summary "<text>" [--description "<text>"] [--testIssueIds "id1,id2"] [--epicKey JIRA-KEY]
```

**Examples:**
```bash
# Create empty test plan
python "${CLAUDE_PLUGIN_ROOT}/scripts/xray_create_test_plan.py" --project CXQA --summary "Sprint 42 Regression"

# Create with tests and epic link
python "${CLAUDE_PLUGIN_ROOT}/scripts/xray_create_test_plan.py" --project CXQA --summary "Login Tests" --testIssueIds "12345,54321" --epicKey CXQA-100

# Create with priority, fix version, and team
python "${CLAUDE_PLUGIN_ROOT}/scripts/xray_create_test_plan.py" --project CXQA --summary "Sprint 42 Regression" --priority "P1" --fix-version "26.2" --team "CAA" --epicKey CXQA-100
```

---

## What It Does

1. Builds a Jira fields payload with project key, summary, and optional description
2. Sends a `createTestPlan` GraphQL mutation to Xray Cloud
3. Add label `mcp-auto-generated` to the created test plan issue
4. Optionally links the new plan to a Jira Epic via the REST API
5. Returns JSON with the new plan's key, internal ID, and any warnings

---

## Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `--project` | Yes | Jira project key (e.g. `CXQA`) |
| `--summary` | Yes | Test plan summary/title |
| `--description` | No | Test plan description |
| `--testIssueIds` | No | Comma-separated internal Xray test issue IDs to link |
| `--epicKey` | No | Jira Epic key to link the plan to (uses REST API) |
| `--priority` | No | Jira priority name (e.g. `P1`, `P2`, `P3`) |
| `--fix-version` | No | Fix version name (e.g. `26.2`) |
| `--team` | No | Team Name value for `customfield_10098` (e.g. `CAA`, `Mavericks`) |

---

## Output Format

```json
{
  "success": true,
  "issueId": "123456",
  "key": "CXQA-60001",
  "summary": "Sprint 42 Regression",
  "testsLinked": 2,
  "warnings": [],
  "epicLinked": "CXQA-100"
}
```

---

## Required Environment Variables

| Variable | Purpose |
|----------|---------|
| `XRAY_CLIENT_ID` | Xray Cloud API client ID |
| `XRAY_CLIENT_SECRET` | Xray Cloud API client secret |
| `CONFLUENCE_USERNAME` | Jira username (only for `--epicKey`) |
| `CONFLUENCE_TOKEN` | Jira API token (only for `--epicKey`) |

---

## Requirements

- **Python 3** installed with `requests` library (`pip install requests`)
- Shared `skills/atlassian-api-clients/scripts/xray_client.py` module
