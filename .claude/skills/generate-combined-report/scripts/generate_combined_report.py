#!/usr/bin/env python3
"""
Render the single self-contained HTML optimization report by reading ONLY
the persisted TOON artifacts - manual-duplicates.toon, automation-duplicates.toon,
combine-duplicates.toon, removed-tests.toon, and run-manifest.toon. No
Jira/Xray/git access here; all validation and content decisions were
already made upstream.

This script trusts its inputs completely - it does no cross-agent
exclusivity filtering of its own. By the time this runs,
validate-cross-agent-duplicates has already resolved every "same test id in
more than one set" conflict (automation > combine > manual priority) and
rewritten manual-duplicates.toon / combine-duplicates.toon accordingly, so
what's on disk here is already final. This script only renders it.

All three sections - Automation Summary, Manual Summary, Combined Summary -
are ALWAYS rendered, never silently omitted, even when a side has no data
at all. There are two distinct empty states, and the report says which one
applies rather than leaving it ambiguous:
  - Declined: the corresponding run-manifest.toon entry has skipped=true -
    that side was never checked because the user said so. Shown as a plain
    note, no tile grid (there is nothing to count).
  - Checked but zero: the duplicates file exists but has no candidates at
    all (e.g. every test in the source is already automated, so the manual
    agent found zero manual test cases) - shown WITH the tile grid (it
    correctly reads all zeros) plus an explicit note, so a reader can't
    mistake "genuinely zero" for "this section is missing/broken".

Usage:
  python generate_combined_report.py
"""

import sys
import os
import json
import html
import argparse
import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'optimizer-shared-library', 'scripts'))
import toon_io  # noqa: E402
import report_paths  # noqa: E402
from xray_client import get_jira_base_url  # noqa: E402

MERGING_CRITERIA = (
    "<b>How duplicates are identified:</b> Test cases are first grouped according to their functionality. "
    "Within each functional group, the actual content of every step is carefully reviewed to see how "
    "closely the test cases match. If two or three test cases follow the same overall flow and differ "
    "by only zero, one, or two steps, they are flagged as candidates that could be combined into a "
    "single test case. This report is read-only and highlights suggested duplicates for your review - "
    "your feedback would be appreciated to help train the agent."
)


def _load(path):
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return toon_io.loads(f.read())


SOURCE_LABEL = {"plan": "Test Plan", "testset": "Test Set", "execution": "Test Execution", "repository": "Test Repository"}


def _repo_name_from_url(url):
    """'https://github.com/org/SomeRepo.git' -> 'SomeRepo' - used as the
    report identity when there's no Jira key/project at all (a full
    automation-repo scan with no bounded Jira source), so the suggested PDF
    filename still names the actual repo instead of being left as the
    generic, indistinguishable 'Test-Case-Optimization-Report'."""
    if not url:
        return ""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return name


def _report_identity(manual_doc, automation_doc):
    """Returns (label, ident, page_title).
    label/ident feed the on-page badge-style subtitle (e.g. "Test Execution"
    / "PROJ-1234"); label is "" when the source type isn't known - the
    badge is then skipped and only the identifier is shown.
    page_title is filename-safe - "Test-Case-Optimization-Report_PROJ-1234"
    (or _<PROJECT> for a Test Repository, or _<RepoName> when there's no
    Jira identity at all) - used for <title> only, since most browsers
    suggest the document title as the PDF save-as filename and this format
    avoids the spaces/colons that would otherwise need sanitizing."""
    meta = ((manual_doc or {}).get("meta") or {}) or ((automation_doc or {}).get("meta") or {})
    source = meta.get("source")
    ident = meta.get("project") if source == "repository" else (meta.get("key") or meta.get("project"))
    if not ident:
        repo_ident = _repo_name_from_url((automation_doc or {}).get("meta", {}).get("automationRepo"))
        if repo_ident:
            return "Automation Repo", repo_ident, f"Test-Case-Optimization-Report_{repo_ident}"
        return "", "", "Test-Case-Optimization-Report"
    label = SOURCE_LABEL.get(source, "")
    return label, ident, f"Test-Case-Optimization-Report_{ident}"


def _pct(part, whole):
    return round((part / whole) * 100, 1) if whole else 0.0


def _esc(s):
    return html.escape(str(s or ""))


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #

