#!/usr/bin/env python3
"""
Organize tests into Xray Test Repository folders using the Xray Cloud GraphQL API.
Automatically creates folders if they don't exist and adds tests to them.

Usage: python xray_organize_tests.py --project <KEY> --functionality "<name>" --tests <TEST-KEY1> [TEST-KEY2 ...]
       python xray_organize_tests.py --project <KEY> --functionality "<name>" --parent "<parent_folder_name>" --tests <TEST-KEY1>

Required Environment Variables:
  XRAY_CLIENT_ID, XRAY_CLIENT_SECRET
  CONFLUENCE_USERNAME, CONFLUENCE_TOKEN (for Jira project ID lookup)
"""

import sys
import os
import json
import requests
import urllib3
from requests.auth import HTTPBasicAuth
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'atlassian-api-clients', 'scripts'))
from xray_client import xray_graphql

def _ssl_verify():
    val = os.environ.get("SSL_VERIFY", "true").strip().lower()
    if val in ("false", "0", "no"):
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return False
    return True



def get_arg(args, flag):
    try:
        idx = args.index(flag)
        return args[idx + 1] if idx + 1 < len(args) else None
    except ValueError:
        return None


def get_all_args_after_flag(args, flag):
    """Get all arguments after a flag until the next flag."""
    try:
        idx = args.index(flag)
        values = []
        for i in range(idx + 1, len(args)):
            if args[i].startswith("--"):
                break
            values.append(args[i])
        return values
    except ValueError:
        return []


def get_project_id(project_key):
    """Get numeric project ID from project key using Jira REST API."""
    username = os.environ.get("CONFLUENCE_USERNAME")
    token = os.environ.get("CONFLUENCE_TOKEN")
    base_url = os.environ.get("JIRA_BASE_URL", "https://nice-ce-cxone-prod.atlassian.net")
    
    if not username or not token:
        raise Exception("CONFLUENCE_USERNAME and CONFLUENCE_TOKEN environment variables required")
    
    response = requests.get(
        f"{base_url}/rest/api/3/project/{project_key}",
        auth=HTTPBasicAuth(username, token),
        headers={"Accept": "application/json"},
        verify=_ssl_verify()
    )
    response.raise_for_status()
    return response.json().get("id")


def resolve_test_issue_ids(test_keys):
    """Resolve test keys to Xray internal issue IDs."""
    jql = f"key in ({','.join(test_keys)})"
    data = xray_graphql(
        "query($jql: String!, $limit: Int!) { getTests(jql: $jql, limit: $limit) { results { issueId jira(fields: [\"key\"]) } } }",
        {"jql": jql, "limit": len(test_keys)}
    )
    
    tests = (data or {}).get("getTests", {}).get("results", [])
    test_map = {}
    for test in tests:
        jira = test.get("jira") or {}
        key = jira.get("key")
        if key:
            test_map[key] = test["issueId"]
    
    return test_map


def search_folder_recursive(folders, target_name):
    """Recursively search for a folder by name in the folder tree."""
    for folder in folders:
        # Check if this folder matches
        if folder.get("name") == target_name:
            return folder.get("path")
        
        # Recursively search subfolders
        subfolders = folder.get("folders", [])
        if subfolders:
            found_path = search_folder_recursive(subfolders, target_name)
            if found_path:
                return found_path
    
    return None


def create_folder_if_not_exists(project_id, folder_path, folder_name):
    """Create folder in Test Repository using Xray GraphQL createFolder mutation.
    First searches all subfolders recursively to find existing folder with same name."""
    
    # Step 1: Try exact path (fast path)
    try:
        data = xray_graphql(
            """query GetFolder($projectId: String!, $path: String!) {
                getFolder(projectId: $projectId, path: $path) {
                    name
                    path
                }
            }""",
            {"projectId": project_id, "path": folder_path}
        )
        
        if data and data.get("getFolder"):
            print(f"  → Found at exact path: {folder_path}", file=sys.stderr)
            return {"exists": True, "path": folder_path, "searched": "exact_path"}
    except:
        pass  # Folder doesn't exist at exact path
    
    # Step 2: Search entire folder tree for matching folder name
    try:
        print(f"  → Searching all subfolders for '{folder_name}'...", file=sys.stderr)
        data = xray_graphql(
            """query GetFolder($projectId: String!, $path: String!) {
                getFolder(projectId: $projectId, path: $path) {
                    name
                    path
                    folders
                }
            }""",
            {"projectId": project_id, "path": "/"}
        )
        
        root_folder = data.get("getFolder")
        if root_folder:
            all_folders = root_folder.get("folders", [])
            existing_path = search_folder_recursive(all_folders, folder_name)
            
            if existing_path:
                print(f"  → Found existing folder: {existing_path}", file=sys.stderr)
                return {"exists": True, "path": existing_path, "searched": "recursive"}
            else:
                print(f"  → No existing folder named '{folder_name}' found", file=sys.stderr)
    except Exception as e:
        print(f"  → Warning: Could not complete folder search: {str(e)}", file=sys.stderr)
    
    # Step 3: Folder not found anywhere, create it at specified path
    print(f"  → Creating new folder: {folder_path}", file=sys.stderr)
    data = xray_graphql(
        """mutation CreateFolder($projectId: String!, $path: String!) {
            createFolder(projectId: $projectId, path: $path) {
                folder {
                    name
                    path
                }
                warnings
            }
        }""",
        {"projectId": project_id, "path": folder_path}
    )
    
    result = (data or {}).get("createFolder", {})
    folder = result.get("folder")
    
    if folder:
        return {"exists": False, "created": True, "path": folder.get("path")}
    else:
        raise Exception(f"Failed to create folder: {result.get('warnings')}")



