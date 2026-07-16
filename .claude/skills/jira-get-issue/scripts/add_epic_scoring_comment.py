#!/usr/bin/env python3
"""
Add an Epic Scoring report as a comment on a Jira issue, tagging the reporter and assignee.

Usage: python add_epic_scoring_comment.py <ISSUE-KEY> [--assignee-id <ACCOUNT_ID>] [--reporter-id <ACCOUNT_ID>]

The comment body is read from stdin (pipe the report text).

Example:
  echo "Report text here..." | python add_epic_scoring_comment.py CXREC-12345 --assignee-id abc123 --reporter-id def456

Required Environment Variables:
  CONFLUENCE_USERNAME, CONFLUENCE_TOKEN

Optional Environment Variables:
  JIRA_BASE_URL (default: https://nice-ce-cxone-prod.atlassian.net)
"""

import os
import sys
import json
import base64
import argparse
import requests
import urllib3

def _ssl_verify():
    val = os.environ.get("SSL_VERIFY", "true").strip().lower()
    if val in ("false", "0", "no"):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return False
    return True


FETCH_TIMEOUT_S = 60


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


def build_comment_body(report_text, assignee_id, reporter_id):
    """Build comment body with user mentions using Jira wiki markup notation."""
    mentions = []
    if reporter_id:
        mentions.append(f"[~accountId:{reporter_id}]")
    if assignee_id:
        mentions.append(f"[~accountId:{assignee_id}]")

    mention_line = ""
    if mentions:
        mention_line = f"cc: {' '.join(mentions)}\n\n"

    # Use Jira wiki markup code block for the report
    body = f"{mention_line}{{noformat}}\n{report_text}\n{{noformat}}"
    return body


def add_comment(issue_key, body):
    """Add a comment to the Jira issue."""
    url = f"{get_base_url()}/rest/api/2/issue/{issue_key}/comment"
    payload = {"body": body}

    response = requests.post(
        url,
        headers={
            "Authorization": get_basic_auth(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=FETCH_TIMEOUT_S,
        verify=_ssl_verify()
    )

    if not response.ok:
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text}"}

    data = response.json()
    return {"success": True, "comment_id": data.get("id", ""), "issue_key": issue_key}


def main():
    parser = argparse.ArgumentParser(description="Add Epic Scoring report as a Jira comment")
    parser.add_argument("issue_key", help="Jira issue key (e.g., CXREC-12345)")
    parser.add_argument("--assignee-id", default="", help="Assignee account ID for tagging")
    parser.add_argument("--reporter-id", default="", help="Reporter account ID for tagging")
    args = parser.parse_args()

    # Read report from stdin
    if sys.stdin.isatty():
        print("Error: Report text must be piped via stdin.", file=sys.stderr)
        print("Example: echo \"report\" | python add_epic_scoring_comment.py CXREC-12345", file=sys.stderr)
        sys.exit(1)

    report_text = sys.stdin.read().strip()
    if not report_text:
        print("Error: Empty report text received from stdin.", file=sys.stderr)
        sys.exit(1)

    comment_body = build_comment_body(report_text, args.assignee_id, args.reporter_id)
    result = add_comment(args.issue_key, comment_body)
    print(json.dumps(result, indent=2))
    if not result["success"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
