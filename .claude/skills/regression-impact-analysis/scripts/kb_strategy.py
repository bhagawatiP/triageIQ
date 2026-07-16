"""
KB strategy determination based on explicit CLI flags.

Semantic intent detection from user prompts is handled by Claude
via SKILL.md guidance. This module only processes explicit flags
(--rebuild-kb, --skip-kb-check) and provides disk-state validation.

Strategies
----------
- 'skip'     : Skip all KB validation (--skip-kb-check)
- 'minimal' : Check files exist, do NOT check staleness (default)
- 'standard': Full validation - existence + staleness warning
- 'rebuild' : Force a full KB rebuild (--rebuild-kb)
"""

from __future__ import annotations

from typing import Dict, Optional

# 'skip' | 'minimal' | 'standard' | 'rebuild'
KBStrategy = str


def infer_kb_strategy_from_prompt(
    user_prompt: Optional[str] = None,
    explicit_flags: Optional[Dict[str, bool]] = None,
) -> KBStrategy:
    """
    Infer KB validation strategy from explicit CLI flags.

    Semantic intent detection is now handled by Claude via SKILL.md.
    This function only maps explicit flags to strategies.

    Args:
        user_prompt:    Retained for backward-compatible signature only.
                        Not used for strategy detection any more.
        explicit_flags: CLI flags. Recognised keys:
                            'rebuild_kb'     -> force 'rebuild'
                            'skip_kb_check'  -> force 'skip'

    Returns:
        One of: 'skip', 'minimal', 'rebuild'.
    """
    flags = explicit_flags or {}

    if flags.get('rebuild_kb'):
        return 'rebuild'
    if flags.get('skip_kb_check'):
        return 'skip'

    # Default: minimal validation (check existence, reuse if present)
    return 'minimal'


def get_validation_behavior(strategy: KBStrategy) -> Dict[str, object]:
    """
    Translate a strategy name into concrete validation behaviour.

    Returns a dict with:
        check_exists      (bool)        - call check_kb_exists()?
        check_freshness   (bool)        - call check_kb_freshness()?
        abort_on_missing  (bool)        - exit 1 if files missing?
        warn_on_stale     (bool)        - print warning if KB old?
        max_age_hours     (int | None)  - threshold for staleness warnings.
    """
    behaviors = {
        'skip': {
            'check_exists': False,
            'check_freshness': False,
            'abort_on_missing': False,
            'warn_on_stale': False,
            'max_age_hours': None,
        },
        'minimal': {
            'check_exists': True,
            'check_freshness': False,    # do NOT enforce age
            'abort_on_missing': True,    # but files must still exist
            'warn_on_stale': False,
            'max_age_hours': None,
        },
        'standard': {
            'check_exists': True,
            'check_freshness': True,
            'abort_on_missing': True,
            'warn_on_stale': True,       # warn but never abort
            'max_age_hours': 168,        # 7 days - more sensible than 24h
        },
        'rebuild': {
            'check_exists': False,       # going to rebuild anyway
            'check_freshness': False,
            'abort_on_missing': False,
            'warn_on_stale': False,
            'max_age_hours': None,
        },
    }
    if strategy not in behaviors:
        # Defensive fallback - never raise just because a caller mistypes.
        strategy = 'minimal'
    return behaviors[strategy]


def explain_strategy(strategy: KBStrategy) -> str:
    """Return a single-line, user-friendly description of the strategy."""
    explanations = {
        'skip': "Skipping KB validation (speed mode)",
        'minimal': "Checking KB files exist (focused KB will be rebuilt)",
        'standard': "Validating KB completeness and freshness",
        'rebuild': "Rebuilding KB from scratch",
    }
    return explanations.get(strategy, explanations['minimal'])


# ---------------------------------------------------------------------------
# Tiny self-test (run `python3 kb_strategy.py` to sanity-check the matrix).
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    # Test explicit flags
    assert infer_kb_strategy_from_prompt('', {'rebuild_kb': True}) == 'rebuild'
    assert infer_kb_strategy_from_prompt('', {'skip_kb_check': True}) == 'skip'
    assert infer_kb_strategy_from_prompt('', {}) == 'minimal'
    assert infer_kb_strategy_from_prompt(None, None) == 'minimal'
    print("[OK] All flag-based tests passed")
