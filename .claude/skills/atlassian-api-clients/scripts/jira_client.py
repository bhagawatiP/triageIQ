#!/usr/bin/env python3
"""
Shared Jira REST API client for standalone skill scripts.

Required Environment Variables:
  CONFLUENCE_USERNAME  - Atlassian username (email)
  CONFLUENCE_TOKEN     - Atlassian API token

Optional Environment Variables:
  JIRA_BASE_URL        - Base URL (default: https://nice-ce-cxone-prod.atlassian.net)
"""

import os
import sys
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


def get_jira_base_url():
    return os.environ.get("JIRA_BASE_URL", "https://nice-ce-cxone-prod.atlassian.net")


def get_basic_auth():
    username = os.environ.get("CONFLUENCE_USERNAME")
    token = os.environ.get("CONFLUENCE_TOKEN")
    if not username or not token:
        print("Error: CONFLUENCE_USERNAME and CONFLUENCE_TOKEN environment variables are required.", file=sys.stderr)
        sys.exit(1)
    credentials = base64.b64encode(f"{username}:{token}".encode()).decode()
    return f"Basic {credentials}"


def jira_fetch(path, method="GET", body=None):
    url = f"{get_jira_base_url()}{path}"
    headers = {
        "Authorization": get_basic_auth(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        response = requests.request(
            method,
            url,
            headers=headers,
            json=body,
            timeout=FETCH_TIMEOUT_S,
            verify=_ssl_verify()
        )
        return {"ok": response.ok, "status": response.status_code, "body": response.text}
    except requests.Timeout:
        raise Exception(f"Request timed out after {FETCH_TIMEOUT_S}s")
