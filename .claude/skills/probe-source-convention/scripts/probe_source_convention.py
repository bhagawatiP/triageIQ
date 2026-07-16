#!/usr/bin/env python3
"""
Probe a source (Test Plan/Set/Execution/Repository) with a small sample of
real tests so the agent can decide how THIS project marks manual vs automated
tests, before any exclusion JQL is built.

Returns each sampled test's testType, status, labels, and full description
text (already flattened from Jira's ADF format) - enough for the agent to
cross-check whether testType is trustworthy (e.g. does an "Automated[...]"
testType actually carry automation-linkage content in its description, such
as Xray's auto-generated Playwright boilerplate, or does it look like plain
hand-written manual steps - a mismatch means testType may be stale for this
project).

Always samples 5 (random spread across the source, not just the first page)
for reliability - a single test is too easy to misjudge as representative of
the whole project's convention.

READ-ONLY: only issues Xray GraphQL queries.

Usage:
  python probe_source_convention.py --source plan --key PROJ-1234
  python probe_source_convention.py --source repository --project PROJ

Required Environment Variables:
  XRAY_CLIENT_ID, XRAY_CLIENT_SECRET
"""

import sys
import os
import json
import argparse
import random

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'optimizer-shared-library', 'scripts'))
from xray_client import xray_graphql  # noqa: E402
import jql_builder  # noqa: E402
from jira_key_checks import invalid_issue_key_reason, invalid_project_key_reason  # noqa: E402

TOTAL_QUERY = """query($jql: String!) { getTests(jql: $jql, limit: 1) { total } }"""

SAMPLE_QUERY = """query($jql: String!, $limit: Int!, $start: Int!) {
    getTests(jql: $jql, limit: $limit, start: $start) {
        results {
            issueId
            jira(fields: ["key", "summary", "status", "labels", "description"])
            testType { name }
        }
    }
}"""


def _status_name(jira):
    st = jira.get("status")
    return st.get("name") if isinstance(st, dict) else st


def _description_text(jira):
    desc = jira.get("description")
    if desc is None:
        return ""
    if isinstance(desc, str):
        return desc
    texts = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text" and "text" in node:
                texts.append(node["text"])
            for child in node.get("content") or []:
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(desc)
    return "\n".join(texts)


def main():
    parser = argparse.ArgumentParser(description="Probe a source for its manual/automated marking convention (read-only).")
    parser.add_argument("--source", required=True, choices=["plan", "testset", "execution", "repository"])
    parser.add_argument("--key")
    parser.add_argument("--project")
    parser.add_argument("--sample", type=int, default=5, help="Number of tests to sample (default 5, for reliability)")
    args = parser.parse_args()

    reason = invalid_issue_key_reason(args.key) or invalid_project_key_reason(args.project)
    if reason:
        print(json.dumps({"success": False, "error": reason}, indent=2))
        sys.exit(1)

    try:
        base_jql = jql_builder.container_scope_jql(args.source, args.key, args.project)
    except ValueError as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)

    try:
        total = xray_graphql(TOTAL_QUERY, {"jql": base_jql})["getTests"]["total"]
        if total == 0:
            print(json.dumps({"success": True, "total": 0, "samples": []}, indent=2))
            return

        sample_size = min(args.sample, total)
        start = random.randint(0, max(0, total - sample_size))
        data = xray_graphql(SAMPLE_QUERY, {"jql": base_jql, "limit": sample_size, "start": start})
        results = data["getTests"]["results"]

        samples = []
        for r in results:
            jira = r.get("jira") or {}
            samples.append({
                "key": jira.get("key"),
                "summary": jira.get("summary"),
                "status": _status_name(jira),
                "labels": jira.get("labels") or [],
                "testType": (r.get("testType") or {}).get("name"),
                "description": _description_text(jira),
            })

        print(json.dumps({"success": True, "total": total, "samples": samples}, indent=2))

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
