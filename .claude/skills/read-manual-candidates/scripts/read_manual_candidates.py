#!/usr/bin/env python3
"""
Read the manual agent's candidate tests directly via a JQL-filtered query,
using the filter decided by probe-source-convention.

Always excludes Jira status Removed at the query level. Optionally also
allow-lists/excludes by whichever field the probe step found to be a
reliable manual/automated signal for this project - testType first choice,
falling back to labels or status when testType itself isn't trustworthy -
using the exact value and polarity decided by the probe. This saves the
token cost of ever reading Automated tests' content when a reliable filter
exists on any of these fields.

If --filter-value is omitted (probe found no reliable filter on any field),
only the Removed exclusion is applied - the caller must then classify each
returned candidate's testType client-side via fetch-test-content.

READ-ONLY: only issues Xray GraphQL queries.

Usage:
  python read_manual_candidates.py --source plan --key PROJ-1234 --filter-field testType --filter-value Manual --filter-positive
  python read_manual_candidates.py --source repository --project PROJ --filter-field testType --filter-value "Automated[Generic]"
  python read_manual_candidates.py --source execution --key PROJ-1234 --filter-field labels --filter-value Automated
  python read_manual_candidates.py --source execution --key PROJ-1234   # no filter at all (fallback mode)

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

PAGE_SIZE = 100

QUERY = """query($jql: String!, $limit: Int!, $start: Int!) {
    getTests(jql: $jql, limit: $limit, start: $start) {
        total
        results { issueId jira(fields: ["key", "summary"]) }
    }
}"""


def main():
    parser = argparse.ArgumentParser(description="Read manual candidate tests via a JQL-filtered query (read-only).")
    parser.add_argument("--source", required=True, choices=["plan", "testset", "execution", "repository"])
    parser.add_argument("--key")
    parser.add_argument("--project")
    parser.add_argument("--filter-field", choices=["testType", "labels", "status"], default="testType",
                         help="Which field carries the manual/automated signal for this project, per probe-source-convention (default testType)")
    parser.add_argument("--filter-value", help="Exact value observed via probe-source-convention for --filter-field")
    parser.add_argument("--filter-positive", action="store_true", help="Allow-list this value (field = value) instead of excluding it (field != value)")
    args = parser.parse_args()

    try:
        jql = jql_builder.build_manual_candidates_jql(
            args.source, args.key, args.project,
            filter_field=args.filter_field,
            filter_value=args.filter_value,
            filter_positive=args.filter_positive,
        )
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
            "project": args.project,
            "jqlUsed": jql,
            "candidatesReturned": len(tests),
            "tests": tests,
        }, indent=2))

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
