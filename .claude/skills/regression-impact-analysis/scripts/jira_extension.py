#!/usr/bin/env python3
"""
JIRA Extension Point - Document RIA results in JIRA

After RIA pipeline completes, this script:
1. Checks if the parent JIRA card has a "Quality" tab
2. If Quality tab exists -> Document RIA results there
3. If Quality tab doesn't exist -> Create RIA sub-task under parent card
4. Format follows CXWFM-77817 reference

Usage:
    python3 jira_extension.py --jira-card CXWFM-12345

Environment variables (from ria_config.env):
    XRAY_CLIENT_ID, XRAY_CLIENT_SECRET, PROJECT_KEYS
"""

import argparse
import json
import os
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any

try:
    import httpx
except ImportError:
    print("[ERROR] httpx is required. Install with: pip install httpx")
    sys.exit(1)


# Ensure UTF-8 stdout/stderr so glyphs like ✓ don't crash on Windows cp1252
# consoles (UnicodeEncodeError: 'charmap' codec can't encode character '\u2713').
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


# ── Constants ────────────────────────────────────────────────────────
XRAY_AUTH_URL = "https://xray.cloud.getxray.app/api/v1/authenticate"
XRAY_GRAPHQL_URL = "https://xray.cloud.getxray.app/api/v2/graphql"
# JIRA_REST_API will be set from config after loading

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent.parent  # Up to repository root
OUTPUT_DIR = REPO_ROOT / '.github' / 'RIA_OUTPUT'
CONFIG_FILE = SCRIPT_DIR.parent / 'configs' / 'ria_config.env'
REPORTS_DIR = REPO_ROOT / 'reports'  # Local backup for failed JIRA updates


# ── Configuration Loader ────────────────────────────────────────────
def load_config() -> Dict[str, str]:
    """Load configuration from ria_config.env"""
    config = {}

    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    config[key.strip()] = value.strip()

    # Environment variables override config file
    for key in ['XRAY_CLIENT_ID', 'XRAY_CLIENT_SECRET', 'PROJECT_KEYS']:
        if key in os.environ:
            config[key] = os.environ[key]

    return config


# ── Xray Authentication ────────────────────────────────────────────
def get_xray_token(client_id: str, client_secret: str) -> str:
    """Authenticate with Xray and get bearer token"""
    print(f"[Xray] Authenticating...")

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            XRAY_AUTH_URL,
            json={"client_id": client_id, "client_secret": client_secret},
            headers={"Content-Type": "application/json"}
        )

        if response.status_code != 200:
            print(f"[ERROR] Xray authentication failed: {response.status_code}")
            print(f"[ERROR] {response.text}")
            sys.exit(1)

        token = response.text.strip('"')
        print(f"[Xray] Authenticated successfully")
        return token


# ── Xray GraphQL Mutations ────────────────────────────────────────
# Correct mutation format for Xray Cloud API v2
CREATE_TEST_EXECUTION_MUTATION = """
mutation {
  createTestExecution(
    jira: {
      fields: {
        project: { key: "%PROJECT_KEY%" }
        summary: "%SUMMARY%"
        description: "%DESCRIPTION%"
        issuetype: { name: "Test Execution" }
      }
    }
    testEnvironments: ["Development"]
  ) {
    testExecution {
      issueId
      jira(fields: ["key"])
    }
    warnings
  }
}
"""

CREATE_TEST_EXECUTION_WITH_PARENT_MUTATION = """
mutation {
  createTestExecution(
    jira: {
      fields: {
        project: { key: "%PROJECT_KEY%" }
        summary: "%SUMMARY%"
        description: "%DESCRIPTION%"
        parent: { key: "%PARENT_KEY%" }
        issuetype: { name: "Test Execution" }
      }
    }
    testEnvironments: ["Development"]
  ) {
    testExecution {
      issueId
      jira(fields: ["key"])
    }
    warnings
  }
}
"""

ADD_TESTS_TO_EXECUTION_MUTATION = """
mutation {
  addTestsToTestExecution(
    issueId: "%ISSUE_ID%"
    testIssueIds: [%TEST_IDS%]
  ) {
    addedTests
    warning
  }
}
"""


