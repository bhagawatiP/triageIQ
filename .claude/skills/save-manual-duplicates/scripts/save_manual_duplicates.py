#!/usr/bin/env python3
"""
Persist the manual agent's within-group duplicate (merge candidate) analysis
to manual-duplicates.toon.

Reads manual-groups.toon for the same run to obtain the candidate/group
totals, then writes only the groups that contain duplicate sets. Each set is
2-3 tests that exercise the same flow and differ by at most two steps
(compared on actual step content, never raw step-row count), found strictly
within one functional group.

Input JSON (via --input file or stdin):
{
  "duplicateGroups": [
    {
      "name": "<functional group name>",
      "sets": [
        {
          "stepDiff": <0, 1, or 2>,
          "criteria": "<what differs>",
          "mergeRationale": "<one-line, content-based explanation of why these merge>",
          "suggestedName": "<optional merged-test name>",
          "tests": [ { "key": "<TEST-KEY>", "summary": "<summary>", "stepCount": <number>,
                       "stepSource": "field" | "description" } ]
        }
      ]
    }
  ],
  "noSteps": [ { "key": "<TEST-KEY>", "summary": "<summary>" } ]
}

Writes: test-cases-optimizer-work/manual-agent-work/manual-duplicates.toon
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
    parser = argparse.ArgumentParser(description="Persist the manual agent's within-group duplicate analysis to a TOON file.")
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

    groups_file = report_paths.manual_groups_path()
    if not os.path.isfile(groups_file):
        print(json.dumps({"success": False,
                          "error": "manual-groups.toon not found. Run save-manual-groups first."}, indent=2))
        sys.exit(1)
    with open(groups_file, "r", encoding="utf-8") as f:
        groups_doc = toon_io.loads(f.read())
    gmeta = groups_doc.get("meta", {})
    candidate_count = int(gmeta.get("candidateTestsCount") or 0)
    total_groups = int(gmeta.get("totalGroups") or 0)

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
            tests_sorted = sorted(tests, key=lambda t: ((t.get("summary") or "").lower(), t.get("key") or ""))
            sets_out.append({
                "stepDiff": step_diff,
                "criteria": s.get("criteria") or "",
                "mergeRationale": s.get("mergeRationale") or "",
                "suggestedName": s.get("suggestedName").strip(),
                "tests": [
                    {"key": t.get("key") or "",
                     "summary": t.get("summary") or "",
                     "stepCount": int(t.get("stepCount") or 0),
                     "stepSource": ("description" if t.get("stepSource") == "description" else "field")}
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

    no_steps = [{"key": r.get("key") or "", "summary": r.get("summary") or ""}
                for r in (payload.get("noSteps") or []) if r.get("key")]

    doc = {
        "meta": {
            "source": gmeta.get("source") or "",
            "key": gmeta.get("key") or "",
            "project": gmeta.get("project") or "",
            "containerSummary": gmeta.get("containerSummary") or "",
            "candidateTestsCount": candidate_count,
            "totalGroups": total_groups,
            "groupsWithDuplicates": len(out_groups),
            "noStepsCount": len(no_steps),
        },
        "groups": out_groups,
        "noSteps": no_steps,
    }

    out_path = report_paths.manual_duplicates_path()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(toon_io.dumps(doc))

    mergeable_sets = sum(len(g["sets"]) for g in out_groups)
    print(json.dumps({
        "success": True,
        "manualDuplicatesToonPath": os.path.abspath(out_path),
        "candidateTestsCount": candidate_count,
        "totalGroups": total_groups,
        "groupsWithDuplicates": len(out_groups),
        "mergeableSets": mergeable_sets,
        "noStepsCount": len(no_steps),
        "invalidSets": invalid,
    }, indent=2))


if __name__ == "__main__":
    main()
