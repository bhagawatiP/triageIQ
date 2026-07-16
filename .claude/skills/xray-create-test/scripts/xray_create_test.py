#!/usr/bin/env python3
"""
Create a new Xray Test issue in Jira via the Xray Cloud GraphQL API.

Usage: python xray_create_test.py --project <KEY> --summary "<text>" [options]
Options:
  --type Manual|Cucumber|Generic|"Automated[Generic]"  (default: Manual)
  --description "<text>"   Inline description (or pipe via stdin)
  --functionality "<name>" Organize test into Test Repository folder (auto-creates if needed)
  --parent "<name>"        Parent folder for nested organization (requires --functionality)

Required Environment Variables:
  XRAY_CLIENT_ID, XRAY_CLIENT_SECRET
"""

import sys
import os
import json
import subprocess
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'atlassian-api-clients', 'scripts'))
from xray_client import xray_graphql


def get_arg(args, flag):
    try:
        idx = args.index(flag)
        return args[idx + 1] if idx + 1 < len(args) else None
    except ValueError:
        return None


def read_stdin():
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read().strip()


def organize_test_into_folder(project_key, test_key, functionality, parent_folder=None):
    """Organize test into Test Repository folder using xray_organize_tests.py"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    organize_script = os.path.join(script_dir, '..', '..', 'xray-test-repository', 'scripts', 'xray_organize_tests.py')
    
    cmd = [sys.executable, organize_script, 
           "--project", project_key,
           "--functionality", functionality,
           "--tests", test_key]
    
    if parent_folder:
        cmd.extend(["--parent", parent_folder])
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"[WARNING] Failed to organize test into folder: {result.stderr}", file=sys.stderr)
        return None
    
    try:
        return json.loads(result.stdout)
    except:
        return None


def main():
    args = sys.argv[1:]
    project_key = get_arg(args, "--project")
    summary = get_arg(args, "--summary")

    if not project_key or not summary:
        print('Usage: python xray_create_test.py --project <KEY> --summary "<text>" [--type TYPE] [--description "<text>"] [--functionality "<name>"] [--parent "<name>"]', file=sys.stderr)
        sys.exit(1)

    test_type = get_arg(args, "--type") or "Manual"
    description_inline = get_arg(args, "--description")
    priority = get_arg(args, "--priority")
    fix_version = get_arg(args, "--fix-version")
    team = get_arg(args, "--team")
    functionality = get_arg(args, "--functionality")
    parent_folder = get_arg(args, "--parent")

    description = description_inline or read_stdin() or None

    try:
        jira_fields = {
            "summary": summary,
            "project": {"key": project_key},
            "labels": ["mcp-auto-generated"],
        }
        if description:
            jira_fields["description"] = description
        if priority:
            jira_fields["priority"] = {"name": priority}
        if fix_version:
            jira_fields["fixVersions"] = [{"name": fix_version}]
        if team:
            jira_fields["customfield_10098"] = {"value": team}

        variables = {
            "jira": {"fields": jira_fields},
            "testType": {"name": test_type},
        }

        data = xray_graphql(
            """mutation CreateTest($jira: JSON!, $testType: UpdateTestTypeInput) {
                createTest(jira: $jira, testType: $testType) {
                    test {
                        issueId
                        jira(fields: ["key", "summary", "labels"])
                    }
                    warnings
                }
            }""",
            variables,
        )

        result = (data or {}).get("createTest", {})
        if not result.get("test"):
            print(json.dumps({"success": False, "error": "No test returned from API", "raw": data}, indent=2))
            sys.exit(1)

        test = result["test"]
        jira = test.get("jira") or {}
        test_key = jira.get("key")
        
        output = {
            "success": True,
            "issueId": test["issueId"],
            "key": test_key,
            "summary": jira.get("summary", summary),
            "labels": jira.get("labels", ["mcp-auto-generated"]),
            "testType": test_type,
            "warnings": result.get("warnings") or [],
        }
        
        # Organize into Test Repository folder if requested
        if functionality and test_key:
            print(f"[INFO] Organizing test {test_key} into '{functionality}' folder...", file=sys.stderr)
            organize_result = organize_test_into_folder(project_key, test_key, functionality, parent_folder)
            if organize_result and organize_result.get("success"):
                output["testRepository"] = {
                    "organized": True,
                    "folderPath": organize_result.get("folderPath"),
                    "method": organize_result.get("method", "xray-test-repository")
                }
                print(f"[INFO] Test organized in folder: {organize_result.get('folderPath')}", file=sys.stderr)
            else:
                output["testRepository"] = {
                    "organized": False,
                    "error": organize_result.get("error") if organize_result else "Failed to organize test"
                }
        
        print(json.dumps(output, indent=2))
    except Exception as e:
        print(json.dumps({"success": False, "error": str(e)}, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
