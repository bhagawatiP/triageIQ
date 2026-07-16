#!/usr/bin/env python3
"""
Stage 0b: Build Component Map (RIA v2 - Framework-Agnostic)

Maps components to methods and keywords with comprehensive variation generation.

Keyword Variation Types:
  1. Original: "WorkPolicy"
  2. Lowercase: "workpolicy"
  3. Split camelCase: "work policy"
  4. With underscore: "work_policy"
  5. With dash: "work-policy"
  6. Partial words: "work", "policy"

Output: component_map.json
"""

import argparse
import json
import os
import sys
import re
from collections import defaultdict, Counter
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from serena_mcp_client import SerenaMCPClient
from configs.ria_config import RIA_OUTPUT_DIR, TC_DATA_PATH, REPO_ROOT


def generate_keyword_variations(component_name):
    """
    Generate keyword variations with deterministic priority order (BUG-C2 fix).

    GAP 6 (INTENTIONAL DEVIATION FROM SPEC, USER-APPROVED 2026-05-10):
    Single-word "partial parts" (e.g. "work", "policy", "template", "helper")
    are NOT emitted as standalone keywords, even though the HTML spec line 894
    says "Keywords include ALL forms (camelCase, spaces, underscores, dashes,
    partials)".

    Rationale: emitting single common English words causes massive false
    positives in Stage 4's check_component_mention. Examples observed:
      - "work"     -> matches every test mentioning workforce/workflow/...
      - "policy"   -> matches every test mentioning privacy policy
      - "template" -> matches every email/report template test
      - "helper"   -> matches generic "helper text" UI prose
    On the EEM corpus this generated 4,595 false matches per measurement.

    Decision: keep current 2-token-minimum behaviour. Compound phrases
    ("work policy", "policy template", "work policy template") are precise
    enough to avoid false positives but broad enough to match natural
    language test descriptions.

    Priority: Original -> lowercase -> split-spaces -> underscore -> dash.
    Multi-token components produce ALL N-1 .. 2-token contiguous sub-phrases
    (e.g. WorkPolicyTemplateHelper ->
       "work policy template helper",
       "work policy template", "policy template helper",
       "work policy",        "policy template", "template helper").
    These compound sub-phrases keep matching broad without resorting to
    single common-dictionary words.

    Example: WorkPolicy ->
        [WorkPolicy, workpolicy, "work policy", work_policy, work-policy]
    """
    variations = []

    def _push(value):
        if value and value not in variations:
            variations.append(value)

    # 1. Original (exact match)
    _push(component_name)

    # 2. Lowercase
    _push(component_name.lower())

    # 3. Split camelCase with spaces
    camel_parts = re.findall(r'[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\b)', component_name)
    if camel_parts:
        full_lower = ' '.join(camel_parts).lower()
        _push(full_lower)
        # 4. Underscore
        _push('_'.join(camel_parts).lower())
        # 5. Dash
        _push('-'.join(camel_parts).lower())

        # 6. Compound sub-phrases (length >= 2 tokens). This gives us
        #    "work policy", "policy template", "work policy template",
        #    etc., which are precise enough to avoid false positives but
        #    broad enough to match natural language test descriptions.
        n = len(camel_parts)
        if n >= 3:
            # All contiguous windows of size 2..n-1 (full phrase already added).
            for window in range(n - 1, 1, -1):
                for start in range(0, n - window + 1):
                    sub = ' '.join(camel_parts[start:start + window]).lower()
                    _push(sub)
        # GAP-1: Do NOT emit single-token parts (camel_parts[i].lower()).
        # Single common words cause Stage 4 false positives.

    return variations  # deterministic ordered list


# --------------------------------------------------------------------------
# GAP 4 + GAP 5 helpers (USER DECISIONS 2026-05-10):
#
# GAP 4 (Title Case display_name):
#   - Spec line 889 says component_name should be Title Case ("Work Policy"
#     not "WorkPolicy"). User decision: keep `component_name` PascalCase for
#     lookups (matches Java class name) AND add a `display_name` field with
#     spec-compliant Title Case for human-readable rendering.
#
# GAP 5 (Component consolidation, file_count):
#   - Spec line 893 says "file_count: Number of files contributing to this
#     component". User decision: NOT one-component-per-file. Components
#     sharing a base name (after stripping framework suffixes) are merged
#     into ONE component entry. Methods, keywords, and file paths are
#     aggregated across the group; file_count = number of files in the group.
# --------------------------------------------------------------------------

