#!/usr/bin/env python3
"""
Fetch full content for one or more Xray test cases: summary, test type,
description text, and structured steps. Called exactly once per candidate -
the caller (agent) caches the result for both grouping and
duplicate-detection, never re-fetching.

testType is kept because the manual agent's fallback path (no reliable JQL
filter) classifies it client-side from this same fetch. labels/status/parent
Epic were removed - fetched on every candidate but never read by any grouping,
duplicate-detection, or report code (probe-source-convention samples those
fields separately, only 5 times per run, for a different purpose).

READ-ONLY: only issues Xray GraphQL queries; never modifies anything.

Usage:
  python fetch_test_content.py <TEST-KEY>
  python fetch_test_content.py <TEST-KEY> <TEST-KEY> <TEST-KEY>

Required Environment Variables:
  XRAY_CLIENT_ID, XRAY_CLIENT_SECRET
"""

import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'optimizer-shared-library', 'scripts'))
from xray_client import xray_graphql  # noqa: E402

# Keys are queried in modest batches via getTests(jql: "key in (...)").
BATCH_SIZE = 50

QUERY = """query($jql: String!, $limit: Int!) {
    getTests(jql: $jql, limit: $limit) {
        total
        results {
            issueId
            jira(fields: ["key", "summary", "description"])
            testType { name }
            steps {
                id
                action
                data
                result
            }
        }
    }
}"""


def _description_text(jira):
    """Return the description as readable text. Jira Cloud returns ADF (a nested
    dict); flatten any text nodes. Falls back to a plain string if that's what
    comes back."""
    desc = jira.get("description")
    if desc is None:
        return ""
    if isinstance(desc, str):
        return desc.strip()

    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text" and isinstance(node.get("text"), str):
                out.append(node["text"])
            for child in node.get("content") or []:
                walk(child)
            # Treat block-level nodes as line breaks so step lists stay separated.
            if node.get("type") in ("paragraph", "listItem", "heading", "tableRow"):
                out.append("\n")
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(desc)
    return "".join(out).strip()


def shape(t):
    jira = t.get("jira") or {}
    steps = [
        {
            "action": (s.get("action") or "").strip(),
            "data": (s.get("data") or "").strip(),
            "result": (s.get("result") or "").strip(),
        }
        for s in (t.get("steps") or [])
    ]
    return {
        "issueId": t.get("issueId"),
        "key": jira.get("key"),
        "summary": jira.get("summary"),
        "testType": (t.get("testType") or {}).get("name"),
        "description": _description_text(jira),
        "stepCount": len(steps),
        "steps": steps,
    }


def main():
    keys = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not keys:
        print("Usage: python fetch_test_content.py <TEST-KEY> [TEST-KEY ...]", file=sys.stderr)
        sys.exit(1)

    try:
        all_tests = []
        for i in range(0, len(keys), BATCH_SIZE):
            batch = keys[i:i + BATCH_SIZE]
            jql = "key in (" + ", ".join(batch) + ")"
            data = xray_graphql(QUERY, {"jql": jql, "limit": len(batch)})
            results = (data or {}).get("getTests", {}).get("results", [])
            all_tests.extend(shape(t) for t in results)

        found_keys = {t["key"] for t in all_tests}
        missing = [k for k in keys if k not in found_keys]

        print(json.dumps({
            "success": True,
            "requested": len(keys),
            "returned": len(all_tests),
            "missing": missing,
            "tests": all_tests,
        }, indent=2))
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()