# ── JIRA API Client ────────────────────────────────────────────────
class JiraClient:
    def __init__(self, jira_user: str, jira_token: str, xray_token: str, jira_base_url: str):
        """
        Initialize JIRA client with separate credentials:
        - jira_user + jira_token: For JIRA REST API (Basic Auth)
        - xray_token: For Xray GraphQL API (Bearer token)
        - jira_base_url: JIRA instance URL
        """
        import base64

        self.jira_user = jira_user
        self.jira_token = jira_token
        self.xray_token = xray_token
        self.jira_base_url = jira_base_url
        self.jira_rest_api = f"{jira_base_url}/rest/api/3"

        # JIRA REST API uses Basic Auth (email + API token)
        auth_string = f"{jira_user}:{jira_token}"
        encoded_auth = base64.b64encode(auth_string.encode()).decode()

        self.jira_headers = {
            "Authorization": f"Basic {encoded_auth}",
            "Content-Type": "application/json"
        }

        # Xray GraphQL API uses Bearer token
        self.xray_headers = {
            "Authorization": f"Bearer {xray_token}",
            "Content-Type": "application/json"
        }

    def get_issue(self, issue_key: str) -> Optional[Dict]:
        """Fetch JIRA issue details"""
        url = f"{self.jira_rest_api}/issue/{issue_key}"

        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, headers=self.jira_headers)

            if response.status_code == 200:
                return response.json()
            elif response.status_code == 404:
                print(f"[ERROR] JIRA card {issue_key} not found")
                print(f"[DEBUG] URL: {url}")
                print(f"[DEBUG] Response: {response.text}")
                return None
            else:
                print(f"[ERROR] Failed to fetch {issue_key}: {response.status_code}")
                print(f"[ERROR] {response.text}")
                return None

    def get_available_fields(self, issue_key: str) -> set:
        """Get list of fields available for this issue via editmeta"""
        url = f"{self.jira_rest_api}/issue/{issue_key}/editmeta"

        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, headers=self.jira_headers)

            if response.status_code == 200:
                editmeta = response.json()
                fields = editmeta.get('fields', {})
                return set(fields.keys())
            else:
                print(f"[WARN] Could not fetch editmeta: {response.status_code}")
                return set()

    def add_label(self, issue_key: str, label: str) -> bool:
        """Add a label to a JIRA issue WITHOUT removing existing labels.

        Uses the JIRA REST 'update' operation with the 'add' verb, which
        appends to the labels array atomically and leaves all current
        labels intact. Adding a label that already exists is a no-op on
        the JIRA side, so this is safe to call repeatedly.
        """
        url = f"{self.jira_rest_api}/issue/{issue_key}"

        payload = {
            "update": {
                "labels": [
                    {"add": label}
                ]
            }
        }

        with httpx.Client(timeout=30.0) as client:
            response = client.put(url, headers=self.jira_headers, json=payload)

            if response.status_code in [200, 204]:
                print(f"[JIRA] Added label '{label}' to {issue_key}")
                return True
            else:
                print(f"[WARN] Failed to add label '{label}' to {issue_key}: {response.status_code}")
                print(f"[WARN] {response.text}")
                return False

    def has_quality_tab(self, issue_key: str) -> bool:
        """Check if issue has a Quality tab/field available"""
        # Quality tab custom field (from SKILL 2.md reference)
        quality_field = 'customfield_12794'

        # Check if field is available for editing via editmeta API
        available_fields = self.get_available_fields(issue_key)

        if quality_field in available_fields:
            print(f"[JIRA] Quality field ({quality_field}) is available for {issue_key}")
            return True

        # Also check if field exists in current issue (might be read-only)
        issue = self.get_issue(issue_key)
        if issue:
            fields = issue.get('fields', {})
            if quality_field in fields:
                print(f"[JIRA] Quality field ({quality_field}) exists but may be read-only")
                return True

        print(f"[JIRA] Quality field ({quality_field}) not available for {issue_key}")
        return False

    def update_quality_field(self, issue_key: str, ria_content: str) -> bool:
        """Update Quality field with RIA results"""
        url = f"{self.jira_rest_api}/issue/{issue_key}"

        # Quality tab custom field (from SKILL 2.md reference)
        payload = {
            "fields": {
                "customfield_12794": ria_content  # Quality tab field
            }
        }

        with httpx.Client(timeout=30.0) as client:
            response = client.put(url, headers=self.jira_headers, json=payload)

            if response.status_code in [200, 204]:
                print(f"[JIRA] Updated Quality field in {issue_key}")
                return True
            else:
                print(f"[ERROR] Failed to update Quality field: {response.status_code}")
                print(f"[ERROR] {response.text}")
                return False

    def attach_file(self, issue_key: str, file_path: Path) -> bool:
        """Attach a file to a JIRA issue"""
        url = f"{self.jira_rest_api}/issue/{issue_key}/attachments"

        if not file_path.exists():
            print(f"[WARN] File not found: {file_path}")
            return False

        # JIRA attachment requires multipart/form-data and X-Atlassian-Token header
        headers = {
            "Authorization": self.jira_headers["Authorization"],
            "X-Atlassian-Token": "no-check"
        }

        with httpx.Client(timeout=60.0) as client:
            with open(file_path, 'rb') as f:
                files = {'file': (file_path.name, f, 'application/octet-stream')}
                response = client.post(url, headers=headers, files=files)

            if response.status_code == 200:
                print(f"[JIRA] Attached {file_path.name} to {issue_key}")
                return True
            else:
                print(f"[WARN] Failed to attach {file_path.name}: {response.status_code}")
                print(f"[WARN] {response.text}")
                return False

    def create_test_execution(self, parent_key: str, summary: str, description: str, test_issue_keys: List[str]) -> Optional[str]:
        """Create Xray Test Execution and link test cases to it"""

        # Determine project key based on test cases
        # Xray requires Test Execution to be in the same project as the tests
        if test_issue_keys:
            # Extract project from first test case (e.g., "EEM-29502" -> "EEM")
            test_project_key = test_issue_keys[0].split('-')[0]
            print(f"[Xray] Tests are in project {test_project_key}")
            project_key = test_project_key
        else:
            # Fallback to parent card's project if no tests
            parent = self.get_issue(parent_key)
            if not parent:
                return None
            project_key = parent['fields']['project']['key']

        print(f"[Xray] Creating Test Execution in project {project_key}...")

        # Check if parent card is in same project
        parent = self.get_issue(parent_key)
        parent_project = parent['fields']['project']['key'] if parent else None
        same_project = (parent_project == project_key)

        # Update description to reference parent card
        description_with_ref = f"{description}\\n\\nRelated Card: {parent_key}"

        # Escape description for GraphQL
        description_escaped = description_with_ref.replace('"', '\\"').replace('\n', '\\n')
        summary_escaped = summary.replace('"', '\\"')

        # Step 1: Create Test Execution via GraphQL
        # Use parent link only if same project (Xray limitation)
        if same_project:
            print(f"[Xray] Linking Test Execution as child of {parent_key}")
            mutation = CREATE_TEST_EXECUTION_WITH_PARENT_MUTATION.replace('%PROJECT_KEY%', project_key) \
                                                                   .replace('%SUMMARY%', summary_escaped) \
                                                                   .replace('%DESCRIPTION%', description_escaped) \
                                                                   .replace('%PARENT_KEY%', parent_key)
        else:
            print(f"[Xray] Creating standalone Test Execution (different project from parent card)")
            mutation = CREATE_TEST_EXECUTION_MUTATION.replace('%PROJECT_KEY%', project_key) \
                                                       .replace('%SUMMARY%', summary_escaped) \
                                                       .replace('%DESCRIPTION%', description_escaped)

        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                XRAY_GRAPHQL_URL,
                headers=self.xray_headers,
                json={"query": mutation}
            )

            if response.status_code == 200:
                result = response.json()

                # Check for errors
                if "errors" in result:
                    print(f"[ERROR] GraphQL errors: {result['errors']}")
                    return None

                data = result.get('data', {}).get('createTestExecution', {})
                test_execution = data.get('testExecution', {})
                execution_key = test_execution.get('jira', {}).get('key')

                if not execution_key:
                    print(f"[ERROR] No execution key in response: {result}")
                    return None

                print(f"[Xray] Created Test Execution: {execution_key}")

                # Step 2: Add tests to execution
                if test_issue_keys:
                    print(f"[Xray] Adding {len(test_issue_keys)} tests to execution...")

                    execution_id = test_execution.get('issueId')

                    # Convert test keys to issue IDs (Xray requires numeric IDs, not keys)
                    test_ids = []
                    for key in test_issue_keys[:100]:  # Batch limit
                        test_issue = self.get_issue(key)
                        if test_issue:
                            test_ids.append(test_issue['id'])

                    if not test_ids:
                        print(f"[WARN] No valid test IDs found - skipping test mapping")
                    else:
                        print(f"[Xray] Converted {len(test_ids)} test keys to IDs")

                        # Format test IDs as quoted strings for GraphQL
                        test_ids_str = ', '.join([f'"{tid}"' for tid in test_ids])

                        add_mutation = ADD_TESTS_TO_EXECUTION_MUTATION.replace('%ISSUE_ID%', execution_id) \
                                                                        .replace('%TEST_IDS%', test_ids_str)

                        add_response = client.post(
                            XRAY_GRAPHQL_URL,
                            headers=self.xray_headers,
                            json={"query": add_mutation}
                        )

                        if add_response.status_code == 200:
                            add_result = add_response.json()
                            if "errors" in add_result:
                                print(f"[WARN] Could not add tests: {add_result['errors']}")
                            else:
                                added_count = add_result.get('data', {}).get('addTestsToTestExecution', {}).get('addedTests', 0)
                                print(f"[Xray] Added {added_count} tests to execution")
                        else:
                            print(f"[WARN] Failed to add tests: {add_response.status_code}")

                return execution_key

            else:
                print(f"[ERROR] Failed to create Test Execution: {response.status_code}")
                print(f"[ERROR] {response.text}")
                return None

    def create_subtask(self, parent_key: str, summary: str, description: str) -> Optional[str]:
        """Create RIA sub-task under parent issue"""
        url = f"{self.jira_rest_api}/issue"

        # Get parent issue to extract project key and issue type
        parent = self.get_issue(parent_key)
        if not parent:
            return None

        project_key = parent['fields']['project']['key']

        payload = {
            "fields": {
                "project": {"key": project_key},
                "parent": {"key": parent_key},
                "summary": summary,
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": description}
                            ]
                        }
                    ]
                },
                "issuetype": {"name": "Sub-task"}
            }
        }

        with httpx.Client(timeout=30.0) as client:
            response = client.post(url, headers=self.jira_headers, json=payload)

            if response.status_code == 201:
                subtask_key = response.json().get('key')
                print(f"[JIRA] Created sub-task: {subtask_key}")
                return subtask_key
            else:
                print(f"[ERROR] Failed to create sub-task: {response.status_code}")
                print(f"[ERROR] {response.text}")
                return None