# Framework / role suffixes are now DISCOVERED at runtime from the codebase
# (build_discovered_vocabularies.py - Stage 0d) instead of hardcoded. The
# discovery uses PascalCase-tail frequency analysis, so suffixes that are
# Spring/JEE/architectural patterns surface naturally with no per-product
# tuning. Order in the loaded tuple matters: longer suffixes appear first
# so 'ServiceImpl' is stripped before 'Service'.
def _load_framework_suffixes(kb_dir):
    """Load discovered framework suffixes from KB (runtime-generated)."""
    from pathlib import Path as _Path
    path = _Path(kb_dir) / "discovered_framework_suffixes.json"
    if not path.exists():
        raise FileNotFoundError(
            f"[build_component_map] Required prerequisite missing: {path}\n"
            f"  Run first: python3 build_discovered_vocabularies.py --only framework_suffixes\n"
            f"  Or invoke via the orchestrator: python3 ria_agent.py --rebuild-kb"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"[build_component_map] Failed to parse {path}: {e}")

    suffixes = tuple(data.get("suffixes", []))
    if not suffixes:
        # Small codebases (few classes) may legitimately yield no recurring
        # framework suffixes. Degrade gracefully: with an empty tuple,
        # _strip_framework_suffixes is a no-op and each class name is treated
        # as its own component base. This avoids a hard crash on tiny repos.
        print(
            f"[build_component_map] WARNING: {path} contains 0 framework "
            f"suffixes (codebase too small to discover any). Proceeding with "
            f"no suffix stripping; each class is its own component base."
        )
    return suffixes


# Lazily filled in build_component_map() once the KB output dir is known.
_FRAMEWORK_SUFFIXES = ()


def _strip_framework_suffixes(component_name):
    """
    Strip one or more framework/role suffixes from a class name to reveal
    its business-domain base name.

    Examples:
        WorkPolicyTemplateHelper       -> WorkPolicyTemplate
        WorkPolicyTemplateServiceImpl  -> WorkPolicyTemplate
        WorkPolicyTemplateDao          -> WorkPolicyTemplate
        WorkPolicyTemplate             -> WorkPolicyTemplate (no change)
        UserController                 -> User
        TradeValidator                 -> Trade

    Guarantees: returns at least 3 characters (won't shrink to a stub).
    """
    out = component_name
    # Iteratively peel suffixes - some classes have multiple
    # (e.g. 'WorkPolicyTemplateCacheServiceImpl').
    changed = True
    while changed:
        changed = False
        for suf in _FRAMEWORK_SUFFIXES:
            if out.endswith(suf) and len(out) - len(suf) >= 3:
                out = out[:-len(suf)]
                changed = True
                break
    return out or component_name


def _to_title_case(component_name):
    """
    Convert PascalCase / camelCase to space-separated Title Case.

    Examples:
        WorkPolicyTemplateHelper -> "Work Policy Template Helper"
        workPolicy               -> "Work Policy"
        SSOLogin                 -> "SSO Login"  (acronyms preserved)
        getCalloutAgents         -> "Get Callout Agents"

    Used for the GAP-4 display_name field, leaving component_name (which is
    used for lookups by downstream stages) as the original PascalCase form.
    """
    # Same camelCase splitter used elsewhere - keeps acronyms (SSO, ID).
    parts = re.findall(
        r'[A-Z]+(?=[A-Z][a-z]|[^A-Za-z]|$)|[A-Z]?[a-z]+',
        component_name
    )
    if not parts:
        return component_name
    titled = []
    for p in parts:
        if p.isupper():
            # Preserve acronyms as-is (SSO, ID, URL).
            titled.append(p)
        else:
            titled.append(p[:1].upper() + p[1:].lower())
    return ' '.join(titled)


def _clean_tokenize(text):
    """Tokenization cleanup: lowercase, strip non-alphanumeric, collapse spaces (BUG-C1)."""
    text = text.lower()
    text = re.sub(r'[^a-z0-9 ]', ' ', text)  # remove quotes/brackets/JSON syntax
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def precompute_test_texts(test_corpus):
    """Pre-compute cleaned test texts once for reuse across all component searches."""
    test_texts = []
    for test in test_corpus:
        step_text = ' '.join([
            (step.get('action') or '') + ' ' +
            (step.get('data') or '') + ' ' +
            (step.get('result') or '')
            for step in test.get('steps', [])
        ])

        raw = ' '.join([
            test.get('summary') or '',
            test.get('description') or '',
            step_text
        ])
        cleaned = _clean_tokenize(raw)

        test_texts.append({
            'test': test,
            'text': cleaned,
            'words': cleaned.split(),
        })
    return test_texts


