#!/usr/bin/env python3
"""
Minimal TOON (Token-Oriented Object Notation) reader/writer shared by the
test-cases-optimizer skill scripts.

TOON is a compact, indentation-based, token-efficient serialization. This
module implements the subset needed by this plugin:

  - nested objects (2-space indentation)
  - scalars:                         key: value
  - inline scalar arrays:            key[N]: a,b,c
  - tabular arrays of uniform,
    scalar-only objects:             key[N]{f1,f2}:
                                       v1,v2
                                       ...
  - block arrays of objects (when
    items contain nested arrays):    key[N]:
                                       -
                                         <object>

Comma-bearing values inside tabular/inline arrays are CSV-quoted ("" escapes a
quote). Pure-integer and true/false tokens are decoded to int/bool.

No external dependency. No domain knowledge of any particular schema.
"""

import re

INDENT = "  "


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #

def dumps(obj):
    lines = []
    _emit_dict(obj, 0, lines)
    return "\n".join(lines) + "\n"


def _pad(indent):
    return INDENT * indent


def _scalar(v):
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    return str(v).replace("\n", " ")


def _csv(v):
    s = _scalar(v)
    if s == "" or ("," in s) or ('"' in s) or (s != s.strip()):
        return '"' + s.replace('"', '""') + '"'
    return s


def _uniform_scalar(lst):
    keys = list(lst[0].keys())
    for it in lst:
        if list(it.keys()) != keys:
            return False
        for val in it.values():
            if isinstance(val, (dict, list)):
                return False
    return True


def _emit_dict(d, indent, lines):
    for k, v in d.items():
        _emit_kv(k, v, indent, lines)


def _emit_kv(k, v, indent, lines):
    pad = _pad(indent)
    if isinstance(v, dict):
        lines.append(f"{pad}{k}:")
        _emit_dict(v, indent + 1, lines)
    elif isinstance(v, list):
        n = len(v)
        if n == 0:
            lines.append(f"{pad}{k}[0]:")
        elif all(not isinstance(x, (dict, list)) for x in v):
            lines.append(f"{pad}{k}[{n}]: " + ",".join(_csv(x) for x in v))
        elif all(isinstance(x, dict) for x in v) and _uniform_scalar(v):
            fields = list(v[0].keys())
            lines.append(f"{pad}{k}[{n}]{{{','.join(fields)}}}:")
            for item in v:
                lines.append(_pad(indent + 1) + ",".join(_csv(item.get(f, "")) for f in fields))
        else:
            lines.append(f"{pad}{k}[{n}]:")
            for item in v:
                lines.append(_pad(indent + 1) + "-")
                if isinstance(item, dict):
                    _emit_dict(item, indent + 2, lines)
                else:
                    lines.append(_pad(indent + 2) + _scalar(item))
    else:
        lines.append(f"{pad}{k}: {_scalar(v)}".rstrip())


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #

def loads(text):
    lines = []
    for raw in text.split("\n"):
        if raw.strip() == "":
            continue
        stripped = raw.lstrip(" ")
        indent = (len(raw) - len(stripped)) // 2
        lines.append((indent, stripped))
    obj, _ = _parse_dict(lines, 0, 0)
    return obj


def _coerce(s):
    if s == "":
        return ""
    if s == "true":
        return True
    if s == "false":
        return False
    if re.match(r"^-?\d+$", s):
        try:
            return int(s)
        except ValueError:
            return s
    return s


def _split_csv(s):
    fields = []
    i, n = 0, len(s)
    if s == "":
        return fields
    while True:
        if i < n and s[i] == '"':
            i += 1
            buf = []
            while i < n:
                if s[i] == '"':
                    if i + 1 < n and s[i + 1] == '"':
                        buf.append('"')
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    buf.append(s[i])
                    i += 1
            field = "".join(buf)
            while i < n and s[i] != ",":
                i += 1
        else:
            buf = []
            while i < n and s[i] != ",":
                buf.append(s[i])
                i += 1
            field = "".join(buf).strip()
        fields.append(_coerce(field))
        if i < n and s[i] == ",":
            i += 1
            continue
        break
    return fields


def _parse_dict(lines, pos, indent):
    obj = {}
    while pos < len(lines):
        ind, content = lines[pos]
        if ind != indent or content == "-":
            break
        key, val, pos = _parse_entry(lines, pos, indent)
        obj[key] = val
    return obj, pos


def _parse_entry(lines, pos, indent):
    _, content = lines[pos]

    m = re.match(r"^([^:\[\]]+)\[(\d+)\]\{([^}]*)\}:\s*$", content)
    if m:
        key, count, fields = m.group(1), int(m.group(2)), _split_field_names(m.group(3))
        pos += 1
        items = []
        for _ in range(count):
            _, row = lines[pos]
            vals = _split_csv(row)
            items.append({fields[i]: (vals[i] if i < len(vals) else "") for i in range(len(fields))})
            pos += 1
        return key, items, pos

    m = re.match(r"^([^:\[\]]+)\[(\d+)\]:\s*(.*)$", content)
    if m:
        key, count, rest = m.group(1), int(m.group(2)), m.group(3)
        pos += 1
        if count == 0:
            return key, [], pos
        if rest.strip() != "":
            return key, _split_csv(rest), pos
        items = []
        for _ in range(count):
            pos += 1  # consume the "-" marker line
            item, pos = _parse_dict(lines, pos, indent + 2)
            items.append(item)
        return key, items, pos

    m = re.match(r"^([^:\[\]]+):\s?(.*)$", content)
    if m:
        key, rest = m.group(1), m.group(2)
        pos += 1
        if rest != "":
            return key, _coerce(rest), pos
        if pos < len(lines) and lines[pos][0] > indent and lines[pos][1] != "-":
            sub, pos = _parse_dict(lines, pos, indent + 1)
            return key, sub, pos
        return key, "", pos

    pos += 1
    return content, "", pos


def _split_field_names(s):
    return [f.strip() for f in s.split(",")] if s.strip() else []
