#!/usr/bin/env python3
"""
Get all tests inside an existing Xray Test Plan via the Xray Cloud GraphQL API.

Usage: python xray_get_testplan_tests.py <TEST-PLAN-KEY> [--limit N]
Example: python xray_get_testplan_tests.py CXREC-107937 --limit 100

Required Environment Variables:
  XRAY_CLIENT_ID, XRAY_CLIENT_SECRET
"""

import sys
import os
import json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'atlassian-api-clients', 'scripts'))
from xray_client import xray_graphql


def main():
    args = sys.argv[1:]
    if not args:
        print("Usage: python xray_get_testplan_tests.py <TEST-PLAN-KEY> [--limit N]", file=sys.stderr)
        sys.exit(1)

    test_plan_key = args[0]
    limit = 50
    if "--limit" in args:
        idx = args.index("--limit")
        if idx + 1 < len(args):
            limit = min(int(args[idx + 1]) or 50, 100)

    try:
        data = xray_graphql(
            """query($jql: String!, $limit: Int!) {
                getTestPlans(jql: $jql, limit: 1) {
                    results {
                        issueId
                        tests(limit: $limit) {
                            total
                            results { issueId testType { name } }
                        }
                        testExecutions(limit: 20) {
                            total
                            results { issueId }
                        }
                    }
                }
            }""",
            {"jql": f"key = {test_plan_key}", "limit": limit},
        )

        results = (data or {}).get("getTestPlans", {}).get("results", [])
        if not results:
            print(json.dumps({"success": False, "testPlanKey": test_plan_key, "error": "Test plan not found"}, indent=2))
            sys.exit(1)

        plan = results[0]
        print(json.dumps({
            "success": True,
            "testPlanKey": test_plan_key,
            "testPlanIssueId": plan["issueId"],
            "testsTotal": plan["tests"]["total"],
            "tests": [{"issueId": t["issueId"], "testType": t["testType"]["name"]} for t in plan["tests"]["results"]],
            "testExecutionsTotal": plan["testExecutions"]["total"],
            "testExecutions": [e["issueId"] for e in plan["testExecutions"]["results"]],
        }, indent=2))
    except Exception as e:
        print(json.dumps({"success": False, "testPlanKey": test_plan_key, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
