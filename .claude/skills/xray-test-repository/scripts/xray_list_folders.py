#!/usr/bin/env python3
"""
List all folders in the Xray Test Repository via the Xray Cloud GraphQL API.

Usage: python xray_list_folders.py --project <KEY> [--parent "<folder_name>"]

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


def flatten_folders(folders, parent_path=""):
    """Recursively flatten the folder tree into a list of paths."""
    result = []
    for folder in folders:
        name = folder.get("name", "")
        path = parent_path + "/" + name if parent_path else "/" + name
        result.append(path)
        subfolders = folder.get("folders", [])
        if subfolders:
            result.extend(flatten_folders(subfolders, path))
    return result


def find_folder_by_name(folders, name, parent_path=""):
    """Find a folder subtree by name, returning its subfolders."""
    for folder in folders:
        current_path = parent_path + "/" + folder.get("name", "")
        if folder.get("name") == name:
            return folder.get("folders", []), current_path
        subfolders = folder.get("folders", [])
        if subfolders:
            found, path = find_folder_by_name(subfolders, name, current_path)
            if found is not None:
                return found, path
    return None, None


def main():
    args = sys.argv[1:]
    project_key = get_arg(args, "--project")
    parent_name = get_arg(args, "--parent")

    if not project_key:
        print('Usage: python xray_list_folders.py --project <KEY> [--parent "<folder_name>"]', file=sys.stderr)
        sys.exit(1)

    try:
        project_id = get_project_id(project_key)
        print(f"[INFO] Resolved project {project_key} to ID: {project_id}", file=sys.stderr)

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

        root = (data or {}).get("getFolder")
        if not root:
            print(json.dumps({"success": False, "error": "Could not retrieve folder tree."}, indent=2))
            sys.exit(1)

        all_folders = root.get("folders", [])

        if parent_name:
            sub, base_path = find_folder_by_name(all_folders, parent_name)
            if sub is None:
                print(json.dumps({"success": False, "error": f"Folder '{parent_name}' not found."}, indent=2))
                sys.exit(1)
            paths = [base_path] + flatten_folders(sub, base_path)
        else:
            paths = flatten_folders(all_folders)

        paths.sort()

        print(json.dumps({
            "success": True,
            "project": project_key,
            "folders": paths,
            "totalFolders": len(paths)
        }, indent=2))

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()