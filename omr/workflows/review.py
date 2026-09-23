"""Terminal review and verification workflow for parsed batch results.

The batch parser is intentionally conservative: it flags anything uncertain
instead of guessing. This module is the local audit layer that lets a human
review those flags and produce a separate verified artifact for later email,
grading, or backend import.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from omr.workflows.parse import _json_path


VERIFICATION_SCHEMA_VERSION = 1
VERIFIED_INDEX_NAME = "verified_index.json"
VERIFICATION_CSV_NAME = "verification_report.csv"
VERIFICATION_HTML_NAME = "verification_report.html"

STUDENT_STATUSES = {"pending_verification", "needs_review", "missing_pages", "verified", "rejected"}
UNMATCHED_PAGE_STATUSES = {"needs_review", "assigned", "ignored"}
PAGE_ERROR_STATUSES = {"error", "ignored"}

VERIFICATION_REPORT_COLUMNS = [
    "item_type",
    "status",
    "roll_no",
    "program",
    "student_name",
    "student_email",
    "pages_found",
    "expected_pages",
    "missing_pages",
    "source_indices",
    "parser_status",
    "eligible_for_email",
    "sheet_pdf_path",
    "verified_sheet_pdf_path",
    "details_path",
    "review_flag_count",
    "review_flags",
    "last_decision_by",
    "last_decision_at",
    "last_decision_note",
    "source_path",
    "assigned_to_roll_no",
    "error_type",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _parse_index_path(parsed_dir: str | Path) -> Path:
    path = Path(parsed_dir)
    if path.name == "parse_index.json":
        return path.resolve()
    return (path / "parse_index.json").resolve()


def _parsed_dir_from_index(parse_index_path: Path) -> Path:
    return parse_index_path.parent.resolve()


def _verified_index_path(parsed_dir: str | Path) -> Path:
    path = Path(parsed_dir)
    if path.name == VERIFIED_INDEX_NAME:
        return path.resolve()
    return (path / VERIFIED_INDEX_NAME).resolve()


def _resolve_path(value: str | Path | None, parsed_dir: Path, base_dir: Path | None = None) -> Path | None:
    if value is None or str(value) == "":
        return None
    path = Path(value)
    if path.is_absolute():
        return path

    candidates: list[Path] = []
    if base_dir is not None:
        candidates.append(base_dir / path)
    candidates.append(parsed_dir / path)
    candidates.append(Path.cwd() / path)

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _relative_path(value: str | Path | None, parsed_dir: Path, base_dir: Path | None = None) -> str | None:
    path = _resolve_path(value, parsed_dir, base_dir)
    if path is None:
        return None
    return _json_path(path, parsed_dir)


def _join_values(values: list[Any]) -> str:
    return ",".join(str(value) for value in values)


def _join_flags(flags: list[Any]) -> str:
    return " | ".join(str(flag).replace("\r", " ").replace("\n", " ") for flag in flags)


def _student_details_payload(student: dict[str, Any], parsed_dir: Path) -> dict[str, Any]:
    details_path = _resolve_path(student.get("details_path"), parsed_dir)
    if details_path is None or not details_path.exists():
        return {}
    return _load_json(details_path)


def _page_number(page: dict[str, Any]) -> int | None:
    value = page.get("page", page.get("page_index"))
    if value is None:
        return None
    return int(value)


def _normalized_student_page(page: dict[str, Any], parsed_dir: Path, base_dir: Path) -> dict[str, Any]:
    page_no = _page_number(page)
    source_index = page.get("source_index")
    return {
        "page": page_no,
        "source_index": int(source_index) if source_index is not None else None,
        "canonical_image_path": _relative_path(page.get("canonical_image_path"), parsed_dir, base_dir),
        "debug_image_path": _relative_path(page.get("debug_image_path"), parsed_dir, base_dir),
        "alignment_overlay_path": _relative_path(page.get("alignment_overlay_path"), parsed_dir, base_dir),
        "sampling_overlay_path": _relative_path(page.get("sampling_overlay_path"), parsed_dir, base_dir),
        "origin": "parser",
    }


def _initial_status(parser_status: str, missing_pages: list[int]) -> str:
    if parser_status == "ready" and not missing_pages:
        return "pending_verification"
    if missing_pages:
        return "missing_pages"
    return "needs_review"


def _student_from_parse_index(
    student: dict[str, Any],
    parsed_dir: Path,
    expected_pages: list[int],
    *,
    auto_verify_ready: bool,
    reviewer: str | None,
    note: str | None,
) -> dict[str, Any]:
    details_path = _resolve_path(student.get("details_path"), parsed_dir)
    details_dir = details_path.parent if details_path is not None else parsed_dir
    details_payload = _student_details_payload(student, parsed_dir)
    detail_student = dict(details_payload.get("student", {}))

    pages = [
        _normalized_student_page(page, parsed_dir, details_dir)
        for page in student.get("pages", [])
        if _page_number(page) is not None
    ]
    pages_found = sorted({page["page"] for page in pages if page["page"] is not None})
    missing_pages = [page for page in expected_pages if page not in pages_found]
    parser_status = str(student.get("status") or "needs_review")
    status = _initial_status(parser_status, missing_pages)
    decision_log: list[dict[str, Any]] = []

    if auto_verify_ready and status == "pending_verification":
        status = "verified"
        decision_log.append(
            {
                "action": "auto_verify_ready",
                "reviewer": reviewer or "system",
                "note": note or "Parser-ready student was auto-verified.",
                "created_at": _now(),
            }
        )

    roll_no = str(student.get("roll_no") or detail_student.get("roll_no") or "")
    sheet_pdf_path = _relative_path(student.get("sheet_pdf_path"), parsed_dir)
    output = {
        "roll_no": roll_no,
        "status": status,
        "parser_status": parser_status,
        "program": detail_student.get("program") or student.get("program"),
        "student_name": student.get("student_name") or detail_student.get("name"),
        "student_email": detail_student.get("email") or student.get("student_email"),
        "mcq_score": student.get("mcq_score"),
        "mcq_total": student.get("mcq_total"),
        "numerical_score": student.get("numerical_score", 0.0),
        "numerical_total": student.get("numerical_total", 0.0),
        "pages": pages,
        "manual_pages": [],
        "pages_found": pages_found,
        "missing_pages": missing_pages,
        "source_indices": sorted(
            source_index
            for source_index in (page.get("source_index") for page in pages)
            if source_index is not None
        ),
        "sheet_pdf_path": sheet_pdf_path,
        "verified_sheet_pdf_path": None,
        "details_path": _relative_path(student.get("details_path"), parsed_dir),
        "review_flags": list(student.get("review_flags", [])),
        "decision_log": decision_log,
        "eligible_for_email": False,
    }
    if status == "verified":
        output["eligible_for_email"] = True
    return output


def _normalized_unmatched_page(result: dict[str, Any], parsed_dir: Path) -> dict[str, Any]:
    details_path = _resolve_path(result.get("details_path"), parsed_dir)
    details_dir = details_path.parent if details_path is not None else parsed_dir
    pages = [
        _normalized_student_page(page, parsed_dir, details_dir)
        for page in result.get("pages", [])
        if _page_number(page) is not None
    ]
    for page in pages:
        page["origin"] = "unmatched_page"
    return {
        "source_index": int(result.get("source_index") or 0),
        "status": "needs_review",
        "page_index": result.get("page_index"),
        "identity_kind": result.get("identity_kind"),
        "identity": result.get("identity"),
        "pages": pages,
        "details_path": _relative_path(result.get("details_path"), parsed_dir),
        "source_path": str(result.get("source_path") or ""),
        "review_flags": list(result.get("review_flags", [])),
        "assigned_to_roll_no": None,
        "assigned_page": None,
        "decision_log": [],
    }


def _normalized_page_error(result: dict[str, Any], parsed_dir: Path) -> dict[str, Any]:
    return {
        "source_index": int(result.get("source_index") or 0),
        "status": "error",
        "error_type": result.get("error_type"),
        "details_path": _relative_path(result.get("details_path"), parsed_dir),
        "source_path": str(result.get("source_path") or ""),
        "review_flags": list(result.get("review_flags", [])),
        "decision_log": [],
    }


def _refresh_student_page_state(student: dict[str, Any], expected_pages: list[int]) -> None:
    selected: dict[int, dict[str, Any]] = {}
    for page in student.get("pages", []):
        page_no = page.get("page")
        if page_no is not None:
            selected[int(page_no)] = page
    for page in student.get("manual_pages", []):
        page_no = page.get("page")
        if page_no is not None:
            selected[int(page_no)] = page

    pages_found = sorted(selected)
    missing_pages = [page for page in expected_pages if page not in pages_found]
    source_indices = sorted(
        source_index
        for source_index in (page.get("source_index") for page in selected.values())
        if source_index is not None
    )
    student["pages_found"] = pages_found
    student["missing_pages"] = missing_pages
    student["source_indices"] = source_indices
    if missing_pages and student.get("status") not in {"verified", "rejected"}:
        student["status"] = "missing_pages"


def _student_by_roll(index: dict[str, Any], roll_no: str) -> dict[str, Any]:
    normalized = str(roll_no).strip()
    for student in index.get("students", []):
        if str(student.get("roll_no")) == normalized:
            return student
    raise ValueError(f"student roll number not found in verified index: {roll_no}")


def _unmatched_by_source(index: dict[str, Any], source_index: int) -> dict[str, Any]:
    for page in index.get("unmatched_pages", []):
        if int(page.get("source_index") or 0) == int(source_index):
            return page
    raise ValueError(f"unmatched source page not found: {source_index}")


def _decision(action: str, reviewer: str, note: str | None, **extra: Any) -> dict[str, Any]:
    payload = {
        "action": action,
        "reviewer": reviewer,
        "note": note or "",
        "created_at": _now(),
    }
    payload.update(extra)
    return payload


def _latest_decision(item: dict[str, Any]) -> dict[str, Any]:
    decisions = item.get("decision_log", [])
    if not decisions:
        return {}
    return dict(decisions[-1])


def _status_counts(index: dict[str, Any]) -> dict[str, Any]:
    students = {status: 0 for status in sorted(STUDENT_STATUSES)}
    for student in index.get("students", []):
        status = str(student.get("status"))
        students[status] = students.get(status, 0) + 1

    unmatched_pages = {status: 0 for status in sorted(UNMATCHED_PAGE_STATUSES)}
    for page in index.get("unmatched_pages", []):
        status = str(page.get("status"))
        unmatched_pages[status] = unmatched_pages.get(status, 0) + 1

    page_errors = {status: 0 for status in sorted(PAGE_ERROR_STATUSES)}
    for error in index.get("page_errors", []):
        status = str(error.get("status"))
        page_errors[status] = page_errors.get(status, 0) + 1

    return {
        "students": students,
        "unmatched_pages": unmatched_pages,
        "page_errors": page_errors,
        "eligible_for_email": sum(1 for student in index.get("students", []) if student.get("eligible_for_email")),
    }


def _review_rows(index: dict[str, Any]) -> list[dict[str, Any]]:
    expected_pages = int(index.get("expected_pages") or 0)
    rows: list[dict[str, Any]] = []
    for student in index.get("students", []):
        latest = _latest_decision(student)
        rows.append(
            {
                "item_type": "student",
                "status": student.get("status", ""),
                "roll_no": student.get("roll_no", ""),
                "program": student.get("program") or "",
                "student_name": student.get("student_name") or "",
                "student_email": student.get("student_email") or "",
                "pages_found": _join_values(student.get("pages_found", [])),
                "expected_pages": expected_pages,
                "missing_pages": _join_values(student.get("missing_pages", [])),
                "source_indices": _join_values(student.get("source_indices", [])),
                "parser_status": student.get("parser_status", ""),
                "eligible_for_email": str(bool(student.get("eligible_for_email"))).lower(),
                "sheet_pdf_path": student.get("sheet_pdf_path") or "",
                "verified_sheet_pdf_path": student.get("verified_sheet_pdf_path") or "",
                "details_path": student.get("details_path") or "",
                "review_flag_count": len(student.get("review_flags", [])),
                "review_flags": _join_flags(student.get("review_flags", [])),
                "last_decision_by": latest.get("reviewer", ""),
                "last_decision_at": latest.get("created_at", ""),
                "last_decision_note": latest.get("note", ""),
                "source_path": "",
                "assigned_to_roll_no": "",
                "error_type": "",
            }
        )

    for page in index.get("unmatched_pages", []):
        latest = _latest_decision(page)
        rows.append(
            {
                "item_type": "unmatched_page",
                "status": page.get("status", ""),
                "roll_no": "",
                "program": "",
                "student_name": "",
                "student_email": "",
                "pages_found": str(page.get("page_index") or ""),
                "expected_pages": expected_pages,
                "missing_pages": "",
                "source_indices": str(page.get("source_index") or ""),
                "parser_status": "",
                "eligible_for_email": "false",
                "sheet_pdf_path": "",
                "verified_sheet_pdf_path": "",
                "details_path": page.get("details_path") or "",
                "review_flag_count": len(page.get("review_flags", [])),
                "review_flags": _join_flags(page.get("review_flags", [])),
                "last_decision_by": latest.get("reviewer", ""),
                "last_decision_at": latest.get("created_at", ""),
                "last_decision_note": latest.get("note", ""),
                "source_path": page.get("source_path") or "",
                "assigned_to_roll_no": page.get("assigned_to_roll_no") or "",
                "error_type": "",
            }
        )

    for error in index.get("page_errors", []):
        latest = _latest_decision(error)
        rows.append(
            {
                "item_type": "page_error",
                "status": error.get("status", ""),
                "roll_no": "",
                "program": "",
                "student_name": "",
                "student_email": "",
                "pages_found": "",
                "expected_pages": expected_pages,
                "missing_pages": "",
                "source_indices": str(error.get("source_index") or ""),
                "parser_status": "",
                "eligible_for_email": "false",
                "sheet_pdf_path": "",
                "verified_sheet_pdf_path": "",
                "details_path": error.get("details_path") or "",
                "review_flag_count": len(error.get("review_flags", [])),
                "review_flags": _join_flags(error.get("review_flags", [])),
                "last_decision_by": latest.get("reviewer", ""),
                "last_decision_at": latest.get("created_at", ""),
                "last_decision_note": latest.get("note", ""),
                "source_path": error.get("source_path") or "",
                "assigned_to_roll_no": "",
                "error_type": error.get("error_type") or "",
            }
        )
    return rows


def _write_verification_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=VERIFICATION_REPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _html_link(value: str | None) -> str:
    if not value:
        return ""
    escaped = html.escape(value)
    href = html.escape(Path(value).as_posix(), quote=True)
    return f'<a href="{href}">{escaped}</a>'


def _status_class(status: str) -> str:
    if status == "verified":
        return "verified"
    if status in {"pending_verification", "needs_review", "missing_pages"}:
        return "review"
    if status in {"rejected", "error"}:
        return "error"
    return "neutral"


def _render_table(title: str, rows: list[dict[str, Any]], columns: list[str]) -> str:
    headers = "".join(f"<th>{html.escape(column.replace('_', ' ').title())}</th>" for column in columns)
    body = []
    for row in rows:
        cells = []
        for column in columns:
            value = str(row.get(column, ""))
            if column in {"sheet_pdf_path", "verified_sheet_pdf_path", "details_path"}:
                content = _html_link(value)
            elif column == "status":
                content = f'<span class="badge {_status_class(value)}">{html.escape(value)}</span>'
            else:
                content = html.escape(value)
            cells.append(f"<td>{content}</td>")
        body.append(f"<tr>{''.join(cells)}</tr>")
    if not body:
        body.append(f'<tr><td class="empty" colspan="{len(columns)}">None</td></tr>')
    return f"""
      <section>
        <h2>{html.escape(title)}</h2>
        <table>
          <thead><tr>{headers}</tr></thead>
          <tbody>{''.join(body)}</tbody>
        </table>
      </section>
    """


def _write_verification_html(path: Path, index: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    counts = index.get("status_counts", {})
    student_counts = counts.get("students", {})
    unmatched_counts = counts.get("unmatched_pages", {})
    error_counts = counts.get("page_errors", {})
    metrics = [
        ("Verified", student_counts.get("verified", 0)),
        ("Pending", student_counts.get("pending_verification", 0)),
        ("Needs Review", student_counts.get("needs_review", 0)),
        ("Missing Pages", student_counts.get("missing_pages", 0)),
        ("Rejected", student_counts.get("rejected", 0)),
        ("Email Eligible", counts.get("eligible_for_email", 0)),
        ("Unmatched", unmatched_counts.get("needs_review", 0)),
        ("Errors", error_counts.get("error", 0)),
    ]
    metric_html = "".join(
        f"<div class=\"metric\"><span>{html.escape(label)}</span><strong>{html.escape(str(value))}</strong></div>"
        for label, value in metrics
    )
    student_rows = [row for row in rows if row["item_type"] == "student"]
    unmatched_rows = [row for row in rows if row["item_type"] == "unmatched_page"]
    error_rows = [row for row in rows if row["item_type"] == "page_error"]
    student_columns = [
        "status",
        "roll_no",
        "program",
        "student_name",
        "pages_found",
        "missing_pages",
        "eligible_for_email",
        "verified_sheet_pdf_path",
        "sheet_pdf_path",
        "review_flag_count",
        "review_flags",
        "last_decision_by",
        "last_decision_note",
    ]
    unmatched_columns = [
        "status",
        "pages_found",
        "source_indices",
        "assigned_to_roll_no",
        "review_flags",
        "last_decision_by",
        "last_decision_note",
        "details_path",
    ]
    error_columns = [
        "status",
        "source_indices",
        "error_type",
        "review_flags",
        "last_decision_by",
        "last_decision_note",
        "details_path",
    ]
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(index.get("exam_id", ""))} Verification Report</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: Arial, Helvetica, sans-serif;
      background: #f7f8fa;
      color: #172033;
    }}
    body {{
      margin: 0;
      padding: 24px;
    }}
    main {{
      max-width: 1280px;
      margin: 0 auto;
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 24px;
      line-height: 1.2;
    }}
    h2 {{
      margin: 28px 0 12px;
      font-size: 17px;
    }}
    .subtitle {{
      margin: 0 0 18px;
      color: #5c667a;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
      margin: 18px 0 22px;
    }}
    .metric {{
      border: 1px solid #d9dde6;
      border-radius: 6px;
      background: #ffffff;
      padding: 10px 12px;
    }}
    .metric span {{
      display: block;
      color: #667085;
      font-size: 12px;
      margin-bottom: 5px;
    }}
    .metric strong {{
      display: block;
      font-size: 16px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: #ffffff;
      border: 1px solid #d9dde6;
      font-size: 13px;
    }}
    th, td {{
      border-bottom: 1px solid #edf0f5;
      padding: 8px 9px;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: #f0f3f8;
      color: #344054;
      font-size: 12px;
      white-space: nowrap;
    }}
    a {{
      color: #1f5fbf;
      text-decoration: none;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    .badge {{
      display: inline-block;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .verified {{
      background: #e8f5ee;
      color: #166534;
    }}
    .review {{
      background: #fff7df;
      color: #8a4b08;
    }}
    .error {{
      background: #feeceb;
      color: #b42318;
    }}
    .neutral {{
      background: #eef2f7;
      color: #344054;
    }}
    .empty {{
      color: #667085;
      text-align: center;
      padding: 18px;
    }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(index.get("exam_id", ""))} Verification Report</h1>
    <p class="subtitle">Human decisions written to {VERIFIED_INDEX_NAME}; raw parser output is unchanged.</p>
    <div class="metrics">{metric_html}</div>
    {_render_table("Students", student_rows, student_columns)}
    {_render_table("Unmatched Pages", unmatched_rows, unmatched_columns)}
    {_render_table("Page Errors", error_rows, error_columns)}
  </main>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")


def _write_reports(parsed_dir: Path, index: dict[str, Any]) -> dict[str, str]:
    rows = _review_rows(index)
    csv_path = parsed_dir / VERIFICATION_CSV_NAME
    html_path = parsed_dir / VERIFICATION_HTML_NAME
    _write_verification_csv(csv_path, rows)
    _write_verification_html(html_path, index, rows)
    return {
        "csv": _json_path(csv_path, parsed_dir),
        "html": _json_path(html_path, parsed_dir),
    }


def _write_verified_index(parsed_dir: Path, index: dict[str, Any]) -> Path:
    expected_pages = list(range(1, int(index.get("expected_pages") or 0) + 1))
    for student in index.get("students", []):
        _refresh_student_page_state(student, expected_pages)
        student["eligible_for_email"] = student.get("status") == "verified" and bool(
            student.get("verified_sheet_pdf_path")
        ) and not student.get("missing_pages")
    index["status_counts"] = _status_counts(index)
    index["updated_at"] = _now()
    index["reports"] = _write_reports(parsed_dir, index)
    path = parsed_dir / VERIFIED_INDEX_NAME
    _write_json(path, index)
    return path


def initialize_verification_index(
    parsed_dir: str | Path,
    *,
    auto_verify_ready: bool = False,
    reviewer: str | None = None,
    note: str | None = None,
    force: bool = False,
) -> tuple[dict[str, Any], Path]:
    parse_index_path = _parse_index_path(parsed_dir)
    parsed_root = _parsed_dir_from_index(parse_index_path)
    output_path = parsed_root / VERIFIED_INDEX_NAME
    if output_path.exists() and not force:
        index = _load_json(output_path)
        _write_verified_index(parsed_root, index)
        return index, output_path

    parse_index = _load_json(parse_index_path)
    expected_pages = list(range(1, int(parse_index.get("expected_pages") or 0) + 1))
    if not expected_pages:
        page_numbers = []
        for student in parse_index.get("students", []):
            page_numbers.extend(_page_number(page) for page in student.get("pages", []))
        for page in parse_index.get("unmatched_pages", []):
            page_numbers.append(page.get("page_index"))
        max_page = max((int(page) for page in page_numbers if page), default=1)
        expected_pages = list(range(1, max_page + 1))

    index = {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "exam_id": parse_index.get("exam_id"),
        "mode": "verification_review",
        "source_parse_index_path": _json_path(parse_index_path, parsed_root),
        "parser_grouping_mode": parse_index.get("grouping_mode"),
        "detected_page_sequence": parse_index.get("detected_page_sequence", []),
        "expected_pages": len(expected_pages),
        "roster_reconciliation": parse_index.get("roster_reconciliation"),
        "created_at": _now(),
        "updated_at": None,
        "students": [
            _student_from_parse_index(
                student,
                parsed_root,
                expected_pages,
                auto_verify_ready=auto_verify_ready,
                reviewer=reviewer,
                note=note,
            )
            for student in parse_index.get("students", [])
        ],
        "unmatched_pages": [
            _normalized_unmatched_page(page, parsed_root) for page in parse_index.get("unmatched_pages", [])
        ],
        "page_errors": [_normalized_page_error(error, parsed_root) for error in parse_index.get("page_errors", [])],
        "status_counts": {},
        "reports": {},
    }
    for student in index["students"]:
        if student["status"] == "verified":
            student["verified_sheet_pdf_path"] = _create_verified_sheet_pdf(parsed_root, student)
    _write_verified_index(parsed_root, index)
    return index, output_path


def load_or_initialize_verified_index(parsed_dir: str | Path) -> tuple[dict[str, Any], Path]:
    path = _verified_index_path(parsed_dir)
    if path.exists():
        return _load_json(path), path
    return initialize_verification_index(parsed_dir)


def selected_student_pages(student: dict[str, Any]) -> list[dict[str, Any]]:
    """Current page selection, including manual replacements, in template order."""
    selected: dict[int, dict[str, Any]] = {}
    for page in student.get("pages", []):
        page_no = page.get("page")
        if page_no is not None:
            selected[int(page_no)] = page
    for page in student.get("manual_pages", []):
        page_no = page.get("page")
        if page_no is not None:
            selected[int(page_no)] = page
    return [selected[page_no] for page_no in sorted(selected)]


def _create_verified_sheet_pdf(parsed_dir: Path, student: dict[str, Any]) -> str:
    output_dir = parsed_dir / "verified" / "students" / str(student["roll_no"])
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "sheet.pdf"

    if not student.get("manual_pages") and student.get("sheet_pdf_path"):
        source_pdf = _resolve_path(student.get("sheet_pdf_path"), parsed_dir)
        if source_pdf is not None and source_pdf.exists():
            shutil.copyfile(source_pdf, output_path)
            return _json_path(output_path, parsed_dir)

    images: list[Image.Image] = []
    for page in selected_student_pages(student):
        image_path = _resolve_path(page.get("canonical_image_path"), parsed_dir)
        if image_path is None or not image_path.exists():
            raise FileNotFoundError(f"canonical page image not found for roll {student['roll_no']}: {image_path}")
        images.append(Image.open(image_path).convert("RGB"))
    if not images:
        raise ValueError(f"student {student['roll_no']} has no pages to verify")
    first, rest = images[0], images[1:]
    first.save(output_path, save_all=True, append_images=rest, resolution=300.0)
    for image in images:
        image.close()
    return _json_path(output_path, parsed_dir)


def verify_student(
    parsed_dir: str | Path,
    roll_no: str,
    *,
    reviewer: str,
    note: str | None = None,
    allow_missing: bool = False,
) -> tuple[dict[str, Any], Path]:
    index, _path = load_or_initialize_verified_index(parsed_dir)
    parsed_root = _parsed_dir_from_index(_parse_index_path(parsed_dir))
    student = _student_by_roll(index, roll_no)
    expected_pages = list(range(1, int(index.get("expected_pages") or 0) + 1))
    _refresh_student_page_state(student, expected_pages)
    if student.get("missing_pages") and not allow_missing:
        missing = _join_values(student["missing_pages"])
        raise ValueError(f"student {roll_no} is missing page(s) {missing}; use assign-page first or --allow-missing")

    student["status"] = "verified"
    student["verified_sheet_pdf_path"] = _create_verified_sheet_pdf(parsed_root, student)
    student["decision_log"].append(
        _decision(
            "verify_student",
            reviewer,
            note or "Human verified this student sheet.",
            cleared_review_flags=list(student.get("review_flags", [])),
        )
    )
    path = _write_verified_index(parsed_root, index)
    return index, path


def reject_student(
    parsed_dir: str | Path,
    roll_no: str,
    *,
    reviewer: str,
    reason: str,
) -> tuple[dict[str, Any], Path]:
    index, _path = load_or_initialize_verified_index(parsed_dir)
    parsed_root = _parsed_dir_from_index(_parse_index_path(parsed_dir))
    student = _student_by_roll(index, roll_no)
    student["status"] = "rejected"
    student["eligible_for_email"] = False
    student["decision_log"].append(_decision("reject_student", reviewer, reason))
    path = _write_verified_index(parsed_root, index)
    return index, path


def hold_student(
    parsed_dir: str | Path,
    roll_no: str,
    *,
    reviewer: str,
    reason: str,
) -> tuple[dict[str, Any], Path]:
    index, _path = load_or_initialize_verified_index(parsed_dir)
    parsed_root = _parsed_dir_from_index(_parse_index_path(parsed_dir))
    student = _student_by_roll(index, roll_no)
    student["status"] = "needs_review"
    student["eligible_for_email"] = False
    student["decision_log"].append(_decision("hold_student", reviewer, reason))
    path = _write_verified_index(parsed_root, index)
    return index, path


def assign_unmatched_page(
    parsed_dir: str | Path,
    source_index: int,
    roll_no: str,
    *,
    page_index: int | None = None,
    reviewer: str,
    note: str | None = None,
) -> tuple[dict[str, Any], Path]:
    index, _path = load_or_initialize_verified_index(parsed_dir)
    parsed_root = _parsed_dir_from_index(_parse_index_path(parsed_dir))
    student = _student_by_roll(index, roll_no)
    unmatched = _unmatched_by_source(index, source_index)
    if unmatched.get("status") == "ignored":
        raise ValueError(f"source page {source_index} is ignored; cannot assign it")

    chosen_page = int(page_index or unmatched.get("page_index") or 0)
    if chosen_page <= 0:
        raise ValueError(f"page index could not be determined for source page {source_index}")

    candidate_page = None
    for page in unmatched.get("pages", []):
        if int(page.get("page") or 0) == chosen_page:
            candidate_page = dict(page)
            break
    if candidate_page is None:
        raise ValueError(f"source page {source_index} does not contain OMR page {chosen_page}")

    candidate_page.update(
        {
            "page": chosen_page,
            "origin": "manual_assignment",
            "assigned_from_source_index": int(source_index),
            "assigned_at": _now(),
            "assigned_by": reviewer,
            "assignment_note": note or "",
        }
    )
    student.setdefault("manual_pages", [])
    student["manual_pages"] = [
        page for page in student["manual_pages"] if int(page.get("source_index") or 0) != int(source_index)
    ]
    student["manual_pages"].append(candidate_page)
    student["status"] = "needs_review"
    student["verified_sheet_pdf_path"] = None
    student["eligible_for_email"] = False
    student["decision_log"].append(
        _decision(
            "assign_unmatched_page",
            reviewer,
            note or f"Assigned source page {source_index} as page {chosen_page}.",
            source_index=int(source_index),
            page_index=chosen_page,
        )
    )

    unmatched["status"] = "assigned"
    unmatched["assigned_to_roll_no"] = str(roll_no)
    unmatched["assigned_page"] = chosen_page
    unmatched["decision_log"].append(
        _decision(
            "assign_unmatched_page",
            reviewer,
            note or f"Assigned to roll {roll_no} as page {chosen_page}.",
            roll_no=str(roll_no),
            page_index=chosen_page,
        )
    )

    expected_pages = list(range(1, int(index.get("expected_pages") or 0) + 1))
    _refresh_student_page_state(student, expected_pages)
    path = _write_verified_index(parsed_root, index)
    return index, path


def ignore_unmatched_page(
    parsed_dir: str | Path,
    source_index: int,
    *,
    reviewer: str,
    reason: str,
) -> tuple[dict[str, Any], Path]:
    index, _path = load_or_initialize_verified_index(parsed_dir)
    parsed_root = _parsed_dir_from_index(_parse_index_path(parsed_dir))
    unmatched = _unmatched_by_source(index, source_index)
    unmatched["status"] = "ignored"
    unmatched["decision_log"].append(_decision("ignore_unmatched_page", reviewer, reason))
    path = _write_verified_index(parsed_root, index)
    return index, path


def summarize(index: dict[str, Any]) -> str:
    counts = index.get("status_counts") or _status_counts(index)
    students = counts.get("students", {})
    unmatched = counts.get("unmatched_pages", {})
    errors = counts.get("page_errors", {})
    return (
        f"exam_id={index.get('exam_id')} "
        f"verified={students.get('verified', 0)} "
        f"pending_verification={students.get('pending_verification', 0)} "
        f"needs_review={students.get('needs_review', 0)} "
        f"missing_pages={students.get('missing_pages', 0)} "
        f"rejected={students.get('rejected', 0)} "
        f"email_eligible={counts.get('eligible_for_email', 0)} "
        f"unmatched_needs_review={unmatched.get('needs_review', 0)} "
        f"page_errors={errors.get('error', 0)}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smartomr-review",
        description="Create and update human verification artifacts from a SmartOMR batch parse.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Create or refresh verified_index.json from parse_index.json")
    init.add_argument("--parsed-dir", required=True, type=Path)
    init.add_argument("--force", action="store_true", help="Rebuild from parse_index.json even if verified_index exists")
    init.add_argument("--auto-verify-ready", action="store_true", help="Mark parser-ready students verified")
    init.add_argument("--reviewer", default=None)
    init.add_argument("--note", default=None)

    summary = subparsers.add_parser("summary", help="Print verification status counts")
    summary.add_argument("--parsed-dir", required=True, type=Path)

    verify = subparsers.add_parser("verify", help="Mark a student sheet as human-verified")
    verify.add_argument("--parsed-dir", required=True, type=Path)
    verify.add_argument("--roll-no", required=True)
    verify.add_argument("--reviewer", required=True)
    verify.add_argument("--note", default=None)
    verify.add_argument("--allow-missing", action="store_true")

    reject = subparsers.add_parser("reject", help="Reject a student grouping")
    reject.add_argument("--parsed-dir", required=True, type=Path)
    reject.add_argument("--roll-no", required=True)
    reject.add_argument("--reviewer", required=True)
    reject.add_argument("--reason", required=True)

    hold = subparsers.add_parser("hold", help="Keep a student in manual review")
    hold.add_argument("--parsed-dir", required=True, type=Path)
    hold.add_argument("--roll-no", required=True)
    hold.add_argument("--reviewer", required=True)
    hold.add_argument("--reason", required=True)

    assign = subparsers.add_parser("assign-page", help="Attach an unmatched source page to a student")
    assign.add_argument("--parsed-dir", required=True, type=Path)
    assign.add_argument("--source-index", required=True, type=int)
    assign.add_argument("--roll-no", required=True)
    assign.add_argument("--page", dest="page_index", default=None, type=int)
    assign.add_argument("--reviewer", required=True)
    assign.add_argument("--note", default=None)

    ignore = subparsers.add_parser("ignore-page", help="Mark an unmatched source page as intentionally ignored")
    ignore.add_argument("--parsed-dir", required=True, type=Path)
    ignore.add_argument("--source-index", required=True, type=int)
    ignore.add_argument("--reviewer", required=True)
    ignore.add_argument("--reason", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            index, path = initialize_verification_index(
                args.parsed_dir,
                auto_verify_ready=args.auto_verify_ready,
                reviewer=args.reviewer,
                note=args.note,
                force=args.force,
            )
        elif args.command == "summary":
            index, path = load_or_initialize_verified_index(args.parsed_dir)
            parsed_root = _parsed_dir_from_index(_parse_index_path(args.parsed_dir))
            path = _write_verified_index(parsed_root, index)
        elif args.command == "verify":
            index, path = verify_student(
                args.parsed_dir,
                args.roll_no,
                reviewer=args.reviewer,
                note=args.note,
                allow_missing=args.allow_missing,
            )
        elif args.command == "reject":
            index, path = reject_student(
                args.parsed_dir,
                args.roll_no,
                reviewer=args.reviewer,
                reason=args.reason,
            )
        elif args.command == "hold":
            index, path = hold_student(
                args.parsed_dir,
                args.roll_no,
                reviewer=args.reviewer,
                reason=args.reason,
            )
        elif args.command == "assign-page":
            index, path = assign_unmatched_page(
                args.parsed_dir,
                args.source_index,
                args.roll_no,
                page_index=args.page_index,
                reviewer=args.reviewer,
                note=args.note,
            )
        elif args.command == "ignore-page":
            index, path = ignore_unmatched_page(
                args.parsed_dir,
                args.source_index,
                reviewer=args.reviewer,
                reason=args.reason,
            )
        else:  # pragma: no cover - argparse enforces the command set
            raise ValueError(f"unknown command: {args.command}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Wrote {path}")
    print(f"verification_report_html={path.parent / VERIFICATION_HTML_NAME}")
    print(f"verification_report_csv={path.parent / VERIFICATION_CSV_NAME}")
    print(summarize(index))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
