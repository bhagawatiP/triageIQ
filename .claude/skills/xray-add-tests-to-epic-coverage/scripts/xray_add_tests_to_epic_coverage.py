#!/usr/bin/env python3
"""
Add test(s) to a Jira Epic's Test Coverage section via the Jira REST API.

Mirrors the working logic from epicTestcoverage.py:
  - Primary:  "Test" link type with test as inwardIssue, epic as outwardIssue
  - Fallback: Other link types (outward direction), then Epic Link custom fields

Usage:
  # Single test
  python xray_add_tests_to_epic_coverage.py --epic EPIC-KEY --tests TEST-KEY

  # Multiple tests
  python xray_add_tests_to_epic_coverage.py --epic EPIC-KEY --tests TEST-KEY-1 TEST-KEY-2 TEST-KEY-3

Examples:
  python xray_add_tests_to_epic_coverage.py --epic AN-135469 --tests AN-139035
  python xray_add_tests_to_epic_coverage.py --epic AN-135469 --tests AN-139035 AN-139036

Required Environment Variables:
  CONFLUENCE_USERNAME  - Atlassian username (email)
  CONFLUENCE_TOKEN     - Atlassian API token
"""

import sys
import os
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


JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "https://nice-ce-cxone-prod.atlassian.net")

# Fallback Epic Link custom field IDs to try as last resort
EPIC_FIELD_IDS = [
    "customfield_10014",
    "customfield_10008",
    "customfield_10009",
    "customfield_10015",
]


def get_auth():
    username = os.environ.get("CONFLUENCE_USERNAME")
    token = os.environ.get("CONFLUENCE_TOKEN")
    if not username or not token:
        print("Error: CONFLUENCE_USERNAME and CONFLUENCE_TOKEN are required.", file=sys.stderr)
        sys.exit(1)
    return requests.auth.HTTPBasicAuth(username, token)


def get_headers():
    return {"Accept": "application/json", "Content-Type": "application/json"}