def _set_elim(sets):
    return sum(max(len(s.get("tests", [])) - 1, 0) for s in sets)


def _automation_stats(doc):
    if not doc:
        return None
    meta = doc.get("meta", {})
    groups = doc.get("groups", [])
    mode = meta.get("mode") or "A"
    analyzed = int(meta.get("analyzedCount") or 0)
    total = int(meta.get("totalRequested") or 0) if mode == "A" else analyzed
    sets = [s for g in groups for s in g.get("sets", [])]
    elim = _set_elim(sets)
    return {
        "mode": mode,
        "total": total,
        "analyzed": analyzed,
        "totalGroups": int(meta.get("totalGroups") or len(groups)),
        "groupsWithDuplicates": len(groups),
        "mergeableSets": len(sets),
        "eliminated": elim,
        "afterMerge": max(analyzed - elim, 0),
        "reductionPct": _pct(elim, analyzed),
        "notFoundCount": int(meta.get("notFoundCount") or 0),
        "automationRepo": meta.get("automationRepo") or "",
    }


def _manual_stats(doc, removed_count):
    meta = doc.get("meta", {}) if doc else {}
    candidate_count = int(meta.get("candidateTestsCount") or 0)
    groups = doc.get("groups", []) if doc else []
    sets = [s for g in groups for s in g.get("sets", [])]
    elim = _set_elim(sets)
    return {
        "total": candidate_count,
        "analyzed": candidate_count,
        "totalGroups": int(meta.get("totalGroups") or 0),
        "groupsWithDuplicates": len(groups),
        "mergeableSets": len(sets),
        "eliminated": elim,
        "afterMerge": max(candidate_count - elim, 0),
        "reductionPct": _pct(elim, candidate_count),
        "removedCount": removed_count,
        "noStepsCount": int(meta.get("noStepsCount") or 0),
    }


def _combine_stats(doc):
    if not doc:
        return None
    groups = doc.get("groups", [])
    sets = [s for g in groups for s in g.get("sets", [])]
    all_ids = set()
    for s in sets:
        for t in s.get("tests", []):
            all_ids.add(t.get("id") or t.get("label"))
    total = len(all_ids)
    elim = _set_elim(sets)
    return {
        "total": total,
        "analyzed": total,
        "totalGroups": len(groups),
        "groupsWithDuplicates": len(groups),
        "mergeableSets": len(sets),
        "eliminated": elim,
        "afterMerge": max(total - elim, 0),
        "reductionPct": _pct(elim, total),
    }


# --------------------------------------------------------------------------- #
# HTML fragments
# --------------------------------------------------------------------------- #

# Single accent (the former Manual green) applied uniformly across every
# section - no per-section color differentiation. Kept as a dict (rather than
# one bare constant) so the rest of the rendering code doesn't need to change
# if per-section colors are ever reintroduced.
_UNIFORM_ACCENT = "#1baf7a"
_UNIFORM_TINT = "#e6f7f1"
ACCENT = {"automation": _UNIFORM_ACCENT, "manual": _UNIFORM_ACCENT, "combined": _UNIFORM_ACCENT}
ACCENT_TINT = {"automation": _UNIFORM_TINT, "manual": _UNIFORM_TINT, "combined": _UNIFORM_TINT}

STEP_DIFF_COLOR = {0: ("#eee", "#333"), 1: ("#eee", "#333"), 2: ("#eee", "#333")}


def _section_heading(title, section):
    color = ACCENT.get(section)
    dot = f'<span class="accent-dot" style="background:{color}"></span>' if color else ""
    style = f' style="border-bottom-color:{color}"' if color else ""
    return f'<h2{style}>{dot}{_esc(title)}</h2>'


def _diff_badge(diff):
    bg, fg = STEP_DIFF_COLOR.get(diff, STEP_DIFF_COLOR[2])
    return f'<span class="diff-badge" style="background:{bg};color:{fg}">diff {diff}</span>'