# ── RIA Results Formatter ────────────────────────────────────────
def load_ria_results() -> Dict[str, Any]:
    """Load RIA pipeline results"""
    stage6_file = OUTPUT_DIR / 'stage6_aggressive_tests.json'
    summary_file = OUTPUT_DIR / 'consolidated_summary.json'

    if not stage6_file.exists():
        print(f"[ERROR] RIA results not found: {stage6_file}")
        sys.exit(1)

    with open(stage6_file, 'r') as f:
        stage6_data = json.load(f)

    summary_data = {}
    if summary_file.exists():
        with open(summary_file, 'r') as f:
            summary_data = json.load(f)

    return {
        'stage6': stage6_data,
        'summary': summary_data
    }


def save_report_locally(jira_card: str, ria_content: str, reason: str):
    """Save RIA report locally when JIRA update fails (per SKILL 2.md)"""
    REPORTS_DIR.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    filename = f"RIA-{jira_card}-{timestamp}.md"
    filepath = REPORTS_DIR / filename

    header = f"""# RIA Report for {jira_card}
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Status: JIRA Update Failed
Reason: {reason}

---

"""

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(header)
        f.write(ria_content)

    print()
    print(f"[LOCAL BACKUP] Report saved to: {filepath}")
    print(f"[LOCAL BACKUP] Reason: {reason}")
    return filepath


