#!/usr/bin/env python3
"""
Persist the manual agent's functional grouping to manual-groups.toon.

Removed and Automated tests are never present here - they were excluded
upstream at the JQL level (read-manual-candidates) or listed separately
(list-removed-tests), so this script only ever sees genuine manual candidates.

Input JSON (via --input file or stdin):
{
  "source": "<plan|testset|execution|repository>",
  "key": "<CONTAINER-KEY>",
  "project": "<PROJECT-KEY, for repository>",
  "containerSummary": "<container summary>",
  "testTypeFilterUsed": "<human-readable note on which JQL filter was applied, for transparency>",
  "tests": [ { "key": "<TEST-KEY>", "summary": "<summary>" } ],
  "groups": [ { "name": "<functional group>", "keys": ["<TEST-KEY>", ...] } ]
}

Writes: test-cases-optimizer-work/manual-agent-work/manual-groups.toon
"""

import sys
import os
import json
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'optimizer-shared-library', 'scripts'))
import toon_io  # noqa: E402
import report_paths  # noqa: E402


def _read_payload():
    parser = argparse.ArgumentParser(description="Persist the manual agent's functional grouping to a TOON file.")
    parser.add_argument("--input")
    parser.add_argument("--cleanup-input", action="store_true")
    args = parser.parse_args()
    if args.input:
        with open(args.input, "r", encoding="utf-8") as f:
            raw = f.read()
        if args.cleanup_input:
            try:
                os.remove(args.input)
            except OSError:
                pass
    else:
        raw = sys.stdin.read()
    return json.loads(raw)


def main():
    try:
        payload = _read_payload()
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(json.dumps({"success": False, "error": f"Could not read input: {e}"}, indent=2))
        sys.exit(1)

    tests = payload.get("tests") or []
    groups = payload.get("groups") or []
    if not tests:
        print(json.dumps({"success": False, "error": "Payload contains no tests."}, indent=2))
        sys.exit(1)
    if not groups:
        print(json.dumps({"success": False, "error": "Payload contains no groups."}, indent=2))
        sys.exit(1)

    by_key = {t.get("key"): t for t in tests if t.get("key")}

    out_groups = []
    for g in sorted(groups, key=lambda x: (x.get("name") or "").lower()):
        keys = [k for k in (g.get("keys") or []) if k in by_key]
        keys.sort(key=lambda k: ((by_key[k].get("summary") or "").lower(), k or ""))
        if not keys:
            continue
        out_groups.append({
            "name": g.get("name") or "",
            "tests": [{"key": k, "summary": by_key[k].get("summary") or ""} for k in keys],
        })

    doc = {
        "meta": {
            "source": payload.get("source") or "",
            "key": payload.get("key") or "",
            "project": payload.get("project") or "",
            "containerSummary": payload.get("containerSummary") or "",
            "testTypeFilterUsed": payload.get("testTypeFilterUsed") or "",
            "candidateTestsCount": len(by_key),
            "totalGroups": len(out_groups),
        },
        "groups": out_groups,
    }

    out_path = report_paths.manual_groups_path()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(toon_io.dumps(doc))

    print(json.dumps({
        "success": True,
        "manualGroupsToonPath": os.path.abspath(out_path),
        "candidateTestsCount": len(by_key),
        "totalGroups": len(out_groups),
    }, indent=2))


if __name__ == "__main__":
    main()
