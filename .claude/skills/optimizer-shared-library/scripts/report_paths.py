#!/usr/bin/env python3
"""
Shared path helpers for the test-cases-optimizer working folder.

Layout (fixed - this is the entire file allowlist, nothing else is ever
written by any skill script):

  test-cases-optimizer-work/
    run-manifest.toon
    manual-agent-work/
      manual-groups.toon
      manual-duplicates.toon
      removed-tests.toon
    automation-agent-work/
      automation-groups.toon
      automation-duplicates.toon
    combine-duplicates.toon
    test-cases-optimizer-report.html

The work folder is created at the current repo's root (detected via
`git rev-parse --show-toplevel`), or at the current working directory if the
caller is not inside a git repository.
"""

import os
import subprocess

WORK_DIRNAME = "test-cases-optimizer-work"
MANUAL_SUBDIR = "manual-agent-work"
AUTOMATION_SUBDIR = "automation-agent-work"


def _repo_root_or_cwd():
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            top = result.stdout.strip()
            if top:
                return top
    except Exception:
        pass
    return os.getcwd()


def work_root():
    root = os.path.join(_repo_root_or_cwd(), WORK_DIRNAME)
    os.makedirs(root, exist_ok=True)
    return root


def manual_dir():
    d = os.path.join(work_root(), MANUAL_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


def automation_dir():
    d = os.path.join(work_root(), AUTOMATION_SUBDIR)
    os.makedirs(d, exist_ok=True)
    return d


def run_manifest_path():
    return os.path.join(work_root(), "run-manifest.toon")


def manual_groups_path():
    return os.path.join(manual_dir(), "manual-groups.toon")


def manual_duplicates_path():
    return os.path.join(manual_dir(), "manual-duplicates.toon")


def removed_tests_path():
    return os.path.join(manual_dir(), "removed-tests.toon")


def automation_groups_path():
    return os.path.join(automation_dir(), "automation-groups.toon")


def automation_duplicates_path():
    return os.path.join(automation_dir(), "automation-duplicates.toon")


def combine_duplicates_path():
    return os.path.join(work_root(), "combine-duplicates.toon")


def report_html_path():
    return os.path.join(work_root(), "test-cases-optimizer-report.html")