def search_tests_with_variations(test_corpus, variations, precomputed_texts=None):
    """
    Search test corpus with ALL variations and extract context keywords.

    BUG-C1 fix: Tokenization cleanup (strip JSON syntax, quotes, brackets)
    before splitting words so we don't capture noise like '"agentoid":"abc"'.

    Args:
        test_corpus: Raw test corpus list (used only if precomputed_texts is None)
        variations: List of keyword variations to search for
        precomputed_texts: Pre-computed test texts from precompute_test_texts()

    Returns:
        - matched_tests: Tests that mention any variation
        - context_keywords: Phrases found around variations
    """
    matched_tests = []
    context_keywords = set()

    # Use pre-computed texts if available, otherwise compute inline (backward compat)
    if precomputed_texts is not None:
        test_texts = precomputed_texts
    else:
        test_texts = precompute_test_texts(test_corpus)

    # Deduplicate variations after cleaning (many collapse to the same string)
    seen_cleaned = set()
    unique_variations = []
    for variation in variations:
        var_lower = _clean_tokenize(variation)
        if not var_lower or var_lower in seen_cleaned:
            continue
        seen_cleaned.add(var_lower)
        unique_variations.append(var_lower)

    seen_test_ids = set()

    # Search with each unique cleaned variation
    for var_lower in unique_variations:
        for test_data in test_texts:
            text = test_data['text']

            if var_lower in text:
                # Dedupe matched tests by issue_key
                tid = test_data['test'].get('issue_key') or id(test_data['test'])
                if tid not in seen_test_ids:
                    seen_test_ids.add(tid)
                    matched_tests.append(test_data['test'])

                # Extract bigrams/trigrams containing the variation
                words = test_data['words']
                for i in range(len(words)):
                    if i < len(words) - 1:
                        bigram = f"{words[i]} {words[i+1]}"
                        if var_lower in bigram:
                            context_keywords.add(bigram)
                    if i < len(words) - 2:
                        trigram = f"{words[i]} {words[i+1]} {words[i+2]}"
                        if var_lower in trigram:
                            context_keywords.add(trigram)

    return matched_tests, context_keywords


def filter_false_positives(context_keywords, primary_variations):
    """
    Filter out false positive keywords.

    GAP-7 FIX: The previous implementation used [:3] of variations as anchors.
    For 4+ token components (e.g. WorkPolicyTemplateHelper) the first three
    variations are: original CamelCase, lowercase concatenation, and the
    full 4-token split. None of these can possibly be contained inside a
    bigram or trigram extracted from the test corpus, so 100% of context
    keywords were filtered out (258 -> 0 for WorkPolicyTemplateHelper).

    Fix strategy (per HTML spec Stage 0b Step 5, lines 765-798):
    Use 2-token contiguous sub-phrases of every variation as anchors. A
    keyword is kept iff it CONTAINS at least one anchor.

    Example: For WorkPolicyTemplateHelper:
        2-token anchors: "work policy", "policy template", "template helper"
        Plus single-token concatenation: "workpolicytemplatehelper"
        - "create work policy"     contains "work policy"     -> KEEP
        - "work policy template"   contains "work policy"     -> KEEP
        - "privacy policy rules"   contains none              -> FILTER
        - "associate work policy"  contains "work policy"     -> KEEP
    """
    if not primary_variations:
        return set()

    # Build anchor list:
    #   - For each variation, derive every 2-token contiguous sub-phrase.
    #   - 2-token variations are themselves anchors.
    #   - Single-token / concatenated forms (e.g. "workpolicytemplatehelper")
    #     are kept as-is so plain substring matches still apply.
    anchors = []
    for var in primary_variations:
        var_lower = var.lower().strip()
        if not var_lower:
            continue
        tokens = var_lower.split()
        if len(tokens) <= 1:
            # Concatenated / single-token form -> use as-is.
            anchors.append(var_lower)
        elif len(tokens) == 2:
            anchors.append(var_lower)
        else:
            # 3+ tokens: extract every contiguous 2-token sub-phrase.
            for i in range(len(tokens) - 1):
                anchors.append(f"{tokens[i]} {tokens[i+1]}")

    # Dedupe anchors.
    anchors = list({a for a in anchors if a})
    if not anchors:
        return set()

    # Keep keyword if it contains any anchor as a substring.
    filtered = set()
    for keyword in context_keywords:
        keyword_lower = keyword.lower()
        if any(anchor in keyword_lower for anchor in anchors):
            filtered.add(keyword)
    return filtered