def format_ria_content_adf(ria_data: Dict[str, Any], jira_card: str, test_execution_key: Optional[str] = None) -> Dict:
    """
    Format RIA results in Atlassian Document Format (ADF) following AW-54814 template structure.

    Comprehensive sections:
    1. Pre-Deployment Checklist
    2. New functionality / Issue to be fixed
    3. New code / Code Changes (per component)
    4. Regression impact (with failure modes, blast radius, fallback)
    5. Regression Tests (references Test Execution)
    6. Existing Test Plans/Executions
    7. Coverage Gaps

    Returns ADF JSON structure for customfield_12794 (Quality tab).
    """
    stage6 = ria_data['stage6']
    summary = ria_data.get('summary', {})

    # Extract data
    tests = stage6.get('aggressive_tests', [])
    test_count = len(tests)
    methods = summary.get('changed_methods', 1)

    # Collect unique flows with priority
    unique_flows = set()
    flow_criticality = {}
    flow_test_map = {}
    
    for test in tests:
        for flow in test.get('matched_flows', []):
            flow_name = flow.get('flow_name')
            if flow_name:
                unique_flows.add(flow_name)

                # Extract priority name from dict or string
                priority_raw = test.get('priority', {})
                if isinstance(priority_raw, dict):
                    priority = priority_raw.get('name', 'P2')  # P0, P1, P2, P3
                else:
                    priority = str(priority_raw)

                # Track highest priority for each flow
                if flow_name not in flow_criticality or priority_rank(priority) > priority_rank(flow_criticality.get(flow_name, 'P2')):
                    flow_criticality[flow_name] = priority

                # Group tests by flow
                if flow_name not in flow_test_map:
                    flow_test_map[flow_name] = []
                flow_test_map[flow_name].append(test)

    # Get changed methods details
    per_method = summary.get('per_method', [])

    # Build method title
    if per_method and len(per_method) == 1:
        method_info = per_method[0]
        class_name = method_info.get('class_name', '')
        method_name = method_info.get('method_name', 'Unknown')
        title_suffix = f"{class_name}.{method_name}" if class_name else method_name
    elif per_method and len(per_method) > 1:
        title_suffix = f"Multiple Methods ({len(per_method)} changed)"
    else:
        title_suffix = "Code Changes"

    # JIRA base URL
    jira_base_url = os.getenv('JIRA_BASE_URL', '')

    # Start building ADF content
    adf_content = []

    # ========== TITLE ==========
    adf_content.append({
        "type": "heading",
        "attrs": {"level": 3},
        "content": [{
            "type": "text",
            "text": f"Regression Impact Analysis (RIA) — {title_suffix}"
        }]
    })

    # ========== SECTION 1: Pre-Deployment Checklist ==========
    adf_content.append({
        "type": "heading",
        "attrs": {"level": 4},
        "content": [{
            "type": "text",
            "text": "Pre-Deployment Checklist:",
            "marks": [{"type": "strong"}]
        }]
    })

    checklist_items = []
    
    # Group flows by criticality for checklist (P0/P1 = high, P2 = medium)
    high_critical_flows = [f for f in unique_flows if flow_criticality.get(f) in ['P0', 'P1', 'CRITICAL']]
    high_flows = [f for f in unique_flows if flow_criticality.get(f) in ['P2', 'HIGH', 'MEDIUM']]
    
    # Add HIGH priority items for CRITICAL flows
    for flow in high_critical_flows[:3]:
        method_names = ", ".join([m.get('method_name', 'Unknown') for m in per_method[:2]])
        checklist_items.append({
            "type": "listItem",
            "content": [{
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "[HIGH] ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": f"{flow} flow: Verify functionality still works correctly after changes to "},
                    {"type": "text", "text": method_names, "marks": [{"type": "code"}]},
                    {"type": "text", "text": f". {len(flow_test_map.get(flow, []))} regression test(s) available in Test Execution."}
                ]
            }]
        })
    
    # Add MEDIUM priority items
    for flow in high_flows[:2]:
        checklist_items.append({
            "type": "listItem",
            "content": [{
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "[MEDIUM] ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": f"{flow} flow: Test for potential side effects from code changes."}
                ]
            }]
        })
    
    if not checklist_items:
        checklist_items.append({
            "type": "listItem",
            "content": [{
                "type": "paragraph",
                "content": [{
                    "type": "text",
                    "text": f"Verify all {len(unique_flows)} impacted flows via Test Execution."
                }]
            }]
        })

    adf_content.append({"type": "bulletList", "content": checklist_items})

    # ========== SECTION 2: New functionality / Issue to be fixed ==========
    adf_content.append({
        "type": "heading",
        "attrs": {"level": 4},
        "content": [{
            "type": "text",
            "text": "New functionality / Issue to be fixed:",
            "marks": [{"type": "strong"}]
        }]
    })

    # Build detailed functional description
    adf_content.append({
        "type": "paragraph",
        "content": [{
            "type": "text",
            "text": "⚠️ ",
            "marks": [{"type": "strong"}]
        }, {
            "type": "text",
            "text": "This section should be manually updated with the actual issue description from the PR/JIRA card. Below is RIA's analysis:",
            "marks": [{"type": "em"}]
        }]
    })

    # RIA-generated context
    flow_descriptions = []
    for flow in sorted(unique_flows):
        flow_readable = flow.replace('_', ' ').title()
        flow_descriptions.append(flow_readable)

    # Format flow list properly
    if len(flow_descriptions) <= 5:
        flow_list_text = ', '.join(flow_descriptions)
    else:
        flow_list_text = ', '.join(flow_descriptions[:5]) + f" (and {len(flow_descriptions) - 5} more)"

    adf_content.append({
        "type": "paragraph",
        "content": [{
            "type": "text",
            "text": f"Code changes detected in {methods} method(s) impacting {len(unique_flows)} business flow(s): {flow_list_text}."
        }]
    })

    # Method-level functional context
    for method_info in per_method[:3]:
        method_name = method_info.get('method_name', '')
        class_name = method_info.get('class_name', '')
        purpose = extract_purpose_from_method(method_name)

        adf_content.append({
            "type": "bulletList",
            "content": [{
                "type": "listItem",
                "content": [{
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": f"{class_name}.{method_name}: ", "marks": [{"type": "code"}]},
                        {"type": "text", "text": f"Handles {purpose}. Based on impacted flows, this likely affects how the system processes {', '.join(flow_descriptions[:2])} logic."}
                    ]
                }]
            }]
        })

    adf_content.append({
        "type": "paragraph",
        "content": [{
            "type": "text",
            "text": f"For detailed root cause analysis, code-level changes, and user impact description, refer to {jira_card} issue description and pull request.",
            "marks": [{"type": "em"}]
        }]
    })

    # ========== SECTION 3: New code / Code Changes ==========
    adf_content.append({
        "type": "heading",
        "attrs": {"level": 4},
        "content": [{
            "type": "text",
            "text": "New code / Code Changes:",
            "marks": [{"type": "strong"}]
        }]
    })

    adf_content.append({
        "type": "paragraph",
        "content": [{
            "type": "text",
            "text": f"Changes will be done at {len(per_method)} place(s):",
            "marks": [{"type": "strong"}]
        }]
    })

    # Detailed per-component breakdown
    for idx, method_info in enumerate(per_method, 1):
        method_name = method_info.get('method_name', 'Unknown')
        file_path = method_info.get('file_path', 'Unknown')
        class_name = method_info.get('class_name', '')
        
        full_name = f"{class_name}.{method_name}" if class_name else method_name
        component_name = class_name.split('.')[-1] if class_name else method_name
        
        # Determine component type from file path
        component_type = "library"
        if "/webapp/" in file_path or "/ui-" in file_path:
            component_type = "webapp"
        elif "/service/" in file_path or "/api/" in file_path:
            component_type = "service"
        
        # Component header
        adf_content.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": f"{idx}. ", "marks": [{"type": "strong"}]},
                {"type": "text", "text": component_name, "marks": [{"type": "strong"}]}
            ]
        })

        # Component details
        component_details = [
            {
                "type": "listItem",
                "content": [{
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Component name: ", "marks": [{"type": "strong"}]},
                        {"type": "text", "text": component_name}
                    ]
                }]
            },
            {
                "type": "listItem",
                "content": [{
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Component type: ", "marks": [{"type": "strong"}]},
                        {"type": "text", "text": component_type}
                    ]
                }]
            },
            {
                "type": "listItem",
                "content": [{
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "File name: ", "marks": [{"type": "strong"}]},
                        {"type": "text", "text": file_path, "marks": [{"type": "code"}]}
                    ]
                }]
            },
            {
                "type": "listItem",
                "content": [{
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Behavior changed: ", "marks": [{"type": "strong"}]},
                        {"type": "text", "text": f"⚠️ ", "marks": [{"type": "strong"}]},
                        {"type": "text", "text": "Update with specific behavior changes from code diff. ", "marks": [{"type": "em"}]},
                        {"type": "text", "text": f"Method {full_name} modified - RIA analysis suggests this impacts {extract_functionality_description(method_name, flow_test_map, unique_flows)}"}
                    ]
                }]
            },
            {
                "type": "listItem",
                "content": [{
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Logic added/modified: ", "marks": [{"type": "strong"}]},
                        {"type": "text", "text": "⚠️ ", "marks": [{"type": "strong"}]},
                        {"type": "text", "text": "Add specific code snippets here. Example format: ", "marks": [{"type": "em"}]},
                        {"type": "text", "text": "Added validation check: if (condition) { /* logic */ }", "marks": [{"type": "code"}]},
                        {"type": "text", "text": ". See pull request diff for complete changes."}
                    ]
                }]
            },
            {
                "type": "listItem",
                "content": [{
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "Impact on data flow: ", "marks": [{"type": "strong"}]},
                        {"type": "text", "text": f"Based on {len(flow_test_map.get(list(unique_flows)[0] if unique_flows else 'N/A', []))} test cases, changes may affect: data validation, business rule processing, state management, or API response handling in related flows."}
                    ]
                }]
            }
        ]

        adf_content.append({"type": "bulletList", "content": component_details})

    # ========== SECTION 4: Regression impact ==========
    adf_content.append({
        "type": "heading",
        "attrs": {"level": 4},
        "content": [{
            "type": "text",
            "text": "Regression impact:",
            "marks": [{"type": "strong"}]
        }]
    })

    # Sort flows by priority (CRITICAL first)
    sorted_flows = sorted(unique_flows, key=lambda f: priority_rank(flow_criticality.get(f, 'LOW')), reverse=True)
    
    impact_items = []

    # Add guidance note
    impact_items.append({
        "type": "listItem",
        "content": [{
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "⚠️ ", "marks": [{"type": "strong"}]},
                {"type": "text", "text": "Update failure modes with specific technical details. RIA provides flow analysis below:", "marks": [{"type": "em"}]}
            ]
        }]
    })

    for flow_name in sorted_flows[:5]:  # Top 5 flows
        test_list = flow_test_map.get(flow_name, [])
        priority = flow_criticality.get(flow_name, 'MEDIUM')
        method_names = ", ".join([m.get('method_name', '') for m in per_method[:2]])

        # Generate more specific failure mode description
        flow_readable = flow_name.replace('_', ' ').title()
        failure_examples = {
            "get": f"could return incorrect/stale data for {flow_readable}, causing UI display issues or downstream processing errors",
            "set": f"might fail to persist changes correctly in {flow_readable}, leading to data inconsistency",
            "update": f"could apply incorrect business rules during {flow_readable} processing",
            "create": f"might fail validation or create incomplete records in {flow_readable}",
            "fetch": f"could retrieve wrong dataset or fail under load for {flow_readable}",
            "validate": f"might incorrectly accept/reject data in {flow_readable}",
            "default": f"could break core functionality in {flow_readable} workflow"
        }

        # Match method pattern to failure mode
        failure_desc = failure_examples.get("default", "")
        for method_info in per_method[:1]:
            m_name = method_info.get('method_name', '').lower()
            for key in failure_examples.keys():
                if m_name.startswith(key):
                    failure_desc = failure_examples[key]
                    break

        impact_items.append({
            "type": "listItem",
            "content": [{
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": f"[{priority}] ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": flow_readable, "marks": [{"type": "strong"}]},
                    {"type": "text", "text": ": "},
                    {"type": "text", "text": "Failure mode: ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": f"Changes to {method_names} {failure_desc}. "},
                    {"type": "text", "text": "Blast radius: ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": f"All users/agents using {flow_readable} feature. "},
                    {"type": "text", "text": "Active in prod: ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": "Yes (verify feature toggle status if applicable). "},
                    {"type": "text", "text": "Fallback: ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": f"Verify via {len(test_list)} regression test(s) in Test Execution. "},
                    {"type": "text", "text": f"Manual testing recommended for edge cases.", "marks": [{"type": "em"}]}
                ]
            }]
        })

    adf_content.append({"type": "bulletList", "content": impact_items})

    # ========== SECTION 5: Regression Tests ==========
    adf_content.append({
        "type": "heading",
        "attrs": {"level": 4},
        "content": [{
            "type": "text",
            "text": "Regression Tests:",
            "marks": [{"type": "strong"}]
        }]
    })

    # Critical tests subsection - extract P0/P1 priorities
    critical_tests = []
    for t in tests:
        p = t.get('priority', {})
        p_name = p.get('name', 'P2') if isinstance(p, dict) else str(p)
        if p_name in ['P0', 'P1', 'CRITICAL', 'HIGH']:
            critical_tests.append(t)
    if critical_tests:
        adf_content.append({
            "type": "paragraph",
            "content": [{
                "type": "text",
                "text": "Critical (Must Test):",
                "marks": [{"type": "strong"}]
            }]
        })

        critical_items = []
        for test in critical_tests[:5]:  # Top 5 critical
            test_id = test.get('issue_key', 'N/A')
            summary_text = test.get('summary', 'No summary')[:100]

            critical_items.append({
                "type": "listItem",
                "content": [{
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": test_id,
                            "marks": [{
                                "type": "link",
                                "attrs": {"href": f"{jira_base_url}/browse/{test_id}"}
                            }]
                        },
                        {"type": "text", "text": f" {summary_text}"}
                    ]
                }]
            })

        adf_content.append({"type": "bulletList", "content": critical_items})

    # Reference to Test Execution
    adf_content.append({
        "type": "paragraph",
        "content": [{
            "type": "text",
            "text": f"All {test_count} recommended tests mapped to Test Execution. See section below for link.",
            "marks": [{"type": "em"}]
        }]
    })

    # ========== SECTION 6: Existing Test Plans/Executions ==========
    adf_content.append({
        "type": "heading",
        "attrs": {"level": 4},
        "content": [{
            "type": "text",
            "text": "Existing Test Plans/Executions:",
            "marks": [{"type": "strong"}]
        }]
    })

    if test_execution_key:
        adf_content.append({
            "type": "paragraph",
            "content": [
                {
                    "type": "text",
                    "text": test_execution_key,
                    "marks": [
                        {"type": "strong"},
                        {
                            "type": "link",
                            "attrs": {"href": f"{jira_base_url}/browse/{test_execution_key}"}
                        }
                    ]
                },
                {"type": "text", "text": f" — RIA Generated Test Execution [{test_count} tests mapped]"}
            ]
        })
    else:
        adf_content.append({
            "type": "paragraph",
            "content": [{
                "type": "text",
                "text": "No existing test executions. See attached HTML report for recommended tests."
            }]
        })

    # ========== SECTION 7: Coverage Gaps ==========
    adf_content.append({
        "type": "heading",
        "attrs": {"level": 4},
        "content": [{
            "type": "text",
            "text": "Coverage Gaps:",
            "marks": [{"type": "strong"}]
        }]
    })

    # Introduction
    adf_content.append({
        "type": "paragraph",
        "content": [{
            "type": "text",
            "text": "RIA identified business flows impacted by code changes. This section shows which flows have test cases in Xray and which have gaps."
        }]
    })

    # ---- Build flow_details (sorted: gaps first) ----
    flow_details = []
    for flow in unique_flows:
        flow_tests = flow_test_map.get(flow, [])
        flow_details.append({
            'name': flow,
            'count': len(flow_tests),
            'tests': flow_tests
        })
    flow_details.sort(key=lambda x: (x['count'], x['name']))

    # ---- Statistics ----
    total_identified    = len(unique_flows)
    flows_no_coverage   = sum(1 for f in flow_details if f['count'] == 0)
    flows_insufficient  = sum(1 for f in flow_details if f['count'] == 1)
    flows_minimal       = sum(1 for f in flow_details if f['count'] == 2)
    flows_adequate      = sum(1 for f in flow_details if f['count'] >= 3)
    total_test_count    = sum(f['count'] for f in flow_details)

    def _pretty(name: str) -> str:
        return name.replace('_', ' ').title()

    # ============================================================
    # Coverage Analysis - Flow by Flow
    # ============================================================

    for f in flow_details:
        flow_name = _pretty(f['name'])
        test_count = f['count']
        tests = f['tests']

        # Flow name as header
        adf_content.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": f"{flow_name}: ", "marks": [{"type": "strong"}]},
                {"type": "text", "text": f"{test_count} test case(s) found in Xray"}
            ]
        })

        # Show test IDs if available
        if test_count > 0:
            test_ids = [t.get('issue_key', 'N/A') for t in tests]
            test_links = []
            for i, key in enumerate(test_ids):
                if i > 0:
                    test_links.append({"type": "text", "text": ", "})
                test_links.append({
                    "type": "text",
                    "text": key,
                    "marks": [{"type": "link", "attrs": {"href": f"{jira_base_url}/browse/{key}"}}]
                })

            adf_content.append({
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "  Test Cases: "}
                ] + test_links
            })

        # Assessment
        if test_count == 0:
            adf_content.append({
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "  ❌ Gap: ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": "NO test cases exist for this flow. Create 2-3 test cases before deployment."}
                ]
            })
        elif test_count == 1:
            adf_content.append({
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "  ⚠️ Gap: ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": "Only 1 test case exists. Add 1-2 more test cases to cover different scenarios (positive/negative/edge cases)."}
                ]
            })
        elif test_count == 2:
            adf_content.append({
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "  ✓ Acceptable: ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": "2 test cases exist - baseline coverage is present."}
                ]
            })
        else:
            adf_content.append({
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "  ✅ Good: ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": f"{test_count} test cases exist - good coverage."}
                ]
            })

    # ============================================================
    # Summary
    # ============================================================
    adf_content.append({
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "Summary: ", "marks": [{"type": "strong"}]},
            {"type": "text", "text": f"{total_identified} flows impacted, {total_test_count} test cases found in Xray. "},
            {"type": "text", "text": f"{flows_no_coverage} flows have NO tests, {flows_insufficient} flows have only 1 test, {flows_minimal} flows have 2 tests, {flows_adequate} flows have 3+ tests."}
        ]
    })

    # ============================================================
    # ACTION PLAN PANEL — only if gaps exist
    # ============================================================
    gap_count = flows_no_coverage + flows_insufficient
    if gap_count > 0:
        action_items = []

        if flows_no_coverage > 0:
            no_cov_names = ", ".join(_pretty(f['name'])
                                     for f in flow_details if f['count'] == 0)
            action_items.append({
                "type": "listItem",
                "content": [{
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "P0 Blocker: ", "marks": [{"type": "strong"}]},
                        {"type": "text", "text": (
                            f"Create 2-3 test cases (happy path, error handling, edge case) "
                            f"for the {flows_no_coverage} flow(s) with zero coverage: "
                        )},
                        {"type": "text", "text": no_cov_names, "marks": [{"type": "strong"}]},
                        {"type": "text", "text": "."}
                    ]
                }]
            })

        if flows_insufficient > 0:
            insuff_names = ", ".join(_pretty(f['name'])
                                     for f in flow_details if f['count'] == 1)
            action_items.append({
                "type": "listItem",
                "content": [{
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "P1 High: ", "marks": [{"type": "strong"}]},
                        {"type": "text", "text": (
                            f"Add at least 1 more test case to the {flows_insufficient} flow(s) "
                            f"with only a single test: "
                        )},
                        {"type": "text", "text": insuff_names, "marks": [{"type": "strong"}]},
                        {"type": "text", "text": "."}
                    ]
                }]
            })

        action_items.append({
            "type": "listItem",
            "content": [{
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Owner: ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": "QA Team Lead — assign authoring + automation."}
                ]
            }]
        })

        action_items.append({
            "type": "listItem",
            "content": [{
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Estimated effort: ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": (
                        f"~{flows_no_coverage * 2 + flows_insufficient} new test case(s), "
                        f"approximately {(flows_no_coverage * 2 + flows_insufficient) * 1} hour(s) "
                        f"to author plus review/automation time."
                    )}
                ]
            }]
        })

        adf_content.append({
            "type": "panel",
            "attrs": {"panelType": "error" if flows_no_coverage > 0 else "warning"},
            "content": [
                {
                    "type": "paragraph",
                    "content": [{
                        "type": "text",
                        "text": "Action plan (required before deployment):",
                        "marks": [{"type": "strong"}]
                    }]
                },
                {"type": "bulletList", "content": action_items}
            ]
        })

    # Footer
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    adf_content.append({
        "type": "paragraph",
        "content": [{
            "type": "text",
            "text": f"Generated by RIA Pipeline v2 on {timestamp}",
            "marks": [{"type": "em"}]
        }]
    })

    # Return ADF document
    return {
        "type": "doc",
        "version": 1,
        "content": adf_content
    }


