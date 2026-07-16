---
name: jira-remove-test
description: Removes Jira test issues by executing workflow transitions (handles Approved → Open → Removed or direct → Removed) via a standalone Python script. No MCP server required.
---

# Jira Test Removal

## When to Use This Skill

Use this skill when you need to remove a Jira test issue by transitioning it to "Removed" status. This is a standalone script that calls the JIRA REST API directly — it does **not** require the ms-mcr-auto-tp-gen MCP server to be running.

**Trigger conditions:**
- User asks to remove a test (e.g., "remove test CXREC-107916")
- User asks to remove tests from a test plan
- MCP `jira_remove_test` tool is unavailable and a fallback is needed

---

## How to Execute

**Command:**
```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/remove_jira_test.py" <TEST-KEY>
```

**Example:**
```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/remove_jira_test.py" CXREC-107916
```

---

## What It Does

The script handles multi-step JIRA workflow transitions:

1. **Gets current status** of the test issue
2. **If status is "Approved"**: Transitions to "Open" first (required intermediate step)
3. **Transitions to "Removed"**: Executes the final removal transition

This mirrors the `jira_remove_test` MCP tool from the ms-mcr-auto-tp-gen Java server.

---

## Output Format

**Success:**
```json
{
  "success": true,
  "testKey": "CXREC-107916",
  "newStatus": "Removed",
  "message": "Test CXREC-107916 has been successfully removed"
}
```

**Failure:**
```json
{
  "success": false,
  "testKey": "CXREC-107916",
  "message": "Failed to remove test CXREC-107916",
  "error": "No \"Removed\" transition found for CXREC-107916. Available: [...]"
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

## Troubleshooting

### Test Already Removed
**Problem**: Script fails with "No Removed transition found"
**Solution**: The test may already be in Removed status. Check the current status in JIRA.

### Approved Test Cannot Be Removed Directly
**Problem**: Test is in "Approved" status and removal fails
**Solution**: The script handles this automatically — it transitions Approved → Open → Removed. If the Open transition fails, the workflow may not support this path.

---

## Requirements

- **Python 3** installed with `requests` library (`pip install requests`)
