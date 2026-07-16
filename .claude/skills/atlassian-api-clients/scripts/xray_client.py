#!/usr/bin/env python3
"""
Shared Xray Cloud GraphQL client for standalone skill scripts.

Required Environment Variables:
  XRAY_CLIENT_ID      - Xray Cloud API client ID
  XRAY_CLIENT_SECRET  - Xray Cloud API client secret
"""

import os
import sys
import requests
import urllib3

def _ssl_verify():
    val = os.environ.get("SSL_VERIFY", "true").strip().lower()
    if val in ("false", "0", "no"):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return False
    return True


XRAY_AUTH_URL = "https://xray.cloud.getxray.app/api/v2/authenticate"
XRAY_GRAPHQL_URL = "https://xray.cloud.getxray.app/api/v2/graphql"
FETCH_TIMEOUT_S = 30

_cached_token = None


def get_xray_credentials():
    client_id = os.environ.get("XRAY_CLIENT_ID")
    client_secret = os.environ.get("XRAY_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("Error: XRAY_CLIENT_ID and XRAY_CLIENT_SECRET environment variables are required.", file=sys.stderr)
        print("\nSet them using:", file=sys.stderr)
        print('  Windows PowerShell: $env:XRAY_CLIENT_ID = "your_client_id"', file=sys.stderr)
        print('  Linux/macOS:        export XRAY_CLIENT_ID="your_client_id"', file=sys.stderr)
        print("\nGet credentials from: https://xray.cloud.getxray.app/api/settings", file=sys.stderr)
        sys.exit(1)
    return client_id, client_secret


def authenticate_xray():
    global _cached_token
    if _cached_token:
        return _cached_token

    client_id, client_secret = get_xray_credentials()
    try:
        response = requests.post(
            XRAY_AUTH_URL,
            json={"client_id": client_id, "client_secret": client_secret},
            timeout=FETCH_TIMEOUT_S,
            verify=_ssl_verify()
        )
        if not response.ok:
            raise Exception(f"Xray authentication failed (HTTP {response.status_code}): {response.text}")

        token = response.json()
        if not isinstance(token, str) or not token:
            raise Exception("Failed to parse Xray auth token from response.")
        _cached_token = token
        return token
    except requests.Timeout:
        raise Exception(f"Xray authentication timed out after {FETCH_TIMEOUT_S}s")


def xray_graphql(query, variables=None):
    token = authenticate_xray()
    try:
        response = requests.post(
            XRAY_GRAPHQL_URL,
            json={"query": query, "variables": variables or {}},
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            timeout=FETCH_TIMEOUT_S,
            verify=_ssl_verify()
        )
        if not response.ok:
            raise Exception(f"Xray GraphQL request failed (HTTP {response.status_code}): {response.text}")

        data = response.json()
        if data.get("errors"):
            messages = "; ".join(e.get("message", "Unknown error") for e in data["errors"])
            raise Exception(f"Xray GraphQL errors: {messages}")

        return data.get("data")
    except requests.Timeout:
        raise Exception(f"Xray GraphQL request timed out after {FETCH_TIMEOUT_S}s")