def priority_rank(priority: str) -> int:
    """Convert priority to numeric rank for sorting (supports both JIRA P0-P3 and CRITICAL/HIGH/MEDIUM/LOW)"""
    # JIRA priority names
    jira_ranks = {'P0': 4, 'P1': 3, 'P2': 2, 'P3': 1}
    # Generic priority names
    generic_ranks = {'CRITICAL': 4, 'HIGH': 3, 'MEDIUM': 2, 'LOW': 1}

    # Try JIRA format first, then generic
    return jira_ranks.get(priority, generic_ranks.get(priority, 0))


def extract_purpose_from_method(method_name: str) -> str:
    """Extract human-readable purpose from method name"""
    if not method_name:
        return ""

    # Common patterns
    if method_name.startswith('get'):
        entity = method_name[3:]  # Remove 'get'
        return f"{camel_to_title(entity)} retrieval"
    elif method_name.startswith('set') or method_name.startswith('update'):
        prefix_len = 3 if method_name.startswith('set') else 6
        entity = method_name[prefix_len:]
        return f"{camel_to_title(entity)} modification"
    elif method_name.startswith('create') or method_name.startswith('add'):
        prefix_len = 6 if method_name.startswith('create') else 3
        entity = method_name[prefix_len:]
        return f"{camel_to_title(entity)} creation"
    elif method_name.startswith('delete') or method_name.startswith('remove'):
        prefix_len = 6 if method_name.startswith('delete') else 6
        entity = method_name[prefix_len:]
        return f"{camel_to_title(entity)} deletion"
    elif method_name.startswith('validate') or method_name.startswith('verify'):
        prefix_len = 8 if method_name.startswith('validate') else 6
        entity = method_name[prefix_len:]
        return f"{camel_to_title(entity)} validation"
    elif method_name.startswith('fetch'):
        entity = method_name[5:]
        return f"{camel_to_title(entity)} data retrieval"
    else:
        return f"{camel_to_title(method_name)} logic"


