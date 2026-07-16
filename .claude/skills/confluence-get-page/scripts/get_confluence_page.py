#!/usr/bin/env python3
"""
Fetch raw Confluence page content by page ID or URL.

Usage:
  By ID:  python get_confluence_page.py --id <PAGE-ID>
  By URL: python get_confluence_page.py --url <CONFLUENCE-URL>

Returns: JSON with success, pageId, title, and content (plain text)

Required Environment Variables:
  CONFLUENCE_USERNAME, CONFLUENCE_TOKEN

Optional Environment Variables:
  JIRA_BASE_URL (default: https://nice-ce-cxone-prod.atlassian.net)
"""

import os
import sys
import json
import re
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


def extract_page_id_from_url(url):
    if "/pages/" not in url:
        raise Exception(f"Invalid Confluence URL format: Could not extract page ID from URL: {url}")
    after_pages = url.split("/pages/")[1]
    page_id = after_pages.split("/")[0]
    if not page_id:
        raise Exception(f"Invalid Confluence URL format: Could not extract page ID from URL: {url}")
    return page_id


def html_to_plain_text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def get_confluence_page(page_id):
    url = f"{get_base_url()}/wiki/rest/api/content/{page_id}?expand=body.storage"
    try:
        response = requests.get(
            url,
            headers={"Authorization": get_basic_auth(), "Accept": "application/json"},
            timeout=FETCH_TIMEOUT_S,
            verify=_ssl_verify()
        )
        if not response.ok:
            return {"success": False, "pageId": page_id, "title": "", "content": "", "error": f"HTTP {response.status_code}: {response.text}"}

        data = response.json()
        title = data.get("title", f"Page {page_id}")
        html_content = data.get("body", {}).get("storage", {}).get("value", "")
        return {"success": True, "pageId": page_id, "title": title, "content": html_to_plain_text(html_content)}
    except requests.Timeout:
        return {"success": False, "pageId": page_id, "title": "", "content": "", "error": f"Request timed out after {FETCH_TIMEOUT_S}s"}
    except Exception as e:
        return {"success": False, "pageId": page_id, "title": "", "content": "", "error": str(e)}


def main():
    args = sys.argv[1:]
    if len(args) < 2 or args[0] not in ("--id", "--url"):
        print("Usage:", file=sys.stderr)
        print("  python get_confluence_page.py --id <PAGE-ID>", file=sys.stderr)
        print("  python get_confluence_page.py --url <CONFLUENCE-URL>", file=sys.stderr)
        sys.exit(1)

    if args[0] == "--url":
        try:
            page_id = extract_page_id_from_url(args[1])
        except Exception as e:
            print(json.dumps({"success": False, "pageId": "", "title": "", "content": "", "error": str(e)}, indent=2))
            sys.exit(1)
    else:
        page_id = args[1]

    result = get_confluence_page(page_id)
    print(json.dumps(result, indent=2))
    if not result["success"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
