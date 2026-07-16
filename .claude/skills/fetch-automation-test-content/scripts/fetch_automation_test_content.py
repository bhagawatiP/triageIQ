#!/usr/bin/env python3
"""
Read the actual body content of specific automation test blocks (already
located by scan-automation-repo / match-automation-tests-by-id) from the
still-cloned repo, for content-based duplicate judgment. Reads from disk
only - no network access.

Extracts the test body starting at the given line using a pragmatic
heuristic per language:
  - curly-brace languages (.ts/.tsx/.js/.jsx/.java/.cs): balance braces from
    the first "{" at/after the start line until they close, capped at
    MAX_LINES as a safety valve.
  - Python (.py): capture lines while their indentation stays greater than
    the declaration line's indentation.
  - Cucumber (.feature): capture until the next "Scenario"/"Examples"/
    "Feature" keyword line or end of file.
  - anything else: a fixed MAX_LINES window as a fallback.

Called once per test (results should be cached by the caller, same as
fetch-test-content on the Jira side) - never re-read for the same ref during
duplicate detection.

Usage:
  python fetch_automation_test_content.py --repo-path <path> --refs-file <path to JSON array of {filePath, lineNo}>
"""

import sys
import os
import json
import argparse

MAX_LINES = 200


def _extract_curly(lines, start_idx):
    depth = 0
    started = False
    out = []
    for i in range(start_idx, min(len(lines), start_idx + MAX_LINES)):
        line = lines[i]
        out.append(line)
        depth += line.count("{") - line.count("}")
        if "{" in line:
            started = True
        if started and depth <= 0:
            break
    return "\n".join(out)


def _extract_python(lines, start_idx):
    def indent_of(s):
        return len(s) - len(s.lstrip(" "))

    base_indent = indent_of(lines[start_idx])
    out = [lines[start_idx]]
    for i in range(start_idx + 1, min(len(lines), start_idx + MAX_LINES)):
        line = lines[i]
        if line.strip() == "":
            out.append(line)
            continue
        if indent_of(line) <= base_indent:
            break
        out.append(line)
    return "\n".join(out)


def _extract_feature(lines, start_idx):
    out = [lines[start_idx]]
    for i in range(start_idx + 1, min(len(lines), start_idx + MAX_LINES)):
        stripped = lines[i].strip()
        if stripped.startswith("Scenario") or stripped.startswith("Feature") or stripped.startswith("Examples"):
            break
        out.append(lines[i])
    return "\n".join(out)


def extract_body(filepath, line_no):
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.read().splitlines()
    start_idx = max(0, line_no - 1)
    if start_idx >= len(lines):
        return ""
    ext = os.path.splitext(filepath)[1]
    if ext in (".ts", ".tsx", ".js", ".jsx", ".java", ".cs"):
        return _extract_curly(lines, start_idx)
    if ext == ".py":
        return _extract_python(lines, start_idx)
    if ext == ".feature":
        return _extract_feature(lines, start_idx)
    return "\n".join(lines[start_idx:min(len(lines), start_idx + MAX_LINES)])


def main():
    parser = argparse.ArgumentParser(description="Read automation test body content from a cloned repo (local, no network).")
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--refs-file", required=True, help="Path to a JSON array of {filePath, lineNo}")
    args = parser.parse_args()

    if not os.path.isdir(args.repo_path):
        print(json.dumps({"success": False, "error": f"repo path not found: {args.repo_path}"}, indent=2))
        sys.exit(1)
    with open(args.refs_file, "r", encoding="utf-8") as f:
        refs = json.load(f)

    out = []
    for ref in refs:
        full_path = os.path.join(args.repo_path, ref.get("filePath", ""))
        try:
            body = extract_body(full_path, int(ref.get("lineNo") or 1))
        except OSError as e:
            body = ""
            ref = dict(ref, error=str(e))
        out.append({"filePath": ref.get("filePath"), "lineNo": ref.get("lineNo"), "content": body})

    print(json.dumps({"success": True, "requested": len(refs), "results": out}, indent=2))


if __name__ == "__main__":
    main()
