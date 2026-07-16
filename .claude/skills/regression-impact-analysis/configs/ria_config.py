"""
RIA Configuration

Loads required CREDENTIALS from environment variables (set via
ria_config.env file). Everything else (paths, log levels, language
detection, etc.) is hardcoded to sensible defaults in this module and
intentionally NOT exposed in ria_config.env.

LLM reasoning is performed by the GitHub Copilot agent directly (the pipeline
pauses at each reasoning stage and the agent fills that stage's output file;
see scripts/agent_reasoning.py). RIA does NOT call AWS Bedrock or any other
cloud LLM, so no AWS credentials are required.

Usage:
    1. Copy ria_config.env.template to ria_config.env
    2. Fill in the required credentials (Xray, JIRA)
    3. Run scripts — they will load config from ria_config.env automatically
"""

import os
from pathlib import Path


def load_env_file(env_file: str) -> None:
    """
    Load environment variables from a .env file.
    Sets environment variables that don't already exist.
    """
    if not os.path.exists(env_file):
        return

    with open(env_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, value = line.partition('=')
                os.environ.setdefault(key.strip(), value.strip())


def load_config() -> None:
    """
    Load configuration from ria_config.env file.
    Searches for config file in:
    1. .github/skills/regression-impact-analysis/configs/ria_config.env
    2. Current directory
    """
    # Try to find config file
    config_paths = [
        Path(__file__).parent / 'ria_config.env',  # Same directory as this file
        Path.cwd() / '.github' / 'skills' / 'regression-impact-analysis' / 'configs' / 'ria_config.env',
        Path.cwd() / 'ria_config.env'
    ]

    for config_path in config_paths:
        if config_path.exists():
            load_env_file(str(config_path))
            print(f"[ria_config] Loaded configuration from: {config_path}")
            return

    print("[ria_config] WARNING: ria_config.env not found. Using environment variables only.")


# Auto-load config when module is imported
load_config()


# =============================================================================
# Configuration Access
# =============================================================================
# Required credentials are read from environment variables (set in
# ria_config.env). Everything else is hardcoded to sensible defaults below
# and is intentionally NOT exposed in ria_config.env to keep developer
# configuration minimal.

# Xray Cloud API (REQUIRED - set in ria_config.env)
XRAY_CLIENT_ID = os.getenv('XRAY_CLIENT_ID', '')
XRAY_CLIENT_SECRET = os.getenv('XRAY_CLIENT_SECRET', '')
PROJECT_KEYS = [k.strip() for k in os.getenv('PROJECT_KEYS', '').split(',') if k.strip()]

# Test Filtering (can be overridden in ria_config.env by advanced users)
APPROVED_ONLY = os.getenv('APPROVED_ONLY', 'true').lower() in ('true', '1', 'yes')
_priority_str = os.getenv('PRIORITY_FILTER', '').strip()
PRIORITY_FILTER = [p.strip() for p in _priority_str.split(',') if p.strip()] if _priority_str else None

# Knowledge Base Storage (local JSON KB built by the pipeline)
KB_STORAGE_TYPE = os.getenv('KB_STORAGE_TYPE', 'local')

# AWS Bedrock Knowledge Base (Pre-provisioned by DevOps)
RIA_KB_ENABLED = os.getenv('RIA_KB_ENABLED', 'true').lower() == 'true'
RIA_KB_ID = os.getenv('RIA_KB_ID', '')
RIA_KB_REGION = os.getenv('RIA_KB_REGION', 'us-east-1')
RIA_KB_ENDPOINT = os.getenv('RIA_KB_ENDPOINT', f'https://bedrock-agent-runtime.{RIA_KB_REGION}.amazonaws.com')
RIA_AWS_ACCESS_KEY_ID = os.getenv('RIA_AWS_ACCESS_KEY_ID', '')
RIA_AWS_SECRET_ACCESS_KEY = os.getenv('RIA_AWS_SECRET_ACCESS_KEY', '')
RIA_KB_MAX_RESULTS = int(os.getenv('RIA_KB_MAX_RESULTS', '5'))
RIA_KB_RELEVANCE_THRESHOLD = float(os.getenv('RIA_KB_RELEVANCE_THRESHOLD', '0.50'))
RIA_KB_MAX_ROUNDS = int(os.getenv('RIA_KB_MAX_ROUNDS', '3'))

# Bedrock query cache
KB_ENABLE_LOCAL_CACHE = os.getenv('KB_ENABLE_LOCAL_CACHE', 'true').lower() == 'true'
KB_CACHE_DIR = os.getenv('KB_CACHE_DIR', '.github/RIA_OUTPUT/kb_cache')
KB_CACHE_TTL_HOURS = int(os.getenv('KB_CACHE_TTL_HOURS', '24'))

# LLM reasoning — Stage 1.5 (Method Understanding), test-keyword generation,
# Stage 7 (TC Judgment) and Stage 8 (semantic dedup).
#
# These stages are driven by the GitHub Copilot agent directly (pause/resume;
# see scripts/agent_reasoning.py). There is NO AWS Bedrock call and NO cloud
# credential requirement. The model-id constants below are kept only as
# descriptive labels for report metadata.
RIA_LLM_MODEL_ID = 'copilot'
RIA_LLM_MODEL_ID_DEEP = 'copilot'
RIA_LLM_MAX_TOKENS = 4096

# Local Knowledge Base (if KB_STORAGE_TYPE='local')
# One-time KB (synonym_groups, component_map) rebuilds only when files are
# missing OR when the user explicitly requests a rebuild via the prompt
# (translated to the --rebuild-kb CLI flag). There is no time-based auto
# rebuild — it is purely prompt-driven.
# Hardcoded sensible default — not exposed in ria_config.env
KB_PATH = '.github/RIA_OUTPUT/knowledge_base'

# KB Files
KB_FILES = {
    'flow_registry': 'flow_registry.json',
    'component_map': 'component_map.json',
    'flow_dependencies': 'flow_dependencies.json',
    'reverse_index': 'flow_dependencies_reverse_index.json'
}

# Paths (hardcoded sensible defaults — not exposed in ria_config.env)
# Scripts must be executed from the repository root (CWD), so relative paths
# resolve correctly.
REPO_ROOT = '.'
RIA_INPUT_DIR = '.github/RIA_INPUT'
RIA_OUTPUT_DIR = '.github/RIA_OUTPUT'
TC_DATA_PATH = os.path.join(RIA_INPUT_DIR, 'all_tcs_extracted.json')

# Scoring
# FLOW_MATCH_POINTS removed - redundant after tag filtering (Step 6)
# Tag filter already proved flow match, no need to reward it again in scoring
FLOW_MATCH_POINTS = 0  # Was 50, but redundant after tag pre-filter
COMPONENT_MATCH_POINTS = 40
INCLUSION_THRESHOLD = 40  # Reduced from 90 since FLOW_MATCH_POINTS=0

# Priority Multipliers and Criticality Rules: REMOVED.
#
#
# Criticality is now derived in stage4_test_correlation.assign_criticality()
# from impact_type ONLY:
#   DIRECT   -> CRITICAL
#   INDIRECT -> HIGH
#   Other    -> MEDIUM

# KB Build Configuration
FLOW_REGISTRY = {
    'min_test_count': 5,
    'min_entry_points': 1,
    'tag_extraction_pattern': r'\[([^\]]+)\]',
    'enable_fuzzy_matching': True,
    'fuzzy_threshold': 0.8
}

COMPONENT_MAP = {
    'min_methods': 1,
    'min_keywords': 3,
    'min_classes': 3,
    'use_tfidf': True,
    'max_keywords': 20,
    'keyword_min_frequency': 2
}

FLOW_DEPENDENCIES = {
    'max_call_depth': 10,
    'enable_indirect': True,
    'indirect_methods': ['data_flow', 'entity_usage'],
    'enable_reverse_index': True
}

# =============================================================================
# Phase 2: Multi-Language Support
# =============================================================================
# LANGUAGE_PROFILES defines per-language configuration for the RIA pipeline.
# This is purely additive - existing Java logic remains the default and is
# untouched. Other modules can call get_active_profile() to obtain the right
# extensions / regexes / entry-point markers for the active language.
#
# RIA_LANGUAGE controls selection:
#   - 'auto'       : detect_repo_language() picks the language with the most
#                    git-tracked source files in the repo (default).
#   - 'java'       : force Java (legacy behaviour, identical to Phase 1).
#   - 'typescript' : force TypeScript / Angular.
#   - 'javascript' : force JavaScript / NodeJS.
#   - 'python'     : force Python.

LANGUAGE_PROFILES = {
    "java": {
        "name": "Java/Spring Boot",
        "source_extensions": [".java", ".kt"],
        "test_patterns": ["Test.java", "Tests.java", "IT.java", "Test.kt"],
        "test_dir_markers": ["src/test/", "/test/"],
        "method_declaration_regex": (
            r'(?:public|protected|private|static|final|synchronized|abstract|native|default)\s+'
            r'[\w<>\[\],\s\?]+\s+{METHOD}\s*\('
        ),
        "entry_point_markers": [
            "@RestController", "@Controller", "@RequestMapping", "@GetMapping",
            "@PostMapping", "@PutMapping", "@DeleteMapping", "@PatchMapping",
            "@Scheduled", "@EventListener", "@JmsListener",
            "@Path", "@GET", "@POST", "@PUT", "@DELETE",
            "@WebServlet", "@WebService",
            "@KafkaListener", "@RabbitListener", "@SqsListener",
            "@PostConstruct", "@PreDestroy",
            "@SpringBootApplication", "CommandLineRunner",
            "implements Runnable", "implements Callable",
            "extends Thread", "extends TimerTask",
            "extends HttpServlet", "extends GenericServlet",
            "implements Filter", "implements MessageListener",
            "implements ApplicationListener", "implements ServletContextListener",
        ],
        "class_declaration_regex": (
            r'(?:public|protected|private|static|final|abstract)\s+'
            r'(?:class|interface|enum|record)\s+(\w+)'
        ),
        "scan_glob": "**/*.java",
    },
    "typescript": {
        "name": "TypeScript/Angular",
        "source_extensions": [".ts", ".tsx"],
        "test_patterns": [".spec.ts", ".test.ts", ".spec.tsx", ".test.tsx"],
        "test_dir_markers": ["__tests__/", "/test/", "/spec/"],
        "method_declaration_regex": (
            r'(?:public|private|protected|static|async)?\s*'
            r'{METHOD}\s*\([^)]*\)\s*[:{]'
        ),
        "entry_point_markers": [
            "@Component", "@Injectable", "@Controller",  # NestJS/Angular
            "@Get", "@Post", "@Put", "@Delete", "@Patch",
            "@HostListener", "@Input", "@Output",
            "@Query", "@Mutation", "@Resolver",  # GraphQL
            "app.get", "app.post", "app.put", "app.delete",
            "router.get", "router.post", "router.put", "router.delete",
            "export default", "export function", "export class",
            "module.exports",
        ],
        "class_declaration_regex": (
            r'(?:export\s+)?(?:abstract\s+)?(?:class|interface|enum|type|namespace)\s+(\w+)'
        ),
        "scan_glob": "**/*.ts",
    },
    "javascript": {
        "name": "JavaScript/NodeJS",
        "source_extensions": [".js", ".jsx", ".mjs"],
        "test_patterns": [".spec.js", ".test.js", ".spec.jsx", ".test.jsx"],
        "test_dir_markers": ["__tests__/", "/test/"],
        "method_declaration_regex": r'(?:function\s+|const\s+){METHOD}\s*[=(]',
        "entry_point_markers": [
            "app.get", "app.post", "app.put", "app.delete",
            "router.get", "router.post", "router.put", "router.delete",
            "@Controller", "@Get", "@Post",  # NestJS
            "exports.handler",  # AWS Lambda
            "export default", "export function",
            "module.exports",
        ],
        "class_declaration_regex": r'(?:class|function)\s+(\w+)',
        "scan_glob": "**/*.js",
    },
    "python": {
        "name": "Python",
        "source_extensions": [".py"],
        "test_patterns": ["_test.py", "test_", "_tests.py"],
        "test_dir_markers": ["/tests/", "/test/"],
        "method_declaration_regex": r'^\s*(?:async\s+)?def\s+{METHOD}\s*\(',
        "entry_point_markers": [
            "@app.route", "@app.get", "@app.post",
            "@router.get", "@router.post", "@router.put", "@router.delete",
            "@api_view", "@require_http_methods",
            "@celery.task", "@shared_task",
            "@click.command", "@click.group",
            "if __name__",
            "@pytest.fixture",
        ],
        "class_declaration_regex": r'^class\s+(\w+)',
        "scan_glob": "**/*.py",
    },
}

# Language selection: 'auto' | 'java' | 'typescript' | 'javascript' | 'python'
# Default is 'auto', but an explicit RIA_LANGUAGE env var (set by the agent's
# --language flag via set_active_language) is honored at import time so that
# spawned sub-processes inherit the forced language instead of re-detecting.
# Use the --language CLI flag (or set_active_language) to override at runtime.
RIA_LANGUAGE = os.getenv('RIA_LANGUAGE', 'auto').lower()

# Cache for the auto-detected language so repeat calls don't re-scan the repo.
_DETECTED_LANGUAGE_CACHE = None


def detect_repo_language(repo_root) -> str:
    """
    Auto-detect the primary language of the repo by counting git-tracked
    source files for each language profile.

    Test/build/dependency directories are excluded so they don't bias the
    count. Falls back to 'java' if nothing is found (preserves Phase 1
    behaviour on this Java-only codebase).
    """
    import subprocess

    try:
        repo_root_str = str(repo_root)
    except Exception:
        return 'java'

    counts = {}
    for lang, profile in LANGUAGE_PROFILES.items():
        lang_count = 0
        for ext in profile['source_extensions']:
            try:
                result = subprocess.run(
                    ['git', 'ls-files', f'*{ext}'],
                    cwd=repo_root_str,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except Exception:
                continue

            if result.returncode != 0:
                continue

            for f in result.stdout.split('\n'):
                f = f.strip()
                if not f:
                    continue
                # Skip test directories and dependency / build folders so they
                # do not bias the detection result.
                if any(marker in f for marker in profile['test_dir_markers']):
                    continue
                if '/node_modules/' in f or 'node_modules/' in f:
                    continue
                if '/target/' in f or 'target/' in f:
                    continue
                if '/build/' in f or 'build/' in f:
                    continue
                if '/dist/' in f or 'dist/' in f:
                    continue
                lang_count += 1
        counts[lang] = lang_count

    if not counts or max(counts.values()) == 0:
        print("[Language Detection] WARNING: No source files found for any known language. Defaulting to 'java'.")
        return 'java'

    primary = max(counts, key=counts.get)
    print(f"[Language Detection] Auto-detected: {primary} ({counts[primary]} files) | All: {counts}")
    return primary


def get_active_profile() -> dict:
    """
    Return the active language profile.

    If RIA_LANGUAGE is explicit ('java', 'typescript', etc.) that profile is
    returned directly. If RIA_LANGUAGE == 'auto' the result of
    detect_repo_language() is used and cached.
    """
    global _DETECTED_LANGUAGE_CACHE

    lang = (RIA_LANGUAGE or 'auto').lower()
    if lang != 'auto':
        if lang not in LANGUAGE_PROFILES:
            print(f"[ria_config] WARNING: unknown RIA_LANGUAGE='{lang}', "
                  f"falling back to 'java'")
            return LANGUAGE_PROFILES['java']
        return LANGUAGE_PROFILES[lang]

    if _DETECTED_LANGUAGE_CACHE is None:
        _DETECTED_LANGUAGE_CACHE = detect_repo_language(REPO_ROOT)

    return LANGUAGE_PROFILES.get(_DETECTED_LANGUAGE_CACHE, LANGUAGE_PROFILES['java'])


def set_active_language(lang: str) -> None:
    """
    Force the active language at runtime (used by --language CLI flag).
    Pass 'auto' to re-enable auto-detection.
    """
    global RIA_LANGUAGE, _DETECTED_LANGUAGE_CACHE
    if lang and lang.lower() in (set(LANGUAGE_PROFILES.keys()) | {'auto'}):
        RIA_LANGUAGE = lang.lower()
        _DETECTED_LANGUAGE_CACHE = None  # Invalidate cache
        os.environ['RIA_LANGUAGE'] = RIA_LANGUAGE


# Logging & report flags (hardcoded sensible defaults — not exposed in ria_config.env)
RIA_LOG_LEVEL = 'INFO'
RIA_LOG_TO_FILE = False
RIA_INCLUDE_INTERMEDIATE = False

# Features
FEATURES = {
    'enable_kb_auto_discovery': True,
    'enable_semantic_matching': True,
    'enable_flow_clustering': True,
    'enable_indirect_flows': True,
    'enable_html_report': True,
    'enable_criticality_scoring': True
}


# =============================================================================
# Validation & Utilities
# =============================================================================

def validate_config() -> list:
    """Validate configuration and return list of errors."""
    errors = []

    # Check Xray credentials
    if not XRAY_CLIENT_ID:
        errors.append("XRAY_CLIENT_ID not set in ria_config.env")
    if not XRAY_CLIENT_SECRET:
        errors.append("XRAY_CLIENT_SECRET not set in ria_config.env")

    # Check AWS Bedrock KB configuration (legacy; only when explicitly
    # opted into bedrock KB storage). Default storage is 'local'.
    if KB_STORAGE_TYPE == 'bedrock' and RIA_KB_ENABLED:
        if not RIA_AWS_ACCESS_KEY_ID:
            errors.append("RIA_AWS_ACCESS_KEY_ID not set in ria_config.env")
        if not RIA_AWS_SECRET_ACCESS_KEY:
            errors.append("RIA_AWS_SECRET_ACCESS_KEY not set in ria_config.env")
        if not RIA_KB_ID:
            errors.append("RIA_KB_ID not set in ria_config.env (Contact DevOps for KB ID)")

    # LLM reasoning is provided by the Copilot agent directly (pause/resume).
    # No cloud LLM credentials are required.

    return errors


def print_config() -> None:
    """Print current configuration (masked sensitive data)."""
    print("\nRIA v7 Configuration:")
    print(f"  XRAY_CLIENT_ID: {'***' if XRAY_CLIENT_ID else 'NOT SET'}")
    print(f"  XRAY_CLIENT_SECRET: {'***' if XRAY_CLIENT_SECRET else 'NOT SET'}")
    print(f"  PROJECT_KEYS: {', '.join(PROJECT_KEYS)}")
    print(f"  KB_STORAGE_TYPE: {KB_STORAGE_TYPE}")

    if KB_STORAGE_TYPE == 'bedrock':
        print(f"  RIA_KB_ENABLED: {RIA_KB_ENABLED}")
        print(f"  RIA_KB_ID: {RIA_KB_ID or 'NOT SET'}")
        print(f"  RIA_KB_REGION: {RIA_KB_REGION}")
        print(f"  RIA_AWS_ACCESS_KEY_ID: {'***' if RIA_AWS_ACCESS_KEY_ID else 'NOT SET'}")
        print(f"  RIA_AWS_SECRET_ACCESS_KEY: {'***' if RIA_AWS_SECRET_ACCESS_KEY else 'NOT SET'}")
        print(f"  RIA_KB_MAX_RESULTS: {RIA_KB_MAX_RESULTS}")
        print(f"  RIA_KB_RELEVANCE_THRESHOLD: {RIA_KB_RELEVANCE_THRESHOLD}")
        print(f"  KB_ENABLE_LOCAL_CACHE: {KB_ENABLE_LOCAL_CACHE}")
    else:
        print(f"  KB_PATH: {KB_PATH}")

    print(f"  REPO_ROOT: {REPO_ROOT}")
    print(f"  RIA_INPUT_DIR: {RIA_INPUT_DIR}")
    print(f"  RIA_OUTPUT_DIR: {RIA_OUTPUT_DIR}")
    print(f"  INCLUSION_THRESHOLD: {INCLUSION_THRESHOLD}")


def ensure_directories() -> None:
    """Create runtime directories if they don't exist."""
    dirs = [RIA_INPUT_DIR, RIA_OUTPUT_DIR]

    if KB_STORAGE_TYPE == 'local':
        dirs.append(KB_PATH)

    if KB_ENABLE_LOCAL_CACHE:
        dirs.append(KB_CACHE_DIR)

    for dir_path in dirs:
        full_path = os.path.join(REPO_ROOT, dir_path) if not os.path.isabs(dir_path) else dir_path
        Path(full_path).mkdir(parents=True, exist_ok=True)


# =============================================================================
# Main
# =============================================================================

if __name__ == '__main__':
    # Print config when run directly
    print_config()

    # Validate
    errors = validate_config()
    if errors:
        print("\n⚠️  Configuration Errors:")
        for error in errors:
            print(f"  - {error}")
    else:
        print("\n✅ Configuration is valid")

    # Ensure directories
    try:
        ensure_directories()
        print(f"\n✅ Runtime directories created/verified")
    except Exception as e:
        print(f"\n❌ Failed to create directories: {e}")
