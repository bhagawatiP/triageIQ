#!/usr/bin/env python3
"""
Test Case Extractor — Fetches test cases from Xray Cloud (Atlassian) via GraphQL
and saves them as JSON for the RIA v2 pipeline.

Supports multiple project keys (comma-separated). Produces:
  - One JSON file per project (e.g. all_tcs_PROJ1.json, all_tcs_PROJ2.json)
  - One merged JSON file combining all projects (all_tcs_extracted.json)

Configurable via environment variables or CLI arguments:
  --project-key        Comma-separated Jira project keys (e.g. PROJ1,PROJ2)
  --xray-client-id     Xray Cloud API client ID
  --xray-client-secret Xray Cloud API client secret
  --approved-only      Only fetch test cases with status "Approved"
  --priority-filter    Comma-separated priorities (e.g. P1,P2)
  --output             Output JSON file path (merged) or directory
  --env-file           Path to env file with config (key=value format)

Can also read credentials from the RIA env file (configs/ria_config.env) using:
  XRAY_CLIENT_ID, XRAY_CLIENT_SECRET, PROJECT_KEYS

Other behaviour (APPROVED_ONLY, PRIORITY_FILTER, RIA_INPUT_DIR) is hardcoded
in configs/ria_config.py and intentionally NOT exposed via env. CLI flags
(--approved-only, --priority-filter, --output) still take precedence for
ad-hoc overrides.
"""

import argparse
import io
import json
import os
import re
import sys
import time
from pathlib import Path

# Make the sibling configs/ directory importable so we can pull hardcoded
# defaults (APPROVED_ONLY, PRIORITY_FILTER, RIA_INPUT_DIR) from ria_config.py.
# Script lives at: skills/regression-impact-analysis/scripts/tc_extractor.py
# Configs live at: skills/regression-impact-analysis/configs/ria_config.py
_SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SKILL_ROOT not in sys.path:
    sys.path.insert(0, _SKILL_ROOT)

from configs.ria_config import APPROVED_ONLY, PRIORITY_FILTER, RIA_INPUT_DIR

# Fix Windows console encoding: force UTF-8 on stdout/stderr so Unicode
# characters (e.g. box-drawing, emoji, non-ASCII names) don't crash with
# UnicodeEncodeError: 'charmap' codec can't encode characters.
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    import httpx
except ImportError:
    # Exit code 2 signals "missing dependencies" to the caller (ria_agent.py),
    # so it can print a friendly first-run setup message instead of treating
    # this as a generic failure.
    _req_path = os.path.normpath(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "requirements.txt",
        )
    )
    print("=" * 78, file=sys.stderr)
    print("ERROR: Missing required Python dependency: 'httpx'", file=sys.stderr)
    print("=" * 78, file=sys.stderr)
    print("", file=sys.stderr)
    print("tc_extractor.py needs the 'httpx' package to call the Xray Cloud API.",
          file=sys.stderr)
    print("", file=sys.stderr)
    print("To fix this, install the RIA pipeline dependencies:", file=sys.stderr)
    print("", file=sys.stderr)
    print(f"  pip install -r {_req_path}", file=sys.stderr)
    print("", file=sys.stderr)
    print("Or install httpx by itself:", file=sys.stderr)
    print("", file=sys.stderr)
    print("  pip install httpx", file=sys.stderr)
    print("", file=sys.stderr)
    print("See FIRST_RUN_SETUP.md for full first-run instructions.",
          file=sys.stderr)
    print("=" * 78, file=sys.stderr)
    sys.exit(2)


# ── Constants ────────────────────────────────────────────────────────

XRAY_GRAPHQL_URL = "https://xray.cloud.getxray.app/api/v2/graphql"
XRAY_AUTH_URL = "https://xray.cloud.getxray.app/api/v1/authenticate"
PAGE_SIZE = 100

_PROJECT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,255}$")

# ── GraphQL Query ────────────────────────────────────────────────────

GQL_GET_TESTS = """
query GetTests {{
  getTests(jql: "project = '{project_key}'", start: {start}, limit: {limit}) {{
    total
    results {{
      issueId
      jira(fields: ["key", "summary", "description", "priority", "status"])
      testType {{
        name
        kind
      }}
      steps {{
        id
        action
        data
        result
      }}
    }}
  }}
}}
"""


# ── Helpers ──────────────────────────────────────────────────────────

def log(msg):
    print(f"[tc_extractor] {msg}", flush=True)


def parse_env_file(env_path):
    """Parse a key=value env file, ignoring comments and blank lines."""
    env = {}
    if not os.path.exists(env_path):
        return env
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


