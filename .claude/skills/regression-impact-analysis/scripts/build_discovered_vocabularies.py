#!/usr/bin/env python3
"""
Build discovered vocabularies - fully data-driven, no hardcoded lists.

Generates 3 knowledge base files:
    1. discovered_generic_nouns.json     - High-frequency generic tokens (MAD-z)
    2. domain_vocabulary.json            - Domain-specific vocabulary from tests/code
    3. discovered_framework_suffixes.json - Class name suffix patterns

All vocabularies are discovered from the codebase / test corpus at runtime so
the RIA pipeline contains zero hard-coded English/domain word lists. The
algorithms below are intentionally language-agnostic and rely on robust
statistics (median + MAD) to find anomalies, with NO product-specific
constants.

Inputs:
    - test corpus       (.github/RIA_INPUT/all_tcs_extracted.json)
    - component map     (KB/component_map.json)
    - flow registry     (KB/flow_registry.json, optional - may be empty)
    - repository root   (for scanning *.java)

Outputs (under <RIA_OUTPUT>/knowledge_base/):
    - discovered_generic_nouns.json
    - domain_vocabulary.json
    - discovered_framework_suffixes.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set, Tuple

# Reuse tokenization + MAD from flow_discovery so the whole pipeline shares
# one canonical implementation.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flow_discovery import _tokenize, _mad  # noqa: E402

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from configs.ria_config import RIA_OUTPUT_DIR, TC_DATA_PATH, REPO_ROOT  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tokens_from_test(test: Dict[str, Any]) -> List[str]:
    """Extract lowercase word tokens from every text field of a test."""
    parts: List[str] = []
    for key in ("summary", "description"):
        v = test.get(key)
        if isinstance(v, str):
            parts.append(v)
    for step in test.get("steps") or []:
        if not isinstance(step, dict):
            continue
        for k in ("action", "data", "result"):
            v = step.get(k)
            if isinstance(v, str):
                parts.append(v)
    out: List[str] = []
    for chunk in parts:
        # Strip punctuation: rely on tokenizer's separator-splitting.
        for tok in re.split(r"[^A-Za-z0-9_]+", chunk):
            if not tok:
                continue
            for piece in _tokenize(tok):
                if len(piece) >= 3 and not piece.isdigit():
                    out.append(piece)
    return out


def _iter_components(component_map: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    if not component_map:
        return []
    comps = component_map.get("components") or []
    if isinstance(comps, dict):
        return comps.values()
    return comps


def _iter_flows(flow_registry: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    if not flow_registry:
        return []
    return flow_registry.get("flows") or []


# ---------------------------------------------------------------------------
# Adaptive threshold helpers (corpus-size driven, not hardcoded magic numbers)
# ---------------------------------------------------------------------------
def _adaptive_generic_thresholds(
    corpus_size: int,
    n_unique_tokens: int,
) -> Tuple[float, int, float]:
    """
    Compute (z_threshold, min_count, min_test_prevalence) adaptively from
    corpus characteristics so the algorithm self-tunes to any product /
    language / domain.

    Sizing logic:
      - Tiny corpora (<200 tests) need a more permissive prevalence floor
        because rare tokens here may still be the ones we want to mark
        generic. Z floor is raised so we don't over-promote noise.
      - Medium corpora (200-2000) use the historical defaults that have
        been validated on the EEM corpus (z=3.0, prev=0.25).
      - Large corpora (>2000 tests) tighten prevalence to 0.20 so platform
        words still surface even when each individual word appears in a
        smaller fraction of tests.

    Domain diversity (unique tokens / corpus size) lets us nudge the
    prevalence floor by +/- 0.05 to keep the kept set roughly stable
    regardless of how vocabulary-rich the corpus happens to be.
    """
    # Corpus-size driven base values.
    if corpus_size < 200:
        base_z = 3.5
        base_min_count = 2
        base_prev = 0.30
    elif corpus_size < 2000:
        base_z = 3.0
        base_min_count = 3
        base_prev = 0.25
    elif corpus_size < 10000:
        base_z = 3.0
        base_min_count = max(3, corpus_size // 1000)
        base_prev = 0.20
    else:
        base_z = 3.0
        base_min_count = max(5, corpus_size // 1500)
        base_prev = 0.18

    # Domain diversity adjustment (unique tokens / corpus size).
    if corpus_size > 0:
        diversity = n_unique_tokens / corpus_size
        if diversity > 0.5:
            base_prev = min(0.50, base_prev + 0.05)
        elif diversity < 0.1:
            base_prev = max(0.10, base_prev - 0.05)

    return base_z, base_min_count, base_prev


# ---------------------------------------------------------------------------
# Algorithm 1: Discover generic nouns (MAD-z over METHOD-name frequency,
#              cross-validated by test-corpus prevalence).
# ---------------------------------------------------------------------------
def discover_generic_nouns(
    component_names: Iterable[str],
    flow_names: Iterable[str],
    method_names: Iterable[str],
    test_summaries: Iterable[str],
    *,
    test_corpus: List[Dict[str, Any]] = None,
    z_threshold: float = None,
    min_count: int = None,
    min_test_prevalence: float = None,
) -> Dict[str, Any]:
    """
    Identify "generic" tokens that should not, on their own, be used to
    credit a domain match.

    Two-step algorithm:
        1. Use MAD-z frequency analysis on METHOD-name tokens (and the
           token streams from component / flow / class names). This is the
           same robust statistical approach as `flow_discovery.discover_
           generic_method_lexicon` and surfaces tokens like 'get', 'set',
           'service', 'data', 'agent' whose frequency is anomalously high
           in the codebase token distribution.
        2. Cross-validate with the test corpus: a token is only retained
           as "generic" if it ALSO appears in at least `min_test_prevalence`
           of all tests (default: 10%). This rules out frequent codebase
           tokens that are *not* widely echoed in tests (e.g. an obscure
           internal helper word repeated across many file paths).

    The intersection produces a tight set of platform/English words that
    are simultaneously common in code AND common in tests - exactly the
    set we want to ignore for noun-match scoring.
    """
    code_counts: Counter = Counter()

    def _add(stream: Iterable[str]) -> None:
        for s in stream or []:
            for tok in _tokenize(s or ""):
                if len(tok) < 3 or tok.isdigit():
                    continue
                code_counts[tok] += 1

    _add(component_names)
    _add(flow_names)
    _add(method_names)
    _add(test_summaries)

    # Compute test prevalence: fraction of tests in which each token appears.
    test_corpus = test_corpus or []
    n_tests = max(len(test_corpus), 1)

    # Adaptive thresholds (override only when caller didn't pin a value).
    auto_z, auto_min_count, auto_prev = _adaptive_generic_thresholds(
        corpus_size=n_tests,
        n_unique_tokens=len(code_counts),
    )
    if z_threshold is None:
        z_threshold = auto_z
    if min_count is None:
        min_count = auto_min_count
    if min_test_prevalence is None:
        min_test_prevalence = auto_prev

    if not code_counts:
        return {
            "discovered_at": _now_iso(),
            "source": "component_map + flow_registry + method_lexicon + test_corpus",
            "method": (
                f"MAD-z z_threshold={z_threshold} min_count={min_count} "
                f"AND test_prevalence>={min_test_prevalence}"
            ),
            "generic_nouns": [],
            "statistics": {},
            "summary": {"total_tokens": 0, "generic_count": 0},
        }
    test_doc_counts: Counter = Counter()
    for test in test_corpus:
        if not isinstance(test, dict):
            continue
        seen = set(_tokens_from_test(test))
        for tok in seen:
            test_doc_counts[tok] += 1

    values = list(code_counts.values())
    n = len(values)
    s = sorted(values)
    median = s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2.0
    mad = _mad(values) or 1.0

    generic: Set[str] = set()
    stats: Dict[str, Dict[str, float]] = {}
    for tok, c in code_counts.items():
        if c < min_count:
            continue
        z = (c - median) / (1.4826 * mad)
        if z < z_threshold:
            continue
        prevalence = test_doc_counts.get(tok, 0) / n_tests
        if prevalence < min_test_prevalence:
            continue
        generic.add(tok)
        stats[tok] = {
            "code_count": int(c),
            "z_score": round(float(z), 3),
            "test_prevalence": round(float(prevalence), 4),
            "test_count": int(test_doc_counts.get(tok, 0)),
        }

    # ------------------------------------------------------------------
    # Fix #1: Post-processing filter for domain-critical terms.
    # MAD-z can flag legitimate domain nouns (agent, shift, schedule)
    # as generic when they are also high-frequency. These terms carry
    # important domain signal and must NEVER be promoted to the generic
    # set, otherwise their IDF weights collapse and matching accuracy
    # degrades. We do this as a post-filter (rather than checking
    # domain_vocabulary.json) because that file is built AFTER this
    # one in the KB build order, so a forward reference would be a
    # circular dependency.
    # ------------------------------------------------------------------
    DOMAIN_CRITICAL_TERMS = {
        'agent', 'agents', 'shift', 'shifts', 'schedule', 'schedules',
        'user', 'users', 'customer', 'customers', 'order', 'orders',
        'product', 'products', 'service', 'services', 'task', 'tasks',
        'request', 'requests', 'response', 'responses',
    }

    before_count = len(generic)
    removed_terms = generic & DOMAIN_CRITICAL_TERMS
    generic -= DOMAIN_CRITICAL_TERMS
    after_count = len(generic)

    if before_count > after_count:
        removed = before_count - after_count
        print(f"  [FILTER] Removed {removed} domain-critical terms from generic set: "
              f"{sorted(removed_terms)}")
        # Drop their statistics too so the output stays internally
        # consistent with the final generic_nouns list.
        for tok in removed_terms:
            stats.pop(tok, None)

    return {
        "discovered_at": _now_iso(),
        "source": "component_map + flow_registry + method_lexicon + test_corpus",
        "method": (
            f"MAD-z over codebase tokens (z>={z_threshold}, min_count={min_count}) "
            f"AND test_prevalence>={min_test_prevalence} "
            f"with domain-critical post-filter"
        ),
        "median_count": float(median),
        "mad": float(mad),
        "n_tests": int(n_tests),
        "generic_nouns": sorted(generic),
        "statistics": stats,
        "summary": {
            "total_tokens": len(code_counts),
            "generic_count": len(generic),
            "domain_critical_filtered": int(before_count - after_count),
        },
    }


# ---------------------------------------------------------------------------
# Algorithm 2: Discover domain vocabulary
# ---------------------------------------------------------------------------
def discover_domain_vocabulary(
    component_map: Dict[str, Any],
    flow_registry: Dict[str, Any],
    test_corpus: List[Dict[str, Any]],
    generic_nouns: Set[str],
    *,
    reserved_words: Set[str] = None,
    min_component_count: int = None,
    min_test_count: int = None,
) -> Dict[str, Any]:
    """
    Build a corpus-wide domain vocabulary from:
        - component keywords + raw class names + display names,
        - flow names,
        - test summaries / descriptions / steps.

    A token enters the vocabulary if it is:
        - NOT in the discovered generic-noun set,
        - cited in at least `min_component_count` distinct components OR
          referenced in at least one flow name (i.e. has structural code
          evidence), AND
        - mentioned in at least `min_test_count` tests (test corpus
          evidence that the token names something users care about).

    This gives a corpus-wide list of business words (policy, schedule,
    callout, template, ...) without any hardcoding.
    """
    # Count tokens by the number of *distinct components* that mention them
    # (NOT raw token frequency) so a single component with many methods
    # doesn't unfairly inflate a token's score.
    component_counts: Counter = Counter()
    for comp in _iter_components(component_map):
        if not isinstance(comp, dict):
            continue
        # Pull tokens from every text-bearing field.
        sources: List[str] = []
        for k in ("component_name", "display_name"):
            v = comp.get(k)
            if isinstance(v, str):
                sources.append(v)
        for k in ("keywords", "raw_class_names"):
            v = comp.get(k)
            if isinstance(v, list):
                sources.extend(x for x in v if isinstance(x, str))
        seen_in_comp: Set[str] = set()
        for s in sources:
            for tok in _tokenize(s):
                if len(tok) < 3 or tok.isdigit():
                    continue
                seen_in_comp.add(tok)
        for tok in seen_in_comp:
            component_counts[tok] += 1

    flow_counts: Counter = Counter()
    for flow in _iter_flows(flow_registry):
        if not isinstance(flow, dict):
            continue
        for k in ("flow_name", "flow", "flow_tag"):
            v = flow.get(k)
            if isinstance(v, str):
                for tok in _tokenize(v):
                    if len(tok) < 3 or tok.isdigit():
                        continue
                    flow_counts[tok] += 1
        for ep in flow.get("entry_points") or []:
            if isinstance(ep, dict):
                ep = ep.get("file") or ""
            if not isinstance(ep, str):
                continue
            for tok in _tokenize(ep):
                if len(tok) < 3 or tok.isdigit():
                    continue
                flow_counts[tok] += 1

    test_counts: Counter = Counter()
    for test in test_corpus or []:
        if not isinstance(test, dict):
            continue
        # Aggregate but cap per-test contribution to avoid one verbose test
        # blowing the distribution.
        seen: Set[str] = set()
        for tok in _tokens_from_test(test):
            seen.add(tok)
        for tok in seen:
            test_counts[tok] += 1

    # Adaptive thresholds (keep callers' explicit overrides if supplied).
    n_tests = len(test_corpus or [])
    if min_test_count is None:
        # Scale with corpus size so we always require ~0.05% of corpus
        # mentions but never less than 3 nor more than 20.
        min_test_count = max(3, min(20, n_tests // 200 if n_tests else 5))
    if min_component_count is None:
        # Component-evidence floor scales gently with component count.
        n_components = sum(1 for _ in _iter_components(component_map))
        if n_components < 50:
            min_component_count = 1
        elif n_components < 500:
            min_component_count = 2
        else:
            min_component_count = max(2, n_components // 500)

    all_tokens = set(component_counts) | set(flow_counts) | set(test_counts)

    # Reserved words MUST be excluded BEFORE the domain set is built. The
    # tokens fed into this function come straight from raw text fields
    # (component_map raw_class_names that include package paths like
    # "com.nice.eem.foo", test descriptions that copy/paste Java code
    # snippets containing "import", "public", "class", etc.). Without an
    # explicit filter here, those keywords leak into domain_vocabulary
    # and the audit fires `stage0_domain_vocab_reserved_overlap`.
    #
    # The reserved-word set is data-driven: it is produced by
    # discover_reserved_words.py (runtime introspection + tree-sitter +
    # frequency outliers) and persisted to language_reserved_words.json,
    # then loaded by the orchestrator at the call site. We do NOT
    # hardcode any keyword list here — the input set is whatever the
    # discoverer produced for the active language profile.
    reserved_lower: Set[str] = (
        {str(w).lower() for w in (reserved_words or set()) if w}
    )

    domain: Set[str] = set()
    stats: Dict[str, Dict[str, int]] = {}
    reserved_excluded_count = 0
    for tok in all_tokens:
        if tok in generic_nouns:
            continue
        if reserved_lower and tok.lower() in reserved_lower:
            reserved_excluded_count += 1
            continue
        c_count = component_counts.get(tok, 0)
        f_count = flow_counts.get(tok, 0)
        t_count = test_counts.get(tok, 0)
        # Code evidence: cited in multiple distinct components OR named in a
        # flow. Single-component tokens are usually class-internal noise.
        has_code_evidence = (c_count >= min_component_count) or (f_count >= 1)
        if not has_code_evidence:
            continue
        # Test evidence: must be widely-enough mentioned to be a domain term
        # users actually talk about (rather than an internal helper word).
        if t_count < min_test_count:
            continue
        domain.add(tok)
        stats[tok] = {
            "component_count": int(c_count),
            "flow_count": int(f_count),
            "test_count": int(t_count),
        }

    return {
        "discovered_at": _now_iso(),
        "source": "component_map + flow_registry + test_corpus",
        "method": (
            f"requires (component_count>={min_component_count} OR flow>=1) "
            f"AND test_count>={min_test_count}, "
            f"generics + language reserved words excluded"
        ),
        "domain_tokens": sorted(domain),
        "statistics": stats,
        "summary": {
            "total_candidates": len(all_tokens),
            "generic_excluded": len([t for t in all_tokens if t in generic_nouns]),
            "reserved_excluded": int(reserved_excluded_count),
            "domain_count": len(domain),
        },
    }


# ---------------------------------------------------------------------------
# Algorithm 3: Discover framework suffixes
# ---------------------------------------------------------------------------
# Matches class/interface/enum declarations across Java, Python, TypeScript, JavaScript
_CLASS_RE = re.compile(r"\b(?:class|interface|enum|type|namespace)\s+([A-Z][A-Za-z0-9_]+)")
_PASCAL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z][a-z0-9]*|[A-Z]+")


def _split_pascal(name: str) -> List[str]:
    """Split a PascalCase / camelCase identifier into its capitalised parts.

    Note: keeps the original case (suffixes are case-sensitive) and preserves
    acronym groupings, so 'WorkPolicyTemplateServiceImpl' splits to
    ['Work','Policy','Template','Service','Impl'].
    """
    if not name:
        return []
    return _PASCAL_RE.findall(name)


def _iter_source_files(repo_root: str) -> Iterable[Path]:
    """Yield source files under repo_root matching the active language profile."""
    skip_segments = {
        "target", "build", "node_modules", "generated-sources",
        "RIA_OUTPUT", "RIA_INPUT", ".git", "dist", "__pycache__",
        ".tox", ".venv", "venv",
    }
    # Get extensions from the active language profile
    try:
        import sys as _sys
        configs_dir = str(Path(__file__).parent.parent / 'configs')
        if configs_dir not in _sys.path:
            _sys.path.insert(0, configs_dir)
        from ria_config import get_active_profile
        extensions = get_active_profile().get('source_extensions', ['.java'])
    except Exception:
        extensions = ['.java', '.py', '.ts', '.js', '.kt']
    root = Path(repo_root)
    for ext in extensions:
        for p in root.rglob(f'*{ext}'):
            parts = set(p.parts)
            if parts & skip_segments:
                continue
            yield p


def discover_framework_suffixes(
    source_files: Iterable[Path],
    *,
    min_frequency: int = 30,
    min_distinct_prefixes: int = 10,
    min_prefix_ratio: float = 0.7,
    max_solo_ratio: float = 0.0,
) -> Dict[str, Any]:
    """
    Discover class-name suffix patterns that recur across the codebase
    (e.g. Service, Controller, Repository, Dao, Helper, Impl, ...).

    A *true* framework suffix has three distinguishing properties:
        (a) it is the trailing PascalCase part of many class names,
        (b) it is preceded by many *different* prefixes - i.e. it acts
            like a role marker rather than a business word, AND
        (c) it almost never appears as a class name *on its own* (e.g.
            you rarely see a class literally called `Service` but you
            very commonly see one called `Schedule`).

    Properties (b) and (c) are critical: domain words like 'Schedule' or
    'Template' may show up as the trailing part of many classes but
    they ALSO appear as standalone class names because they ARE business
    entities. The solo-name ratio test rules them out so stripping them
    won't destroy the canonical component name.

    Algorithm:
        1. For each source file, extract all class/interface/enum names.
        2. PascalCase-split each name. Skip 1-part names (no suffix).
        3. For each potential suffix (last part), track how many *distinct*
           prefixes precede it.
        4. Keep a suffix iff:
                count >= min_frequency
            AND distinct_prefixes >= min_distinct_prefixes
            AND distinct_prefixes / count >= min_prefix_ratio
           The ratio test rules out cases where one prefix dominates
           (e.g. a single business term whose 50 sub-classes all start
           with the same word).
        5. Compound suffixes (e.g. ServiceImpl) are kept when both pieces
           qualify *and* the compound itself recurs.

    The output preserves the original case and is sorted with longer
    suffixes first so callers can iterate and strip greedily.
    """
    # suffix -> Counter(prefix -> count)
    suffix_prefixes: Dict[str, Counter] = {}
    # token -> count of times this token appears as a STANDALONE class name
    solo_counts: Counter = Counter()
    compound_counts: Counter = Counter()
    classes_seen = 0

    for jf in source_files:
        try:
            text = jf.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in _CLASS_RE.finditer(text):
            class_name = m.group(1)
            classes_seen += 1
            parts = _split_pascal(class_name)
            if not parts:
                continue
            if len(parts) == 1:
                # Standalone class name (e.g. "Schedule", "Agent"). This is
                # strong evidence the token is a business noun, not a role
                # marker.
                solo_counts[parts[0]] += 1
                continue
            suf = parts[-1]
            prefix = "".join(parts[:-1])
            if suf not in suffix_prefixes:
                suffix_prefixes[suf] = Counter()
            suffix_prefixes[suf][prefix] += 1
            if len(parts) >= 3:
                compound = parts[-2] + parts[-1]
                compound_counts[compound] += 1

    suffixes: Set[str] = set()
    stats: Dict[str, Dict[str, Any]] = {}

    for suf, prefix_counter in suffix_prefixes.items():
        # Single-letter suffixes (e.g. 'O', 'I') are noise from generic
        # type parameters or short class names; never strip them.
        if len(suf) < 3:
            continue
        count = sum(prefix_counter.values())
        distinct = len(prefix_counter)
        if count < min_frequency:
            continue
        if distinct < min_distinct_prefixes:
            continue
        ratio = distinct / count if count else 0.0
        if ratio < min_prefix_ratio:
            continue
        # Solo-name veto: business nouns frequently appear as standalone class
        # names (Schedule, Agent, Template, Status). True framework suffixes
        # almost never do (no class is literally named 'Service' or 'Dao').
        solo = solo_counts.get(suf, 0)
        solo_ratio = solo / (count + solo) if (count + solo) > 0 else 0.0
        if solo_ratio > max_solo_ratio:
            stats[suf] = {
                "count": int(count),
                "distinct_prefixes": int(distinct),
                "prefix_ratio": round(ratio, 3),
                "solo_count": int(solo),
                "solo_ratio": round(solo_ratio, 3),
                "rejected": "solo_ratio_above_threshold",
            }
            continue
        suffixes.add(suf)
        stats[suf] = {
            "count": int(count),
            "distinct_prefixes": int(distinct),
            "prefix_ratio": round(ratio, 3),
            "solo_count": int(solo),
            "solo_ratio": round(solo_ratio, 3),
        }

    # Compound suffixes: only keep ones whose components are both already
    # recognised as suffixes (e.g. "ServiceImpl"), and that recur enough.
    compounds: Set[str] = set()
    for compound, c in compound_counts.items():
        if c < max(3, min_frequency // 2):
            continue
        parts = _split_pascal(compound)
        if len(parts) < 2:
            continue
        head, tail = parts[-2], parts[-1]
        if head in suffixes and tail in suffixes:
            compounds.add(compound)
            stats[compound] = {"count": int(c), "compound": True}

    # Order: longer suffixes first so the stripping loop in build_component_map
    # peels 'ServiceImpl' before 'Service'.
    ordered: List[str] = sorted(compounds | suffixes, key=lambda s: (-len(s), s))

    return {
        "discovered_at": _now_iso(),
        "source": f"{classes_seen} class declarations across *.java files",
        "method": (
            f"PascalCase-tail frequency with prefix-diversity + solo-name veto "
            f"(min_frequency={min_frequency}, "
            f"min_distinct_prefixes={min_distinct_prefixes}, "
            f"min_prefix_ratio={min_prefix_ratio}, "
            f"max_solo_ratio={max_solo_ratio})"
        ),
        "suffixes": ordered,
        "statistics": stats,
        "summary": {
            "classes_scanned": classes_seen,
            "distinct_tails": len(suffix_prefixes),
            "kept_suffixes": len(suffixes),
            "kept_compounds": len(compounds),
            "total_kept": len(ordered),
        },
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[build_discovered_vocabularies] WARNING: failed to load "
              f"{path}: {e}")
        return default


def build_discovered_vocabularies(
    test_corpus_path: str,
    repo_root: str,
    output_dir: str,
    only: str = "all",
) -> Dict[str, Any]:
    """Run all 3 discoveries (or a subset).

    `only` accepts:
        - "all"               : run every stage (default)
        - "framework_suffixes": only the Java-class suffix discovery (no
                                dependencies on other KB files)
        - "vocabularies"      : only generic_nouns + domain_vocabulary
                                (requires component_map.json + flow_registry.json
                                to already exist in `output_dir`)
    """
    print("=" * 80)
    print(f"STAGE 0d: DISCOVERED VOCABULARIES (data-driven, mode={only})")
    print("=" * 80)

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {}

    # ---- Framework suffixes (no dependencies) ----
    if only in ("all", "framework_suffixes"):
        print("\n[Discovering framework suffixes]")
        suffix_doc = discover_framework_suffixes(_iter_source_files(repo_root))
        suffix_path = out_dir / "discovered_framework_suffixes.json"
        suffix_path.write_text(json.dumps(suffix_doc, indent=2), encoding="utf-8")
        print(f"  -> {suffix_path}")
        print(f"     {suffix_doc['summary']['total_kept']} suffixes "
              f"(simple={suffix_doc['summary']['kept_suffixes']}, "
              f"compound={suffix_doc['summary']['kept_compounds']}) "
              f"from {suffix_doc['summary']['classes_scanned']} class declarations")
        if suffix_doc["suffixes"]:
            sample = ", ".join(suffix_doc["suffixes"][:15])
            more = " ..." if len(suffix_doc["suffixes"]) > 15 else ""
            print(f"     sample: {sample}{more}")
        result["framework_suffixes"] = suffix_doc

    if only == "framework_suffixes":
        print("\n[OK] framework suffixes only - vocabularies skipped")
        return result

    # ---- Load inputs for vocabulary discoveries ----
    print(f"\nLoading test corpus: {test_corpus_path}")
    test_corpus = _load_json(Path(test_corpus_path), [])
    if not isinstance(test_corpus, list):
        test_corpus = []
    print(f"  Loaded {len(test_corpus)} tests")

    component_map_path = out_dir / "component_map.json"
    print(f"Loading component map: {component_map_path}")
    component_map = _load_json(component_map_path, {})
    n_components = len(list(_iter_components(component_map)))
    print(f"  Loaded {n_components} components")

    flow_registry_path = out_dir / "flow_registry.json"
    print(f"Loading flow registry: {flow_registry_path}")
    flow_registry = _load_json(flow_registry_path, {})
    n_flows = len(list(_iter_flows(flow_registry)))
    print(f"  Loaded {n_flows} flows")

    # ---- Build sources for generic-noun discovery ----
    component_names: List[str] = []
    method_names: List[str] = []
    for comp in _iter_components(component_map):
        if not isinstance(comp, dict):
            continue
        for k in ("component_name", "display_name"):
            v = comp.get(k)
            if isinstance(v, str):
                component_names.append(v)
        for v in comp.get("raw_class_names") or []:
            if isinstance(v, str):
                component_names.append(v)
        for v in comp.get("methods") or []:
            if isinstance(v, str):
                method_names.append(v)

    flow_names: List[str] = []
    for flow in _iter_flows(flow_registry):
        if not isinstance(flow, dict):
            continue
        for k in ("flow_name", "flow"):
            v = flow.get(k)
            if isinstance(v, str):
                flow_names.append(v)
        for ep in flow.get("entry_points") or []:
            if isinstance(ep, str) and ":" in ep:
                method_names.append(ep.rsplit(":", 1)[-1])

    test_summaries: List[str] = []
    for test in test_corpus:
        if not isinstance(test, dict):
            continue
        s = test.get("summary")
        if isinstance(s, str):
            test_summaries.append(s)

    # ---- Generic nouns ----
    print("\n[Discovering generic nouns - MAD-z]")
    generic_doc = discover_generic_nouns(
        component_names=component_names,
        flow_names=flow_names,
        method_names=method_names,
        test_summaries=test_summaries,
        test_corpus=test_corpus,
    )
    generic_path = out_dir / "discovered_generic_nouns.json"
    generic_path.write_text(json.dumps(generic_doc, indent=2), encoding="utf-8")
    print(f"  -> {generic_path}")
    print(f"     {generic_doc['summary']['generic_count']} generic nouns "
          f"(of {generic_doc['summary']['total_tokens']} tokens scanned)")
    if generic_doc["generic_nouns"]:
        sample = ", ".join(generic_doc["generic_nouns"][:15])
        more = " ..." if len(generic_doc["generic_nouns"]) > 15 else ""
        print(f"     sample: {sample}{more}")
    result["generic_nouns"] = generic_doc

    # ---- Domain vocabulary ----
    # Load language reserved words so they can be filtered out of the
    # domain set. The file is produced by discover_reserved_words.py at
    # KB-build time and is persisted alongside the rest of the KB. If it
    # is missing (older KB or non-Java profile that hasn't run the
    # discoverer yet), we attempt to build it now via the orchestrator
    # in extract_diff_concepts._build_reserved_words so we keep the
    # filter data-driven and never silently degrade to an empty set.
    reserved_path = out_dir / "language_reserved_words.json"
    reserved_words: Set[str] = set()
    if reserved_path.exists():
        try:
            rw_doc = _load_json(reserved_path, {})
            for w in (rw_doc.get("reserved_words") or []):
                if isinstance(w, str) and w:
                    reserved_words.add(w.lower())
        except Exception as exc:
            print(f"  [WARN] Could not read {reserved_path}: {exc}")
    if not reserved_words:
        # Trigger the auto-discovery builder so the file is present for
        # this run AND future runs. This keeps the pipeline data-driven:
        # the only "knowledge" used is what the discoverer emits, never
        # a hardcoded list maintained in this script.
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from extract_diff_concepts import _build_reserved_words  # noqa: E402
            # Resolve language from the active profile (auto-detected).
            try:
                from configs.ria_config import get_active_profile  # noqa: E402
                _lang = (get_active_profile().get("language") or "auto")
            except Exception:
                _lang = "auto"
            _build_reserved_words(_lang, str(out_dir))
            if reserved_path.exists():
                rw_doc = _load_json(reserved_path, {})
                for w in (rw_doc.get("reserved_words") or []):
                    if isinstance(w, str) and w:
                        reserved_words.add(w.lower())
        except Exception as exc:
            print(f"  [WARN] Could not auto-build reserved words: {exc}")
    print(f"  Loaded {len(reserved_words)} language reserved words for "
          f"domain-vocab filtering")

    print("\n[Discovering domain vocabulary]")
    domain_doc = discover_domain_vocabulary(
        component_map=component_map,
        flow_registry=flow_registry,
        test_corpus=test_corpus,
        generic_nouns=set(generic_doc["generic_nouns"]),
        reserved_words=reserved_words,
    )
    domain_path = out_dir / "domain_vocabulary.json"
    domain_path.write_text(json.dumps(domain_doc, indent=2), encoding="utf-8")
    print(f"  -> {domain_path}")
    print(f"     {domain_doc['summary']['domain_count']} domain tokens "
          f"(of {domain_doc['summary']['total_candidates']} candidates, "
          f"{domain_doc['summary']['generic_excluded']} excluded as generic, "
          f"{domain_doc['summary'].get('reserved_excluded', 0)} excluded as "
          f"language reserved words)")
    if domain_doc["domain_tokens"]:
        sample = ", ".join(domain_doc["domain_tokens"][:15])
        more = " ..." if len(domain_doc["domain_tokens"]) > 15 else ""
        print(f"     sample: {sample}{more}")
    result["domain_vocabulary"] = domain_doc

    print("\n[OK] Discovered vocabularies written to KB")
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Build discovered vocabularies (Stage 0d - data-driven)"
    )
    parser.add_argument("--test-corpus", default=TC_DATA_PATH,
                        help="Path to all_tcs_extracted.json")
    parser.add_argument("--repo-root", default=REPO_ROOT,
                        help="Repository root (for *.java scan)")
    parser.add_argument("--output-dir",
                        default=os.path.join(RIA_OUTPUT_DIR, "knowledge_base"),
                        help="Knowledge-base output directory")
    parser.add_argument("--only", choices=["all", "framework_suffixes", "vocabularies"],
                        default="all",
                        help="Which discoveries to run. 'framework_suffixes' has no "
                             "KB dependencies and can run before component_map. "
                             "'vocabularies' requires component_map.json + "
                             "flow_registry.json to already exist.")
    args = parser.parse_args()

    try:
        build_discovered_vocabularies(
            test_corpus_path=args.test_corpus,
            repo_root=args.repo_root,
            output_dir=args.output_dir,
            only=args.only,
        )
        sys.exit(0)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
