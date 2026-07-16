"""
EntryPointDetector — universal, data-driven entry-point detection.

ZERO hardcoded patterns. ALL language-specific rules come from
`configs/languages/<lang>.yaml`. The algorithm is identical for every
language:

    FOR each method m in file f:
      IF call_graph.has_internal_callers(m, f): NOT_ENTRY_POINT
      apply universal filters from YAML:
          private / abstract / constructor / trivial / test_paths / vendor /
          plus any language_specific_filters.
      IF passed all filters: ENTRY_POINT

The detector exposes a CLI self-test runnable as:

    python -m core.detector --self-test <repo_root>

(or directly: `python detector.py --self-test <repo_root>`). The self-test
walks the repo, classifies each file's methods, and prints a per-language
summary plus the first few entry points it finds.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

# Allow running as a script: add the parent dir to sys.path so `core`
# is importable.
_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from core.call_graph import CallGraph, InMemoryCallGraph  # noqa: E402
from core.config_adapter import ConfigAdapter  # noqa: E402
from core.language_adapter import LanguageAdapter  # noqa: E402


# ---------------------------------------------------------------------------
# DEFAULT_CONFIG_DIR — points to the shipped YAMLs. Resolved relative to
# this file so the detector works regardless of cwd.
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, "configs")
)


class EntryPointDetector:
    """
    Universal entry-point detector. Construct once with the config root,
    then call `detect_entry_points_in_file(...)` per file or
    `detect_entry_points_in_repo(...)` for a full sweep.

    All decisions are config-driven; the only Python-side logic is the
    universal "no internal callers AND no filter triggers" pipeline.
    """

    def __init__(
        self,
        config_dir: Optional[str] = None,
        adapters: Optional[Mapping[str, ConfigAdapter]] = None,
    ):
        if adapters is not None:
            self._adapters = dict(adapters)
        else:
            cfg_dir = config_dir or DEFAULT_CONFIG_DIR
            self._adapters = ConfigAdapter.load_languages(cfg_dir)
        if not self._adapters:
            raise RuntimeError(
                "No language adapters loaded. Check configs/languages/."
            )
        # Build (extension -> language_name) map for fast detection.
        self._ext_index: Dict[str, str] = {}
        for lang, adapter in self._adapters.items():
            for ext in adapter.extensions:
                self._ext_index[ext.lower()] = lang

    # ------------------------------------------------------------------
    # Language detection
    # ------------------------------------------------------------------
    def detect_language(self, file_path: str) -> Optional[str]:
        if not file_path:
            return None
        lower = file_path.lower()
        # Match longest-first so .d.ts could in principle override .ts; the
        # current YAML keeps `.d.ts` as a filter (not a language extension).
        best: Optional[Tuple[str, str]] = None
        for ext, lang in self._ext_index.items():
            if lower.endswith(ext):
                if best is None or len(ext) > len(best[0]):
                    best = (ext, lang)
        return best[1] if best else None

    def get_adapter(self, language: str) -> Optional[ConfigAdapter]:
        return self._adapters.get(language)

    # ------------------------------------------------------------------
    # Single-method classification (fast path)
    # ------------------------------------------------------------------
    def is_entry_point(
        self,
        method_name: str,
        file_path: str,
        file_content: str,
        call_graph: CallGraph,
        method_offset: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Classify one method. Returns (is_entry, reason). On rejection the
        reason is the YAML filter key OR `internal-callers` OR `no-method-decl`.
        """
        language = self.detect_language(file_path)
        if not language:
            return False, "unknown-language"
        adapter = self._adapters[language]

        # Stage A — universal: any internal callers => not an entry point.
        if call_graph.has_internal_callers(method_name, file_path):
            return False, "internal-callers"

        # Stage B — config-driven filters (path filters first, since they
        # don't need the method declaration).
        filtered, reason = adapter.is_filtered(
            method_name=method_name,
            file_path=file_path,
            file_content=file_content,
            method_offset=method_offset,
        )
        if filtered:
            return False, f"filter:{reason}"

        # Stage C — sanity: there must be an actual method declaration in
        # the file. Without one, we can't trust the candidate.
        try:
            decl_re = adapter.method_declaration_regex(method_name)
        except Exception:
            return False, "no-method-decl-regex"
        if not decl_re.search(file_content or ""):
            return False, "no-method-decl"

        return True, "entry-point"

    # ------------------------------------------------------------------
    # File-level scan
    # ------------------------------------------------------------------
    def detect_entry_points_in_file(
        self,
        file_path: str,
        file_content: str,
        methods: Iterable[Tuple[str, int]],
        call_graph: CallGraph,
    ) -> List[Dict]:
        """
        For each (method_name, offset) in `methods`, return the list of
        accepted entry-point dicts: {"file", "method", "offset", "language"}.
        """
        language = self.detect_language(file_path)
        if not language:
            return []
        out = []
        for name, offset in methods:
            ok, reason = self.is_entry_point(
                method_name=name,
                file_path=file_path,
                file_content=file_content,
                call_graph=call_graph,
                method_offset=offset,
            )
            if ok:
                out.append({
                    "file": file_path,
                    "method": name,
                    "offset": offset,
                    "language": language,
                })
        return out

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _derive_walker_skip_dirs(self) -> set:
        """
        Return the set of directory *names* worth pruning up-front in
        `os.walk`. Computed by inspecting every loaded language config's
        `vendor_paths` and `test_paths` filters and extracting bare
        directory tokens (substring patterns of the form `dir/`,
        `/dir/`, or `dir`). Glob patterns (`*Test.java`, etc.) are
        ignored since they apply to file names not directories.

        Example: a YAML pattern `/node_modules/` contributes `node_modules`
        to the walker skip set; `*Test.java` contributes nothing.
        """
        skip: set = set()
        for adapter in self._adapters.values():
            cfg = getattr(adapter, "_config", {}) or {}
            for block in (cfg.get("filters", {}) or {},
                          cfg.get("language_specific_filters", {}) or {}):
                for _name, fspec in block.items():
                    if not isinstance(fspec, dict):
                        continue
                    if fspec.get("type") != "path_pattern":
                        continue
                    for pat in fspec.get("patterns", []) or []:
                        if not isinstance(pat, str) or not pat:
                            continue
                        if "*" in pat or "?" in pat:
                            continue
                        # Strip leading/trailing slashes; if a single token
                        # remains, treat as a directory name.
                        token = pat.strip("/")
                        if token and "/" not in token:
                            skip.add(token)
        return skip

    @staticmethod
    def _instantiate_finder_template(template: str) -> str:
        """
        Return a regex string with `{METHOD}` substituted by an identifier
        match. The first occurrence is the named group `mname`; every
        subsequent occurrence is an un-named identifier match that still
        binds at the same textual class. This keeps templates that mention
        `{METHOD}` multiple times (e.g. JS) compilable without duplicating
        group names.
        """
        ident = r"[A-Za-z_][A-Za-z0-9_]*"
        named = r"(?P<mname>" + ident + ")"
        # Replace first {METHOD} with named, the rest with bare identifier.
        if "{METHOD}" not in template:
            return template
        first_idx = template.index("{METHOD}")
        head = template[:first_idx] + named
        tail = template[first_idx + len("{METHOD}"):].replace("{METHOD}", ident)
        return head + tail

    # ------------------------------------------------------------------
    # Repo-wide sweep — walk filesystem, parse methods via regex.
    # ------------------------------------------------------------------
    def detect_entry_points_in_repo(
        self,
        repo_root: str,
        call_graph: Optional[CallGraph] = None,
        languages: Optional[Iterable[str]] = None,
        max_files: Optional[int] = None,
        progress: bool = False,
    ) -> List[Dict]:
        """
        Walk `repo_root`, classify every method, return entry points.

        If `call_graph` is None, an InMemoryCallGraph (no callers known)
        is used — meaning EVERY method passes Stage A and only the YAML
        filters reduce the set. This is the mode used for the EEM
        candidate-count validation.
        """
        if call_graph is None:
            call_graph = InMemoryCallGraph()
        languages = set(languages) if languages else None

        # Build (lang -> adapter) restricted set.
        active = {
            lang: ad for lang, ad in self._adapters.items()
            if languages is None or lang in languages
        }
        if not active:
            return []

        # Pre-compile per-language method-finders (loose regex without name
        # binding). We use the YAML's method_declaration regex by replacing
        # `{METHOD}` with a generic identifier capture group.
        #
        # NOTE: Some YAML templates contain `{METHOD}` more than once
        # (e.g. JS where the same name appears in multiple alternatives).
        # We make the FIRST occurrence a named group and every subsequent
        # one a generic identifier match — Python's `re` rejects duplicate
        # group names but accepts un-named alternates that share the same
        # textual class.
        method_finders: Dict[str, re.Pattern] = {}
        for lang, ad in active.items():
            template = ad._method_decl_template  # type: ignore[attr-defined]
            if not template:
                continue
            replaced = self._instantiate_finder_template(template)
            try:
                method_finders[lang] = re.compile(replaced, re.MULTILINE)
            except re.error:
                continue

        results: List[Dict] = []
        scanned = 0
        # `os.walk` traversal-pruning set. Derived UNION of every loaded
        # language config's `vendor_paths` and `test_paths` — so as soon as
        # an author edits a YAML the walker honours it. Includes `.git` and
        # `__pycache__` as obvious always-skips that no YAML would ever opt
        # out of (and excluding them isn't language-specific). NO patterns
        # are hardcoded here beyond those.
        skip_dirs = self._derive_walker_skip_dirs() | {".git", "__pycache__"}
        for dirpath, dirnames, filenames in os.walk(repo_root):
            # Prune obviously-irrelevant trees up-front (the YAML vendor
            # filters will catch anything we miss).
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, repo_root)
                lang = self.detect_language(rel)
                if not lang or lang not in active:
                    continue
                if max_files is not None and scanned >= max_files:
                    break
                scanned += 1
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                except Exception:
                    continue

                finder = method_finders.get(lang)
                if not finder:
                    continue
                seen_offsets = set()
                methods: List[Tuple[str, int]] = []
                for m in finder.finditer(content):
                    name = m.group("mname")
                    if not name:
                        continue
                    off = m.start()
                    key = (name, off)
                    if key in seen_offsets:
                        continue
                    seen_offsets.add(key)
                    methods.append((name, off))

                if not methods:
                    continue

                eps = self.detect_entry_points_in_file(
                    file_path=rel,
                    file_content=content,
                    methods=methods,
                    call_graph=call_graph,
                )
                results.extend(eps)

                if progress and scanned % 200 == 0:
                    print(f"  ... {scanned} files scanned, {len(results)} candidates")
            if max_files is not None and scanned >= max_files:
                break

        return results


