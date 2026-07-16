# CXone Swarm Triage — Web App

A single-page app to triage a Dashboard/CXCV bug. Enter a **Bug ID** and/or a
**description**; the app runs the enhanced `cxone-swarm-sme` agent and returns:

1. **Owning Team** (with code-RCA eligibility)
2. **Test-Case Coverage** — link(s) to existing aligned test(s), or a drafted new test case with steps + expected results
3. **RCA**
4. **Priority** (P-level + severity)

Two files, no build step, no `pip install`:

| File | Role |
|------|------|
| `triage_server.py` | Local backend (Python stdlib). Serves the UI + `POST /triage`. Fetches Jira by ID, then runs the agent headless via the bundled `claude.exe`. |
| `index.html` | The single-page UI (served by the backend at `/`). |

## Prerequisites

- **Python 3.9+** — invoke as `python` (this machine's `python3` is a broken Store alias).
- **Claude CLI** — auto-detected from the VS Code extension (`…/anthropic.claude-code-*/resources/native-binary/claude.exe`) or `PATH`; override with `CLAUDE_BIN`.
- **Credentials** — all live in one file, **`config.env`** (see below).
- The **pmn-shared** clone at `C:\Code\cxone-cxdvi-pmn-shared` and the **test-bed** snapshot at `C:\Code\sparkathon\cxdv-test-repository` (both auto-added to the agent's allowed dirs).

## Configuration — one file for all credentials

Every token and setting lives in a single file, **`config.env`** (next to `triage_server.py`).
To set it up on any machine:

```powershell
cd C:\Code\sparkathon\ps2-triage-commander\webapp
copy config.env.example config.env      # then edit config.env and paste your tokens
```

`config.env` holds four credentials — `CONFLUENCE_USERNAME`, `CONFLUENCE_TOKEN`
(Atlassian email + API token) and `XRAY_CLIENT_ID`, `XRAY_CLIENT_SECRET` (Xray Cloud API
key) — plus optional settings (`JIRA_BASE_URL`, `XRAY_PROJECT`, `TRIAGE_PORT`, …). The
server loads it at startup; **file values win**, and any value left blank/`PASTE_…` falls
back to a real environment variable if one is set.

> **Never commit or share `config.env`** — it contains real secrets (it's gitignored). Share
> `config.env.example` (placeholders only) instead.

## Run

```powershell
cd C:\Code\sparkathon\ps2-triage-commander\webapp
python triage_server.py
```

Then open **http://localhost:8756**. The startup banner prints the resolved `claude.exe`
path, creds status, and permission posture; `GET /health` returns the same as JSON and the
UI shows a green/red backend dot.

## Usage

- **Bug ID** (e.g. `CXDV-80765`) → the backend pulls the ticket from Jira and feeds it in.
- **Description** → free-text symptom / observed-vs-expected / repro / evidence.
- Provide either or both, then click **Triage bug**. A full run executes the 7-step
  framework, team routing, test-bed search, and code RCA — **this takes a few minutes**;
  the UI shows staged progress.

## Creating test cases (confirm-gated)

When a triage finds a coverage **Gap/Partial**, the drafted test case(s) render under
**Test Case Coverage** marked *"(draft — not created)"*, with a green **Create N test
case(s) in Xray** button. Clicking it shows an inline **confirm** ("Yes, create" /
"Cancel"). Only on confirm does the UI `POST /create-tests`, and the backend then:

1. Creates each test via the `xray-create-test` skill (`--project CXDV`, `--type Manual`,
   `--priority`, `--team <owning team>`), server-side — **no agent, no Bash** in the write path.
2. Best-effort organizes them into the widget's Test Repository folder (`xray_organize_tests.py`).
3. Best-effort links them to the bug's Test Coverage (`xray-add-tests-to-epic-coverage`).
4. Returns the created keys as clickable Jira links.

Nothing is written to Jira/Xray until you click **Yes, create**. Requests are capped at 5
tests; the team is validated against the known roster; the Jira key is format-checked. Needs
the `XRAY_CLIENT_ID/SECRET` env vars.

## Safety posture (important)

- **Draft-only:** the agent never creates/updates/approves anything in Jira/Xray during
  triage — coverage gaps come back as *drafted* test cases in the UI.
- **Read-only code RCA:** the pmn-shared clone is only read; fixes are *proposed*, never applied.
- **Agent permissions:** by default the backend runs the agent with a **scoped tool
  allowlist** (`Read Grep Glob WebFetch WebSearch Bash`) under `--permission-mode acceptEdits`
  — it runs unattended without hanging, but does **not** pass the blanket
  `--dangerously-skip-permissions` override. If a run stalls on a permission the allowlist
  doesn't cover, opt in explicitly:

  ```powershell
  $env:TRIAGE_ALLOW_DANGEROUS = "1"   # adds --dangerously-skip-permissions
  python triage_server.py
  ```

## Configuration (env vars)

| Var | Default | Purpose |
|-----|---------|---------|
| `CLAUDE_BIN` | auto-detected | Full path to `claude.exe`. |
| `TRIAGE_PORT` | `8756` | Web server port. |
| `TRIAGE_TIMEOUT` | `1200` | Max seconds per triage before the backend gives up. |
| `TRIAGE_ALLOW_DANGEROUS` | *(off)* | `1` → run the agent with `--dangerously-skip-permissions`. |
| `PMN_SHARED_DIR` | `C:\Code\cxone-cxdvi-pmn-shared` | Source repo for code RCA. |
| `TESTBED_DIR` | `C:\Code\sparkathon\cxdv-test-repository` | Cached CXDV Xray snapshot. |
| `JIRA_BASE_URL` | `https://nice-ce-cxone-prod.atlassian.net` | Base for building existing-test links. |

## How it works

```
Browser (index.html)
   │  POST /triage { bugId?, description? }
   ▼
triage_server.py
   │  1. if bugId → python .../jira-get-issue/get_jira_issue.py <ID>   (live Jira)
   │  2. build prompt → claude.exe -p --agent cxone-swarm-sme
   │        --output-format json --add-dir <pmn-shared> --add-dir <test-bed>
   │  3. parse the agent's fenced ```json envelope
   ▼
JSON { bug_summary, team, priority, rca, code_rca, test_coverage } → rendered as cards
```

If the agent ever returns non-JSON (format drift), the backend passes the raw text through
and the UI shows it verbatim, so a run never hard-fails on formatting.

## Troubleshooting

- **Backend dot red / "claude.exe not found"** → set `CLAUDE_BIN` to the full path.
- **Jira fetch failed** → check the four Confluence/Xray env vars and the key spelling.
- **Run stalls or returns partial** → set `TRIAGE_ALLOW_DANGEROUS=1` (see above).
- **Takes minutes** → expected; raise `TRIAGE_TIMEOUT` for very large tickets.
