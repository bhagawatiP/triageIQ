---
name: clone-automation-repo
description: "Clones the automation repo (shallow, blob-filtered) to a fresh folder under the system temp directory for the duration of one run, and deletes it afterward via --action cleanup. STOP before --action clone: the --repo-url MUST come from the user's own words in the CURRENT turn - never inferred from the current working directory, never read from a previous run's run-manifest.toon, never carried over from earlier in this conversation for a different request. If you cannot point to the exact message where the user just gave you this URL, ask for it before calling this skill at all - do not guess, do not reuse, do not assume. Never persists between runs and never lives under test-cases-optimizer-work/. --action cleanup-previous runs first, before anything else, deleting only a clone path recorded in THIS work folder's own run-manifest.toon from a possibly-crashed prior run - never a global sweep of other tco-automation-repo-* folders, which could belong to an unrelated concurrent run."
---

# Clone Automation Repo

## Step 1 - Always first, unconditionally: clear out any leftover clone

This needs no user input and is always safe to run immediately, before asking anything:

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/clone-automation-repo/scripts/clone_automation_repo.py" --action cleanup-previous
```

This reads `automation.clonePath` from `run-manifest.toon` (if present, e.g. left behind by a crashed or interrupted earlier run) and deletes **only** that specific path, then clears the field. It never touches any other `tco-automation-repo-*` folder in the system temp directory - a different one there could belong to a genuinely concurrent, unrelated run on the same machine and must not be touched.

## Step 2 - STOP before cloning anything new: answer this first

Has the user, in **this current turn**, explicitly given you a git URL (or explicitly said "no, don't check automation")? If not: **do not call `--action clone`.** Your response right now should just be the question - `Do you also want me to check for duplicates in your automation test repository? If yes, please share its git URL.` - and nothing else. Wait for their answer before coming back to this skill.

This applies **no matter what is calling this skill** - whether you are running as the `automation-test-cases-optimizer` agent, or directly invoking this skill yourself as part of a broader task. The rule doesn't change based on how you got here. In particular, do not:
- Infer the URL from the current working directory, even if it's already a checkout of what looks like the right repo.
- Read a URL back out of `run-manifest.toon` - Step 1 above already cleared it, and even if it hadn't, a prior run's URL is not this turn's answer.
- Reuse a URL mentioned earlier in this same conversation for a *different* request - a prior answer only ever applies to the request it was given for.

Getting this wrong has a real, confirmed consequence: it has previously caused this skill to analyze the wrong repository entirely against a different project's Jira tests, silently producing meaningless results. Asking first, every single time, is not optional.

## Step 3 - Clone the URL the user just gave you

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/clone-automation-repo/scripts/clone_automation_repo.py" --action clone --repo-url <git-url>
```

Returns `path` - the local clone location, used by `scan-automation-repo` and `fetch-automation-test-content`. Also records that path as `automation.clonePath` in `run-manifest.toon`, for `cleanup-previous` to find if this run doesn't get to clean up properly.

## Step 4 - MUST be cleaned up at the end of the automation agent's run, success or failure

```powershell
python "${CLAUDE_PLUGIN_ROOT}/skills/clone-automation-repo/scripts/clone_automation_repo.py" --action cleanup
```

`--path` is optional here and defaults to whatever was recorded in `run-manifest.toon` at clone time - normally you don't need to pass it at all. If you do pass it, it **must** match that recorded path exactly, or the script refuses to delete anything (it will not silently clean up the wrong folder). This means the clone path and the cleanup path can never diverge, regardless of what gets passed in: `--action cleanup` either uses the one true recorded path, or it does nothing.
