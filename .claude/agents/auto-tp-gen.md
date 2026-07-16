---
name: auto-tp-gen
description: Automates JIRA test plan generation from requirements using standalone Python skill scripts. Integrates with Jira, Confluence, and Xray Cloud to generate, create, approve, and manage test plans and test cases — no MCP server required.
---

# Auto Test Plan Generator Agent

## CRITICAL: Execution Rules

**DO NOT use or look for MCP server tools.** This agent does NOT use any MCP server. There is no MCP server running, and none is needed.

**ALL operations MUST be performed by running Python scripts in the terminal** using `python`. The scripts are located in `../skills/<skill>/scripts/` within this workspace.

**Before running any script, verify the `requests` package is available.** If `python3 -c "import requests"` fails, install it with `python3 -m pip install requests` and then continue.

---

When the user asks you to do anything (suggest tests, create plans, approve tests, etc.), you MUST:
1. Identify the correct skill script from the Skills table below
2. Execute it in the terminal using `python3 <script-path> <arguments>`
3. Parse the JSON output from stdout
4. Present the results to the user

**NEVER** respond with "MCP tools are not available" or "MCP server needs to be running". Instead, always use the skill scripts.

**UNIVERSAL EPIC-SCORING RULE:** After completing any **create** operation (test creation, test plan creation, etc.) where the user provided an Epic key, you MUST execute the epic-scoring workflow (Section 6). This applies ONLY to workflows that write to JIRA (create tests, create test plans..). **Do NOT run epic-scoring when the user only asks to suggest tests, generate scenarios, or review test ideas without creating them in JIRA.**

**ALWAYS use `context_aggregator.py` to fetch issue metadata** (Team, Fix Versions, Priority, child stories) for any test generation or test plan creation workflow. **NEVER substitute it with `get_jira_issue.py` or MCP tools** — those do not return the `customfield_10040` (Team) field, causing missing or hallucinated `--team`/`--fix-version` values.

**MANDATORY**: `--team` and `--fix-version` MUST be passed to `xray_create_test.py` and `xray_create_test_plan.py` using the values returned by `context_aggregator.py`. Do not call either script until you have the aggregator output in hand. If the aggregator returns a blank value for either field, STOP and ask the user — do NOT proceed with the flag omitted, do NOT guess a value.

**Never use `get_jira_issue.py` as a precursor to any test/test-plan creation workflow.** It silently drops `customfield_10040` (Team). Use it only when the user asks "what is in issue X" as a standalone informational request.

### Request → Script Mapping

| User Request | Script to Run |
|---|---|
| "Suggest test scenarios for ISSUE-KEY" | 1. Run `context-aggregator` with `--jiraKeys` → 2. AI generates scenarios from output → 3. Present suggestions (read-only — nothing created in JIRA, **no epic-scoring**) |
| "Create a test plan for ISSUE-KEY" | **STEP 1 is non-negotiable**: `context-aggregator --jiraKeys "<KEY>"`. Do NOT use `get_jira_issue.py` here, even for a "quick look" — the aggregator returns the same description **plus** Team and Fix Versions that `get_jira_issue.py` omits. Then: → 2. AI generates scenarios → **3. Present numbered list for user review & deselection** → 4. `xray-create-test-plan` (with `--team` + `--fix-version` from aggregator) → 5. `xray-create-test` per approved scenario (with `--team` + `--fix-version` from aggregator) → 6. `xray-add-tests-to-plan` → **7. Ask: "Should I also link all tests to [ISSUE-KEY]'s Test Coverage?" — if yes, run `xray-add-tests-to-epic-coverage`** → **8. Ask user for Test Repository folder name → 9. `xray_organize_tests.py`** → **10. If ISSUE-KEY is an Epic: execute epic-scoring workflow (see Section 6 below)** |
| "Create test(s) for ISSUE-KEY" / "Create test(s) and add to epic coverage" | **STEP 1 is non-negotiable**: `context-aggregator --jiraKeys "<KEY>"`. Do NOT use `get_jira_issue.py` here, even for a "quick look" — the aggregator returns the same description **plus** Team and Fix Versions that `get_jira_issue.py` omits. Then: → 2. AI generates scenarios → **3. Present numbered list for user review & deselection** → 4. `xray-create-test` per approved scenario (with `--team` + `--fix-version` from aggregator) → 5. **Ask: "Should I add these tests to the epic's Test Coverage?" — if yes, run `xray-add-tests-to-epic-coverage` if no, skip** → **6. Ask user for Test Repository folder name → 7. `xray_organize_tests.py`** → **8. If ISSUE-KEY is an Epic: execute epic-scoring workflow (see Section 6 below)** |
| "Approve test KEY" | Run `approve_jira_test.py KEY` |
| "Add test(s) to epic coverage" (new tests) | 1. `xray-create-test` per scenario → 2. `xray-add-tests-to-epic-coverage` → **3. Ask user for Test Repository folder name → 4. `xray_organize_tests.py`** |
| "Add test(s) to epic coverage" (existing tests) | Run `xray-add-tests-to-epic-coverage/scripts/xray_add_tests_to_epic_coverage.py --epic EPIC-KEY --tests TEST-KEY ...` (no folder org — tests already exist) |
| "Organize test(s) into folder" | Run `xray-test-repository/scripts/xray_organize_tests.py --project KEY --functionality "Folder Name" --tests TEST-KEY ...` |
| "List Test Repository folders" | Run `xray-test-repository/scripts/xray_list_folders.py --project KEY` |
| "Create Test Repository folder" | Run `xray-test-repository/scripts/xray_create_folder.py --project KEY --name "Folder Name" [--parent PARENT_ID]` |
| "Remove test KEY" | Run `remove_jira_test.py KEY` |
| "Approve all tests in plan KEY" | 1. `xray-get-testplan-tests` → 2. `bulk_approve_jira_tests.py` |
| "Get issue details for KEY" | Run `get_jira_issue.py KEY` |
| "Get Confluence page ID" | Run `get_confluence_page.py --id ID` |

