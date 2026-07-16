#!/usr/bin/env python3
"""
Create the test-cases-optimizer-work/ folder tree and record/merge this run's
identity into run-manifest.toon, so later stages (cross-agent combine, report
generation) don't need the source identity re-passed by hand.

Safe to call once per agent (manual and automation each call it independently
with their own --agent value); merges into the existing manifest rather than
overwriting it.

READ-ONLY with respect to Jira/Xray/git - only creates local folders/files
under test-cases-optimizer-work/.

Usage:
  python init_work_folder.py --agent manual --source plan --key PROJ-1234
  python init_work_folder.py --agent manual --source repository --project PROJ
  python init_work_folder.py --agent automation --source execution --key PROJ-1234 --automation-repo <git-url>
  python init_work_folder.py --agent automation --source execution --key PROJ-1234 --skipped

--skipped (automation agent only): the user declined automation-side checking
when asked. Records automation.skipped=true in run-manifest.toon so the
manual agent knows not to wait for automation-duplicates.toon (it will never
be created) and should generate a manual-only report instead once it's done.
"""

import sys
import os
import argparse
import json

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'optimizer-shared-library', 'scripts'))
import toon_io  # noqa: E402
import report_paths  # noqa: E402
from jira_key_checks import invalid_issue_key_reason, invalid_project_key_reason  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Initialize the shared test-cases-optimizer-work folder (read-only wrt Jira/Xray).")
    parser.add_argument("--agent", required=True, choices=["manual", "automation"])
    parser.add_argument("--source", required=True, choices=["plan", "testset", "execution", "repository"])
    parser.add_argument("--key", help="Jira key of the Test Plan/Set/Execution")
    parser.add_argument("--project", help="Jira project key (source=repository)")
    parser.add_argument("--automation-repo", help="Git URL of the automation repo (automation agent only)")
    parser.add_argument("--skipped", action="store_true", help="Automation agent only: user declined automation-side checking")
    args = parser.parse_args()

    reason = invalid_issue_key_reason(args.key) or invalid_project_key_reason(args.project)
    if reason:
        print(json.dumps({"success": False, "error": reason}, indent=2))
        sys.exit(1)

    manifest_path = report_paths.run_manifest_path()
    manifest = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = toon_io.loads(f.read())
        except Exception:
            manifest = {}

    entry = {"source": args.source}
    if args.key:
        entry["key"] = args.key
    if args.project:
        entry["project"] = args.project
    if args.automation_repo:
        entry["automationRepo"] = args.automation_repo
    if args.skipped:
        entry["skipped"] = True

    manifest[args.agent] = entry

    with open(manifest_path, "w", encoding="utf-8") as f:
        f.write(toon_io.dumps(manifest))

    print(json.dumps({
        "success": True,
        "workRoot": report_paths.work_root(),
        "manualDir": report_paths.manual_dir(),
        "automationDir": report_paths.automation_dir(),
        "runManifestPath": manifest_path,
    }, indent=2))


if __name__ == "__main__":
    main()
