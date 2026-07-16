"""
CallGraph — minimal interface used by the universal entry-point detector.

The detector ONLY needs to ask one question of a call graph:

    has_internal_callers(method, file) -> bool

i.e. "does any other method in this codebase call `method`?".

This file ships:

    - `CallGraph` — the abstract contract.
    - `InMemoryCallGraph` — a trivial dict-backed implementation used by
      tests / one-shot scans.
    - `SerenaCallGraphAdapter` — bridge to the existing SerenaMCPClient
      `find_referencing_symbols(...)` API used in the RIA pipeline. This
      adapter introduces NO hardcoded behaviour; it just translates calls.

NO tree-sitter integration is hardwired here — callers can plug whatever
backend builds the graph as long as it returns the boolean. A tree-sitter
adapter can be added separately without touching the detector.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional, Tuple


class CallGraph:
    """Abstract call-graph adapter."""

    def has_internal_callers(self, method: str, file_path: str) -> bool:
        """Return True if any *external* (non-self-file) caller exists."""
        raise NotImplementedError

    def get_methods(self, file_path: str) -> Iterable[Tuple[str, int]]:
        """Yield (method_name, char_offset) tuples in declaration order."""
        raise NotImplementedError


class InMemoryCallGraph(CallGraph):
    """Trivial dict-backed adapter used by self-tests."""

    def __init__(
        self,
        callers: Optional[Mapping[Tuple[str, str], int]] = None,
        methods_by_file: Optional[Mapping[str, Iterable[Tuple[str, int]]]] = None,
    ):
        # callers maps (file, method) -> external caller count
        self._callers = dict(callers or {})
        self._methods_by_file = {
            k: list(v) for k, v in (methods_by_file or {}).items()
        }

    def has_internal_callers(self, method: str, file_path: str) -> bool:
        return self._callers.get((file_path, method), 0) > 0

    def get_methods(self, file_path):
        return list(self._methods_by_file.get(file_path, []))

    # Test helpers -----------------------------------------------------
    def add_method(self, file_path: str, method: str, offset: int = 0) -> None:
        self._methods_by_file.setdefault(file_path, []).append((method, offset))

    def add_caller(self, file_path: str, method: str, count: int = 1) -> None:
        key = (file_path, method)
        self._callers[key] = self._callers.get(key, 0) + count


class SerenaCallGraphAdapter(CallGraph):
    """
    Thin bridge to SerenaMCPClient.

    The detector uses `has_internal_callers` only. This adapter calls
    `serena.find_referencing_symbols(method_name, from_file=file_path)`
    and counts references whose `file` differs from `file_path`. That
    matches the existing RIA semantics ("external caller" = not in the
    declaring file).
    """

    def __init__(self, serena, repo_root: str = ""):
        self._serena = serena
        self._repo_root = repo_root

    def has_internal_callers(self, method: str, file_path: str) -> bool:
        try:
            refs_result = self._serena.find_referencing_symbols(
                method, from_file=file_path
            )
        except Exception:
            # If we can't determine references, be conservative: assume there
            # are callers (i.e. NOT an entry point) so we don't flood the
            # downstream pipeline with false positives.
            return True
        refs = refs_result.get("references", []) or []
        for ref in refs:
            if ref.get("file") != file_path:
                return True
        return False

    def get_methods(self, file_path: str):
        # Mirror SerenaMCPClient.get_symbols_overview shape used in
        # build_flow_registry.py. Yields (name, 0) — offset is computed
        # downstream via regex.
        try:
            sym = self._serena.get_symbols_overview(file_path)
        except Exception:
            return []
        out = []
        for s in sym.get("symbols", []) or []:
            if s.get("kind") in ("method", "function"):
                out.append((s.get("name", ""), s.get("offset", 0) or 0))
        return out