def validate_project_key(key):
    if not _PROJECT_KEY_RE.match(key):
        log(f"ERROR: Invalid project key: {key}")
        sys.exit(1)
    return key


def extract_name(field):
    """Extract 'name' from a Jira field that may be a dict or string."""
    if isinstance(field, dict):
        return str(field.get("name", "")).strip()
    return str(field).strip() if field else ""


def parse_jira_field(raw):
    """Normalize the jira() field from Xray GraphQL."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


# ── Xray API ─────────────────────────────────────────────────────────

def authenticate_xray(client_id, client_secret):
    """Obtain a bearer token for Xray Cloud."""
    log("Authenticating with Xray Cloud...")
    with httpx.Client(timeout=30, verify=False) as client:
        resp = client.post(
            XRAY_AUTH_URL,
            json={"client_id": client_id, "client_secret": client_secret},
        )
        resp.raise_for_status()
        token = resp.text.strip().strip('"')
    log("Authentication successful")
    return token


def fetch_page(token, project_key, start, limit):
    """Fetch one page of test cases via GraphQL."""
    query_str = GQL_GET_TESTS.format(
        project_key=project_key, start=start, limit=limit
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=60, verify=False) as client:
        resp = client.post(
            XRAY_GRAPHQL_URL,
            json={"query": query_str, "variables": ""},
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()


def fetch_all_test_cases(token, project_key, approved_only, priority_filter):
    """Fetch all test cases, paginating through the full dataset."""
    all_entries = []
    start = 0
    total = None

    pf_set = None
    if priority_filter:
        pf_set = {p.strip().lower() for p in priority_filter if p.strip()}
        log(f"Priority filter: {sorted(pf_set)}")

    if approved_only:
        log("Filter: approved only")

    while True:
        gql = fetch_page(token, project_key, start, PAGE_SIZE)

        if "errors" in gql:
            log(f"GraphQL errors: {json.dumps(gql['errors'], indent=2)}")
            sys.exit(1)

        data = gql.get("data", {}).get("getTests", {})
        if total is None:
            total = data.get("total", 0)
            log(f"Total test cases in project: {total}")

        results = data.get("results", [])
        if not results:
            break

        for tc in results:
            jira_fields = parse_jira_field(tc.get("jira"))
            key = jira_fields.get("key", "")
            status_name = extract_name(jira_fields.get("status", ""))
            priority_name = extract_name(jira_fields.get("priority", ""))

            # Filter: approved only
            if approved_only and status_name.lower() != "approved":
                continue

            # Filter: priority
            if pf_set and priority_name.lower() not in pf_set:
                continue

            entry = {
                "issue_key": key,
                "issue_id": tc.get("issueId"),
                "summary": jira_fields.get("summary", ""),
                "description": jira_fields.get("description", ""),
                "test_type": tc.get("testType"),
                "steps": tc.get("steps", []),
                "priority": jira_fields.get("priority"),
                "status": jira_fields.get("status"),
            }
            all_entries.append(entry)

        fetched_so_far = start + len(results)
        log(f"  Fetched {fetched_so_far}/{total} (kept {len(all_entries)} after filters)")

        if fetched_so_far >= total:
            break
        start += PAGE_SIZE
        time.sleep(0.2)  # rate limit courtesy

    return all_entries


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Fetch test cases from Xray Cloud and save as JSON for RIA v2"
    )
    parser.add_argument("--project-key", default="", help="Comma-separated Jira project keys (e.g. PROJ1,PROJ2)")
    parser.add_argument("--xray-client-id", default="", help="Xray Cloud API client ID")
    parser.add_argument("--xray-client-secret", default="", help="Xray Cloud API client secret")
    parser.add_argument("--approved-only", action="store_true", help="Only fetch approved TCs")
    parser.add_argument("--priority-filter", default="", help="Comma-separated priorities (e.g. P1,P2)")
    parser.add_argument("--output", default="", help="Output JSON file path (merged)")
    parser.add_argument("--env-file", default="", help="Path to env file with config")

    args = parser.parse_args()

    # Load env file if provided
    env = {}
    if args.env_file:
        env = parse_env_file(args.env_file)
        log(f"Loaded {len(env)} settings from {args.env_file}")

    # Resolve config: CLI > env file > environment variable
    # Support both PROJECT_KEY (singular) and PROJECT_KEYS (plural) from config
    project_key_raw = (
        args.project_key
        or env.get("PROJECT_KEYS", "")
        or env.get("PROJECT_KEY", "")
        or os.environ.get("PROJECT_KEYS", "")
        or os.environ.get("PROJECT_KEY", "")
    )
    xray_client_id = args.xray_client_id or env.get("XRAY_CLIENT_ID", "") or os.environ.get("XRAY_CLIENT_ID", "")
    xray_client_secret = args.xray_client_secret or env.get("XRAY_CLIENT_SECRET", "") or os.environ.get("XRAY_CLIENT_SECRET", "")
    output_path = args.output or env.get("TC_DATA_PATH", "") or os.environ.get("TC_DATA_PATH", "")

    # CLI arg takes precedence, then env file, then hardcoded default from ria_config
    if args.approved_only:
        approved_only = True
    elif env.get("APPROVED_ONLY", "").lower() in ("1", "true", "yes"):
        approved_only = True
    else:
        approved_only = APPROVED_ONLY  # Hardcoded default from ria_config.py

    # CLI arg takes precedence, then env file, then hardcoded default
    priority_str = args.priority_filter or env.get("PRIORITY_FILTER", "") or (PRIORITY_FILTER or "")
    priority_filter = [p.strip() for p in priority_str.split(",") if p.strip()] if priority_str else []

    # Parse project keys (comma-separated)
    project_keys = [k.strip() for k in project_key_raw.split(",") if k.strip()]

    # Validate required fields
    if not project_keys:
        log("ERROR: --project-key is required (or set PROJECT_KEYS / PROJECT_KEY in env file). Supports comma-separated keys.")
        sys.exit(1)
    if not xray_client_id or not xray_client_secret:
        log("ERROR: --xray-client-id and --xray-client-secret are required (or set XRAY_CLIENT_ID/XRAY_CLIENT_SECRET in env file)")
        sys.exit(1)
    if not output_path:
        # Use hardcoded default from ria_config.py (no warning needed)
        ria_input_dir_raw = env.get("RIA_INPUT_DIR", "") or RIA_INPUT_DIR
        _input_dir = os.path.abspath(ria_input_dir_raw)

        # Validate and create directory
        try:
            os.makedirs(_input_dir, exist_ok=True)
        except OSError as e:
            log(f"ERROR: Cannot create RIA_INPUT_DIR '{_input_dir}': {e}")
            log(f"  Ensure the path is valid and you have write permissions.")
            sys.exit(1)

        output_path = os.path.join(_input_dir, "all_tcs_extracted.json")
        log(f"Output path: {output_path}")

    # Resolve user-provided output path: if relative, resolve from CWD
    output_path = os.path.abspath(output_path)

    for pk in project_keys:
        validate_project_key(pk)

    log(f"Projects: {', '.join(project_keys)}")
    log(f"Output:   {output_path}")

    # Determine output directory from the merged output path and ensure it exists
    output_dir = os.path.dirname(output_path)
    os.makedirs(output_dir, exist_ok=True)

    # Authenticate once (token works across projects)
    token = authenticate_xray(xray_client_id, xray_client_secret)

    # Fetch from each project, save per-project files, and collect for merge
    all_merged = []
    seen_keys = set()
    per_project_files = []

    for pk in project_keys:
        log(f"\n{'-' * 40}")
        log(f"Fetching project: {pk}")
        log(f"{'-' * 40}")

        test_cases = fetch_all_test_cases(token, pk, approved_only, priority_filter)

        # Tag each TC with its source project
        for tc in test_cases:
            tc["project_key"] = pk

        log(f"  {pk}: {len(test_cases)} TCs after filtering")

        # Save per-project file
        per_project_path = os.path.join(output_dir, f"all_tcs_{pk}.json")
        with open(per_project_path, "w", encoding="utf-8") as f:
            json.dump(test_cases, f, indent=2, ensure_ascii=False)
        file_size = os.path.getsize(per_project_path)
        log(f"  Saved: {per_project_path} ({file_size:,} bytes)")
        per_project_files.append(per_project_path)

        # Merge (deduplicate by issue_key)
        for tc in test_cases:
            ik = tc.get("issue_key", "")
            if ik and ik not in seen_keys:
                seen_keys.add(ik)
                all_merged.append(tc)
            elif not ik:
                all_merged.append(tc)

    # Save merged file
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_merged, f, indent=2, ensure_ascii=False)
    merged_size = os.path.getsize(output_path)

    log(f"\n{'=' * 40}")
    log(f"EXTRACTION COMPLETE")
    log(f"{'=' * 40}")
    log(f"  Projects:     {', '.join(project_keys)}")
    for ppf in per_project_files:
        count = len(json.load(open(ppf, encoding="utf-8")))
        log(f"  - {os.path.basename(ppf)}: {count} TCs")
    log(f"  Merged:       {output_path} ({merged_size:,} bytes, {len(all_merged)} TCs)")


if __name__ == "__main__":
    main()
