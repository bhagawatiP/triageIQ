#!/usr/bin/env python3
"""
One bulk, framework-agnostic pass over an already-cloned automation repo
(Playwright, Cypress, Selenium/JUnit/TestNG, pytest, Cucumber/Gherkin, RSpec,
or generic) that indexes every test block it can find to:
  - folderPath / filePath (relative to repo root) - used for the
    functional-vs-deployment-stage folder judgment
  - testName - the test's title/method name
  - possibleId - a Jira-style ID (e.g. PROJ-1234), if found in the test
    title itself, in a comment/decorator/tag on one of the few lines directly
    above the test block, or in an inline comment on the declaration line.
    null if none found anywhere - the caller falls back to test name only.

This single scan output serves BOTH automation-agent modes:
  - Mode A (bounded source: Test Set/Plan/Execution): match-automation-tests-by-id
    intersects `possibleId` against the Jira ID list from read-all-source-tests.
  - Mode B (Test Repository / whole project): save-automation-groups groups
    directly by folderPath, ignoring `possibleId` entirely.

The full per-test index (which can be thousands of entries for a large repo)
is written to a cache file INSIDE the cloned repo path (so it is deleted for
free when clone-automation-repo's cleanup runs) rather than printed in full -
only a compact per-folder count summary is printed, so the agent can judge
folder-name-vs-functional-area from counts alone without spending tokens on
every individual test name. Downstream scripts read the cache file directly.

Deliberately avoids per-file/per-test network or API calls - this is a local,
single-pass filesystem scan only.

Usage:
  python scan_automation_repo.py --repo-path <local clone path>

No environment variables required (no network access).
"""

import sys
import os
import re
import json
import argparse

SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", "out", "target",
    "__pycache__", ".venv", "venv", "coverage", ".pytest_cache",
    "playwright-report", "test-results",
}

SCAN_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".py", ".java", ".cs", ".rb", ".feature"}

ID_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{2,7})\b")

CONTEXT_LINES_ABOVE = 5

# A describe/context block's title often carries the shared Jira ID for every
# test nested inside it (e.g. Playwright's `test.describe.serial('PROJ-1234:
# ...', () => { test(...); test(...); })`) - individual test() calls nested
# deep inside frequently have no ID of their own at all. Tracked separately
# from TEST_DECLARATION_PATTERNS so its ID can be inherited by every test
# within its brace scope, not just ones within the 5-line lookback.
DESCRIBE_PATTERN = re.compile(r"""\b(?:describe|context)(?:\.serial|\.parallel|\.only|\.skip|\.fixme)?\(\s*['"`](.+?)['"`]""")

# (regex, title group index, "kind" label). Applied per-line; a match means
# "this line declares a test block".
TEST_DECLARATION_PATTERNS = [
    (re.compile(r"""\btest(?:\.only|\.skip)?\(\s*['"`](.+?)['"`]"""), 1, "playwright/jest"),
    (re.compile(r"""\bit(?:\.only|\.skip)?\(\s*['"`](.+?)['"`]"""), 1, "cypress/jest/rspec"),
    (re.compile(r"^\s*def\s+(test_\w+)\s*\("), 1, "pytest"),
    (re.compile(r"^\s*Scenario(?:\s+Outline)?:\s*(.+)$"), 1, "cucumber"),
    (re.compile(r"(?:public\s+)?void\s+(\w+)\s*\("), 1, "attribute-marked-method"),
]

# A line carrying one of these is a genuine test-method marker - the method
# declaration that follows (found via the same void-lookahead used for Java's
# @Test) is a real test. Deliberately does NOT include class-level attributes
# like [TestFixture]/[TestClass] (\b on both sides of "Test"/"TestMethod"/etc.
# stops "TestFixture" from matching "Test"), and does NOT match a bare
# "public void SomeMethod()" with no marker at all - a support/helper method
# in the same file (page-object navigation, click helpers, wait utilities)
# has no such marker and is correctly never counted as a test, per
# duplicate-detection-guidelines rule 13. Without this, a plain "public void"
# method is invisible to the scanner - it can only ever be found via a marker
# lookahead, never on its own.
ATTRIBUTE_MARKER_PATTERNS = [
    re.compile(r"@Test\b"),                                        # JUnit/TestNG (Java)
    re.compile(r"^\s*\[\s*(?:Test|TestMethod|Fact|Theory|DataTestMethod|TestCase)\b"),  # NUnit/MSTest/xUnit (.NET)
]


