#!/usr/bin/env python3
"""
Fetch the content of a Jira issue (Epic, Story, or any type) by its key.

Usage: python get_jira_issue.py <ISSUE-KEY>
Example: python get_jira_issue.py CXREC-105393

Returns: JSON with key, summary, description, issueType, status, acceptanceCriteria, etc.

Required Environment Variables:
  CONFLUENCE_USERNAME, CONFLUENCE_TOKEN

Optional Environment Variables:
  JIRA_BASE_URL (default: https://nice-ce-cxone-prod.atlassian.net)
"""

import os
import sys
import json
import base64
import requests
import urllib3

def _ssl_verify():
    val = os.environ.get("SSL_VERIFY", "true").strip().lower()
    if val in ("false", "0", "no"):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return False
    return True


FETCH_TIMEOUT_S = 60

EMPTY_RESULT = {
    "success": False,
    "key": "",
    "summary": "",
    "description": "",
    "issueType": "",
    "status": "",
    "acceptanceCriteria": "",
    "labels": [],
    "priority": "",
    "assignee": "",
    "reporter": "",
    "components": [],
    "attachmentsCount": 0,
    "riskNotes": "",
    "tshirtSize": "",
    "releaseContent": "",
    "affectedDocumentation": "",
}


def get_basic_auth():
    username = os.environ.get("CONFLUENCE_USERNAME")
    token = os.environ.get("CONFLUENCE_TOKEN")
    if not username or not token:
        print("Error: CONFLUENCE_USERNAME and CONFLUENCE_TOKEN environment variables are required.", file=sys.stderr)
        sys.exit(1)
    credentials = base64.b64encode(f"{username}:{token}".encode()).decode()
    return f"Basic {credentials}"


def get_base_url():
    return os.environ.get("JIRA_BASE_URL", "https://nice-ce-cxone-prod.atlassian.net")


def get_issue(issue_key):
    url = f"{get_base_url()}/rest/api/2/issue/{issue_key}"
    try:
        response = requests.get(
            url,
            headers={"Authorization": get_basic_auth(), "Accept": "application/json"},
            timeout=FETCH_TIMEOUT_S,
            verify=_ssl_verify()
        )
        if not response.ok:
            return {**EMPTY_RESULT, "key": issue_key, "error": f"HTTP {response.status_code}: {response.text}"}

        data = response.json()
        fields = data.get("fields") or {}

        tshirt_raw = fields.get("customfield_10041")
        if isinstance(tshirt_raw, dict):
            tshirt_size = tshirt_raw.get("value", "")
        elif isinstance(tshirt_raw, str):
            tshirt_size = tshirt_raw
        else:
            tshirt_size = ""

        return {
            "success": True,
            "key": data.get("key", issue_key),
            "summary": fields.get("summary", ""),
            "description": fields.get("description", ""),
            "issueType": (fields.get("issuetype") or {}).get("name", ""),
            "status": (fields.get("status") or {}).get("name", ""),
            "acceptanceCriteria": fields.get("customfield_10039", ""),
            "labels": fields.get("labels") or [],
            "priority": (fields.get("priority") or {}).get("name", ""),
            "assignee": (fields.get("assignee") or {}).get("displayName", ""),
            "assigneeAccountId": (fields.get("assignee") or {}).get("accountId", ""),
            "reporter": (fields.get("reporter") or {}).get("displayName", ""),
            "reporterAccountId": (fields.get("reporter") or {}).get("accountId", ""),
            "components": [(c.get("name") or "") for c in (fields.get("components") or [])],
            "attachmentsCount": len(fields.get("attachment") or []),
            "riskNotes": fields.get("customfield_10057", ""),
            "tshirtSize": tshirt_size,
            "releaseContent": fields.get("customfield_11581", ""),
            "affectedDocumentation": fields.get("customfield_10069", ""),
        }
    except requests.Timeout:
        return {**EMPTY_RESULT, "key": issue_key, "error": f"Request timed out after {FETCH_TIMEOUT_S}s"}
    except Exception as e:
        return {**EMPTY_RESULT, "key": issue_key, "error": str(e)}


def main():
    args = sys.argv[1:]
    if len(args) != 1:
        print("Usage: python get_jira_issue.py <ISSUE-KEY>", file=sys.stderr)
        print("Example: python get_jira_issue.py CXREC-105393", file=sys.stderr)
        sys.exit(1)

    result = get_issue(args[0])
    print(json.dumps(result, indent=2))
    if not result["success"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
