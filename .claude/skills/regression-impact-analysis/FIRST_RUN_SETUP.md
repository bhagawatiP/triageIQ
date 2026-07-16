# RIA Pipeline - First Run Setup

This guide helps new developers set up the RIA pipeline on their local machine.

---

## Prerequisites

- **Python 3.10 or higher** (Python 3.9 support ends April 2026)
- Git repository cloned
- JIRA/Xray API credentials

**Note**: Python 3.9 will work but shows deprecation warnings. Upgrade to Python 3.10+ for long-term compatibility.

---

## Step 1: Install Dependencies

```bash
cd .github/skills/regression-impact-analysis
pip install -r requirements.txt
```

**Required packages:**
- `httpx` - For JIRA/Xray API calls
- `sentence-transformers` - For semantic similarity (optional)

> **No AWS / Bedrock SDK is needed.** LLM reasoning is performed by the GitHub
> Copilot agent directly: the pipeline pauses at each reasoning stage and the
> agent fills that stage's output file (see `scripts/agent_reasoning.py`).

**If pip install fails**, install manually:
```bash
pip install httpx sentence-transformers
```

---

## Step 2: Configure Credentials

Copy the template and fill in your credentials:

```bash
cd configs
cp ria_config.env ria_config.env
```

Edit `ria_config.env` and fill in:

```bash
# XRAY API (required for test extraction)
XRAY_CLIENT_ID=your_xray_client_id
XRAY_CLIENT_SECRET=your_xray_client_secret

# JIRA API (required for RIA reports)
JIRA_BASE_URL=https://your-domain.atlassian.net
JIRA_USER=your.email@company.com
JIRA_API_TOKEN=your_jira_api_token

# Project Keys (comma-separated)
PROJECT_KEYS=YOUR_PROJECT_KEY

# LLM reasoning is handled by the GitHub Copilot agent directly (pause/resume;
# see scripts/agent_reasoning.py). No AWS / cloud LLM credentials are required.
```

---

## Step 3: First-Time Test Corpus Extraction

The first run will automatically fetch tests from Xray:

```bash
cd ../../../  # Back to repo root
python3 "${CLAUDE_SKILL_DIR}/scripts/ria_agent.py" \
    --user-prompt "Run RIA on my changes"
```

**What happens on first run:**
1. Detects missing test corpus
2. Automatically calls `tc_extractor.py` to fetch from Xray
3. Builds knowledge base (synonym groups, component map, flow registry)
4. Runs analysis on your changed methods

**Expected duration:** 2-5 minutes (depending on corpus size)

---

## Step 4: Verify Installation

Check that these directories were created:

```bash
ls -la .github/RIA_INPUT/     # Test corpus
ls -la .github/RIA_OUTPUT/    # Analysis results
```

You should see:
- `.github/RIA_INPUT/all_tcs_extracted.json` - Raw test corpus
- `.github/RIA_OUTPUT/knowledge_base/` - KB files
- `.github/RIA_OUTPUT/RIA_Report.html` - Analysis report

---

## Troubleshooting

### Error: "httpx is required"
```bash
pip install httpx
```

### Error: "Missing test corpus"
Your Xray credentials may be incorrect. Check `configs/ria_config.env`:
- XRAY_CLIENT_ID
- XRAY_CLIENT_SECRET
- PROJECT_KEYS

### Error: "No changed methods detected"
Make sure you have uncommitted changes in Java/TypeScript/JavaScript/Python files.

---

## What Gets Generated (First Run)

### `.github/RIA_INPUT/` (Test Corpus)
- `all_tcs_extracted.json` - All tests from your JIRA projects
- `all_tcs_EEM.json` - Tests from EEM project (if applicable)
- `all_tcs_EMOB.json` - Tests from EMOB project (if applicable)

### `.github/RIA_OUTPUT/knowledge_base/` (One-Time KB)
- `synonym_groups.json` - Domain vocabulary
- `component_map.json` - Code component mapping
- `flow_registry.json` - Business flow definitions
- `flow_dependencies.json` - Flow relationships

### `.github/RIA_OUTPUT/` (Per-Run Outputs)
- `stage1_entry_points.json` - Public endpoints
- `stage2_impacted_flows.json` - Directly impacted features
- `stage3_indirect_flows.json` - Related features
- `stage4_recommended_tests.json` - Initial test recommendations
- `stage5_refined_tests.json` - Refined test list
- `stage6_aggressive_tests.json` - Final recommendations
- `RIA_Report.html` - Interactive HTML report

---

## Next Steps

After first run completes successfully:

1. **Open the HTML report:**
   ```bash
   open .github/RIA_OUTPUT/RIA_Report.html
   ```

2. **Review recommended tests** in the "All Tests" section

3. **Run tests** marked as "MUST RUN" priority before merging

---

## For Other Products

When rolling out to a new product:

1. Copy the entire `.github/skills/regression-impact-analysis/` directory
2. Create new `configs/ria_config.env` with product-specific credentials
3. Update `PROJECT_KEYS` to match the new product's JIRA projects
4. Run first-time extraction (will fetch tests for the new product)

**Note:** Each product will have its own:
- Test corpus (`.github/RIA_INPUT/`)
- Knowledge base (`.github/RIA_OUTPUT/knowledge_base/`)

These are NOT part of the packaged distribution.
