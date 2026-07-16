---
name: atlassian-user-info
description: Gets the current authenticated Jira user's account ID and display name via the JIRA REST API. Used to populate custom fields like "Automated By". No MCP server required.
---

# Atlassian User Info

## When to Use This Skill

Use this skill to get the current authenticated user's account ID and display name. The `accountId` is needed to populate custom fields such as "Automated By" (`customfield_10075`) when updating Jira issues after test generation.

**Trigger conditions:**
- Need to set the "Automated By" field on a test issue
- Need to get the current user's `accountId` for assignee operations
- MCP `atlassianUserInfo` tool is unavailable

---

## How to Execute

**Command:**
```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/get_atlassian_user.py"
```

No arguments required — uses the credentials from environment variables.

---

## Output Format

**Success:**
```json
{
  "success": true,
  "accountId": "5f1234567890abcdef123456",
  "displayName": "John Doe",
  "emailAddress": "john.doe@company.com",
  "active": true,
  "accountType": "atlassian"
}
```

**Failure:**
```json
{
  "success": false,
  "accountId": null,
  "displayName": null,
  "emailAddress": null,
  "active": null,
  "accountType": null,
  "error": "HTTP 401: Unauthorized"
}
```

---

## Common Use Case

After generating a test, update the "Automated By" field:
1. Run this script to get `accountId`
2. Use the `accountId` to update the test issue via `editJiraIssue` or the jira-management skill

---

## Required Environment Variables

| Variable | Purpose |
|----------|---------|
| `CONFLUENCE_USERNAME` | Atlassian username (email) for Basic Auth |
| `CONFLUENCE_TOKEN` | Atlassian API token for Basic Auth |

**Optional:**
| Variable | Purpose | Default |
|----------|---------|---------|
| `JIRA_BASE_URL` | JIRA instance URL | `https://nice-ce-cxone-prod.atlassian.net` |

---

## Requirements

- **Python 3** installed with `requests` library (`pip install requests`)
