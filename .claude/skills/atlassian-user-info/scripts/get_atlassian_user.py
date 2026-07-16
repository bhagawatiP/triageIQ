#!/usr/bin/env python3
"""
Get the current authenticated Jira user's information.

Usage: python get_atlassian_user.py

Returns: JSON with accountId, displayName, emailAddress, active, accountType

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


def get_current_user():
    url = f"{get_base_url()}/rest/api/2/myself"
    try:
        response = requests.get(
            url,
            headers={"Authorization": get_basic_auth(), "Accept": "application/json"},
            timeout=FETCH_TIMEOUT_S,
            verify=_ssl_verify()
        )
        if not response.ok:
            return {
                "success": False, "accountId": None, "displayName": None,
                "emailAddress": None, "active": None, "accountType": None,
                "error": f"HTTP {response.status_code}: {response.text}",
            }
        data = response.json()
        return {
            "success": True,
            "accountId": data.get("accountId"),
            "displayName": data.get("displayName"),
            "emailAddress": data.get("emailAddress"),
            "active": data.get("active"),
            "accountType": data.get("accountType"),
        }
    except requests.Timeout:
        return {
            "success": False, "accountId": None, "displayName": None,
            "emailAddress": None, "active": None, "accountType": None,
            "error": f"Request timed out after {FETCH_TIMEOUT_S}s",
        }
    except Exception as e:
        return {
            "success": False, "accountId": None, "displayName": None,
            "emailAddress": None, "active": None, "accountType": None,
            "error": str(e),
        }


def main():
    result = get_current_user()
    print(json.dumps(result, indent=2))
    if not result["success"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
