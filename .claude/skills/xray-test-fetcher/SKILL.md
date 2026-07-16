---
name: xray-test-fetcher
description: Fetch test cases from Xray issues using the Xray Cloud GraphQL API v2 (with Jira REST used for Epic resolution when configured). Reads XRAY_CLIENT_ID and XRAY_CLIENT_SECRET from system environment variables. Use when you need the test case list that the standard Jira MCP cannot access.
allowed-tools:
  - Bash(python:*)
  - Bash(python3:*)
---

# Xray Test Fetcher

Fetches test cases from Xray issues using the Xray Cloud **GraphQL API v2**. Supports Test Execution, Test Plan, Test Set, single Test, and Epic (Epic resolution uses Jira REST API when Jira env vars are provided). The standard Jira API cannot directly access all Xray test associations — this skill queries Xray directly.

## Prerequisites

`XRAY_CLIENT_ID` and `XRAY_CLIENT_SECRET` must be set as **system environment variables** (not in .env):

```powershell
# Windows (run once, then restart terminal)
setx XRAY_CLIENT_ID your_client_id
setx XRAY_CLIENT_SECRET your_client_secret
```

```bash
# Linux / Mac
export XRAY_CLIENT_ID=your_client_id
export XRAY_CLIENT_SECRET=your_client_secret
```

Get these from the Xray Cloud console → API Keys section.

## Usage

When the user asks to fetch tests for a Jira issue key, run the bundled script via `${CLAUDE_SKILL_DIR}` so it works regardless of the current working directory or whether the plugin is loaded from a repo checkout or installed cache:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/fetch_xray_tests.py <JIRA_ISSUE_KEY>
```

### Examples

```bash
# Fetch tests from a Test Execution
python ${CLAUDE_SKILL_DIR}/scripts/fetch_xray_tests.py CXQA-562392

# Fetch tests from a Test Set
python ${CLAUDE_SKILL_DIR}/scripts/fetch_xray_tests.py CXQA-7585

# Fetch tests from a Test Plan
python ${CLAUDE_SKILL_DIR}/scripts/fetch_xray_tests.py CXQA-7629
```

## What it does

1. Reads `XRAY_CLIENT_ID` and `XRAY_CLIENT_SECRET` from **system environment variables** (not from `.env`).
2. Validates the Jira issue key format (e.g., `CXQA-9514`).
3. Authenticates with `https://xray.cloud.getxray.app/api/v2/authenticate` to get a Bearer token.
4. Uses Xray GraphQL queries to detect issue type and fetch linked tests (with pagination, 50 per page).
5. Enriches tests with step details when available.
6. If the issue is an Epic and `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` are set, resolves linked Test Executions/Plans/Sets via Jira REST, then fetches their tests from Xray.
7. Prints each test's key, summary, status, type, and steps (when present), then saves JSON to `xray_tests_<ISSUE_KEY_WITH_DASHES_REPLACED_BY_UNDERSCORES>.json` in the current working directory (for example, `CXQA-562392` becomes `xray_tests_CXQA_562392.json`).

## Output format (console)

```
============================================================
Xray Test Fetcher — CXQA-562392
Issue Type : Test Execution
Total Tests: 12
============================================================

  [  1] CXQA-7449
        Summary : CXone Navigation on ACD, Reporting, WFI
        Status  : PASS
        Type    : Manual

  [  2] CXQA-7420
        Summary : Login to Max and make a call validate Supervisor Application
        Status  : FAIL
        Type    : Manual
...
Full JSON written to: xray_tests_CXQA_562392.json
```

## Error handling

- Missing `XRAY_CLIENT_ID` / `XRAY_CLIENT_SECRET` environment variables → clear setup error with platform-specific instructions.
- Invalid Jira issue key format (not matching pattern like `CXQA-9514`) → immediate validation error.
- Authentication failure → error with Xray API response body.
- Unsupported/unresolvable issue type or no linked tests found → explicit stop message.
- Epic lookup without Jira env vars (`JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`) → clear guidance to set required variables.

## Script location

```
skills/xray-test-fetcher/
├── SKILL.md               ← this file
└── scripts/
    └── fetch_xray_tests.py
```
