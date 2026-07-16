#!/usr/bin/env python3
"""
Update Epic Scoring labels on a Jira issue.

Removes any existing scoring labels (epicScoreBelow70, epicScoreAbove70) and adds the new one
based on the overall score percentage.

Usage: python update_epic_scoring_label.py <ISSUE-KEY> <SCORE>
Example: python update_epic_scoring_label.py CXREC-12345 75

  - If SCORE >= 70, adds "epicScoreAbove70" label
  - If SCORE < 70, adds "epicScoreBelow70" label
  - Removes the opposite label if it exists

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
SCORING_LABELS = ["epicScoreBelow70", "epicScoreAbove70"]


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


def update_labels(issue_key, score):
    """Remove old scoring labels and add the new one based on score."""
    new_label = "epicScoreAbove70" if score >= 70 else "epicScoreBelow70"

    # Build update operations: remove all scoring labels, then add the correct one
    label_operations = []
    for label in SCORING_LABELS:
        label_operations.append({"remove": label})
    label_operations.append({"add": new_label})

    url = f"{get_base_url()}/rest/api/2/issue/{issue_key}"
    body = {"update": {"labels": label_operations}}

    response = requests.put(
        url,
        headers={
            "Authorization": get_basic_auth(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=FETCH_TIMEOUT_S,
        verify=_ssl_verify()
    )

    if not response.ok:
        print(f"Error updating labels: HTTP {response.status_code}: {response.text}", file=sys.stderr)
        return {"success": False, "error": f"HTTP {response.status_code}: {response.text}"}

    return {"success": True, "label_added": new_label, "issue_key": issue_key, "score": score}


def main():
    args = sys.argv[1:]
    if len(args) != 2:
        print("Usage: python update_epic_scoring_label.py <ISSUE-KEY> <SCORE>", file=sys.stderr)
        print("Example: python update_epic_scoring_label.py CXREC-12345 75", file=sys.stderr)
        sys.exit(1)

    issue_key = args[0]
    try:
        score = int(args[1])
    except ValueError:
        print(f"Error: SCORE must be an integer, got '{args[1]}'", file=sys.stderr)
        sys.exit(1)

    result = update_labels(issue_key, score)
    print(json.dumps(result, indent=2))
    if not result["success"]:
        sys.exit(1)


if __name__ == "__main__":
    main()