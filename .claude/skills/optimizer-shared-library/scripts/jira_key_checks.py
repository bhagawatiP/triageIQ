#!/usr/bin/env python3
"""
Shared Jira key/project-key shape validation, used by every skill script
that accepts a --key or --project argument.

Jira enforces this shape on every real project key (uppercase letters/digits,
starting with a letter) as a platform rule, not a style preference - so
rejecting anything else is a safe, accurate check. A repo folder/module name
(e.g. "PaymentsAutomation") is mixed-case and will never pass this, which is
exactly the failure mode this guards against: a request naming an automation
repo folder got misread as a Jira project key and a real Jira query was
attempted with it.
"""

import re

PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}-\d+$")


def invalid_project_key_reason(value):
    """Return an error string if value doesn't look like a real Jira project
    key, or None if it's fine."""
    if value and not PROJECT_KEY_RE.match(value):
        return (f"{value!r} does not look like a real Jira project key (Jira project keys are "
                f"always uppercase letters/digits, e.g. 'CXQA'). This looks like it might be a repo "
                f"folder/module name instead - confirm the actual Jira project key with the user "
                f"before proceeding.")
    return None


def invalid_issue_key_reason(value):
    """Return an error string if value doesn't look like a real Jira issue
    key, or None if it's fine."""
    if value and not ISSUE_KEY_RE.match(value):
        return (f"{value!r} does not look like a real Jira issue key (expected a shape like "
                f"'PROJ-1234' - uppercase letters/digits, a dash, then digits). This looks like it "
                f"might be a repo folder/module name instead of a Jira Test Plan/Set/Execution key - "
                f"confirm the actual Jira key with the user before proceeding.")
    return None
