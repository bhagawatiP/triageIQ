#!/usr/bin/env python3
"""
Remove a Jira test issue by transitioning it through the required workflow states.

Usage: python remove_jira_test.py <TEST-KEY>
Example: python remove_jira_test.py CXREC-107916

Workflow:
  - If status is "Approved" → Open → Removed
  - Otherwise → Removed (direct transition)

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


def get_current_status(test_key):
    url = f"{get_base_url()}/rest/api/2/issue/{test_key}?fields=status"
    try:
        response = requests.get(
            url,
            headers={"Authorization": get_basic_auth(), "Accept": "application/json"},
            timeout=FETCH_TIMEOUT_S,
            verify=_ssl_verify()
        )
        if not response.ok:
            return None
        return response.json().get("fields", {}).get("status", {}).get("name")
    except Exception:
        return None


def move_to_open(test_key):
    response = fetch_transitions(test_key)
    transitions = response.get("transitions", [])
    transition_id = find_transition_to_status(transitions, "open") or find_transition_to_status(transitions, "re-open")
    if not transition_id:
        available = ", ".join(f"{t.get('name')} → {t.get('to', {}).get('name')}" for t in transitions)
        raise Exception(f'No "Open" or "Re-Open" transition found for {test_key}. Available: [{available}]')
    execute_transition(test_key, transition_id)


def move_to_removed(test_key):
    response = fetch_transitions(test_key)
    transitions = response.get("transitions", [])
    transition_id = find_transition_to_status(transitions, "removed")
    if not transition_id:
        available = ", ".join(f"{t.get('name')} → {t.get('to', {}).get('name')}" for t in transitions)
        raise Exception(f'No "Removed" transition found for {test_key}. Available: [{available}]')
    execute_transition(test_key, transition_id)


def remove_test(test_key):
    try:
        current_status = get_current_status(test_key)
        if not current_status:
            return {"success": False, "testKey": test_key, "message": f"Failed to get current status for test: {test_key}", "error": "Unable to retrieve test status"}

        if current_status.lower() == "approved":
            move_to_open(test_key)

        move_to_removed(test_key)
        return {"success": True, "testKey": test_key, "newStatus": "Removed", "message": f"Test {test_key} has been successfully removed"}
    except Exception as e:
        return {"success": False, "testKey": test_key, "message": f"Failed to remove test {test_key}", "error": str(e)}


def main():
    args = sys.argv[1:]
    if len(args) != 1:
        print("Usage: python remove_jira_test.py <TEST-KEY>", file=sys.stderr)
        print("Example: python remove_jira_test.py CXREC-107916", file=sys.stderr)
        sys.exit(1)

    result = remove_test(args[0])
    print(json.dumps(result, indent=2))
    if not result["success"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
