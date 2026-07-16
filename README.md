# TriageIQ (Bugs to Quality Coverage)

An AI triage co-pilot for **CXone Swarm**. Paste a Jira bug ID or a description and TriageIQ
takes it from a raw bug all the way to quality coverage — in one pass it returns the
**owning team**, existing or freshly-drafted **test coverage**, a code-level **root-cause
analysis** with a suggested fix, and a **priority**. Then, behind one-click confirmations, it
can **create the drafted test cases** in Xray and **post the RCA back** to the Jira ticket.

**Repository:** https://github.com/bhagawatiP/triageIQ

---

## What you get from one triage

| Output | Detail |
|--------|--------|
| 👥 **Owning Team** | Routes the bug to its team (Waves / Agni / Sapphire / Titans / Dragonfly / Hornet) from the widget/area map, with code-RCA eligibility. |
| 🧪 **Test Coverage** | Searches the CXDV Xray test bed. If an aligned test exists → its **link**; if not → a **drafted** JIRA-ready case (steps + expected results). |
| 🔍 **RCA** | Evidence-based root cause; for Titans/Sapphire/Waves bugs, adds suspect source files + a **proposed fix** from the product code. |
| 🚦 **Priority** | P1–P4 + severity + justification. |
| ✅ **Actions** | One-click (confirm-gated): **create** the drafted tests in Xray, and **add the RCA** as a comment on the bug. |

---

## Prerequisites

- **Python 3.9+** — invoked as `python`.
- **Claude CLI** — auto-detected from the VS Code “Claude Code” extension, or on `PATH`, or set `CLAUDE_BIN` in `config.env`.
- **Atlassian API token** (Jira + Confluence) and an **Xray Cloud API key** (client id + secret).
- **GitHub access to the private `nice-cxone` org** — *only* needed for the code-RCA step; without it, code RCA is skipped and everything else still works.
- **Windows:** clone to a **short path** (e.g. `C:\triageIQ`) — the code-RCA source has deeply-nested files near the 260-char limit.

---

## Setup — required to run

```bash
# 1. Clone
git clone https://github.com/bhagawatiP/triageIQ
cd triageIQ/webapp

# 2. Create your config from the template
copy config.env.example config.env        # Windows
# cp config.env.example config.env         # macOS / Linux

# 3. Edit config.env and fill the FOUR credentials (see below)

# 4. Run
python triage_server.py

# 5. Open the app
#    http://localhost:8756
```

The **test bed is bundled** in the repo and the **code-RCA source auto-clones** on first run —
nothing else to download.

### The four credentials to put in `config.env`

| Key | What to paste | Where to get it |
|-----|---------------|-----------------|
| `CONFLUENCE_USERNAME` | Your Atlassian account email | — |
| `CONFLUENCE_TOKEN` | Atlassian API token | https://id.atlassian.com/manage-profile/security/api-tokens |
| `XRAY_CLIENT_ID` | Xray Cloud client id | Jira → Apps → Xray → **API Keys** |
| `XRAY_CLIENT_SECRET` | Xray Cloud client secret | same as above |

`config.env` is **gitignored** — it is never committed. Share `config.env.example` (placeholders) instead.

### Optional settings (sensible defaults; override in `config.env`)

| Key | Default | Purpose |
|-----|---------|---------|
| `TRIAGE_PORT` | `8756` | Web server port. |
| `JIRA_BASE_URL` | `https://nice-ce-cxone-prod.atlassian.net` | Jira/Confluence base URL. |
| `XRAY_PROJECT` | `CXDV` | Project key for created tests. |
| `CLAUDE_BIN` | *(auto-detected)* | Full path to `claude.exe` if not found automatically. |
| `PMN_SHARED_REPO` | `github.com/nice-cxone/cxone-cxdvi-pmn-shared` | Code-RCA source (auto partial-cloned). |
| `PMN_SHARED_BRANCH` | `develop` | Branch tracked for the code-RCA cache. |
| `PMN_SHARED_DIR` | `<repo>/.external/cxone-cxdvi-pmn-shared` | Cache path; point it at an existing clone → used **read-only**. |
| `PMN_SHARED_SPARSE` | *(widget subtrees)* | `;`-separated dirs to check out; `*` = full checkout. |
| `TRIAGE_TIMEOUT` | `1200` | Max seconds per triage. |
| `TRIAGE_ALLOW_DANGEROUS` | *(off)* | `1` gives the agent full Bash (unsafe with untrusted input). |

---

## Using it

1. Open **http://localhost:8756**, enter a **Bug ID** (e.g. `CXDV-80790`) and/or a **description**, click **Triage bug**.
2. Review the four cards — **Owning Team & Priority**, **Test Case Coverage**, **RCA**.
3. On a coverage gap → **Create test case(s) in Xray** → confirm → the new `CXDV-####` links appear.
4. In the RCA card → **Add this RCA to `<BUG>`** → confirm → posted as a Jira comment.

Nothing is written to Jira/Xray until you click a confirm button.

---

## What's inside

| Path | What it is |
|------|------------|
| `webapp/` | Single-page UI + local backend (`triage_server.py`, stdlib-only). **Start here.** |
| `.claude/agents/cxone-swarm-sme.md` | The triage agent — 7-step RCA, team routing, test-coverage, code-level RCA. |
| `.claude/workflows/cxone-swarm-triage.js` | Multi-phase pipeline version (Claude Code). |
| `.claude/skills/` | Python helpers for Jira/Xray (create/fetch/organize tests, comments). |
| `team-assignment.md` | Widget / product-area → owning-team map. |
| `cxone-dashboard-kb.md` | Product knowledge base used during triage. |
| `testbed/` | **Bundled** CXDV Xray test-bed snapshot (offline coverage lookup). |
| `.external/` | Auto-cloned code-RCA source cache (gitignored). |

---

## How it works

```
Browser (index.html)
   │  POST /triage { bugId?, description? }
   ▼
triage_server.py
   │  loads config.env → runs claude.exe -p --agent cxone-swarm-sme (cwd = repo)
   │  injects resolved resource paths; --add-dir testbed + pmn-shared cache
   ▼
JSON { team, priority, rca, code_rca, test_coverage } → rendered as cards
   │  POST /create-tests  → creates confirmed tests via the Xray skills
   └  POST /add-rca       → posts the RCA as a Jira comment
```

- **Test bed** is bundled (`testbed/`) so coverage lookup works offline.
- **Code-RCA source** is a lightweight, always-current cache: a blobless + sparse partial
  clone refreshed to the latest `develop` HEAD each run (a `git ls-remote` check skips the
  download when nothing changed).

---

## Security

- **Credentials** live only in `webapp/config.env` (gitignored). Loaded at startup; file wins over environment.
- **No silent writes** — creating tests and posting the RCA are each behind an explicit in-app confirmation.
- **Read-only code RCA** — the pmn-shared source is only read; fixes are *proposed*, never applied.
- **Scoped agent** — the headless agent runs with a read-only tool allowlist (no Bash) unless you opt into `TRIAGE_ALLOW_DANGEROUS=1`.
- The backend binds `127.0.0.1`, checks the `Host` header, and sends no CORS headers.

---
🤖 Built with [Claude Code](https://claude.com/claude-code)