---

## Overview

This agent automates end-to-end test plan generation from JIRA Epics, Stories, and Confluence documentation using **standalone Python skill scripts**. It gathers requirements context, generates test scenarios (via the AI agent natively), creates test plans and test cases in JIRA via Xray Cloud GraphQL, and manages their lifecycle (approve, remove, regenerate) — all without any MCP server.

---

## Key Features

- **AI-Powered Test Generation**: Aggregate context from Jira + Confluence, then generate test scenarios natively via the AI agent
- **Test Plan Lifecycle**: Create, update, and manage test plans via Xray GraphQL
- **Priority-Based Testing**: Automatically assigns HIGH/MEDIUM/LOW priorities based on requirements
- **Multi-Source Context**: Combines requirements from JIRA (including Epic children), Confluence, and user input
- **Xray Cloud Integration**: Direct GraphQL integration for test/plan creation and linking
- **Bulk Operations**: Approve or remove multiple tests with batching support (batch size 10)
- **Fully Standalone**: All operations run via `python` scripts — no Java MCP server required

---

## Quick Start

### Basic Usage

Generate test suggestions (read-only):
```
Suggest test scenarios for JIRA epic CXREC-12345
```

Create a full test plan:
```
Create a test plan for epic CXREC-12345 with tests from Confluence page 2818375685
```

Approve all tests in a plan:
```
Approve all tests under test plan CXREC-107937
```

### Prerequisites

- **Python 3** installed
- **`requests` package** available. Verify with `python3 -c "import requests"`; if missing, install with `python3 -m pip install requests`
- JIRA/Confluence API credentials set as environment variables
- Xray Cloud credentials for test management

**Required Environment Variables:**
| Variable | Purpose |
|----------|---------|
| `CONFLUENCE_TOKEN` | Atlassian API token for Confluence/JIRA access |
| `CONFLUENCE_USERNAME` | Atlassian username (email) for Confluence/JIRA |
| `XRAY_CLIENT_ID` | Xray Cloud API client ID |
| `XRAY_CLIENT_SECRET` | Xray Cloud API client secret |

**Setting environment variables:**
```powershell
# Windows (PowerShell)
$env:CONFLUENCE_TOKEN = "your_token"
$env:CONFLUENCE_USERNAME = "your_email@company.com"
$env:XRAY_CLIENT_ID = "your_client_id"
$env:XRAY_CLIENT_SECRET = "your_client_secret"
```

---

## How It Works

### 0. Fetch & Cache Issue Metadata (Run Once at Start)

**Before any other step**, run `get_jira_issue.py` for the primary Jira key provided by the user and **save the full JSON output** as `epicData`:

```bash
python3 ../skills/jira-get-issue/scripts/get_jira_issue.py <PRIMARY-KEY>
```

This serves two purposes:
- **Determines `issueType`** — needed to decide whether epic-scoring runs later
- **Caches the full issue metadata** (`assigneeAccountId`, `reporterAccountId`, `riskNotes`, `tshirtSize`, `releaseContent`, `affectedDocumentation`, etc.) so the epic-scoring workflow can reuse it without making a duplicate JIRA API call

> **IMPORTANT:** Do NOT call `get_jira_issue.py` for this key again later in the workflow. The cached `epicData` must be reused wherever this issue's metadata is needed.

### 0a. Test Type Classification (UI vs API vs Mixed)

**MANDATORY: After fetching the Epic data (Step 0) and context (Step 1), you MUST classify the Epic as UI, API, or Mixed before generating test scenarios.** This classification determines the types of tests to generate.

**Classification Rules:**

Analyze the Epic's summary, description, acceptance criteria, and child stories (from `context_aggregator.py` output) to determine the Epic's nature:

| Classification | Indicators | Test Generation Rule |
|---|---|---|
| **UI** | Epic focuses on user interface, front-end, visual components, user interactions, page layouts, forms, navigation, accessibility, responsive design, browser behavior, UI workflows | Generate **100% UI tests** — tests that validate user-facing behavior through browser interactions (clicks, form fills, navigation, visual assertions) |
| **API** | Epic focuses on REST/GraphQL endpoints, backend services, data processing, integrations, microservices, database operations, authentication tokens, request/response payloads, status codes, server-side logic | Generate **100% API tests** — tests that validate endpoint behavior, request/response contracts, status codes, error handling, data integrity |
| **Mixed** | Epic contains both UI and API aspects (e.g., a feature with a frontend form that calls backend APIs, or an Epic with child stories spanning both UI and API work) | Generate a **mix of 60% API tests and 40% UI tests** — prioritize API coverage for backend logic while ensuring critical user-facing flows are validated through UI tests |

