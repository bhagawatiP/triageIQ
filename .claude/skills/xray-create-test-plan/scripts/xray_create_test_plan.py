#!/usr/bin/env python3
"""
Create a new Xray Test Plan issue in Jira via the Xray Cloud GraphQL API.

Usage: python xray_create_test_plan.py --project <KEY> --summary "<text>" [options]
Options:
  --description "<text>"
  --testIssueIds "<id1>,<id2>,..."   Comma-separated internal Xray test issue IDs
  --epicKey "<JIRA-KEY>"             Link the test plan to a Jira Epic
  --priority \"<NAME>\"               Jira priority name (e.g. P1, P2, P3)
  --fix-version \"<NAME>\"            Fix version name (e.g. 26.2)
  --team \"<NAME>\"                   Team Name for customfield_10098 (e.g. CAA, Mavericks)

Required Environment Variables:
  XRAY_CLIENT_ID, XRAY_CLIENT_SECRET
  CONFLUENCE_USERNAME, CONFLUENCE_TOKEN  (only if --epicKey is used)
"""

import os
import sys
import json
import base64
import requests
import urllib3
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'atlassian-api-clients', 'scripts'))
from xray_client import xray_graphql

def _ssl_verify():
    val = os.environ.get("SSL_VERIFY", "true").strip().lower()
    if val in ("false", "0", "no"):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return False
    return True



def get_arg(args, flag):
    try:
        idx = args.index(flag)
        return args[idx + 1] if idx + 1 < len(args) else None
    except ValueError:
        return None


def link_epic(test_plan_key, epic_key):
    base_url = os.environ.get("JIRA_BASE_URL", "https://nice-ce-cxone-prod.atlassian.net")
    username = os.environ.get("CONFLUENCE_USERNAME") or os.environ.get("JIRA_USERNAME")
    token = os.environ.get("CONFLUENCE_TOKEN") or os.environ.get("JIRA_TOKEN")
    if not username or not token:
        return "Skipped epic link: CONFLUENCE_USERNAME/CONFLUENCE_TOKEN not set"

    auth = base64.b64encode(f"{username}:{token}".encode()).decode()
    headers = {"Content-Type": "application/json", "Authorization": f"Basic {auth}"}
    try:
        response = requests.put(
            f"{base_url}/rest/api/2/issue/{test_plan_key}",
            json={"fields": {"customfield_10014": epic_key}},
            headers=headers,
            timeout=60,
            verify=_ssl_verify()
        )
        if response.ok:
            return None
        # customfield_10014 only works when the linked issue type is "Epic".
        # Fall back to a Jira "Test" issue link which works for any issue type.
        if response.status_code == 400:
            fallback = requests.post(
                f"{base_url}/rest/api/2/issueLink",
                json={
                    "type": {"name": "Test"},
                    "inwardIssue": {"key": epic_key},
                    "outwardIssue": {"key": test_plan_key},
                },
                headers=headers,
                timeout=60,
                verify=_ssl_verify()
            )
            if fallback.ok:
                return None
            return f"Epic link fallback failed (HTTP {fallback.status_code}): {fallback.text}"
        return f"Epic link failed (HTTP {response.status_code}): {response.text}"
    except Exception as e:
        return f"Epic link error: {e}"


def main():
    args = sys.argv[1:]
    project_key = get_arg(args, "--project")
    summary = get_arg(args, "--summary")

    if not project_key or not summary:
        print('Usage: python xray_create_test_plan.py --project <KEY> --summary "<text>" [--description "<text>"] [--testIssueIds "id1,id2"] [--epicKey JIRA-KEY]', file=sys.stderr)
        sys.exit(1)

    description = get_arg(args, "--description")
    test_issue_ids_raw = get_arg(args, "--testIssueIds")
    epic_key = get_arg(args, "--epicKey")
    priority = get_arg(args, "--priority")
    fix_version = get_arg(args, "--fix-version")
    team = get_arg(args, "--team")

    test_issue_ids = [i.strip() for i in test_issue_ids_raw.split(",") if i.strip()] if test_issue_ids_raw else None

    try:
        jira_fields = {
            "summary": summary,
            "project": {"key": project_key},
            "labels": ["mcp-auto-generated"],
        }
        if description:
            jira_fields["description"] = description
        if priority:
            jira_fields["priority"] = {"name": priority}
        if fix_version:
            jira_fields["fixVersions"] = [{"name": fix_version}]
        if team:
            jira_fields["customfield_10098"] = {"value": team}

        variables = {
            "jira": {"fields": jira_fields},
            "testIssueIds": test_issue_ids if test_issue_ids else None,
        }

        data = xray_graphql(
            """mutation CreateTestPlan($jira: JSON!, $testIssueIds: [String]) {
                createTestPlan(jira: $jira, testIssueIds: $testIssueIds) {
                    testPlan {
                        issueId
                        jira(fields: ["key", "summary", "labels"])
                    }
                    warnings
                }
            }""",
            variables,
        )

        result = (data or {}).get("createTestPlan", {})
        if not result.get("testPlan"):
            print(json.dumps({"success": False, "error": "No test plan returned from API", "raw": data}, indent=2))
            sys.exit(1)

        plan = result["testPlan"]
        jira = plan.get("jira") or {}
        output = {
            "success": True,
            "issueId": plan["issueId"],
            "key": jira.get("key"),
            "summary": jira.get("summary", summary),
            "labels": jira.get("labels", ["mcp-auto-generated"]),
            "testsLinked": len(test_issue_ids) if test_issue_ids else 0,
            "warnings": result.get("warnings") or [],
        }

        if epic_key and jira.get("key"):
            link_error = link_epic(jira["key"], epic_key)
            if link_error:
                output["epicLinkWarning"] = link_error
            else:
                output["epicLinked"] = epic_key

        print(json.dumps(output, indent=2))
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
