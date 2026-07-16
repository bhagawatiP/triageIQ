---
name: context-aggregator
description: Fetches requirements from Jira issues (including Epic children) and Confluence pages, returning structured context text. Replaces the AI-dependent test_scenarios_generate MCP tool. The AI agent generates tests natively from the returned context.
---

# Context Aggregator

## When to Use This Skill

Use this skill to gather requirements context from multiple sources before generating test scenarios. This is the first step in the `test_plan_create_with_tests` and `regenerate_test_plan_tests` workflows.

**Trigger conditions:**
- User asks to generate test scenarios from Jira issues and/or Confluence pages
- Part of test plan creation workflow (replaces `test_scenarios_generate`)
- Need to aggregate multi-source requirements into structured text

---

## How to Execute

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/context_aggregator.py" --jiraKeys "<KEY1>,<KEY2>" [--confluenceIds "<ID1>,<ID2>"] [--confluenceUrls "<URL1>,<URL2>"] [--featureHint "<text>"]
```

**Examples:**
```bash
# Jira issues only
python "${CLAUDE_PLUGIN_ROOT}/scripts/context_aggregator.py" --jiraKeys "CXQA-123,CXQA-456"

# Jira + Confluence
python "${CLAUDE_PLUGIN_ROOT}/scripts/context_aggregator.py" --jiraKeys "CXQA-123" --confluenceIds "12345" --featureHint "Login feature"
```

---

## What It Does

1. Fetches each Jira issue (summary, description, acceptance criteria, priority, labels)
2. If an issue is an **Epic**, automatically fetches all child issues/stories
3. Fetches each Confluence page content (strips HTML to plain text)
4. Builds a structured requirements document with sections per source
5. Returns JSON with the `requirementsText` and source counts

---

## Output Format

```json
{
  "success": true,
  "sources": {
    "jiraIssues": 2,
    "childIssues": 5,
    "confluencePages": 1,
    "failedJira": [],
    "failedConfluence": []
  },
  "requirementsText": "## Feature Hint\nLogin feature\n\n---\n\n## JIRA Issues\n\n### CXQA-123: Implement Login...\n..."
}
```

---

## Workflow: Test Scenario Generation (No MCP)

After running this skill, the AI agent uses the `requirementsText` to generate test scenarios:

1. **Run context-aggregator** → get `requirementsText`
2. **AI generates test scenarios** from the requirements text (Copilot does this natively) — each scenario should include a title, description, and steps formatted as plain text inside the description
3. **For each test**, pipe the description (with steps) via stdin to `xray-create-test` to create it in Jira
4. **Run `xray-create-test-plan`** to create the test plan
5. **Run `xray-add-tests-to-plan`** to link tests to the plan

---

## Required Environment Variables

| Variable | Purpose |
|----------|---------|
| `CONFLUENCE_USERNAME` | Jira/Confluence username (email) |
| `CONFLUENCE_TOKEN` | Jira/Confluence API token |

---

## Requirements

- **Python 3** installed with `requests` library (`pip install requests`)
