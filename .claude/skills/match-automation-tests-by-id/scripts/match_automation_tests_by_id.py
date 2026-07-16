#!/usr/bin/env python3
"""
Mode A only (bounded source: Test Set/Plan/Execution): intersect the Jira ID
list (from read-all-source-tests) against the scan-automation-repo cache's
`possibleId` values. One bulk set-intersection in Python - no per-ID lookups,
no Method Name fallback (dropped by design - if the ID itself isn't found in
the repo, the test is simply not in the automation results).

Matched tests carry their Jira ID (the report always shows ID + test name in
Mode A). Not-found tests are reported as a COUNT ONLY - never listed by ID -
per the report design.

Usage:
  python match_automation_tests_by_id.py --scan-cache <path> --ids-file <path to a JSON array of Jira IDs>
"""

import sys
import os
import json
import argparse


def main():
    parser = argparse.ArgumentParser(description="Intersect Jira IDs against the scan-automation-repo cache (local, no network).")
    parser.add_argument("--scan-cache", required=True)
    parser.add_argument("--ids-file", required=True, help="Path to a JSON file containing an array of Jira test IDs")
    args = parser.parse_args()

    if not os.path.isfile(args.scan_cache):
        print(json.dumps({"success": False, "error": f"scan cache not found: {args.scan_cache}"}, indent=2))
        sys.exit(1)
    if not os.path.isfile(args.ids_file):
        print(json.dumps({"success": False, "error": f"ids file not found: {args.ids_file}"}, indent=2))
        sys.exit(1)

    with open(args.scan_cache, "r", encoding="utf-8") as f:
        cache = json.load(f)
    with open(args.ids_file, "r", encoding="utf-8") as f:
        requested_ids = json.load(f)

    by_id = {}
    for t in cache.get("tests", []):
        pid = t.get("possibleId")
        if pid:
            by_id.setdefault(pid, []).append(t)

    matched = []
    not_found = 0
    for jira_id in requested_ids:
        hits = by_id.get(jira_id)
        if hits:
            # If the same ID appears in more than one place, take the first -
            # ambiguity here is a repo-authoring issue, not this script's to resolve.
            t = hits[0]
            matched.append({
                "id": jira_id,
                "testName": t.get("testName"),
                "folderPath": t.get("folderPath"),
                "filePath": t.get("filePath"),
                "lineNo": t.get("lineNo"),
            })
        else:
            not_found += 1

    print(json.dumps({
        "success": True,
        "totalRequested": len(requested_ids),
        "matchedCount": len(matched),
        "notFoundCount": not_found,
        "matched": matched,
    }, indent=2))


if __name__ == "__main__":
    main()
