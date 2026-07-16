#!/usr/bin/env python3
"""
Context Aggregator - Fetches requirements from Jira issues and Confluence pages,
then returns structured context for AI-driven test scenario generation.

Usage: python context_aggregator.py [options]
Options:
  --jiraKeys "<KEY1>,<KEY2>,..."
  --confluenceIds "<ID1>,<ID2>,..."
  --confluenceUrls "<URL1>,<URL2>,..."
  --featureHint "<text>"

Required Environment Variables:
  CONFLUENCE_USERNAME, CONFLUENCE_TOKEN
"""

import os
import sys
import json
import re
import base64
import requests
import urllib3
from urllib.parse import quote, unquote

def _ssl_verify():
    val = os.environ.get("SSL_VERIFY", "true").strip().lower()
    if val in ("false", "0", "no"):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return False
    return True


BASE_URL = os.environ.get("JIRA_BASE_URL", "https://nice-ce-cxone-prod.atlassian.net")


def get_basic_auth():
    username = os.environ.get("CONFLUENCE_USERNAME") or os.environ.get("JIRA_USERNAME")
    token = os.environ.get("CONFLUENCE_TOKEN") or os.environ.get("JIRA_TOKEN")
    if not username or not token:
        print("Error: CONFLUENCE_USERNAME and CONFLUENCE_TOKEN (or JIRA_USERNAME/JIRA_TOKEN) are required.", file=sys.stderr)
        sys.exit(1)
    return base64.b64encode(f"{username}:{token}".encode()).decode()


def get_arg(args, flag):
    try:
        idx = args.index(flag)
        return args[idx + 1] if idx + 1 < len(args) else None
    except ValueError:
        return None


def strip_html(html):
    text = re.sub(r"<[^>]*>", " ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
    text = text.replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\s+", " ", text).strip()


def fetch_jira_issue(auth, issue_key):
    try:
        url = f"{BASE_URL}/rest/api/2/issue/{quote(issue_key)}?fields=summary,description,issuetype,status,labels,priority,customfield_10037,fixVersions,customfield_10040"
        response = requests.get(url, headers={"Authorization": f"Basic {auth}", "Accept": "application/json"}, timeout=60,
            verify=_ssl_verify()
        )
        if not response.ok:
            return None
        data = response.json()
        fields = data.get("fields", {})
        return {
            "key": data["key"],
            "summary": fields.get("summary", ""),
            "description": fields.get("description"),
            "issueType": (fields.get("issuetype") or {}).get("name", ""),
            "status": (fields.get("status") or {}).get("name", ""),
            "acceptanceCriteria": fields.get("customfield_10037"),
            "labels": fields.get("labels") or [],
            "priority": (fields.get("priority") or {}).get("name", "Medium"),
            "fixVersions": [v.get("name", "") for v in (fields.get("fixVersions") or [])],
            "team": ((fields.get("customfield_10040") or [{}])[0]).get("value", ""),
        }
    except Exception:
        return None


def fetch_child_issues(auth, epic_key):
    try:
        jql = f'"Epic Link" = {epic_key} OR parent = {epic_key}'
        url = f"{BASE_URL}/rest/api/3/search/jql"
        payload = {
            "jql": jql,
            "fields": ["summary", "description", "issuetype", "status", "labels", "priority", "customfield_10037"],
            "maxResults": 50
        }
        response = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Basic {auth}", "Accept": "application/json", "Content-Type": "application/json"},
            timeout=60,
            verify=_ssl_verify()
        )
        if not response.ok:
            return []
        issues = response.json().get("issues", [])
        return [{
            "key": issue["key"],
            "summary": issue["fields"].get("summary", ""),
            "description": issue["fields"].get("description"),
            "issueType": (issue["fields"].get("issuetype") or {}).get("name", ""),
            "status": (issue["fields"].get("status") or {}).get("name", ""),
            "acceptanceCriteria": issue["fields"].get("customfield_10037"),
            "labels": issue["fields"].get("labels") or [],
            "priority": (issue["fields"].get("priority") or {}).get("name", "Medium"),
        } for issue in issues]
    except Exception:
        return []


def fetch_confluence_page(auth, page_id):
    try:
        url = f"{BASE_URL}/wiki/rest/api/content/{page_id}?expand=body.storage"
        response = requests.get(url, headers={"Authorization": f"Basic {auth}", "Accept": "application/json"}, timeout=60,
            verify=_ssl_verify()
        )
        if not response.ok:
            return None
        data = response.json()
        return {
            "id": data["id"],
            "title": data["title"],
            "content": strip_html(data.get("body", {}).get("storage", {}).get("value", "")),
        }
    except Exception:
        return None