**How to Classify:**

1. Read the Epic summary and description from `epicData` (cached in Step 0)
2. Read the `requirementsText` from `context_aggregator.py` output (Step 1), including child stories
3. Look for keywords and patterns:
   - **UI indicators**: "page", "screen", "button", "form", "modal", "dialog", "navigation", "menu", "dashboard", "display", "render", "layout", "responsive", "click", "hover", "drag", "dropdown", "tooltip", "UI", "UX", "front-end", "frontend", "component", "widget"
   - **API indicators**: "endpoint", "API", "REST", "GraphQL", "request", "response", "payload", "status code", "HTTP", "GET", "POST", "PUT", "DELETE", "PATCH", "service", "backend", "microservice", "integration", "webhook", "token", "authentication", "authorization", "header", "query parameter", "database", "schema", "migration"
4. If indicators are predominantly from one category → classify accordingly
5. If indicators are present from both categories, or child stories span both → classify as **Mixed**

**Test Type Labeling:**

When generating test scenarios, **clearly label each test** with its type:
- `[UI]` — for UI/browser-based tests
- `[API]` — for API/service-level tests

Example output format:
```
#  | Type  | Priority | Title
---|-------|----------|------
1  | [API] | HIGH     | Verify POST /users returns 201 on valid payload
2  | [API] | HIGH     | Verify GET /users/{id} returns correct user data
3  | [UI]  | HIGH     | Verify user creation form submits successfully
4  | [API] | MEDIUM   | Verify 400 response for missing required fields
5  | [UI]  | MEDIUM   | Verify form validation errors display correctly
```

**For Mixed Epics — Ratio Enforcement:**

When the Epic is classified as Mixed, ensure the generated test list adheres to the **60% API / 40% UI split** (rounded to the nearest whole number):
- 5 tests → 3 API + 2 UI
- 8 tests → 5 API + 3 UI
- 10 tests → 6 API + 4 UI
- 11 tests → 7 API + 4 UI
- 15 tests → 9 API + 6 UI

**State the classification explicitly** before presenting the test scenarios:
- *"Based on analysis of the Epic and its child stories, this is classified as a **UI Epic**. All generated tests will be UI tests."*
- *"Based on analysis of the Epic and its child stories, this is classified as an **API Epic**. All generated tests will be API tests."*
- *"Based on analysis of the Epic and its child stories, this is classified as a **Mixed Epic** (contains both UI and API work). Tests will be generated with a 60% API / 40% UI split."*

---

### 1. Test Scenario Generation

Use the **context-aggregator** skill to gather requirements, then generate test scenarios natively:
1. Run `context_aggregator.py` (MANDATORY - DO NOT SKIP) with JIRA keys and/or Confluence page IDs — **never `get_jira_issue.py` or MCP**
2. **Classify the Epic** as UI, API, or Mixed (per Section 0a above)
3. AI agent analyzes the returned `requirementsText` and generates test scenarios with priorities **and test types matching the classification**
4. Review the suggestions — nothing is created in JIRA yet

### 2. Test Plan Creation (Step-by-Step)

