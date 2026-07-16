---
name: jira-approve-test
description: Approves Jira test issues by executing two-step workflow transitions (Draft → Under Review → Approved) via a standalone Python script. No MCP server required.
---

# Jira Test Approval

## When to Use This Skill

Use this skill when you need to approve a Jira test issue by transitioning it to "Approved" status. This is a standalone script that calls the JIRA REST API directly — it does **not** require the ms-mcr-auto-tp-gen MCP server to be running.

**Trigger conditions:**
- User asks to approve a test (e.g., "approve test CXREC-107916")
- User asks to approve all tests in a test plan (combine with `xray_get_testplan_tests`)
- After creating tests via `jira_add_test`, user wants them approved
- MCP `jira_approve_test` tool is unavailable and a fallback is needed

---

## How to Execute

**Command:**
```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/approve_jira_test.py" <TEST-KEY>
```

**Example:**
```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/approve_jira_test.py" CXREC-107916
```

**For multiple tests, run sequentially:**
```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/approve_jira_test.py" CXREC-107916
python "${CLAUDE_PLUGIN_ROOT}/scripts/approve_jira_test.py" CXREC-107917
python "${CLAUDE_PLUGIN_ROOT}/scripts/approve_jira_test.py" CXREC-107918
```

---

## What It Does

The script executes a **two-step JIRA workflow transition**:

1. **Step 1**: Fetches available transitions → finds and executes "Under Review" transition
2. **Step 2**: Fetches available transitions again → finds and executes "Approved" transition

This mirrors the `jira_approve_test` MCP tool from the ms-mcr-auto-tp-gen Java server.

---

## Output Format

**Success:**
```json
{
  "success": true,
  "testKey": "CXREC-107916",
  "newStatus": "Approved",
  "message": "Test CXREC-107916 has been successfully approved"
}
```

**Failure:**
```json
{
  "success": false,
  "testKey": "CXREC-107916",
  "message": "Failed to approve test CXREC-107916",
  "error": "No \"Approved\" transition found for CXREC-107916. Available transitions: [...]"
}
```

---

## Decision Logic

```
IF user requests test approval AND MCP jira_approve_test tool is available
  → Prefer using the MCP tool directly
ELSE (MCP tool unavailable OR standalone execution preferred)
  → Execute this skill script
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

### Authentication Issues
**Problem**: Script fails with "CONFLUENCE_USERNAME and CONFLUENCE_TOKEN environment variables are required"  
**Solution**: 
- Verify environment variables are set in your terminal session
- Windows PowerShell: `$env:CONFLUENCE_USERNAME = "you@company.com"`
- Linux/macOS: `export CONFLUENCE_USERNAME="you@company.com"`

### No Transition Found
**Problem**: Script fails with "No Under Review transition found" or "No Approved transition found"  
**Solution**:
- The test may already be in the target state (check current status in JIRA)
- The test's workflow may differ from the expected Draft → Under Review → Approved flow
- Verify you have permission to transition the issue

### Timeout Errors
**Problem**: Script fails with "Request timed out after 60000ms"  
**Solution**:
- Check network connectivity to your JIRA instance
- Verify the JIRA instance URL is correct (`JIRA_BASE_URL`)

---

## Requirements

- **Python 3** installed with `requests` library (`pip install requests`)
