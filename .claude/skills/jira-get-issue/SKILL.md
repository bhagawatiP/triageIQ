---
name: jira-get-issue
description: Fetches Jira issue content (Epic, Story, or any type) by key. Returns summary, description, issue type, status, acceptance criteria, labels, and priority. No MCP server required.
---

# Jira Get Issue

## When to Use This Skill

Use this skill to fetch the content of any Jira issue (Epic, Story, Bug, etc.) by its key. This is the standalone equivalent of the `jira_get_issue` MCP tool from ms-mcr-auto-tp-gen.

**Trigger conditions:**
- User asks to get details of a Jira issue
- Need to fetch requirements from an Epic or Story for test generation
- Need to check issue status, description, or acceptance criteria
- MCP `jira_get_issue` tool is unavailable

---

## How to Execute

**Command:**
```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/get_jira_issue.py" <ISSUE-KEY>
```

**Example:**
```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/get_jira_issue.py" CXREC-105393
```

---

## Output Format

**Success:**
```json
{
  "success": true,
  "key": "CXREC-105393",
  "summary": "ESFU Redis Data Format V2 Migration",
  "description": "Migrate Redis data format from V1 to V2...",
  "issueType": "Epic",
  "status": "In Progress",
  "acceptanceCriteria": "All data migrated without loss...",
  "labels": ["backend", "redis"],
  "priority": "High"
}
```

**Failure:**
```json
{
  "success": false,
  "key": "CXREC-999999",
  "error": "HTTP 404: Issue Does Not Exist"
}
```

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
