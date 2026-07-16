#!/usr/bin/env python3
"""
Persist the automation agent's functional grouping to automation-groups.toon.

Handles BOTH modes. The agent only ever has to reason about folder NAMES (a
small set) plus a small residual of tests that didn't fit a functional
folder - the actual per-test list for each folder-based group is resolved
here in Python from the already-cached scan data, never round-tripped
through the agent's context.

Mode A (bounded source): folder-based groups are resolved against
`matchedPool` (the output of match-automation-tests-by-id) - every test
carries its Jira ID. `totalRequested`/`matchedCount`/`notFoundCount` are
carried through for the report's summary counts and the one-line
not-found note.

Mode B (Test Repository): folder-based groups are resolved against the
scan-automation-repo cache directly via `--scan-cache` - no Jira ID list is
ever matched against in this mode, but a real ID the scanner already found
for a test (in a comment, a doc-block, a describe title, or the filename) is
still surfaced as that test's `id`, purely for traceability in the report -
it is not discarded just because there's nothing to match it against.

`fallbackGroups` covers the residual tests that don't sit under a genuinely
functional folder (e.g. deployment-stage folders like SanityTest/Failover)
- the agent supplies these directly since it already read their content to
group them.

Input JSON (via --input file or stdin):
{
  "mode": "A" | "B",
  "totalRequested": <int, Mode A only>,
  "matchedCount": <int, Mode A only>,
  "notFoundCount": <int, Mode A only>,
  "matchedPool": [ {id, testName, folderPath, filePath, lineNo}, ... ]   # Mode A only
  "folderGroups": [ { "name": "<group>", "folderPaths": ["<folder>", ...] } ],
  "fallbackGroups": [ { "name": "<group>", "tests": [ {"id": "<optional>", "testName": "...", "filePath": "...", "lineNo": <int>} ] } ],
  "requestedFolders": ["<folder name>", ...]   # optional, only for a request scoped to specific folder(s), in the order the user named them - lets the report render one section per requested folder instead of one flat list
}

--scan-cache is required when mode=B (to resolve folderGroups' tests) or
whenever fallbackGroups relies on re-deriving anything - Mode A's
folderGroups resolve against matchedPool instead.

Writes: test-cases-optimizer-work/automation-agent-work/automation-groups.toon
"""

import sys
import os
import re
import json
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'optimizer-shared-library', 'scripts'))
import toon_io  # noqa: E402

# A group above this size cannot be fully read and compared in Step 4 without
# resorting to sampling - which has been observed to silently miss real
# duplicates at scale (a 716-test group led to a "0 duplicates" false
# negative). The agent is instructed to split any oversized folder into
# named batches ("<FolderName> (batch 1)", etc.) via fallbackGroups before
# ever submitting here - this constant enforces that in code rather than
# trusting the instruction alone.
MAX_GROUP_SIZE = 110
import report_paths  # noqa: E402


def _read_payload():
    parser = argparse.ArgumentParser(description="Persist the automation agent's functional grouping to a TOON file.")
    parser.add_argument("--input")
    parser.add_argument("--cleanup-input", action="store_true")
    parser.add_argument("--scan-cache", help="Required for mode=B to resolve folderGroups' test lists")
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
    return json.loads(raw), args.scan_cache


def _read_manifest():
    path = report_paths.run_manifest_path()
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            return toon_io.loads(f.read()).get("automation", {})
    return {}


