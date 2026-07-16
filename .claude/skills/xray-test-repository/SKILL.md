---
name: xray-test-repository
description: Manages Xray Test Repository folders - list existing folders, create new folders, and organize tests into functional folders. No MCP server required.
---

# Xray Test Repository Management

## When to Use This Skill

Use this skill to organize and manage tests within the Xray Test Repository folder structure.

**Trigger conditions:**
- Need to organize newly created tests into functional folders
- Scan existing folder structure in Test Repository
- Create new functional folders for test organization
- Add/move tests to specific folders

---

## How It Works

The Xray Test Repository allows hierarchical organization of tests. This skill provides three core operations:

1. **List Folders**: Scan existing folder structure and return folder paths/IDs
2. **Create Folder**: Create new folders (supports nested paths)
3. **Add Tests to Folder**: Organize test issues into folders by functionality

---

## Typical Workflow

When creating new test cases:

1. **Identify Functionality**: Determine the functional area (e.g., "Authentication", "Reporting", "API")
2. **Scan for Existing Folder**: List folders to check if a folder for this functionality exists
3. **Create Folder if Needed**: If no matching folder exists, create one
4. **Add Test to Folder**: Place the newly created test into the appropriate folder

---

## Scripts

### 1. List Test Repository Folders

Lists all folders in the Test Repository for a given project.

```powershell
python ${CLAUDE_SKILL_DIR}/scripts/xray_list_folders.py --project CXQA
```

**Output:**
```json
{
  "success": true,
  "project": "CXQA",
  "folders": [
    {
      "id": "12345",
      "name": "Authentication",
      "path": "/Authentication",
      "parentId": null
    },
    {
      "id": "12346",
      "name": "Login",
      "path": "/Authentication/Login",
      "parentId": "12345"
    }
  ]
}
```

---

### 2. Create Folder

Creates a new folder in the Test Repository. Supports nested paths.

```powershell
# Create top-level folder
python ${CLAUDE_SKILL_DIR}/scripts/xray_create_folder.py --project CXQA --name "API Tests"

# Create nested folder
python ${CLAUDE_SKILL_DIR}/scripts/xray_create_folder.py --project CXQA --name "Login" --parent "12345"
```

**Arguments:**
| Argument | Required | Description |
|----------|----------|-------------|
| `--project` | Yes | Jira project key (e.g. `CXQA`) |
| `--name` | Yes | Folder name |
| `--parent` | No | Parent folder ID (omit for top-level folder) |

**Output:**
```json
{
  "success": true,
  "folderId": "12347",
  "name": "API Tests",
  "path": "/API Tests",
  "parentId": null
}
```

---

### 3. Organize Tests (Interactive & Automated)

**NEW**: Intelligently organizes tests by searching ALL subfolders recursively before creating new folders.

#### Interactive Mode (Prompts for folder name)

```powershell
# User will be prompted to enter folder name
python ${CLAUDE_SKILL_DIR}/scripts/xray_organize_tests.py --project AN --tests AN-142995 AN-142996

# Output:
============================================================
ORGANIZE TESTS INTO TEST REPOSITORY FOLDER
============================================================

Tests to organize: AN-142995, AN-142996

Enter folder name to add tests to:
  (e.g., 'Analytics Policies', 'Login Tests', 'API Tests')

> Analytics Policies

✓ Found existing folder at: /Analytics Hub/Analytics Policies
  (Searched through all subfolders)

[1/2] ✓ AN-142995
[2/2] ✓ AN-142996

SUMMARY: Tests organized: 2/2
```

#### Automated Mode (Folder name provided)

```powershell
# Non-interactive - folder name provided via --functionality
python ${CLAUDE_SKILL_DIR}/scripts/xray_organize_tests.py --project AN --functionality "Analytics Policies" --tests AN-142995
```

**Key Features:**
- ✅ **Recursive Search**: Searches ALL subfolders to find existing folder with matching name
- ✅ **Smart Matching**: Uses existing folder even if located in nested path (e.g., `/Analytics Hub/Analytics Policies`)
- ✅ **Auto-Creation**: Creates folder only if truly doesn't exist anywhere
- ✅ **Prevents Duplicates**: Won't create multiple folders with same name
- ✅ **Bulk Organization**: Organize multiple tests at once with progress tracking

**Arguments:**
| Argument | Required | Description |
|----------|----------|-------------|
| `--project` | Yes | Jira project key (e.g. `AN`) |
| `--functionality` | No* | Folder name (*required for automated mode, optional for interactive) |
| `--parent` | No | Parent folder name for nested organization |
| `--tests` | Yes | One or more test issue keys (space-separated) |

**Output:**
```json
{
  "success": true,
  "organized": 2,
  "folderPath": "/Analytics Hub/Analytics Policies",
  "tests": ["AN-142995", "AN-142996"],
  "method": "xray-test-repository"
}
```

---

### 4. Add Tests to Folder (Low-level)

Adds one or more test issues to a Test Repository folder.

```powershell
# Add single test
python ${CLAUDE_SKILL_DIR}/scripts/xray_add_tests_to_folder.py --project CXQA --folder "12345" --tests "CXQA-101"

# Add multiple tests
python ${CLAUDE_SKILL_DIR}/scripts/xray_add_tests_to_folder.py --project CXQA --folder "12345" --tests "CXQA-101" "CXQA-102" "CXQA-103"
```

