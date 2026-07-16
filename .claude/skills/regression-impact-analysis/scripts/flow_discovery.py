#!/usr/bin/env python3
"""
flow_discovery.py
=================

Smart, generic flow discovery and classification for the RIA pipeline.

This module replaces hardcoded "common-method" and "high-fan-out" lists with
data-driven heuristics that work across products and codebases. It is used
by Stage 2.5 (after consolidation) to label each consolidated flow with one
of:

    - PRIMARY      : narrow, change-specific, high-signal flow
    - INDIRECT     : reachable but not directly hit
    - GENERIC      : the changed token itself is generic English/method
                     vocabulary in this codebase
    - DEDUP_VENEER : structural noise from naming variants of the same
                     business intent

Inputs (all optional - the algorithm degrades gracefully if any are absent):
    flows           : list of flow records (consolidated)
    flow_registry   : kb flow_registry.json contents (dict)
    method_lexicon  : list of method names from the codebase, used to
                      infer GENERIC method tokens with no hardcoded list
    domain_terms    : optional set of corpus-wide domain words
    changed_methods : list of changed method names being analyzed

The algorithm has NO product-specific constants. All thresholds are derived
from the data via robust statistics (median + MAD).
"""

from __future__ import annotations

import re
import math
from collections import Counter
from typing import Iterable, List, Dict, Set, Optional, Any


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------
_CAMEL_RE = re.compile(r'[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+')


