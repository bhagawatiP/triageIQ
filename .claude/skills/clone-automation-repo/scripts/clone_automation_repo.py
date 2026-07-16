#!/usr/bin/env python3
"""
Manage the temporary local clone of the automation repo for the duration of a
single run. Never leaves the repo's own working tree - clones to a fresh
folder under the system temp directory, and deletes it on --action cleanup.

Uses a shallow, blob-filtered clone (fast, minimal bandwidth) since only file
paths/contents are needed for scanning, not history.

--action clone also records the new path as automation.clonePath in THIS
work folder's own run-manifest.toon (test-cases-optimizer-work/ at the
current repo root/cwd - see report_paths). --action cleanup-previous reads
that field back and deletes ONLY that specific path, then clears the field -
it never globs or touches any other tco-automation-repo-* folder in the
system temp directory, because another one could belong to a genuinely
concurrent, unrelated run on the same machine and must not be touched.

Run --action cleanup-previous first, before asking the user anything and
before any new clone, at the start of every automation agent run. This
closes a real failure mode: an agent referencing an old clone path it
remembers from earlier in the same conversation instead of asking for a
fresh repo URL and cloning fresh - with that specific old path guaranteed
deleted first, the reference fails loudly (path not found) instead of
silently analyzing stale data from the wrong repo. Scoping the check to this
work folder's own manifest (rather than a global sweep) means it only ever
touches a path THIS work folder's own prior run created.

--action cleanup's --path is optional and defaults to the same recorded
clonePath. If a --path IS given, it must match the recorded path exactly -
the script refuses to delete anything otherwise, rather than risk cleaning
up the wrong folder. This means the clone path and the cleanup path can
never silently diverge.

Usage:
  python clone_automation_repo.py --action cleanup-previous
  python clone_automation_repo.py --action clone --repo-url https://github.com/org/repo.git
  python clone_automation_repo.py --action cleanup [--path <path, optional - defaults to recorded clonePath>]
"""

import sys
import os
import json
import argparse
import subprocess
import shutil
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'optimizer-shared-library', 'scripts'))
import toon_io  # noqa: E402
import report_paths  # noqa: E402


def _load_manifest():
    path = report_paths.run_manifest_path()
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return toon_io.loads(f.read())
        except Exception:
            return {}
    return {}


def _save_manifest(manifest):
    with open(report_paths.run_manifest_path(), "w", encoding="utf-8") as f:
        f.write(toon_io.dumps(manifest))


def _is_safe_clone_path(path):
    return bool(path) and os.path.isdir(path) and os.path.basename(path).startswith("tco-automation-repo-")


def do_clone(repo_url):
    dest = os.path.join(tempfile.gettempdir(), f"tco-automation-repo-{uuid.uuid4().hex[:12]}")
    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--filter=blob:none", repo_url, dest],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise Exception(f"git clone failed: {result.stderr.strip()[:500]}")

    manifest = _load_manifest()
    automation_entry = dict(manifest.get("automation") or {})
    automation_entry["clonePath"] = dest
    manifest["automation"] = automation_entry
    _save_manifest(manifest)

    return dest


def _norm(path):
    return os.path.normcase(os.path.normpath(path)) if path else path


def do_cleanup(path, expected_path=None):
    """Delete path. If expected_path is given (the path recorded in
    run-manifest.toon at clone time), path MUST match it exactly - refuses
    otherwise, so the clone path and the cleanup path can never silently
    diverge regardless of what an agent tries to pass in."""
    if expected_path and _norm(path) != _norm(expected_path):
        raise Exception(
            f"Refusing to clean up {path!r} - it does not match the clone path "
            f"recorded for this run ({expected_path!r}). The clone and cleanup "
            f"path must always be the same; pass no --path at all to use the "
            f"recorded one automatically."
        )
    if _is_safe_clone_path(path):
        shutil.rmtree(path, ignore_errors=True)
        return not os.path.isdir(path)
    return False


def do_cleanup_previous():
    """Delete ONLY the clone path recorded in this work folder's own
    run-manifest.toon from a prior run - never any other tco-automation-repo-*
    folder, which could belong to an unrelated concurrent run."""
    manifest = _load_manifest()
    automation_entry = dict(manifest.get("automation") or {})
    prev_path = automation_entry.get("clonePath")
    if not prev_path:
        return None, False

    removed = do_cleanup(prev_path)
    automation_entry.pop("clonePath", None)
    manifest["automation"] = automation_entry
    _save_manifest(manifest)
    return prev_path, removed


def main():
    parser = argparse.ArgumentParser(description="Clone/cleanup temporary local copies of the automation repo.")
    parser.add_argument("--action", required=True, choices=["cleanup-previous", "clone", "cleanup"])
    parser.add_argument("--repo-url", help="Git URL (required for --action clone)")
    parser.add_argument("--path", help="Local path to remove for --action cleanup - optional, defaults to the path recorded at clone time; if given, MUST match that recorded path exactly")
    args = parser.parse_args()

    try:
        if args.action == "cleanup-previous":
            prev_path, removed = do_cleanup_previous()
            print(json.dumps({"success": True, "action": "cleanup-previous", "previousPath": prev_path, "removed": removed}, indent=2))
        elif args.action == "clone":
            if not args.repo_url:
                print(json.dumps({"success": False, "error": "--repo-url is required for --action clone"}, indent=2))
                sys.exit(1)
            path = do_clone(args.repo_url)
            print(json.dumps({"success": True, "action": "clone", "path": path}, indent=2))
        else:
            manifest = _load_manifest()
            automation_entry = dict(manifest.get("automation") or {})
            recorded_path = automation_entry.get("clonePath")
            path = args.path or recorded_path
            if not path:
                print(json.dumps({"success": False, "error": "--path is required for --action cleanup (no clonePath recorded in run-manifest.toon to default to either)"}, indent=2))
                sys.exit(1)
            removed = do_cleanup(path, expected_path=recorded_path)
            if recorded_path and _norm(path) == _norm(recorded_path):
                automation_entry.pop("clonePath", None)
                manifest["automation"] = automation_entry
                _save_manifest(manifest)
            print(json.dumps({"success": True, "action": "cleanup", "path": path, "removed": removed}, indent=2))
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