def camel_to_title(text: str) -> str:
    """Convert camelCase to Title Case with spaces"""
    import re
    # Insert space before uppercase letters
    result = re.sub(r'([A-Z])', r' \1', text)
    return result.strip().title()


def extract_functionality_description(method_name: str, flow_test_map: dict, unique_flows: set) -> str:
    """Generate detailed functionality description based on method name and impacted flows"""
    # Extract entity/domain from method name
    purpose = extract_purpose_from_method(method_name)

    # Get related flows to add context
    related_flows = []
    for flow in list(unique_flows)[:3]:
        flow_readable = flow.replace('_', ' ').title()
        related_flows.append(flow_readable)

    if related_flows:
        return f"Handles {purpose} for business flows including: {', '.join(related_flows)}. Changes may affect data processing, validation logic, or business rule enforcement in these workflows."
    else:
        return f"Handles {purpose}. See test cases for detailed impact on specific business scenarios."


def format_ria_content_markdown(ria_data: Dict[str, Any], jira_card: str, test_execution_key: Optional[str] = None) -> str:
    """
    Format RIA results as Markdown (for dry-run local files and sub-tasks).
    """
    stage6 = ria_data['stage6']
    summary = ria_data.get('summary', {})

    tests = stage6.get('aggressive_tests', [])
    test_count = len(tests)
    methods = summary.get('changed_methods', 1)

    unique_flows = set()
    for test in tests:
        for flow in test.get('matched_flows', []):
            flow_name = flow.get('flow_name')
            if flow_name:
                unique_flows.add(flow_name)

    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    jira_base_url = os.getenv('JIRA_BASE_URL', '')

    content = f"""
# Regression Impact Analysis (RIA)

**Parent Card:** [{jira_card}]({jira_base_url}/browse/{jira_card})
**Generated:** {timestamp}
**Pipeline:** RIA v2

---

## Summary

- **Changed Methods:** {methods}
- **Impacted Flows:** {len(unique_flows)}
- **Recommended Tests:** {test_count}
"""

    if test_execution_key:
        content += f"- **Test Execution:** [{test_execution_key}]({jira_base_url}/browse/{test_execution_key})\n"

    content += "\n---\n\n## Impacted Flows\n\n"

    for flow in sorted(unique_flows):
        content += f"- {flow}\n"

    content += "\n---\n\n## Recommended Test Cases\n\n"

    if test_execution_key:
        # Test Execution created - reference it
        content += f"""All **{test_count} recommended test cases** have been mapped to Test Execution: **[{test_execution_key}]({jira_base_url}/browse/{test_execution_key})**

Click the Test Execution link above to view all test cases, execute them, and track results.
"""
    else:
        # No Test Execution - list first few tests
        content += f"Total recommended tests: {test_count}. See attached HTML report for full details.\n\n"

        for test in tests[:10]:
            test_id = test.get('issue_key', 'N/A')
            summary_text = test.get('summary', 'No summary')[:80]
            test_link = f"[{test_id}]({jira_base_url}/browse/{test_id})"
            content += f"- {test_link} - {summary_text}\n"

        if test_count > 10:
            content += f"\n*... and {test_count - 10} more tests. See attached HTML report for complete list.*\n"

    content += f"""

---

## Analysis Context

**Changed Methods:**
"""

    per_method = summary.get('per_method', [])
    if per_method:
        for idx, method_info in enumerate(per_method, 1):
            method_name = method_info.get('method_name', 'Unknown')
            file_path = method_info.get('file_path', 'Unknown')
            class_name = method_info.get('class_name', '')
            if class_name:
                content += f"{idx}. `{class_name}.{method_name}`\n"
            else:
                content += f"{idx}. `{method_name}`\n"
            content += f"   - File: `{file_path}`\n"
    else:
        content += "- Method details not available\n"

    content += f"""

---

## Attachments

📎 **stage6_aggressive_tests.json** - Full test list with all metadata ({test_count} tests)
📎 **RIA_Report.html** - Interactive dark-themed HTML report with filters and sorting
📎 **consolidated_summary.json** - Per-method breakdown and analysis summary

*Note: Files are located in `.github/RIA_OUTPUT/` directory. In dry-run mode, manually attach these files to the JIRA card.*

---

## Next Steps

1. **Review recommended tests** - Click on Test IDs above to view full details
2. **Run tests locally** - Execute tests in development environment
3. **Verify test coverage** - Ensure all impacted flows are covered
4. **Report results** - Document test execution results in this JIRA card

---

*Generated by RIA Pipeline v2*
"""

    return content