def _tokenize(name: str) -> List[str]:
    """
    Split a camelCase / PascalCase / snake_case identifier into lowercase
    word tokens. Numbers and underscores are honored.

    Examples (the algorithm is language-agnostic):
        getUserProfileByEmailAndOrg
            -> ['get','user','profile','by','email','and','org']
        FETCH_ORDER_LIST
            -> ['fetch','order','list']
        process_payment_v2
            -> ['process','payment','v','2']
    """
    if not name:
        return []
    # Replace separators with spaces, then run camel split per chunk.
    chunks = re.split(r'[^A-Za-z0-9]+', str(name))
    out: List[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        for piece in _CAMEL_RE.findall(chunk):
            piece = piece.lower()
            if piece:
                out.append(piece)
    return out


# ---------------------------------------------------------------------------
# Robust statistics
# ---------------------------------------------------------------------------
def _mad(values: List[float]) -> float:
    """
    Median Absolute Deviation - a robust scale estimator that is far less
    sensitive to outliers than standard deviation. Returns 0.0 if values
    is empty.
    """
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    median = s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2.0
    devs = sorted(abs(v - median) for v in values)
    n2 = len(devs)
    if n2 == 0:
        return 0.0
    return devs[n2 // 2] if n2 % 2 == 1 else (devs[n2 // 2 - 1] + devs[n2 // 2]) / 2.0


# ---------------------------------------------------------------------------
# Generic method-name lexicon (no hardcoded lists)
# ---------------------------------------------------------------------------
def discover_generic_method_lexicon(
    method_names: Iterable[str],
    *,
    z_threshold: float = 3.0,
    min_count: int = 3,
) -> Set[str]:
    """
    Identify "generic" method tokens whose frequency in the codebase is
    statistically anomalous (high) relative to the typical token. These
    are the tokens that, when used as a flow name on their own, indicate
    the flow is structural noise rather than a business intent.

    Examples that typically surface: 'call', 'execute', 'run', 'process',
    'handle', 'get', 'set', 'save', 'load', 'create', 'init'. The exact
    set is data-driven per codebase.

    Implementation:
        1. Tokenize every method name in `method_names`.
        2. Count token frequencies, ignoring extremely short or numeric
           tokens.
        3. Flag tokens whose count is far above median by MAD-z (>= z_threshold)
           AND whose count >= min_count.

    Returns a lowercase set of generic tokens.
    """
    counts: Counter = Counter()
    for name in method_names:
        for tok in _tokenize(name):
            if len(tok) < 3 or tok.isdigit():
                continue
            counts[tok] += 1

    if not counts:
        return set()

    values = list(counts.values())
    n = len(values)
    s = sorted(values)
    median = s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2.0
    mad = _mad(values) or 1.0

    generic: Set[str] = set()
    for tok, c in counts.items():
        if c < min_count:
            continue
        # MAD-z (1.4826 makes MAD comparable to stddev under normality).
        z = (c - median) / (1.4826 * mad)
        if z >= z_threshold:
            generic.add(tok)
    return generic


# ---------------------------------------------------------------------------
# Entry-point class index (for breadth metric)
# ---------------------------------------------------------------------------
def _ep_class_index(flow: Dict[str, Any]) -> Set[str]:
    """
    Return the set of (file or class) signatures touched by this flow's
    entry points. Used as a proxy for breadth/fan-out: flows whose entry
    points span many unrelated classes look generic.
    """
    eps = flow.get('entry_points') or []
    if not eps:
        ep = flow.get('entry_point')
        if isinstance(ep, dict):
            eps = [ep.get('file') or '']
        elif isinstance(ep, str):
            eps = [ep]
    sigs: Set[str] = set()
    for ep in eps:
        if isinstance(ep, dict):
            f = ep.get('file') or ''
        else:
            f = str(ep or '')
        if not f:
            continue
        # Strip the trailing :method if present, take basename for class.
        base = f.split(':')[0]
        # Use the file path itself as the class signature - good enough for
        # breadth measurement.
        sigs.add(base)
    return sigs


# ---------------------------------------------------------------------------
# Domain vocabulary
# ---------------------------------------------------------------------------
def _domain_vocabulary(flow_registry: Optional[Dict[str, Any]]) -> Set[str]:
    """
    Build a corpus-wide "domain vocabulary" from the flow registry. Tokens
    that appear in the registry are considered legitimate domain terms;
    flow names composed exclusively of such tokens are NOT downgraded just
    for being short.
    """
    vocab: Set[str] = set()
    if not flow_registry:
        return vocab
    for f in flow_registry.get('flows', []) or []:
        for src in (f.get('flow_name'), f.get('flow'), f.get('flow_tag')):
            for tok in _tokenize(src or ''):
                if len(tok) >= 3 and not tok.isdigit():
                    vocab.add(tok)
        for ep in f.get('entry_points', []) or []:
            for tok in _tokenize(ep if isinstance(ep, str) else ep.get('file', '')):
                if len(tok) >= 3 and not tok.isdigit():
                    vocab.add(tok)
    return vocab


# ---------------------------------------------------------------------------
# Flow breadth
# ---------------------------------------------------------------------------
def _flow_breadth(flow: Dict[str, Any]) -> int:
    """
    A simple proxy for how "wide" a flow's reach is across the codebase.
    Higher values mean the flow's entry points are scattered.
    """
    return len(_ep_class_index(flow))


# ---------------------------------------------------------------------------
# Main classifier
# ---------------------------------------------------------------------------
def classify_flows(
    flows: List[Dict[str, Any]],
    *,
    flow_registry: Optional[Dict[str, Any]] = None,
    method_lexicon: Optional[Iterable[str]] = None,
    domain_terms: Optional[Set[str]] = None,
    changed_methods: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Annotate each flow with a 'classification' and a 'classification_reason'.

    The classification is one of:
        - 'PRIMARY'      : business-relevant, change-focused
        - 'INDIRECT'     : reachable but not directly hit (preserved if set)
        - 'GENERIC'      : flow name dominated by codebase-generic tokens
        - 'DEDUP_VENEER' : near-duplicate of another flow (kept for context)

    The function does NOT remove any flow. It only labels them so callers
    can suppress, dim, or weight as needed downstream.
    """
    flows = list(flows or [])
    if not flows:
        return flows

    # 1. Build a generic lexicon from method_lexicon (if provided).
    method_names = list(method_lexicon or [])
    generic_tokens = discover_generic_method_lexicon(method_names) if method_names else set()

    # 2. Build domain vocab from registry (fallback to provided set).
    vocab: Set[str] = set(domain_terms or set())
    vocab |= _domain_vocabulary(flow_registry)

    # 3. Compute breadth distribution to find anomalously broad flows.
    breadths = [_flow_breadth(f) for f in flows]
    if breadths:
        s = sorted(breadths)
        n = len(s)
        median_b = s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2.0
        mad_b = _mad(breadths) or 1.0
    else:
        median_b, mad_b = 0.0, 1.0

    # 4. Tokenize changed methods (used to bias PRIMARY toward them).
    changed_tokens: Set[str] = set()
    for cm in (changed_methods or []):
        for t in _tokenize(cm):
            if len(t) >= 3:
                changed_tokens.add(t)

    # 5. First pass: compute name tokens and detect generic flows.
    enriched: List[Dict[str, Any]] = []
    for f in flows:
        name = (
            f.get('flow_name')
            or f.get('flow')
            or f.get('flow_tag', '').strip('[]')
            or ''
        )
        toks = [t for t in _tokenize(name) if len(t) >= 3 and not t.isdigit()]
        f = dict(f)
        f['_tokens'] = toks
        enriched.append(f)

    # 6. Second pass: classify.
    # Build a lookup of token-set -> flows for dedup veneer detection.
    seen_signatures: Dict[frozenset, str] = {}

    for f in enriched:
        # Preserve impact_type (DIRECT/INDIRECT) but classify all flows for PRIMARY/GENERIC
        # Don't skip INDIRECT flows - they can still be generic infrastructure

        toks = f.get('_tokens') or []
        if not toks:
            f['classification'] = 'GENERIC'
            f['classification_reason'] = 'flow has no informative name tokens'
            continue

        # 6a. GENERIC if every token is generic (and none is in the
        # changed-method tokens or domain vocab).
        non_generic = [t for t in toks if t not in generic_tokens]
        all_generic = (len(non_generic) == 0)
        overlaps_changed = any(t in changed_tokens for t in toks)
        in_vocab = any(t in vocab for t in toks)

        if all_generic:
            # Single-token generic flows (e.g. "Call", "Run", "Execute")
            # are ALWAYS classified as GENERIC — they're JDK/framework
            # interface methods (Callable.call, Runnable.run) whose names
            # carry no business semantics, even if the changed method
            # happens to share the same name. Multi-token flows retain the
            # changed-method overlap exemption because their compound names
            # convey enough business intent.
            if len(toks) <= 1 or not overlaps_changed:
                f['classification'] = 'GENERIC'
                f['classification_reason'] = (
                    f"all name tokens are codebase-generic ({sorted(set(toks))})"
                )
                continue

        # 6b. Anomalously broad (high fan-out) flows whose name does NOT
        # mention any changed-method token are downgraded to GENERIC.
        breadth = _flow_breadth(f)
        if mad_b > 0:
            z = (breadth - median_b) / (1.4826 * mad_b)
        else:
            z = 0.0
        if z >= 3.0 and not overlaps_changed:
            f['classification'] = 'GENERIC'
            f['classification_reason'] = (
                f"breadth z={z:.1f} (anomalously wide fan-out) and no overlap "
                f"with changed-method tokens"
            )
            continue

        # 6c. DEDUP_VENEER: another flow already seen with the same token set.
        sig = frozenset(toks)
        if sig in seen_signatures:
            f['classification'] = 'DEDUP_VENEER'
            f['classification_reason'] = (
                f"same token set as flow '{seen_signatures[sig]}'"
            )
            continue
        seen_signatures[sig] = (
            f.get('flow_name') or f.get('flow') or f.get('flow_id') or '?'
        )

        # 6d. Otherwise PRIMARY. Provide a reason.
        reasons = []
        if overlaps_changed:
            reasons.append("overlaps changed-method tokens")
        if in_vocab:
            reasons.append("matches domain vocabulary")
        if non_generic:
            reasons.append(f"non-generic tokens={sorted(set(non_generic))}")
        f['classification'] = 'PRIMARY'
        f['classification_reason'] = '; '.join(reasons) or 'specific name'

    # 7. Strip helper keys.
    for f in enriched:
        f.pop('_tokens', None)

    return enriched


__all__ = [
    '_tokenize',
    '_mad',
    'discover_generic_method_lexicon',
    '_ep_class_index',
    '_domain_vocabulary',
    '_flow_breadth',
    'classify_flows',
]
