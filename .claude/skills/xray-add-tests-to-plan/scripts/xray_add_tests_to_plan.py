#!/usr/bin/env python3
"""
Add tests to an existing Xray Test Plan via the Xray Cloud GraphQL API.

Usage: python xray_add_tests_to_plan.py <TEST-PLAN-KEY> <TEST-KEY-1> <TEST-KEY-2> ...
Example: python xray_add_tests_to_plan.py CXREC-107937 CXREC-107916 CXREC-107917

Required Environment Variables:
  XRAY_CLIENT_ID, XRAY_CLIENT_SECRET
  CONFLUENCE_USERNAME, CONFLUENCE_TOKEN
"""

import sys
import os
import json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'atlassian-api-clients', 'scripts'))
from xray_client import xray_graphql


def resolve_test_plan_id(test_plan_key):
    data = xray_graphql(
        "query($jql: String!) { getTestPlans(jql: $jql, limit: 1) { results { issueId } } }",
        {"jql": f"key = {test_plan_key}"},
    )
    results = (data or {}).get("getTestPlans", {}).get("results", [])
    if not results:
        raise Exception(f"Test plan {test_plan_key} not found in Xray")
    return results[0]["issueId"]


def resolve_test_ids(test_keys):
    resolved = []
    for i in range(0, len(test_keys), 10):
        batch = test_keys[i:i + 10]
        jql = f"key in ({','.join(batch)})"
        data = xray_graphql(
            "query($jql: String!, $limit: Int!) { getTests(jql: $jql, limit: $limit) { results { issueId jira(fields: [\"key\"]) } } }",
            {"jql": jql, "limit": len(batch)},
        )
        for r in (data or {}).get("getTests", {}).get("results", []):
            jira = r.get("jira") or {}
            key = jira.get("key", "") if isinstance(jira, dict) else ""
            resolved.append({"jiraKey": key, "issueId": r["issueId"]})
    return resolved


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print("Usage: python xray_add_tests_to_plan.py <TEST-PLAN-KEY> <TEST-KEY-1> [TEST-KEY-2] ...", file=sys.stderr)
        sys.exit(1)

    test_plan_key = args[0]
    test_keys = args[1:]

    try:
        plan_issue_id = resolve_test_plan_id(test_plan_key)
        resolved_tests = resolve_test_ids(test_keys)
        test_issue_ids = [r["issueId"] for r in resolved_tests]

        if not test_issue_ids:
            print(json.dumps({"success": False, "error": "No test IDs could be resolved from provided keys"}, indent=2))
            sys.exit(1)

        data = xray_graphql(
            """mutation($issueId: String!, $testIssueIds: [String]!) {
                addTestsToTestPlan(issueId: $issueId, testIssueIds: $testIssueIds) {
                    addedTests
                    warning
                }
            }""",
            {"issueId": plan_issue_id, "testIssueIds": test_issue_ids},
        )

        result = (data or {}).get("addTestsToTestPlan", {})
        print(json.dumps({
            "success": True,
            "testPlanKey": test_plan_key,
            "addedCount": len(result.get("addedTests") or []),
            "addedTestIds": result.get("addedTests") or [],
            "warning": result.get("warning"),
            "resolvedTests": [{"key": r["jiraKey"], "issueId": r["issueId"]} for r in resolved_tests],
        }, indent=2))
    except Exception as e:
        print(json.dumps({"success": False, "testPlanKey": test_plan_key, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
