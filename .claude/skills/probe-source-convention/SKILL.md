---
name: probe-source-convention
description: "Reads a sample of 5 real tests from a Test Plan/Set/Execution/Repository - testType, status, labels, and full description - so the agent can decide how this specific project marks manual vs automated tests before building any exclusion JQL. Read-only. Step 1 of the manual agent workflow, run before read-manual-candidates."
---

# Probe Source Convention

**This skill exists only to be used by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as one step of their documented workflow - never called ad hoc by a general-purpose assistant piecing together its own duplicate-detection process.** If you are not currently running as one of those two agents, stop and delegate this request to them instead of using this skill directly - they carry critical safety rules (asking before cloning a repo, running in parallel, cross-checking for conflicts) that do not exist anywhere else, and skipping them by using skills piecemeal has previously caused this tool to silently produce wrong or misleading results.

**Before proceeding:** duplicate detection for this source is supposed to check both manual (Jira) and automation (repo) sides. If you're running this skill as part of a "find duplicate test cases" request and haven't also started (or asked about) automation-side checking - via the `automation-test-cases-optimizer` agent, or `clone-automation-repo`'s own gate question if you're driving that side yourself - do that too. Don't silently produce a manual-only result just because this was the skill you happened to reach for first.

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/probe-source-convention/scripts/probe_source_convention.py" --source <plan|testset|execution|repository> [--key <KEY>] [--project <PROJECT>]
```

Always samples 5 real tests (random spread, not just the first page) - one test is too easy to misjudge as representative of the whole project's convention.

## How the agent must use the result

Try the three signals in this order - each is a fallback for when the previous one didn't produce a reliable, JQL-filterable answer. Stop at the first one that works.

1. **`testType` first.** Look across all 5 samples:
   - Consistently reads as `Manual` (or an unambiguous manual-sounding custom name) → decide filter = **positive allow-list** `testType = "<that exact value>"`.
   - Consistently reads as an automated-sounding value (e.g. `Automated[Generic]`) → **cross-check against `description`**: does it contain automation-linkage evidence (an auto-generated-by-Xray / Playwright / script-file-path pattern)? If yes, `testType` is trustworthy for this source → decide filter = `testType != "<that exact value>"`, using the value **exactly as observed**, never a guessed string like `"Automated"`.
   - If `testType` and `description` **disagree** on one or more samples (e.g. says Manual but the description is clearly automation boilerplate, or vice versa), or the 5 samples show mixed/inconsistent values with no clear pattern → `testType` is not reliable for this project; move to step 2.
2. **If `testType` gave no reliable answer, check `labels` and `status` next** - some projects mark manual/automated through a label (e.g. a `manual`/`automated`/`automation` label) or through a workflow status/state (e.g. a status like "Ready for Automation" or "Automated" used as a marker, separate from the Removed status already excluded elsewhere) instead of `testType`. Look across all 5 samples for a **consistent** correlation - the same label or status value showing up specifically on tests that otherwise look manual (or otherwise look automated), not a one-off. If a consistent pattern is found, decide the filter the same way as step 1, just on `labels` or `status` as the field instead of `testType`.
3. **Only if neither `testType` nor `labels`/`status` produced a reliable, consistent signal** - no filter exists for this source. Proceed with no field filter in `read-manual-candidates` (Removed-status-only exclusion), and classify each candidate's `testType` client-side later via `fetch-test-content` instead (accept the extra fetch cost for this project only).

Never build a negative filter from a guessed value on any of these fields - only from the literal string observed in this probe.
