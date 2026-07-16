---
name: init-work-folder
description: "Creates the shared test-cases-optimizer-work/ folder tree (at the git repo root, or cwd if not in a repo) and records/merges this run's source identity into run-manifest.toon. Called once by each agent (manual and automation) at the start of its own flow. No Jira/Xray/git-write access - local folder/file creation only. Step 0 of both agents' workflow."
---

# Init Work Folder

**This skill exists only to be used by the `manual-test-cases-optimizer` or `automation-test-cases-optimizer` agent, as one step of their documented workflow - never called ad hoc by a general-purpose assistant piecing together its own duplicate-detection process.** If you are not currently running as one of those two agents, stop and delegate this request to them instead of using this skill directly - they carry critical safety rules (asking before cloning a repo, running in parallel, cross-checking for conflicts) that do not exist anywhere else, and skipping them by using skills piecemeal has previously caused this tool to silently produce wrong or misleading results.

Run first, by both agents, independently:

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/init-work-folder/scripts/init_work_folder.py" --agent manual --source <plan|testset|execution|repository> [--key <KEY>] [--project <PROJECT>]
python "${CLAUDE_PLUGIN_ROOT}/skills/init-work-folder/scripts/init_work_folder.py" --agent automation --source <plan|testset|execution|repository> [--key <KEY>] [--project <PROJECT>] --automation-repo <git-url>
```

If the user declines automation-side checking when asked, the automation agent calls this instead (no `--automation-repo`) and then stops - no clone, no further steps:

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/init-work-folder/scripts/init_work_folder.py" --agent automation --source <plan|testset|execution|repository> [--key <KEY>] [--project <PROJECT>] --skipped
```

Creates:

```
test-cases-optimizer-work/
  run-manifest.toon
  manual-agent-work/
  automation-agent-work/
```

Merges this agent's identity into `run-manifest.toon` (does not overwrite the other agent's entry). Returns the resolved folder paths as JSON.
