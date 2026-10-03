"""Student review presentation and queue navigation, without recognition work."""
from __future__ import annotations

import html
import io
import json
from pathlib import Path
import urllib.parse

from PIL import Image

from omr.workflows.review import selected_student_pages


QUEUE_FILTERS = {
    "all": "All students",
    "unchecked": "Needs human attention",
    "pending_verification": "Awaiting verification",
    "auto_matched": "Auto matched",
    "approved": "Approved for release",
    "needs_review": "Needs review",
    "missing_pages": "Missing pages",
    "verified": "Manually checked",
    "rejected": "Rejected",
}
STATUS_LABELS = {
    "auto_matched": "Auto matched",
    "approved": "Approved for release",
    "pending_verification": "Awaiting verification",
    "needs_review": "Needs review",
    "missing_pages": "Missing pages",
    "verified": "Manually checked",
    "rejected": "Rejected",
}


def queue_parameters(values: dict[str, str]) -> tuple[str, str]:
    kind = values.get("queue", "all")
    return kind if kind in QUEUE_FILTERS else "all", values.get("q", "").strip()[:200]


def queue_matches(student: dict, kind: str, search: str) -> bool:
    status = student.get("status") or "needs_review"
    matches = (
        kind == "all"
        or (kind == "unchecked" and status not in {"verified", "approved", "auto_matched"})
        or (kind == "needs_review" and status in {"needs_review", "missing_pages", "rejected"})
        or (kind == "missing_pages" and bool(student.get("missing_pages")))
        or (kind not in {"unchecked", "needs_review", "missing_pages"} and status == kind)
    )
    return matches and search.casefold() in (
        str(student.get("roll_no") or "") + " " + str(student.get("student_name") or "")
    ).casefold()


def student_url(run_id: str, roll_no: str, kind: str = "all", search: str = "", **extra: str) -> str:
    query = urllib.parse.urlencode({"queue": kind, "q": search, **extra})
    return f"/runs/{run_id}/students/{urllib.parse.quote(roll_no, safe='')}?{query}"


def next_student(index: dict, roll_no: str, kind: str, search: str) -> str | None:
    rolls = sorted(str(student["roll_no"]) for student in index.get("students", [])
                   if queue_matches(student, kind, search) and str(student["roll_no"]) > roll_no)
    return rolls[0] if rolls else None


def selection_conflicts(index: dict, student: dict) -> list[str]:
    """Reject ambiguous ownership; manual replacements legitimately override a slot."""
    conflicts = []
    expected = int(index.get("expected_pages") or 0)
    for collection in (student.get("pages", []), student.get("manual_pages", [])):
        slots = [int(page.get("page") or 0) for page in collection]
        if len(slots) != len(set(slots)):
            conflicts.append("Duplicate sheet-page slots in the saved selection.")
        if any(slot < 1 or (expected and slot > expected) for slot in slots):
            conflicts.append("A selected sheet page is outside the manifest.")
    pages = selected_student_pages(student)
    sources = [page["source_index"] for page in pages if page.get("source_index") is not None]
    if len(sources) != len(set(sources)):
        conflicts.append("The same source page occupies more than one sheet-page slot.")
    for other in index.get("students", []):
        if str(other.get("roll_no")) == str(student.get("roll_no")):
            continue
        overlap = set(sources) & {page.get("source_index") for page in selected_student_pages(other)}
        if overlap:
            conflicts.append(f"Source page(s) {', '.join(str(source) for source in sorted(overlap))} "
                             f"also belong to {other['roll_no']}.")
    return list(dict.fromkeys(conflicts))


def preview_pdf(image_paths: list[Path]) -> bytes:
    if not image_paths:
        raise ValueError("This student has no selected pages to preview")
    images = []
    try:
        for path in image_paths:
            with Image.open(path) as image:
                images.append(image.convert("RGB"))
        output = io.BytesIO()
        images[0].save(output, format="PDF", save_all=True, append_images=images[1:], resolution=300.0)
        return output.getvalue()
    finally:
        for image in images:
            image.close()