def main():
    args = sys.argv[1:]
    project_key = get_arg(args, "--project")
    functionality = get_arg(args, "--functionality")
    parent_folder = get_arg(args, "--parent")
    test_keys = get_all_args_after_flag(args, "--tests")

    if not project_key or not test_keys:
        print('Usage: python xray_organize_tests.py --project <KEY> [--functionality "<name>"] [--parent "<parent>"] --tests <TEST-KEY1> [TEST-KEY2 ...]', file=sys.stderr)
        print('  If --functionality is not provided, you will be prompted to enter a folder name.', file=sys.stderr)
        sys.exit(1)
    
    # Interactive mode: prompt for folder name if not provided
    if not functionality:
        print("\n" + "="*60, file=sys.stderr)
        print("ORGANIZE TESTS INTO TEST REPOSITORY FOLDER", file=sys.stderr)
        print("="*60, file=sys.stderr)
        print(f"\nTests to organize: {', '.join(test_keys)}", file=sys.stderr)
        print(f"\nEnter folder name to add tests to:", file=sys.stderr)
        print("  (e.g., 'Analytics Policies', 'Login Tests', 'API Tests')", file=sys.stderr)
        print("\n> ", end='', file=sys.stderr)
        
        try:
            functionality = input().strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[ERROR] Operation cancelled by user", file=sys.stderr)
            sys.exit(1)
        
        if not functionality:
            print("[ERROR] Folder name cannot be empty", file=sys.stderr)
            sys.exit(1)
        
        print(f"\n[INFO] Searching for folder: '{functionality}'...", file=sys.stderr)

    try:
        # Build folder path
        if parent_folder:
            folder_path = f"/{parent_folder}/{functionality}"
        else:
            folder_path = f"/{functionality}"
        
        print(f"[INFO] Organizing {len(test_keys)} test(s) into '{folder_path}' folder...", file=sys.stderr)
        
        # Get project ID from project key
        project_id = get_project_id(project_key)
        print(f"[INFO] Resolved project {project_key} to ID: {project_id}", file=sys.stderr)
        
        # Create folder if it doesn't exist, or find existing folder with same name
        folder_result = create_folder_if_not_exists(project_id, folder_path, functionality)
        actual_folder_path = folder_result.get("path")
        
        if folder_result.get("created"):
            print(f"\n✓ Created new folder: {actual_folder_path}", file=sys.stderr)
        elif folder_result.get("searched") == "recursive":
            print(f"\n✓ Found existing folder at: {actual_folder_path}", file=sys.stderr)
            print(f"  (Searched through all subfolders)", file=sys.stderr)
        else:
            print(f"\n✓ Using existing folder: {actual_folder_path}", file=sys.stderr)
        
        # Resolve test keys to issue IDs
        print(f"\n[INFO] Resolving test issue IDs...", file=sys.stderr)
        test_map = resolve_test_issue_ids(test_keys)
        
        # Use updateTestFolder mutation to add each test to the folder
        print(f"\n[INFO] Adding tests to folder...", file=sys.stderr)
        organized_tests = []
        failed_tests = []
        
        for idx, test_key in enumerate(test_keys, 1):
            issue_id = test_map.get(test_key)
            if not issue_id:
                failed_tests.append({"key": test_key, "error": "Test not found or not resolved"})
                print(f"  [{idx}/{len(test_keys)}] ✗ {test_key} - Not found", file=sys.stderr)
                continue
            
            try:
                # Use updateTestFolder mutation to move test to folder
                data = xray_graphql(
                    """mutation UpdateTestFolder($issueId: String!, $folderPath: String!) {
                        updateTestFolder(issueId: $issueId, folderPath: $folderPath)
                    }""",
                    {"issueId": issue_id, "folderPath": actual_folder_path}
                )
                
                organized_tests.append(test_key)
                print(f"  [{idx}/{len(test_keys)}] ✓ {test_key}", file=sys.stderr)
            except Exception as e:
                failed_tests.append({"key": test_key, "error": str(e)})
                print(f"  [{idx}/{len(test_keys)}] ✗ {test_key} - {str(e)}", file=sys.stderr)
        
        # Print summary
        print(f"\n" + "="*60, file=sys.stderr)
        print(f"SUMMARY", file=sys.stderr)
        print(f"="*60, file=sys.stderr)
        print(f"Folder: {actual_folder_path}", file=sys.stderr)
        print(f"Tests organized: {len(organized_tests)}/{len(test_keys)}", file=sys.stderr)
        if failed_tests:
            print(f"Failed: {len(failed_tests)}", file=sys.stderr)
        print(f"="*60 + "\n", file=sys.stderr)
        
        # JSON output for programmatic consumption
        result = {
            "success": len(organized_tests) > 0,
            "organized": len(organized_tests),
            "folderPath": actual_folder_path,
            "tests": organized_tests,
            "method": "xray-test-repository"
        }
        
        if failed_tests:
            result["failed"] = failed_tests
            result["partialSuccess"] = True
        
        print(json.dumps(result, indent=2))

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
