#!/usr/bin/env python3
"""
Read every test (key + summary) from a Test Plan/Set/Execution, with NO
filtering at all - no Removed exclusion, no testType exclusion. Used by the
automation agent in Mode A (bounded source) to get the full ID list to search
for in the automation repository, and by the manual agent's fallback path
when probe-source-convention found no reliable JQL filter for that project.

Not used for source=repository - Mode B (Test Repository) never starts from
a Jira ID list; it scans the automation repo directly instead
(see scan-automation-repo).

READ-ONLY: only issues Xray GraphQL queries.

Usage:
  python read_all_source_tests.py --source plan --key PROJ-1234
  python read_all_source_tests.py --source execution --key PROJ-1234

Required Environment Variables:
  XRAY_CLIENT_ID, XRAY_CLIENT_SECRET
"""

import sys
import os
import json
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'optimizer-shared-library', 'scripts'))
from xray_client import xray_graphql  # noqa: E402
import jql_builder  # noqa: E402
from jira_key_checks import invalid_issue_key_reason  # noqa: E402

PAGE_SIZE = 100

QUERY = """query($jql: String!, $limit: Int!, $start: Int!) {
    getTests(jql: $jql, limit: $limit, start: $start) {
        total
        results { issueId jira(fields: ["key", "summary"]) }
    }
}"""


def main():
    parser = argparse.ArgumentParser(description="Read every test from a bounded source with no filtering (read-only).")
    parser.add_argument("--source", required=True, choices=["plan", "testset", "execution"])
    parser.add_argument("--key", required=True)
    args = parser.parse_args()

    reason = invalid_issue_key_reason(args.key)
    if reason:
        print(json.dumps({"success": False, "error": reason}, indent=2))
        sys.exit(1)

    try:
        jql = jql_builder.build_unfiltered_source_jql(args.source, args.key, None)
    except ValueError as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)

    try:
        tests = []
        start = 0
        total = None
        while True:
            data = xray_graphql(QUERY, {"jql": jql, "limit": PAGE_SIZE, "start": start})
            block = data.get("getTests", {})
            if total is None:
                total = block.get("total", 0)
            page = block.get("results", [])
            for r in page:
                jira = r.get("jira") or {}
                tests.append({"issueId": r.get("issueId"), "key": jira.get("key"), "summary": jira.get("summary")})
            start += PAGE_SIZE
            if not page or len(tests) >= total:
                break

        print(json.dumps({
            "success": True,
            "source": args.source,
            "key": args.key,
            "total": total,
            "tests": tests,
        }, indent=2))

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