def _iter_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if os.path.splitext(fn)[1] in SCAN_EXTENSIONS:
                yield os.path.join(dirpath, fn)


_COMMENT_LINE_RE = re.compile(r"^\s*(///|//|\*|/\*)")


def _extend_through_comment_block(lines, window_start):
    """A doc-comment block (XML /// summary+remarks+params, or a plain
    multi-line // block) can run well past a small fixed lookback - a
    <remarks> section alone can push an ID several lines further up than
    CONTEXT_LINES_ABOVE reaches. Rather than guess a bigger fixed number
    (which just moves the same problem), keep walking backward past
    window_start as long as the lines immediately above it are still part of
    the same contiguous comment block, however long it runs, and stop the
    instant a non-comment line is hit."""
    i = window_start
    while i > 0 and _COMMENT_LINE_RE.match(lines[i - 1]):
        i -= 1
    return i


def _find_id_nearby(lines, idx):
    """Search the current line, the few lines above it, and (if that region
    borders a comment block) the rest of that comment block however long it
    runs, for an ID - preferring the match CLOSEST to the current line (not
    the first one in the window) - an ID on the test's own line is more
    specific than an ancestor block's ID that also happens to fall within
    the same lookback window."""
    window_start = max(0, idx - CONTEXT_LINES_ABOVE)
    window_start = _extend_through_comment_block(lines, window_start)
    window = "\n".join(lines[window_start:idx + 1])
    matches = list(ID_PATTERN.finditer(window))
    return matches[-1].group(1) if matches else None


def _enclosing_describe_id(stack):
    """Nearest enclosing describe/context block's ID, innermost first."""
    for entry in reversed(stack):
        if entry["id"]:
            return entry["id"]
    return None


def scan(root):
    tests = []
    files_scanned = 0
    for filepath in _iter_files(root):
        files_scanned += 1
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except OSError:
            continue

        rel_path = os.path.relpath(filepath, root).replace("\\", "/")
        folder_path = os.path.dirname(rel_path)

        # Some suites name the file itself after the Jira ID it covers
        # (e.g. PROJ-1234.test.ts) instead of, or in addition to, tagging it
        # inline - a cheap, reliable fallback signal when nothing nearby the
        # test/describe line carries an ID of its own.
        filename_match = ID_PATTERN.search(os.path.basename(rel_path))
        filename_id = filename_match.group(1) if filename_match else None

        brace_depth = 0
        describe_stack = []  # [{"closeDepth": int, "id": str|None}, ...]

        for idx, line in enumerate(lines):
            depth_before = brace_depth
            is_describe = DESCRIBE_PATTERN.search(line) is not None
            brace_depth += line.count("{") - line.count("}")

            if is_describe:
                describe_stack.append({"closeDepth": depth_before, "id": _find_id_nearby(lines, idx)})
            while describe_stack and brace_depth <= describe_stack[-1]["closeDepth"]:
                describe_stack.pop()

            # A test-attribute/annotation marker: the method name is usually
            # 1-5 lines below (stacked attributes like [Test]\n[Category(...)]
            # are common in .NET, hence the wider lookahead than a single
            # bare annotation would need).
            if any(p.search(line) for p in ATTRIBUTE_MARKER_PATTERNS):
                for lookahead in range(idx + 1, min(idx + 6, len(lines))):
                    m = re.search(r"(?:public\s+)?void\s+(\w+)\s*\(", lines[lookahead])
                    if m:
                        possible_id = _find_id_nearby(lines, idx) or _enclosing_describe_id(describe_stack) or filename_id
                        tests.append({
                            "folderPath": folder_path,
                            "filePath": rel_path,
                            "lineNo": lookahead + 1,
                            "testName": m.group(1),
                            "possibleId": possible_id,
                            "kind": "attribute-marked-method",
                        })
                        break
                continue

            for pattern, group_idx, kind in TEST_DECLARATION_PATTERNS:
                if kind == "attribute-marked-method":
                    continue  # only matched via the attribute-marker lookahead above
                m = pattern.search(line)
                if m:
                    possible_id = _find_id_nearby(lines, idx) or _enclosing_describe_id(describe_stack) or filename_id
                    tests.append({
                        "folderPath": folder_path,
                        "filePath": rel_path,
                        "lineNo": idx + 1,
                        "testName": m.group(group_idx).strip(),
                        "possibleId": possible_id,
                        "kind": kind,
                    })
                    break

    return tests, files_scanned