def fetch_confluence_page_by_url(auth, page_url):
    id_match = re.search(r"/pages/(\d+)", page_url)
    if id_match:
        return fetch_confluence_page(auth, id_match.group(1))

    title_match = re.search(r"/display/[^/]+/(.+?)(?:\?|$)", page_url)
    if title_match:
        title = unquote(title_match.group(1).replace("+", " "))
        try:
            url = f"{BASE_URL}/wiki/rest/api/content?title={quote(title)}&expand=body.storage&limit=1"
            response = requests.get(url, headers={"Authorization": f"Basic {auth}", "Accept": "application/json"}, timeout=60,
                verify=_ssl_verify()
            )
            if not response.ok:
                return None
            results = response.json().get("results", [])
            if not results:
                return None
            r = results[0]
            return {
                "id": r["id"],
                "title": r["title"],
                "content": strip_html(r.get("body", {}).get("storage", {}).get("value", "")),
            }
        except Exception:
            return None
    return None


def build_requirements_text(jira_issues, child_issues, confluence_pages, feature_hint):
    sections = []

    if feature_hint:
        sections.append(f"## Feature Hint\n{feature_hint}")

    if jira_issues:
        sections.append("## JIRA Issues\n")
        for issue in jira_issues:
            lines = [
                f"### {issue['key']}: {issue['summary']}",
                f"Type: {issue['issueType']} | Status: {issue['status']} | Priority: {issue['priority']}",
            ]
            if issue.get("labels"):
                lines.append(f"Labels: {', '.join(issue['labels'])}")
            if issue.get("fixVersions"):
                lines.append(f"Fix Versions: {', '.join(issue['fixVersions'])}")
            if issue.get("team"):
                lines.append(f"Team: {issue['team']}")
            if issue.get("description"):
                lines.append(f"\nDescription:\n{issue['description']}")
            if issue.get("acceptanceCriteria"):
                lines.append(f"\nAcceptance Criteria:\n{issue['acceptanceCriteria']}")
            sections.append("\n".join(lines))

    if child_issues:
        sections.append("## Child Issues / Stories\n")
        for issue in child_issues:
            lines = [
                f"### {issue['key']}: {issue['summary']}",
                f"Type: {issue['issueType']} | Status: {issue['status']} | Priority: {issue['priority']}",
            ]
            if issue.get("description"):
                lines.append(f"\nDescription:\n{issue['description']}")
            if issue.get("acceptanceCriteria"):
                lines.append(f"\nAcceptance Criteria:\n{issue['acceptanceCriteria']}")
            sections.append("\n".join(lines))

    if confluence_pages:
        sections.append("## Confluence Design Documents\n")
        for page in confluence_pages:
            sections.append(f"### {page['title']} (ID: {page['id']})\n{page['content']}")

    return "\n\n---\n\n".join(sections)


def main():
    args = sys.argv[1:]
    jira_keys_raw = get_arg(args, "--jiraKeys")
    confluence_ids_raw = get_arg(args, "--confluenceIds")
    confluence_urls_raw = get_arg(args, "--confluenceUrls")
    feature_hint = get_arg(args, "--featureHint")

    if not jira_keys_raw and not confluence_ids_raw and not confluence_urls_raw:
        print("Usage: python context_aggregator.py --jiraKeys <keys> [--confluenceIds <ids>] [--confluenceUrls <urls>] [--featureHint <text>]", file=sys.stderr)
        sys.exit(1)

    auth = get_basic_auth()
    jira_keys = [k.strip() for k in jira_keys_raw.split(",") if k.strip()] if jira_keys_raw else []
    confluence_ids = [k.strip() for k in confluence_ids_raw.split(",") if k.strip()] if confluence_ids_raw else []
    confluence_urls = [k.strip() for k in confluence_urls_raw.split(",") if k.strip()] if confluence_urls_raw else []

    jira_issues, child_issues, failed_jira = [], [], []
    for key in jira_keys:
        issue = fetch_jira_issue(auth, key)
        if issue:
            jira_issues.append(issue)
            if issue["issueType"] == "Epic":
                child_issues.extend(fetch_child_issues(auth, key))
        else:
            failed_jira.append(key)

    confluence_pages, failed_confluence = [], []
    for page_id in confluence_ids:
        page = fetch_confluence_page(auth, page_id)
        if page:
            confluence_pages.append(page)
        else:
            failed_confluence.append(page_id)

    for url in confluence_urls:
        page = fetch_confluence_page_by_url(auth, url)
        if page:
            confluence_pages.append(page)
        else:
            failed_confluence.append(url)

    requirements_text = build_requirements_text(jira_issues, child_issues, confluence_pages, feature_hint)

    issue_metadata = {
        issue["key"]: {
            "team": issue.get("team", ""),
            "fixVersions": issue.get("fixVersions", []),
            "priority": issue.get("priority", ""),
            "issueType": issue.get("issueType", ""),
            "status": issue.get("status", ""),
        }
        for issue in jira_issues
    }

    print(json.dumps({
        "success": True,
        "sources": {
            "jiraIssues": len(jira_issues),
            "childIssues": len(child_issues),
            "confluencePages": len(confluence_pages),
            "failedJira": failed_jira,
            "failedConfluence": failed_confluence,
        },
        "issueMetadata": issue_metadata,
        "requirementsText": requirements_text,
    }, indent=2))


if __name__ == "__main__":
    main()
