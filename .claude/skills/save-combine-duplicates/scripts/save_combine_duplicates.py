#!/usr/bin/env python3
"""
Persist the cross-agent combine analysis to combine-duplicates.toon.

The agent has already: (1) matched at most one manual functional group to at
most one automation functional group per side (semantic judgment, not string
equality), and (2) compared step content across the two groups' tests to
find sets that differ by at most two steps, max three tests, mixing manual
and automation tests.

Matching happens against the GROUPS files (manual-groups.toon /
automation-groups.toon), not the DUPLICATES files - a test that wasn't
flagged as a within-side duplicate can still be a good combine candidate
with a test on the other side, so the full group membership is needed, not
just the subset each agent already flagged internally.

Whichever agent ends up performing this comparison reuses its OWN side's
content for free (still cached in its own context from its own Step 3/4)
but must fetch the OTHER side's content fresh - manual and automation run
as two separate, memory-isolated agents, so neither one has the other's
cached content available:
  - The manual/Jira side is always reachable directly via fetch-test-content,
    keyed by the test keys read from manual-groups.toon - no re-run of any
    Jira ID discovery, no lifecycle concern.
  - The automation side requires the repo to be on disk. If the automation
    agent is the one performing this step, its own clone is still present
    (it deliberately delays cleanup until after this check). If the MANUAL
    agent ends up performing this step, the automation agent's clone is
    already gone (it always cleans up before stopping when it isn't the
    owner) - the manual agent must re-clone the repo itself (URL read from
    run-manifest.toon), fetch only the specific files referenced in
    automation-groups.toon's matched group, then delete that temporary
    clone again immediately after this script runs.
Either way the fetch is narrow (only the specific tests in matched groups),
not a full re-scan.

Before calling this script, the agent is expected to have already skipped
any test whose id/key is already claimed in manual-duplicates.toon or
automation-duplicates.toon while building the matched-group comparison -
there's no point proposing (or spending a fetch on) a test that already has
an outcome elsewhere. This script does NOT itself re-check that, though -
it only validates the STRUCTURE of each proposed set (2-3 tests,
stepDiff <= 2, suggestedName present). The authoritative cross-agent
exclusivity check - catching anything that slips through anyway, such as
two different matched group pairs both grabbing the same free test - runs
once, separately, in validate-cross-agent-duplicates, AFTER this script
writes combine-duplicates.toon and both other duplicates files already
exist - see that skill for the priority rule (automation > combine >
manual) and the set-size revalidation that follows removing a conflicting
test.

## Who runs this script

There is no third, dedicated orchestrator agent - whichever of the two
agents (manual or automation) finishes its own work and finds the OTHER
side's duplicates file already on disk is the one that runs this script,
then validate-cross-agent-duplicates, then generate-combined-report. If the
other file isn't there yet, that agent just reports its own completion and
stops; the other agent will run this sequence when it finishes and sees
both files present. Since only one of the two can ever be the one to
observe both files already existing, this avoids a collision without
needing a third agent. Even in the unlikely case both attempted it, the
scripts are deterministic given the same TOON inputs - re-running is
wasteful, not harmful.

Input JSON (via --input file or stdin):
{
  "combineGroups": [
    {
      "name": "<paired group label, e.g. 'ACD (manual: ACD Flows / automation: ACD)'>",
      "sets": [
        {
          "stepDiff": <0, 1, or 2>,
          "criteria": "<what differs>",
          "mergeRationale": "<one-line, content-based explanation>",
          "suggestedName": "<optional>",
          "tests": [ { "source": "manual"|"automation", "id": "<key, blank if Mode B automation side>",
                       "label": "<summary or testName>", "stepCount": <int>,
                       "origin": "<field|description|file:line>" } ]
        }
      ]
    }
  ]
}

Writes: test-cases-optimizer-work/combine-duplicates.toon
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
    parser = argparse.ArgumentParser(description="Persist the cross-agent combine duplicate analysis to a TOON file.")
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

    combine_groups_in = payload.get("combineGroups") or []
    invalid = []
    out_groups = []
    for g in sorted(combine_groups_in, key=lambda x: (x.get("name") or "").lower()):
        sets_out = []
        for s in (g.get("sets") or []):
            tests = [t for t in (s.get("tests") or [])]
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
            tests_sorted = sorted(tests, key=lambda t: ((t.get("label") or "").lower(),))
            sets_out.append({
                "stepDiff": step_diff,
                "criteria": s.get("criteria") or "",
                "mergeRationale": s.get("mergeRationale") or "",
                "suggestedName": s.get("suggestedName").strip(),
                "tests": [
                    {"source": t.get("source") or "", "id": t.get("id") or "",
                     "label": t.get("label") or "", "stepCount": int(t.get("stepCount") or 0),
                     "origin": t.get("origin") or ""}
                    for t in tests_sorted
                ],
            })
        if sets_out:
            out_groups.append({"name": g.get("name") or "", "sets": sets_out})

    doc = {
        "meta": {
            "totalGroups": len(out_groups),
        },
        "groups": out_groups,
    }

    out_path = report_paths.combine_duplicates_path()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(toon_io.dumps(doc))

    mergeable_sets = sum(len(g["sets"]) for g in out_groups)
    print(json.dumps({
        "success": True,
        "combineDuplicatesToonPath": os.path.abspath(out_path),
        "totalGroups": len(out_groups),
        "mergeableSets": mergeable_sets,
        "invalidSets": invalid,
    }, indent=2))


if __name__ == "__main__":
    main()
