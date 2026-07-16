#!/usr/bin/env python3
"""
Approve a Jira test issue by transitioning it through the required workflow states.

Usage: python approve_jira_test.py <TEST-KEY>
Example: python approve_jira_test.py CXREC-107916

Workflow: Draft → Under Review → Approved (two-step JIRA transition)

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


def fetch_transitions(test_key):
    url = f"{get_base_url()}/rest/api/2/issue/{test_key}/transitions"
    try:
        response = requests.get(
            url,
            headers={"Authorization": get_basic_auth(), "Accept": "application/json"},
            timeout=FETCH_TIMEOUT_S,
            verify=_ssl_verify()
        )
        if not response.ok:
            raise Exception(f"HTTP {response.status_code}: {response.text}")
        return response.json()
    except requests.Timeout:
        raise Exception(f"Request timed out after {FETCH_TIMEOUT_S}s")


def find_transition_to_status(transitions, target_status):
    target_lower = target_status.lower()
    for t in transitions:
        if (t.get("to", {}).get("name") or "").lower() == target_lower:
            return t["id"]
    for t in transitions:
        t_name = (t.get("name") or "").lower()
        to_name = (t.get("to", {}).get("name") or "").lower()
        if target_lower in to_name or target_lower in t_name:
            return t["id"]
    return None


def fetch_current_user_account_id():
    url = f"{get_base_url()}/rest/api/2/myself"
    try:
        response = requests.get(
            url,
            headers={"Authorization": get_basic_auth(), "Accept": "application/json"},
            timeout=FETCH_TIMEOUT_S,
            verify=_ssl_verify()
        )
        if not response.ok:
            raise Exception(f"HTTP {response.status_code}: {response.text}")
        return response.json()["accountId"]
    except requests.Timeout:
        raise Exception(f"Request timed out after {FETCH_TIMEOUT_S}s")


def assign_issue(test_key, account_id):
    url = f"{get_base_url()}/rest/api/2/issue/{test_key}/assignee"
    try:
        response = requests.put(
            url,
            json={"accountId": account_id},
            headers={"Authorization": get_basic_auth(), "Content-Type": "application/json"},
            timeout=FETCH_TIMEOUT_S,
            verify=_ssl_verify()
        )
        if not response.ok:
            raise Exception(f"Failed to assign issue: HTTP {response.status_code}: {response.text}")
    except requests.Timeout:
        raise Exception(f"Request timed out after {FETCH_TIMEOUT_S}s")


def execute_transition(test_key, transition_id):
    url = f"{get_base_url()}/rest/api/2/issue/{test_key}/transitions"
    try:
        response = requests.post(
            url,
            json={"transition": {"id": transition_id}},
            headers={"Authorization": get_basic_auth(), "Content-Type": "application/json"},
            timeout=FETCH_TIMEOUT_S,
            verify=_ssl_verify()
        )
        if not response.ok:
            raise Exception(f"HTTP {response.status_code}: {response.text}")
    except requests.Timeout:
        raise Exception(f"Request timed out after {FETCH_TIMEOUT_S}s")


def approve_test(test_key):
    try:
        account_id = fetch_current_user_account_id()
        assign_issue(test_key, account_id)

        pre_transitions = fetch_transitions(test_key).get("transitions", [])
        under_review_id = find_transition_to_status(pre_transitions, "under review")
        if under_review_id:
            execute_transition(test_key, under_review_id)

        post_transitions = fetch_transitions(test_key).get("transitions", [])
        approved_id = find_transition_to_status(post_transitions, "approved")
        if not approved_id:
            available = ", ".join(f"{t.get('name')} → {t.get('to', {}).get('name')}" for t in post_transitions)
            raise Exception(f'No "Approved" transition found for {test_key}. Available transitions: [{available}]')

        execute_transition(test_key, approved_id)
        return {"success": True, "testKey": test_key, "newStatus": "Approved", "message": f"Test {test_key} has been successfully approved"}
    except Exception as e:
        return {"success": False, "testKey": test_key, "message": f"Failed to approve test {test_key}", "error": str(e)}


def main():
    args = sys.argv[1:]
    if len(args) != 1:
        print("Usage: python approve_jira_test.py <TEST-KEY>", file=sys.stderr)
        print("Example: python approve_jira_test.py CXREC-107916", file=sys.stderr)
        sys.exit(1)

    result = approve_test(args[0])
    print(json.dumps(result, indent=2))
    if not result["success"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
