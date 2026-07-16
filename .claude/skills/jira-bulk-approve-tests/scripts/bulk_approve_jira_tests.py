#!/usr/bin/env python3
"""
Approve multiple Jira test issues in bulk with batching support.

Usage: python bulk_approve_jira_tests.py <TEST-KEY-1> <TEST-KEY-2> ...
Example: python bulk_approve_jira_tests.py CXREC-107916 CXREC-107917 CXREC-107918

Workflow per test: Draft → Under Review → Approved
Processes in batches of 10.

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
BATCH_SIZE = 10


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


def approve_test(test_key):
    resp1 = fetch_transitions(test_key)
    tid1 = find_transition_to_status(resp1.get("transitions", []), "under review")
    if not tid1:
        raise Exception(f'No "Under Review" transition found for {test_key}')
    execute_transition(test_key, tid1)

    resp2 = fetch_transitions(test_key)
    tid2 = find_transition_to_status(resp2.get("transitions", []), "approved")
    if not tid2:
        raise Exception(f'No "Approved" transition found for {test_key}')
    execute_transition(test_key, tid2)


def main():
    test_keys = sys.argv[1:]
    if not test_keys:
        print("Usage: python bulk_approve_jira_tests.py <TEST-KEY-1> <TEST-KEY-2> ...", file=sys.stderr)
        sys.exit(1)

    successful, failed, timed_out = [], [], []

    for i in range(0, len(test_keys), BATCH_SIZE):
        batch = test_keys[i:i + BATCH_SIZE]
        for test_key in batch:
            try:
                approve_test(test_key)
                successful.append(test_key)
            except Exception as e:
                if "timed out" in str(e):
                    timed_out.append(test_key)
                else:
                    failed.append(test_key)

    result = {
        "operation": "approve",
        "total": len(test_keys),
        "successful": len(successful),
        "failed": len(failed),
        "timedOut": len(timed_out),
        "successfulTests": successful,
        "failedTests": failed,
        "timedOutTests": timed_out,
    }

    print(json.dumps(result, indent=2))
    if failed or timed_out:
        sys.exit(1)


if __name__ == "__main__":
    main()