# ---------------------------------------------------------------------------
# CLI / self-test
# ---------------------------------------------------------------------------
def _self_test(repo_root: str, language: Optional[str], output: Optional[str]) -> int:
    detector = EntryPointDetector()
    print(f"[detector] loaded adapters: {sorted(detector._adapters)}")  # noqa: SLF001

    languages = [language] if language else None
    t0 = time.time()
    eps = detector.detect_entry_points_in_repo(
        repo_root=repo_root,
        languages=languages,
        progress=True,
    )
    elapsed = time.time() - t0

    by_lang: Dict[str, int] = {}
    for ep in eps:
        by_lang[ep["language"]] = by_lang.get(ep["language"], 0) + 1
    print(f"[detector] candidates: {len(eps)} (elapsed {elapsed:.1f}s)")
    for lang, cnt in sorted(by_lang.items()):
        print(f"  {lang}: {cnt}")

    if output:
        with open(output, "w", encoding="utf-8") as fh:
            json.dump(eps, fh, indent=2)
        print(f"[detector] wrote {output}")

    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Universal entry-point detector")
    parser.add_argument("--self-test", metavar="REPO_ROOT",
                        help="walk REPO_ROOT and report candidate counts")
    parser.add_argument("--language", default=None,
                        help="restrict to a single language (java/python/...)")
    parser.add_argument("--out", default=None, help="write JSON report")
    args = parser.parse_args(argv)
    if args.self_test:
        return _self_test(args.self_test, args.language, args.out)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
