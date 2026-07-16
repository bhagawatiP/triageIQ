"""
ConfigAdapter — reads YAML language configs and applies filters dynamically.

NO PATTERNS, NO KEYWORDS, NO PATHS are hardcoded in this file. Everything
the adapter inspects comes from the parsed YAML config of the active
language.

The adapter exposes:

    load_languages(config_dir) -> {lang_name: ConfigAdapter}
    detect_language(file_path)  -> Optional[str]
    is_filtered(method, file_path, content) -> Tuple[bool, str]
    method_declaration_regex(method_name)   -> compiled regex
    class_declaration_regex                 -> compiled regex (or None)

YAML loading:
    - Prefers `yaml` (PyYAML) when available.
    - Falls back to a minimal embedded loader that handles the SUBSET of
      YAML used by the language configs (mappings, lists, scalars, double-
      and single-quoted strings, '#' comments). It does NOT support YAML
      anchors / merge keys / type tags — none of the configs need them.
"""

from __future__ import annotations

import fnmatch
import os
import re
from typing import Dict, Iterable, List, Mapping, Optional, Tuple


# ---------------------------------------------------------------------------
# YAML loader: PyYAML if installed, otherwise embedded fallback.
# ---------------------------------------------------------------------------
try:  # pragma: no cover — exercised by both branches in CI.
    import yaml as _pyyaml  # type: ignore
except Exception:  # pragma: no cover
    _pyyaml = None


def _load_yaml_text(text: str) -> dict:
    """Parse YAML text into a plain dict. Uses PyYAML when present."""
    if _pyyaml is not None:
        loaded = _pyyaml.safe_load(text)
        return loaded if isinstance(loaded, dict) else {}
    return _MiniYAML(text).parse()