def _summary_tiles(title, stats, section, extra_rows=None, page_break=True):
    """page_break=False for the first summary actually rendered (whichever
    section that turns out to be) - forcing a break right after the short
    title/criteria block leaves page 1 almost blank. Every summary after the
    first one still starts on its own page."""
    color = ACCENT.get(section, "#999")
    tint = ACCENT_TINT.get(section, "#f5f5f5")
    rows = [
        ("Total test cases", stats["total"]),
        ("Analyzed", stats["analyzed"]),
        ("Functional groups", stats["totalGroups"]),
        ("Groups with duplicates", stats["groupsWithDuplicates"]),
        ("Mergeable sets", stats["mergeableSets"]),
        ("Eliminable", stats["eliminated"]),
        ("Reduction", f'{stats["reductionPct"]}%'),
        ("After merge", stats["afterMerge"]),
    ]
    if extra_rows:
        rows.extend(extra_rows)
    tiles = "".join(
        f'<div class="tile" style="border-top-color:{color};background:{tint}">'
        f'<div class="tile-label">{_esc(k)}</div><div class="tile-value">{_esc(v)}</div></div>'
        for k, v in rows
    )
    wrapper_class = "page-start" if page_break else ""
    return f'<div class="{wrapper_class}">{_section_heading(title, section)}<div class="tile-grid">{tiles}</div></div>'


_JIRA_BASE = ""


def _key_link(key):
    """Render a Jira key as a clickable link when present, plain text otherwise
    (e.g. Mode B automation tests have no Jira key at all)."""
    if not key:
        return "&nbsp;"
    k = _esc(key)
    return f'<a href="{_JIRA_BASE}/browse/{k}" target="_blank" rel="noopener">{k}</a>'


def _manual_set_row(t):
    return (f'<tr><td>{_key_link(t.get("key"))}</td><td>{_esc(t.get("summary"))}</td>'
            f'<td>{t.get("stepCount", 0)}</td><td>{_esc("Description" if t.get("stepSource")=="description" else "Steps Field")}</td></tr>')


def _automation_set_row(t):
    return (f'<tr><td>{_key_link(t.get("id"))}</td><td>{_esc(t.get("testName"))}</td>'
            f'<td>{t.get("stepCount", 0)}</td><td>{_esc(t.get("automationFilePath"))}</td></tr>')


def _combine_set_row(t):
    origin = _esc(t.get("origin"))
    src = (t.get("source") or "").strip().capitalize()
    return (f'<tr><td>{_key_link(t.get("id"))}</td><td>{_esc(t.get("label"))} <b>[{_esc(src)}]</b></td>'
            f'<td>{t.get("stepCount", 0)}</td><td>{origin}</td></tr>')


def _render_group_block(name, sets, row_fn, header, colgroup, gnum):
    group_parts = [f'<h3>{gnum}. {_esc(name)}</h3>']
    for i, s in enumerate(sets, 1):
        crit = _esc(s.get("criteria"))
        rationale = _esc(s.get("mergeRationale"))
        suggested = (f' &mdash; <span class="suggest-inline"><b>Suggested name:</b> {_esc(s.get("suggestedName"))}</span>'
                     if s.get("suggestedName") else "")
        group_parts.append(
            f'<div class="set-block">'
            f'<div class="set-header">Set {i} {_diff_badge(s.get("stepDiff", 0))}{suggested}</div>'
            f'<table class="settbl"><colgroup>{colgroup}</colgroup><thead><tr>{header}</tr></thead><tbody>'
            + "".join(row_fn(t) for t in s.get("tests", []))
            + "</tbody></table>"
            f'<div class="criteria"><b>Difference:</b> {crit}</div>'
            f'<div class="criteria"><b>How to combine:</b> {rationale}</div></div>'
        )
    return f'<div class="group-block">{"".join(group_parts)}</div>'


