#!/usr/bin/env python3
"""
Add tests to a folder in the Xray Test Repository via the Xray Cloud GraphQL API.

Usage: python xray_add_tests_to_folder.py --project <KEY> --folder <folder_id_or_path> --tests <TEST-KEY1> [TEST-KEY2 ...]

Required Environment Variables:
  XRAY_CLIENT_ID, XRAY_CLIENT_SECRET
"""

import sys
import os
import json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'atlassian-api-clients', 'scripts'))
from xray_client import xray_graphql


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


def resolve_folder_id(project_key, folder_identifier):
    """
    Resolve folder identifier to folder ID.
    If it starts with '/', treat as path and look up ID.
    Otherwise, assume it's already an ID.
    """
    if folder_identifier.startswith("/"):
        # It's a path - fetch all folders and find matching path
        data = xray_graphql(
            """query GetTestRepositoryFolders($projectKey: String!) {
                getTestRepositoryFolders(projectKey: $projectKey) {
                    folders {
                        id
                        name
                        parentId
                    }
                }
            }""",
            {"projectKey": project_key},
        )
        
        folders = (data or {}).get("getTestRepositoryFolders", {}).get("folders") or []
        
        # Build paths and find match
        def build_path(folder, all_folders):
            if not folder.get("parentId"):
                return "/" + folder["name"]
            parent = next((f for f in all_folders if f["id"] == folder["parentId"]), None)
            if parent:
                return build_path(parent, all_folders) + "/" + folder["name"]
            return "/" + folder["name"]
        
        for folder in folders:
            path = build_path(folder, folders)
            if path == folder_identifier:
                return folder["id"], path
        
        raise Exception(f"Folder not found with path: {folder_identifier}")
    else:
        # Assume it's already an ID
        return folder_identifier, None


def resolve_test_ids(project_key, test_keys):
    """Convert test keys to internal Xray test IDs."""
    if not test_keys:
        return []
    
    jql = f"issue in ({','.join(test_keys)}) AND project = {project_key}"
    
    data = xray_graphql(
        "query($jql: String!, $limit: Int!) { getTests(jql: $jql, limit: $limit) { results { issueId jira(fields: [\"key\"]) } } }",
        {"jql": jql, "limit": len(test_keys)},
    )
    
    tests = (data or {}).get("getTests", {}).get("results") or []
    
    test_map = {}
    for test in tests:
        jira = test.get("jira") or {}
        key = jira.get("key")
        if key:
            test_map[key] = test["issueId"]
    
    # Build list maintaining original order
    test_ids = []
    for key in test_keys:
        if key in test_map:
            test_ids.append(test_map[key])
        else:
            print(f"Warning: Test {key} not found", file=sys.stderr)
    
    return test_ids


def main():
    args = sys.argv[1:]
    project_key = get_arg(args, "--project")
    folder_identifier = get_arg(args, "--folder")
    test_keys = get_all_args_after_flag(args, "--tests")

    if not project_key or not folder_identifier or not test_keys:
        print('Usage: python xray_add_tests_to_folder.py --project <KEY> --folder <folder_id_or_path> --tests <TEST-KEY1> [TEST-KEY2 ...]', file=sys.stderr)
        sys.exit(1)

    try:
        # Resolve folder ID
        folder_id, folder_path = resolve_folder_id(project_key, folder_identifier)
        
        # Resolve test IDs from keys
        test_ids = resolve_test_ids(project_key, test_keys)
        
        if not test_ids:
            print(json.dumps({"success": False, "error": "No valid tests found"}, indent=2))
            sys.exit(1)

        # Add tests to folder
        # Note: The exact GraphQL schema may vary - adjust based on Xray Cloud API version
        variables = {
            "folderId": folder_id,
            "testIds": test_ids
        }

        data = xray_graphql(
            """mutation AddTestsToFolder($folderId: String!, $testIds: [String]!) {
                addTestsToFolder(folderId: $folderId, testIds: $testIds) {
                    success
                    folder {
                        id
                        name
                    }
                }
            }""",
            variables,
        )

        result = (data or {}).get("addTestsToFolder", {})

        if not result.get("success"):
            print(json.dumps({"success": False, "error": "Failed to add tests to folder", "raw": data}, indent=2))
            sys.exit(1)

        folder_info = result.get("folder") or {}
        
        print(json.dumps({
            "success": True,
            "folderId": folder_id,
            "folderName": folder_info.get("name"),
            "folderPath": folder_path or f"ID:{folder_id}",
            "testsAdded": test_keys,
            "count": len(test_keys)
        }, indent=2))

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