class _MiniYAML:
    """
    Tiny YAML subset parser.

    Supports:
        - block mappings (`key:` then indented mapping/list/scalar).
        - block lists (`- value`, `- {scalar/dict}`).
        - flow mapping `{a: 1, b: 2}` only one-line.
        - scalars: bare, double-quoted, single-quoted.
        - integers (`max: 3`).
        - inline `# comment` and full-line comments.

    Does NOT support: anchors (&), references (*), merge keys (<<:),
    multi-line literals (|, >), type tags (!!str). The shipped configs
    deliberately avoid all of these.
    """

    _INT_RE = re.compile(r"^-?\d+$")

    def __init__(self, text: str):
        # Strip BOM, normalise newlines, drop trailing whitespace per line.
        text = text.lstrip("﻿")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self._lines: List[Tuple[int, str]] = []
        for raw in text.split("\n"):
            stripped = self._strip_inline_comment(raw)
            if not stripped.strip():
                continue
            indent = len(stripped) - len(stripped.lstrip(" "))
            self._lines.append((indent, stripped.rstrip()))
        self._idx = 0

    # ----- public ----------------------------------------------------------
    def parse(self) -> dict:
        if not self._lines:
            return {}
        result = self._parse_mapping(0)
        return result if isinstance(result, dict) else {}

    # ----- helpers ---------------------------------------------------------
    @staticmethod
    def _strip_inline_comment(line: str) -> str:
        """
        Strip ` # comment` style trailing comments, but ONLY when the `#` is
        outside any quoted string. Returns the line minus the comment.
        """
        out = []
        in_single = False
        in_double = False
        i = 0
        n = len(line)
        while i < n:
            ch = line[i]
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                # honour backslash-escapes inside double quotes
                if in_double and i > 0 and line[i - 1] == "\\":
                    pass
                else:
                    in_double = not in_double
            elif ch == "#" and not in_single and not in_double:
                # Only treat as comment if preceded by whitespace OR at start.
                if i == 0 or line[i - 1] in (" ", "\t"):
                    break
            out.append(ch)
            i += 1
        return "".join(out)

    def _peek(self) -> Optional[Tuple[int, str]]:
        if self._idx >= len(self._lines):
            return None
        return self._lines[self._idx]

    def _consume(self) -> Tuple[int, str]:
        line = self._lines[self._idx]
        self._idx += 1
        return line

    def _parse_mapping(self, base_indent: int) -> dict:
        out: Dict = {}
        while True:
            peek = self._peek()
            if peek is None:
                return out
            indent, content = peek
            if indent < base_indent:
                return out
            if indent > base_indent:
                # Should be consumed by the previous key; treat as terminator.
                return out
            # indent == base_indent
            if content.lstrip().startswith("- "):
                return out  # mapping ends; caller (list parser) takes over.
            self._consume()
            stripped = content.strip()
            if ":" not in stripped:
                # Stray scalar at mapping level — skip.
                continue
            key, _, rest = stripped.partition(":")
            key = key.strip()
            rest = rest.strip()
            if rest == "" or rest is None:
                # Block-style nested value follows.
                out[key] = self._parse_block_value(base_indent)
            else:
                # Inline value.
                out[key] = self._parse_inline_value(rest)
        # unreachable

    def _parse_block_value(self, parent_indent: int):
        """Parse the value introduced by a `key:` line with no inline value."""
        peek = self._peek()
        if peek is None:
            return None
        indent, content = peek
        if indent <= parent_indent:
            return None  # empty value
        stripped = content.lstrip()
        if stripped.startswith("- "):
            return self._parse_list(indent)
        return self._parse_mapping(indent)

    def _parse_list(self, base_indent: int) -> List:
        out: List = []
        while True:
            peek = self._peek()
            if peek is None:
                return out
            indent, content = peek
            if indent < base_indent:
                return out
            stripped = content.lstrip()
            if not stripped.startswith("- "):
                return out
            if indent != base_indent:
                return out
            self._consume()
            item = stripped[2:].strip()
            if item == "":
                # Nested mapping/list under the dash.
                nested = self._parse_block_value(base_indent)
                out.append(nested)
            else:
                # Could be an inline mapping `- key: value` (block-mapping
                # member) — detect by ":" at top level outside quotes.
                if self._looks_like_mapping_entry(item):
                    # Re-feed: treat as mapping starting at indent+2.
                    rebuilt_indent = indent + 2
                    self._lines.insert(self._idx, (rebuilt_indent, " " * rebuilt_indent + item))
                    out.append(self._parse_mapping(rebuilt_indent))
                else:
                    out.append(self._parse_inline_value(item))
        # unreachable

    @staticmethod
    def _looks_like_mapping_entry(item: str) -> bool:
        in_single = in_double = False
        for i, ch in enumerate(item):
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == ":" and not in_single and not in_double:
                # space after colon OR end of line means mapping
                if i + 1 == len(item) or item[i + 1] in (" ", ""):
                    return True
        return False

    def _parse_inline_value(self, value: str):
        v = value.strip()
        if not v:
            return None
        # Flow mapping?
        if v.startswith("{") and v.endswith("}"):
            return self._parse_flow_mapping(v[1:-1])
        # Flow list?
        if v.startswith("[") and v.endswith("]"):
            return self._parse_flow_list(v[1:-1])
        # Quoted strings.
        if (v.startswith('"') and v.endswith('"') and len(v) >= 2):
            return self._unescape_double(v[1:-1])
        if (v.startswith("'") and v.endswith("'") and len(v) >= 2):
            return v[1:-1].replace("''", "'")
        # Booleans / null.
        low = v.lower()
        if low in ("true", "yes"):
            return True
        if low in ("false", "no"):
            return False
        if low in ("null", "~", ""):
            return None
        # Integer.
        if self._INT_RE.match(v):
            try:
                return int(v)
            except ValueError:
                pass
        # Bare scalar.
        return v

    @staticmethod
    def _unescape_double(s: str) -> str:
        # Minimal escape support: \\ \" \n \t \\\\ — sufficient for regex
        # patterns where we recommend single-quoted strings anyway.
        out = []
        i = 0
        while i < len(s):
            ch = s[i]
            if ch == "\\" and i + 1 < len(s):
                nxt = s[i + 1]
                mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}
                out.append(mapping.get(nxt, nxt))
                i += 2
            else:
                out.append(ch)
                i += 1
        return "".join(out)

    def _parse_flow_mapping(self, body: str) -> dict:
        out: Dict = {}
        for part in self._split_flow(body):
            if ":" not in part:
                continue
            key, _, val = part.partition(":")
            out[key.strip()] = self._parse_inline_value(val.strip())
        return out

    def _parse_flow_list(self, body: str) -> List:
        return [self._parse_inline_value(p.strip()) for p in self._split_flow(body) if p.strip()]

    @staticmethod
    def _split_flow(body: str) -> List[str]:
        parts: List[str] = []
        depth_brace = depth_bracket = 0
        in_single = in_double = False
        cur: List[str] = []
        for ch in body:
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif not in_single and not in_double:
                if ch == "{":
                    depth_brace += 1
                elif ch == "}":
                    depth_brace -= 1
                elif ch == "[":
                    depth_bracket += 1
                elif ch == "]":
                    depth_bracket -= 1
                elif ch == "," and depth_brace == 0 and depth_bracket == 0:
                    parts.append("".join(cur))
                    cur = []
                    continue
            cur.append(ch)
        if cur:
            parts.append("".join(cur))
        return parts


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class FilterConfigError(ValueError):
    """Raised when a filter spec cannot be applied."""