# ── Main Workflow ────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description='JIRA Extension Point - Document RIA results in JIRA'
    )
    parser.add_argument('--jira-card', required=True,
                        help='JIRA card number (e.g., CXWFM-12345)')
    parser.add_argument('--config', default=str(CONFIG_FILE),
                        help='Path to ria_config.env')
    parser.add_argument('--dry-run', action='store_true',
                        help='Save report locally only (skip JIRA update). '
                             'Useful for review before posting to JIRA.')

    args = parser.parse_args()

    print("=" * 80)
    print("JIRA EXTENSION POINT")
    print("=" * 80)
    print(f"Parent JIRA Card: {args.jira_card}")
    if args.dry_run:
        print("Mode: DRY RUN (local file only)")
    print()

    # Load RIA results
    print("[RIA] Loading pipeline results...")
    ria_data = load_ria_results()
    print(f"[RIA] Loaded {len(ria_data['stage6'].get('aggressive_tests', []))} tests")

    # DRY RUN MODE: Generate markdown and save locally
    if args.dry_run:
        ria_content_md = format_ria_content_markdown(ria_data, args.jira_card, test_execution_key=None)

        REPORTS_DIR.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        filename = f"RIA-{args.jira_card}-{timestamp}.md"
        filepath = REPORTS_DIR / filename

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(ria_content_md)

        print()
        print("=" * 80)
        print("✓ DRY RUN - REPORT SAVED LOCALLY")
        print("=" * 80)
        print(f"File: {filepath}")
        print(f"Size: {filepath.stat().st_size} bytes")
        print()
        print("Review the report and run without --dry-run to post to JIRA:")
        print(f"  python3 jira_extension.py --jira-card {args.jira_card}")
        print()
        return

    # LIVE MODE: Authenticate and post to JIRA
    # Load configuration
    config = load_config()

    if 'XRAY_CLIENT_ID' not in config or 'XRAY_CLIENT_SECRET' not in config:
        print("[ERROR] XRAY_CLIENT_ID and XRAY_CLIENT_SECRET not found in config")
        print(f"[ERROR] Check: {CONFIG_FILE}")
        sys.exit(1)

    if 'JIRA_USER' not in config or 'JIRA_API_TOKEN' not in config:
        print("[ERROR] JIRA_USER and JIRA_API_TOKEN not found in config")
        print(f"[ERROR] Check: {CONFIG_FILE}")
        sys.exit(1)

    if 'JIRA_BASE_URL' not in config:
        print("[ERROR] JIRA_BASE_URL not found in config")
        print(f"[ERROR] Check: {CONFIG_FILE}")
        sys.exit(1)

    # Authenticate with Xray
    xray_token = get_xray_token(config['XRAY_CLIENT_ID'], config['XRAY_CLIENT_SECRET'])

    # Initialize JIRA client with both JIRA and Xray credentials
    jira = JiraClient(
        jira_user=config['JIRA_USER'],
        jira_token=config['JIRA_API_TOKEN'],
        xray_token=xray_token,
        jira_base_url=config['JIRA_BASE_URL']
    )

    # Check if parent card exists
    parent_issue = jira.get_issue(args.jira_card)
    if not parent_issue:
        print(f"[ERROR] Cannot proceed without valid JIRA card")
        sys.exit(1)

    # Strategy: Create Test Execution FIRST, then document with Test Execution link
    print()
    execution_key = None

    # Step 1: Create Test Execution with all recommended tests
    test_keys = [test.get('issue_key') for test in ria_data['stage6'].get('aggressive_tests', [])]
    test_keys = [k for k in test_keys if k and k != 'N/A']  # Filter valid keys

    if test_keys:
        execution_summary = f"RIA Test Execution for {args.jira_card}"
        execution_desc = f"Automated Test Execution created by RIA Pipeline for regression testing of {args.jira_card}"

        print(f"[Xray] Creating Test Execution with {len(test_keys)} tests...")
        execution_key = jira.create_test_execution(
            args.jira_card,
            execution_summary,
            execution_desc,
            test_keys
        )

        if execution_key:
            print()
            print("=" * 80)
            print("✓ TEST EXECUTION CREATED")
            print("=" * 80)
            print(f"Parent Card: {args.jira_card}")
            print(f"Test Execution: {execution_key}")
            print(f"Tests Mapped: {len(test_keys)}")
            jira_base_url = os.getenv('JIRA_BASE_URL', '')
            print(f"URL: {jira_base_url}/browse/{execution_key}")
            print()
        else:
            print(f"[WARN] Failed to create Test Execution - continuing with documentation...")
    else:
        print(f"[WARN] No valid test keys found - skipping Test Execution creation")

    # Step 2: Document in Quality tab or sub-task (include Test Execution link)
    print(f"[JIRA] Checking for Quality tab in {args.jira_card}...")
    has_quality = jira.has_quality_tab(args.jira_card)

    success = False
    local_backup = None

    if has_quality:
        # Attempt: Update Quality field with ADF format
        print(f"[JIRA] Quality tab (customfield_12794) is available - generating ADF content...")
        ria_content_adf = format_ria_content_adf(ria_data, args.jira_card, test_execution_key=execution_key)

        print(f"[JIRA] Updating Quality tab...")
        success = jira.update_quality_field(args.jira_card, ria_content_adf)

        if success:
            print()
            print("=" * 80)
            print("✓ RIA RESULTS DOCUMENTED IN QUALITY TAB")
            print("=" * 80)
            print(f"JIRA Card: {args.jira_card}")
            print(f"Field: customfield_12794 (Quality tab)")
            print(f"Format: Atlassian Document Format (ADF)")
            if execution_key:
                print(f"Test Execution: {execution_key} (linked in report)")

            # Step 3: Attach files
            print()
            print("[JIRA] Attaching RIA output files...")
            attachments = [
                OUTPUT_DIR / 'stage6_aggressive_tests.json',
                OUTPUT_DIR / 'RIA_Report.html',
                OUTPUT_DIR / 'consolidated_summary.json'
            ]

            for file_path in attachments:
                if file_path.exists():
                    jira.attach_file(args.jira_card, file_path)
            print()
        else:
            # Quality field update failed - save locally and try sub-task
            ria_content_md = format_ria_content_markdown(ria_data, args.jira_card, test_execution_key=execution_key)
            reason = "Quality field update failed (HTTP error or permission issue)"
            local_backup = save_report_locally(args.jira_card, ria_content_md, reason)
            print(f"[WARN] Quality tab update failed - trying sub-task fallback...")

    if not success:
        # Fallback: Create sub-task (either Quality tab unavailable or update failed)
        # Sub-tasks use Markdown description, not ADF
        print(f"[JIRA] Creating RIA documentation sub-task...")
        ria_content_md = format_ria_content_markdown(ria_data, args.jira_card, test_execution_key=execution_key)

        subtask_summary = f"RIA: Documentation for {args.jira_card}"
        subtask_key = jira.create_subtask(args.jira_card, subtask_summary, ria_content_md)

        if subtask_key:
            print()
            print("=" * 80)
            print("✓ RIA DOCUMENTATION SUB-TASK CREATED")
            print("=" * 80)
            print(f"Parent Card: {args.jira_card}")
            print(f"Sub-task: {subtask_key}")
            jira_base_url = os.getenv('JIRA_BASE_URL', '')
            print(f"URL: {jira_base_url}/browse/{subtask_key}")
            if execution_key:
                print(f"Test Execution: {execution_key} (linked in report)")

            # Attach files to sub-task
            print()
            print("[JIRA] Attaching RIA output files to sub-task...")
            attachments = [
                OUTPUT_DIR / 'stage6_aggressive_tests.json',
                OUTPUT_DIR / 'RIA_Report.html',
                OUTPUT_DIR / 'consolidated_summary.json'
            ]

            for file_path in attachments:
                if file_path.exists():
                    jira.attach_file(subtask_key, file_path)
            print()

            success = True
        else:
            # Both Quality tab AND sub-task failed - save locally
            if not local_backup:
                ria_content_md = format_ria_content_markdown(ria_data, args.jira_card, test_execution_key=execution_key)
                reason = "Quality tab unavailable and sub-task creation failed"
                local_backup = save_report_locally(args.jira_card, ria_content_md, reason)

            print()
            print("=" * 80)
            print("⚠ JIRA DOCUMENTATION FAILED - REPORT SAVED LOCALLY")
            print("=" * 80)
            print(f"Local file: {local_backup}")
            print(f"[WARN] Could not document in JIRA, but Test Execution may have been created")
            print(f"[WARN] Please manually copy the report to JIRA card {args.jira_card}")

    # Final JIRA step: tag the card so it is discoverable as RIA-automated work.
    # Done last so it runs after the Test Execution and documentation steps.
    # This appends to the existing labels (does not overwrite them).
    jira.add_label(args.jira_card, "AI-AUTO-RIA")

    # Success summary
    if local_backup:
        print()
        print(f"Note: Local backup available at: {local_backup}")


if __name__ == '__main__':
    main()
