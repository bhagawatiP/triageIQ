---
name: jira-bulk-remove-tests
description: Removes multiple Jira test issues in bulk with batching support (batch size 10). Handles Approved → Open → Removed workflow automatically. No MCP server required.
---

# Jira Bulk Test Removal

## When to Use This Skill

Use this skill when you need to remove **multiple** Jira test issues at once.

**Trigger conditions:**
- User asks to remove all tests in a test plan
- User provides multiple test keys to remove
- MCP `jira_bulk_remove_tests` tool is unavailable

---

## How to Execute

**Command:**
```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/bulk_remove_jira_tests.py" <KEY1> <KEY2> <KEY3> ...
```

**Example:**
```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/bulk_remove_jira_tests.py" CXREC-107916 CXREC-107917 CXREC-107918
```

---

## What It Does

1. Processes tests in **batches of 10**
2. For each test:
   - Checks current status
   - If "Approved": transitions Approved → Open → Removed
   - Otherwise: transitions directly to Removed
3. Returns a summary JSON with successful, failed, and timed-out counts

---

## Output Format

```json
{
  "operation": "remove",
  "total": 3,
  "successful": 2,
  "failed": 1,
  "timedOut": 0,
  "successfulTests": ["CXREC-107916", "CXREC-107917"],
  "failedTests": ["CXREC-107918"],
  "timedOutTests": []
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