def _groups_section(title, groups, row_fn, header_cols, section, col_widths, one_liner=None, requested_folders=None):
    """requested_folders (optional, ordered): when the run was scoped to
    specific folders, render one sub-section per requested folder, in the
    order the user named them, instead of one flat alphabetical list - a
    group's own name is expected to be '<requestedFolder>/<leafName>' (see
    the automation agent's naming convention), so the requested-folder
    prefix both scopes each sub-section and disambiguates two different
    requested folders that happen to contain a same-named leaf functional
    area (a real, confirmed case, not hypothetical)."""
    parts = [_section_heading(title, section)]
    if one_liner:
        parts.append(f'<p class="note">{_esc(one_liner)}</p>')
    if not groups and not requested_folders:
        parts.append("<p><i>No duplicate sets found.</i></p>")
        return "".join(parts)

    header = "".join(f"<th>{_esc(c)}</th>" for c in header_cols)
    colgroup = "".join(f'<col style="width:{w}%">' for w in col_widths)

    if requested_folders:
        def _matches_scope(name, scope):
            """A group belongs to this scope if its name IS the scope
            (the whole folder was one group, no split needed), sits under it
            as a deeper leaf ('scope/leaf'), or is the scope itself with a
            '(batch N)' suffix directly appended ('scope (batch 1)') - the
            case a naive '<scope>/' prefix check misses entirely, since
            there's no '/' before the batch suffix when the requested folder
            IS the leaf-level functional group (verified as a real case, not
            hypothetical)."""
            return name == scope or name.startswith(scope + "/") or name.startswith(scope + " (")

        def _leaf_name_for_scope(name, scope):
            if name.startswith(scope + "/"):
                return name[len(scope) + 1:]
            return name  # exact match or "scope (batch N)" - show in full, self-explanatory

        claimed = set()
        for scope in requested_folders:
            scope_groups = [g for g in groups if _matches_scope(g.get("name") or "", scope)]
            claimed.update(id(g) for g in scope_groups)
            parts.append(f'<h3 class="scope-heading">{_esc(scope)}</h3>')
            scope_groups = [g for g in scope_groups if g.get("sets")]
            if not scope_groups:
                parts.append("<p><i>No duplicate sets found.</i></p>")
                continue
            for gnum, g in enumerate(sorted(scope_groups, key=lambda x: (x.get("name") or "").lower()), 1):
                leaf_name = _leaf_name_for_scope(g.get("name") or "", scope)
                parts.append(_render_group_block(leaf_name, g.get("sets", []), row_fn, header, colgroup, gnum))
        leftover = [g for g in groups if id(g) not in claimed and g.get("sets")]
        if leftover:
            parts.append('<h3 class="scope-heading">Other</h3>')
            for gnum, g in enumerate(sorted(leftover, key=lambda x: (x.get("name") or "").lower()), 1):
                parts.append(_render_group_block(g.get("name"), g.get("sets", []), row_fn, header, colgroup, gnum))
        return "".join(parts)

    gnum = 0
    for g in sorted(groups, key=lambda x: (x.get("name") or "").lower()):
        sets = g.get("sets", [])
        if not sets:
            continue
        gnum += 1
        parts.append(_render_group_block(g.get("name"), sets, row_fn, header, colgroup, gnum))
    return "".join(parts)


def _declined_summary(title, section, message, page_break=True):
    """For a side that was never checked at all (no file exists) - a plain
    note instead of a tile grid, since there is nothing to count."""
    wrapper_class = "page-start" if page_break else ""
    return f'<div class="{wrapper_class}">{_section_heading(title, section)}<p class="note">{_esc(message)}</p></div>'


def _dense_id_table(title, keys, jira_base):
    """Compact multi-column, ID-only table - used for both Removed and
    No-Steps-Available so either can hold hundreds of IDs in minimal space."""
    if not keys:
        return ""
    cols = 10
    rows = []
    for i in range(0, len(keys), cols):
        chunk = keys[i:i + cols]
        cells = "".join(
            f'<td><a href="{jira_base}/browse/{_esc(k)}" target="_blank" rel="noopener">{_esc(k)}</a></td>'
            for k in chunk
        )
        cells += "<td></td>" * (cols - len(chunk))
        rows.append(f"<tr>{cells}</tr>")
    return (f'<div class="dense-block"><h2>{_esc(title)} ({len(keys)})</h2>'
            f'<table class="idgrid">{"".join(rows)}</table></div>')


