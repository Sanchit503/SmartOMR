"""Written-answer grading packet and manual marks import workflow.

This is the production-safe baseline for written answers: export verified
written-answer crops for human grading, import validated marks, and write
structured score artifacts. Automated LLM grading can later produce the same
grade records without changing downstream reporting.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omr.workflows.parse import _json_path
from omr.workflows.review import VERIFIED_INDEX_NAME


WRITTEN_SCHEMA_VERSION = 1
WRITTEN_DIR_NAME = "written_grading"
WRITTEN_PACKET_JSON = "written_packet.json"
WRITTEN_ANSWER_INDEX_CSV = "written_answer_index.csv"
MANUAL_MARKS_TEMPLATE_CSV = "manual_marks_template.csv"
WRITTEN_REVIEW_HTML = "written_review.html"
WRITTEN_GRADES_JSON = "written_grades.json"
WRITTEN_GRADES_REPORT_CSV = "written_grades_report.csv"
FINAL_SCORES_CSV = "final_scores.csv"

ANSWER_INDEX_COLUMNS = [
    "roll_no",
    "student_status",
    "student_name",
    "student_email",
    "q_no",
    "page",
    "max_marks",
    "lines",
    "crop_path",
    "ocr_text",
    "ocr_confidence",
    "ocr_provider",
    "ocr_review_flags",
    "verified_sheet_pdf_path",
]
MANUAL_MARKS_COLUMNS = [
    "roll_no",
    "q_no",
    "max_marks",
    "marks_awarded",
    "needs_human_review",
    "grader_comment",
    "crop_path",
    "student_status",
    "student_name",
]
WRITTEN_GRADES_COLUMNS = [
    "roll_no",
    "q_no",
    "status",
    "marks_awarded",
    "max_marks",
    "needs_human_review",
    "method",
    "grader",
    "grader_comment",
    "crop_path",
    "student_status",
]
FINAL_SCORES_COLUMNS = [
    "roll_no",
    "student_status",
    "student_name",
    "student_email",
    "written_status",
    "written_score",
    "written_total",
    "mcq_score",
    "mcq_total",
    "total_score",
    "total_marks",
    "grading_complete",
    "eligible_for_email",
    "verified_sheet_pdf_path",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _parsed_root(parsed_dir: str | Path) -> Path:
    path = Path(parsed_dir)
    if path.name in {VERIFIED_INDEX_NAME, WRITTEN_PACKET_JSON, WRITTEN_GRADES_JSON}:
        return path.parent.parent if path.parent.name == WRITTEN_DIR_NAME else path.parent
    if path.name == WRITTEN_DIR_NAME:
        return path.parent
    return path


def _written_dir(parsed_root: Path) -> Path:
    return parsed_root / WRITTEN_DIR_NAME


def _verified_index_path(parsed_dir: str | Path) -> Path:
    path = Path(parsed_dir)
    if path.name == VERIFIED_INDEX_NAME:
        return path
    if path.name == WRITTEN_DIR_NAME:
        return path.parent / VERIFIED_INDEX_NAME
    return path / VERIFIED_INDEX_NAME


def _packet_path(parsed_root: Path) -> Path:
    return _written_dir(parsed_root) / WRITTEN_PACKET_JSON


def _grades_path(parsed_root: Path) -> Path:
    return _written_dir(parsed_root) / WRITTEN_GRADES_JSON


def _resolve_path(value: str | Path | None, parsed_root: Path, base_dir: Path | None = None) -> Path | None:
    if value is None or str(value) == "":
        return None
    path = Path(value)
    if path.is_absolute():
        return path

    candidates: list[Path] = []
    if base_dir is not None:
        candidates.append(base_dir / path)
    candidates.append(parsed_root / path)
    candidates.append(Path.cwd() / path)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _relative_path(value: str | Path | None, parsed_root: Path, base_dir: Path | None = None) -> str | None:
    path = _resolve_path(value, parsed_root, base_dir)
    if path is None:
        return None
    return _json_path(path, parsed_root)


def _format_number(value: Any) -> str:
    if value is None:
        return ""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:g}"


def _join_flags(flags: list[Any]) -> str:
    return " | ".join(str(flag).replace("\r", " ").replace("\n", " ") for flag in flags)


def _parse_bool(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y", "review", "needs_review"}:
        return True
    if text in {"", "0", "false", "no", "n"}:
        return False
    raise ValueError(f"invalid boolean value {value!r}; use yes/no")


def _load_verified_index(parsed_root: Path) -> dict[str, Any]:
    path = _verified_index_path(parsed_root)
    if not path.exists():
        raise FileNotFoundError(f"{VERIFIED_INDEX_NAME} not found; run `python -m omr.workflows.review init` first")
    return _load_json(path)


def _load_student_details(student: dict[str, Any], parsed_root: Path) -> tuple[dict[str, Any], Path]:
    details_path = _resolve_path(student.get("details_path"), parsed_root)
    if details_path is None or not details_path.exists():
        raise FileNotFoundError(f"student details not found for roll {student.get('roll_no')}: {details_path}")
    return _load_json(details_path), details_path.parent


def _student_is_included(student: dict[str, Any], include_unverified: bool) -> bool:
    if include_unverified:
        return student.get("status") != "rejected"
    return student.get("status") == "verified"


def _ocr_payload(written: dict[str, Any]) -> dict[str, Any]:
    ocr = written.get("ocr")
    if not isinstance(ocr, dict):
        return {"text": "", "confidence": "", "provider": "", "review_flags": []}
    return {
        "text": ocr.get("text") or "",
        "confidence": ocr.get("confidence") or "",
        "provider": ocr.get("provider") or "",
        "review_flags": list(ocr.get("review_flags", [])),
    }


def _answer_rows_from_student(
    student: dict[str, Any],
    parsed_root: Path,
    include_unverified: bool,
) -> list[dict[str, Any]]:
    if not _student_is_included(student, include_unverified):
        return []

    details, student_dir = _load_student_details(student, parsed_root)
    detail_student = dict(details.get("student", {}))
    roll_no = str(student.get("roll_no") or detail_student.get("roll_no") or "")
    answers = []
    for written in sorted(details.get("written_responses", []), key=lambda item: int(item["q_no"])):
        ocr = _ocr_payload(written)
        answers.append(
            {
                "roll_no": roll_no,
                "student_status": student.get("status", ""),
                "student_name": student.get("student_name") or detail_student.get("name") or "",
                "student_email": student.get("student_email") or detail_student.get("email") or "",
                "eligible_for_email": bool(student.get("eligible_for_email")),
                "q_no": int(written["q_no"]),
                "page": int(written.get("page", 1)),
                "max_marks": float(written["max_marks"]),
                "lines": int(written.get("lines", 1)),
                "crop_path": _relative_path(written.get("crop_path"), parsed_root, student_dir),
                "ocr_text": ocr["text"],
                "ocr_confidence": ocr["confidence"],
                "ocr_provider": ocr["provider"],
                "ocr_review_flags": ocr["review_flags"],
                "verified_sheet_pdf_path": student.get("verified_sheet_pdf_path") or "",
                "mcq_score": student.get("mcq_score"),
                "mcq_total": student.get("mcq_total"),
            }
        )
    return answers


def _student_summaries(index: dict[str, Any], included_rolls: set[str]) -> list[dict[str, Any]]:
    summaries = []
    for student in index.get("students", []):
        roll_no = str(student.get("roll_no") or "")
        if roll_no not in included_rolls:
            continue
        summaries.append(
            {
                "roll_no": roll_no,
                "student_status": student.get("status", ""),
                "student_name": student.get("student_name") or "",
                "student_email": student.get("student_email") or "",
                "eligible_for_email": bool(student.get("eligible_for_email")),
                "verified_sheet_pdf_path": student.get("verified_sheet_pdf_path") or "",
                "mcq_score": student.get("mcq_score"),
                "mcq_total": student.get("mcq_total"),
            }
        )
    return summaries


def _write_answer_index_csv(path: Path, answers: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ANSWER_INDEX_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for answer in answers:
            row = dict(answer)
            row["max_marks"] = _format_number(answer.get("max_marks"))
            row["ocr_review_flags"] = _join_flags(answer.get("ocr_review_flags", []))
            writer.writerow(row)


def _write_manual_template_csv(path: Path, answers: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANUAL_MARKS_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for answer in answers:
            writer.writerow(
                {
                    "roll_no": answer["roll_no"],
                    "q_no": answer["q_no"],
                    "max_marks": _format_number(answer["max_marks"]),
                    "marks_awarded": "",
                    "needs_human_review": "",
                    "grader_comment": "",
                    "crop_path": answer.get("crop_path") or "",
                    "student_status": answer.get("student_status") or "",
                    "student_name": answer.get("student_name") or "",
                }
            )


def _root_relative_href(value: str | None) -> str:
    if not value:
        return ""
    return ("../" + Path(value).as_posix()).replace("//", "/")


def _html_link(value: str | None) -> str:
    if not value:
        return ""
    escaped = html.escape(value)
    return f'<a href="{html.escape(_root_relative_href(value), quote=True)}">{escaped}</a>'


def _write_written_review_html(
    path: Path,
    packet: dict[str, Any],
    grades: dict[tuple[str, int], dict[str, Any]] | None = None,
) -> None:
    answers = packet.get("answers", [])
    rows = []
    for answer in answers:
        key = (str(answer["roll_no"]), int(answer["q_no"]))
        grade = grades.get(key, {}) if grades else {}
        crop_path = answer.get("crop_path") or ""
        img = ""
        if crop_path:
            img = f'<img src="{html.escape(_root_relative_href(crop_path), quote=True)}" alt="Q{answer["q_no"]} crop">'
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(answer['roll_no']))}</td>"
            f"<td>Q{html.escape(str(answer['q_no']))}</td>"
            f"<td>{html.escape(_format_number(answer['max_marks']))}</td>"
            f"<td>{html.escape(str(grade.get('status', 'pending')))}</td>"
            f"<td>{html.escape(_format_number(grade.get('marks_awarded')))}</td>"
            f"<td>{html.escape(str(grade.get('grader_comment') or ''))}</td>"
            f"<td>{_html_link(crop_path)}{img}</td>"
            f"<td>{html.escape(str(answer.get('ocr_text') or ''))}</td>"
            "</tr>"
        )
    if not rows:
        rows.append('<tr><td class="empty" colspan="8">No written answers exported.</td></tr>')
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(str(packet.get("exam_id") or ""))} Written Grading</title>
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
    }}
    .subtitle {{
      margin: 0 0 18px;
      color: #5c667a;
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
    img {{
      display: block;
      max-width: 420px;
      max-height: 140px;
      margin-top: 6px;
      border: 1px solid #d9dde6;
      background: #ffffff;
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
    <h1>{html.escape(str(packet.get("exam_id") or ""))} Written Grading</h1>
    <p class="subtitle">Use the manual marks template CSV for marks entry; this page links each answer crop.</p>
    <table>
      <thead>
        <tr>
          <th>Roll No</th>
          <th>Question</th>
          <th>Max</th>
          <th>Status</th>
          <th>Marks</th>
          <th>Comment</th>
          <th>Crop</th>
          <th>OCR Text</th>
        </tr>
      </thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </main>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")


def export_written_grading_packet(
    parsed_dir: str | Path,
    *,
    include_unverified: bool = False,
) -> tuple[dict[str, Any], Path]:
    parsed_root = _parsed_root(parsed_dir)
    verified_index = _load_verified_index(parsed_root)
    answers: list[dict[str, Any]] = []
    for student in verified_index.get("students", []):
        answers.extend(_answer_rows_from_student(student, parsed_root, include_unverified))
    answers.sort(key=lambda item: (str(item["roll_no"]), int(item["q_no"])))

    included_rolls = {str(answer["roll_no"]) for answer in answers}
    packet = {
        "schema_version": WRITTEN_SCHEMA_VERSION,
        "exam_id": verified_index.get("exam_id"),
        "mode": "written_grading_packet",
        "source_verified_index_path": VERIFIED_INDEX_NAME,
        "include_unverified": include_unverified,
        "created_at": _now(),
        "updated_at": _now(),
        "students": _student_summaries(verified_index, included_rolls),
        "answers": answers,
        "reports": {
            "answer_index_csv": WRITTEN_ANSWER_INDEX_CSV,
            "manual_marks_template_csv": MANUAL_MARKS_TEMPLATE_CSV,
            "review_html": WRITTEN_REVIEW_HTML,
        },
    }

    output_dir = _written_dir(parsed_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    packet_path = output_dir / WRITTEN_PACKET_JSON
    _write_json(packet_path, packet)
    _write_answer_index_csv(output_dir / WRITTEN_ANSWER_INDEX_CSV, answers)
    _write_manual_template_csv(output_dir / MANUAL_MARKS_TEMPLATE_CSV, answers)
    _write_written_review_html(output_dir / WRITTEN_REVIEW_HTML, packet)
    return packet, packet_path


def _load_packet(parsed_root: Path) -> dict[str, Any]:
    path = _packet_path(parsed_root)
    if not path.exists():
        raise FileNotFoundError(f"{WRITTEN_PACKET_JSON} not found; run `python -m omr.workflows.written export` first")
    return _load_json(path)


def _pending_grade(answer: dict[str, Any]) -> dict[str, Any]:
    return {
        "roll_no": str(answer["roll_no"]),
        "q_no": int(answer["q_no"]),
        "status": "pending",
        "marks_awarded": None,
        "max_marks": float(answer["max_marks"]),
        "needs_human_review": False,
        "method": "manual",
        "grader": "",
        "grader_comment": "",
        "crop_path": answer.get("crop_path") or "",
        "student_status": answer.get("student_status") or "",
        "updated_at": None,
    }


def _read_marks_rows(path: Path) -> list[tuple[int, dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"roll_no", "q_no", "marks_awarded"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} is missing required column(s): {', '.join(sorted(missing))}")
        return [(row_no, row) for row_no, row in enumerate(reader, start=2)]


def _apply_manual_mark(
    grade: dict[str, Any],
    row: dict[str, str],
    *,
    row_no: int,
    grader: str,
) -> None:
    raw_marks = str(row.get("marks_awarded", "")).strip()
    comment = str(row.get("grader_comment") or row.get("comment") or "").strip()
    needs_review = _parse_bool(row.get("needs_human_review"))

    if not raw_marks:
        grade.update(
            {
                "status": "pending",
                "marks_awarded": None,
                "needs_human_review": needs_review,
                "grader": grader,
                "grader_comment": comment,
                "updated_at": _now(),
            }
        )
        return

    try:
        marks = float(raw_marks)
    except ValueError as exc:
        raise ValueError(f"row {row_no}: invalid marks_awarded {raw_marks!r}") from exc
    max_marks = float(grade["max_marks"])
    if marks < 0 or marks > max_marks:
        raise ValueError(f"row {row_no}: marks_awarded {marks:g} must be between 0 and {max_marks:g}")

    grade.update(
        {
            "status": "needs_review" if needs_review else "graded",
            "marks_awarded": marks,
            "needs_human_review": needs_review,
            "grader": grader,
            "grader_comment": comment,
            "updated_at": _now(),
        }
    )


def _student_aggregates(packet: dict[str, Any], grades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    student_info = {str(student["roll_no"]): student for student in packet.get("students", [])}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for grade in grades:
        grouped.setdefault(str(grade["roll_no"]), []).append(grade)

    rows = []
    for roll_no, items in sorted(grouped.items()):
        student = student_info.get(roll_no, {})
        written_total = sum(float(item["max_marks"]) for item in items)
        written_score = sum(float(item["marks_awarded"]) for item in items if item["marks_awarded"] is not None)
        pending = sum(1 for item in items if item["status"] == "pending")
        review = sum(1 for item in items if item["status"] == "needs_review")
        if pending:
            written_status = "pending"
        elif review:
            written_status = "needs_review"
        else:
            written_status = "complete"

        mcq_score = student.get("mcq_score")
        mcq_total = student.get("mcq_total")
        grading_complete = written_status == "complete" and mcq_score is not None and mcq_total is not None
        total_score = float(mcq_score) + written_score if grading_complete else None
        total_marks = float(mcq_total) + written_total if mcq_total is not None else None
        rows.append(
            {
                "roll_no": roll_no,
                "student_status": student.get("student_status") or "",
                "student_name": student.get("student_name") or "",
                "student_email": student.get("student_email") or "",
                "written_status": written_status,
                "written_score": written_score,
                "written_total": written_total,
                "mcq_score": mcq_score,
                "mcq_total": mcq_total,
                "total_score": total_score,
                "total_marks": total_marks,
                "grading_complete": grading_complete,
                "eligible_for_email": bool(student.get("eligible_for_email")),
                "verified_sheet_pdf_path": student.get("verified_sheet_pdf_path") or "",
                "pending_answers": pending,
                "needs_review_answers": review,
            }
        )
    return rows


def _write_written_grades_csv(path: Path, grades: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=WRITTEN_GRADES_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for grade in grades:
            row = dict(grade)
            row["marks_awarded"] = _format_number(grade.get("marks_awarded"))
            row["max_marks"] = _format_number(grade.get("max_marks"))
            row["needs_human_review"] = str(bool(grade.get("needs_human_review"))).lower()
            writer.writerow(row)


def _write_final_scores_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FINAL_SCORES_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            formatted = dict(row)
            for key in ("written_score", "written_total", "mcq_score", "mcq_total", "total_score", "total_marks"):
                formatted[key] = _format_number(row.get(key))
            formatted["grading_complete"] = str(bool(row.get("grading_complete"))).lower()
            formatted["eligible_for_email"] = str(bool(row.get("eligible_for_email"))).lower()
            writer.writerow(formatted)


def import_written_marks(
    parsed_dir: str | Path,
    marks_csv: str | Path,
    *,
    grader: str,
) -> tuple[dict[str, Any], Path]:
    parsed_root = _parsed_root(parsed_dir)
    packet = _load_packet(parsed_root)
    marks_path = Path(marks_csv)
    grades_by_key = {
        (str(answer["roll_no"]), int(answer["q_no"])): _pending_grade(answer)
        for answer in packet.get("answers", [])
    }

    for row_no, row in _read_marks_rows(marks_path):
        roll_no = str(row.get("roll_no") or "").strip()
        try:
            q_no = int(str(row.get("q_no") or "").strip())
        except ValueError as exc:
            raise ValueError(f"row {row_no}: invalid q_no {row.get('q_no')!r}") from exc
        key = (roll_no, q_no)
        if key not in grades_by_key:
            raise ValueError(f"row {row_no}: roll/q pair is not in the written packet: {roll_no} Q{q_no}")
        _apply_manual_mark(grades_by_key[key], row, row_no=row_no, grader=grader)

    grades = [grades_by_key[key] for key in sorted(grades_by_key)]
    student_scores = _student_aggregates(packet, grades)
    payload = {
        "schema_version": WRITTEN_SCHEMA_VERSION,
        "exam_id": packet.get("exam_id"),
        "mode": "written_manual_grades",
        "source_packet_path": f"{WRITTEN_DIR_NAME}/{WRITTEN_PACKET_JSON}",
        "source_marks_csv": str(marks_path),
        "grader": grader,
        "imported_at": _now(),
        "grades": grades,
        "students": student_scores,
        "status_counts": {
            "answers": {
                "graded": sum(1 for grade in grades if grade["status"] == "graded"),
                "pending": sum(1 for grade in grades if grade["status"] == "pending"),
                "needs_review": sum(1 for grade in grades if grade["status"] == "needs_review"),
            },
            "students": {
                "complete": sum(1 for student in student_scores if student["written_status"] == "complete"),
                "pending": sum(1 for student in student_scores if student["written_status"] == "pending"),
                "needs_review": sum(1 for student in student_scores if student["written_status"] == "needs_review"),
            },
        },
        "reports": {
            "written_grades_report_csv": WRITTEN_GRADES_REPORT_CSV,
            "final_scores_csv": FINAL_SCORES_CSV,
            "review_html": WRITTEN_REVIEW_HTML,
        },
    }

    output_dir = _written_dir(parsed_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(output_dir / WRITTEN_GRADES_JSON, payload)
    _write_written_grades_csv(output_dir / WRITTEN_GRADES_REPORT_CSV, grades)
    _write_final_scores_csv(output_dir / FINAL_SCORES_CSV, student_scores)
    _write_written_review_html(
        output_dir / WRITTEN_REVIEW_HTML,
        packet,
        {(grade["roll_no"], grade["q_no"]): grade for grade in grades},
    )
    return payload, output_dir / WRITTEN_GRADES_JSON


def load_written_summary(parsed_dir: str | Path) -> str:
    parsed_root = _parsed_root(parsed_dir)
    grades_path = _grades_path(parsed_root)
    if grades_path.exists():
        payload = _load_json(grades_path)
        counts = payload.get("status_counts", {})
        answer_counts = counts.get("answers", {})
        student_counts = counts.get("students", {})
        return (
            f"exam_id={payload.get('exam_id')} "
            f"answers_graded={answer_counts.get('graded', 0)} "
            f"answers_pending={answer_counts.get('pending', 0)} "
            f"answers_needs_review={answer_counts.get('needs_review', 0)} "
            f"students_complete={student_counts.get('complete', 0)} "
            f"students_pending={student_counts.get('pending', 0)} "
            f"students_needs_review={student_counts.get('needs_review', 0)}"
        )

    packet_path = _packet_path(parsed_root)
    if packet_path.exists():
        packet = _load_json(packet_path)
        return (
            f"exam_id={packet.get('exam_id')} "
            f"written_answers_exported={len(packet.get('answers', []))} "
            f"students_exported={len(packet.get('students', []))}"
        )
    return f"no written grading packet at {packet_path}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smartomr-written",
        description="Export written-answer crops and import manual written marks.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export", help="Create written grading packet/template from verified_index.json")
    export.add_argument("--parsed-dir", required=True, type=Path)
    export.add_argument(
        "--include-unverified",
        action="store_true",
        help="Also export non-rejected students that are not verified yet; use for debugging only",
    )

    import_cmd = subparsers.add_parser("import-marks", help="Import a filled manual marks CSV")
    import_cmd.add_argument("--parsed-dir", required=True, type=Path)
    import_cmd.add_argument("--marks-csv", required=True, type=Path)
    import_cmd.add_argument("--grader", required=True)

    summary = subparsers.add_parser("summary", help="Print written grading status")
    summary.add_argument("--parsed-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "export":
            packet, path = export_written_grading_packet(
                args.parsed_dir,
                include_unverified=args.include_unverified,
            )
            print(f"Wrote {path}")
            print(f"written_review_html={path.parent / WRITTEN_REVIEW_HTML}")
            print(f"manual_marks_template_csv={path.parent / MANUAL_MARKS_TEMPLATE_CSV}")
            print(
                f"students_exported={len(packet.get('students', []))} "
                f"written_answers_exported={len(packet.get('answers', []))}"
            )
        elif args.command == "import-marks":
            payload, path = import_written_marks(args.parsed_dir, args.marks_csv, grader=args.grader)
            print(f"Wrote {path}")
            print(f"written_grades_report_csv={path.parent / WRITTEN_GRADES_REPORT_CSV}")
            print(f"final_scores_csv={path.parent / FINAL_SCORES_CSV}")
            counts = payload["status_counts"]["answers"]
            print(
                f"answers_graded={counts['graded']} "
                f"answers_pending={counts['pending']} "
                f"answers_needs_review={counts['needs_review']}"
            )
        elif args.command == "summary":
            print(load_written_summary(args.parsed_dir))
        else:  # pragma: no cover - argparse enforces the command set
            raise ValueError(f"unknown command: {args.command}")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