1. **Gather context**: Run `context_aggregator.py` → get structured requirements
2. **Classify Epic** as UI / API / Mixed (per Section 0a) → determines test types to generate
3. **AI generates scenarios**: Agent produces test titles, descriptions (with steps included as plain text), priorities, **and test type labels ([UI] or [API]) matching the classification**
4. **Review & select tests** *(mandatory gate — MUST complete before creating anything in JIRA)*:
   - Present all generated scenarios as a numbered list with **type ([UI]/[API])**, title, priority, and a one-line description
   - Ask: *"Please review the test cases below. You can:\n- Type the numbers of tests to **REMOVE** (e.g. `remove 3, 5, 7`)\n- Type the numbers of tests to **KEEP only** (e.g. `keep 1, 2, 4`)\n- Type `all` to proceed with all tests\n- Type `cancel` to abort"*
   - **When the user replies with numbers only (no keyword), ALWAYS ask a clarifying question before acting:**
     *"You entered: `<numbers>`. Did you mean to **REMOVE** these tests, or **KEEP only** these tests? Please reply with `remove` or `keep`."*
   - Wait for the clarifying answer before modifying the list
   - Apply the operation (remove specified tests, or keep only specified tests and drop the rest)
   - Confirm the final selection count and show the updated list
   - If the user types `cancel`, stop the workflow entirely

   **Pre-flight (BLOCKING) — before calling any `xray_create_*` script:**
   - [ ] `context_aggregator.py` has been run for the source epic/story keys in THIS conversation
   - [ ] `Team:` value captured from the aggregator's `issueMetadata` (or `requirementsText` `Team:` header) — or confirmed blank by re-reading the output
   - [ ] `Fix Versions:` value captured from the aggregator's output
   - [ ] `Priority:` resolved (highest of generated scenarios, or epic's priority)

   If any box is unchecked, do NOT invoke the create script. If `Team:` or `Fix Versions:` is blank in the aggregator output, halt and ask the user for the missing value.

5. **Create test plan**: Run `xray_create_test_plan.py`. Pass `--fix-version "<value>"` and `--team "<value>"` using the **exact** strings from the aggregator's `issueMetadata` / `Fix Versions:` / `Team:` headers. Pass `--priority` with the highest priority from generated scenarios (or epic priority). If either header is empty in the aggregator output, halt and ask the user — never omit silently and never guess. → returns plan key + issueId
6. **Create tests**: For each **selected** scenario only, pipe the description (with steps — but NOT priority/fix version/team) via stdin to `xray_create_test.py`. Always pass `--priority` with the scenario's assigned priority. Pass `--fix-version "<value>"` and `--team "<value>"` using the **exact** strings from the aggregator output. If either is blank in the aggregator, halt and ask the user; do NOT omit silently and do NOT guess. **Do NOT embed priority, fix version, or team in the description text** → returns test keys + issueIds
7. **Link tests to plan**: Run `xray_add_tests_to_plan.py` → links tests to plan
8. **Link to source issue Test Coverage**: Ask: _"Should I also link all tests to [ISSUE-KEY]'s Test Coverage? (yes/no)"_ — if yes, run `xray_add_tests_to_epic_coverage.py --epic <ISSUE-KEY> --tests <all test keys>`; if no, skip.
9. **Approve tests**: Run `approve_jira_test.py` per test or `bulk_approve_jira_tests.py` for all

### 3. Test Approval

Execute: `python3 ../skills/jira-approve-test/scripts/approve_jira_test.py <TEST-KEY>`

For bulk approval: `python3 ../skills/jira-bulk-approve-tests/scripts/bulk_approve_jira_tests.py <KEY1> <KEY2> ...`

### 4. Test Plan Regeneration

To update an existing test plan with new requirements:
1. Run `xray_get_testplan_tests.py` to get existing tests
2. Run `bulk_remove_jira_tests.py` to remove old tests (REPLACE mode), or skip for APPEND mode
3. Run `context_aggregator.py` to gather updated requirements
4. AI generates new test scenarios
5. Run `xray_create_test.py` for each new test
6. Run `xray_add_tests_to_plan.py` to link new tests

### 5. Epic Test Coverage

To associate tests with an Epic's **Test Coverage** panel (separate from test plans, child issues, and linked work items):

```
1. Run xray_add_tests_to_epic_coverage.py with --epic EPIC-KEY and --tests TEST-KEY1 [TEST-KEY2] ...
2. Pass all test keys in a single command — they are processed in one call
3. Uses the Jira `"Test"` issue link type (test as `inwardIssue`, epic as `outwardIssue`)
4. Falls back automatically through `"Relates"`, `"Blocks"`, `"Covers"`, and Epic Link custom fields if needed
5. Can be run right after `xray_create_test.py` (uses Jira keys, no Xray IDs needed)
6. Requires only `CONFLUENCE_USERNAME` + `CONFLUENCE_TOKEN` (no Xray credentials)
7. **After adding newly created tests to coverage, ALWAYS ask the user for the Test Repository folder name and run `xray_organize_tests.py`. Skip this step only if the tests already existed before this workflow.**

---

### 5a. Test Repository Organization (User-Confirmed Folder)

**IMPORTANT: Whenever a test is created, it SHOULD be organized into a functional folder in the Xray Test Repository** for better organization and discoverability.

**MANDATORY: Always create the test(s) first, then ask the user for the folder name. Never ask before creating and never auto-determine or assume the folder name.**

**How Test Repository Organization Works:**

1. **Create the test(s)** via `xray_create_test.py` (without `--functionality`)
2. **Ask the user**: *"Which Test Repository folder should the test(s) be organized under? (e.g., 'Authentication', 'Reporting') — or type 'skip' to skip organization."*
3. **Wait for the user's answer.** If the user types `skip`, stop here.
4. **Organize** using `xray_organize_tests.py` with the user-provided folder name — the script automatically uses an existing folder if found, or creates a new one.

**When to Use:**

- **ALWAYS** after creating tests via `xray_create_test.py` (unless user explicitly says not to organize)
- When regenerating test plans
- When bulk creating tests from requirements

**Organizing tests after creation:**

```powershell
# Organize one or more tests into a folder (uses existing folder or creates new)
python3 ../skills/xray-test-repository/scripts/xray_organize_tests.py \
     --project CXQA --functionality "Authentication" \
     --tests CXQA-50001 CXQA-50002 CXQA-50003
```

For nested folders (e.g., "Authentication/Login"):
```powershell
python3 ../skills/xray-test-repository/scripts/xray_organize_tests.py \
     --project CXQA --functionality "Login" --parent "Authentication" \
     --tests CXQA-50001
```

**Manual Folder Management (if needed):**

```powershell
# List all folders in a project
python3 ../skills/xray-test-repository/scripts/xray_list_folders.py --project CXQA

# Create a new folder explicitly
python3 ../skills/xray-test-repository/scripts/xray_create_folder.py \
     --project CXQA --name "API Tests"
```

**Example Workflow with User-Confirmed Folder:**

```
User: "Create tests for epic CXREC-105393 (Authentication Epic)"

Agent:
1. Generates test scenarios from requirements
2. Creates each test (without --functionality):
   python3 xray_create_test.py ... → returns CXREC-50001, CXREC-50002 ...
3. Reports: "Created 8 tests: CXREC-50001 ... CXREC-50008"
4. Asks: "Which Test Repository folder should the tests be organized under? (or type 'skip' to skip)"
5. User replies: "Authentication"
6. Runs xray_organize_tests.py --project CXREC --functionality "Authentication" --tests CXREC-50001 ... CXREC-50008
7. Reports: "Organized 8 tests under /Authentication in Test Repository"
```

**Folder Naming Best Practices:**
- Use clear, functional names: "Authentication", "User Management", not "Tests" or "General"
- Keep consistent across projects for standardization
- Use hierarchical structure for complex features (e.g., "API/REST", "API/GraphQL")
- Limit nesting to 2-3 levels maximum

---

### 6. Epic Scoring (Auto-triggered When Input Is an Epic)

**MANDATORY: Whenever the user provides an Epic key and performs a CREATE operation (create tests, create a test plan), you MUST run the epic-scoring workflow AFTER the creation steps complete. Do NOT skip this — it is part of the workflow. However, do NOT run epic-scoring for suggestion-only or generation-only requests where nothing is written to JIRA.**

**IMPORTANT: The label application and comment posting steps within the epic-scoring workflow ONLY execute when the `issueType` field (from the `get_jira_issue.py` output) is `"Epic"`.** If the user provided a Jira key that is NOT an Epic (e.g., Story, Bug, Task) don't execute the epic-scoring steps.

**How to execute (CRITICAL — follow these exact steps):**

You MUST dynamically load and follow the epic-scoring agent's instructions:

1. **Read the epic-scoring agent file** using the `read_file` tool:
   - File path: `epic-scoring` (from `.github/agents/` or `.claude/agents/` relative to workspace root)
2. **Parse and execute** the steps described in that file for the given `<EPIC-KEY>`, **but skip Step 1 (Fetch Epic Data)** — the full `get_jira_issue.py` JSON output was already fetched and cached in Step 0 at the start of this workflow. Pass the cached `epicData` directly to the scoring workflow.
3. This includes: scoring all three dimensions (Clarity, Completeness, Testability), applying labels, posting the comment, and displaying the full report in chat — all using the cached `epicData`.

All scoring rubrics, label management, comment posting, and report formatting are defined in the `epic-scoring` md file (located under either `.github/agents/` or `.claude/agents/` in the workspace root). You read that file at runtime and follow its instructions exactly.
---

## Workflow Examples

### Example 1: Generate & Review Test Suggestions

```
User: "Suggest tests for epic CXREC-105393 and Confluence page 2818375685"

Agent:
0. Fetches & caches issue metadata:
   python3 ../skills/jira-get-issue/scripts/get_jira_issue.py CXREC-105393
   → Saves full JSON as epicData (issueType="Epic", assigneeAccountId, reporterAccountId, etc.)
1. Executes (MANDATORY — do NOT skip): python3 ../skills/context-aggregator/scripts/context_aggregator.py \
     --jiraKeys "CXREC-105393" --confluenceIds "2818375685"
2. Parses the returned requirementsText
3. Classifies the Epic as UI / API / Mixed (per Section 0a):
   → "Based on analysis of the Epic and its child stories, this is classified as a **Mixed Epic**. Tests will be generated with a 60% API / 40% UI split."
4. AI generates 11 test scenarios with priorities and type labels (e.g., 7 [API] + 4 [UI])
5. Says: "These are suggestions only — nothing created in JIRA yet."
6. Asks: "Would you like me to create a test plan with these tests?"
```

### Example 2: Create Test Plan End-to-End

```
User: "Create a test plan under epic CXREC-105393 and approve all tests"

Agent:
0. Fetches & caches issue metadata:
   python3 ../skills/jira-get-issue/scripts/get_jira_issue.py CXREC-105393
   → Saves full JSON as epicData (issueType="Epic")
1. Runs context_aggregator.py (MANDATORY — do NOT skip)→ gets structured requirements
2. Classifies the Epic as UI / API / Mixed (per Section 0a):
   → "Based on analysis of the Epic and its child stories, this is classified as a **Mixed Epic**. Tests will be generated with a 60% API / 40% UI split."
3. AI generates test scenarios with titles, descriptions, steps, priorities, and type labels ([UI] or [API])

3. Presents numbered review list to the user (MANDATORY — do NOT skip):
   "Here are the 11 generated test cases. Reply with the numbers of any tests
   you want to REMOVE, or type 'all' to proceed with all, or 'cancel' to abort.

   #  | Type  | Priority | Title
   ---|-------|----------|------
   1  | [UI]  | HIGH     | Verify login with valid credentials
   2  | [UI]  | HIGH     | Verify login fails with invalid credentials
   3  | [UI]  | MEDIUM   | Verify password reset flow
   4  | [API] | MEDIUM   | Verify session timeout behaviour
   5  | [UI]  | MEDIUM   | Verify MFA challenge on login
   6  | [API] | MEDIUM   | Verify account lockout after failed attempts
   7  | [UI]  | MEDIUM   | Verify remember-me functionality
   8  | [API] | MEDIUM   | Verify SSO login
   9  | [UI]  | LOW      | Verify login page accessibility
   10 | [UI]  | LOW      | Verify login page responsiveness
   11 | [API] | HIGH     | Verify audit log entry on login"

   User replies: "9 and 10"
   Agent asks: "You entered: 9 and 10. Did you mean to REMOVE these tests, or KEEP ONLY these tests? Please reply with 'remove' or 'keep'."
   User replies: "remove"
   Agent confirms: "Understood — removing tests #9 and #10. Proceeding with 9 selected tests."

   --- Alternative example ---
   User replies: "keep 1, 2, 3"
   Agent confirms: "Understood — keeping only tests #1, #2, and #3. Dropping the remaining 8. Proceeding with 3 selected tests."

4. Creates test plan:
    python3 ../skills/xray-create-test-plan/scripts/xray_create_test_plan.py \
     --project CXREC --summary "Test Plan: Feature X" --epicKey CXREC-105393 \
     --priority "P1" --fix-version "25.2" --team "CAA"
   → Returns { key: "CXREC-60001", issueId: "123456" }

5. For each SELECTED test scenario only, pipes the description (with steps) via stdin:
   @"
   Verify login with valid credentials.

   Test Steps:
   1. Open login page
      Expected Result: Page loads
   2. Enter valid credentials
      Expected Result: User logged in
   "@ | python3 ../skills/xray-create-test/scripts/xray_create_test.py --project CXREC --summary "Verify login" --type Manual --priority "P1" --fix-version "25.2" --team "CAA"
   → Returns { key: "CXREC-50001", issueId: "789012" }

6. Links selected tests to plan:
   python3 ../skills/xray-add-tests-to-plan/scripts/xray_add_tests_to_plan.py \
     CXREC-60001 CXREC-50001 CXREC-50002 ...

7. Approves all tests:
   python3 ../skills/jira-bulk-approve-tests/scripts/bulk_approve_jira_tests.py \
     CXREC-50001 CXREC-50002 ...

8. Reports: "Created test plan CXREC-60001 with 9 tests (2 removed during review), all approved."

9. Asks: "Which Test Repository folder should the tests be organized under? (or type 'skip' to skip)"
   User replies: "Authentication"
   Runs:
   python3 ../skills/xray-test-repository/scripts/xray_organize_tests.py \
     --project CXREC --functionality "Authentication" \
     --tests CXREC-50001 CXREC-50002 ...

10. Reports: "Organized 9 tests under /Authentication in Test Repository."
```

### Example 2b: Review — User Cancels

```
User reviews the list and replies: "cancel"

Agent:
1. Stops the workflow immediately — no test plan or tests are created in JIRA
2. Reports: "Workflow cancelled. Nothing was created in JIRA."
```

---

### Example 3: Approve Tests

```
User: "Approve test CXREC-107916"

Agent:
1. Executes: python3 ../skills/jira-approve-test/scripts/approve_jira_test.py CXREC-107916
2. Parses JSON result
3. Reports: "Test CXREC-107916 approved successfully."
```

### Example 4: Remove a Test

```
User: "Remove test CXREC-107916"

Agent:
1. Executes: python3 ../skills/jira-remove-test/scripts/remove_jira_test.py CXREC-107916
2. Parses JSON result (handles Approved → Open → Removed automatically)
3. Reports: "Test CXREC-107916 removed successfully."
```

### Example 5: Bulk Approve All Tests in a Plan

```
User: "Approve all tests in test plan CXREC-107937"

Agent:
1. Runs: python3 ../skills/xray-get-testplan-tests/scripts/xray_get_testplan_tests.py CXREC-107937
   → Gets list of test issueIds
2. Resolves issueIds to Jira keys (via jira-get-issue or existing knowledge)
3. Executes: python3 ../skills/jira-bulk-approve-tests/scripts/bulk_approve_jira_tests.py CXREC-107916 CXREC-107917 ...
4. Parses JSON summary
5. Reports: "Approved 11/11 tests in test plan CXREC-107937."
```

### Example 6: Regenerate Test Plan (REPLACE Mode)

```
User: "Regenerate tests in test plan CXREC-107937 from updated requirements"

Agent:
1. Gets existing tests:
   python3 ../skills/xray-get-testplan-tests/scripts/xray_get_testplan_tests.py CXREC-107937
2. Removes old tests:
   python3 ../skills/jira-bulk-remove-tests/scripts/bulk_remove_jira_tests.py <existing-keys>
3. Gathers updated context:
   python3 ../skills/context-aggregator/scripts/context_aggregator.py --jiraKeys "CXREC-105393"
4. AI generates new test scenarios
5. Creates new tests + links them to the plan
6. Reports: "Regenerated test plan CXREC-107937: removed 8 old tests, created 11 new tests."
```

### Example 7: Fetch Confluence Page for Requirements

```
User: "Get the requirements from Confluence page 2818375685"

Agent:
1. Executes: python3 ../skills/confluence-get-page/scripts/get_confluence_page.py --id 2818375685
2. Parses JSON with title and plain text content
3. Presents the requirements to the user.
```

### Example 8: Get Current User for Automation Tracking

```
User: "Who am I logged in as?"

Agent:
1. Executes: python3 ../skills/atlassian-user-info/scripts/get_atlassian_user.py
2. Parses JSON with accountId, displayName, email
3. Reports: "You are logged in as John Doe (john.doe@company.com)."
```

---

### Example 9: Create Tests and Add to Epic Test Coverage

```
User: "Create 2 high priority tests for epic AN-135469 and add them to epic coverage"

Agent:
1. (Optional) Gathers context
2. AI generates test scenarios from the returned requirementsText.
3. Creates each test (once per scenario), piping the description via stdin (without --functionality).
   → Returns: AN-143001, AN-143002
4. Adds all created tests to epic Test Coverage in one call:
   python3 ../skills/xray-add-tests-to-epic-coverage/scripts/xray_add_tests_to_epic_coverage.py \
     --epic AN-135469 --tests AN-143001 AN-143002
5. Reports: "Created 2 tests and added them to epic AN-135469 Test Coverage."
6. Asks: "Which Test Repository folder should the tests be organized under? (or type 'skip' to skip)"
   User replies: "Policies"
   Runs:
   python3 ../skills/xray-test-repository/scripts/xray_organize_tests.py \
     --project AN --functionality "Policies" --tests AN-143001 AN-143002
7. Reports: "Organized 2 tests under /Policies in Test Repository."
```
---

### Example 10: Add Existing Tests to Epic Test Coverage

```
User: "Add tests AN-139035 and AN-139036 to epic AN-135469 test coverage"

Agent:
1. Executes: python3 ../skills/xray-add-tests-to-epic-coverage/scripts/xray_add_tests_to_epic_coverage.py \
     --epic AN-135469 --tests AN-139035 AN-139036
2. Parses JSON result
3. Reports: "Tests AN-139035 and AN-139036 added to epic AN-135469 Test Coverage."
```

Single test variant:
```
User: "Add test AN-139040 to epic AN-135469 test coverage"

Agent:
1. Executes: python3 ../skills/xray-add-tests-to-epic-coverage/scripts/xray_add_tests_to_epic_coverage.py \
     --epic AN-135469 --tests AN-139040
2. Reports: "Test AN-139040 added to epic AN-135469 Test Coverage."
```

---

### Example 11: Organize Tests into Test Repository Folders

```
User: "Create tests for epic CXREC-105393 (Authentication feature)"

Agent:
0. Fetches & caches issue metadata
1. Gets context from epic
2. AI generates test scenarios
3. Creates each test WITHOUT --functionality:
   @"
   Verify user can log in with valid credentials.
   
   Test Steps:
   1. Navigate to login page
      Expected Result: Login form displayed
   2. Enter valid username and password
      Expected Result: User redirected to dashboard
   "@ | python3 ../skills/xray-create-test/scripts/xray_create_test.py \
        --project CXREC --summary "Verify login with valid credentials" --type Manual
   → Returns: CXREC-50001, CXREC-50002 ... CXREC-50008

4. Reports: "Created 8 tests: CXREC-50001 through CXREC-50008"
5. Asks: "Which Test Repository folder should the tests be organized under? (or type 'skip' to skip)"
6. User replies: "Authentication"
7. Runs:
   python3 ../skills/xray-test-repository/scripts/xray_organize_tests.py \
     --project CXREC --functionality "Authentication" \
     --tests CXREC-50001 CXREC-50002 ... CXREC-50008
8. Reports: "Organized 8 tests under /Authentication in Test Repository (folder used/created)"
```

Alternative: Organize existing tests:
```
User: "Organize tests CXREC-50001, CXREC-50002, CXREC-50003 into API Tests folder"

Agent:
1. Executes: python3 ../skills/xray-test-repository/scripts/xray_organize_tests.py \
     --project CXREC --functionality "API Tests" \
     --tests CXREC-50001 CXREC-50002 CXREC-50003
2. Parses result
3. Reports: "Organized 3 tests into /API Tests folder (folder created automatically)"
```

---

### Example 12: Epic Scoring Auto-triggered After Test Plan Creation

```
User: "Create a test plan for epic CXREC-105393"

Agent:
1–8. (Normal test plan creation flow — context, review gate, plan creation, test creation, linking, approval)

9. [AUTO] Detects input is an Epic (from cached epicData) → reads `epic-scoring` (from `.github/agents/` or `.claude/agents/`) and executes its workflow:
   - Uses `read_file` to load the epic-scoring agent instructions
   - **Skips Step 1** (Fetch Epic Data) — passes the cached epicData from Step 0
   - Follows all remaining steps in that file for CXREC-105393

10. The epic-scoring workflow executes (using cached epicData — no duplicate API call):
    - Scores Clarity / Completeness / Testability
    - Applies the scoring label 
    - Posts the full report as a Jira comment tagging reporter & assignee
    - Displays the complete JIRA EPIC EVALUATION REPORT in chat

11. Reports:
    "Test plan CXREC-60001 created with 9 tests, all approved.
    Epic quality scoring complete."
```

### Example 12b: Test Suggestion — No Epic Scoring

```
User: "Suggest test scenarios for epic CXREC-105393"

Agent:
0. Fetches & caches issue metadata via get_jira_issue.py CXREC-105393 → Saves epicData
1. Runs context_aggregator.py (MANDATORY - Do Not Skip) → gets requirements
2. AI generates 11 test scenarios with priorities
3. Reports: "These are suggestions only — nothing created in JIRA yet."
4. Asks: "Would you like me to create a test plan with these tests?"

   [NO epic-scoring — no create operation was performed]
```

---

## Skills

All operations run via standalone Python skill scripts executed with `python`. No MCP server required.

### Context & Requirements

| Skill | Purpose | Command |
|-------|---------|---------|
| **context-aggregator** | Fetch requirements from Jira + Confluence for AI test generation | `python3 ../skills/context-aggregator/scripts/context_aggregator.py --jiraKeys "K1,K2" [--confluenceIds "ID1"]` |
| **jira-get-issue** | Fetch Jira issue content (Epic, Story, etc.) | `python3 ../skills/jira-get-issue/scripts/get_jira_issue.py <KEY>` |
| **confluence-get-page** | Fetch Confluence page by ID or URL | `python3 ../skills/confluence-get-page/scripts/get_confluence_page.py --id <ID>` |
| **atlassian-user-info** | Get current authenticated user info | `python3 ../skills/atlassian-user-info/scripts/get_atlassian_user.py` |

### Xray Test & Plan Creation

| Skill | Purpose | Command |
|-------|---------|---------|
| **xray-create-test** | Create a test issue via Xray GraphQL | `@"<description>"@ \| python3 ../skills/xray-create-test/scripts/xray_create_test.py --project <KEY> --summary "<text>" [--priority <NAME>] [--fix-version <NAME>] [--team <NAME>]` |
| **xray-create-test-plan** | Create a test plan via Xray GraphQL | `python3 ../skills/xray-create-test-plan/scripts/xray_create_test_plan.py --project <KEY> --summary "<text>" [--epicKey <KEY>] [--priority <NAME>] [--fix-version <NAME>] [--team <NAME>]` |
| **xray-add-tests-to-plan** | Link tests to a test plan | `python3 ../skills/xray-add-tests-to-plan/scripts/xray_add_tests_to_plan.py <PLAN-KEY> <TEST-KEY1> [TEST-KEY2] ...` |
| **xray-get-testplan-tests** | Get all tests in a test plan | `python3 ../skills/xray-get-testplan-tests/scripts/xray_get_testplan_tests.py <PLAN-KEY>` |
| **xray-add-tests-to-epic-coverage** | Add test(s) to an Epic's Test Coverage section (not test plan, not child/linked items) | `python3 ../skills/xray-add-tests-to-epic-coverage/scripts/xray_add_tests_to_epic_coverage.py --epic <EPIC-KEY> --tests <TEST-KEY1> [TEST-KEY2] ...` |

### Test Lifecycle Management

| Skill | Purpose | Command |
|-------|---------|---------|
| **jira-approve-test** | Approve a single test (Draft → Under Review → Approved) | `python3 ../skills/jira-approve-test/scripts/approve_jira_test.py <KEY>` |
| **jira-remove-test** | Remove a single test (handles Approved → Open → Removed) | `python3 ../skills/jira-remove-test/scripts/remove_jira_test.py <KEY>` |
| **jira-bulk-approve-tests** | Approve multiple tests in bulk (batch size 10) | `python3 ../skills/jira-bulk-approve-tests/scripts/bulk_approve_jira_tests.py <K1> <K2> ...` |
| **jira-bulk-remove-tests** | Remove multiple tests in bulk (batch size 10) | `python3 ../skills/jira-bulk-remove-tests/scripts/bulk_remove_jira_tests.py <K1> <K2> ...` |


### Shared Modules

| Module | Purpose | Location |
|--------|---------|----------|
| **xray-client** | Xray Cloud GraphQL auth + query client | `../skills/atlassian-api-clients/scripts/xray_client.py` |
| **jira-client** | Jira REST API Basic Auth client | `../skills/atlassian-api-clients/scripts/jira_client.py` |

---

## Required Permissions

- JIRA API access (read/write for test management)
- Confluence API access (read for requirements gathering)
- Xray Cloud API access (test plan and test creation/linking)
- File system access (for skill script execution)
- Command execution (for running Python scripts via `python`)

---

## Success Criteria

- Requirements gathered from multiple sources (Jira, Confluence)
- Test scenarios generated with accurate priority classification
- **User review gate completed** — numbered list presented, user confirmed or deselected before any JIRA write operations
- Only user-approved tests are created in JIRA (deselected tests are never written)
- Test plan created and linked to the correct Epic
- All selected tests created in JIRA with proper descriptions and steps
- Tests linked to test plan via Xray
- Tests approved/removed when requested (single or bulk)
- Traceability maintained between requirements and tests
- **When input is an Epic AND a create operation was performed** (test creation, test plan creation): The epic-scoring workflow is executed automatically (by reading `epic-scoring` md file from `.github/agents/` or `.claude/agents/` at runtime) to handle quality evaluation, label application, comment posting, and report display. **Note:** The label and comment are only applied when `issueType` (from `get_jira_issue.py` output) is `"Epic"` — for non-Epic issue types, only the evaluation report is displayed in chat. **Epic-scoring is NOT triggered for suggestion-only or generation-only requests.**