def _icon(name: str) -> str:
    return f'<img class="tool-icon" src="/static/icons/{name}.svg" alt="" aria-hidden="true">'


def _tool(identifier: str, name: str, label: str) -> str:
    return (f'<button id="{identifier}" type="button" class="icon-tool" title="{label}" '
            f'aria-label="{label}">{_icon(name)}</button>')


def workspace_body(*, state: dict, student: dict, index: dict, summary: dict, page_views: list[dict],
                   flags: list[str], conflicts: list[str], revision: str, kind: str, search: str,
                   pdf_link: str, operations: str, answers: str, original: str,
                   marks: str = "", saved: str = "", finished: bool = False) -> str:
    esc = html.escape
    run_id = str(state["run_id"])
    roll = str(student["roll_no"])
    base = f"/runs/{run_id}/students/{urllib.parse.quote(roll, safe='')}"
    missing = list(student.get("missing_pages", []))
    expected = int(index.get("expected_pages") or len(page_views))
    status = student.get("status") or "needs_review"
    counts = {
        "Students": summary.get("students", 0),
        "Manually checked": summary.get("verified", 0),
        "Auto matched": summary.get("auto_matched", 0),
        "Approved for release": summary.get("approved", 0),
        "Awaiting verification": summary.get("pending_verification", 0),
        "Needs review": summary.get("needs_review", 0),
    }
    unmatched = summary.get("unmatched_pages", 0)
    rows = []
    for row in sorted(index.get("students", []), key=lambda row: str(row["roll_no"])):
        row_roll = str(row["roll_no"])
        row_status = str(row.get("status") or "needs_review")
        name = str(row.get("student_name") or "")
        is_missing = bool(row.get("missing_pages"))
        visible = queue_matches(row, kind, search)
        current = ' aria-current="page"' if row_roll == roll else ""
        rows.append(
            f'<a class="student-item" data-roll="{esc(row_roll)}" data-name="{esc(name)}" '
            f'data-status="{esc(row_status)}" data-missing="{str(is_missing).lower()}" '
            f'href="{esc(student_url(run_id, row_roll, kind, search))}"{current}{" hidden" if not visible else ""}>'
            f'<strong>{esc(row_roll)}</strong><span>{esc(name)}</span>'
            f'<small class="queue-status {esc(row_status)}">{esc(STATUS_LABELS.get(row_status, row_status))}</small></a>'
        )
    filters = "".join(f'<option value="{value}"{" selected" if kind == value else ""}>{label}</option>'
                      for value, label in QUEUE_FILTERS.items())
    page_buttons = []
    for page in page_views:
        number = page["page"]
        thumbnail = (f'<img src="{esc(page["aligned"])}" alt="" loading="lazy">'
                     if page.get("aligned") else '<span class="missing-thumb">Missing</span>')
        source = f'Source {page["source_index"]}' if page.get("source_index") else "No source"
        page_buttons.append(f'<button class="sheet-thumb" type="button" data-page="{number}" '
                            f'aria-label="Sheet page {number}" aria-pressed="false">{thumbnail}'
                            f'<span>Page {number}</span><small>{esc(source)}</small></button>')
    disabled = " disabled" if conflicts or not any(page.get("aligned") for page in page_views) else ""
    missing_choice = (
        '<label class="incomplete-choice"><input type="checkbox" name="allow_missing" value="1">'
        'Mark incomplete sheet checked (not eligible for email)</label>' if missing else ""
    )
    flags_html = "".join(f"<li>{esc(flag)}</li>" for flag in flags) or '<li class="muted">No active review flags</li>'
    conflicts_html = "".join(f'<p class="conflict">{esc(reason)}</p>' for reason in conflicts)
    missing_html = (f'<p class="missing-warning">Missing sheet page(s): {esc(", ".join(map(str, missing)))}</p>'
                    if missing else "")
    evidence = "".join(
        '<tr>'
        f'<td>{page["page"]}</td><td>'
        + (f'<a href="/runs/{esc(run_id)}/pages?page={page["source_index"]}">Source {page["source_index"]}</a>'
           if page.get("source_index") else "-" )
        + f'</td><td>{esc(page.get("origin") or "Missing")}</td></tr>' for page in page_views
    )
    data = json.dumps({"pages": page_views, "roll": roll, "runId": run_id, "missing": missing,
                       "blocked": bool(disabled)}).replace("<", "\\u003c")
    feedback = {"verify": "Saved: sheet manually checked.", "hold": "Saved: sheet kept for review.",
                "reject": "Saved: grouping rejected."}.get(saved, "")
    if feedback and finished:
        feedback += " End of this queue."
    stage_label = "Sheet matching" if state.get("inputs", {}).get("grouping_only") else "Sheet matching and grading"
    return f"""
    <link rel="stylesheet" href="/static/student.css">
    <script src="/static/student.js" defer></script>
    <section id="student-workspace" data-roll="{esc(roll)}">
      <script id="student-view-data" type="application/json">{data}</script>
      <div class="student-heading">
        <div><div class="student-breadcrumb"><a href="/runs/{esc(run_id)}">{esc(state.get('exam_id', ''))}</a><span>/</span>Student review</div>
          <h1>{esc(roll)} <span>{esc(str(student.get('student_name') or ''))}</span></h1>
          <div class="student-subtitle">{esc(str(student.get('student_email') or 'No roster email'))}<span>{stage_label}</span>{marks}</div>
        </div>
        <div class="workspace-links"><a href="/runs/{esc(run_id)}/review">Review Sheets</a>
          <a href="/runs/{esc(run_id)}/pages?page-filter=matching_unmatched">Unmatched Sources ({unmatched})</a>
          <a href="/runs/{esc(run_id)}/email">Email Release</a></div>
      </div>
      <div class="student-summary">{''.join(f'<span><strong>{count}</strong> {label}</span>' for label, count in counts.items())}</div>
      <div id="student-save-feedback" role="status"{" hidden" if not feedback else ""}>{esc(feedback)}</div>
      <div class="student-workspace-grid">
        <aside class="student-queue" aria-label="Student queue">
          <div class="queue-filters"><h2>Students <output id="student-queue-count"></output></h2>
            <label class="visually-hidden" for="student-search">Search roll or name</label>
            <div class="student-search">{_icon('search')}<input id="student-search" type="search" value="{esc(search)}" placeholder="Roll or name" maxlength="200"></div>
            <label class="visually-hidden" for="student-filter">Review status</label><select id="student-filter">{filters}</select>
            <div class="student-navigation">{_tool('student-prev', 'chevron-left', 'Previous student')}<output id="student-position"></output>{_tool('student-next', 'chevron-right', 'Next student')}</div>
          </div>
          <nav class="student-list" aria-label="Student sheets">{''.join(rows)}</nav>
          <p id="student-queue-empty" class="empty" hidden>No matching students</p>
        </aside>
        <section class="student-document" aria-label="Current selected sheet">
          <div class="sheet-preview-heading"><div><h2>Current Selection Preview</h2><span>{len(page_views) - len(missing)} of {expected} pages selected</span></div>
            <span class="student-state {esc(status)}">{esc(STATUS_LABELS.get(status, status))}</span></div>
          <div class="sheet-toolbar">
            <div class="sheet-navigation">{_tool('sheet-prev', 'chevron-left', 'Previous sheet page')}<label class="visually-hidden" for="sheet-jump">Sheet page</label>
              <input id="sheet-jump" type="number" min="1" max="{max(expected, len(page_views), 1)}" value="1"><span>/ {expected}</span>{_tool('sheet-next', 'chevron-right', 'Next sheet page')}</div>
            <div class="sheet-modes" aria-label="Page image mode">
              <button type="button" data-mode="original" aria-pressed="false">Original</button>
              <button type="button" data-mode="aligned" aria-pressed="true">Aligned</button>
              <button type="button" data-mode="overlay" aria-pressed="false">Overlay</button>
              <button type="button" data-mode="sampling" aria-pressed="false">Sampling</button>
            </div>
            <div class="sheet-zoom">{_tool('sheet-zoom-out', 'zoom-out', 'Zoom out')}<output id="sheet-zoom-value">100%</output>{_tool('sheet-zoom-in', 'zoom-in', 'Zoom in')}{_tool('sheet-fit', 'maximize', 'Fit page')}</div>
          </div>
          <div id="sheet-canvas" tabindex="0" aria-label="Selected sheet page"><div id="sheet-image-stage"><img id="sheet-image" alt="" hidden></div><p id="sheet-empty" role="status">Loading sheet page</p></div>
          <div class="sheet-source"><strong id="sheet-source-label"></strong><a id="sheet-correct-link" hidden>Correct Page Assignment {_icon('arrow-right')}</a></div>
          <nav class="sheet-thumbnails" aria-label="Sheet pages">{''.join(page_buttons)}</nav>
          <div class="sheet-pdf-links"><a href="{esc(base)}/preview.pdf" target="_blank" rel="noopener">{_icon('file-text')}Open Current Preview PDF</a>{pdf_link}</div>
          {answers}
        </section>
        <aside class="student-evidence" aria-label="Ownership and review decisions">
          <h2>Review Decision</h2>{missing_html}{conflicts_html}
          <form id="student-decision-form" method="post" action="{esc(base)}/verify">
            <input type="hidden" name="workspace" value="1"><input type="hidden" name="revision" value="{esc(revision)}">
            <input type="hidden" name="queue" value="{esc(kind)}" data-sync-ignore><input type="hidden" name="q" value="{esc(search)}" data-sync-ignore>
            <label for="student-review-note">Review note</label><textarea id="student-review-note" name="note" rows="2"></textarea>
            {missing_choice}
            <div class="student-decisions"><button type="submit" name="advance" value="1" data-decision="verify"{disabled}>Verify &amp; Next {_icon('arrow-right')}</button>
              <button type="submit" class="secondary" name="advance" value="1" data-decision="hold" formaction="{esc(base)}/hold">Keep for Review &amp; Next {_icon('arrow-right')}</button>
              <button type="submit" class="secondary" name="advance" value="0" data-decision="verify"{disabled}>Mark Manually Checked</button>
              <button type="submit" class="danger" name="advance" value="0" data-decision="reject" formaction="{esc(base)}/reject">Reject Grouping</button></div>
          </form>
          <section><h2>Original OCR Evidence</h2>
            <dl class="sheet-roll-evidence"><div><dt>Assigned roll</dt><dd>{esc(roll)}</dd></div>
              <div><dt>Detected sheet page</dt><dd id="sheet-detected-page">-</dd></div>
              <div><dt>Literal roll</dt><dd id="sheet-literal-roll">-</dd></div>
              <div><dt>Bubble roll</dt><dd id="sheet-bubble-roll">-</dd></div>
              <div><dt>Written roll</dt><dd id="sheet-written-roll">-</dd></div>
              <div><dt>Read confidence</dt><dd id="sheet-read-confidence">-</dd></div></dl>
            <ul id="sheet-identity-flags"></ul>
          </section>
          <section><h2>Current Source Pages</h2><table><thead><tr><th>Sheet</th><th>Source</th><th>Selection</th></tr></thead><tbody>{evidence}</tbody></table></section>
          <section><h2>Review Flags</h2><ul>{flags_html}</ul></section>
          <details class="review-tools"><summary>Additional Review Operations</summary>{operations}</details>
          <details class="parser-history"><summary>Original Parser Observations</summary>{original}</details>
        </aside>
      </div>
    </section>
    """