def main():
    parser = argparse.ArgumentParser(description="Scan a cloned automation repo for test blocks (local, no network).")
    parser.add_argument("--repo-path", required=True)
    args = parser.parse_args()

    if not os.path.isdir(args.repo_path):
        print(json.dumps({"success": False, "error": f"repo path not found: {args.repo_path}"}, indent=2))
        sys.exit(1)

    tests, files_scanned = scan(args.repo_path)

    folders = {}
    for t in tests:
        folders.setdefault(t["folderPath"], []).append(t["testName"])

    cache_path = os.path.join(args.repo_path, ".tco-scan-cache.json")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"tests": tests}, f)

    result = {
        "success": True,
        "scanCachePath": cache_path,
        "filesScanned": files_scanned,
        "testsFound": len(tests),
        "folderSummary": {k: len(v) for k, v in sorted(folders.items())},
    }

    # ID extraction is a syntax-agnostic "check everywhere at once" search
    # (inline title, comment/doc-block above of any length, an enclosing
    # describe/context block, the filename) rather than picking one
    # convention up front - it works across most real repos without needing
    # to know which one applies. But it never says how well that actually
    # worked for THIS repo, so a convention it doesn't recognize at all (an
    # external id-to-test mapping file, an unfamiliar tag format) would just
    # look identical to "this test genuinely has no id" with no signal to
    # look closer. Surface per-folder coverage so a MIXED result (some tests
    # in a folder have an id, others don't) - the strongest sign a real
    # convention exists but wasn't fully recognized - gets flagged instead
    # of silently accepted.
    with_id = sum(1 for t in tests if t.get("possibleId"))
    result["idCoverage"] = {"testsWithId": with_id, "testsWithoutId": len(tests) - with_id}
    mixed_coverage = []
    for folder, names in sorted(folders.items()):
        folder_tests = [t for t in tests if t["folderPath"] == folder]
        folder_with_id = sum(1 for t in folder_tests if t.get("possibleId"))
        if 0 < folder_with_id < len(folder_tests):
            mixed_coverage.append({"folder": folder, "withId": folder_with_id, "withoutId": len(folder_tests) - folder_with_id})
    if mixed_coverage:
        result["mixedIdCoverageFolders"] = mixed_coverage
        result["idCoverageNote"] = (
            "Some folders have an id on only part of their tests, not all or none - a strong signal "
            "that a real id convention exists there but wasn't fully recognized (e.g. only some files "
            "use it, or it takes a form the known patterns don't cover). Before treating the "
            "id-less ones in these folders as genuinely id-less, read a couple of them directly to "
            "check for a convention that was missed, rather than assuming the blank ones simply have none."
        )

    # These known patterns cover the common frameworks (Playwright/Jest,
    # Cypress, pytest, Cucumber, JUnit/TestNG, NUnit/MSTest/xUnit) but cannot
    # cover every custom in-house test framework. A scanned codebase with
    # zero matches is either genuinely not a test repo, or uses a
    # declaration convention these patterns don't recognize - the caller
    # cannot tell which from this count alone. Silently treating "0 found"
    # as "0 real tests" and falling back to some other heuristic (e.g.
    # counting every method or every file as a test, which has been
    # observed to badly inflate real counts by including support/helper
    # code) is exactly the failure this warning exists to prevent.
    if files_scanned > 0 and len(tests) == 0:
        result["warning"] = (
            "0 tests matched the known test-declaration patterns across "
            f"{files_scanned} scanned file(s). This may genuinely not be a "
            "test repository, or it may use a test-declaration convention "
            "these patterns don't recognize (a custom attribute/decorator, "
            "a naming-convention-only framework, a config-driven runner). "
            "Do not assume either answer - read a small representative "
            "sample of files directly (per duplicate-detection-guidelines) "
            "to find this repo's real test-declaration convention before "
            "concluding there are no tests, and never fall back to counting "
            "every method or every file as a test."
        )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