def discover_components_from_structure(repo_root, serena):
    """Discover components from codebase structure.

    Scans all source files matching the active language profile's
    source_extensions. Language is auto-detected from the repository.
    """
    print("Discovering components from codebase structure...")
    components = {}

    # Get source extensions from the active language profile.
    # FAIL-FAST: a missing/broken language profile produces a component
    # map scoped to the wrong file types, silently dropping real
    # components. Surface the failure instead of guessing extensions.
    import sys as _sys
    configs_dir = str(Path(__file__).parent.parent / 'configs')
    if configs_dir not in _sys.path:
        _sys.path.insert(0, configs_dir)
    try:
        from ria_config import get_active_profile
    except ImportError as e:
        raise RuntimeError(
            f"[build_component_map] Cannot load configs/ria_config.py: {e}\n"
            f"Root cause: configs directory missing or ria_config.py import failed.\n"
            f"Fix: ensure {configs_dir}/ria_config.py exists and is valid.\n"
            f"CLI: python3 -c 'from ria_config import get_active_profile; print(get_active_profile())'"
        ) from e
    profile = get_active_profile()
    extensions = profile.get('source_extensions')
    if not extensions:
        raise RuntimeError(
            f"[build_component_map] Active language profile "
            f"'{profile.get('name', 'unknown')}' declares no source_extensions.\n"
            f"Root cause: profile is missing 'source_extensions' list.\n"
            f"Fix: add e.g. source_extensions=['.java'] to the active profile in "
            f"configs/ria_config.py."
        )
    print(f"  Language profile: {profile.get('name', 'unknown')}")

    for ext in extensions:
        pattern = f"**/*{ext}"
        files = list(Path(repo_root).glob(pattern))
        print(f"  Found {len(files)} {ext} files, filtering and processing...")

        processed = 0
        for file_path in files:
            rel_path = str(file_path.relative_to(repo_root))

            # Skip test/generated files and RIA tool files
            if any(skip in rel_path.lower() for skip in [
                '/test/', '/tests/', '/generated/', '/target/', '/build/',
                'node_modules', '__pycache__',
                'regression-impact-analysis/', '.github/skills/', 'backup/skills/'
            ]):
                continue

            processed += 1
            if processed % 500 == 0:
                print(f"    Progress: {processed} files, {len(components)} components...")

            # PREVENTION-OVER-DETECTION: index ALL source files including
            # top-level files (parts < 2). Earlier this gate silently
            # dropped repository-root scripts that are still legitimate
            # components; downstream stages then failed to resolve those
            # filenames to a canonical component.
            # Use last directory name or filename as component
            file_name = Path(rel_path).stem  # Filename without extension

            # Convert to PascalCase if needed
            if '_' in file_name:
                component_name = ''.join(word.capitalize() for word in file_name.split('_'))
            elif '-' in file_name:
                component_name = ''.join(word.capitalize() for word in file_name.split('-'))
            else:
                component_name = file_name

            # BUG FIX: Use file path as unique key to avoid collisions
            # Different packages can have same class name - they are DIFFERENT components
            component_key = rel_path  # Use full file path as unique identifier

            if component_key not in components:
                components[component_key] = {
                    "component_name": component_name,  # Store the display name
                    "file_path": rel_path,
                    "methods": set()
                }

            # Extract methods from file. Filter out getters/setters/Object
            # boilerplate (GAP-C3) so the call-tree noise floor stays low.
            # FAIL-FAST: previously this `except Exception: continue` silently
            # dropped components when the symbol parser hit a real bug. We
            # now surface the failure with the offending file path.
            try:
                symbols = serena.get_symbols_overview(rel_path)
            except Exception as e:
                raise RuntimeError(
                    f"[build_component_map] Failed to extract symbols from "
                    f"'{rel_path}': {e}\n"
                    f"Root cause: serena.get_symbols_overview() raised an "
                    f"exception, indicating a parser bug or unreadable file.\n"
                    f"Fix: inspect the file for syntax errors and verify the "
                    f"active language profile in configs/ria_config.py."
                ) from e
            for sym in symbols.get('symbols', []):
                if sym['kind'] not in ('method', 'function'):
                    continue
                name = sym['name']
                if name in ('toString', 'equals', 'hashCode', 'clone', 'finalize'):
                    continue
                # Only exclude trivial accessors, not business logic methods
                # Keep methods like getCalloutAgents, getWorkPolicyConfiguration, etc.
                # Trivial = get/set/is + single word (e.g., getName, setId, isActive)
                # Business = get/set/is + compound/long names (e.g., getWorkPolicyTemplatesByAgentIdsAndProgram)
                if name.startswith(('get', 'set', 'is')):
                    # If method name has more than 15 chars, it's likely business logic
                    # Or if it contains multiple capital letters (compound name)
                    is_compound = len([c for c in name if c.isupper()]) >= 3
                    is_long = len(name) > 15
                    if not (is_compound or is_long):
                        continue  # Skip trivial accessor
                components[component_key]["methods"].add(name)

    print(f"  Found {len(components)} components")
    return components


