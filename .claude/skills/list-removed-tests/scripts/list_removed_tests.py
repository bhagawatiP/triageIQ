#!/usr/bin/env python3
"""
Collect just the KEYS of every test whose Jira status is Removed, scoped to
the given source, and persist them to removed-tests.toon. No summary, no
content - the report only needs to show these are removed, and this list can
be large (hundreds), so it is kept as cheap as a single key-only paged query.
This is the only place a Removed-tests table is ever built - nothing
automation-related gets this treatment.

READ-ONLY with respect to Jira/Xray: only issues Xray GraphQL queries and
writes the one local TOON artifact below.

Usage:
  python list_removed_tests.py --source plan --key PROJ-1234
  python list_removed_tests.py --source repository --project PROJ

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
import toon_io  # noqa: E402
import report_paths  # noqa: E402

PAGE_SIZE = 100

QUERY = """query($jql: String!, $limit: Int!, $start: Int!) {
    getTests(jql: $jql, limit: $limit, start: $start) {
        total
        results { jira(fields: ["key"]) }
    }
}"""


def main():
    parser = argparse.ArgumentParser(description="Collect Removed test keys for a source (read-only, key-only).")
    parser.add_argument("--source", required=True, choices=["plan", "testset", "execution", "repository"])
    parser.add_argument("--key")
    parser.add_argument("--project")
    args = parser.parse_args()

    try:
        jql = jql_builder.build_removed_ids_jql(args.source, args.key, args.project)
    except ValueError as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)

    try:
        keys = []
        start = 0
        total = None
        while True:
            data = xray_graphql(QUERY, {"jql": jql, "limit": PAGE_SIZE, "start": start})
            block = data.get("getTests", {})
            if total is None:
                total = block.get("total", 0)
            page = block.get("results", [])
            for r in page:
                k = (r.get("jira") or {}).get("key")
                if k:
                    keys.append(k)
            start += PAGE_SIZE
            if not page or len(keys) >= total:
                break

        sorted_keys = sorted(keys)
        doc = {
            "meta": {"source": args.source, "key": args.key or "", "project": args.project or "", "removedCount": len(sorted_keys)},
            "removed": sorted_keys,
        }
        out_path = report_paths.removed_tests_path()
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(toon_io.dumps(doc))

        print(json.dumps({
            "success": True,
            "removedTestsToonPath": os.path.abspath(out_path),
            "removedCount": len(sorted_keys),
        }, indent=2))

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
