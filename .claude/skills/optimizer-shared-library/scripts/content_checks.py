#!/usr/bin/env python3
"""
Shared sanity checks applied to agent-authored free text (criteria,
mergeRationale, suggestedName) before it is persisted to a *-duplicates.toon
file, by all three save-*-duplicates scripts.
"""

import re

_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def find_unfilled_placeholder(text):
    """Return the first unfilled template placeholder (e.g. '{vc_mch_count}')
    found in text, or None. Real explanatory text never legitimately contains
    a bare {identifier} - its presence means the agent generated templated
    text and forgot to substitute the actual value in, which has been
    observed reaching a real report ('Remove the {vc_mch_count} duplicate
    method declarations...')."""
    if not text:
        return None
    m = _PLACEHOLDER_RE.search(text)
    return m.group(0) if m else None


def uniform_stats_signature(sets_out, min_sets=8):
    """Return a description of a suspiciously uniform stepDiff/stepCount
    pattern across a whole submission, or None if the stats look like real
    per-test analysis.

    Real content-based step counting (duplicate-detection-guidelines rule 1)
    produces varied stepCount values across unrelated tests, and produces at
    least some stepDiff of 1 or 2 across a large, functionally diverse
    submission. If every single set - across many unrelated functional
    groups - shows exactly the same stepCount for every test and stepDiff 0,
    that is the signature of an agent skipping real content reading and
    defaulting every set to the same placeholder pair of values instead of
    computing them per test. This exact pattern (36 sets across 4 unrelated
    functional areas, every stepCount=1, every stepDiff=0) has been observed
    reaching a real report. Only fires once there are enough sets
    (min_sets) that the uniformity can't be simple coincidence."""
    if len(sets_out) < min_sets:
        return None
    if any(s.get("stepDiff", 0) != 0 for s in sets_out):
        return None
    all_counts = {t.get("stepCount", 0) for s in sets_out for t in s.get("tests", [])}
    if len(all_counts) == 1:
        (only_value,) = all_counts
        return (f"all {len(sets_out)} sets have stepDiff=0 and every test's stepCount={only_value} - "
                f"this is the signature of matching by name/location instead of reading each test's actual "
                f"content and counting/diffing its real steps (duplicate-detection-guidelines rule 1). "
                f"Re-verify by fetching and reading each test's real body content before resubmitting.")
    return None
