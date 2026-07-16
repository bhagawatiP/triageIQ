#!/usr/bin/env python3
"""
Create a new folder in the Xray Test Repository via the Xray Cloud GraphQL API.

Usage: python xray_create_folder.py --project <KEY> --name "<folder name>" [--parent "<parent folder name>"]

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


def find_folder_path(project_id, parent_name):
    """Find the full path of a folder by name by searching the entire tree."""
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

    def search(folders, name, current_path=""):
        for folder in folders:
            path = current_path + "/" + folder.get("name", "")
            if folder.get("name") == name:
                return path
            sub = folder.get("folders", [])
            if sub:
                found = search(sub, name, path)
                if found:
                    return found
        return None

    root = (data or {}).get("getFolder", {})
    return search(root.get("folders", []), parent_name)


def main():
    args = sys.argv[1:]
    project_key = get_arg(args, "--project")
    folder_name = get_arg(args, "--name")
    parent_name = get_arg(args, "--parent")

    if not project_key or not folder_name:
        print('Usage: python xray_create_folder.py --project <KEY> --name "<folder name>" [--parent "<parent folder name>"]', file=sys.stderr)
        sys.exit(1)

    try:
        project_id = get_project_id(project_key)
        print(f"[INFO] Resolved project {project_key} to ID: {project_id}", file=sys.stderr)

        if parent_name:
            parent_path = find_folder_path(project_id, parent_name)
            if not parent_path:
                print(json.dumps({"success": False, "error": f"Parent folder '{parent_name}' not found."}, indent=2))
                sys.exit(1)
            folder_path = parent_path + "/" + folder_name
        else:
            folder_path = "/" + folder_name

        print(f"[INFO] Creating folder: {folder_path}", file=sys.stderr)

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

        if not folder:
            print(json.dumps({"success": False, "error": "Folder creation failed", "warnings": result.get("warnings")}, indent=2))
            sys.exit(1)

        print(json.dumps({
            "success": True,
            "name": folder.get("name"),
            "path": folder.get("path")
        }, indent=2))

    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
