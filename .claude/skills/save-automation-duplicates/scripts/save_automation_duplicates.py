#!/usr/bin/env python3
"""
Persist the automation agent's within-group duplicate (merge candidate)
analysis to automation-duplicates.toon.

Reads automation-groups.toon for the same run to obtain totals and the
Mode A not-found count (carried through so the report can render its
one-line note - no ID list, count only, per design).

Input JSON (via --input file or stdin):
{
  "duplicateGroups": [
    {
      "name": "<functional group name>",
      "sets": [
        {
          "stepDiff": <0, 1, or 2>,
          "criteria": "<what differs>",
          "mergeRationale": "<one-line, content-based explanation>",
          "suggestedName": "<optional merged-test name>",
          "tests": [ { "id": "<optional - a Jira ID if one was found for this test, in a comment/tag/doc-block/filename/describe title; blank if none was found>", "testName": "...",
                       "stepCount": <agent-decided count from actual content>,
                       "automationFilePath": "<file:line>" } ]
        }
      ]
    }
  ]
}

Writes: test-cases-optimizer-work/automation-agent-work/automation-duplicates.toon
"""

import sys
import os
import json
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'optimizer-shared-library', 'scripts'))
import toon_io  # noqa: E402
import report_paths  # noqa: E402
from content_checks import find_unfilled_placeholder, uniform_stats_signature  # noqa: E402


def _read_payload():
    parser = argparse.ArgumentParser(description="Persist the automation agent's within-group duplicate analysis to a TOON file.")
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

    groups_file = report_paths.automation_groups_path()
    if not os.path.isfile(groups_file):
        print(json.dumps({"success": False,
                          "error": "automation-groups.toon not found. Run save-automation-groups first."}, indent=2))
        sys.exit(1)
    with open(groups_file, "r", encoding="utf-8") as f:
        groups_doc = toon_io.loads(f.read())
    gmeta = groups_doc.get("meta", {})

    # automation-groups.toon already has the correctly-resolved id for every
    # test (from scan-automation-repo's comment/tag/doc-block/filename
    # extraction) - it is the authoritative source, not the agent's own
    # memory of it while writing this payload. Backfill from here whenever
    # the agent leaves id blank, rather than trusting it was carried forward
    # correctly - this has been observed to silently drop real IDs that were
    # actually found and already sitting in this exact file.
    id_by_test = {}
    for g in groups_doc.get("groups", []):
        for t in g.get("tests", []):
            if t.get("id"):
                key = (t.get("testName") or "", f"{t.get('filePath') or ''}:{t.get('lineNo') or ''}")
                id_by_test[key] = t["id"]

    dup_groups_in = payload.get("duplicateGroups") or []
    invalid = []
    out_groups = []
    for g in sorted(dup_groups_in, key=lambda x: (x.get("name") or "").lower()):
        sets_out = []
        for s in (g.get("sets") or []):
            tests = s.get("tests") or []
            if len(tests) < 2 or len(tests) > 3:
                invalid.append({"group": g.get("name") or "", "size": len(tests)})
                continue
            step_diff = max(int(s.get("stepDiff") or 0), 0)
            if step_diff > 2:
                invalid.append({"group": g.get("name") or "", "stepDiff": step_diff})
                continue
            if not (s.get("suggestedName") or "").strip():
                invalid.append({"group": g.get("name") or "", "reason": "missing suggestedName - every set must propose a combined test name"})
                continue
            placeholder = (find_unfilled_placeholder(s.get("criteria"))
                           or find_unfilled_placeholder(s.get("mergeRationale"))
                           or find_unfilled_placeholder(s.get("suggestedName")))
            if placeholder:
                invalid.append({"group": g.get("name") or "", "reason": f"unfilled template placeholder {placeholder!r} found in criteria/mergeRationale/suggestedName - write the actual value, not a template variable"})
                continue
            tests_sorted = sorted(tests, key=lambda t: ((t.get("testName") or "").lower(),))
            sets_out.append({
                "stepDiff": step_diff,
                "criteria": s.get("criteria") or "",
                "mergeRationale": s.get("mergeRationale") or "",
                "suggestedName": s.get("suggestedName").strip(),
                "tests": [
                    {"id": t.get("id") or id_by_test.get((t.get("testName") or "", t.get("automationFilePath") or ""), ""),
                     "testName": t.get("testName") or "",
                     "stepCount": int(t.get("stepCount") or 0),
                     "automationFilePath": t.get("automationFilePath") or ""}
                    for t in tests_sorted
                ],
            })
        if sets_out:
            out_groups.append({"name": g.get("name") or "", "sets": sets_out})

    all_sets = [s for g in out_groups for s in g["sets"]]
    anomaly = uniform_stats_signature(all_sets)
    if anomaly:
        print(json.dumps({"success": False, "error": f"Refusing to persist: {anomaly}"}, indent=2))
        sys.exit(1)

    doc = {
        "meta": {
            "mode": gmeta.get("mode") or "",
            "source": gmeta.get("source") or "",
            "key": gmeta.get("key") or "",
            "project": gmeta.get("project") or "",
            "automationRepo": gmeta.get("automationRepo") or "",
            "totalRequested": int(gmeta.get("totalRequested") or 0),
            "matchedCount": int(gmeta.get("matchedCount") or 0),
            "analyzedCount": int(gmeta.get("analyzedCount") or 0),
            "notFoundCount": int(gmeta.get("notFoundCount") or 0),
            "totalGroups": int(gmeta.get("totalGroups") or 0),
            "groupsWithDuplicates": len(out_groups),
            "requestedFolders": list(gmeta.get("requestedFolders") or []),
        },
        "groups": out_groups,
    }

    out_path = report_paths.automation_duplicates_path()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(toon_io.dumps(doc))

    mergeable_sets = sum(len(g["sets"]) for g in out_groups)
    print(json.dumps({
        "success": True,
        "automationDuplicatesToonPath": os.path.abspath(out_path),
        "groupsWithDuplicates": len(out_groups),
        "mergeableSets": mergeable_sets,
        "notFoundCount": int(gmeta.get("notFoundCount") or 0),
        "invalidSets": invalid,
    }, indent=2))


if __name__ == "__main__":
    main()
