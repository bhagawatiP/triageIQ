---
name: generate-combined-report
description: "Renders the single self-contained HTML optimization report by reading ONLY the persisted manual-duplicates.toon, automation-duplicates.toon, combine-duplicates.toon, and removed-tests.toon - no Jira/Xray/git access at this stage. Does no cross-agent filtering itself - it trusts its inputs completely, since validate-cross-agent-duplicates has already resolved every same-test-in-more-than-one-set conflict and rewritten the files before this runs. Final step, run only after both agents, the combine stage, and validate-cross-agent-duplicates have completed."
---

# Generate Combined Report

**This skill exists only to be used by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as one step of their documented workflow - never called ad hoc by a general-purpose assistant piecing together its own duplicate-detection process.** If you are not currently running as one of those two agents, stop and delegate this request to them instead of using this skill directly - they carry critical safety rules (asking before cloning a repo, running in parallel, cross-checking for conflicts) that do not exist anywhere else, and skipping them by using skills piecemeal has previously caused this tool to silently produce wrong or misleading results.

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/generate-combined-report/scripts/generate_combined_report.py"
```

No input payload - it reads the fixed TOON artifacts directly from `test-cases-optimizer-work/`. Produces `test-cases-optimizer-report.html` with, in order:

1. **Automation Summary** (Total/Analyzed/Functional groups/Groups with duplicates/Mergeable sets/Eliminated/Reduction %/After merge) + the Automation duplicate groups table (Test Case ID may be blank in Mode B) + the one-line not-found note (Mode A only, count only, no IDs) if applicable.
2. **Manual Summary** (same counts + Removed count + No-steps count) + the Manual duplicate groups table + the compact multi-column Removed-IDs table (this is the only Removed-ID table in the whole report) + the No-steps-available list.
3. **Combined Summary** + the cross-agent combined duplicate groups table (each test tagged `[manual]`/`[automation]`).

Print this tool's `terminalSummary` field verbatim in the terminal - nothing else (no raw group/set dumps).