def main():
    try:
        payload, scan_cache_path = _read_payload()
    except (json.JSONDecodeError, FileNotFoundError) as e:
        print(json.dumps({"success": False, "error": f"Could not read input: {e}"}, indent=2))
        sys.exit(1)

    mode = payload.get("mode")
    if mode not in ("A", "B"):
        print(json.dumps({"success": False, "error": "mode must be 'A' or 'B'"}, indent=2))
        sys.exit(1)

    folder_groups_in = payload.get("folderGroups") or []
    fallback_groups_in = payload.get("fallbackGroups") or []

    def _folder_matches(candidate, requested):
        """A requested folder path covers itself AND everything nested under
        it - a folder can legitimately hold test files directly (folderPath
        == requested) as well as functional sub-folders (folderPath starts
        with requested + "/"). Without this, resolving a mid-level folder
        (e.g. a broad product-area folder) would silently drop every test
        sitting in one of its sub-folders and keep only the handful of tests
        directly at that exact level - verified as a real gap, not a
        hypothetical one."""
        return candidate == requested or (candidate or "").startswith(requested.rstrip("/") + "/")

    def _resolve_by_folder(by_folder, folder_paths):
        out = []
        for fp in folder_paths:
            for folder_path, tests in by_folder.items():
                if _folder_matches(folder_path, fp):
                    out.extend(tests)
        return out

    _BATCH_SUFFIX_RE = re.compile(r"\s*\(batch\s+\d+\)\s*$", re.IGNORECASE)

    def _requested_folder_match(folder_path, requested_folders):
        """Longest requested-folder prefix that actually contains folder_path -
        the real, deterministic signal for which section a group belongs in,
        instead of trusting whatever name string was typed for the group."""
        best = None
        for rf in requested_folders:
            rf_norm = (rf or "").rstrip("/")
            if not rf_norm:
                continue
            if folder_path == rf_norm or (folder_path or "").startswith(rf_norm + "/"):
                if best is None or len(rf_norm) > len(best):
                    best = rf_norm
        return best

    def _corrected_group_name(original_name, folder_path, requested_folders):
        """Rebuild the group name from the real folder its tests live in,
        preserving only a trailing '(batch N)' suffix from whatever name was
        originally given - this is what makes the report's per-scope section
        matching reliable, since it no longer depends on an agent correctly
        reproducing the 'RequestedFolder/LeafName' convention by hand. When
        the matched requested folder already covers the tests directly (no
        deeper leaf beneath it), the name is just the requested folder itself
        (e.g. 'CXOneAutomation/ACD Users', not '.../ACD Users/ACD Users')."""
        if not requested_folders or not folder_path:
            return original_name
        matched = _requested_folder_match(folder_path, requested_folders)
        if not matched:
            return original_name
        remainder = folder_path[len(matched):].lstrip("/")
        base = matched if not remainder else f"{matched}/{remainder}"
        suffix_match = _BATCH_SUFFIX_RE.search(original_name or "")
        suffix = f" {suffix_match.group(0).strip()}" if suffix_match else ""
        return f"{base}{suffix}"

    if mode == "A":
        pool = payload.get("matchedPool") or []
        by_folder = {}
        for t in pool:
            by_folder.setdefault(t.get("folderPath"), []).append(t)

        def resolve(folder_paths):
            return [{"id": t.get("id") or "", "testName": t.get("testName") or "",
                     "filePath": t.get("filePath") or "", "lineNo": t.get("lineNo") or ""}
                    for t in _resolve_by_folder(by_folder, folder_paths)]
    else:
        if not scan_cache_path or not os.path.isfile(scan_cache_path):
            print(json.dumps({"success": False, "error": "--scan-cache is required and must exist for mode=B"}, indent=2))
            sys.exit(1)
        with open(scan_cache_path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        by_folder = {}
        for t in cache.get("tests", []):
            by_folder.setdefault(t.get("folderPath"), []).append(t)

        def resolve(folder_paths):
            # Mode B never matches against a Jira ID list (no bounded source
            # exists to match against), but that's a MATCHING decision - it
            # doesn't mean a real ID the scanner already found (in a
            # comment, a describe title, or the filename) should be thrown
            # away. Surface it purely for traceability in the report.
            return [{"id": t.get("possibleId") or "", "testName": t.get("testName") or "",
                     "filePath": t.get("filePath") or "", "lineNo": t.get("lineNo") or ""}
                    for t in _resolve_by_folder(by_folder, folder_paths)]

    requested_folders = [str(f) for f in (payload.get("requestedFolders") or [])]

    out_groups = []
    for g in folder_groups_in:
        tests = resolve(g.get("folderPaths") or [])
        if tests:
            folder_path = (g.get("folderPaths") or [None])[0]
            out_groups.append({"name": g.get("name") or "", "tests": tests, "_folderPath": folder_path})

    for g in fallback_groups_in:
        tests = [{"id": t.get("id") or "", "testName": t.get("testName") or "",
                  "filePath": t.get("filePath") or "", "lineNo": t.get("lineNo") or ""}
                 for t in (g.get("tests") or [])]
        if tests:
            first_file_path = tests[0].get("filePath") or ""
            folder_path = os.path.dirname(first_file_path) if first_file_path else None
            out_groups.append({"name": g.get("name") or "", "tests": tests, "_folderPath": folder_path})

    # Rebuild each group's name from the real folder its tests live in
    # (deterministic) rather than trusting the name string as typed - this is
    # what makes the report's per-requested-folder sections reliable even if
    # the naming convention wasn't reproduced correctly for this run.
    for g in out_groups:
        g["name"] = _corrected_group_name(g.get("name"), g.pop("_folderPath", None), requested_folders)

    oversized = [{"group": g["name"], "size": len(g["tests"])} for g in out_groups if len(g["tests"]) > MAX_GROUP_SIZE]
    if oversized:
        print(json.dumps({
            "success": False,
            "error": (
                f"Refusing to persist - {len(oversized)} group(s) exceed the {MAX_GROUP_SIZE}-test cap that Step 4 "
                f"can fully read and compare without sampling. Split each oversized group into batches of at most "
                f"100 by test-name similarity (name it '<FolderName> (batch 1)', '<FolderName> (batch 2)', etc., "
                f"zero-padded) and resubmit each batch via fallbackGroups instead of folderGroups."
            ),
            "oversizedGroups": oversized,
        }, indent=2))
        sys.exit(1)

    out_groups.sort(key=lambda g: g["name"].lower())
    for g in out_groups:
        g["tests"].sort(key=lambda t: (t["testName"] or "").lower())

    # analyzedCount is the mode-agnostic "how many tests are actually being
    # analyzed" figure the report needs: Mode A uses matchedCount (tests found
    # in the repo out of the requested Jira ID list); Mode B has no Jira ID
    # list at all, so it's simply every test the scan grouped.
    if mode == "A":
        analyzed_count = int(payload.get("matchedCount") or 0)
    else:
        analyzed_count = sum(len(g["tests"]) for g in out_groups)

    manifest = _read_manifest()
    doc = {
        "meta": {
            "mode": mode,
            "source": manifest.get("source") or "",
            "key": manifest.get("key") or "",
            "project": manifest.get("project") or "",
            "automationRepo": manifest.get("automationRepo") or "",
            "totalRequested": int(payload.get("totalRequested") or 0),
            "matchedCount": int(payload.get("matchedCount") or 0),
            "analyzedCount": analyzed_count,
            "notFoundCount": int(payload.get("notFoundCount") or 0),
            "totalGroups": len(out_groups),
            "requestedFolders": requested_folders,
        },
        "groups": out_groups,
    }

    out_path = report_paths.automation_groups_path()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(toon_io.dumps(doc))

    print(json.dumps({
        "success": True,
        "automationGroupsToonPath": os.path.abspath(out_path),
        "mode": mode,
        "totalGroups": len(out_groups),
        "notFoundCount": int(payload.get("notFoundCount") or 0),
    }, indent=2))


if __name__ == "__main__":
    main()
