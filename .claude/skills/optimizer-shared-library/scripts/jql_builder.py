#!/usr/bin/env python3
"""
Shared JQL construction helpers for the test-cases-optimizer skill scripts.

These patterns were verified live against Xray Cloud (two different projects,
two different Jira workflow schemes) before being hard-coded here:

  - A Test Plan / Test Set / Test Execution's tests CANNOT be filtered via the
    nested `tests(jql: ...)` connection (Xray's schema rejects a `jql` argument
    there). The working alternative is Xray's `issueFunction` JQL function
    against the FLAT `getTests(jql: ...)` query:
        issueFunction in testPlanTests("<KEY>")
        issueFunction in testSetTests("<KEY>")
        issueFunction in testExecutionTests("<KEY>")
    This was confirmed to return the exact same total as the real (unfiltered)
    container test count, and to combine correctly with extra AND clauses.

  - A Test Repository (whole project) is filtered directly on the flat query:
        project = <PROJECT>

  - `testType` IS filterable in JQL, but only using the bare keyword `testType`
    (unquoted, no spaces) - NOT the quoted field name "Test Type", which
    silently returns zero results instead of erroring. Always confirm the
    exact value via a probe read first (see probe-source-convention) rather
    than assuming "Manual"/"Automated" - the real automated value observed in
    this org is "Automated[Generic]", not "Automated".

  - Negative filters (`testType != "X"`) are only safe when X is the EXACT
    observed string for that project. A guessed/generic negative filter (e.g.
    `!= "Automated"` when the real value is "Automated[Generic]") silently
    matches almost everything and excludes nothing - this was proven to fail.
    Prefer a positive allow-list (`testType = "Manual"`) whenever the probe
    step identifies the manual value directly.
"""

CONTAINER_ISSUE_FUNCTIONS = {
    "plan": "testPlanTests",
    "testset": "testSetTests",
    "execution": "testExecutionTests",
}

STATUS_REMOVED = "Removed"


def container_scope_jql(source, key=None, project=None):
    """Base JQL scoping to a single source, with no status/testType filter."""
    if source in CONTAINER_ISSUE_FUNCTIONS:
        if not key:
            raise ValueError(f"--key is required for source '{source}'")
        func = CONTAINER_ISSUE_FUNCTIONS[source]
        return f'issueFunction in {func}("{key}")'
    if source == "repository":
        if not project:
            raise ValueError("--project is required for source 'repository'")
        return f"project = {project}"
    raise ValueError(f"Unknown source '{source}' (expected plan/testset/execution/repository)")


def status_not_removed_clause():
    return f"status != {STATUS_REMOVED}"


def status_removed_clause():
    return f"status = {STATUS_REMOVED}"


FILTERABLE_FIELDS = {"testType", "labels", "status"}


def field_clause(field, value, positive=True):
    """Build a `field = "value"` / `field != "value"` clause for whichever
    field probe-source-convention identified as the real manual/automated
    signal for this project (testType, or - when testType isn't reliable -
    labels or status). `value` must be the exact string observed on a real
    test via the probe - never a guessed label."""
    if field not in FILTERABLE_FIELDS:
        raise ValueError(f"Unsupported filter field '{field}' (expected one of {sorted(FILTERABLE_FIELDS)})")
    op = "=" if positive else "!="
    return f'{field} {op} "{value}"'


def combine_jql(*clauses):
    parts = [c for c in clauses if c]
    return " AND ".join(parts)


def build_manual_candidates_jql(source, key=None, project=None, filter_field=None, filter_value=None, filter_positive=True):
    """JQL for the manual agent's filtered candidate read: scoped to the
    source, excluding Removed, and (if the probe step found a reliable
    manual/automated signal - on testType, or falling back to labels/status
    when testType wasn't trustworthy) allow-listing/excluding by that field."""
    clauses = [container_scope_jql(source, key, project), status_not_removed_clause()]
    if filter_value:
        clauses.append(field_clause(filter_field or "testType", filter_value, positive=filter_positive))
    return combine_jql(*clauses)


def build_removed_ids_jql(source, key=None, project=None):
    """JQL for the manual agent's cheap Removed-ID-only collection."""
    return combine_jql(container_scope_jql(source, key, project), status_removed_clause())


def build_unfiltered_source_jql(source, key=None, project=None):
    """JQL for the automation agent's unfiltered full ID listing."""
    return container_scope_jql(source, key, project)