PAGE_CSS = """
@page { size: A4; margin: 10mm 8mm; }
* { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; color-adjust: exact; }
body { font-family: Arial, Helvetica, sans-serif; max-width: 1000px; margin: 20px auto; padding: 0 14px; color: #1a1a1a; line-height: 1.25; font-size: 11px; }
.page-start { page-break-before: always; break-before: page; }
.report-header { background: linear-gradient(135deg, #eafaf3, #f4fbf8); border: 1px solid #cfeee1; border-radius: 5px; padding: 10px 14px; margin: 0 0 10px; }
h1 { font-size: 18px; margin: 0; color: #0d7d54; }
.report-subtitle { margin-top: 5px; display: flex; align-items: center; gap: 8px; }
.subtitle-chip { display: inline-block; background: #1baf7a; color: #fff; font-size: 10px; font-weight: 700; padding: 2px 9px; border-radius: 10px; letter-spacing: .02em; }
.subtitle-ident { font-size: 13px; font-weight: 700; color: #1a1a1a; }
.generated-at { font-size: 9.5px; color: #666; margin-bottom: 8px; }
.pdf-btn { margin: 0 0 10px; }
.pdf-btn button { font-size: 11px; padding: 4px 12px; cursor: pointer; }
h2 { font-size: 13px; margin: 16px 0 6px; border-bottom: 2px solid #999; padding-bottom: 2px; page-break-after: avoid; break-after: avoid; display: flex; align-items: center; gap: 5px; }
h3 { font-size: 11.5px; margin: 8px 0 4px; page-break-after: avoid; break-after: avoid; }
h3.scope-heading { font-size: 13px; margin: 14px 0 6px; padding-bottom: 2px; border-bottom: 1px solid #ccc; color: #0d7d54; page-break-after: avoid; break-after: avoid; }
.accent-dot { display: inline-block; width: 9px; height: 9px; border-radius: 50%; }
a { color: #0563c1; }
a:hover { text-decoration: none; }
table { border-collapse: collapse; width: 100%; margin: 3px 0; font-size: 10.5px; table-layout: fixed; }
table.settbl th, table.settbl td { border: 1px solid #ccc; padding: 3px 6px; text-align: left; font-size: 10.5px; line-height: 1.35; word-wrap: break-word; overflow-wrap: break-word; }
table.settbl th { background: #c9def6; font-weight: bold; color: #10365c; }
table.settbl td:first-child { font-size: 9.5px; color: #444; }
table.settbl tbody tr:nth-child(odd) { background: #f2f7fd; }
table.settbl tbody tr:nth-child(even) { background: #e6f0fb; }
table.idgrid td { border: 1px solid #ddd; padding: 2px 4px; font-size: 8.5px; text-align: center; }
table.idgrid a { text-decoration: none; }
table.idgrid a:hover { text-decoration: underline; }
.tile-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 4px; margin: 4px 0 10px; page-break-inside: avoid; break-inside: avoid; }
.tile { border-top: 3px solid #999; padding: 4px 6px; }
.tile-label { font-size: 8.5px; color: #52514e; text-transform: uppercase; letter-spacing: .02em; }
.tile-value { font-size: 14px; font-weight: 700; color: #1a1a1a; }
.diff-badge { display: inline-block; font-size: 9px; font-weight: 700; padding: 1px 6px; border-radius: 8px; margin-left: 4px; }
.set-block { border: 1px solid #333; padding: 4px 8px; margin: 5px 0; page-break-inside: avoid; break-inside: avoid; }
.set-header { font-weight: bold; margin-bottom: 3px; font-size: 10.5px; }
.suggest-inline { font-weight: normal; font-size: 10px; color: #333; }
.suggest-inline b { font-weight: bold; }
.criteria { font-size: 10px; margin-top: 3px; color: #333; word-wrap: break-word; overflow-wrap: break-word; }
.note { font-style: italic; color: #444; font-size: 10.5px; }
.crit-box { background: #e6f7f1; border: 1px solid #bfe9dc; border-left: 4px solid #1baf7a; border-radius: 3px; padding: 8px 12px; font-size: 12.5px; line-height: 1.5; margin-bottom: 6px; page-break-inside: avoid; break-inside: avoid; }
.group-block { border: 1px solid #000; border-radius: 3px; padding: 6px 8px; margin: 8px 0; overflow: hidden; }
.dense-block { page-break-inside: avoid; break-inside: avoid; }
tr { page-break-inside: avoid; break-inside: avoid; }
@media print {
  body { max-width: 100%; margin: 0; padding: 0; font-size: 10.5px; }
  table.settbl th, table.settbl td { font-size: 11px; }
  table.settbl td:first-child { font-size: 9.5px; }
  table.idgrid td { font-size: 8.5px; }
  .tile-value { font-size: 12px; }
  .pdf-btn { display: none; }
}
"""


