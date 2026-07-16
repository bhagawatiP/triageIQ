# TriageIQ

An AI triage co-pilot for **CXone Swarm**. Paste a Jira bug ID or a description and it returns
the **owning team**, existing or freshly-drafted **test coverage**, a code-level **root-cause
analysis** with a suggested fix, and a **priority** — then lets you create the test cases and
post the RCA back to the Jira ticket, each behind a one-click confirmation.

## What's inside

| Path | What it is |
|------|------------|
| `webapp/` | Single-page UI + local backend (`triage_server.py`). Start here. |
| `.claude/agents/cxone-swarm-sme.md` | The triage agent — 7-step RCA, team routing, test-coverage analysis, code-level RCA. |
| `.claude/workflows/cxone-swarm-triage.js` | Multi-phase pipeline version (parallel intake → RCA verify → team → coverage → drafts). |
| `.claude/skills/` | Standalone Python helpers for Jira/Xray (create/fetch/organize tests, comments, etc.). |
| `team-assignment.md` | Widget / product-area → owning-team map. |
| `cxone-dashboard-kb.md` | Product knowledge base used during triage. |

## Quick start

```powershell
cd webapp
copy config.env.example config.env    # then paste your Atlassian + Xray tokens
python triage_server.py               # → http://localhost:8756
```

Requires **Python 3.9+** and the **Claude CLI** (auto-detected from the VS Code extension).
Full setup, configuration, and safety notes are in [`webapp/README.md`](webapp/README.md).

## Security

All credentials live in a single file, `webapp/config.env`, which is **gitignored** and never
committed. Share `webapp/config.env.example` (placeholders only) so others can set up their own.

---
🤖 Built with [Claude Code](https://claude.com/claude-code)