# ---------------------------------------------------------------------------
# ConfigAdapter
# ---------------------------------------------------------------------------
class ConfigAdapter:
    """
    Wraps a single language YAML config and exposes filter dispatch.

    Public API:
        is_filtered(method, file_path, content) -> (bool, reason)
        method_declaration_regex(method_name)   -> compiled regex
        class_declaration_regex                 -> compiled regex (or None)
        modifier_window                         -> int
        extensions                              -> tuple
    """

    # Filter type -> handler method-name mapping. Adding a new filter type
    # only requires implementing the handler; YAML drives the dispatch.
    _FILTER_DISPATCH = {
        "modifier_keyword": "_filter_modifier_keyword",
        "regex_pattern":    "_filter_regex_pattern",
        "path_pattern":     "_filter_path_pattern",
        "name_equals_class":"_filter_name_equals_class",
        "name_pattern":     "_filter_name_pattern",
        "file_extension":   "_filter_file_extension",
    }

    def __init__(self, config: Mapping):
        self._config = dict(config or {})
        lang_block = self._config.get("language", {}) or {}
        self.language_name = lang_block.get("name") or "unknown"
        self.extensions = tuple(lang_block.get("extensions", []) or [])

        md_block = self._config.get("method_declaration", {}) or {}
        self._method_decl_template = md_block.get("regex") or ""
        self.modifier_window = int(md_block.get("modifier_window", 200) or 200)

        cd_block = self._config.get("class_declaration", {}) or {}
        cd_regex = cd_block.get("regex") or ""
        self._class_decl_regex = re.compile(cd_regex, re.MULTILINE) if cd_regex else None

        # Pre-compile what we can. Filters carry their own regex compilation
        # at apply-time so we don't pay for unused branches.
        self._filters: List[Tuple[str, dict]] = list(
            (self._config.get("filters", {}) or {}).items()
        )
        self._language_specific_filters: List[Tuple[str, dict]] = list(
            (self._config.get("language_specific_filters", {}) or {}).items()
        )

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------
    @classmethod
    def from_yaml_file(cls, yaml_path: str) -> "ConfigAdapter":
        with open(yaml_path, "r", encoding="utf-8") as fh:
            text = fh.read()
        return cls(_load_yaml_text(text))

    @classmethod
    def load_languages(cls, config_dir: str) -> Dict[str, "ConfigAdapter"]:
        """
        Load every `*.yaml` file under `<config_dir>/languages/`.
        Returns a mapping `lang_name -> ConfigAdapter`.
        """
        langs_dir = os.path.join(config_dir, "languages")
        if not os.path.isdir(langs_dir):
            raise FileNotFoundError(f"languages dir not found: {langs_dir}")
        out: Dict[str, ConfigAdapter] = {}
        for fname in sorted(os.listdir(langs_dir)):
            if not (fname.endswith(".yaml") or fname.endswith(".yml")):
                continue
            adapter = cls.from_yaml_file(os.path.join(langs_dir, fname))
            out[adapter.language_name] = adapter
        return out

    # ------------------------------------------------------------------
    # Method declaration regex
    # ------------------------------------------------------------------
    def method_declaration_regex(self, method_name: str) -> re.Pattern:
        """Build the compiled method-declaration regex for `method_name`."""
        if not self._method_decl_template:
            raise FilterConfigError(
                f"language '{self.language_name}' has no method_declaration.regex"
            )
        pattern = self._method_decl_template.replace(
            "{METHOD}", re.escape(method_name)
        )
        return re.compile(pattern, re.MULTILINE)

    @property
    def class_declaration_regex(self) -> Optional[re.Pattern]:
        return self._class_decl_regex

    # ------------------------------------------------------------------
    # Top-level dispatch
    # ------------------------------------------------------------------
    def is_filtered(
        self,
        method_name: str,
        file_path: str,
        file_content: str,
        method_offset: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """
        Returns (filtered, reason).
        `filtered=True` means the method should NOT be reported as an entry
        point. `reason` is the YAML filter key that triggered the rejection.
        """
        ctx = _FilterContext(
            method=method_name,
            file_path=file_path,
            content=file_content,
            method_offset=method_offset,
            adapter=self,
        )

        for fname, fspec in self._filters:
            if self._apply(fspec, ctx):
                return True, fname

        for fname, fspec in self._language_specific_filters:
            if self._apply(fspec, ctx):
                return True, fname

        return False, ""

    # ------------------------------------------------------------------
    # Filter dispatch
    # ------------------------------------------------------------------
    def _apply(self, fspec: Mapping, ctx: "_FilterContext") -> bool:
        ftype = (fspec or {}).get("type")
        if not ftype:
            return False
        handler_name = self._FILTER_DISPATCH.get(ftype)
        if handler_name is None:
            raise FilterConfigError(
                f"unknown filter type '{ftype}' for language "
                f"'{self.language_name}'"
            )
        return bool(getattr(self, handler_name)(fspec, ctx))

    # ------------------------------------------------------------------
    # Filter handlers — each consumes (fspec, ctx) and returns True if the
    # method MATCHES the filter (i.e. should be rejected).
    # ------------------------------------------------------------------

    def _filter_modifier_keyword(self, fspec: Mapping, ctx: "_FilterContext") -> bool:
        keywords = fspec.get("keywords") or []
        if not keywords or not ctx.content:
            return False
        # Locate the method declaration so we can isolate the modifier window.
        try:
            decl_re = self.method_declaration_regex(ctx.method)
        except FilterConfigError:
            return False
        # Prefer the declaration at/near `method_offset` so we don't inspect
        # an unrelated method's modifiers (e.g. an `abstract void foo()`
        # later in the same file).
        m = self._best_decl_match(decl_re, ctx.content, ctx.method_offset)
        if not m:
            return False
        start = m.start()
        # Cap the window at the method-body open-brace or end-of-statement
        # `;` so we never bleed into the next method.
        cap = self._signature_end_cap(ctx.content, start, self.modifier_window)
        window = ctx.content[start:cap]
        for kw in keywords:
            pattern = r"(?<![A-Za-z0-9_])" + re.escape(kw) + r"(?![A-Za-z0-9_])"
            if re.search(pattern, window):
                return True
        return False

    @staticmethod
    def _best_decl_match(decl_re: re.Pattern, content: str,
                         method_offset: Optional[int]) -> Optional[re.Match]:
        """
        Return the declaration match closest to `method_offset` (when given)
        so modifier checks stay bound to the candidate's own signature.
        """
        if method_offset is None:
            return decl_re.search(content)
        best: Optional[re.Match] = None
        best_dist: Optional[int] = None
        for m in decl_re.finditer(content):
            dist = abs(m.start() - method_offset)
            if best_dist is None or dist < best_dist:
                best, best_dist = m, dist
                if dist == 0:
                    break
        return best

    @staticmethod
    def _signature_end_cap(content: str, start: int, max_window: int) -> int:
        """
        Return the index at which the method *signature* ends — the first
        `{` (body) or `;` (interface/abstract decl) after `start`, capped at
        `start + max_window`. Honors quoted strings.
        """
        end_limit = min(len(content), start + max_window)
        i = start
        in_str: Optional[str] = None
        while i < end_limit:
            ch = content[i]
            if in_str:
                if ch == "\\":
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
            else:
                if ch in ("'", '"'):
                    in_str = ch
                elif ch in ("{", ";"):
                    return i
            i += 1
        return end_limit

    def _filter_regex_pattern(self, fspec: Mapping, ctx: "_FilterContext") -> bool:
        patterns = fspec.get("patterns") or []
        excludes = fspec.get("exclude") or []
        for ex in excludes:
            if re.search(ex, ctx.method or ""):
                return False
        matched = any(re.search(p, ctx.method or "") for p in patterns)
        if not matched:
            return False
        # Optional AND clauses.
        and_block = fspec.get("and") or {}
        bl = and_block.get("body_lines") if isinstance(and_block, dict) else None
        if isinstance(bl, dict) and "max" in bl:
            max_lines = int(bl.get("max", 0) or 0)
            if not _method_body_within_lines(ctx, max_lines, self):
                return False
        return True

    def _filter_path_pattern(self, fspec: Mapping, ctx: "_FilterContext") -> bool:
        patterns = fspec.get("patterns") or []
        if not patterns or not ctx.file_path:
            return False
        path = ctx.file_path.replace("\\", "/")
        for pat in patterns:
            if "*" in pat or "?" in pat:
                if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(os.path.basename(path), pat):
                    return True
            else:
                if pat in path:
                    return True
        return False

    def _filter_name_equals_class(self, fspec: Mapping, ctx: "_FilterContext") -> bool:
        if not self._class_decl_regex or not ctx.content:
            return False
        # Find the *enclosing* class — last class declaration before
        # method_offset (or before the candidate's call-site).
        #
        # Constructors don't match the formal method_declaration regex
        # (no return type), so we fall back to a simpler `\b<name>\s*\(`
        # locator so the anchor is still meaningful.
        anchor = ctx.method_offset
        if anchor is None:
            anchor = self._locate_method_offset(ctx.method, ctx.content)
        last = None
        for cls_match in self._class_decl_regex.finditer(ctx.content):
            if cls_match.start() <= anchor:
                last = cls_match
            else:
                break
        if not last:
            return False
        class_name = last.group(1) if last.lastindex else ""
        return bool(class_name) and class_name == ctx.method

    def _locate_method_offset(self, method: str, content: str) -> int:
        """
        Best-effort offset of the candidate's first call-like occurrence.

        Prefers the formal method_declaration regex; falls back to a bare
        `\\b<name>\\s*\\(` scan that catches Java/TS constructors and any
        declaration shape the YAML's regex doesn't enumerate.
        """
        if not method or not content:
            return 0
        try:
            m = self.method_declaration_regex(method).search(content)
            if m:
                return m.start()
        except FilterConfigError:
            pass
        bare = re.search(r"\b" + re.escape(method) + r"\s*\(", content)
        return bare.start() if bare else 0

    def _filter_name_pattern(self, fspec: Mapping, ctx: "_FilterContext") -> bool:
        # Reuse regex_pattern but ignore body_lines AND clause.
        patterns = fspec.get("patterns") or []
        excludes = fspec.get("exclude") or []
        for ex in excludes:
            if re.search(ex, ctx.method or ""):
                return False
        return any(re.search(p, ctx.method or "") for p in patterns)

    def _filter_file_extension(self, fspec: Mapping, ctx: "_FilterContext") -> bool:
        exts = fspec.get("extensions") or []
        if not exts or not ctx.file_path:
            return False
        path = ctx.file_path.lower()
        return any(path.endswith(e.lower()) for e in exts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _FilterContext:
    __slots__ = ("method", "file_path", "content", "method_offset", "adapter")

    def __init__(self, method, file_path, content, method_offset, adapter):
        self.method = method
        self.file_path = file_path
        self.content = content
        self.method_offset = method_offset
        self.adapter = adapter


def _method_body_within_lines(
    ctx: "_FilterContext", max_lines: int, adapter: ConfigAdapter
) -> bool:
    """
    Approximate body-length check: count newlines between the opening `{`
    that follows the declaration and the matching `}`. For Python (no
    braces) we count INDENTED lines after the `def`. Both heuristics are
    intentionally cheap; the goal is to filter trivial accessors, not to
    parse the full AST.
    """
    if not ctx.content or not ctx.method:
        return False
    try:
        decl_re = adapter.method_declaration_regex(ctx.method)
    except FilterConfigError:
        return False
    m = decl_re.search(ctx.content)
    if not m:
        return False
    after = ctx.content[m.end():]
    # Brace languages: find the next '{', then walk to balanced '}'.
    brace_idx = after.find("{")
    if 0 <= brace_idx < 200:
        depth = 0
        i = brace_idx
        body_lines = 0
        in_str = None
        while i < len(after):
            ch = after[i]
            if in_str:
                if ch == "\\":
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
            else:
                if ch in ("'", '"'):
                    in_str = ch
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        body_text = after[brace_idx + 1: i]
                        body_lines = body_text.count("\n")
                        return body_lines <= max_lines
                elif ch == "\n":
                    pass
            i += 1
        return False
    # Indent-based languages (Python): count subsequent indented lines.
    decl_line_end = after.find("\n")
    if decl_line_end == -1:
        return True
    indent_body = after[decl_line_end + 1:]
    body_lines = 0
    base_indent: Optional[int] = None
    for line in indent_body.split("\n"):
        if not line.strip():
            continue
        cur_indent = len(line) - len(line.lstrip(" \t"))
        if base_indent is None:
            base_indent = cur_indent
            body_lines += 1
            continue
        if cur_indent < base_indent:
            break
        body_lines += 1
        if body_lines > max_lines:
            return False
    return body_lines <= max_lines
