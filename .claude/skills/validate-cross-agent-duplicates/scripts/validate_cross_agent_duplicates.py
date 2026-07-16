#!/usr/bin/env python3
"""
Final cross-agent validation - the one point where manual-duplicates.toon,
automation-duplicates.toon, and combine-duplicates.toon are all checked
against each other and, if needed, corrected in place. Runs once, after all
three files exist, and before generate-combined-report.

Why this runs separately, as its own last step: the manual and automation
agents build their own duplicate sets independently, in parallel, with no
visibility into each other, and the combine step builds its sets by
comparing the two sides afterward. None of those three steps check whether
the SAME test id ended up claimed in more than one of the three final
artifacts - that cross-check is only possible once all three exist, so it
happens here, as an explicit step, rather than scattered across the earlier
steps.

Priority when the same test id appears in more than one artifact:
  1. automation-duplicates.toon always wins. A test claimed there is left
     untouched, and is removed from combine-duplicates.toon and/or
     manual-duplicates.toon wherever else it appears.
  2. combine-duplicates.toon is second priority. A test claimed there
     (after rule 1 has already been applied) is left untouched there, and
     is removed from manual-duplicates.toon if it also appears there.
  3. manual-duplicates.toon never wins a conflict - it only ever loses
     tests to rules 1 and 2, never the reverse.

After removing a losing test from a set, the set's size is re-validated:
  - A set that had 2 tests and loses one is left with 1 - a 1-test set is
    not a valid merge candidate, so the whole set is dropped.
  - A set that had 3 tests and loses one is left with 2 - still a valid
    set (2 is the minimum), so it is KEPT as a 2-test set.
A group left with zero sets afterward is dropped too.

Rewrites manual-duplicates.toon and combine-duplicates.toon in place with
the corrected groups/sets and recomputed group counts.
automation-duplicates.toon is never modified - it never loses a conflict.

Usage:
  python validate_cross_agent_duplicates.py
"""

import sys
import os
import json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'optimizer-shared-library', 'scripts'))
import toon_io  # noqa: E402
import report_paths  # noqa: E402


def _load(path):
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return toon_io.loads(f.read())


def _ids_in(doc, id_field):
    ids = set()
    if not doc:
        return ids
    for g in doc.get("groups") or []:
        for s in g.get("sets") or []:
            for t in s.get("tests") or []:
                v = t.get(id_field)
                if v:
                    ids.add(v)
    return ids


def _strip_ids(doc, id_field, drop_ids, reason, removed_log):
    """Remove any test whose id_field value is in drop_ids from every set in
    doc. A set left with fewer than 2 tests is dropped entirely; a group
    left with zero sets is dropped entirely. Returns (new_doc, changed)."""
    if not doc or not drop_ids:
        return doc, False
    changed = False
    out_groups = []
    for g in doc.get("groups") or []:
        out_sets = []
        for s in g.get("sets") or []:
            tests = s.get("tests") or []
            kept = []
            for t in tests:
                tid = t.get(id_field)
                if tid and tid in drop_ids:
                    changed = True
                    removed_log.append({"id": tid, "group": g.get("name") or "", "reason": reason})
                else:
                    kept.append(t)
            if len(kept) < 2:
                continue
            out_sets.append({**s, "tests": kept})
        if out_sets:
            out_groups.append({**g, "sets": out_sets})
    return {**doc, "groups": out_groups}, changed


def main():
    automation_doc = _load(report_paths.automation_duplicates_path())
    manual_doc = _load(report_paths.manual_duplicates_path())
    combine_doc = _load(report_paths.combine_duplicates_path())

    missing = [name for name, doc in (
        ("manual-duplicates.toon", manual_doc),
        ("automation-duplicates.toon", automation_doc),
        ("combine-duplicates.toon", combine_doc),
    ) if doc is None]
    if missing:
        print(json.dumps({
            "success": False,
            "error": f"Missing required file(s): {', '.join(missing)}. Run save-combine-duplicates first; all three artifacts must exist before this validator runs.",
        }, indent=2))
        sys.exit(1)

    removed_log = []

    # Rule 1: automation wins over combine and manual.
    automation_ids = _ids_in(automation_doc, "id")
    combine_doc, combine_changed = _strip_ids(combine_doc, "id", automation_ids, "already in an automation-only duplicate set", removed_log)
    manual_doc, manual_changed_1 = _strip_ids(manual_doc, "key", automation_ids, "already in an automation-only duplicate set", removed_log)

    # Rule 2: combine wins over manual, using combine's ids AFTER rule 1 has
    # already removed anything automation claimed from it.
    combine_ids = _ids_in(combine_doc, "id")
    manual_doc, manual_changed_2 = _strip_ids(manual_doc, "key", combine_ids, "already in a combine duplicate set", removed_log)

    changed = combine_changed or manual_changed_1 or manual_changed_2

    manual_doc = {**manual_doc, "meta": {**(manual_doc.get("meta") or {}), "groupsWithDuplicates": len(manual_doc.get("groups") or [])}}
    combine_doc = {**combine_doc, "meta": {**(combine_doc.get("meta") or {}), "totalGroups": len(combine_doc.get("groups") or [])}}

    with open(report_paths.manual_duplicates_path(), "w", encoding="utf-8") as f:
        f.write(toon_io.dumps(manual_doc))
    with open(report_paths.combine_duplicates_path(), "w", encoding="utf-8") as f:
        f.write(toon_io.dumps(combine_doc))

    print(json.dumps({
        "success": True,
        "changed": changed,
        "removed": removed_log,
        "manualGroupsRemaining": len(manual_doc.get("groups") or []),
        "combineGroupsRemaining": len(combine_doc.get("groups") or []),
    }, indent=2))


if __name__ == "__main__":
    main()
