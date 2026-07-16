"""Universal entry-point detection package.

Sub-modules:
    detector         — orchestrates per-method classification (NO hardcoding).
    config_adapter   — loads YAML configs and dynamically applies filters.
    call_graph       — minimal call-graph adapter interface.
    language_adapter — base class describing a language adapter.
"""

from .detector import EntryPointDetector  # noqa: F401
from .config_adapter import ConfigAdapter, FilterConfigError  # noqa: F401
from .language_adapter import LanguageAdapter  # noqa: F401
from .call_graph import CallGraph, InMemoryCallGraph  # noqa: F401