def link_via_relationship(test_key: str, epic_key: str, link_name: str, direction: str) -> dict:
    """
    Create a Jira issue link between a test and an epic.
    Mirrors epicTestcoverage.py link_via_relationship() exactly.

    direction="inward":  test is inwardIssue,  epic is outwardIssue
    direction="outward": epic is inwardIssue,   test is outwardIssue

    Returns {"success": True} on success, or
    {"success": False, "status_code": <int>, "error": <str>} on HTTP failure, or
    {"success": False, "error": <str>} on request exception.
    """
    if direction == "inward":
        payload = {
            "type": {"name": link_name},
            "inwardIssue": {"key": test_key},
            "outwardIssue": {"key": epic_key},
        }
    else:
        payload = {
            "type": {"name": link_name},
            "inwardIssue": {"key": epic_key},
            "outwardIssue": {"key": test_key},
        }

    try:
        response = requests.post(
            f"{JIRA_BASE_URL}/rest/api/3/issueLink",
            json=payload,
            headers=get_headers(),
            auth=get_auth(),
            timeout=60,
            verify=_ssl_verify()
        )
        if response.status_code in (200, 201):
            return {"success": True}
        return {
            "success": False,
            "status_code": response.status_code,
            "error": response.text or f"HTTP {response.status_code}",
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def link_via_epic_field(test_key: str, epic_key: str, field_id: str) -> dict:
    """
    Set Epic Link custom field on the test issue as last resort.

    Returns {"success": True} on success, or
    {"success": False, "status_code": <int>, "error": <str>} on HTTP failure, or
    {"success": False, "error": <str>} on request exception.
    """
    try:
        response = requests.put(
            f"{JIRA_BASE_URL}/rest/api/3/issue/{test_key}",
            json={"fields": {field_id: epic_key}},
            headers=get_headers(),
            auth=get_auth(),
            timeout=60,
            verify=_ssl_verify()
        )
        if response.status_code in (200, 204):
            return {"success": True}
        return {
            "success": False,
            "status_code": response.status_code,
            "error": response.text or f"HTTP {response.status_code}",
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def link_test_to_epic(test_key: str, epic_key: str) -> dict:
    """
    Link a single test to an epic for Test Coverage.
    Attempt order mirrors epicTestcoverage.py link_issue_to_epic():
      1. "Test" inward  (test as inwardIssue, epic as outwardIssue) — preferred
      2. "Test" outward (epic as inwardIssue, test as outwardIssue)
      3. "Relates" outward
      4. "Blocks"  outward
      5. "Covers"  outward
      6. Epic Link custom field fallback
    """
    attempt_errors = []

    def _record_error(method_label: str, result: dict) -> None:
        entry = {"method": method_label, "error": result.get("error", "Unknown error")}
        if "status_code" in result:
            entry["status_code"] = result["status_code"]
        attempt_errors.append(entry)

    # Step 1: Try "Test" inward — primary working method from epicTestcoverage.py
    result = link_via_relationship(test_key, epic_key, "Test", "inward")
    if result["success"]:
        return {
            "success": True,
            "epicKey": epic_key,
            "testKey": test_key,
            "method": "Test link (inward: test is tested by epic)",
            "message": f"Test {test_key} added to epic {epic_key} Test Coverage",
        }
    _record_error("Test inward", result)

    # Steps 2-5: Try other link types
    fallbacks = [
        ("Test",    "outward"),
        ("Relates", "outward"),
        ("Blocks",  "outward"),
        ("Covers",  "outward"),
    ]
    for link_name, direction in fallbacks:
        result = link_via_relationship(test_key, epic_key, link_name, direction)
        if result["success"]:
            return {
                "success": True,
                "epicKey": epic_key,
                "testKey": test_key,
                "method": f"{link_name} link ({direction})",
                "message": f"Test {test_key} linked to epic {epic_key} via '{link_name}' ({direction})",
            }
        _record_error(f"{link_name} {direction}", result)

    # Step 6: Epic Link custom field fallback
    for field_id in EPIC_FIELD_IDS:
        result = link_via_epic_field(test_key, epic_key, field_id)
        if result["success"]:
            return {
                "success": True,
                "epicKey": epic_key,
                "testKey": test_key,
                "method": f"Epic Link field ({field_id})",
                "message": f"Test {test_key} linked to epic {epic_key} via Epic Link field '{field_id}'",
            }
        _record_error(f"Epic Link field {field_id}", result)

    return {
        "success": False,
        "epicKey": epic_key,
        "testKey": test_key,
        "error": "All linking methods failed.",
        "details": attempt_errors,
    }


def add_tests_to_epic_coverage(epic_key: str, test_keys: list) -> dict:
    """Add multiple tests to an epic's Test Coverage section."""
    results = []
    successfully_added = []
    failed = []

    for test_key in test_keys:
        r = link_test_to_epic(test_key, epic_key)
        results.append(r)
        if r.get("success"):
            successfully_added.append(test_key)
        else:
            failed.append({"testKey": test_key, "error": r.get("error")})

    output = {
        "success": len(failed) == 0,
        "epicKey": epic_key,
        "totalTests": len(test_keys),
        "successfullyAdded": len(successfully_added),
        "failed": len(failed),
        "addedTestKeys": successfully_added,
        "results": results,
    }
    if failed:
        output["failedTests"] = failed
    return output


def main():
    args = sys.argv[1:]
    epic_key = None
    test_keys = []

    i = 0
    while i < len(args):
        if args[i] == "--epic" and i + 1 < len(args):
            epic_key = args[i + 1]
            i += 2
        elif args[i] == "--tests":
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                test_keys.append(args[i])
                i += 1
        else:
            i += 1

    if not epic_key or not test_keys:
        print(
            "Usage: python xray_add_tests_to_epic_coverage.py --epic <EPIC-KEY> --tests <TEST-KEY-1> [TEST-KEY-2] ...",
            file=sys.stderr,
        )
        sys.exit(1)

    result = add_tests_to_epic_coverage(epic_key, test_keys)
    print(json.dumps(result, indent=2))
    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
