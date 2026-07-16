---
name: confluence-get-page
description: Fetches raw Confluence page content by page ID or URL. Returns title and plain text content. Combines confluence_get_page and confluence_get_page_by_url tools. No MCP server required.
---

# Confluence Get Page

## When to Use This Skill

Use this skill to fetch Confluence page content for requirements gathering or context aggregation. Combines the `confluence_get_page` (by ID) and `confluence_get_page_by_url` (by URL) MCP tools from ms-mcr-auto-tp-gen into a single script.

**Trigger conditions:**
- User provides a Confluence page ID or URL for test generation context
- Need to fetch requirements documentation from Confluence
- MCP `confluence_get_page` or `confluence_get_page_by_url` tools are unavailable

---

## How to Execute

**By Page ID:**
```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/get_confluence_page.py" --id <PAGE-ID>
```

**By URL:**
```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/get_confluence_page.py" --url <CONFLUENCE-URL>
```

**Examples:**
```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/get_confluence_page.py" --id 2818375685
python "${CLAUDE_PLUGIN_ROOT}/scripts/get_confluence_page.py" --url "https://nice-ce-cxone-prod.atlassian.net/wiki/spaces/TEAM/pages/2818375685/My+Page"
```

---

## What It Does

1. Accepts either a page ID (`--id`) or full Confluence URL (`--url`)
2. If URL provided, extracts the page ID from the `/pages/{id}/` pattern
3. Fetches the page via Confluence REST API (`/wiki/rest/api/content/{pageId}?expand=body.storage`)
4. Strips HTML tags and returns plain text content

---

## Output Format

**Success:**
```json
{
  "success": true,
  "pageId": "2818375685",
  "title": "Redis V2 Migration Design",
  "content": "Plain text content of the page..."
}
```

**Failure:**
```json
{
  "success": false,
  "pageId": "2818375685",
  "title": "",
  "content": "",
  "error": "HTTP 404: Page not found"
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
| `JIRA_BASE_URL` | Atlassian instance URL | `https://nice-ce-cxone-prod.atlassian.net` |

---

## URL Format

The script expects Confluence URLs with the `/pages/{pageId}/` pattern:
```
https://your-instance.atlassian.net/wiki/spaces/TEAM/pages/2818375685/My+Page+Title
```

---

## Requirements

- **Python 3** installed with `requests` library (`pip install requests`)