def build_component_map(test_corpus_path, repo_root, output_path):
    """
    Build component map with comprehensive keyword variations.

    Args:
        test_corpus_path: Path to all_tcs_extracted.json
        repo_root: Repository root path
        output_path: Output path for component_map.json
    """
    print(f"\n{'=' * 80}")
    print(f"STAGE 0b: Build Component Map")
    print(f"{'=' * 80}")

    # Load discovered framework suffixes (Stage 0d output). This was a
    # hardcoded tuple in earlier revisions; it is now data-driven.
    global _FRAMEWORK_SUFFIXES
    kb_dir = os.path.dirname(output_path) or os.path.join(RIA_OUTPUT_DIR, "knowledge_base")
    _FRAMEWORK_SUFFIXES = _load_framework_suffixes(kb_dir)
    print(f"Loaded {len(_FRAMEWORK_SUFFIXES)} discovered framework suffixes from KB")

    # Initialize Serena MCP
    serena = SerenaMCPClient(repo_path=repo_root, enabled=True, max_symbols=10000)

    # Discover components
    components_data = discover_components_from_structure(repo_root, serena)

    # Load test corpus
    print("\nLoading test corpus...")
    with open(test_corpus_path, 'r', encoding='utf-8') as f:
        test_corpus = json.load(f)
    print(f"  Loaded {len(test_corpus)} tests")

    # ---------------------------------------------------------------
    # GAP 5 (USER DECISION 2026-05-10): Spec-based consolidation.
    # Group raw class entries by their FRAMEWORK-STRIPPED base name so a
    # business component (e.g. "WorkPolicyTemplate") that is split across
    # multiple files (Helper, Service, Dao, ServiceImpl, Cache, Servlet,
    # Job, Model) collapses into ONE entry with file_count = N.
    # ---------------------------------------------------------------
    print("\nConsolidating components by base name (GAP 5)...")
    grouped = {}  # base_name -> {component_name, files, methods, raw_class_names}
    # PREVENTION-OVER-DETECTION: include EVERY discovered component, even
    # those with zero method symbols. Interfaces, data classes, enums and
    # constants files commonly extract no callable methods but they are
    # still legitimate components that may be referenced by flows. Dropping
    # them here is what causes Stage 4's "component not in component_map"
    # error to trigger when one of these files is the sole change.
    for comp_key, comp_data in components_data.items():
        raw_class = comp_data.get("component_name", Path(comp_key).stem)
        base = _strip_framework_suffixes(raw_class)
        # Use base as the canonical component_name (PascalCase, lookup-friendly).
        bucket = grouped.setdefault(base, {
            "component_name": base,         # PascalCase canonical name
            "files": [],
            "methods": set(),
            "raw_class_names": set(),
        })
        bucket["files"].append(comp_data.get("file_path", comp_key))
        bucket["methods"].update(comp_data["methods"])
        bucket["raw_class_names"].add(raw_class)

    print(f"  Consolidated {len(components_data)} files -> {len(grouped)} components")

    # Build component map with keyword variations
    print("\nGenerating keyword variations and searching tests...")
    print(f"Processing ALL {len(grouped)} consolidated components...")

    # Pre-compute test texts ONCE (avoids re-processing 10k tests per component)
    precomputed_texts = precompute_test_texts(test_corpus)
    print(f"  Pre-computed text for {len(precomputed_texts)} tests")

    components = []

    processed = 0
    for base_name, bucket in grouped.items():
        processed += 1
        if processed % 100 == 0:
            print(f"  Progress: {processed}/{len(grouped)} components processed...")

        comp_name = bucket["component_name"]              # PascalCase (lookups)
        display_name = _to_title_case(comp_name)          # Title Case (human)

        # Step 1: Generate ALL variations from canonical PascalCase name.
        variations = generate_keyword_variations(comp_name)

        # GAP 5: also harvest variations from the raw class names so a
        # downstream consumer searching for "WorkPolicyTemplateHelper" (the
        # original class name) still finds this consolidated entry.
        for raw_class in bucket["raw_class_names"]:
            if raw_class and raw_class != comp_name:
                for v in generate_keyword_variations(raw_class):
                    if v not in variations:
                        variations.append(v)

        # Step 2: Search tests with ALL variations (using pre-computed texts)
        matched_tests, context_keywords = search_tests_with_variations(test_corpus, variations, precomputed_texts)

        # Step 3: Filter false positives
        filtered_keywords = filter_false_positives(context_keywords, variations)

        # Step 4: Build the final keyword list with two-tier ranking so the
        # 30-cap (per spec line 891 "limited to top 30") never drops core
        # variations.
        #
        # GAP-7 follow-on: When a component has hundreds of context keywords
        # (e.g. WorkPolicyTemplateHelper has 252 natural-language phrases),
        # naive ``sorted(...)[:30]`` collates ASCII first ("15 work policy",
        # "2 work policy", etc.) and EVICTS the foundational variation
        # tokens (e.g. "work policy", "policy template", "template helper").
        # Stage 4's word-boundary matcher then loses 145 tests because the
        # surviving 30 keywords are all noun-prefix phrases that rarely
        # match natural test text.
        #
        # Fix: variations are ALWAYS included (deduped, deterministic order).
        # Remaining slots are filled with filtered context keywords sorted
        # alphabetically (matches spec sample output ordering).
        seen = set()
        ordered_keywords = []
        for v in variations:
            if v and v not in seen:
                seen.add(v)
                ordered_keywords.append(v)
        # Append context keywords (alphabetical, deduped against variations).
        for ctx in sorted(filtered_keywords):
            if ctx and ctx not in seen:
                seen.add(ctx)
                ordered_keywords.append(ctx)
        final_keywords = ordered_keywords[:30]

        # Extract package path from FIRST file (representative path).
        files = bucket["files"]
        first_file = files[0] if files else ""
        package_path = "/".join(first_file.split("/")[:-1]) if first_file else ""

        # PREVENTION-OVER-DETECTION: include EVERY consolidated component
        # unconditionally. The previous "matched_tests > 0 OR methods > 0"
        # gate silently dropped interface / enum / constants components,
        # producing a sparse component_map.json that triggered Stage 4's
        # "no component in component_map matches stem" fail-fast for
        # changes touching those files.
        # GAP 4: add display_name (Title Case, spec line 889).
        # GAP 5: add file_count (spec line 893) and file_paths list.
        if True:
            components.append({
                "component_name": comp_name,                  # PascalCase (lookups)
                "display_name": display_name,                  # Title Case (human-readable)
                "package_path": package_path,
                "file_paths": sorted(files),                   # all files in the group
                "file_count": len(files),                      # GAP 5
                "raw_class_names": sorted(bucket["raw_class_names"]),
                # Keep ALL methods (no [:50] truncation) so call-tree
                # tracers in build_flow_dependencies and stage4 can match
                # any modified method, regardless of alphabetical position.
                # The previous [:50] cap silently dropped methods like
                # partialAgentSwap, sameDayPartialAgentSwap, etc., which
                # made cross-file DIRECT detection impossible.
                "methods": sorted(list(bucket["methods"])),
                "keywords": final_keywords,
                "test_count": len(matched_tests),
            })

    # Save component map
    comp_map = {
        "components": components,
        "total_components": len(components),
        "source": "codebase structure + test corpus keyword extraction"
    }

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(comp_map, f, indent=2, ensure_ascii=False)

    print(f"\n{'=' * 80}")
    print(f"STAGE 0b COMPLETE")
    print(f"{'=' * 80}")
    print(f"Components mapped: {len(components)}")
    print(f"Output saved: {output_path}")

    return comp_map


def main():
    parser = argparse.ArgumentParser(description="Build Component Map (Stage 0b)")
    parser.add_argument("--test-corpus", default=TC_DATA_PATH, help="Test corpus path")
    parser.add_argument("--repo-root", default=REPO_ROOT, help="Repository root")
    parser.add_argument("--output", default=os.path.join(RIA_OUTPUT_DIR, "knowledge_base", "component_map.json"),
                        help="Output path")

    args = parser.parse_args()

    try:
        build_component_map(args.test_corpus, args.repo_root, args.output)
        sys.exit(0)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
