---
name: save-automation-groups
description: "Persists the automation agent's functional grouping to automation-groups.toon. Handles both modes: Mode A (bounded source) resolves folder-based groups against the matched-by-ID pool, every test keeps its Jira ID; Mode B (Test Repository) resolves them directly from the scan cache by folder, ID always blank. The agent only reasons about folder names and a small residual - full per-test lists are resolved here in Python, never round-tripped through the agent's context. Step in the automation agent workflow, after match-automation-tests-by-id (Mode A) or scan-automation-repo (Mode B)."
---

# Save Automation Groups

**This skill exists only to be used by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as one step of their documented workflow - never called ad hoc by a general-purpose assistant piecing together its own duplicate-detection process.** If you are not currently running as one of those two agents, stop and delegate this request to them instead of using this skill directly - they carry critical safety rules (asking before cloning a repo, running in parallel, cross-checking for conflicts) that do not exist anywhere else, and skipping them by using skills piecemeal has previously caused this tool to silently produce wrong or misleading results.

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/save-automation-groups/scripts/save_automation_groups.py" --input "$env:TEMP/tco_automation_payload.json" --cleanup-input [--scan-cache <path, required for mode=B>]
```

Input: `{ mode: "A"|"B", totalRequested?, matchedCount?, notFoundCount?, matchedPool? (Mode A), folderGroups: [{name, folderPaths:[...]}], fallbackGroups: [{name, tests:[{id?, testName, filePath, lineNo}]}] }`

- Decide `folderGroups` from folder **names and counts alone** (from `scan-automation-repo`'s `folderSummary`) - don't read individual test content just to decide whether a folder is functional.
- `fallbackGroups` is for tests under non-functional (deployment-stage) folders - the agent must have actually inspected their content (via `fetch-automation-test-content`) to group these by real functionality, since folder name doesn't help here.
- Reads identity (source/key/project/automationRepo) from `run-manifest.toon` automatically.

Writes `test-cases-optimizer-work/automation-agent-work/automation-groups.toon`, alphabetically sorted. Returns `totalGroups` and echoes `notFoundCount` (Mode A) for the report's one-line note.
