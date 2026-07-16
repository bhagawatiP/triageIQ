---
name: xray-get-testplan-tests
description: Fetches all tests (and test execution summary) from an Xray Cloud Test Plan via GraphQL. Use when listing tests in a plan, verifying test additions, or checking plan contents before regeneration.
---

# xray-get-testplan-tests

## Purpose
Fetches all tests (and test execution summary) from an Xray Cloud Test Plan via GraphQL.

## When to Use
- When you need to list tests inside a test plan
- When verifying tests were added to a plan
- Before regenerating test plan contents

## Script
`${CLAUDE_SKILL_DIR}/scripts/xray_get_testplan_tests.py`

## Usage
```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/xray_get_testplan_tests.py" <TEST-PLAN-KEY> [--limit N]
```

## Arguments
| Arg | Required | Description |
|-----|----------|-------------|
| `<TEST-PLAN-KEY>` | Yes | Jira issue key (e.g. `CXREC-107937`) |
| `--limit N` | No | Max tests to return (default 50, max 100) |

## Environment Variables
| Variable | Required | Description |
|----------|----------|-------------|
| `XRAY_CLIENT_ID` | Yes | Xray Cloud API client ID |
| `XRAY_CLIENT_SECRET` | Yes | Xray Cloud API client secret |

## Output (JSON stdout)
```json
{
  "success": true,
  "testPlanKey": "CXREC-107937",
  "testPlanIssueId": "123456",
  "testsTotal": 5,
  "tests": [
    { "issueId": "654321", "testType": "Manual" }
  ],
  "testExecutionsTotal": 1,
  "testExecutions": ["789012"]
}
```

## Error Output
```json
{ "success": false, "testPlanKey": "CXREC-999999", "error": "Test plan not found" }
```

## Integration
- Uses shared `skills/atlassian-api-clients/scripts/xray_client.py` for authentication and GraphQL
- Works with `xray-add-tests-to-plan` to verify test additions
