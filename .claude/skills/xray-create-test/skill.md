---
name: xray-create-test
description: Creates a new Xray Test issue in Jira via the Xray Cloud GraphQL API. Accepts a plain-text description (including any formatted steps) via --description or piped stdin. No Xray structured test steps are created. No MCP server required.
---

# Xray Create Test

## When to Use This Skill

Use this skill to create new test issues in Jira/Xray. Mirrors the `jira_add_test` / `create_test` MCP tools.

**Trigger conditions:**
- User wants to create test cases from generated scenarios
- Part of `test_plan_create_with_tests` or `regenerate_test_plan_tests` workflow
- MCP `create_test` or `jira_add_test` tools are unavailable

---

## Important: Description-Only — No Xray Test Steps

This script creates the Jira Test issue with **only a description field**. It does NOT create Xray structured test steps. If you need test steps to appear on the issue, **include them as formatted text inside the description**.

The AI agent is responsible for composing the full description (including steps formatted as plain text). This matches the behaviour of `ms-mcr-auto-tp-gen`.

---

## How to Execute

### Option 1: Pipe description via stdin (RECOMMENDED for multi-line)

Use a PowerShell here-string to pipe the description. No temp files needed.

```powershell
@"
Verify the user can log in with valid credentials.

Test Steps:
1. Open login page
   Expected Result: Page loads successfully
2. Enter valid credentials (user/pass)
   Expected Result: User is logged in and redirected to dashboard
"@ | python "${CLAUDE_PLUGIN_ROOT}/scripts/xray_create_test.py" --project CXQA --summary "Verify login" --type Manual
```

### Option 2: Inline --description (for short text)

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/xray_create_test.py" --project CXQA --summary "Verify login" --description "Verify the user can log in."
```

### Option 3: Minimal (no description)

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/xray_create_test.py" --project CXQA --summary "Verify user login"
```

> **Agent workflow:** For multi-line descriptions with test steps, ALWAYS use the stdin pipe approach (Option 1) with a PowerShell here-string `@"..."@`. This avoids quoting issues and does not create any temp files.

---

## What It Does

1. Reads description from `--description` flag or stdin (piped text)
2. Builds a Jira fields payload with project key, summary, and description
3. Sends a `createTest` GraphQL mutation to Xray Cloud (**no Xray structured test steps**)
4. Adds the label `mcp-auto-generated` to the created test issue
5. Returns JSON with the new issue's key, internal ID, and any warnings

---

## Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `--project` | Yes | Jira project key (e.g. `CXQA`) |
| `--summary` | Yes | Test issue summary/title |
| `--type` | No | Test type: `Manual` (default), `Cucumber`, `Generic`, `Automated[Generic]` |
| `--description` | No | Short inline description text |
| `--priority` | No | Jira priority name (e.g. `P1`, `P2`, `P3`, `P4`). Sets the `priority` field on the issue. |
| `--fix-version` | No | Fix version name (e.g. `25.2`). Sets the `fixVersions` field on the issue. |
| `--team` | No | Team name (e.g. `CAA`, `DPA`, `E2E`). Sets the `customfield_10098` (Team Name) field on the issue. |
| stdin (piped) | No | Multi-line description piped via stdin (takes effect when `--description` is not given) |

---

## Output Format

```json
{
  "success": true,
  "issueId": "789012",
  "key": "CXQA-50001",
  "summary": "Verify user login",
  "testType": "Manual",
  "warnings": []
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