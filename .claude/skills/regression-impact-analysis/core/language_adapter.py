"""
LanguageAdapter — base class describing the minimal contract a language
adapter must satisfy.

A LanguageAdapter is purely a *thin shim* that pairs:
    - the YAML config dict for a language
    - the file-extension-based identification rule

NO language-specific behaviour goes here. All filtering decisions are made
in `config_adapter.ConfigAdapter` against the YAML rules. New languages are
added by dropping a YAML file into `configs/languages/` and (optionally)
calling `LanguageAdapter.from_config(...)`.
"""

from __future__ import annotations

from typing import Iterable, Mapping


class LanguageAdapter:
    """Simple holder. NOT a place to hardcode language behaviour."""

    def __init__(self, name: str, extensions: Iterable[str], config: Mapping):
        self.name = name
        # Normalise extensions to tuple so list-comparisons stay cheap.
        self.extensions = tuple(extensions)
        self.config = dict(config)

    # ------------------------------------------------------------------
    # Identification
    # ------------------------------------------------------------------
    def matches_path(self, file_path: str) -> bool:
        """True if `file_path` belongs to this language (by extension)."""
        if not file_path:
            return False
        lower = file_path.lower()
        return any(lower.endswith(ext.lower()) for ext in self.extensions)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, config: Mapping) -> "LanguageAdapter":
        """Build a LanguageAdapter from a parsed YAML language config."""
        lang = config.get("language", {}) or {}
        name = lang.get("name") or "unknown"
        exts = lang.get("extensions") or []
        if not isinstance(exts, list):
            raise ValueError(
                f"language.extensions must be a list (got {type(exts).__name__})"
            )
        return cls(name=name, extensions=exts, config=config)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------
    def __repr__(self) -> str:
        return f"<LanguageAdapter name={self.name!r} exts={self.extensions}>"