def main():
    parser = argparse.ArgumentParser(description="Render the combined optimization report from the persisted TOON files (read-only).")
    parser.parse_args()

    jira_base = get_jira_base_url()
    global _JIRA_BASE
    _JIRA_BASE = jira_base

    manual_doc = _load(report_paths.manual_duplicates_path())
    automation_doc = _load(report_paths.automation_duplicates_path())
    combine_doc = _load(report_paths.combine_duplicates_path())
    removed_doc = _load(report_paths.removed_tests_path())
    manifest_doc = _load(report_paths.run_manifest_path()) or {}

    automation_skipped = bool((manifest_doc.get("automation") or {}).get("skipped"))
    manual_skipped = bool((manifest_doc.get("manual") or {}).get("skipped"))

    no_steps_keys = [t.get("key") for t in ((manual_doc or {}).get("noSteps") or []) if t.get("key")]
    removed_keys = (removed_doc or {}).get("removed") or []

    automation_stats = _automation_stats(automation_doc)
    manual_stats = _manual_stats(manual_doc, len(removed_keys))
    combine_stats = _combine_stats(combine_doc)

    generated_at = datetime.datetime.now().strftime("%d-%m-%Y %I:%M:%S %p")
    subtitle_label, subtitle_ident, page_title = _report_identity(manual_doc, automation_doc)
    subtitle_html = ""
    if subtitle_ident:
        chip = f'<span class="subtitle-chip">{_esc(subtitle_label)}</span>' if subtitle_label else ""
        subtitle_html = f'<div class="report-subtitle">{chip}<span class="subtitle-ident">{_esc(subtitle_ident)}</span></div>'
    sections = [
        f'<div class="report-header"><h1>Test Case Optimization Report</h1>{subtitle_html}</div>',
        f'<div class="generated-at">Generated: {_esc(generated_at)}</div>',
        '<p class="pdf-btn"><button onclick="window.print()">Download as PDF</button></p>',
        f'<div class="crit-box">{MERGING_CRITERIA}</div>',
    ]
    first_summary_done = False

    # --- Automation Summary - always rendered ---
    if automation_skipped:
        sections.append(_declined_summary(
            "Automation Summary", "automation",
            "Automation-side duplicate detection was not requested for this run, so no automation repository was checked.",
            page_break=first_summary_done,
        ))
        first_summary_done = True
    elif not automation_stats:
        sections.append(_declined_summary(
            "Automation Summary", "automation",
            "No automation data is available for this run.",
            page_break=first_summary_done,
        ))
        first_summary_done = True
    else:
        sections.append(_summary_tiles("Automation Summary", automation_stats, "automation", page_break=first_summary_done))
        first_summary_done = True
        if automation_stats["analyzed"] == 0:
            sections.append(
                '<p class="note">No automation test cases were found to analyze for this source '
                '(the repository scan matched zero tests) - this is a genuine result, not an error.</p>'
            )
        one_liner = None
        if automation_stats["mode"] == "A" and automation_stats["notFoundCount"] > 0:
            one_liner = f'{automation_stats["notFoundCount"]} test cases from this set were not found in the automation repository.'
        requested_folders = ((automation_doc or {}).get("meta") or {}).get("requestedFolders") or None
        sections.append(_groups_section(
            "Duplicate Test Cases in Automation Repo",
            (automation_doc or {}).get("groups", []),
            _automation_set_row,
            ["Test Case", "Test Name", "Steps", "Automation File"],
            "automation",
            [12, 42, 7, 39],
            one_liner=one_liner,
            requested_folders=requested_folders,
        ))

    # --- Manual Summary - always rendered ---
    if manual_skipped:
        sections.append(_declined_summary(
            "Manual Summary", "manual",
            "Manual/Jira duplicate detection was not requested for this run, so no Jira test cases were checked.",
            page_break=first_summary_done,
        ))
        first_summary_done = True
    elif not manual_doc:
        sections.append(_declined_summary(
            "Manual Summary", "manual",
            "No manual/Jira data is available for this run.",
            page_break=first_summary_done,
        ))
        first_summary_done = True
    else:
        sections.append(_summary_tiles("Manual Summary", manual_stats, "manual", extra_rows=[
            ("Removed (Jira status)", manual_stats["removedCount"]),
            ("No steps available", manual_stats["noStepsCount"]),
        ], page_break=first_summary_done))
        first_summary_done = True
        if manual_stats["analyzed"] == 0:
            sections.append(
                '<p class="note">No manual test cases were found to analyze for this source - for example, '
                'every test case in scope may already be automated. This is a genuine result, not an error.</p>'
            )
        sections.append(_groups_section(
            "Duplicate Test Cases (Manual)",
            manual_doc.get("groups", []),
            _manual_set_row,
            ["Test Case", "Summary", "Steps", "Step Source"],
            "manual",
            [12, 63, 7, 18],
        ))

    # --- Combined Summary - always rendered ---
    if automation_skipped or manual_skipped:
        skipped_side = "automation" if automation_skipped else "manual"
        sections.append(_declined_summary(
            "Combined Summary", "combined",
            f'Combined cross-agent comparison was skipped because {skipped_side}-side duplicate detection '
            f'was not requested for this run - there is nothing to combine against.',
            page_break=first_summary_done,
        ))
        first_summary_done = True
    elif not combine_doc:
        sections.append(_declined_summary(
            "Combined Summary", "combined",
            "No combined cross-agent comparison is available for this run.",
            page_break=first_summary_done,
        ))
        first_summary_done = True
    else:
        sections.append(_summary_tiles("Combined Summary", combine_stats, "combined", page_break=first_summary_done))
        first_summary_done = True
        if combine_stats["total"] == 0:
            sections.append(
                '<p class="note">No cross-agent duplicate candidates were found - the manual and automation '
                'results did not overlap. This is a genuine result, not an error.</p>'
            )
        sections.append(_groups_section(
            "Duplicate Test Cases (Combined Manual + Automation)",
            (combine_doc or {}).get("groups", []),
            _combine_set_row,
            ["Test Case", "Summary / Test Name", "Steps", "Step Source"],
            "combined",
            [12, 52, 7, 29],
        ))

    # Removed / No-Steps come last, after Combined, per report ordering.
    sections.append(_dense_id_table("Removed Test Cases", removed_keys, jira_base))
    sections.append(_dense_id_table("No Steps Available", no_steps_keys, jira_base))

    html_doc = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{_esc(page_title)}</title>"
        f"<style>{PAGE_CSS}</style></head><body>"
        + "".join(sections) +
        "</body></html>"
    )

    out_path = report_paths.report_html_path()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_doc)

    terminal_summary_lines = []
    if automation_skipped:
        terminal_summary_lines.append("Automation: skipped (not requested for this run).")
    elif not automation_stats:
        terminal_summary_lines.append("Automation: no data available for this run.")
    else:
        terminal_summary_lines.append(
            f'Automation: {automation_stats["analyzed"]} analyzed -> {automation_stats["afterMerge"]} '
            f'({automation_stats["reductionPct"]}% fewer) across {automation_stats["mergeableSets"]} set(s).'
        )
        if automation_stats["mode"] == "A" and automation_stats["notFoundCount"] > 0:
            terminal_summary_lines.append(f'{automation_stats["notFoundCount"]} test cases from this set were not found in the automation repository.')

    if manual_skipped:
        terminal_summary_lines.append("Manual: skipped (not requested for this run).")
    elif not manual_doc:
        terminal_summary_lines.append("Manual: no data available for this run.")
    else:
        terminal_summary_lines.append(
            f'Manual: {manual_stats["analyzed"]} analyzed -> {manual_stats["afterMerge"]} '
            f'({manual_stats["reductionPct"]}% fewer) across {manual_stats["mergeableSets"]} set(s). '
            f'Removed: {manual_stats["removedCount"]}. No steps: {manual_stats["noStepsCount"]}.'
        )

    if automation_skipped or manual_skipped:
        terminal_summary_lines.append("Combined: skipped (one side was not checked).")
    elif not combine_stats:
        terminal_summary_lines.append("Combined: no cross-agent comparison available for this run.")
    else:
        terminal_summary_lines.append(
            f'Combined: {combine_stats["mergeableSets"]} cross-agent set(s), '
            f'{combine_stats["eliminated"]} additional test case(s) eliminable.'
        )
    terminal_summary_lines.append(f"Report: file:///{os.path.abspath(out_path).replace(os.sep, '/')}")
    terminal_summary = "\n".join(terminal_summary_lines)

    print(json.dumps({
        "success": True,
        "reportPath": os.path.abspath(out_path),
        "reportUrl": f"file:///{os.path.abspath(out_path).replace(os.sep, '/')}",
        "terminalSummary": terminal_summary,
    }, indent=2))


if __name__ == "__main__":
    main()
