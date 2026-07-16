#!/usr/bin/env python3
"""
Fetch test cases associated with any Xray Test Execution, Test Plan, Test Set,
single Test case, or Epic using the Xray Cloud GraphQL API v2.

Reads XRAY_CLIENT_ID and XRAY_CLIENT_SECRET from system environment variables.
For Epic lookup, also reads JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN
(all required for Epic support).

Usage:
  python fetch_xray_tests.py <JIRA_ISSUE_KEY>

Examples:
  python fetch_xray_tests.py CXQA-562392    # Test Execution
  python fetch_xray_tests.py CXQA-7585      # Test Set
  python fetch_xray_tests.py CXQA-7629      # Test Plan
  python fetch_xray_tests.py CXQA-940       # Single Test Case
  python fetch_xray_tests.py CXQA-100       # Epic
"""

import sys
import os
import re
import json
import base64
import urllib.request
import urllib.parse
import urllib.error

XRAY_AUTH_URL   = "https://xray.cloud.getxray.app/api/v2/authenticate"
XRAY_GRAPHQL_URL = "https://xray.cloud.getxray.app/api/v2/graphql"
STEP_BATCH_SIZE = 20


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def get_xray_token(client_id, client_secret):
    payload = json.dumps({"client_id": client_id, "client_secret": client_secret}).encode()
    req = urllib.request.Request(
        XRAY_AUTH_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode().strip().strip('"')
    except urllib.error.HTTPError as e:
        sys.exit(f"ERROR: Xray authentication failed ({e.code}): {e.read().decode()}")
    except urllib.error.URLError as e:
        sys.exit(
            "ERROR: Could not reach Xray authentication endpoint. "
            f"Reason: {e.reason}. Check network/DNS/proxy and try again."
        )


# ---------------------------------------------------------------------------
# GraphQL helper
# ---------------------------------------------------------------------------

def gql(token, query_str):
    payload = json.dumps({"query": query_str}).encode()
    req = urllib.request.Request(
        XRAY_GRAPHQL_URL, data=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            if "errors" in data:
                # surface GraphQL errors but don't exit — caller decides
                raise ValueError(f"GraphQL errors: {json.dumps(data['errors'])}")
            return data
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"HTTP {e.code}: {body[:300]}")
    except urllib.error.URLError as e:
        raise RuntimeError(
            "Could not reach Xray GraphQL endpoint. "
            f"Reason: {e.reason}. Check network/DNS/proxy and try again."
        )


# ---------------------------------------------------------------------------
# Issue type detection — uses JQL-based plural queries to resolve key → numeric ID
# Xray GraphQL singular queries (getTestExecution, getTest, …) require the
# numeric Jira issueId, NOT the human-readable key like CXQA-562392.
# The plural queries (getTestExecutions, getTests, …) accept a JQL filter,
# which lets us resolve the key first, then use the numeric ID for all subsequent calls.
# ---------------------------------------------------------------------------

_TYPE_QUERIES = [
    ("Test Execution", "getTestExecutions"),
    ("Test Plan",      "getTestPlans"),
    ("Test Set",       "getTestSets"),
    ("Test",           "getTests"),
]


ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")


def validate_issue_key(issue_key):
    """Validate Jira issue key format to avoid malformed/JQL-injected values."""
    return bool(ISSUE_KEY_PATTERN.fullmatch(issue_key or ""))


def resolve_issue_type(token, issue_key):
    """
    Return (type_name, numeric_issue_id) for issue_key, or (None, None) if not
    found as any Xray type (e.g. it's an Epic or plain Jira issue).

    Only an empty results list is treated as a non-match. API, network, auth,
    or response-shape failures are allowed to propagate so they are not masked
     as an unknown issue type.
    """
    if not validate_issue_key(issue_key):
        return None, None

    for type_name, gql_fn in _TYPE_QUERIES:
        query = f'{{ {gql_fn}(jql: "key = \\\"{issue_key}\\\"", limit: 1) {{ results {{ issueId }} }} }}'
        data = gql(token, query)
        results = data["data"][gql_fn]["results"]
        if results:
            return type_name, results[0]["issueId"]
    return None, None


# ---------------------------------------------------------------------------
# Fetching tests for each container type
# ---------------------------------------------------------------------------

def _paginate_tests(token, gql_type, numeric_id):
    all_tests = []
    start = 0
    limit = 50
    total = None

    while True:
        query = f'''{{
            {gql_type}(issueId: "{numeric_id}") {{
                tests(limit: {limit}, start: {start}) {{
                    total
                    results {{
                        issueId
                        status {{ name }}
                        testType {{ name }}
                        jira(fields: ["summary", "key", "labels", "components", "status"])
                    }}
                }}
            }}
        }}'''
        data = gql(token, query)
        container = data["data"][gql_type]
        if not container:
            break
        batch = container["tests"]
        if total is None:
            total = batch["total"]
        results = batch.get("results", [])
        all_tests.extend(results)
        print(f"  Fetched {len(all_tests)}/{total} tests...", flush=True)
        if not results or len(all_tests) >= total:
            break
        start += limit

    return all_tests


def fetch_tests_for_execution(token, numeric_id):
    return _paginate_tests(token, "getTestExecution", numeric_id)


def fetch_tests_for_plan(token, numeric_id):
    return _paginate_tests(token, "getTestPlan", numeric_id)


def fetch_tests_for_set(token, numeric_id):
    return _paginate_tests(token, "getTestSet", numeric_id)


def fetch_single_test(token, numeric_id):
    """Fetch a single test case with its steps."""
    query = f'''{{
        getTest(issueId: "{numeric_id}") {{
            issueId
            status {{ name }}
            testType {{ name }}
            jira(fields: ["summary", "key", "labels", "components", "status"])
            steps {{ action data result }}
        }}
    }}'''
    data = gql(token, query)
    test = data["data"]["getTest"]
    if not test:
        return None
    test["steps"] = _clean_steps(test.get("steps") or [])
    return test


# ---------------------------------------------------------------------------
# Steps — index field does NOT exist on the Xray Cloud Step GraphQL type;
# only action, data, result are valid.
# ---------------------------------------------------------------------------

def _strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _clean_steps(raw_steps):
    return [
        {
            "action": _strip_html(s.get("action", "")),
            "data":   _strip_html(s.get("data", "")),
            "result": _strip_html(s.get("result", "")),
        }
        for s in raw_steps
    ]


def _fetch_steps_batch(token, numeric_ids):
    alias_to_id = {f"test_{index}": numeric_id for index, numeric_id in enumerate(numeric_ids)}
    fields = "\n".join(
        f'{alias}: getTest(issueId: "{numeric_id}") {{ steps {{ action data result }} }}'
        for alias, numeric_id in alias_to_id.items()
    )
    data = gql(token, f"{{\n{fields}\n}}")
    response_data = data.get("data") or {}

    step_map = {}
    for alias, numeric_id in alias_to_id.items():
        raw_steps = (response_data.get(alias) or {}).get("steps") or []
        step_map[numeric_id] = _clean_steps(raw_steps)

    return step_map


def enrich_with_steps(token, tests):
    print(f"  Fetching steps for {len(tests)} tests...", flush=True)

    tests_by_id = {}
    ordered_ids = []
    for test in tests:
        numeric_id = test.get("issueId")
        if not numeric_id:
            test["steps"] = []
            continue
        tests_by_id[numeric_id] = test
        ordered_ids.append(numeric_id)

    for start in range(0, len(ordered_ids), STEP_BATCH_SIZE):
        batch_ids = ordered_ids[start:start + STEP_BATCH_SIZE]
        try:
            step_map = _fetch_steps_batch(token, batch_ids)
            for numeric_id in batch_ids:
                tests_by_id[numeric_id]["steps"] = step_map.get(numeric_id, [])
        except Exception as batch_error:
            print(
                f"  WARNING: Batched step fetch failed for {len(batch_ids)} tests: {batch_error}",
                flush=True,
            )
            for numeric_id in batch_ids:
                query = f'{{ getTest(issueId: "{numeric_id}") {{ steps {{ action data result }} }} }}'
                try:
                    data = gql(token, query)
                    raw = (data["data"].get("getTest") or {}).get("steps") or []
                    tests_by_id[numeric_id]["steps"] = _clean_steps(raw)
                except Exception as single_error:
                    test_key = (tests_by_id[numeric_id].get("jira") or {}).get("key", numeric_id)
                    print(
                        f"  WARNING: Step fetch failed for {test_key} ({numeric_id}): {single_error}",
                        flush=True,
                    )
                    tests_by_id[numeric_id]["steps"] = []


# ---------------------------------------------------------------------------
# Epic support via Jira REST API
# ---------------------------------------------------------------------------

def fetch_epic_containers(issue_key, jira_base_url, jira_email, jira_api_token):
    """
    Use the Jira REST API to find Test Executions/Plans/Sets linked to an Epic.
    Returns a list of Jira issue dicts.
    """
    if not validate_issue_key(issue_key):
        print(f"  WARNING: Invalid issue key format: {issue_key}", flush=True)
        return []

    creds = base64.b64encode(f"{jira_email}:{jira_api_token}".encode()).decode()
    jql = f'"Epic Link" = "{issue_key}" AND issuetype in ("Test Execution","Test Plan","Test Set") AND status != Removed'
    all_issues = []
    start_at = 0
    max_results = 100

    while True:
        url = (f"{jira_base_url}/rest/api/3/search"
               f"?jql={urllib.parse.quote(jql)}&startAt={start_at}&maxResults={max_results}"
               f"&fields=summary,issuetype,status")
        req = urllib.request.Request(
            url, headers={"Authorization": f"Basic {creds}", "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            print(f"  WARNING: Jira API call failed ({e.code}): {e.read().decode()[:200]}", flush=True)
            return all_issues
        except urllib.error.URLError as e:
            print(
                f"  WARNING: Jira API call failed (network/DNS): {e.reason}. "
                "Check network/DNS/proxy and retry.",
                flush=True,
            )
            return all_issues

        issues = payload.get("issues", [])
        total = payload.get("total", 0)
        all_issues.extend(issues)

        if not issues or len(all_issues) >= total:
            break

        start_at += max_results

    return all_issues


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_and_save(issue_key, issue_type, all_tests):
    print(f"\n{'='*60}")
    print(f"Xray Test Fetcher — {issue_key}")
    print(f"Issue Type : {issue_type}")
    print(f"Total Tests: {len(all_tests)}")
    print(f"{'='*60}\n")

    for i, test in enumerate(all_tests, 1):
        key        = test["jira"]["key"]
        summary    = test["jira"]["summary"]
        status     = (test.get("status") or {}).get("name", "")
        type_name  = (test.get("testType") or {}).get("name", "")

        print(f"  [{i:>3}] {key}")
        print(f"        Summary : {summary}")
        parts = []
        if status:
            parts.append(f"Status : {status}")
        if type_name:
            parts.append(f"Type : {type_name}")
        if parts:
            print("        " + "   ".join(parts))

        for j, s in enumerate(test.get("steps", []), 1):
            print(f"          Step {j}: {s['action']}")
            if s.get("data"):
                print(f"            Data    : {s['data']}")
            if s.get("result"):
                print(f"            Expected: {s['result']}")
        print()

    output_path = os.path.join(
        os.getcwd(), f"xray_tests_{issue_key.replace('-', '_')}.json"
    )
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {"issue_key": issue_key, "issue_type": issue_type, "tests": all_tests},
            f, indent=2
        )
    print(f"Full JSON written to: {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    issue_key = sys.argv[1].strip().upper()
    if not validate_issue_key(issue_key):
        sys.exit(
            f"ERROR: Invalid Jira issue key format: {issue_key}. "
            "Expected format like CXQA-9514."
        )

    client_id     = os.environ.get("XRAY_CLIENT_ID", "")
    client_secret = os.environ.get("XRAY_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        sys.exit(
            "ERROR: XRAY_CLIENT_ID and XRAY_CLIENT_SECRET must be set as system environment variables.\n"
            "  Windows : setx XRAY_CLIENT_ID your_id  &&  setx XRAY_CLIENT_SECRET your_secret\n"
            "  Linux/Mac: export XRAY_CLIENT_ID=...  &&  export XRAY_CLIENT_SECRET=...\n"
            "  Get them from: https://xray.cloud.getxray.app/ → API Keys"
        )

    print("Authenticating with Xray Cloud...")
    token = get_xray_token(client_id, client_secret)
    print(f"Authenticated. Detecting issue type for {issue_key}...")

    issue_type, numeric_id = resolve_issue_type(token, issue_key)

    # ---- Single Test Case ------------------------------------------------
    if issue_type == "Test":
        print(f"Detected: Test (issueId: {numeric_id})")
        test = fetch_single_test(token, numeric_id)
        if not test:
            sys.exit(f"ERROR: Could not fetch test case {issue_key}.")
        print_and_save(issue_key, issue_type, [test])
        return

    # ---- Test Execution / Plan / Set -------------------------------------
    if issue_type in ("Test Execution", "Test Plan", "Test Set"):
        print(f"Detected: {issue_type} (issueId: {numeric_id})")
        if issue_type == "Test Execution":
            tests = fetch_tests_for_execution(token, numeric_id)
        elif issue_type == "Test Plan":
            tests = fetch_tests_for_plan(token, numeric_id)
        else:
            tests = fetch_tests_for_set(token, numeric_id)

        if not tests:
            sys.exit(f"ERROR: No test cases found for {issue_key}.")

        enrich_with_steps(token, tests)
        print_and_save(issue_key, issue_type, tests)
        return

    # ---- Epic (not an Xray type — use Jira REST API) ---------------------
    jira_base_url  = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    jira_email     = os.environ.get("JIRA_EMAIL", "")
    jira_api_token = os.environ.get("JIRA_API_TOKEN", "")

    if jira_base_url and jira_email and jira_api_token:
        print(f"Not an Xray type — trying Epic lookup via Jira REST API...")
        containers = fetch_epic_containers(issue_key, jira_base_url, jira_email, jira_api_token)
        if not containers:
            sys.exit(f"ERROR: No Test Executions/Plans/Sets found linked to Epic {issue_key}.")

        all_tests = []
        seen = set()
        for c in containers:
            c_key  = c["key"]
            c_type = c["fields"]["issuetype"]["name"]
            print(f"  Processing {c_type}: {c_key}...", flush=True)
            _, c_numeric_id = resolve_issue_type(token, c_key)
            if not c_numeric_id:
                print(f"  WARNING: Could not resolve {c_key} via Xray — skipping.", flush=True)
                continue
            if c_type == "Test Execution":
                batch = fetch_tests_for_execution(token, c_numeric_id)
            elif c_type == "Test Plan":
                batch = fetch_tests_for_plan(token, c_numeric_id)
            else:
                batch = fetch_tests_for_set(token, c_numeric_id)
            for t in batch:
                t_key = t["jira"]["key"]
                if t_key not in seen:
                    seen.add(t_key)
                    all_tests.append(t)

        if not all_tests:
            sys.exit(f"ERROR: No test cases found for Epic {issue_key}.")

        enrich_with_steps(token, all_tests)
        print_and_save(issue_key, "Epic", all_tests)
        return

    # ---- Unknown issue type ----------------------------------------------
    sys.exit(
        f"ERROR: Could not identify {issue_key} as a Test Execution, Test Plan, Test Set, or Test.\n"
        "If this is an Epic, set these env vars to enable Epic lookup:\n"
        "  JIRA_BASE_URL   — e.g. https://your-org.atlassian.net\n"
        "  JIRA_EMAIL      — your Atlassian account email\n"
        "  JIRA_API_TOKEN  — Atlassian API token (not your password)"
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError) as exc:
        sys.exit(f"ERROR: {exc}")