**Arguments:**
| Argument | Required | Description |
|----------|----------|-------------|
| `--project` | Yes | Jira project key |
| `--folder` | Yes | Folder ID or folder path (e.g., "/Authentication/Login") |
| `--tests` | Yes | One or more test issue keys (space-separated) |

**Output:**
```json
{
  "success": true,
  "folderId": "12345",
  "folderPath": "/Authentication",
  "testsAdded": ["CXQA-101", "CXQA-102"],
  "count": 2
}
```

---

## Integration with Test Creation

### Simple Workflow (Recommended)

When creating tests via `xray-create-test`, use the built-in `--functionality` parameter:

```powershell
# Create test with automatic folder organization
python ${CLAUDE_PLUGIN_ROOT}/.claude/skills/xray-create-test/scripts/xray_create_test.py `
  --project AN `
  --summary "Verify user login with valid credentials" `
  --type Manual `
  --functionality "Authentication"

# The test is automatically:
# 1. Created in Jira/Xray
# 2. Folder "Authentication" searched recursively
# 3. Added to existing folder OR new folder created
# 4. Organized in Test Repository
```

### Manual Organization Workflow

For existing tests that need organization:

```powershell
# Interactive mode - prompts for folder name
python ${CLAUDE_SKILL_DIR}/scripts/xray_organize_tests.py `
  --project AN `
  --tests AN-101 AN-102 AN-103

# Automated mode - folder name provided
python ${CLAUDE_SKILL_DIR}/scripts/xray_organize_tests.py `
  --project AN `
  --functionality "Login Tests" `
  --tests AN-101 AN-102 AN-103
```

### Legacy Workflow (Manual Steps)

<details>
<summary>Click to expand old manual approach</summary>

```powershell
# 1. Create the test
$result = python ${CLAUDE_PLUGIN_ROOT}/.claude/skills/xray-create-test/scripts/xray_create_test.py --project CXQA --summary "Verify user login" --type Manual | ConvertFrom-Json

# 2. Determine functionality (e.g., "Authentication")
$functionality = "Authentication"

# 3. List folders to find matching folder
$folders = python ${CLAUDE_SKILL_DIR}/scripts/xray_list_folders.py --project CXQA | ConvertFrom-Json

# 4. Find or create folder
$folder = $folders.folders | Where-Object { $_.name -eq $functionality }
if (-not $folder) {
    $folder = python ${CLAUDE_SKILL_DIR}/scripts/xray_create_folder.py --project CXQA --name $functionality | ConvertFrom-Json
}

# 5. Add test to folder
python ${CLAUDE_SKILL_DIR}/scripts/xray_add_tests_to_folder.py --project CXQA --folder $folder.id --tests $result.key
```

</details>

---

## Required Environment Variables

| Variable | Purpose |
|----------|---------|
| `XRAY_CLIENT_ID` | Xray Cloud API client ID |
| `XRAY_CLIENT_SECRET` | Xray Cloud API client secret |
| `CONFLUENCE_USERNAME` | Atlassian username (email) - for Jira REST API |
| `CONFLUENCE_TOKEN` | Atlassian API token - for Jira REST API |

**Note**: The organize_tests script needs Jira REST API access to resolve project keys to numeric IDs.

---

## Agent Workflow Integration

The AI agent should use the **`xray_organize_tests.py`** script which handles everything automatically:

```python
# Agent workflow (simplified)
1. Create test(s) using xray_create_test.py with --functionality parameter
2. Script automatically:
   - Searches entire repo for matching folder name
   - Uses existing folder if found (anywhere in hierarchy)
   - Creates new folder if needed
   - Adds tests to folder
```

**Old approach (deprecated)**:
- ~~Extract functionality from test~~
- ~~Scan folders manually~~
- ~~Match or create folder~~
- ~~Call add_tests_to_folder~~

**New approach (recommended)**:
- Just call `xray_organize_tests.py` with folder name
- Everything is handled automatically with recursive search

---

## Key Features

✅ **Recursive Subfolder Search**: Searches entire Test Repository hierarchy  
✅ **Interactive Mode**: Prompts user for folder name when needed  
✅ **Automated Mode**: Accepts folder via `--functionality` flag  
✅ **Smart Duplicate Prevention**: Won't create folders with duplicate names  
✅ **Bulk Organization**: Organize multiple tests in one operation  
✅ **Progress Tracking**: Shows real-time progress for each test  
✅ **Detailed Summary**: Clear summary of organized vs failed tests  

---

## Notes

- **Recursive Search**: Folder names are searched across ALL subfolders automatically
- **Case-Sensitive Matching**: Folder names must match exactly (case-sensitive)
- **Exact Name Match**: Searches by exact folder name, not partial matches
- **Path Format**: Folder paths use format `/ParentFolder/ChildFolder`
- **Project ID Resolution**: Automatically converts project key to numeric ID
- **Error Handling**: Continues processing remaining tests if one fails
- Use **descriptive folder names** that match your functional test areas

