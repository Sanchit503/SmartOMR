"""Local professor review UI for SmartOMR.

This is intentionally file-based. It wraps the existing batch parser and review
artifacts so the professor can upload a scanned bundle, watch progress, inspect
student-wise marks, and open full-sheet overlays without requiring a database.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import mimetypes
import re
import shutil
import sys
import threading
import traceback
import urllib.parse
import uuid
import zipfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.message import Message
from email.parser import BytesHeaderParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO
from xml.etree import ElementTree

import numpy as np
from PIL import Image

from omr.contracts import load_manifest
from omr.generator.config import ExamConfig, NumericalQuestionConfig, WrittenQuestionConfig
from omr.generator.generate import generate_exam
from omr.io.csv import load_answer_key, load_students
from omr.models import AlignedPage
from omr.reader.handwriting import build_roll_ocr_backend, save_roll_number_crop_sets
from omr.reader.scan import align_scan_page, iter_scan_pages
from omr.ui import identity_cache, inspection, student_view
from omr.ui.inspection_view import inspection_body
from omr.workflows.email import (
    EMAIL_LOG_CSV,
    EMAIL_QUEUE_CSV,
    EMAIL_SKIPPED_CSV,
    prepare_email_release,
    send_email_release,
)
from omr.workflows.batch import (
    _PageRecord,
    _mcq_answer_summary,
    _page_identity,
    _write_review_reports,
    _write_student_group,
    parse_exam_bundle,
)
from omr.workflows.review import (
    assign_source_page,
    decide_identity_suggestion,
    hold_student,
    ignore_unmatched_page,
    initialize_verification_index,
    load_identity_resolution,
    load_or_initialize_verified_index,
    reject_student,
    selected_student_pages,
    verify_student,
)
from omr.workflows.ownership_review import reassess_saved_ownership, approve_clean_matches
from omr.workflows.grading import grade_selected_sheets, validate_grading_key


APP_TITLE = "SmartOMR"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_DATA_DIR = Path("data")
RUNS_DIR_NAME = "ui_runs"
RUN_STATE_NAME = "run_state.json"
ROLL_FEEDBACK_DIR_NAME = "roll_digit_feedback"
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
UPLOAD_READ_SIZE = 1024 * 1024
MAX_MULTIPART_HEADER_BYTES = 64 * 1024
MAX_FORM_FIELD_BYTES = 1024 * 1024


@dataclass(frozen=True)
class UploadedFile:
    filename: str
    path: Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "run"


def _read_json(path: Path) -> dict[str, Any]:
    return inspection.read_json(path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    inspection.write_json_atomic(path, payload)


def _rel(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def _header_parameter(value: str, header: str, name: str) -> str | None:
    message = Message()
    message[header] = value
    parameter = message.get_param(name, header=header)
    return str(parameter) if parameter is not None else None


def parse_multipart_upload(
    stream: BinaryIO,
    *,
    content_type: str,
    content_length: int,
    staging_dir: Path,
) -> tuple[dict[str, str], dict[str, UploadedFile]]:
    """Parse a browser multipart upload while streaming file bodies to disk."""

    if content_length <= 0:
        raise ValueError("empty upload request")
    if content_length > MAX_UPLOAD_BYTES:
        raise ValueError(f"upload exceeds the {MAX_UPLOAD_BYTES // (1024 ** 3)} GB limit")
    boundary_text = _header_parameter(content_type, "content-type", "boundary")
    if not boundary_text:
        raise ValueError("expected multipart/form-data with a boundary")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "multipart/form-data":
        raise ValueError("expected multipart/form-data")

    delimiter = b"--" + boundary_text.encode("utf-8")
    closing_delimiter = delimiter + b"--"
    remaining = content_length
    fields: dict[str, str] = {}
    files: dict[str, UploadedFile] = {}
    staging_dir.mkdir(parents=True, exist_ok=True)

    def read_line(limit: int = UPLOAD_READ_SIZE) -> bytes:
        nonlocal remaining
        if remaining <= 0:
            return b""
        data = stream.readline(min(limit, remaining))
        remaining -= len(data)
        return data

    first = read_line(MAX_MULTIPART_HEADER_BYTES)
    if first.rstrip(b"\r\n") != delimiter:
        raise ValueError("malformed multipart upload: opening boundary is missing")

    finished = False
    while not finished and remaining > 0:
        header_lines: list[bytes] = []
        header_size = 0
        while True:
            line = read_line(MAX_MULTIPART_HEADER_BYTES)
            if not line:
                raise ValueError("malformed multipart upload: truncated part headers")
            header_size += len(line)
            if header_size > MAX_MULTIPART_HEADER_BYTES:
                raise ValueError("multipart part headers are too large")
            if line in {b"\r\n", b"\n"}:
                break
            header_lines.append(line)

        headers = BytesHeaderParser(policy=email_policy).parsebytes(b"".join(header_lines))
        disposition = headers.get("Content-Disposition", "")
        field_name = _header_parameter(disposition, "content-disposition", "name")
        filename = _header_parameter(disposition, "content-disposition", "filename")
        if not field_name:
            raise ValueError("multipart part is missing its field name")

        file_handle = None
        field_buffer = bytearray()
        if filename:
            upload_path = staging_dir / f"{uuid.uuid4().hex}_{_safe_id(Path(filename).name)}"
            file_handle = upload_path.open("wb")
        previous = b""
        try:
            while True:
                line = read_line()
                marker = line.rstrip(b"\r\n")
                if marker in {delimiter, closing_delimiter}:
                    payload_tail = previous
                    if payload_tail.endswith(b"\r\n"):
                        payload_tail = payload_tail[:-2]
                    elif payload_tail.endswith(b"\n"):
                        payload_tail = payload_tail[:-1]
                    if file_handle is not None:
                        file_handle.write(payload_tail)
                    else:
                        field_buffer.extend(payload_tail)
                    finished = marker == closing_delimiter
                    break
                if not line:
                    raise ValueError("malformed multipart upload: closing boundary is missing")
                if previous:
                    if file_handle is not None:
                        file_handle.write(previous)
                    else:
                        field_buffer.extend(previous)
                        if len(field_buffer) > MAX_FORM_FIELD_BYTES:
                            raise ValueError(f"form field {field_name!r} is too large")
                previous = line
        finally:
            if file_handle is not None:
                file_handle.close()

        if filename:
            previous_file = files.get(field_name)
            if previous_file is not None:
                previous_file.path.unlink(missing_ok=True)
            files[field_name] = UploadedFile(filename=Path(filename).name, path=upload_path)
        else:
            fields[field_name] = bytes(field_buffer).decode("utf-8", errors="replace")

    return fields, files


def _canonicalize_roster(path: Path) -> dict[str, int]:
    students = load_students(path)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["roll_no", "name", "email", "program"])
        writer.writeheader()
        for student in students.values():
            writer.writerow(
                {
                    "roll_no": student.roll_no,
                    "name": student.name,
                    "email": student.email,
                    "program": student.program,
                }
            )
    counts: dict[str, int] = {"total": len(students)}
    for student in students.values():
        key = student.program or "UNKNOWN"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _manifest_objective_questions(manifest: dict) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    default_mcq_marks = float(manifest.get("exam", {}).get("marks_per_mcq", 1.0))
    for entry in sorted(manifest.get("mcq_block", []), key=lambda item: int(item["q_no"])):
        questions.append(
            {
                "q_no": int(entry["q_no"]),
                "kind": "mcq",
                "marks": default_mcq_marks,
                "options": list(entry.get("options") or []),
            }
        )
    for entry in sorted(manifest.get("numerical_block", []), key=lambda item: int(item["q_no"])):
        questions.append(
            {
                "q_no": int(entry["q_no"]),
                "kind": "numerical",
                "marks": float(entry.get("max_marks", 1.0)),
                "digits": int(entry.get("positions") or entry.get("digits") or 1),
            }
        )
    return sorted(questions, key=lambda item: int(item["q_no"]))


def _write_answer_key_from_form(manifest: dict, fields: dict[str, str], output_path: Path) -> Path | None:
    if fields.get("answer_key_mode") != "manifest_form":
        return None
    questions = _manifest_objective_questions(manifest)
    if not questions:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["q_no", "answer", "marks"])
        writer.writeheader()
        for question in questions:
            q_no = int(question["q_no"])
            dropped = fields.get(f"drop_q{q_no}") == "1"
            answer = (fields.get(f"answer_q{q_no}") or "").strip().upper()
            marks_text = (fields.get(f"marks_q{q_no}") or "").strip()
            marks = float(marks_text) if marks_text else float(question["marks"])
            if dropped:
                writer.writerow({"q_no": q_no, "answer": "DROPPED", "marks": 0})
                continue
            if not answer:
                raise ValueError(f"answer key is missing Q{q_no}; enter an answer or mark it dropped")
            if question["kind"] == "mcq" and answer not in set(question.get("options") or []):
                raise ValueError(f"MCQ Q{q_no} answer must be one of {', '.join(question.get('options') or [])}")
            if question["kind"] == "numerical" and not all(
                re.fullmatch(r"[0-9]+", item.strip()) for item in re.split(r"[|,]", answer) if item.strip()
            ):
                raise ValueError(f"numerical Q{q_no} answer must be digits, or alternatives separated by |")
            writer.writerow({"q_no": q_no, "answer": answer, "marks": marks})
    return output_path


def _parse_question_rows(text: str, *, kind: str) -> list[WrittenQuestionConfig] | list[NumericalQuestionConfig]:
    rows = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [part.strip() for part in re.split(r"[,:\t ]+", line) if part.strip()]
        if len(parts) != 3:
            raise ValueError(
                f"{kind} question row {line!r} must have exactly 3 values: "
                "q_no marks lines/digits"
            )
        q_no = int(parts[0])
        marks = float(parts[1])
        count = int(parts[2])
        if kind == "written":
            rows.append(WrittenQuestionConfig(q_no=q_no, max_marks=marks, lines=count))
        else:
            rows.append(NumericalQuestionConfig(q_no=q_no, max_marks=marks, digits=count))
    return rows


def _asset_url(path: str | Path | None, base: Path | None = None) -> str:
    if not path:
        return ""
    p = _resolve_output_path(path, base) if base is not None else Path(path)
    return "/artifact?path=" + urllib.parse.quote(str(p.resolve()), safe="")


def _resolve_output_path(path: str | Path | None, base: Path | None = None) -> Path:
    if path is None or str(path) == "":
        return base or Path()
    p = Path(path)
    if p.is_absolute() or p.exists():
        return p
    if base is not None:
        candidate = base / p
        if candidate.exists():
            return candidate
    return p


def _html_page(title: str, body: str, *, refresh_seconds: int | None = None) -> bytes:
    refresh = f'<meta http-equiv="refresh" content="{refresh_seconds}">' if refresh_seconds else ""
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  {refresh}
  <title>{html.escape(title)} - {APP_TITLE}</title>
  <style>
    :root {{
      font-family: Inter, "Segoe UI", Arial, Helvetica, sans-serif;
      color: #111827;
      background: #f3f5f8;
      --border: #d7dde8;
      --soft-border: #e6eaf0;
      --panel: #ffffff;
      --muted: #667085;
      --primary: #1f5fbf;
      --primary-dark: #184c99;
      --danger: #b42318;
    }}
    body {{ margin: 0; font-size: 14px; }}
    header {{
      background: #ffffff;
      border-bottom: 1px solid var(--border);
      padding: 13px 24px;
      display: flex;
      gap: 22px;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 10;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
    }}
    header strong {{ font-size: 16px; letter-spacing: 0; }}
    header a {{ color: var(--primary); text-decoration: none; font-weight: 700; }}
    header a:hover {{ text-decoration: underline; }}
    main {{ max-width: 1480px; margin: 0 auto; padding: 24px; }}
    h1 {{ font-size: 24px; line-height: 1.2; margin: 0 0 16px; font-weight: 800; }}
    h2 {{ font-size: 16px; margin: 0 0 12px; font-weight: 800; }}
    p {{ line-height: 1.45; }}
    .band {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 16px;
      margin-bottom: 16px;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.03);
    }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }}
    .metric {{ background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 12px 14px; min-height: 56px; }}
    .metric span {{ display: block; font-size: 12px; color: var(--muted); margin-bottom: 6px; }}
    .metric strong {{ display: block; font-size: 20px; line-height: 1.2; overflow-wrap: anywhere; }}
    table {{ display: block; width: 100%; max-width: 100%; overflow-x: auto; border-collapse: separate; border-spacing: 0; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; }}
    th, td {{ padding: 10px 12px; border-bottom: 1px solid #edf0f5; text-align: left; vertical-align: top; font-size: 13px; }}
    th {{ background: #eef2f7; color: #344054; font-size: 12px; white-space: nowrap; font-weight: 800; }}
    tr:last-child td {{ border-bottom: 0; }}
    tr:hover td {{ background: #f8fbff; }}
    label {{ display: block; font-size: 12px; color: #344054; font-weight: 800; margin: 0 0 5px; }}
    label.inline {{ display: inline-flex; align-items: center; gap: 6px; margin: 0; font-weight: 700; }}
    label.inline input {{ width: auto; }}
    input, select, textarea {{
      width: 100%;
      box-sizing: border-box;
      border: 1px solid #cbd5e1;
      border-radius: 5px;
      background: #fff;
      padding: 8px 10px;
      font: inherit;
    }}
    input:focus, select:focus, textarea:focus {{ outline: 2px solid rgba(31, 95, 191, 0.18); border-color: var(--primary); }}
    button, .button {{
      display: inline-block;
      border: 1px solid var(--primary);
      background: var(--primary);
      color: #fff;
      padding: 8px 13px;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
      border-radius: 4px;
      line-height: 1.2;
    }}
    button:hover, .button:hover {{ background: var(--primary-dark); border-color: var(--primary-dark); text-decoration: none; }}
    .button.secondary, button.secondary {{ background: #fff; color: var(--primary); }}
    .button.secondary:hover, button.secondary:hover {{ background: #eef5ff; border-color: var(--primary); }}
    button.danger {{ background: #fff; color: var(--danger); border-color: #f0aaa5; }}
    button.danger:hover {{ background: #fff1f0; color: #8f1c13; border-color: var(--danger); }}
    .actions {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
    .ownership-actions {{ align-items: flex-start; gap: 24px; }}
    .ownership-actions form {{ min-width: 0; max-width: 100%; display: flex; flex-direction: column; align-items: flex-start; gap: 8px; }}
    .ownership-actions label {{ display: flex; align-items: flex-start; gap: 8px; margin: 0; }}
    .ownership-actions input[type="checkbox"] {{ width: 16px; height: 16px; margin: 0; flex: none; }}
    .toolbar {{ justify-content: space-between; }}
    .badge {{ display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: 12px; font-weight: 700; }}
    .ready, .auto, .verified {{ background: #e8f5ee; color: #166534; }}
    .review, .pending, .missing {{ background: #fff7df; color: #8a4b08; }}
    .error, .failed, .rejected {{ background: #feeceb; color: #b42318; }}
    .neutral {{ background: #eef2f7; color: #344054; }}
    .split {{ display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(360px, 0.8fr); gap: 16px; align-items: start; }}
    .student-layout {{ display: grid; grid-template-columns: minmax(0, 1fr) 410px; gap: 16px; align-items: start; }}
    .side-stack {{ display: flex; flex-direction: column; gap: 14px; }}
    .side-stack .band {{ margin-bottom: 0; }}
    .operation-grid {{ display: grid; grid-template-columns: 1fr; gap: 14px; }}
    .operation {{ border: 1px solid var(--soft-border); border-radius: 6px; padding: 12px; background: #fbfcfe; }}
    .operation h3 {{ margin: 0 0 10px; font-size: 14px; }}
    .form-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; align-items: end; }}
    .form-grid .wide {{ grid-column: 1 / -1; }}
    .pages {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }}
    figure {{ margin: 0; border: 1px solid var(--border); border-radius: 6px; background: #fff; padding: 8px; }}
    figcaption {{ font-size: 12px; color: var(--muted); margin-bottom: 6px; }}
    img.sheet {{ width: 100%; height: auto; display: block; background: #fff; }}
    .flags {{ max-width: 520px; overflow-wrap: anywhere; }}
    .muted {{ color: var(--muted); }}
    .mono {{ font-family: Consolas, monospace; }}
    .empty {{ color: var(--muted); text-align: center; padding: 20px; }}
    .section-title {{ display: flex; justify-content: space-between; gap: 12px; align-items: center; margin: 20px 0 10px; }}
    .section-title h2 {{ margin: 0; }}
    .suggestion {{ background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 16px; margin-bottom: 16px; }}
    .suggestion h3, .suggestion h4 {{ margin: 0 0 10px; }}
    .suggestion h4 {{ font-size: 13px; margin-top: 16px; }}
    .suggestion-heading {{ display: flex; justify-content: space-between; gap: 16px; align-items: start; }}
    .suggestion-heading p {{ margin: 4px 0 0; }}
    .suggestion-pages {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 14px; }}
    .suggestion-page {{ min-width: 0; border-top: 1px solid var(--soft-border); padding-top: 12px; }}
    .suggestion-sheet {{ max-height: 680px; object-fit: contain; border: 1px solid var(--soft-border); }}
    .evidence-summary {{ min-height: 38px; overflow-wrap: anywhere; }}
    .digit-strip {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(76px, 1fr)); gap: 8px; }}
    .digit-cell {{ min-width: 0; padding: 6px; text-align: center; }}
    .digit-cell img {{ width: 100%; aspect-ratio: 1 / 1; object-fit: contain; display: block; background: #fff; }}
    .digit-cell span {{ display: block; margin-top: 5px; font-size: 12px; font-weight: 700; }}
    .digit-cell small {{ display: block; margin-top: 3px; color: var(--muted); overflow-wrap: anywhere; }}
    .digit-missing {{ display: grid !important; place-items: center; aspect-ratio: 1 / 1; background: #f8fafc; color: var(--muted); font-weight: 400 !important; }}
    .source-evidence {{ margin: 10px 0 0; padding-left: 18px; }}
    .source-evidence li {{ margin: 5px 0; }}
    .suggestion-actions {{ display: grid; grid-template-columns: repeat(3, minmax(220px, 1fr)); gap: 12px; margin-top: 16px; }}
    .suggestion-actions form {{ border-top: 1px solid var(--soft-border); padding-top: 12px; }}
    .suggestion-actions input {{ margin-bottom: 8px; }}
    .decision-record {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 16px; padding: 12px; border: 1px solid var(--soft-border); background: #f8fafc; }}
    .review-sync {{ position: fixed; bottom: 16px; left: 16px; right: 16px; z-index: 30; background: #fff7df; border: 1px solid #d6a651; padding: 12px; }}
    .review-sync[hidden] {{ display: none; }}
    @media (max-width: 1040px) {{ .student-layout, .split {{ grid-template-columns: 1fr; }} }}
    @media (max-width: 820px) {{ .suggestion-pages, .suggestion-actions {{ grid-template-columns: 1fr; }} .suggestion-heading {{ flex-direction: column; }} }}
    @media (max-width: 600px) {{ header {{ flex-wrap: wrap; gap: 12px; padding: 12px; }} main {{ padding: 12px; }} .pages {{ grid-template-columns: minmax(0, 1fr); }} }}
  </style>
</head>
<body>
  <header>
    <strong>{APP_TITLE}</strong>
    <a href="/">Exams</a>
    <a href="/generate">Generate OMR</a>
    <a href="/new">New Run</a>
  </header>
  <main>{body}</main>
</body>
</html>"""
    return document.encode("utf-8")


def _badge(value: str | None) -> str:
    text = html.escape(str(value or ""))
    lowered = text.lower()
    cls = "neutral"
    if lowered in {"ready", "verified", "manually_checked", "auto_graded", "auto graded", "correct", "auto_matched", "auto matched", "approved", "approved for release"}:
        cls = "ready"
    elif "review" in lowered or "pending" in lowered or "missing" in lowered:
        cls = "review"
    elif "error" in lowered or "failed" in lowered or "wrong" in lowered or "rejected" in lowered:
        cls = "error"
    return f'<span class="badge {cls}">{text}</span>'


def _professor_status(parser_status: str | None, verified_status: str | None = None) -> str:
    if verified_status == "auto_matched":
        return "Auto matched"
    if verified_status == "approved":
        return "Approved for release"
    if verified_status == "verified":
        return "MANUALLY_CHECKED"
    if verified_status == "rejected":
        return "REJECTED"
    if verified_status == "missing_pages":
        return "MISSING_PAGES"
    if verified_status == "needs_review":
        return "NEEDS_REVIEW"
    if verified_status == "pending_verification" or parser_status == "ready":
        return "PENDING_VERIFICATION"
    if parser_status == "needs_review":
        return "NEEDS_REVIEW"
    if parser_status == "error":
        return "FAILED"
    return str(parser_status or "NEEDS_REVIEW")


def _review_created_details(student: dict[str, Any], parse_dir: Path) -> dict[str, Any]:
    return {
        **student,
        "student": {
            "roll_no": student["roll_no"], "program": student.get("program"),
            "name": student.get("student_name") or "", "email": student.get("student_email") or "",
        },
        "details_path": str(parse_dir / "verified_index.json"),
        "source_pages": [
            {"source_index": page["source_index"], "page_index": page["page"], "used": True}
            for page in selected_student_pages(student)
        ],
    }


def _score(student: dict[str, Any]) -> tuple[float | None, float | None]:
    if student.get("manual_score_override") is not None:
        return float(student.get("manual_score_override") or 0.0), float(
            student.get("manual_total_override") if student.get("manual_total_override") is not None else 0.0
        )
    score = 0.0
    total = 0.0
    for score_key, total_key in (("mcq_score", "mcq_total"), ("numerical_score", "numerical_total")):
        if student.get(score_key) is not None:
            score += float(student.get(score_key) or 0.0)
        if student.get(total_key) is not None:
            total += float(student.get(total_key) or 0.0)
    has_scores = any(student.get(key) is not None for key in ("mcq_score", "numerical_score"))
    has_totals = any(student.get(key) is not None for key in ("mcq_total", "numerical_total"))
    incomplete = any(
        student.get(f"{kind}_score") is None and student.get(f"{kind}_total")
        for kind in ("mcq", "numerical")
    )
    return score if has_scores and not incomplete else None, total if has_totals else None


def _marks_label(details: dict[str, Any], verified: dict[str, Any] | None = None) -> str:
    if verified and verified.get("grading_status") == "stale":
        return "Regrading required"
    if verified and verified.get("grading_status") == "graded":
        details = verified
    elif _selection_changed(details, verified):
        return "Regrading required"
    if not any(details.get(key) is not None for key in ("mcq_score", "numerical_score")):
        return "Not graded"
    if any(
        details.get(f"{kind}_score") is None and details.get(f"{kind}_total")
        for kind in ("mcq", "numerical")
    ):
        return "Grading incomplete"
    score, total = _score(details)
    return f"{_fmt_num(score)} / {_fmt_num(total)}"


def _selection_changed(details: dict[str, Any], verified: dict[str, Any] | None) -> bool:
    if not verified:
        return False
    if verified.get("manual_pages"):
        return True
    moved = any(decision.get("action") == "move_page_to_identity_suggestion"
                for decision in verified.get("decision_log", []))
    if moved and not verified.get("sheet_pdf_path"):
        return True
    if "pages" not in details:
        return False
    def sources(pages: list[dict[str, Any]]) -> set[tuple[Any, Any]]:
        return {(page.get("page", page.get("page_index")), page.get("source_index")) for page in pages}
    return sources(details["pages"]) != sources(selected_student_pages(verified))


def _missing_roster_students(index: dict[str, Any]) -> list[dict[str, Any]]:
    known = {str(student.get("roll_no")) for student in index.get("students", [])}
    return [student for student in (index.get("roster_reconciliation") or {}).get("missing_students", [])
            if str(student.get("roll_no")) not in known]


def _review_summary(index: dict[str, Any]) -> dict[str, int]:
    students = index.get("students", [])
    selected_sources = {page.get("source_index") for student in students for page in selected_student_pages(student)}
    return {
        "students": len(students),
        "verified": sum(student.get("status") == "verified" for student in students),
        "auto_matched": sum(student.get("status") == "auto_matched" for student in students),
        "approved": sum(student.get("status") == "approved" for student in students),
        "pending_verification": sum(student.get("status") == "pending_verification" for student in students),
        "needs_review": sum(student.get("status") not in {"verified", "approved", "auto_matched", "pending_verification"} for student in students),
        "eligible_for_email": sum(bool(student.get("eligible_for_email")) for student in students),
        "assigned_pages": len(selected_sources),
        "unmatched_pages": sum(page.get("status") == "needs_review" and page.get("source_index") not in selected_sources
                               for page in index.get("unmatched_pages", [])),
        "page_errors": sum(page.get("status") != "ignored" and page.get("source_index") not in selected_sources
                           for page in index.get("page_errors", [])),
        "roster_missing": len(_missing_roster_students(index)),
    }


def _review_metrics(summary: dict[str, Any]) -> str:
    return '<div class="grid">' + "".join(
        f'<div class="metric"><span>{label}</span><strong>{summary.get(key, 0)}</strong></div>'
        for label, key in (("Students", "students"), ("Manually Checked", "verified"),
                           ("Auto Matched", "auto_matched"), ("Approved For Release", "approved"),
                           ("Pending Verification", "pending_verification"), ("Needs Review", "needs_review"),
                           ("Assigned Pages", "assigned_pages"), ("Unmatched Pages", "unmatched_pages"),
                           ("Unreadable Pages", "page_errors"), ("Missing Sheets", "roster_missing"))
    ) + '</div>'


def _is_grouping_only(state: dict[str, Any]) -> bool:
    inputs = state.get("inputs")
    return bool(isinstance(inputs, dict) and inputs.get("grouping_only"))


def _grouping_order_notice(index: dict[str, Any]) -> str:
    inference = index.get("grouping_order_inference")
    if not isinstance(inference, dict) or inference.get("mode") not in {"page-major", "sheet-major"}:
        return ""
    mode = str(inference["mode"])
    matches = (inference.get("matches") or {}).get(mode, "")
    observed = inference.get("observed_pages", "")
    counts = f"({matches}/{observed} readable page codes matched). " if matches != "" and observed != "" else ""
    return (
        '<div class="band">'
        f"<strong>Scanner order:</strong> auto-detected {html.escape(mode)} "
        f"{html.escape(counts)}"
        "Scanner order is not proof of ownership. Order-only pairs require human assignment."
        "</div>"
    )


def _fmt_num(value: float | int | None) -> str:
    if value is None:
        return ""
    value = float(value)
    return str(int(value)) if value.is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def _identity_roll_digits(roll_no: str, program: str) -> str:
    if program == "PHD" and roll_no.startswith("PHD"):
        return roll_no[3:]
    if program == "MTECH" and roll_no.startswith("MT"):
        return roll_no[2:]
    return roll_no


def _identity_probability(value: object) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "unavailable"


def _render_identity_cells(page: dict[str, Any], roll_no: str, program: str, parse_dir: Path) -> str:
    crop_paths = list(page.get("cell_crop_paths") or [])
    probabilities = list(page.get("cell_probabilities") or [])
    digits = _identity_roll_digits(roll_no, program)
    count = max(len(crop_paths), len(probabilities), len(digits))
    if not count:
        return '<p class="muted">No individual digit artifacts were recorded.</p>'
    cells = []
    for position in range(count):
        digit = digits[position] if position < len(digits) else "?"
        row = probabilities[position] if position < len(probabilities) else {}
        selected_probability = row.get(digit) if isinstance(row, dict) else None
        ranked = sorted(
            (
                (str(label), float(probability))
                for label, probability in row.items()
                if isinstance(probability, (int, float))
            ),
            key=lambda item: item[1],
            reverse=True,
        )[:2] if isinstance(row, dict) else []
        ranked_text = ", ".join(
            f"{label} {_identity_probability(probability)}"
            for label, probability in ranked
        )
        crop = crop_paths[position] if position < len(crop_paths) else None
        crop_html = (
            f'<a href="{_asset_url(crop, parse_dir)}"><img src="{_asset_url(crop, parse_dir)}" '
            f'alt="Digit {position + 1} crop"></a>'
            if crop
            else '<span class="digit-missing">No crop</span>'
        )
        cells.append(
            '<figure class="digit-cell">'
            f'<figcaption>Digit {position + 1}: <strong>{html.escape(digit)}</strong></figcaption>'
            f'{crop_html}'
            f'<span>P({html.escape(digit)}) {html.escape(_identity_probability(selected_probability))}</span>'
            f'<small>{html.escape(ranked_text or "No probability vector")}</small>'
            '</figure>'
        )
    return f'<div class="digit-strip">{"".join(cells)}</div>'


def _render_identity_page(
    title: str,
    page: dict[str, Any],
    roll_no: str,
    program: str,
    parse_dir: Path,
) -> str:
    image_path = page.get("page_image_path")
    image = (
        f'<a href="{_asset_url(image_path, parse_dir)}"><img class="sheet suggestion-sheet" '
        f'src="{_asset_url(image_path, parse_dir)}" alt="{html.escape(title)}"></a>'
        if image_path
        else '<p class="empty">No page image artifact</p>'
    )
    summary = (
        f'Bubble: {html.escape(str(page.get("bubble_roll") or "not available"))} | '
        f'Cells: {html.escape(str(page.get("cell_roll") or "unreadable"))} | '
        f'Min cell confidence: {html.escape(_identity_probability(page.get("cell_min_probability")))} | '
        f'Selector: {html.escape(str(page.get("selector_state") or "unavailable"))}'
    )
    source_flags = "".join(
        '<li>'
        f'{_badge(str(flag.get("severity") or "info"))} '
        f'<span class="mono">{html.escape(str(flag.get("code") or ""))}</span>: '
        f'{html.escape(str(flag.get("message") or ""))}'
        '</li>'
        for flag in page.get("evidence_flags", [])
        if isinstance(flag, dict)
    )
    return f"""
    <section class="suggestion-page">
      <h3>{html.escape(title)}</h3>
      <p class="mono evidence-summary">{summary}</p>
      {image}
      <h4>Digit Evidence</h4>
      {_render_identity_cells(page, roll_no, program, parse_dir)}
      <ul class="source-evidence">{source_flags or '<li class="muted">No source-level evidence flags</li>'}</ul>
    </section>
    """


def _render_identity_suggestions(
    run_id: str,
    parse_dir: Path,
    index: dict[str, Any],
    resolution: dict[str, Any],
) -> str:
    candidates = list(resolution.get("candidates") or [])
    if not candidates:
        return """
        <div class="section-title"><h2>Suggested Identity Matches</h2></div>
        <section class="band"><p class="empty">No identity suggestions were generated.</p></section>
        """
    decisions = {
        str(decision.get("candidate_id") or ""): decision
        for decision in index.get("identity_suggestion_decisions", [])
    }
    articles = []
    for candidate in candidates:
        candidate_id = str(candidate.get("candidate_id") or "")
        roll_no = str(candidate.get("roll_no") or "")
        program = str(candidate.get("program") or "")
        decision = decisions.get(candidate_id)
        blocked = bool(candidate.get("blocked"))
        state_badge = _badge("blocked" if blocked else "suggested")
        auto_badge = _badge("shadow eligible" if candidate.get("shadow_auto_eligible") else "human decision")
        evidence_rows = []
        for flag in candidate.get("evidence_flags", []):
            metadata = flag.get("metadata") if isinstance(flag, dict) else None
            metadata_text = json.dumps(metadata, sort_keys=True) if metadata else ""
            evidence_rows.append(
                "<tr>"
                f"<td>{_badge(str(flag.get('severity') or 'info'))}</td>"
                f"<td class=\"mono\">{html.escape(str(flag.get('code') or ''))}</td>"
                f"<td>{html.escape(str(flag.get('message') or ''))}</td>"
                f"<td class=\"mono flags\">{html.escape(metadata_text)}</td>"
                "</tr>"
            )
        neighbour_rows = []
        for neighbour in candidate.get("near_neighbours", []):
            neighbour_rows.append(
                "<tr>"
                f"<td>{html.escape(str(neighbour.get('roll_no') or ''))}</td>"
                f"<td>{html.escape(str(neighbour.get('differing_position') or ''))}</td>"
                f"<td>{html.escape(str(neighbour.get('selected_digit') or ''))} / {html.escape(str(neighbour.get('neighbour_digit') or ''))}</td>"
                f"<td>{html.escape(_identity_probability(neighbour.get('minimum_selected_probability')))}</td>"
                f"<td>{html.escape(_identity_probability(neighbour.get('minimum_pairwise_support')))}</td>"
                f"<td>{html.escape('yes' if neighbour.get('open_slot') else 'no')}</td>"
                "</tr>"
            )
        if decision:
            actions = (
                '<div class="decision-record">'
                f'<strong>Decision: {html.escape(str(decision.get("suggestion_action") or "recorded"))}</strong>'
                f'<span>Resolved roll: {html.escape(str(decision.get("resolved_roll_no") or "none"))}</span>'
                f'<span>Reviewer: {html.escape(str(decision.get("reviewer") or ""))}</span>'
                f'<span>Note: {html.escape(str(decision.get("note") or ""))}</span>'
                '</div>'
            )
        else:
            action_base = (
                f'/runs/{html.escape(run_id)}/suggestions/'
                f'{urllib.parse.quote(candidate_id)}/'
            )
            actions = f"""
            <div class="suggestion-actions">
              <form method="post" action="{action_base}approve">
                <label>Approval note</label>
                <input name="note" required placeholder="What you checked on both pages">
                <button type="submit">Approve {html.escape(roll_no)}</button>
              </form>
              <form method="post" action="{action_base}assign">
                <label>Correct roster roll</label>
                <input name="roll_no" list="student-rolls" required placeholder="Roll number">
                <label>Assignment note</label>
                <input name="note" required placeholder="Why these pages belong to this roll">
                <button class="secondary" type="submit">Assign Different Roll</button>
              </form>
              <form method="post" action="{action_base}reject">
                <label>Rejection note</label>
                <input name="note" required placeholder="Why this proposal is wrong">
                <button class="danger" type="submit">Reject Proposal</button>
              </form>
            </div>
            """
        articles.append(
            f"""
            <article class="suggestion">
              <div class="suggestion-heading">
                <div>
                  <h3>{html.escape(roll_no)} <span class="muted">{html.escape(program)}</span></h3>
                  <p class="mono">Path {html.escape(str(candidate.get('path') or ''))} | page 1 source {html.escape(str(candidate.get('page_one_source_index') or ''))} | page {html.escape(str(candidate.get('sheet_page') or ''))} source {html.escape(str(candidate.get('continuation_source_index') or ''))}</p>
                </div>
                <div class="actions">{state_badge}{auto_badge}</div>
              </div>
              <div class="suggestion-pages">
                {_render_identity_page('Page 1 anchor', dict(candidate.get('page_one') or {}), roll_no, program, parse_dir)}
                {_render_identity_page(f"Continuation page {candidate.get('sheet_page') or ''}", dict(candidate.get('continuation') or {}), roll_no, program, parse_dir)}
              </div>
              <h4>Structured Evidence</h4>
              <table><thead><tr><th>Severity</th><th>Code</th><th>Meaning</th><th>Measurements</th></tr></thead>
              <tbody>{''.join(evidence_rows) or '<tr><td colspan="4" class="empty">No warning or blocking evidence</td></tr>'}</tbody></table>
              <h4>One-Digit Roster Neighbours</h4>
              <table><thead><tr><th>Roll</th><th>Position</th><th>Digits</th><th>Selected P</th><th>Pairwise Support</th><th>Open Slot</th></tr></thead>
              <tbody>{''.join(neighbour_rows) or '<tr><td colspan="6" class="empty">No one-digit roster neighbour</td></tr>'}</tbody></table>
              {actions}
            </article>
            """
        )
    counts = resolution.get("counts") or {}
    summary = (
        f'{len(candidates)} proposal(s); '
        f'{int(counts.get("shadow_auto_eligible") or 0)} meet the shadow threshold; '
        f'{int(counts.get("blocked_suggestions") or 0)} contain blocking evidence.'
    )
    return f"""
    <div class="section-title"><h2>Suggested Identity Matches</h2></div>
    <p class="muted">{html.escape(summary)}</p>
    {''.join(articles)}
    """


def _load_roster_rows(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None or not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    by_roll: dict[str, dict[str, str]] = {}
    for row in rows:
        roll = ""
        for key in ("roll_no", "roll", "roll_number", "student_roll", "student_id"):
            if row.get(key):
                roll = "".join(str(row[key]).upper().split())
                break
        if roll:
            by_roll[roll] = row
    return by_roll


def _build_ui_roll_ocr_backend() -> tuple[object | None, dict[str, Any]]:
    try:
        backend = build_roll_ocr_backend("local")
    except RuntimeError as exc:
        return None, {
            "enabled": False,
            "provider": "none",
            "warning": str(exc),
        }
    provenance = None
    provenance_method = getattr(backend, "provenance", None)
    if callable(provenance_method):
        provenance = provenance_method()
    return backend, {
        "enabled": backend is not None,
        "provider": getattr(backend, "provider", "local") if backend is not None else "none",
        "provenance": provenance,
        "warning": None,
    }


def _require_ui_roll_ocr_backend() -> tuple[object, dict[str, Any]]:
    backend, state = _build_ui_roll_ocr_backend()
    if backend is None:
        detail = str(state.get("warning") or "local roll OCR could not be initialized")
        raise RuntimeError(
            "Roll-number OCR is required for professor UI processing. "
            f"{detail} Ensure the roll-digit ensemble manifest and its fine-tuned checkpoints are present, then start the run again."
        )
    return backend, state


def _load_inspected_pages(run_dir: Path) -> tuple[dict[int, AlignedPage], dict[int, str]]:
    """Load the exact alignment artifacts approved by source-page inspection."""
    index = inspection.load_inventory(run_dir)
    pages: dict[int, AlignedPage] = {}
    errors: dict[int, str] = {}
    for record in index.get("pages", []):
        source_index = int(record["source_index"])
        aligned_value = record.get("aligned")
        page_index = record.get("page_index")
        if not aligned_value or page_index is None:
            errors[source_index] = str(record.get("error") or "page did not pass alignment inspection")
            continue
        aligned_path = (run_dir / str(aligned_value)).resolve()
        if not aligned_path.is_relative_to((run_dir / "inspection").resolve()) or not aligned_path.is_file():
            errors[source_index] = "inspected aligned image is missing or outside this run"
            continue
        image = Image.open(aligned_path).convert("L").copy()
        pages[source_index] = AlignedPage(
            page_index=int(page_index),
            source_index=source_index,
            image=image,
            alignment_confidence=float(record.get("alignment_confidence") or 0.0),
            page_mark_confidence=float(record.get("page_mark_confidence") or 0.0),
            debug_image=image.copy(),
        )
    return pages, errors


def _identity_preview_status(roll_no: str | None, confidence: str, flags: list[str]) -> str:
    """Keep faint-image observations distinct from actual identity uncertainty."""
    identity_warning = any(
        marker in str(flag).lower()
        for flag in flags
        for marker in ("could not be decoded", "low confidence", "conflicts with", "requires manual")
    )
    if not roll_no or confidence == "low" or identity_warning:
        return "needs_review"
    return "detected_with_caution" if flags else "detected"


def _refresh_identity_preview_counts(preview: dict[str, Any]) -> None:
    """Migrate stored preview labels when the audit policy becomes more precise."""
    for row in preview.get("pages", []):
        if row.get("status") == "unavailable":
            continue
        raw_flags = row.get("review_flags", [])
        flags = [str(raw_flags)] if isinstance(raw_flags, str) else [str(flag) for flag in raw_flags]
        row["status"] = _identity_preview_status(
            row.get("literal_roll_no"), str(row.get("confidence") or "low"), flags,
        )
    rows = list(preview.get("pages", []))
    preview["counts"] = {
        "detected": sum(row.get("status") in {"detected", "detected_with_caution"} for row in rows),
        "detected_with_caution": sum(row.get("status") == "detected_with_caution" for row in rows),
        "needs_review": sum(row.get("status") == "needs_review" for row in rows),
        "undetected": sum(not row.get("literal_roll_no") and row.get("status") != "unavailable" for row in rows),
        "unavailable": sum(row.get("status") == "unavailable" for row in rows),
    }


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    path = "xl/sharedStrings.xml"
    if path not in zf.namelist():
        return []
    root = ElementTree.fromstring(zf.read(path))
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    strings: list[str] = []
    for item in root.findall("a:si", ns):
        texts = [node.text or "" for node in item.findall(".//a:t", ns)]
        strings.append("".join(texts))
    return strings


def _xlsx_first_sheet_path(zf: zipfile.ZipFile) -> str:
    workbook = ElementTree.fromstring(zf.read("xl/workbook.xml"))
    rels = ElementTree.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    ns = {
        "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    }
    sheet = workbook.find("a:sheets/a:sheet", ns)
    if sheet is None:
        raise ValueError("XLSX file has no sheets")
    rel_id = sheet.attrib.get(f"{{{ns['r']}}}id")
    for rel in rels.findall("rel:Relationship", ns):
        if rel.attrib.get("Id") == rel_id:
            target = rel.attrib["Target"]
            if target.startswith("/"):
                return target.lstrip("/")
            return "xl/" + target.lstrip("/")
    raise ValueError("XLSX first sheet relationship was not found")


def _xlsx_cell_text(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    value = cell.find("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v")
    if value is None or value.text is None:
        inline = cell.find(".//{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")
        return inline.text if inline is not None and inline.text else ""
    raw = value.text
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (IndexError, ValueError):
            return raw
    return raw


def _column_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    total = 0
    for char in letters:
        total = total * 26 + (ord(char) - ord("A") + 1)
    return max(total - 1, 0)


def _convert_xlsx_to_csv(source: Path, target: Path) -> None:
    with zipfile.ZipFile(source) as zf:
        shared_strings = _xlsx_shared_strings(zf)
        sheet_path = _xlsx_first_sheet_path(zf)
        root = ElementTree.fromstring(zf.read(sheet_path))
    ns = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    rows: list[list[str]] = []
    for row in root.findall(".//a:sheetData/a:row", ns):
        values: dict[int, str] = {}
        for cell in row.findall("a:c", ns):
            ref = cell.attrib.get("r", "")
            values[_column_index(ref)] = _xlsx_cell_text(cell, shared_strings)
        if values:
            width = max(values) + 1
            rows.append([values.get(index, "") for index in range(width)])
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows)


def _normalize_tabular_upload(path: Path, target_csv: Path) -> Path:
    if path.suffix.lower() == ".csv":
        shutil.copy2(path, target_csv)
        return target_csv
    if path.suffix.lower() == ".xlsx":
        _convert_xlsx_to_csv(path, target_csv)
        return target_csv
    raise ValueError(f"unsupported tabular upload type: {path.suffix}; use CSV or XLSX")


def _write_marks_csv(run_dir: Path, parse_dir: Path, index: dict[str, Any]) -> Path:
    reports = run_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    marks_path = reports / "marks.csv"
    rows: list[dict[str, Any]] = []
    for student in index.get("students", []):
        details_path = _resolve_output_path(student.get("details_path"), parse_dir)
        details = _read_json(details_path) if details_path.is_file() else {}
        current = student if student.get("grading_status") else details
        score, total = _score(current)
        rows.append(
            {
                "roll_no": details.get("student", {}).get("roll_no") or student.get("roll_no") or "",
                "name": details.get("student", {}).get("name") or student.get("student_name") or "",
                "email": details.get("student", {}).get("email") or student.get("student_email") or "",
                "status": student.get("status") or details.get("status") or "",
                "marks_obtained": _fmt_num(score),
                "max_marks": _fmt_num(total),
                "manual_override": "yes" if current.get("manual_score_override") is not None else "",
                "manual_note": current.get("manual_score_note") or "",
                "review_flag_count": len(student.get("review_flags", [])),
                "review_flags": " | ".join(str(flag) for flag in student.get("review_flags", [])),
            }
        )
    with marks_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "roll_no",
                "name",
                "email",
                "status",
                "marks_obtained",
                "max_marks",
                "manual_override",
                "manual_note",
                "review_flag_count",
                "review_flags",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    return marks_path


def _student_index_entry(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "roll_no": result["student"]["roll_no"],
        "status": result["status"],
        "student_name": result["student"]["name"],
        "student_email": result["student"].get("email", ""),
        "sheet_pdf_path": (
            str(Path(result["details_path"]).parent / result["sheet_pdf_path"])
            if result.get("sheet_pdf_path")
            else None
        ),
        "mcq_score": result["mcq_score"],
        "mcq_total": result["mcq_total"],
        "numerical_responses": result["numerical_responses"],
        "numerical_score": result["numerical_score"],
        "numerical_total": result["numerical_total"],
        "mcq_answers": _mcq_answer_summary(result),
        "pages": [
            {
                "page": page["page_index"],
                "source_index": page["source_index"],
                "canonical_image_path": page["canonical_image_path"],
                "debug_image_path": page["debug_image_path"],
                "alignment_overlay_path": page["alignment_overlay_path"],
                "sampling_overlay_path": page["sampling_overlay_path"],
            }
            for page in result["pages"]
        ],
        "review_flags": result["review_flags"],
        "details_path": result["details_path"],
    }


def _recalculate_status_counts(index: dict[str, Any]) -> None:
    students = list(index.get("students", []))
    reconciliation = index.get("roster_reconciliation") or {}
    index["status_counts"] = {
        "ready": sum(1 for student in students if student.get("status") == "ready"),
        "needs_review": sum(1 for student in students if student.get("status") == "needs_review"),
        "unmatched_pages": len(index.get("unmatched_pages", [])),
        "page_errors": len(index.get("page_errors", [])),
        "roster_missing": int(reconciliation.get("missing_count") or 0),
    }


def _load_student_results_for_report(index: dict[str, Any], parse_dir: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for student in index.get("students", []):
        details_path = _resolve_output_path(student.get("details_path"), parse_dir)
        if details_path.exists():
            results.append(_read_json(details_path))
    return results


def _replace_verified_student(parse_dir: Path, roll_no: str, result: dict[str, Any]) -> None:
    verified_path = parse_dir / "verified_index.json"
    if not verified_path.exists():
        return
    verified = _read_json(verified_path)
    expected_pages = list(range(1, int(verified.get("expected_pages") or 0) + 1))
    found_pages = sorted(int(page.get("page_index")) for page in result.get("pages", []) if page.get("page_index"))
    missing_pages = [page for page in expected_pages if page not in found_pages]
    score, total = _score(result)
    details_path = Path(str(result.get("details_path") or ""))
    details_dir = details_path.parent if details_path else parsed_dir
    sheet_pdf = result.get("sheet_pdf_path")
    replacement = {
        "status": "needs_review" if result.get("review_flags") else "pending_verification",
        "roll_no": roll_no,
        "program": result.get("student", {}).get("program") or "",
        "student_name": result.get("student", {}).get("name") or "",
        "student_email": result.get("student", {}).get("email") or "",
        "pages": [
            {
                "page": page.get("page_index"),
                "source_index": page.get("source_index"),
                "canonical_image_path": page.get("canonical_image_path"),
                "debug_image_path": page.get("debug_image_path"),
                "alignment_overlay_path": page.get("alignment_overlay_path"),
                "sampling_overlay_path": page.get("sampling_overlay_path"),
                "origin": "student_reupload",
            }
            for page in result.get("pages", [])
        ],
        "manual_pages": [],
        "pages_found": found_pages,
        "expected_pages": expected_pages,
        "missing_pages": missing_pages,
        "parser_status": result.get("status"),
        "eligible_for_email": False,
        "sheet_pdf_path": str(details_dir / sheet_pdf) if sheet_pdf else None,
        "verified_sheet_pdf_path": None,
        "details_path": result.get("details_path"),
        "review_flags": result.get("review_flags", []),
        "mcq_score": result.get("mcq_score"),
        "mcq_total": result.get("mcq_total"),
        "numerical_score": result.get("numerical_score"),
        "numerical_total": result.get("numerical_total"),
        "manual_score_override": result.get("manual_score_override"),
        "manual_total_override": result.get("manual_total_override"),
        "decision_log": [
            {
                "action": "student_reupload",
                "reviewer": "professor-ui",
                "note": f"Re-evaluated from an uploaded replacement sheet; current score {_fmt_num(score)} / {_fmt_num(total)}.",
                "created_at": _now(),
            }
        ],
    }
    students = list(verified.get("students", []))
    for index, student in enumerate(students):
        if str(student.get("roll_no")) == str(roll_no):
            students[index] = replacement
            break
    else:
        students.append(replacement)
    verified["students"] = students
    _write_json(verified_path, verified)


def _parse_student_reupload(
    *,
    upload_path: Path,
    roll_no: str,
    parse_dir: Path,
    manifest_path: Path,
    students_path: Path | None,
    answer_key_path: Path | None,
    roll_ocr_backend: object | None,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    students = load_students(students_path) if students_path and students_path.exists() else None
    default_marks = float(manifest["exam"].get("marks_per_mcq", 1.0))
    answer_key = load_answer_key(answer_key_path, default_marks=default_marks) if answer_key_path and answer_key_path.exists() else None
    records: list[_PageRecord] = []
    source_counter = 0
    for raw_page in iter_scan_pages(upload_path, 200.0):
        source_counter += 1
        identity_dir = parse_dir / "_student_reuploads" / _safe_id(roll_no) / f"source_{source_counter:04d}"
        aligned = align_scan_page(raw_page, manifest, 200.0, source_index=source_counter)
        identity_kind, detected_roll, program, confidence, identity_payload, flags = _page_identity(
            aligned,
            manifest,
            200.0,
            identity_dir,
            roll_ocr_backend,
            set(students) if students else None,
        )
        if detected_roll and str(detected_roll) != str(roll_no):
            flags.append(
                f"replacement upload was forced to roll {roll_no}, but page identity read {detected_roll}; review manually"
            )
        records.append(
            _PageRecord(
                source_path=upload_path,
                source_index=source_counter,
                aligned_page=aligned,
                identity_kind=identity_kind,
                roll_no=roll_no,
                program=program,
                confidence=confidence,
                identity_payload=identity_payload,
                review_flags=flags,
            )
        )
    if not records:
        raise ValueError("uploaded student sheet had no readable pages")
    result = _write_student_group(
        roll_no,
        records,
        manifest,
        parse_dir,
        students,
        answer_key,
        200.0,
        0.0,
        True,
        roll_ocr_backend,
        None,
    )
    action = "reprocessed" if answer_key is None else "re-evaluated"
    result.setdefault("review_flags", []).append(f"student sheet was re-uploaded and {action} from professor UI")
    _write_json(Path(result["details_path"]), result)
    return result


def _existing_student_records(
    *,
    details: dict[str, Any],
    parse_dir: Path,
    roll_no: str,
    skip_page: int,
) -> list[_PageRecord]:
    details_dir = _resolve_output_path(details.get("details_path"), parse_dir).parent
    program = details.get("student", {}).get("program") or None
    records: list[_PageRecord] = []
    for page in details.get("pages", []):
        page_no = int(page.get("page_index") or page.get("page") or 0)
        if page_no <= 0 or page_no == skip_page:
            continue
        image_path = _resolve_output_path(page.get("canonical_image_path"), details_dir)
        if not image_path.exists():
            continue
        image = np.asarray(Image.open(image_path).convert("L"))
        records.append(
            _PageRecord(
                source_path=image_path,
                source_index=int(page.get("source_index") or (10000 + page_no)),
                aligned_page=AlignedPage(
                    page_index=page_no,
                    source_index=int(page.get("source_index") or (10000 + page_no)),
                    image=image,
                    alignment_confidence=float(page.get("alignment_confidence") or 1.0),
                    page_mark_confidence=float(page.get("page_mark_confidence") or 1.0),
                    debug_image=None,
                ),
                identity_kind="existing_verified_page",
                roll_no=roll_no,
                program=program,
                confidence="high",
                identity_payload=None,
                review_flags=[],
            )
        )
    return records


def _parse_student_page_replacement(
    *,
    upload_path: Path,
    roll_no: str,
    target_page: int,
    current_details: dict[str, Any],
    parse_dir: Path,
    manifest_path: Path,
    students_path: Path | None,
    answer_key_path: Path | None,
    roll_ocr_backend: object | None,
) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    if target_page < 1 or target_page > int(manifest.get("num_pages") or 0):
        raise ValueError(f"page number must be between 1 and {manifest.get('num_pages')}")
    students = load_students(students_path) if students_path and students_path.exists() else None
    default_marks = float(manifest["exam"].get("marks_per_mcq", 1.0))
    answer_key = load_answer_key(answer_key_path, default_marks=default_marks) if answer_key_path and answer_key_path.exists() else None

    replacement_records: list[_PageRecord] = []
    source_counter = 0
    for raw_page in iter_scan_pages(upload_path, 200.0):
        source_counter += 1
        aligned = align_scan_page(raw_page, manifest, 200.0, source_index=source_counter)
        identity_dir = parse_dir / "_student_reuploads" / _safe_id(roll_no) / f"replace_page_{target_page}_{source_counter:04d}"
        identity_kind, detected_roll, program, confidence, identity_payload, flags = _page_identity(
            aligned,
            manifest,
            200.0,
            identity_dir,
            roll_ocr_backend,
            set(students) if students else None,
        )
        if detected_roll and str(detected_roll) != str(roll_no):
            flags.append(
                f"replacement page was forced to roll {roll_no}, but page identity read {detected_roll}; review manually"
            )
        if aligned.page_index != target_page:
            flags.append(
                f"uploaded replacement detected as page {aligned.page_index}, but professor selected page {target_page}; "
                "using it as the selected page"
            )
            aligned = replace(aligned, page_index=target_page)
        replacement_records.append(
            _PageRecord(
                source_path=upload_path,
                source_index=source_counter,
                aligned_page=aligned,
                identity_kind=identity_kind,
                roll_no=roll_no,
                program=program or current_details.get("student", {}).get("program"),
                confidence=confidence,
                identity_payload=identity_payload,
                review_flags=flags,
            )
        )

    if not replacement_records:
        raise ValueError("uploaded replacement page had no readable pages")
    selected_replacement = next(
        (record for record in replacement_records if record.aligned_page.page_index == target_page),
        replacement_records[0],
    )
    records = _existing_student_records(
        details=current_details,
        parse_dir=parse_dir,
        roll_no=roll_no,
        skip_page=target_page,
    )
    records.append(selected_replacement)
    result = _write_student_group(
        roll_no,
        records,
        manifest,
        parse_dir,
        students,
        answer_key,
        200.0,
        0.0,
        True,
        roll_ocr_backend,
        None,
    )
    action = "reprocessed" if answer_key is None else "re-evaluated"
    result.setdefault("review_flags", []).append(
        f"page {target_page} was replaced from professor UI and the student was {action}"
    )
    _write_json(Path(result["details_path"]), result)
    return result


def _commit_student_result(
    *,
    store: "RunStore",
    run_id: str,
    state: dict[str, Any],
    parse_dir: Path,
    index_path: Path,
    roll_no: str,
    result: dict[str, Any],
    roll_ocr_state: dict[str, Any],
) -> None:
    run_dir = store.run_dir(run_id)
    index = _read_json(index_path)
    entry = _student_index_entry(result)
    students = list(index.get("students", []))
    for item_index, student in enumerate(students):
        if str(student.get("roll_no")) == str(roll_no):
            students[item_index] = entry
            break
    else:
        students.append(entry)
    index["students"] = students
    _recalculate_status_counts(index)
    manifest = load_manifest(Path(str(state["inputs"]["manifest_path"])))
    report_paths = _write_review_reports(
        parse_dir,
        manifest,
        index,
        _load_student_results_for_report(index, parse_dir),
        index.get("unmatched_pages", []),
        index.get("page_errors", []),
    )
    index["reports"] = report_paths
    index["review_report_csv_path"] = report_paths["csv"]
    index["review_report_html_path"] = report_paths["html"]
    _write_json(index_path, index)
    _replace_verified_student(parse_dir, roll_no, result)
    marks_csv = None if _is_grouping_only(state) else _write_marks_csv(run_dir, parse_dir, index)
    counts = index.get("status_counts", {})
    store.write_state(
        run_id,
        marks_csv_path=str(marks_csv) if marks_csv else None,
        roll_ocr=roll_ocr_state,
        summary={
            **dict(state.get("summary") or {}),
            "students": len(index.get("students", [])),
            "ready": counts.get("ready", 0),
            "needs_review": counts.get("needs_review", 0),
            "unmatched_pages": counts.get("unmatched_pages", 0),
            "page_errors": counts.get("page_errors", 0),
            "roster_missing": counts.get("roster_missing", 0),
        },
    )


def _digits_for_roll_feedback(roll_no: str, cell_count: int) -> str:
    digits = "".join(ch for ch in str(roll_no).upper() if ch.isdigit())
    if len(digits) < cell_count:
        raise ValueError(f"correct roll {roll_no!r} has only {len(digits)} digit(s), but this field needs {cell_count}")
    return digits[-cell_count:]


def _append_roll_feedback_rows(feedback_dir: Path, rows: list[dict[str, Any]]) -> None:
    labels_path = feedback_dir / "labels.csv"
    train_path = feedback_dir / "train.csv"
    fieldnames = [
        "image_path",
        "label",
        "roll_no",
        "program",
        "page_index",
        "cell_index",
        "run_id",
        "old_roll_no",
        "source_crop_path",
        "created_at",
    ]
    for path in (labels_path, train_path):
        write_header = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerows(rows)


def _save_roll_correction_feedback(
    *,
    data_dir: Path,
    run_id: str,
    roll_no: str,
    details: dict[str, Any],
    parse_dir: Path,
    manifest_path: Path,
    correct_roll_no: str,
    program: str,
    page_index: int,
) -> tuple[int, Path]:
    manifest = load_manifest(manifest_path)
    details_dir = _resolve_output_path(details.get("details_path"), parse_dir).parent
    page = next(
        (
            item
            for item in details.get("pages", [])
            if int(item.get("page_index") or item.get("page") or 0) == int(page_index)
        ),
        None,
    )
    if page is None:
        raise ValueError(f"student has no parsed page {page_index} to save roll feedback from")
    image_path = _resolve_output_path(page.get("canonical_image_path"), details_dir)
    if not image_path.exists():
        raise FileNotFoundError(f"canonical page image not found: {image_path}")

    feedback_dir = data_dir / ROLL_FEEDBACK_DIR_NAME
    work_dir = feedback_dir / "source_crops" / _safe_id(run_id) / _safe_id(roll_no) / f"p{int(page_index)}"
    image = np.asarray(Image.open(image_path).convert("L"))
    _crop_paths, cell_crop_paths = save_roll_number_crop_sets(
        image,
        manifest,
        int(page_index),
        work_dir,
        200.0,
    )

    program = program.upper().strip()
    cells = [Path(path) for path in cell_crop_paths.get(program, [])]
    if not cells:
        available = ", ".join(sorted(cell_crop_paths)) or "none"
        raise ValueError(f"no roll cell crops found for program {program} on page {page_index}; available: {available}")

    digits = _digits_for_roll_feedback(correct_roll_no, len(cells))
    images_dir = feedback_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    created_at = _now()
    for index, (cell_path, label) in enumerate(zip(cells, digits, strict=True), start=1):
        target_name = (
            f"{_safe_id(run_id)}__old_{_safe_id(roll_no)}__correct_{_safe_id(correct_roll_no)}"
            f"__p{int(page_index)}__c{index}__label_{label}__{uuid.uuid4().hex[:8]}.png"
        )
        target_path = images_dir / target_name
        shutil.copy2(cell_path, target_path)
        rows.append(
            {
                "image_path": _rel(target_path, feedback_dir),
                "label": label,
                "roll_no": correct_roll_no,
                "program": program,
                "page_index": int(page_index),
                "cell_index": index,
                "run_id": run_id,
                "old_roll_no": roll_no,
                "source_crop_path": str(cell_path),
                "created_at": created_at,
            }
        )
    _append_roll_feedback_rows(feedback_dir, rows)
    return len(rows), feedback_dir


@dataclass(frozen=True)
class UiConfig:
    data_dir: Path = DEFAULT_DATA_DIR

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / RUNS_DIR_NAME


class RunStore:
    def __init__(self, config: UiConfig) -> None:
        self.config = config
        self.config.runs_dir.mkdir(parents=True, exist_ok=True)
        self._state_lock = threading.RLock()
        self._review_lock = threading.RLock()
        self._worker_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        for state in self.list_runs():
            if state.get("status") == "inspecting":
                self.write_state(
                    state["run_id"],
                    status="inspection_interrupted",
                    stage="Inspection interrupted",
                )
            elif state.get("status") in {"queued", "running", "grading"}:
                self.write_state(state["run_id"], status="failed", stage="Operation interrupted; saved artifacts retained")

    def run_dir(self, run_id: str) -> Path:
        return self.config.runs_dir / _safe_id(run_id)

    def state_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / RUN_STATE_NAME

    def read_state(self, run_id: str) -> dict[str, Any]:
        return _read_json(self.state_path(run_id))

    def write_state(self, run_id: str, **updates: Any) -> dict[str, Any]:
        with self._state_lock:
            path = self.state_path(run_id)
            state = _read_json(path) if path.exists() else {}
            state.update(updates)
            state["updated_at"] = _now()
            _write_json(path, state)
            return state

    def list_runs(self) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        for path in sorted(self.config.runs_dir.iterdir(), key=lambda item: item.name, reverse=True):
            state_path = path / RUN_STATE_NAME
            if state_path.exists():
                try:
                    runs.append(_read_json(state_path))
                except json.JSONDecodeError:
                    continue
        return runs

    def create_run(
        self,
        exam_id: str,
        files: dict[str, UploadedFile],
        fields: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self._worker_lock:
            return self._create_run(exam_id, files, fields)

    def _create_run(
        self,
        exam_id: str,
        files: dict[str, UploadedFile],
        fields: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        fields = fields or {}
        if self._worker and self._worker.is_alive():
            raise ValueError("An operation is running. Wait for it to finish before uploading another run.")
        manifest_upload = files.get("manifest")
        if not manifest_upload or not manifest_upload.path.is_file():
            raise ValueError("manifest.json is required")
        manifest = load_manifest(manifest_upload.path)
        manifest_exam_id = str(manifest["exam_id"]).strip()
        if exam_id and exam_id != manifest_exam_id:
            raise ValueError(f"Exam ID must match the manifest: {manifest_exam_id}")
        exam_id = exam_id or manifest_exam_id
        run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_id = _safe_id(f"{exam_id}_{run_stamp}_{uuid.uuid4().hex[:6]}")
        run_dir = self.run_dir(run_id)
        inputs = run_dir / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)

        saved: dict[str, str | None] = {}
        for field, upload in files.items():
            if not upload.filename or not upload.path.exists() or upload.path.stat().st_size == 0:
                saved[field] = None
                continue
            target = inputs / _safe_id(field) / _safe_id(Path(upload.filename).name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(upload.path, target)
            saved[field] = str(target)

        manifest_path = Path(str(saved.get("manifest") or ""))
        if not manifest_path.is_file():
            raise ValueError("manifest.json is required")
        scan_path = Path(str(saved.get("scan_pdf") or ""))
        if not scan_path.is_file():
            raise ValueError("student OMR PDF/image is required")

        manifest = load_manifest(manifest_path)
        manifest_exam_id = str(manifest.get("exam_id") or exam_id).strip()
        if exam_id and exam_id != manifest_exam_id:
            raise ValueError(f"Exam ID must match the manifest: {manifest_exam_id}")
        inspection.create_inventory(run_dir, scan_path, manifest_path)

        two_step = fields.get("workflow") == "two_step"
        grouping_only = two_step or fields.get("grouping_only") == "1"
        answer_key_csv = None
        if not grouping_only and saved.get("answer_key"):
            answer_key_csv = inputs / "answer_key.csv"
            _normalize_tabular_upload(Path(str(saved["answer_key"])), answer_key_csv)
        elif not grouping_only:
            answer_key_csv = _write_answer_key_from_form(manifest, fields, inputs / "answer_key.csv")

        students_csv = None
        roster_summary = None
        if saved.get("master_list"):
            students_csv = inputs / "students.csv"
            _normalize_tabular_upload(Path(str(saved["master_list"])), students_csv)
            roster_summary = _canonicalize_roster(students_csv)

        state = {
            "run_id": run_id,
            "exam_id": exam_id or manifest_exam_id,
            "manifest_exam_id": manifest_exam_id,
            "status": "inspection_pending",
            "stage": "Awaiting inspection",
            "created_at": _now(),
            "updated_at": _now(),
            "inputs": {
                "manifest_path": str(manifest_path),
                "scan_path": str(scan_path),
                "grouping_only": grouping_only,
                "workflow": "two_step" if two_step else "legacy",
                "answer_key_path": str(answer_key_csv) if answer_key_csv else None,
                "students_path": str(students_csv) if students_csv else None,
                "original_answer_key_upload": saved.get("answer_key"),
                "original_master_list_upload": saved.get("master_list"),
            },
            "roster_summary": roster_summary,
            "parse_dir": None,
            "parse_index_path": None,
            "marks_csv_path": None,
            "error": None,
        }
        _write_json(self.state_path(run_id), state)
        return state

    def inspection_index(self, run_id: str) -> dict[str, Any]:
        state = self.read_state(run_id)
        run_dir = self.run_dir(run_id)
        with self._state_lock:
            if not (run_dir / inspection.INDEX_NAME).exists():
                if self._worker and self._worker.is_alive():
                    raise ValueError("Wait for the active operation to finish before indexing a legacy run.")
                inspection.create_inventory(
                    run_dir,
                    Path(state["inputs"]["scan_path"]),
                    Path(state["inputs"]["manifest_path"]),
                )
            return inspection.load_inventory(run_dir)

    def start_run(self, run_id: str, *, evaluate: bool = False) -> None:
        with self._worker_lock:
            if self._worker and self._worker.is_alive():
                raise ValueError("Another operation is running. Wait for it to finish before starting this run.")
            state = self.read_state(run_id)
            index = self.inspection_index(run_id)
            inputs = state["inputs"]
            if (
                inspection.fingerprint(Path(inputs["scan_path"])) != index["source_sha256"]
                or inspection.fingerprint(Path(inputs["manifest_path"])) != index["manifest_sha256"]
            ):
                raise ValueError("Run inputs have changed. Create a new run.")
            if evaluate:
                if state.get("parse_index_path") or (self.run_dir(run_id) / "parsed").exists():
                    raise ValueError(
                        "This run already has evaluation artifacts. Create a new run to evaluate again; "
                        "reviews are preserved."
                    )
                if index["status"] != "completed" or not (
                    index["counts"]["aligned"] + index["counts"]["needs_review"]
                ):
                    raise ValueError("Complete page inspection with at least one aligned page before evaluation.")
            target = self._run_batch if evaluate else self._run_inspection
            self.write_state(
                run_id,
                status="queued" if evaluate else "inspecting",
                stage="Queued",
                progress=None,
            )
            self._worker = threading.Thread(target=target, args=(run_id,), daemon=True)
            self._worker.start()

    def start_identity_preview(self, run_id: str) -> None:
        """Read literal page identities after inspection, without grouping or grading."""
        with self._worker_lock:
            if self._worker and self._worker.is_alive():
                raise ValueError("Another operation is running. Wait for it to finish before starting this run.")
            state = self.read_state(run_id)
            index = self.inspection_index(run_id)
            if index["status"] != "completed":
                raise ValueError("Complete page inspection before previewing roll detection.")
            self.write_state(run_id, status="identity_previewing", stage="Preparing roll detection preview", error=None)
            self._worker = threading.Thread(target=self._run_identity_preview, args=(run_id,), daemon=True)
            self._worker.start()

    def start_matching(self, run_id: str) -> None:
        """One worker owns alignment, cached roll detection, and exact grouping."""
        with self._worker_lock:
            if self._worker and self._worker.is_alive():
                raise ValueError("An operation is already running")
            state = self.read_state(run_id)
            inventory = self.inspection_index(run_id)
            inputs = state["inputs"]
            if (inspection.fingerprint(Path(inputs["scan_path"])) != inventory["source_sha256"]
                    or inspection.fingerprint(Path(inputs["manifest_path"])) != inventory["manifest_sha256"]):
                raise ValueError("Run inputs changed; create a new run")
            if state.get("parse_index_path") or (self.run_dir(run_id) / "parsed").exists():
                raise ValueError("Sheets have already been matched; use their existing review results")
            self.write_state(run_id, status="queued", stage="Preparing roll detection and grouping", error=None)
            self._worker = threading.Thread(target=self._run_matching, args=(run_id,), daemon=True)
            self._worker.start()

    def _run_matching(self, run_id: str) -> None:
        try:
            inventory = self.inspection_index(run_id)
            if inventory["status"] != "completed":
                self.write_state(run_id, status="inspecting", stage="Aligning source pages")
                self._run_inspection(run_id)
                inventory = self.inspection_index(run_id)
            if inventory["status"] != "completed":
                return
            if not inventory["counts"]["aligned"] + inventory["counts"]["needs_review"]:
                raise ValueError("No source pages could be aligned; inspect the failed pages")
            self._run_batch(run_id)
        except Exception as exc:
            self.write_state(run_id, status="failed", stage="Matching failed", error=f"{type(exc).__name__}: {exc}")

    def start_grading(self, run_id: str, answer_key_path: Path | None) -> None:
        with self._worker_lock:
            if self._worker and self._worker.is_alive():
                raise ValueError("Wait for the current operation to finish")
            state = self.read_state(run_id)
            if not state.get("parse_index_path"):
                raise ValueError("Detect rolls and group sheets before grading")
            manifest = load_manifest(state["inputs"]["manifest_path"])
            key = load_answer_key(answer_key_path, default_marks=float(manifest["exam"].get("marks_per_mcq", 1))) if answer_key_path else None
            validate_grading_key(manifest, key)
            self.write_state(run_id, status="grading", stage="Preparing grading from selected sheets", error=None)
            self._worker = threading.Thread(target=self._run_grading, args=(run_id, answer_key_path), daemon=True)
            self._worker.start()

    def _run_grading(self, run_id: str, answer_key_path: Path | None) -> None:
        try:
            with self._review_lock:
                state = self.read_state(run_id)
                inventory = self.inspection_index(run_id)
                def progress(values: dict) -> None:
                    self.write_state(run_id, stage=f"Grading student {values['processed']}/{values['total']}",
                                     progress={"phase": "grading", **values})
                summary = grade_selected_sheets(state["parse_dir"], state["inputs"]["manifest_path"], answer_key_path,
                                                dpi=float(inventory["dpi"]), progress=progress)
                inputs = {**state["inputs"], "grouping_only": False,
                          "answer_key_path": str(answer_key_path) if answer_key_path else None}
                index, _path = load_or_initialize_verified_index(state["parse_dir"])
                marks = _write_marks_csv(self.run_dir(run_id), Path(state["parse_dir"]), index)
                self.write_state(run_id, status="completed", stage="Grading complete", inputs=inputs,
                                 grading_summary=summary, marks_csv_path=str(marks), error=None)
        except Exception as exc:
            self.write_state(run_id, status="grading_failed", stage="Grading failed; grouping retained",
                             error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())

    def assign_source(self, run_id: str, source_index: int, roll_no: str, **options: Any) -> None:
        with self._worker_lock, self._state_lock:
            if self._worker and self._worker.is_alive():
                raise ValueError("Wait for matching to finish before correcting page ownership.")
            state = self.read_state(run_id)
            if not state.get("parse_index_path"):
                raise ValueError("Match sheets before assigning page ownership.")
            assign_source_page(state["parse_dir"], source_index, roll_no, reviewer="professor-ui", **options)

    def restart_evaluation(self, run_id: str) -> None:
        """Archive prior evaluation output and reuse immutable inspection artifacts."""
        with self._worker_lock:
            if self._worker and self._worker.is_alive():
                raise ValueError("Another operation is running. Wait for it to finish before re-running evaluation.")
            state = self.read_state(run_id)
            run_dir = self.run_dir(run_id)
            parsed_root = run_dir / "parsed"
            if not state.get("parse_index_path") or not parsed_root.is_dir():
                raise ValueError("There is no completed evaluation to re-run.")
            archive_root = run_dir / "history"
            archive_root.mkdir(parents=True, exist_ok=True)
            archive_name = "parsed_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            archive_path = archive_root / archive_name
            if archive_path.exists():
                raise ValueError("A previous evaluation archive already uses this timestamp; try again.")
            shutil.move(str(parsed_root), str(archive_path))
            self.write_state(
                run_id,
                status="identity_previewed",
                stage="Previous evaluation archived; ready to re-run grouping",
                parse_dir=None,
                parse_index_path=None,
                marks_csv_path=None,
                summary=None,
                previous_evaluation_path=str(archive_path),
                error=None,
            )
        self.start_run(run_id, evaluate=True)

    def assign_inspection_page(self, run_id: str, source_index: int, page_index: int) -> dict[str, Any]:
        """Apply a human-confirmed template page number before evaluation."""
        with self._worker_lock:
            if self._worker and self._worker.is_alive():
                raise ValueError("Wait for the active operation to finish before correcting a page.")
            state = self.read_state(run_id)
            run_dir = self.run_dir(run_id)
            if state.get("parse_index_path") or (run_dir / "parsed").exists():
                raise ValueError("Page numbers cannot be changed after evaluation. Create a new run instead.")
            inputs = state["inputs"]
            with self._state_lock:
                record = inspection.assign_page_index(
                    run_dir,
                    Path(inputs["scan_path"]),
                    Path(inputs["manifest_path"]),
                    source_index=source_index,
                    page_index=page_index,
                )
                index = inspection.load_inventory(run_dir)
                self.write_state(
                    run_id,
                    status="inspected" if index["status"] == "completed" else "inspection_interrupted",
                    stage=f"Source page {source_index} assigned as sheet page {page_index}",
                    error=None,
                )
                return record

    def _run_inspection(self, run_id: str) -> None:
        state = self.read_state(run_id)
        try:
            def progress(index: dict[str, Any]) -> None:
                self.write_state(
                    run_id,
                    stage=f"Inspecting pages: {index['processed']}/{index['total']}",
                )

            inspection.inspect_pages(
                self.run_dir(run_id),
                Path(state["inputs"]["scan_path"]),
                Path(state["inputs"]["manifest_path"]),
                progress=progress,
            )
            self.write_state(
                run_id,
                status="completed" if state.get("parse_index_path") else "inspected",
                stage="Inspection complete",
                error=None,
            )
        except Exception as exc:
            index = inspection.load_inventory(self.run_dir(run_id))
            index.update(status="interrupted", error=f"{type(exc).__name__}: {exc}")
            inspection.save_inventory(self.run_dir(run_id), index)
            self.write_state(
                run_id,
                status="inspection_interrupted",
                stage="Inspection interrupted",
                error=index["error"],
            )

    def _read_identity_pages(self, run_id: str, backend: object, backend_state: dict,
                             pages: dict[int, AlignedPage], unavailable: dict[int, str]) -> dict[int, dict]:
        state = self.read_state(run_id)
        inputs = state["inputs"]
        run_dir = self.run_dir(run_id)
        manifest = load_manifest(inputs["manifest_path"])
        students = load_students(inputs["students_path"]) if inputs.get("students_path") else None
        valid_rolls = set(students) if students else None
        inventory = self.inspection_index(run_id)
        dpi = float(inventory["dpi"])
        context = identity_cache.identity_context(inputs, backend_state, dpi)
        preview_path = run_dir / "identity_preview" / "index.json"
        preview = {
            "version": 2, "status": "running", "created_at": _now(),
            "context": context, "policy": "literal identity evidence; ownership requires matching and review",
            "counts": {}, "pages": [], "cache_hits": 0, "ocr_pages": 0,
        }
        inspection.write_json_atomic(preview_path, preview)
        self.write_state(run_id, identity_preview_path=str(preview_path), roll_ocr=backend_state)
        identities = {}
        for record in inventory["pages"]:
            source_index = int(record["source_index"])
            aligned = pages.get(source_index)
            if aligned is None:
                row = {"source_index": source_index, "status": "unavailable",
                       "reason": unavailable.get(source_index)}
            else:
                image_hash = inspection.fingerprint(run_dir / record["aligned"])
                row = identity_cache.load_page_evidence(
                    run_dir, source_index, aligned.page_index, image_hash, context,
                )
                if row is None:
                    kind, roll_no, program, confidence, payload, flags = _page_identity(
                        aligned, manifest, dpi,
                        run_dir / "identity_preview" / f"source_{source_index:04d}", backend, valid_rolls,
                    )
                    row = {
                        "source_index": source_index, "sheet_page": aligned.page_index,
                        "identity_kind": kind, "literal_roll_no": roll_no, "program": program,
                        "confidence": confidence, "roster_member": bool(valid_rolls and roll_no in valid_rolls),
                        "status": _identity_preview_status(roll_no, confidence, flags),
                        "review_flags": flags, "identity": identity_cache.relative_evidence(payload, run_dir),
                    }
                    identity_cache.save_page_evidence(run_dir, row, image_hash, context)
                    preview["ocr_pages"] += 1
                else:
                    preview["cache_hits"] += 1
                identities[source_index] = {
                    **row, "identity": identity_cache.absolute_evidence(row.get("identity"), run_dir),
                }
            preview["pages"].append(row)
            _refresh_identity_preview_counts(preview)
            inspection.write_json_atomic(preview_path, preview)
            self.write_state(
                run_id, stage=f"Reading rolls: {source_index}/{inventory['total']}",
                progress={"phase": "reading_rolls", "processed": source_index, "total": inventory["total"],
                          "reused": preview["cache_hits"], "ocr_pages": preview["ocr_pages"]},
            )
        preview["status"] = "completed"
        inspection.write_json_atomic(preview_path, preview)
        return identities

    def _run_identity_preview(self, run_id: str) -> None:
        """Produce review evidence only; this cannot assign page ownership."""
        try:
            state = self.read_state(run_id)
            run_dir = self.run_dir(run_id)
            backend, backend_state = _require_ui_roll_ocr_backend()
            self.write_state(run_id, roll_ocr=backend_state, stage="Loading inspected pages for roll preview")
            pages, unavailable = _load_inspected_pages(run_dir)
            self._read_identity_pages(run_id, backend, backend_state, pages, unavailable)
            preview_path = run_dir / "identity_preview" / "index.json"
            existing_evaluation = bool(state.get("parse_index_path"))
            self.write_state(
                run_id,
                status="completed" if existing_evaluation else "identity_previewed",
                stage="Roll detection preview refreshed; existing evaluation retained" if existing_evaluation else "Roll detection preview complete",
                identity_preview_path=str(preview_path),
                error=None,
            )
        except Exception as exc:
            self.write_state(
                run_id,
                status="identity_preview_failed",
                stage="Roll detection preview failed",
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
            )

    def _run_batch(self, run_id: str) -> None:
        try:
            state = self.write_state(run_id, status="running", stage="Loading inputs", error=None)
            inputs = state["inputs"]
            run_dir = self.run_dir(run_id)
            parsed_root = run_dir / "parsed"
            self.write_state(run_id, stage="Preparing roll OCR cross-check")
            roll_ocr_backend, roll_ocr_state = _require_ui_roll_ocr_backend()
            self.write_state(run_id, roll_ocr=roll_ocr_state)
            self.write_state(run_id, stage="Loading inspected aligned pages")
            prealigned_pages, prealignment_errors = _load_inspected_pages(run_dir)
            precomputed_identities = self._read_identity_pages(
                run_id, roll_ocr_backend, roll_ocr_state, prealigned_pages, prealignment_errors,
            )
            self.write_state(run_id, stage="Matching sheets from saved roll evidence")

            def update_progress(progress: dict[str, Any]) -> None:
                phase = str(progress.get("phase") or "")
                processed = int(progress.get("processed") or 0)
                total = int(progress.get("total") or 0)
                errors = int(progress.get("errors") or 0)
                if phase == "reading_pages":
                    stage = f"Matching inspected page {processed}/{total}" if total else f"Matching page {processed}"
                else:
                    stage = f"Writing student bundle {processed}/{total}" if total else "Writing student bundles"
                if errors:
                    stage += f" ({errors} page error{'s' if errors != 1 else ''})"
                self.write_state(run_id, stage=stage, progress=progress)

            results, index_path = parse_exam_bundle(
                scans_path=inputs["scan_path"],
                manifest_path=inputs["manifest_path"],
                students_path=inputs.get("students_path"),
                answer_key_path=inputs.get("answer_key_path"),
                output_root=parsed_root,
                dpi=200.0,
                course_id=state["exam_id"],
                min_group_confidence="high",
                grouping_mode="auto",
                ocr_backend=roll_ocr_backend,
                progress_callback=update_progress,
                prealigned_pages=prealigned_pages,
                prealignment_errors=prealignment_errors,
                precomputed_identities=precomputed_identities,
                grouping_only=_is_grouping_only(state),
            )
            parse_dir = index_path.parent
            self.write_state(run_id, stage="Preparing review dashboard")
            initialize_verification_index(parse_dir, force=True)
            index = _read_json(index_path)
            marks_csv = None if _is_grouping_only(state) else _write_marks_csv(run_dir, parse_dir, index)
            counts = index.get("status_counts", {})
            self.write_state(
                run_id,
                status="completed",
                stage="Completed",
                parse_dir=str(parse_dir),
                parse_index_path=str(index_path),
                marks_csv_path=str(marks_csv) if marks_csv else None,
                summary={
                    "students": len(index.get("students", [])),
                    "ready": counts.get("ready", 0),
                    "needs_review": counts.get("needs_review", 0),
                    "unmatched_pages": counts.get("unmatched_pages", 0),
                    "page_errors": counts.get("page_errors", 0),
                    "roster_missing": counts.get("roster_missing", 0),
                    "roll_ocr_provider": roll_ocr_state.get("provider"),
                },
            )
        except Exception as exc:  # pragma: no cover - exercised by manual workflows.
            self.write_state(
                run_id,
                status="failed",
                stage="Failed",
                error=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(),
            )


class SmartOmrUiHandler(BaseHTTPRequestHandler):
    store: RunStore

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write("SmartOMR UI: " + (format % args) + "\n")

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, title: str, body: str, *, status: HTTPStatus = HTTPStatus.OK, refresh: int | None = None) -> None:
        if getattr(self, "_view_revision", None):
            endpoint, revision = self._view_revision
            body += (
                f'<div id="review-sync" data-endpoint="{html.escape(endpoint)}" data-revision="{revision}" '
                'class="review-sync" role="status" hidden><span></span> '
                '<button type="button" class="secondary">Refresh</button></div>'
                '<script src="/static/review_sync.js" defer></script>'
            )
        payload = _html_page(title, body, refresh_seconds=refresh)
        self.send_response(status.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER.value)
        self.send_header("Location", location)
        self.end_headers()

    def _not_found(self, message: str = "Not found") -> None:
        self._send_html("Not Found", f"<h1>Not Found</h1><p>{html.escape(message)}</p>", status=HTTPStatus.NOT_FOUND)

    def _bad_request(self, message: str) -> None:
        self._send_html("Bad Request", f"<h1>Bad Request</h1><p>{html.escape(message)}</p>", status=HTTPStatus.BAD_REQUEST)

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path in {"/", "/review-state.json"} or re.fullmatch(
            r"/runs/[^/]+(?:/pages|/review|/email|/inspection.json|/review-state.json|/students/[^/]+(?:/preview.pdf)?)?", path
        ):
            with self.store._review_lock:
                self._handle_get()
        else:
            self._handle_get()

    def _handle_get(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        self._view_revision = None
        try:
            if path == "/" or re.fullmatch(r"/runs/[^/]+(?:/review|/email|/students/[^/]+)?", path):
                run_id = path.split("/")[2] if path.startswith("/runs/") else None
                endpoint = f"/runs/{run_id}/review-state.json" if run_id else "/review-state.json"
                self._view_revision = (endpoint, self._review_revision(run_id))
            if path == "/":
                self._page_runs()
            elif path == "/review-state.json":
                self._send_json({"revision": self._review_revision()})
            elif path == "/new":
                self._page_new()
            elif path == "/generate":
                self._page_generate()
            elif path == "/artifact":
                self._serve_artifact(query)
            elif path.startswith("/static/"):
                self._serve_static(path.removeprefix("/static/"))
            elif path.startswith("/runs/"):
                parts = [part for part in path.split("/") if part]
                if len(parts) == 2:
                    self._page_run(parts[1])
                elif len(parts) == 3 and parts[2] == "pages":
                    state = self.store.read_state(parts[1])
                    self.store.inspection_index(parts[1])
                    self._send_html("Source Pages", inspection_body(state))
                elif len(parts) == 3 and parts[2] == "grade":
                    self._page_grade(parts[1])
                elif len(parts) == 3 and parts[2] == "inspection.json":
                    self._inspection_json(parts[1], query)
                elif len(parts) == 3 and parts[2] == "review-state.json":
                    state = self.store.read_state(parts[1])
                    index = self._current_review(state)
                    self._send_json({"revision": self._review_revision(parts[1]),
                                     "summary": _review_summary(index) if index is not None else {},
                                     "status": state.get("status")})
                elif len(parts) == 3 and parts[2] == "identity-asset":
                    self._identity_asset(parts[1], query)
                elif len(parts) == 5 and parts[2] == "pages":
                    image_path = inspection.inspection_asset(
                        self.store.run_dir(parts[1]),
                        int(parts[3]),
                        parts[4],
                    )
                    if image_path and image_path.is_file():
                        self._send_file(
                            image_path,
                            "application/json" if parts[4] == "report" else "image/png",
                        )
                    else:
                        self._not_found("Page image is not available")
                elif len(parts) == 4 and parts[2] == "students":
                    self._page_student(parts[1], urllib.parse.unquote(parts[3]), query)
                elif len(parts) == 5 and parts[2] == "students" and parts[4] == "preview.pdf":
                    self._student_preview_pdf(parts[1], urllib.parse.unquote(parts[3]))
                elif len(parts) == 3 and parts[2] == "review":
                    self._page_review(parts[1])
                elif len(parts) == 3 and parts[2] == "email":
                    self._page_email(parts[1])
                else:
                    self._not_found()
            else:
                self._not_found()
        except FileNotFoundError:
            self._not_found()
        except ValueError as exc:
            self._bad_request(str(exc))
        except Exception as exc:
            body = f"<h1>UI Error</h1><p>{html.escape(str(exc))}</p><pre>{html.escape(traceback.format_exc())}</pre>"
            self._send_html("Error", body, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        if re.search(r"/(students/|unmatched/|suggestions/|pages/\d+/assign|email/(prepare|send)|reassess-ownership|approve-clean)", self.path):
            with self.store._review_lock:
                self._handle_post()
        else:
            self._handle_post()

    def _handle_post(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/runs":
                self._create_run()
            elif path == "/generate":
                self._generate_omr()
            elif path.startswith("/runs/"):
                parts = [part for part in path.split("/") if part]
                if len(parts) == 3 and parts[2] == "match":
                    self.store.start_matching(parts[1])
                    self._redirect(f"/runs/{parts[1]}/pages?matching=1")
                elif len(parts) == 3 and parts[2] == "grade":
                    self._start_grading(parts[1])
                elif len(parts) == 3 and parts[2] in {"inspect", "evaluate"}:
                    self.store.start_run(parts[1], evaluate=parts[2] == "evaluate")
                    self._redirect(f"/runs/{parts[1]}/pages" + ("?matching=1" if parts[2] == "evaluate" else ""))
                elif len(parts) == 3 and parts[2] in {"reassess-ownership", "approve-clean"}:
                    self._ownership_action(parts[1], parts[2])
                elif len(parts) == 3 and parts[2] == "identity-preview":
                    self.store.start_identity_preview(parts[1])
                    self._redirect(f"/runs/{parts[1]}/pages")
                elif len(parts) == 3 and parts[2] == "re-evaluate":
                    self.store.restart_evaluation(parts[1])
                    self._redirect(f"/runs/{parts[1]}/pages?matching=1")
                elif len(parts) == 5 and parts[2] == "pages" and parts[4] == "page-index":
                    self._assign_page_index(parts[1], int(parts[3]))
                elif len(parts) == 5 and parts[2] == "pages" and parts[4] == "assign":
                    self._assign_source_page(parts[1], int(parts[3]))
                elif len(parts) == 5 and parts[2] == "students" and parts[4] in {"verify", "hold", "reject"}:
                    self._student_decision(parts[1], urllib.parse.unquote(parts[3]), parts[4])
                elif len(parts) == 5 and parts[2] == "students" and parts[4] == "marks":
                    self._update_student_marks(parts[1], urllib.parse.unquote(parts[3]))
                elif len(parts) == 5 and parts[2] == "students" and parts[4] == "correct-roll":
                    self._correct_student_roll_feedback(parts[1], urllib.parse.unquote(parts[3]))
                elif len(parts) == 5 and parts[2] == "students" and parts[4] == "reupload":
                    self._reupload_student_sheet(parts[1], urllib.parse.unquote(parts[3]))
                elif len(parts) == 5 and parts[2] == "students" and parts[4] == "replace-page":
                    self._replace_student_page(parts[1], urllib.parse.unquote(parts[3]))
                elif len(parts) == 5 and parts[2] == "unmatched" and parts[4] in {"assign", "ignore"}:
                    self._unmatched_decision(parts[1], int(parts[3]), parts[4])
                elif len(parts) == 5 and parts[2] == "suggestions" and parts[4] in {"approve", "assign", "reject"}:
                    self._identity_suggestion_decision(
                        parts[1],
                        urllib.parse.unquote(parts[3]),
                        parts[4],
                    )
                elif len(parts) == 4 and parts[2] == "email" and parts[3] == "prepare":
                    self._prepare_email(parts[1])
                elif len(parts) == 4 and parts[2] == "email" and parts[3] == "send":
                    self._send_email(parts[1])
                else:
                    self._not_found()
            else:
                self._not_found()
        except Exception as exc:
            if self.headers.get("Accept") == "application/json":
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            else:
                self._bad_request(str(exc))

    def _create_run(self) -> None:
        staging_dir = self.store.config.runs_dir / "_upload_staging" / uuid.uuid4().hex
        try:
            fields, files = parse_multipart_upload(
                self.rfile,
                content_type=self.headers.get("content-type", ""),
                content_length=int(self.headers.get("content-length", 0)),
                staging_dir=staging_dir,
            )
            exam_id = str(fields.get("exam_id") or "").strip()
            state = self.store.create_run(exam_id, files, fields)
            try:
                if state["inputs"].get("workflow") == "two_step":
                    self.store.start_matching(state["run_id"])
                else:
                    self.store.start_run(state["run_id"])
            except Exception as exc:
                self.store.write_state(
                    state["run_id"],
                    status="inspection_interrupted",
                    stage="Inspection could not start",
                    error=f"{type(exc).__name__}: {exc}",
                )
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
        suffix = "?matching=1" if state["inputs"].get("workflow") == "two_step" else ""
        self._redirect(f"/runs/{state['run_id']}/pages{suffix}")

    def _send_file(self, path: Path, content_type: str) -> None:
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with path.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

    def _serve_static(self, relative: str) -> None:
        root = Path(__file__).parent / "static"
        path = (root / relative).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            self._not_found()
            return
        # Windows can register .js as text/plain. With our nosniff header that
        # prevents browsers from executing the inspection client altogether.
        content_types = {
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }
        self._send_file(
            path,
            content_types.get(path.suffix.lower())
            or mimetypes.guess_type(str(path))[0]
            or "application/octet-stream",
        )

    def _current_review(self, state: dict[str, Any]) -> dict[str, Any] | None:
        if not state.get("parse_index_path"):
            return None
        return load_or_initialize_verified_index(Path(state["parse_dir"]))[0]

    def _review_revision(self, run_id: str | None = None) -> str:
        states = [self.store.read_state(run_id)] if run_id else self.store.list_runs()
        stamps = []
        for state in states:
            run_dir = self.store.run_dir(state["run_id"])
            paths = [run_dir / RUN_STATE_NAME]
            if state.get("parse_dir"):
                parse_dir = Path(state["parse_dir"])
                paths.extend([parse_dir / "verified_index.json", parse_dir / "parse_index.json",
                              parse_dir / "email_release" / EMAIL_QUEUE_CSV,
                              parse_dir / "email_release" / EMAIL_LOG_CSV])
            for path in paths:
                try:
                    stat = path.stat()
                    stamps.append((str(path), stat.st_mtime_ns, stat.st_size))
                except FileNotFoundError:
                    stamps.append((str(path), None, None))
        return hashlib.sha256(json.dumps(stamps, sort_keys=True).encode()).hexdigest()

    def _inspection_json(self, run_id: str, query: dict[str, list[str]] | None = None) -> None:
        state = self.store.read_state(run_id)
        index = self.store.inspection_index(run_id)
        preview_path = Path(str(state.get("identity_preview_path") or ""))
        preview = _read_json(preview_path) if preview_path.is_file() else None
        if preview is not None:
            _refresh_identity_preview_counts(preview)
            selected_source = (query or {}).get("source")
            if selected_source:
                source_index = max(1, min(int(index["total"]), int(selected_source[0])))
                preview["pages"] = [
                    row if int(row["source_index"]) == source_index else
                    {key: value for key, value in row.items() if key != "identity"}
                    for row in preview.get("pages", [])
                ]
        index["run_status"] = state["status"]
        index["stage"] = state.get("stage")
        index["run_error"] = state.get("error")
        index["evaluated"] = bool(state.get("parse_index_path"))
        index["identity_preview"] = preview
        index["operation_progress"] = state.get("progress")
        index["two_step"] = state.get("inputs", {}).get("workflow") == "two_step"
        index["can_assign"] = bool(index["evaluated"] and state["status"] == "completed")
        index["review_rolls"] = []
        if index["evaluated"]:
            with self.store._state_lock:
                verified, _path = load_or_initialize_verified_index(Path(state["parse_dir"]))
            index["review_summary"] = _review_summary(verified)
            ownership = {}
            for student in verified.get("students", []):
                for page in selected_student_pages(student):
                    ownership[int(page["source_index"])] = {
                        "roll_no": student["roll_no"], "sheet_page": page["page"],
                        "status": student["status"], "student_name": student.get("student_name"),
                    }
            unmatched = {int(page["source_index"]): page for page in verified.get("unmatched_pages", [])}
            for page in index["pages"]:
                source = int(page["source_index"])
                page["assignment"] = ownership.get(source) or {
                    "status": unmatched.get(source, {}).get("status", "unavailable"), "roll_no": None,
                }
            index["review_rolls"] = [
                {"roll_no": student["roll_no"], "name": student.get("student_name") or ""}
                for student in verified.get("students", [])
            ] + [
                {"roll_no": student["roll_no"], "name": student.get("student_name") or ""}
                for student in _missing_roster_students(verified)
            ]
        index["can_identity_preview"] = (
            not index["two_step"] and not index["evaluated"]
            and state["status"] not in {"queued", "running", "inspecting", "identity_previewing"}
            and index["status"] == "completed"
            and bool(index["counts"]["aligned"] + index["counts"]["needs_review"])
        )
        index["can_evaluate"] = (
            not index["evaluated"]
            and state["status"] not in {"queued", "running", "inspecting", "identity_previewing"}
            and index["status"] == "completed"
            and bool(index["counts"]["aligned"] + index["counts"]["needs_review"])
            and not (self.store.run_dir(run_id) / "parsed").exists()
        )
        index["can_re_evaluate"] = (
            not index["two_step"] and index["evaluated"]
            and state["status"] == "completed"
        )
        index["can_inspect"] = (
            not index["two_step"] and state["status"] not in {"queued", "running", "inspecting", "identity_previewing"}
            and (index["status"] != "completed" or bool(index["counts"]["failed"]))
        )
        if index["two_step"]:
            index["can_evaluate"] = (not index["evaluated"]
                and state["status"] not in {"queued", "running", "inspecting", "grading"}
                and not (self.store.run_dir(run_id) / "parsed").exists())
        index["can_grade"] = index["two_step"] and index["evaluated"] and state["status"] in {"completed", "grading_failed"}
        payload = json.dumps(index).encode("utf-8")
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _identity_asset(self, run_id: str, query: dict[str, list[str]]) -> None:
        relative = str((query.get("path") or [""])[0]).strip()
        if not relative:
            self._not_found("Identity evidence image is not available")
            return
        run_dir = self.store.run_dir(run_id).resolve()
        preview_root = (run_dir / "identity_preview").resolve()
        path = (run_dir / relative).resolve()
        if not path.is_relative_to(preview_root) or not path.is_file():
            self._not_found("Identity evidence image is not available")
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self._send_file(path, content_type)

    def _assign_page_index(self, run_id: str, source_index: int) -> None:
        form = self._urlencoded_form()
        raw_page_index = (form.get("page_index") or "").strip()
        if not raw_page_index:
            raise ValueError("Sheet page number is required")
        try:
            page_index = int(raw_page_index)
        except ValueError as exc:
            raise ValueError("Sheet page number must be a whole number") from exc
        self.store.assign_inspection_page(run_id, source_index, page_index)
        self._redirect(f"/runs/{run_id}/pages?page={source_index}")

    def _ownership_controls(self, run_id: str, index: dict) -> str:
        revision = html.escape(self._review_revision(run_id), quote=True)
        base = f"/runs/{html.escape(run_id)}"
        count = sum(row.get("status") == "auto_matched" for row in index.get("students", []))
        approval = "" if not count else f"""
          <form method="post" action="{base}/approve-clean">
            <input type="hidden" name="revision" value="{revision}">
            <input type="hidden" name="confirm_count" value="{count}">
            <label><input type="checkbox" name="confirm" value="1" required>Approve {count} clean matches for release</label>
            <button type="submit">Approve Clean Matches ({count})</button>
          </form>"""
        return f"""<div class="actions band ownership-actions">
          <form method="post" action="{base}/reassess-ownership">
            <input type="hidden" name="revision" value="{revision}">
            <label><input type="checkbox" name="confirm" value="1" required>Reassess unreviewed page ownership; preserve manual decisions</label>
            <button class="secondary" type="submit">Reassess Saved Evidence</button>
          </form>{approval}</div>"""

    def _ownership_action(self, run_id: str, action: str) -> None:
        form = self._urlencoded_form()
        if not form.get("revision") or form["revision"] != self._review_revision(run_id):
            self._send_json({"error": "Review state changed. Refresh before confirming this action."}, HTTPStatus.CONFLICT)
            return
        if form.get("confirm") != "1":
            raise ValueError("Explicit confirmation is required")
        with self.store._worker_lock:
            if self.store._worker and self.store._worker.is_alive():
                raise ValueError("Wait for the current inspection or matching operation to finish")
            state = self.store.read_state(run_id)
            if not state.get("parse_index_path"):
                raise ValueError("Match sheets before reassessing or approving ownership")
            if action == "reassess-ownership":
                reassess_saved_ownership(state["parse_dir"], reviewer="professor-ui", grouping_only=_is_grouping_only(state))
            else:
                approve_clean_matches(state["parse_dir"], reviewer="professor-ui", confirm_count=int(form.get("confirm_count") or 0))
        self._redirect(f"/runs/{run_id}/review")

    def _student_decision(self, run_id: str, roll_no: str, action: str) -> None:
        state = self.store.read_state(run_id)
        parse_dir = Path(str(state.get("parse_dir") or ""))
        form = self._urlencoded_form()
        if form.get("revision") and form["revision"] != self._review_revision(run_id):
            message = "Review state changed. Refresh and check the current selected pages before saving."
            if self.headers.get("Accept") == "application/json":
                self._send_json({"error": message}, HTTPStatus.CONFLICT)
            else:
                self._bad_request(message)
            return
        current, _ = load_or_initialize_verified_index(parse_dir)
        kind, search = student_view.queue_parameters(form)
        next_roll = student_view.next_student(current, roll_no, kind, search)
        note = (form.get("note") or "").strip()
        if action == "verify":
            target = next(student for student in current["students"] if str(student["roll_no"]) == roll_no)
            conflicts = student_view.selection_conflicts(current, target)
            if conflicts:
                raise ValueError(" ".join(conflicts))
            verify_student(
                parse_dir,
                roll_no,
                reviewer="professor-ui",
                note=note or "Marked manually checked from UI.",
                allow_missing=form.get("workspace") != "1" or form.get("allow_missing") == "1",
            )
        elif action == "hold":
            hold_student(
                parse_dir,
                roll_no,
                reviewer="professor-ui",
                reason=note or "Kept in review from UI.",
            )
        else:
            reject_student(
                parse_dir,
                roll_no,
                reviewer="professor-ui",
                reason=note or "Rejected from professor UI.",
            )
        advance = form.get("advance") == "1"
        location = student_view.student_url(
            run_id, next_roll if advance and next_roll else roll_no, kind, search,
            saved=action, **({"finished": "1"} if advance and not next_roll else {}),
        )
        if self.headers.get("Accept") == "application/json":
            self._send_json({"redirect_url": location, "saved": action})
        else:
            self._redirect(location)

    def _update_student_marks(self, run_id: str, roll_no: str) -> None:
        state = self.store.read_state(run_id)
        if _is_grouping_only(state):
            raise ValueError("marks cannot be edited in a grouping-only run")
        parse_dir = Path(str(state.get("parse_dir") or ""))
        run_dir = self.store.run_dir(run_id)
        index_path = Path(str(state.get("parse_index_path") or ""))
        form = self._urlencoded_form()
        score_text = (form.get("marks_obtained") or "").strip()
        total_text = (form.get("max_marks") or "").strip()
        note = (form.get("note") or "").strip()
        if score_text == "":
            raise ValueError("marks obtained is required")
        score = float(score_text)
        total = float(total_text) if total_text else None

        index = _read_json(index_path)
        student_index = next(
            (student for student in index.get("students", []) if str(student.get("roll_no")) == str(roll_no)),
            None,
        )
        if student_index is None:
            raise ValueError(f"student {roll_no} not found")
        details_path = _resolve_output_path(student_index["details_path"], parse_dir)
        details = _read_json(details_path)
        _old_score, old_total = _score(details)
        if total is None:
            total = old_total
        details["manual_score_override"] = score
        details["manual_total_override"] = total
        details["manual_score_note"] = note
        details.setdefault("decision_log", []).append(
            {
                "action": "edit_marks",
                "reviewer": "professor-ui",
                "note": note or f"Marks edited to {_fmt_num(score)} / {_fmt_num(total)}.",
                "created_at": _now(),
            }
        )
        _write_json(details_path, details)

        student_index["mcq_score"] = score
        student_index["mcq_total"] = total
        student_index["numerical_score"] = 0.0
        student_index["numerical_total"] = 0.0
        student_index["manual_score_override"] = score
        student_index["manual_total_override"] = total
        _write_json(index_path, index)

        verified_path = parse_dir / "verified_index.json"
        if verified_path.exists():
            verified_index = _read_json(verified_path)
            verified_student = next(
                (student for student in verified_index.get("students", []) if str(student.get("roll_no")) == str(roll_no)),
                None,
            )
            if verified_student is not None:
                verified_student["mcq_score"] = score
                verified_student["mcq_total"] = total
                verified_student["numerical_score"] = 0.0
                verified_student["numerical_total"] = 0.0
                verified_student["manual_score_override"] = score
                verified_student["manual_total_override"] = total
                verified_student.setdefault("decision_log", []).append(
                    {
                        "action": "edit_marks",
                        "reviewer": "professor-ui",
                        "note": note or f"Marks edited to {_fmt_num(score)} / {_fmt_num(total)}.",
                        "created_at": _now(),
                    }
                )
                _write_json(verified_path, verified_index)

        marks_csv = _write_marks_csv(run_dir, parse_dir, index)
        self.store.write_state(run_id, marks_csv_path=str(marks_csv))
        self._redirect(f"/runs/{run_id}/students/{urllib.parse.quote(roll_no)}")

    def _correct_student_roll_feedback(self, run_id: str, roll_no: str) -> None:
        state = self.store.read_state(run_id)
        parse_dir = Path(str(state.get("parse_dir") or ""))
        index_path = Path(str(state.get("parse_index_path") or ""))
        form = self._urlencoded_form()
        correct_roll = (form.get("correct_roll_no") or "").strip().upper()
        program = (form.get("program") or "").strip().upper()
        page_text = (form.get("page_index") or "1").strip()
        note = (form.get("note") or "").strip()
        if not correct_roll:
            raise ValueError("correct roll number is required")
        if program not in {"BTECH", "MTECH", "PHD"}:
            raise ValueError("program must be BTECH, MTECH, or PHD")
        page_index = int(page_text)

        state2, parse_dir, details, verified = self._student_details(run_id, roll_no)
        details_path = _resolve_output_path(details.get("details_path"), parse_dir)
        if details_path.resolve() == (parse_dir / "verified_index.json").resolve():
            raise ValueError("This student has no original parser record for training feedback; use source-page ownership correction instead")
        count, feedback_dir = _save_roll_correction_feedback(
            data_dir=self.store.config.data_dir,
            run_id=run_id,
            roll_no=roll_no,
            details=details,
            parse_dir=parse_dir,
            manifest_path=Path(str(state2["inputs"]["manifest_path"])),
            correct_roll_no=correct_roll,
            program=program,
            page_index=page_index,
        )

        event = {
            "action": "correct_roll_feedback",
            "reviewer": "professor-ui",
            "old_roll_no": roll_no,
            "correct_roll_no": correct_roll,
            "program": program,
            "page_index": page_index,
            "saved_digit_crops": count,
            "feedback_dir": str(feedback_dir),
            "note": note or "Saved corrected roll digit crops for future fine-tuning.",
            "created_at": _now(),
        }
        details.setdefault("decision_log", []).append(event)
        details.setdefault("roll_correction_feedback", []).append(event)
        details.setdefault("review_flags", []).append(
            f"roll mapping feedback saved: displayed roll {roll_no}, corrected roll {correct_roll}, page {page_index}"
        )
        _write_json(details_path, details)

        if index_path.exists():
            index = _read_json(index_path)
            student_index = next(
                (student for student in index.get("students", []) if str(student.get("roll_no")) == str(roll_no)),
                None,
            )
            if student_index is not None:
                student_index["review_flags"] = list(details.get("review_flags", []))
                _write_json(index_path, index)
        verified_path = parse_dir / "verified_index.json"
        if verified_path.exists():
            verified_index = _read_json(verified_path)
            verified_student = next(
                (student for student in verified_index.get("students", []) if str(student.get("roll_no")) == str(roll_no)),
                None,
            )
            if verified_student is not None:
                verified_student.setdefault("decision_log", []).append(event)
                verified_student.setdefault("review_flags", []).append(
                    f"roll mapping feedback saved: displayed roll {roll_no}, corrected roll {correct_roll}, page {page_index}"
                )
                if verified_student.get("status") in {"verified", "approved", "auto_matched"}:
                    verified_student["status"] = "needs_review"
                    verified_student["eligible_for_email"] = False
                _write_json(verified_path, verified_index)

        self._redirect(f"/runs/{run_id}/students/{urllib.parse.quote(roll_no)}")

    def _reupload_student_sheet(self, run_id: str, roll_no: str) -> None:
        state = self.store.read_state(run_id)
        parse_dir = Path(str(state.get("parse_dir") or ""))
        index_path = Path(str(state.get("parse_index_path") or ""))
        run_dir = self.store.run_dir(run_id)
        if not parse_dir.exists() or not index_path.exists():
            raise ValueError("run is not parsed yet")

        staging_dir = run_dir / "_upload_staging" / uuid.uuid4().hex
        try:
            fields, files = parse_multipart_upload(
                self.rfile,
                content_type=self.headers.get("content-type", ""),
                content_length=int(self.headers.get("content-length", 0)),
                staging_dir=staging_dir,
            )
            upload = files.get("student_sheet")
            if upload is None or not upload.path.exists() or upload.path.stat().st_size == 0:
                raise ValueError("student PDF/image upload is required")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            saved_dir = run_dir / "student_reuploads" / _safe_id(roll_no) / timestamp
            saved_dir.mkdir(parents=True, exist_ok=True)
            saved_upload = saved_dir / _safe_id(upload.filename or upload.path.name)
            shutil.copy2(upload.path, saved_upload)
            note = (fields.get("note") or "").strip()
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

        student_dir = parse_dir / "students" / _safe_id(roll_no)
        backup_dir = run_dir / "student_reupload_backups" / _safe_id(roll_no) / timestamp
        if student_dir.exists():
            backup_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(student_dir, backup_dir)

        try:
            shutil.rmtree(student_dir, ignore_errors=True)
            roll_ocr_backend, roll_ocr_state = _build_ui_roll_ocr_backend()
            result = _parse_student_reupload(
                upload_path=saved_upload,
                roll_no=roll_no,
                parse_dir=parse_dir,
                manifest_path=Path(str(state["inputs"]["manifest_path"])),
                students_path=Path(str(state["inputs"]["students_path"])) if state["inputs"].get("students_path") else None,
                answer_key_path=Path(str(state["inputs"]["answer_key_path"])) if state["inputs"].get("answer_key_path") else None,
                roll_ocr_backend=roll_ocr_backend,
            )
            result.setdefault("decision_log", []).append(
                {
                    "action": "student_reupload",
                    "reviewer": "professor-ui",
                    "note": note or (
                        f"Uploaded replacement sheet {saved_upload.name} and "
                        f"{'reprocessed' if _is_grouping_only(state) else 're-evaluated'} only this student."
                    ),
                    "created_at": _now(),
                }
            )
            _write_json(Path(result["details_path"]), result)

            index = _read_json(index_path)
            entry = _student_index_entry(result)
            students = list(index.get("students", []))
            for item_index, student in enumerate(students):
                if str(student.get("roll_no")) == str(roll_no):
                    students[item_index] = entry
                    break
            else:
                students.append(entry)
            index["students"] = students
            _recalculate_status_counts(index)
            manifest = load_manifest(Path(str(state["inputs"]["manifest_path"])))
            report_paths = _write_review_reports(
                parse_dir,
                manifest,
                index,
                _load_student_results_for_report(index, parse_dir),
                index.get("unmatched_pages", []),
                index.get("page_errors", []),
            )
            index["reports"] = report_paths
            index["review_report_csv_path"] = report_paths["csv"]
            index["review_report_html_path"] = report_paths["html"]
            _write_json(index_path, index)
            _replace_verified_student(parse_dir, roll_no, result)
            marks_csv = None if _is_grouping_only(state) else _write_marks_csv(run_dir, parse_dir, index)
            counts = index.get("status_counts", {})
            self.store.write_state(
                run_id,
                marks_csv_path=str(marks_csv) if marks_csv else None,
                roll_ocr=roll_ocr_state,
                summary={
                    **dict(state.get("summary") or {}),
                    "students": len(index.get("students", [])),
                    "ready": counts.get("ready", 0),
                    "needs_review": counts.get("needs_review", 0),
                    "unmatched_pages": counts.get("unmatched_pages", 0),
                    "page_errors": counts.get("page_errors", 0),
                    "roster_missing": counts.get("roster_missing", 0),
                },
            )
        except Exception:
            if backup_dir.exists():
                shutil.rmtree(student_dir, ignore_errors=True)
                shutil.copytree(backup_dir, student_dir)
            raise

        self._redirect(f"/runs/{run_id}/students/{urllib.parse.quote(roll_no)}")

    def _replace_student_page(self, run_id: str, roll_no: str) -> None:
        state = self.store.read_state(run_id)
        parse_dir = Path(str(state.get("parse_dir") or ""))
        index_path = Path(str(state.get("parse_index_path") or ""))
        run_dir = self.store.run_dir(run_id)
        if not parse_dir.exists() or not index_path.exists():
            raise ValueError("run is not parsed yet")
        _state, _parse_dir, current_details, _verified = self._student_details(run_id, roll_no)

        staging_dir = run_dir / "_upload_staging" / uuid.uuid4().hex
        try:
            fields, files = parse_multipart_upload(
                self.rfile,
                content_type=self.headers.get("content-type", ""),
                content_length=int(self.headers.get("content-length", 0)),
                staging_dir=staging_dir,
            )
            upload = files.get("student_page")
            if upload is None or not upload.path.exists() or upload.path.stat().st_size == 0:
                raise ValueError("replacement page PDF/image upload is required")
            page_text = (fields.get("page_index") or "").strip()
            if not page_text:
                raise ValueError("page number is required")
            target_page = int(page_text)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            saved_dir = run_dir / "student_page_replacements" / _safe_id(roll_no) / timestamp
            saved_dir.mkdir(parents=True, exist_ok=True)
            saved_upload = saved_dir / _safe_id(upload.filename or upload.path.name)
            shutil.copy2(upload.path, saved_upload)
            note = (fields.get("note") or "").strip()
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)

        student_dir = parse_dir / "students" / _safe_id(roll_no)
        backup_dir = run_dir / "student_reupload_backups" / _safe_id(roll_no) / timestamp
        if student_dir.exists():
            backup_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(student_dir, backup_dir)

        try:
            roll_ocr_backend, roll_ocr_state = _build_ui_roll_ocr_backend()
            result = _parse_student_page_replacement(
                upload_path=saved_upload,
                roll_no=roll_no,
                target_page=target_page,
                current_details=current_details,
                parse_dir=parse_dir,
                manifest_path=Path(str(state["inputs"]["manifest_path"])),
                students_path=Path(str(state["inputs"]["students_path"])) if state["inputs"].get("students_path") else None,
                answer_key_path=Path(str(state["inputs"]["answer_key_path"])) if state["inputs"].get("answer_key_path") else None,
                roll_ocr_backend=roll_ocr_backend,
            )
            result.setdefault("decision_log", []).append(
                {
                    "action": "replace_page",
                    "reviewer": "professor-ui",
                    "note": note or (
                        f"Replaced page {target_page} with {saved_upload.name} and "
                        f"{'reprocessed' if _is_grouping_only(state) else 're-evaluated'} this student."
                    ),
                    "created_at": _now(),
                }
            )
            _write_json(Path(result["details_path"]), result)
            _commit_student_result(
                store=self.store,
                run_id=run_id,
                state=state,
                parse_dir=parse_dir,
                index_path=index_path,
                roll_no=roll_no,
                result=result,
                roll_ocr_state=roll_ocr_state,
            )
        except Exception:
            if backup_dir.exists():
                shutil.rmtree(student_dir, ignore_errors=True)
                shutil.copytree(backup_dir, student_dir)
            raise

        self._redirect(f"/runs/{run_id}/students/{urllib.parse.quote(roll_no)}")

    def _unmatched_decision(self, run_id: str, source_index: int, action: str) -> None:
        state = self.store.read_state(run_id)
        parse_dir = Path(str(state.get("parse_dir") or ""))
        form = self._urlencoded_form()
        note = (form.get("note") or "").strip()
        if action == "assign":
            roll_no = (form.get("roll_no") or "").strip().upper()
            if not roll_no:
                raise ValueError("roll number is required to assign an unmatched page")
            page_value = (form.get("page_index") or "").strip()
            self.store.assign_source(
                run_id, source_index, roll_no,
                page_index=int(page_value) if page_value else None,
                note=note or "Assigned from professor UI.",
            )
        else:
            ignore_unmatched_page(
                parse_dir,
                source_index,
                reviewer="professor-ui",
                reason=note or "Ignored as a stray/duplicate page from professor UI.",
            )
        self._redirect(f"/runs/{run_id}/review")

    def _assign_source_page(self, run_id: str, source_index: int) -> None:
        form = self._urlencoded_form()
        self.store.assign_source(
            run_id, source_index, str(form.get("roll_no") or ""),
            page_index=int(form.get("page_index") or 0) or None,
            replace_existing=form.get("replace_existing") == "1", note=str(form.get("note") or ""),
        )
        if self.headers.get("Accept") == "application/json":
            self._send_json({"saved": True, "source_index": source_index})
        else:
            self._redirect(f"/runs/{run_id}/pages?page={source_index}")

    def _identity_suggestion_decision(self, run_id: str, candidate_id: str, action: str) -> None:
        state = self.store.read_state(run_id)
        parse_dir = Path(str(state.get("parse_dir") or ""))
        form = self._urlencoded_form()
        note = (form.get("note") or "").strip()
        decide_identity_suggestion(
            parse_dir,
            candidate_id,
            action=action,
            assigned_roll_no=(form.get("roll_no") or "").strip().upper() or None,
            reviewer="professor-ui",
            note=note,
        )
        self._redirect(f"/runs/{run_id}/review")

    def _urlencoded_form(self) -> dict[str, str]:
        length = int(self.headers.get("content-length", 0))
        data = self.rfile.read(length).decode("utf-8")
        parsed = urllib.parse.parse_qs(data)
        return {key: values[0] if values else "" for key, values in parsed.items()}

    def _prepare_email(self, run_id: str) -> None:
        state = self.store.read_state(run_id)
        parse_dir = Path(str(state.get("parse_dir") or ""))
        form = self._urlencoded_form()
        sender = form.get("sender") or "professor@example.edu"
        sender_name = form.get("sender_name") or None
        sheet_only = (form.get("release_mode") or "sheet_verification") == "sheet_verification"
        if _is_grouping_only(state) and not sheet_only:
            raise ValueError("grouping-only runs can release verified sheets without marks only")
        subject = form.get("subject") or (
            "{exam_id}: response sheet verification"
            if sheet_only
            else "{exam_id}: evaluated OMR response sheet"
        )
        body_template = form.get("body_template") or None
        prepare_email_release(
            parse_dir,
            sender=sender,
            sender_name=sender_name,
            subject_template=subject,
            body_template=body_template,
            sheet_only=sheet_only,
        )
        self._redirect(f"/runs/{run_id}/email")

    def _send_email(self, run_id: str) -> None:
        state = self.store.read_state(run_id)
        parse_dir = Path(str(state.get("parse_dir") or ""))
        queue_path = parse_dir / "email_release" / EMAIL_QUEUE_CSV
        if not queue_path.exists():
            raise ValueError("prepare the email queue before sending")
        form = self._urlencoded_form()
        mode = form.get("mode") or "dry_run"
        dry_run = mode != "send"
        limit_value = (form.get("limit") or "").strip()
        limit = int(limit_value) if limit_value else None
        confirm_value = (form.get("confirm_count") or "").strip()
        confirm_count = int(confirm_value) if confirm_value else None
        only_roll = (form.get("only_roll") or "").strip() or None
        test_recipient = (form.get("test_recipient") or "").strip() or None
        password = form.get("password") or ""
        if test_recipient and limit is None and only_roll is None:
            limit = 1
        if not dry_run and not password:
            raise ValueError("SMTP password/app password is required for real sending")
        rows, log_path = send_email_release(
            queue_path,
            smtp_host=form.get("smtp_host") or "smtp.gmail.com",
            smtp_port=int(form.get("smtp_port") or "587"),
            username=form.get("username") or form.get("sender") or "",
            password=password,
            sender=form.get("sender") or form.get("username") or "",
            sender_name=form.get("sender_name") or None,
            dry_run=dry_run,
            limit=limit,
            only_roll=only_roll,
            delay_seconds=float(form.get("delay_seconds") or "0.25"),
            smtp_security=form.get("smtp_security") or "starttls",
            test_recipient=test_recipient,
            confirm_count=confirm_count,
        )
        sent = sum(1 for row in rows if row["status"] == "SENT")
        test_sent = sum(1 for row in rows if row["status"] == "TEST_SENT")
        dry = sum(1 for row in rows if row["status"] == "DRY_RUN")
        failed = sum(1 for row in rows if row["status"] == "FAILED")
        skipped = sum(1 for row in rows if row["status"] == "SKIPPED_ALREADY_SENT")
        self.store.write_state(
            run_id,
            last_email_send={
                "created_at": _now(),
                "mode": mode,
                "sent": sent,
                "test_sent": test_sent,
                "dry_run": dry,
                "failed": failed,
                "already_sent": skipped,
                "log_path": str(log_path),
            },
        )
        self._redirect(f"/runs/{run_id}/email")

    def _page_runs(self) -> None:
        rows = []
        for state in self.store.list_runs():
            current = self._current_review(state)
            summary = _review_summary(current) if current is not None else {}
            rows.append(
                "<tr>"
                f"<td><a href=\"/runs/{html.escape(state['run_id'])}\">{html.escape(state.get('exam_id',''))}</a></td>"
                f"<td>{_badge(state.get('status'))}</td>"
                f"<td>{html.escape(str(summary.get('students','')))}</td>"
                f"<td>{html.escape(str(summary.get('verified','')))}</td>"
                f"<td>{html.escape(str(summary.get('auto_matched','')))}</td>"
                f"<td>{html.escape(str(summary.get('approved','')))}</td>"
                f"<td>{html.escape(str(summary.get('pending_verification','')))}</td>"
                f"<td>{html.escape(str(summary.get('needs_review','')))}</td>"
                f"<td>{html.escape(str(state.get('created_at','')))}</td>"
                "</tr>"
            )
        table = """
        <table>
          <thead><tr><th>Exam</th><th>Status</th><th>Students</th><th>Manually Checked</th><th>Auto Matched</th><th>Approved For Release</th><th>Pending Verification</th><th>Needs Review</th><th>Created</th></tr></thead>
          <tbody>{}</tbody>
        </table>
        """.format("".join(rows) or '<tr><td colspan="9" class="empty">No runs yet</td></tr>')
        body = f"""
        <h1>Exam Runs</h1>
        <div class="actions">
          <a class="button" href="/generate">Generate OMR</a>
          <a class="button secondary" href="/new">Check Copies</a>
        </div>
        <h2>Recent Runs</h2>
        {table}
        """
        self._send_html("Exam Runs", body)

    def _page_generate(self) -> None:
        body = """
        <h1>Generate OMR</h1>
        <form method="post" action="/generate" class="band">
          <div class="grid">
            <div><label>Exam ID</label><input name="exam_id" placeholder="CSE557_QUIZ1_2026" required></div>
            <div><label>University</label><input name="university_name" value="IIIT Delhi"></div>
            <div><label>Course Code</label><input name="course_code" placeholder="CSE557" required></div>
            <div><label>Exam Name</label><input name="exam_name" placeholder="Quiz 1" required></div>
            <div>
              <label>Exam Type</label>
              <select name="exam_type"><option value="quiz">Quiz</option><option value="midsem">Midsem</option><option value="endsem">Endsem</option></select>
            </div>
          </div>
          <div class="section-builder">
            <div class="section-builder-head">
              <div><h2>Answer Sections</h2><p class="muted">Add sections in the same order they should appear on the OMR.</p></div>
              <div class="actions"><select id="section-kind"><option value="mcq">Multiple choice</option><option value="numerical">Numerical bubbles</option><option value="written">Written answers</option></select><button type="button" id="add-section" class="secondary">Add Section</button></div>
            </div>
            <input id="section-order" type="hidden" name="section_order">
            <div id="sections" class="section-list"></div>
            <p id="section-empty" class="muted">No answer sections yet. Choose a type and click Add Section.</p>
          </div>
          <p class="muted">If the sheet has only one answer section, it will print without “Section A”. Section A/B/C appears only when more than one answer section exists.</p>
          <p class="muted">One section prints without a section label. Two or more print as Section A, Section B, and so on.</p>
          <button type="submit">Generate PDF And Manifest</button>
        </form>
        <style>
          .section-builder { margin-top:20px; border-top:1px solid var(--border); padding-top:16px; }
          .section-builder-head { display:flex; gap:16px; justify-content:space-between; align-items:end; flex-wrap:wrap; }
          .section-builder-head h2, .section-builder-head p { margin:0; }
          .section-builder-head select { width:190px; }
          .section-list { display:grid; gap:12px; margin-top:14px; }
          .section-card { border:1px solid var(--border); border-left:4px solid var(--primary); padding:14px; background:#fff; }
          .section-card-head { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }
          .section-card-head strong { font-size:15px; }
          .section-card textarea { min-height:90px; }
          .remove-section { border:0; background:transparent; color:var(--danger); font:inherit; font-weight:700; cursor:pointer; padding:3px; }
        </style>
        <script>
        (() => {
          const kinds=document.getElementById('section-kind'), add=document.getElementById('add-section'), list=document.getElementById('sections'), order=document.getElementById('section-order'), empty=document.getElementById('section-empty');
          const labels={mcq:'Multiple Choice',numerical:'Numerical Answers',written:'Written Answers'};
          const content={
            mcq:'<div class="grid"><div><label>Number of Questions</label><input name="num_mcq" type="number" min="1" value="14" required></div><div><label>Options Per Question</label><input name="mcq_options" type="number" min="2" max="6" value="4" required></div><div><label>Marks Per Question</label><input name="marks_per_mcq" type="number" min="0.01" step="0.01" value="1" required></div></div>',
            numerical:'<label>Questions</label><textarea name="numerical_questions" placeholder="One per line: question-number marks digits&#10;15 1 3" required></textarea><p class="muted">Example: 15 1 3 creates Q15 with a three-digit bubble grid.</p>',
            written:'<label>Questions</label><textarea name="written_questions" placeholder="One per line: question-number marks lines&#10;16 5 8" required></textarea><p class="muted">Example: 16 5 8 creates Q16 with an eight-line answer box.</p>'
          };
          function update(){const cards=[...list.querySelectorAll('[data-kind]')];order.value=cards.map(c=>c.dataset.kind).join(',');empty.hidden=cards.length>0;[...kinds.options].forEach(o=>o.disabled=cards.some(c=>c.dataset.kind===o.value));}
          add.addEventListener('click',()=>{const kind=kinds.value;if(list.querySelector(`[data-kind="${kind}"]`))return;const card=document.createElement('section');card.className='section-card';card.dataset.kind=kind;card.innerHTML=`<div class="section-card-head"><strong>${labels[kind]}</strong><button type="button" class="remove-section">Remove</button></div>${content[kind]}`;card.querySelector('.remove-section').addEventListener('click',()=>{card.remove();update();});list.append(card);update();});
          update();
        })();
        </script>
        """
        self._send_html("Generate OMR", body)

    def _generate_omr(self) -> None:
        form = self._urlencoded_form()
        exam_id = _safe_id((form.get("exam_id") or "").strip())
        if not exam_id:
            raise ValueError("exam id is required")
        written_questions = _parse_question_rows(form.get("written_questions") or "", kind="written")
        numerical_questions = _parse_question_rows(form.get("numerical_questions") or "", kind="numerical")
        section_order = [item.strip() for item in (form.get("section_order") or "").split(",") if item.strip()]
        config = ExamConfig(
            exam_id=exam_id,
            university_name=(form.get("university_name") or "IIIT Delhi").strip() or "IIIT Delhi",
            course_code=(form.get("course_code") or "").strip(),
            exam_name=(form.get("exam_name") or "").strip(),
            exam_type=(form.get("exam_type") or "quiz").strip(),
            num_mcq=int(form.get("num_mcq") or "0"),
            mcq_options=int(form.get("mcq_options") or "4"),
            marks_per_mcq=float(form.get("marks_per_mcq") or "1"),
            written_questions=written_questions,
            numerical_questions=numerical_questions,
            section_order=section_order,
        )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = self.store.config.data_dir / "generated_omr" / f"{exam_id}_{stamp}"
        result = generate_exam(config, output_dir)
        body = f"""
        <h1>OMR Generated</h1>
        <div class="actions band">
          <a class="button" href="{_asset_url(result['pdf_path'])}">Download OMR PDF</a>
          <a class="button secondary" href="{_asset_url(result['manifest_path'])}">Download Manifest JSON</a>
          <a class="button secondary" href="/new">Check Copies</a>
        </div>
        <div class="grid">
          <div class="metric"><span>Exam ID</span><strong>{html.escape(exam_id)}</strong></div>
          <div class="metric"><span>Pages</span><strong>{html.escape(str(result['manifest'].get('num_pages')))}</strong></div>
          <div class="metric"><span>Total Marks</span><strong>{html.escape(str(result['manifest'].get('exam', {}).get('total_marks')))}</strong></div>
        </div>
        <p class="muted">Generated files are stored in {html.escape(str(output_dir))}.</p>
        """
        self._send_html("OMR Generated", body)

    def _page_new(self) -> None:
        body = """
        <h1>New Run</h1>
        <form method="post" action="/runs" enctype="multipart/form-data" class="band">
          <input type="hidden" name="workflow" value="two_step">
          <div class="grid">
            <div><label>Exam ID</label><input name="exam_id" placeholder="From manifest"></div>
            <div><label>Scanned OMR PDF / image</label><input type="file" name="scan_pdf" required accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff"></div>
            <div><label>Manifest JSON</label><input type="file" name="manifest" required accept=".json"></div>
            <div><label>Student roster (optional)</label><input type="file" name="master_list" accept=".csv,.xlsx"></div>
          </div>
          <div class="actions" style="margin-top:16px"><button type="submit">1. Detect Rolls &amp; Group Sheets</button></div>
        </form>
        """
        self._send_html("New Run", body)

    def _page_grade(self, run_id: str) -> None:
        state = self.store.read_state(run_id)
        if not state.get("parse_index_path"):
            raise ValueError("Detect rolls and group sheets before grading")
        manifest = load_manifest(state["inputs"]["manifest_path"])
        body = """
        <h1>Grade Answers</h1>
        <form method="post" action="/runs/__RUN_ID__/grade" enctype="multipart/form-data" class="band">
          <input type="hidden" name="revision" value="__REVISION__">
          <div class="grid">
            <div>
              <label>Answer key Excel / CSV (optional)</label>
              <input type="file" name="answer_key" accept=".csv,.xlsx">
            </div>
          </div>
          <section style="margin-top:16px">
            <h2>Answer Key</h2>
            <input type="hidden" name="answer_key_mode" value="manifest_form">
            <div id="answer-key-summary" class="muted"></div>
            <div id="answer-key-table"></div>
          </section>
          <div class="actions" style="margin-top:16px">
            <button type="submit">2. Grade Answers</button>
            <a class="button secondary" href="/runs/__RUN_ID__/review">Back to Sheets</a>
          </div>
        </form>
        <style>
          #answer-key-table input:not([type="checkbox"]), #answer-key-table select { min-width:104px; }
          #answer-key-table input[type="number"] { min-width:84px; }
        </style>
        <script>
        const summary = document.getElementById("answer-key-summary");
        const target = document.getElementById("answer-key-table");

        function esc(value) {
          return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
        }

        function objectiveQuestions(manifest) {
          const questions = [];
          const mcqMarks = Number(manifest.exam?.marks_per_mcq ?? 1);
          for (const entry of manifest.mcq_block || []) {
            questions.push({ q_no: Number(entry.q_no), kind: "MCQ", marks: mcqMarks, options: entry.options || [] });
          }
          for (const entry of manifest.numerical_block || []) {
            questions.push({ q_no: Number(entry.q_no), kind: "Numerical", marks: Number(entry.max_marks ?? 1), digits: Number(entry.positions ?? 1) });
          }
          return questions.sort((a, b) => a.q_no - b.q_no);
        }

        function renderAnswerKey(manifest) {
          const questions = objectiveQuestions(manifest);
          const written = manifest.written_block || [];
          summary.textContent = `${questions.length} objective question(s), ${written.length} written question(s). Written questions stay for manual/professor review.`;
          if (!questions.length) {
            target.innerHTML = '<p class="empty">No objective questions found in this manifest.</p>';
            return;
          }
          const rows = questions.map(q => {
            const answerControl = q.kind === "MCQ"
              ? `<select name="answer_q${q.q_no}"><option value="">Select</option>${q.options.map(opt => `<option value="${esc(opt)}">${esc(opt)}</option>`).join("")}</select>`
              : `<input name="answer_q${q.q_no}" pattern="[0-9|, ]+" placeholder="${"0".repeat(Math.max(1, q.digits || 1))}">`;
            return `<tr>
              <td>Q${q.q_no}</td>
              <td>${esc(q.kind)}</td>
              <td>${answerControl}</td>
              <td><input name="marks_q${q.q_no}" type="number" step="0.01" min="0" value="${esc(q.marks)}"></td>
              <td><label class="inline"><input name="drop_q${q.q_no}" type="checkbox" value="1"> Dropped</label></td>
            </tr>`;
          }).join("");
          target.innerHTML = `<table>
            <thead><tr><th>Question</th><th>Type</th><th>Correct Answer</th><th>Marks</th><th>Drop</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>`;
        }

        renderAnswerKey(__MANIFEST__);
        </script>
        """
        body = body.replace("__RUN_ID__", html.escape(run_id, quote=True)).replace("__REVISION__", self._review_revision(run_id))
        body = body.replace("__MANIFEST__", json.dumps(manifest).replace("<", "\\u003c"))
        self._send_html("Grade Answers", body)

    def _start_grading(self, run_id: str) -> None:
        staging = self.store.config.runs_dir / "_upload_staging" / uuid.uuid4().hex
        try:
            fields, files = parse_multipart_upload(self.rfile, content_type=self.headers.get("content-type", ""),
                content_length=int(self.headers.get("content-length", 0)), staging_dir=staging)
            with self.store._review_lock:
                if not fields.get("revision") or fields["revision"] != self._review_revision(run_id):
                    self._send_json({"error": "Review changed; refresh before grading"}, HTTPStatus.CONFLICT)
                    return
                state = self.store.read_state(run_id)
                manifest = load_manifest(state["inputs"]["manifest_path"])
                folder = self.store.run_dir(run_id) / "inputs" / "grading" / uuid.uuid4().hex
                folder.mkdir(parents=True)
                key_path = folder / "answer_key.csv"
                upload = files.get("answer_key")
                if upload and upload.path.is_file() and upload.path.stat().st_size:
                    _normalize_tabular_upload(upload.path, key_path)
                else:
                    key_path = _write_answer_key_from_form(manifest, fields, key_path)
                self.store.start_grading(run_id, key_path)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        self._redirect(f"/runs/{run_id}/pages?matching=1")

    def _grading_control(self, run_id: str, state: dict) -> str:
        if state.get("inputs", {}).get("workflow") != "two_step" or not state.get("parse_index_path"):
            return ""
        return f'<a class="button" href="/runs/{html.escape(run_id)}/grade">2. Grade Answers</a>'

    def _grading_notice(self, state: dict) -> str:
        summary = state.get("grading_summary")
        if not summary:
            return ""
        skipped = len(summary.get("skipped", []))
        return (f'<div class="band">Graded: {int(summary.get("graded", 0))} '
                f'| Answer review: {int(summary.get("needs_answer_review", 0))} '
                f'| Not graded (unresolved ownership): {skipped}</div>')

    def _page_run(self, run_id: str) -> None:
        state = self.store.read_state(run_id)
        grouping_only = _is_grouping_only(state)
        if state.get("status") in {
            "inspection_pending",
            "inspecting",
            "inspected",
            "inspection_interrupted",
            "identity_previewing",
            "identity_previewed",
            "identity_preview_failed",
            "queued",
            "running",
            "grading",
        }:
            self._redirect(f"/runs/{run_id}/pages")
            return
        refresh = 3 if state.get("status") in {"queued", "running"} else None
        summary = state.get("summary") or {}
        roster_summary = state.get("roster_summary") or {}
        roll_ocr = state.get("roll_ocr") or {}
        roll_ocr_label = (
            str(roll_ocr.get("provider") or "local")
            if roll_ocr.get("enabled")
            else "disabled"
        )
        metrics = [
            ("Status", _badge(state.get("status"))),
            ("Stage", html.escape(str(state.get("stage") or ""))),
            ("Students", html.escape(str(summary.get("students", "")))),
            ("Roster", html.escape(str(roster_summary.get("total", "not uploaded")))),
            ("Ready", html.escape(str(summary.get("ready", "")))),
            ("Needs Review", html.escape(str(summary.get("needs_review", "")))),
            ("Unmatched", html.escape(str(summary.get("unmatched_pages", "")))),
            ("Missing Sheets", html.escape(str(summary.get("roster_missing", "")))),
            ("Roll OCR", html.escape(roll_ocr_label)),
        ]
        metric_html = "".join(f'<div class="metric"><span>{label}</span><strong>{value}</strong></div>' for label, value in metrics)
        roll_ocr_warning = ""
        if roll_ocr.get("warning"):
            roll_ocr_warning = f'<div class="band review"><strong>Roll OCR warning:</strong> {html.escape(str(roll_ocr["warning"]))}</div>'
        if state.get("status") == "failed":
            body = f"<h1>{html.escape(state.get('exam_id',''))}</h1><div class=\"grid\">{metric_html}</div>{roll_ocr_warning}<div class=\"band\"><pre>{html.escape(state.get('error') or '')}</pre></div>"
            self._send_html("Run Failed", body)
            return
        if state.get("status") not in {"completed", "grading_failed"}:
            activity = "sheet matching" if grouping_only else "evaluation"
            body = f"<h1>{html.escape(state.get('exam_id',''))}</h1><div class=\"grid\">{metric_html}</div>{roll_ocr_warning}<p class=\"muted\">This page refreshes while {activity} runs.</p>"
            self._send_html("Run Progress", body, refresh=refresh)
            return

        parse_dir = Path(str(state["parse_dir"]))
        index = _read_json(Path(str(state["parse_index_path"])))
        grouping_order_notice = _grouping_order_notice(index)
        verified_index = self._current_review(state)
        verified_by_roll = {str(student.get("roll_no")): student for student in verified_index.get("students", [])}
        metric_html = _review_metrics(_review_summary(verified_index))
        rows = []
        students = list(index.get("students", []))
        raw_rolls = {str(student.get("roll_no")) for student in students}
        students.extend(student for roll, student in verified_by_roll.items() if roll not in raw_rolls)
        for student in students:
            details = (
                _read_json(_resolve_output_path(student["details_path"], parse_dir))
                if str(student.get("roll_no")) in raw_rolls else _review_created_details(student, parse_dir)
            )
            score, total = _score(details)
            roll = str(details.get("student", {}).get("roll_no") or student.get("roll_no") or "")
            name = str(details.get("student", {}).get("name") or student.get("student_name") or "")
            email = str(details.get("student", {}).get("email") or student.get("student_email") or "")
            current_student = verified_by_roll.get(roll, {})
            flags = current_student.get("review_flags", []) if current_student.get("status") not in {"verified", "approved"} else []
            marks_cell = "" if grouping_only else f"<td>{_marks_label(details, verified_by_roll.get(roll))}</td>"
            rows.append(
                "<tr>"
                f"<td><a href=\"/runs/{html.escape(run_id)}/students/{urllib.parse.quote(roll)}\">{html.escape(roll)}</a></td>"
                f"<td>{html.escape(name)}</td>"
                f"<td>{html.escape(email)}</td>"
                f"{marks_cell}"
                f"<td>{_badge(_professor_status(details.get('status'), verified_by_roll.get(roll, {}).get('status')))}</td>"
                f"<td>{len(flags)}</td>"
                f"<td class=\"flags\">{html.escape(' | '.join(str(flag) for flag in flags[:3]))}</td>"
                "</tr>"
            )
        marks_label = "Marks CSV" if state.get("inputs", {}).get("workflow") == "two_step" else "Original Parser Marks CSV"
        marks_action = "" if grouping_only else f'<a class="button" href="{_asset_url(state.get("marks_csv_path"))}">{marks_label}</a>'
        review_link = f"/runs/{html.escape(run_id)}/review"
        marks_heading = "" if grouping_only else "<th>Marks</th>"
        column_count = 6 if grouping_only else 7
        review_label = "Sheet Matching Review" if grouping_only else "Review Cases"
        first_roll = student_view.next_student(verified_index, "", "unchecked", "") or student_view.next_student(verified_index, "", "all", "")
        student_review_link = (
            f'<a class="button" href="{html.escape(student_view.student_url(run_id, first_roll, "unchecked"))}">Review Student Sheets</a>'
            if first_roll else ""
        )
        student_rows_html = "".join(rows) or f'<tr><td colspan="{column_count}" class="empty">No students detected</td></tr>'
        body = f"""
        <h1>{html.escape(state.get('exam_id',''))}</h1>
        <div class="grid">{metric_html}</div>
        {f'<div class="band review">{html.escape(str(state.get("error") or ""))}</div>' if state.get("status") == "grading_failed" else ""}
        {self._ownership_controls(run_id, verified_index)}
        {roll_ocr_warning}
        {grouping_order_notice}
        {self._grading_notice(state)}
        <div class="actions band">
          {student_review_link}
          {self._grading_control(run_id, state)}
          <a class="button secondary" href="/runs/{html.escape(run_id)}/pages">Inspect Source Pages</a>
          {marks_action}
          <a class="button secondary" href="{review_link}">{review_label}</a>
          <a class="button secondary" href="/runs/{html.escape(run_id)}/email">Email Release</a>
          <a class="button secondary" href="{_asset_url(parse_dir / 'review_report.html')}">Original Parser Report</a>
        </div>
        <div class="section-title"><h2>Students</h2></div>
        <table>
          <thead><tr><th>Roll No</th><th>Name</th><th>Email</th>{marks_heading}<th>Status</th><th>Flags</th><th>Review Notes</th></tr></thead>
          <tbody>{student_rows_html}</tbody>
        </table>
        """
        self._send_html("Run Dashboard", body)

    def _student_details(self, run_id: str, roll_no: str) -> tuple[dict[str, Any], Path, dict[str, Any], dict[str, Any] | None]:
        state = self.store.read_state(run_id)
        parse_dir = Path(str(state["parse_dir"]))
        index = _read_json(Path(str(state["parse_index_path"])))
        student_index = next(
            (student for student in index.get("students", []) if str(student.get("roll_no")) == str(roll_no)),
            None,
        )
        verified = None
        verified_path = parse_dir / "verified_index.json"
        if verified_path.exists():
            verified_index = _read_json(verified_path)
            verified = next(
                (student for student in verified_index.get("students", []) if str(student.get("roll_no")) == str(roll_no)),
                None,
            )
        if student_index is not None:
            details = _read_json(_resolve_output_path(student_index["details_path"], parse_dir))
            for key in ("pages", "sheet_pdf_path"):
                details.setdefault(key, student_index.get(key))
        elif verified is not None:
            details = _review_created_details(verified, parse_dir)
        else:
            raise ValueError(f"student {roll_no} not found")
        if verified and verified.get("grading_status") == "graded" and verified.get("grading_details_path"):
            grading_path = _resolve_output_path(verified["grading_details_path"], parse_dir)
            if grading_path.is_file():
                grades = _read_json(grading_path)
                for key in ("mcq_responses", "numerical_responses", "mcq_score", "mcq_total", "numerical_score", "numerical_total"):
                    details[key] = grades.get(key)
                written = []
                for response in grades.get("written_responses", []):
                    entry = dict(response)
                    for key in ("crop_path", "ocr_crop_path"):
                        if entry.get(key):
                            entry[key] = str(_resolve_output_path(entry[key], grading_path.parent))
                    written.append(entry)
                details["written_responses"] = written
        elif verified and verified.get("grading_status") == "stale":
            details["mcq_responses"] = []
            details["numerical_responses"] = []
            details["written_responses"] = []
        return state, parse_dir, details, verified

    def _student_page_views(self, state: dict[str, Any], parse_dir: Path, details: dict[str, Any],
                            verified: dict[str, Any], expected: int) -> list[dict[str, Any]]:
        page_base = parse_dir if verified else _resolve_output_path(details.get("details_path"), parse_dir).parent
        selected = {int(page["page"]): page for page in selected_student_pages(verified or details)}
        run_dir = self.store.run_dir(state["run_id"])
        inventory_path = run_dir / inspection.INDEX_NAME
        inventory = _read_json(inventory_path).get("pages", []) if inventory_path.is_file() else []
        originals = {int(page["source_index"]): page for page in inventory}
        scan_path = Path(str(state.get("inputs", {}).get("scan_path") or "")).resolve()
        views = []
        for number in sorted(set(range(1, expected + 1)) | set(selected)):
            page = selected.get(number, {})
            source = page.get("source_index")
            view = {"page": number, "source_index": source, "origin": page.get("origin") or ("parser" if page else "Missing")}
            for mode, key in (("aligned", "canonical_image_path"), ("overlay", "alignment_overlay_path"),
                              ("sampling", "sampling_overlay_path")):
                path = _resolve_output_path(page.get(key), page_base) if page.get(key) else None
                view[mode] = _asset_url(path) if path and path.is_file() else None
            original = originals.get(source, {}).get("original")
            original_path = run_dir / original if original else None
            source_record = next((row for row in details.get("source_pages", [])
                                  if row.get("source_index") == source
                                  and row.get("page_index") == number), {})
            uploaded = (page.get("origin") == "parser" and source_record.get("source_path")
                        and Path(source_record["source_path"]).resolve() != scan_path)
            # Upload-local page numbers are not positions in the original batch.
            if uploaded:
                view.update(source_index=None, origin="uploaded")
            view["original"] = (_asset_url(original_path) if original_path and original_path.is_file()
                                and not uploaded else None)
            cache_path = run_dir / "identity_preview" / f"source_{int(source or 0):04d}" / "evidence.json"
            cached = _read_json(cache_path).get("page", {}) if not uploaded and cache_path.is_file() else {}
            read = next((row for row in details.get("identity_reads", []) if row.get("source_index") == source
                         and row.get("page_index") == number), {})
            payload = cached.get("identity") or read.get("payload") or {}
            written = payload.get("write_in_roll_read") or (payload if number != 1 else {})
            view["evidence"] = {
                "literal_roll": cached.get("literal_roll_no") or read.get("roll_no"),
                "bubble_roll": payload.get("roll_no") if (cached.get("identity_kind") or read.get("kind")) == "bubbled" else None,
                "written_roll": written.get("roll_no"),
                "confidence": cached.get("confidence") or read.get("confidence"),
                "detected_page": cached.get("sheet_page") or read.get("page_index"),
                "flags": cached.get("review_flags") or [],
            }
            views.append(view)
        return views

    def _student_preview_pdf(self, run_id: str, roll_no: str) -> None:
        _state, parse_dir, details, verified = self._student_details(run_id, roll_no)
        page_base = parse_dir if verified else _resolve_output_path(details.get("details_path"), parse_dir).parent
        image_paths = [_resolve_output_path(page.get("canonical_image_path"), page_base)
                       for page in selected_student_pages(verified or details)]
        if any(not page.get("canonical_image_path") for page in selected_student_pages(verified or details)):
            raise ValueError("A selected page image is unavailable; inspect the source before previewing")
        payload = student_view.preview_pdf(image_paths)
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'inline; filename="{_safe_id(roll_no)}_preview.pdf"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _page_student(self, run_id: str, roll_no: str, query: dict[str, list[str]] | None = None) -> None:
        state, parse_dir, details, verified = self._student_details(run_id, roll_no)
        index, _ = load_or_initialize_verified_index(parse_dir)
        verified = next(row for row in index["students"] if str(row["roll_no"]) == roll_no)
        parameters = {key: values[0] for key, values in (query or {}).items() if values}
        kind, search = student_view.queue_parameters(parameters)
        grouping_only = _is_grouping_only(state)
        student = details.get("student", {})
        score, total = _score(details)
        details_dir = _resolve_output_path(details.get("details_path"), parse_dir).parent
        selection_changed = _selection_changed(details, verified)
        feedback_disabled = 'disabled title="No original parser record for this student"' if details_dir == parse_dir else ""
        page_views = self._student_page_views(state, parse_dir, details, verified, int(index.get("expected_pages") or 0))
        answer_rows = []
        for response in details.get("mcq_responses", []):
            marks_display = (
                "Dropped"
                if response.get("dropped")
                else f"{_fmt_num(response.get('marks_awarded'))} / {_fmt_num(response.get('marks'))}"
            )
            answer_rows.append(
                "<tr>"
                f"<td>Q{html.escape(str(response.get('q_no')))}</td>"
                "<td>MCQ</td>"
                f"<td>{html.escape(str(response.get('selected_option') or ''))}</td>"
                f"<td>{html.escape(str(response.get('correct_option') or ''))}</td>"
                f"<td>{html.escape(marks_display)}</td>"
                f"<td>{_badge('dropped' if response.get('dropped') else response.get('outcome'))}</td>"
                f"<td>{html.escape(str(response.get('confidence') or ''))}</td>"
                f"<td>{html.escape(str(response.get('review_reason') or ''))}</td>"
                "</tr>"
            )
        for response in details.get("numerical_responses", []):
            marks_display = (
                "Dropped"
                if response.get("dropped")
                else f"{_fmt_num(response.get('marks_awarded'))} / {_fmt_num(response.get('max_marks'))}"
            )
            answer_rows.append(
                "<tr>"
                f"<td>Q{html.escape(str(response.get('q_no')))}</td>"
                "<td>Numeric</td>"
                f"<td>{html.escape(str(response.get('value') if response.get('value') is not None else ''))}</td>"
                f"<td>{html.escape(str(response.get('correct_value') if response.get('correct_value') is not None else ''))}</td>"
                f"<td>{html.escape(marks_display)}</td>"
                f"<td>{_badge('dropped' if response.get('dropped') else response.get('outcome'))}</td>"
                f"<td>{html.escape(str(response.get('confidence') or ''))}</td>"
                f"<td>{html.escape(' | '.join(str(flag) for flag in response.get('review_flags', [])))}</td>"
                "</tr>"
            )
        flags = list((verified or details).get("review_flags", []))
        if verified and verified.get("status") in {"verified", "approved"}:
            flags = [f"Missing sheet page {page}" for page in verified.get("missing_pages", [])]
        original_flags = details.get("review_flags", [])
        if verified and verified.get("status") in {"verified", "approved"} and verified.get("verified_sheet_pdf_path"):
            pdf_link = (
                f'<a href="{_asset_url(verified["verified_sheet_pdf_path"], parse_dir)}" target="_blank" rel="noopener">'
                "Open Verified PDF</a>"
            )
        elif verified and verified.get("status") == "auto_matched":
            pdf_link = '<span class="muted">Clean match awaiting release approval</span>'
        elif selection_changed:
            pdf_link = '<span class="muted">Current PDF pending verification</span>'
        else:
            pdf_link = '<span class="muted">Not yet manually checked</span>'
        marks_metric = "" if grouping_only else f'<span>Marks: {html.escape(_marks_label(details, verified))}</span>'
        answers_section = "" if grouping_only else (
            '<p class="review">Page selection changed. Original marks are not current.</p>' if selection_changed else f"""
            <div class="section-title"><h2>Answers And Marks</h2></div>
            <table>
              <thead><tr><th>Question</th><th>Type</th><th>Student Answer</th><th>Correct Answer</th><th>Marks</th><th>Status</th><th>Confidence</th><th>Notes</th></tr></thead>
              <tbody>{''.join(answer_rows) or '<tr><td colspan="8" class="empty">No objective answers found</td></tr>'}</tbody>
            </table>
        """
        )
        edit_marks_operation = "" if grouping_only else f"""
                <div class="operation">
                  <h3>Edit Marks</h3>
                  <form method="post" action="/runs/{html.escape(run_id)}/students/{urllib.parse.quote(roll_no)}/marks">
                    <div class="form-grid">
                      <div><label>Marks Obtained</label><input name="marks_obtained" type="number" step="0.01" value="{html.escape(_fmt_num(score))}" required></div>
                      <div><label>Max Marks</label><input name="max_marks" type="number" step="0.01" value="{html.escape(_fmt_num(total))}"></div>
                      <div class="wide"><label>Note</label><input name="note" placeholder="Reason for mark edit"></div>
                    </div>
                    <p><button type="submit">Save Marks And CSV</button></p>
                  </form>
                </div>
        """
        replacement_button = "Upload And Reprocess Sheet" if grouping_only else "Upload And Re-evaluate"
        operations = f"""<div class="operation-grid">
                {edit_marks_operation}
                <div class="operation">
                  <h3>Roll OCR Training Feedback</h3>
                  <form method="post" action="/runs/{html.escape(run_id)}/students/{urllib.parse.quote(roll_no)}/correct-roll">
                    <div class="form-grid">
                      <div><label>Correct Roll No</label><input name="correct_roll_no" placeholder="2023118" required></div>
                      <div><label>Program</label><select name="program"><option value="BTECH">BTECH</option><option value="MTECH">MTECH</option><option value="PHD">PHD</option></select></div>
                      <div><label>Page</label><input name="page_index" type="number" min="1" value="1" required></div>
                      <div class="wide"><label>Note</label><input name="note" placeholder="Model read wrong roll"></div>
                    </div>
                    <p><button type="submit" {feedback_disabled}>Save Training Feedback</button></p>
                  </form>
                </div>
                <div class="operation">
                  <h3>Replace One Page</h3>
                  <form method="post" enctype="multipart/form-data" action="/runs/{html.escape(run_id)}/students/{urllib.parse.quote(roll_no)}/replace-page">
                    <div class="form-grid">
                      <div><label>Page</label><input name="page_index" type="number" min="1" value="1" required></div>
                      <div class="wide"><label>Correct Page PDF / Image</label><input name="student_page" type="file" accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff" required></div>
                      <div class="wide"><label>Note</label><input name="note" placeholder="Replacing this page only"></div>
                    </div>
                    <p><button type="submit">Replace Page</button></p>
                  </form>
                </div>
                <div class="operation">
                  <h3>Replace Full Student Sheet</h3>
                  <form method="post" enctype="multipart/form-data" action="/runs/{html.escape(run_id)}/students/{urllib.parse.quote(roll_no)}/reupload">
                    <div class="form-grid">
                      <div class="wide"><label>Correct Student PDF / Image</label><input name="student_sheet" type="file" accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff" required></div>
                      <div class="wide"><label>Note</label><input name="note" placeholder="Why this replacement is being uploaded"></div>
                    </div>
                    <p><button type="submit">{replacement_button}</button></p>
                  </form>
                </div>
              </div>"""
        original = f"""
        <p>{'<br>'.join(html.escape(str(flag)) for flag in original_flags) or 'No original flags.'}</p>
        <table><thead><tr><th>Source</th><th>Page</th><th>Used</th></tr></thead><tbody>
        {''.join(f"<tr><td>{html.escape(str(src.get('source_index')))}</td><td>{html.escape(str(src.get('page_index')))}</td><td>{html.escape(str(src.get('used')))}</td></tr>" for src in details.get('source_pages', []))}
        </tbody></table>"""
        current_student = {**verified, "student_name": verified.get("student_name") or student.get("name"),
                           "student_email": verified.get("student_email") or student.get("email")}
        body = student_view.workspace_body(
            state=state, student=current_student, index=index, summary=_review_summary(index), page_views=page_views, flags=flags,
            conflicts=student_view.selection_conflicts(index, verified), revision=self._review_revision(run_id),
            kind=kind, search=search, pdf_link=pdf_link, operations=operations, answers=answers_section,
            original=original, marks=marks_metric, saved=parameters.get("saved", ""),
            finished=parameters.get("finished") == "1",
        )
        self._send_html("Student Review", body)

    def _page_review(self, run_id: str) -> None:
        state = self.store.read_state(run_id)
        grouping_only = _is_grouping_only(state)
        parse_dir = Path(str(state["parse_dir"]))
        index, _verified_path = load_or_initialize_verified_index(parse_dir)
        parse_index_path = Path(str(state.get("parse_index_path") or ""))
        parse_index = _read_json(parse_index_path) if parse_index_path.is_file() else {}
        grouping_order_notice = _grouping_order_notice(parse_index)
        identity_resolution = load_identity_resolution(parse_dir)
        rows = []
        for student in index.get("students", []):
            status = str(student.get("status") or "needs_review")
            flags = list(student.get("review_flags", []))
            if status in {"verified", "approved", "auto_matched"}:
                continue
            roll = str(student.get("roll_no") or "")
            score, total = _score(student)
            missing = ", ".join(str(page) for page in student.get("missing_pages", []))
            decisions = student.get("decision_log", [])
            latest_note = str(decisions[-1].get("note") or "") if decisions else ""
            marks_cell = "" if grouping_only else f"<td>{_marks_label(student, student)}</td>"
            rows.append(
                "<tr>"
                f'<td><a href="{html.escape(student_view.student_url(run_id, roll, "unchecked"))}">{html.escape(roll)}</a></td>'
                f"{marks_cell}"
                f"<td>{_badge(status)}</td>"
                f"<td>{html.escape(missing)}</td>"
                f"<td>{len(flags)}</td>"
                f"<td class=\"flags\">{html.escape(' | '.join(str(flag) for flag in flags))}</td>"
                f"<td>{html.escape(latest_note)}</td>"
                "</tr>"
            )
        unmatched_rows = []
        assigned_sources = {page.get("source_index") for student in index.get("students", []) for page in selected_student_pages(student)}
        for page in index.get("unmatched_pages", []):
            source_index = int(page.get("source_index") or 0)
            page_index = page.get("page_index") or ""
            page_status = str(page.get("status") or "needs_review")
            if page_status != "needs_review" or source_index in assigned_sources:
                continue
            actions = ""
            if page_status == "needs_review":
                actions = f"""
                <form method="post" action="/runs/{html.escape(run_id)}/unmatched/{source_index}/assign">
                  <label>Student roll</label>
                  <input name="roll_no" list="student-rolls" required placeholder="Roll number">
                  <label>OMR page</label>
                  <input name="page_index" type="number" min="1" value="{html.escape(str(page_index))}" required>
                  <label>Review note</label>
                  <input name="note" placeholder="Why this page belongs here">
                  <button type="submit">Assign Page</button>
                </form>
                <form method="post" action="/runs/{html.escape(run_id)}/unmatched/{source_index}/ignore">
                  <input name="note" required placeholder="Reason: duplicate, separator, stray page">
                  <button class="secondary" type="submit">Ignore Page</button>
                </form>
                """
            else:
                actions = html.escape(str(page.get("assigned_to_roll_no") or page_status))
            unmatched_rows.append(
                "<tr>"
                f'<td><a href="/runs/{html.escape(run_id)}/pages?page={source_index}">Source {source_index}</a></td>'
                f"<td>{html.escape(str(page_index))}</td>"
                f"<td>{_badge(page_status)}</td>"
                f"<td class=\"flags\">{html.escape(' | '.join(str(flag) for flag in page.get('review_flags', [])))}</td>"
                f"<td><a href=\"{_asset_url(page.get('details_path'), parse_dir)}\">details</a></td>"
                f"<td>{actions}</td>"
                "</tr>"
            )
        page_error_rows = []
        for error in index.get("page_errors", []):
            if error.get("status") == "ignored" or error.get("source_index") in assigned_sources:
                continue
            page_error_rows.append(
                "<tr>"
                f"<td>{html.escape(str(error.get('source_index') or ''))}</td>"
                f"<td>{_badge(str(error.get('status') or 'error'))}</td>"
                f"<td>{html.escape(str(error.get('error_type') or ''))}</td>"
                f"<td class=\"flags\">{html.escape(' | '.join(str(flag) for flag in error.get('review_flags', [])))}</td>"
                f"<td><a href=\"{_asset_url(error.get('details_path'), parse_dir)}\">details</a></td>"
                "</tr>"
            )
        missing_roster_rows = []
        reconciliation = index.get("roster_reconciliation") or {}
        for student in _missing_roster_students(index):
            missing_roster_rows.append(
                "<tr>"
                f"<td>{html.escape(str(student.get('roll_no') or ''))}</td>"
                f"<td>{html.escape(str(student.get('student_name') or ''))}</td>"
                f"<td>{html.escape(str(student.get('student_email') or ''))}</td>"
                f"<td>{html.escape(str(student.get('program') or ''))}</td>"
                "</tr>"
            )
        known_rolls = {
            str(student.get("roll_no") or "")
            for student in index.get("students", [])
            if student.get("roll_no")
        }
        known_rolls.update(
            str(student.get("roll_no") or "")
            for student in reconciliation.get("missing_students", [])
            if student.get("roll_no")
        )
        roll_options = "".join(
            f'<option value="{html.escape(roll)}">'
            for roll in sorted(known_rolls)
        )
        suggestion_html = _render_identity_suggestions(
            run_id,
            parse_dir,
            index,
            identity_resolution,
        )
        marks_heading = "" if grouping_only else "<th>Marks</th>"
        review_column_count = 6 if grouping_only else 7
        review_rows_html = "".join(rows) or f'<tr><td colspan="{review_column_count}" class="empty">No student review cases</td></tr>'
        review_title = "Sheet Matching Review" if grouping_only else "Review Cases"
        first_roll = student_view.next_student(index, "", "unchecked", "") or student_view.next_student(index, "", "all", "")
        student_review_link = (
            f'<a class="button" href="{html.escape(student_view.student_url(run_id, first_roll, "unchecked"))}">Review Student Sheets</a>'
            if first_roll else ""
        )
        body = f"""
        <h1>{review_title}</h1>
        {_review_metrics(_review_summary(index))}
        {self._grading_notice(state)}
        {self._ownership_controls(run_id, index)}
        <div class="actions band"><a class="button secondary" href="/runs/{html.escape(run_id)}">Back to Exam</a>
        <a class="button secondary" href="/runs/{html.escape(run_id)}/pages">Source Pages</a>{student_review_link}{self._grading_control(run_id, state)}</div>
        {grouping_order_notice}
        <datalist id="student-rolls">{roll_options}</datalist>
        {suggestion_html}
        <div class="section-title"><h2>Students Needing Review</h2></div>
        <table><thead><tr><th>Roll No</th>{marks_heading}<th>Status</th><th>Missing Pages</th><th>Flags</th><th>Parser Notes</th><th>Latest Decision</th></tr></thead>
        <tbody>{review_rows_html}</tbody></table>
        <div class="section-title"><h2>Unmatched Pages</h2></div>
        <table><thead><tr><th>Source Index</th><th>Detected Page</th><th>Status</th><th>Flags</th><th>Details</th><th>Resolution</th></tr></thead>
        <tbody>{''.join(unmatched_rows) or '<tr><td colspan="6" class="empty">No unmatched pages</td></tr>'}</tbody></table>
        <div class="section-title"><h2>Unreadable Pages</h2></div>
        <table><thead><tr><th>Source Index</th><th>Status</th><th>Error</th><th>Notes</th><th>Details</th></tr></thead>
        <tbody>{''.join(page_error_rows) or '<tr><td colspan="5" class="empty">No unreadable pages</td></tr>'}</tbody></table>
        <div class="section-title"><h2>Roster Students Without A Detected Sheet</h2></div>
        <table><thead><tr><th>Roll No</th><th>Name</th><th>Email</th><th>Program</th></tr></thead>
        <tbody>{''.join(missing_roster_rows) or '<tr><td colspan="4" class="empty">Every roster student has a detected sheet</td></tr>'}</tbody></table>
        """
        self._send_html(review_title, body)

    def _page_email(self, run_id: str) -> None:
        state = self.store.read_state(run_id)
        grouping_only = _is_grouping_only(state)
        parse_dir = Path(str(state.get("parse_dir") or ""))
        release_dir = parse_dir / "email_release"
        queue_path = release_dir / EMAIL_QUEUE_CSV
        skipped_path = release_dir / EMAIL_SKIPPED_CSV
        release_options = (
            '<option value="sheet_verification">Verified Sheet - no marks</option>'
            if grouping_only
            else (
                '<option value="sheet_verification">Sheet Verification - no marks</option>'
                '<option value="evaluated_marks">Evaluated Sheet + Marks</option>'
            )
        )
        template_placeholders = (
            "{exam_id}, {roll_no}, {name}, {display_name}"
            if grouping_only
            else "{exam_id}, {roll_no}, {name}, {display_name}, {marks_obtained}, {max_marks}"
        )
        body = f"""
        <h1>Email Release</h1>
        {_review_metrics(_review_summary(self._current_review(state) or {}))}
        <div class="actions band"><a class="button secondary" href="/runs/{html.escape(run_id)}">Back to Exam</a></div>
        <section class="band">
          <h2>Prepare Queue</h2>
          <p class="muted">This creates preview emails and queues only verified/eligible students. It does not send anything.</p>
          <form method="post" action="/runs/{html.escape(run_id)}/email/prepare">
            <div class="grid">
              <div>
                <label>Release Type</label>
                <select name="release_mode">
                  {release_options}
                </select>
              </div>
              <div>
                <label>Sender Email</label>
                <input name="sender" placeholder="professor@iiitd.ac.in">
              </div>
              <div>
                <label>Sender Name</label>
                <input name="sender_name" placeholder="Course Staff">
              </div>
            </div>
            <label>Subject</label>
            <input name="subject" placeholder="Uses a safe default for the selected release type">
            <label>Custom Message</label>
            <textarea name="body_template" rows="10" placeholder="Leave blank to use the release-type default"></textarea>
            <p class="muted">Available placeholders: {html.escape(template_placeholders)}</p>
            <button type="submit">Prepare Email Queue</button>
          </form>
        </section>
        """
        if queue_path.exists():
            queued = list(csv.DictReader(queue_path.open(newline="", encoding="utf-8-sig")))
            skipped = list(csv.DictReader(skipped_path.open(newline="", encoding="utf-8-sig"))) if skipped_path.exists() else []
            preview_dir = release_dir / "previews"
            log_path = release_dir / EMAIL_LOG_CSV
            last_send = state.get("last_email_send") or {}
            commands = f""".\\.venv\\Scripts\\python.exe -m omr.workflows.email send `
  --queue-csv "{queue_path}" `
  --smtp-host smtp.gmail.com `
  --smtp-port 587 `
  --username "professor@gmail.com" `
  --sender "professor@gmail.com" `
  --sender-name "Course Staff"

# safe test: redirects one complete message to course staff
.\\.venv\\Scripts\\python.exe -m omr.workflows.email send `
  --queue-csv "{queue_path}" `
  --smtp-host smtp.gmail.com `
  --smtp-port 587 `
  --username "professor@gmail.com" `
  --sender "professor@gmail.com" `
  --sender-name "Course Staff" `
  --test-recipient "professor@gmail.com" `
  --send

# real student send after reviewing the redirected test
.\\.venv\\Scripts\\python.exe -m omr.workflows.email send `
  --queue-csv "{queue_path}" `
  --smtp-host smtp.gmail.com `
  --smtp-port 587 `
  --username "professor@gmail.com" `
  --sender "professor@gmail.com" `
  --sender-name "Course Staff" `
  --confirm-count {len(queued)} `
  --send"""
            rows = []
            for row in queued[:50]:
                marks_cell = "" if grouping_only else (
                    f"<td>{html.escape('Not included' if row.get('release_mode') == 'sheet_verification' else str(row.get('marks_obtained', '')) + ' / ' + str(row.get('max_marks', '')))}</td>"
                )
                rows.append(
                    "<tr>"
                    f"<td>{html.escape(row.get('roll_no',''))}</td>"
                    f"<td>{html.escape(row.get('student_name',''))}</td>"
                    f"<td>{html.escape(row.get('student_email',''))}</td>"
                    f"<td>{html.escape('Sheet verification' if row.get('release_mode') == 'sheet_verification' else 'Evaluated marks')}</td>"
                    f"{marks_cell}"
                    f"<td><a href=\"{_asset_url(row.get('preview_eml_path'), parse_dir)}\">preview</a></td>"
                    "</tr>"
                )
            queue_marks_heading = "" if grouping_only else "<th>Marks</th>"
            queue_column_count = 5 if grouping_only else 6
            body += f"""
            <div class="grid">
              <div class="metric"><span>Queued At Preparation</span><strong>{len(queued)}</strong></div>
              <div class="metric"><span>Skipped At Preparation</span><strong>{len(skipped)}</strong></div>
            </div>
            <div class="actions band">
              <a class="button" href="{_asset_url(queue_path)}">Download Email Queue</a>
              <a class="button secondary" href="{_asset_url(skipped_path)}">Download Skipped CSV</a>
            </div>
            <section class="band">
              <h2>Send From UI</h2>
              <p class="muted">For testing, enter Test Recipient and optionally Only Roll No. The selected student's complete email is redirected to the test address; without a roll or limit, only one queued email is sent as a test.</p>
              <form method="post" action="/runs/{html.escape(run_id)}/email/send">
                <div class="grid">
                  <div>
                    <label>Mode</label>
                    <select name="mode">
                      <option value="dry_run">Dry Run - send nothing</option>
                      <option value="send">Real Send</option>
                    </select>
                  </div>
                  <div>
                    <label>SMTP Host</label>
                    <input name="smtp_host" value="smtp.gmail.com">
                  </div>
                  <div>
                    <label>SMTP Port</label>
                    <input name="smtp_port" value="587">
                  </div>
                  <div>
                    <label>SMTP Security</label>
                    <select name="smtp_security">
                      <option value="starttls">STARTTLS</option>
                      <option value="ssl">SSL/TLS</option>
                    </select>
                  </div>
                  <div>
                    <label>Username</label>
                    <input name="username" placeholder="professor@gmail.com">
                  </div>
                  <div>
                    <label>Sender Email</label>
                    <input name="sender" placeholder="professor@gmail.com">
                  </div>
                  <div>
                    <label>Sender Name</label>
                    <input name="sender_name" placeholder="Course Staff">
                  </div>
                  <div>
                    <label>Password / App Password</label>
                    <input name="password" type="password" autocomplete="off">
                  </div>
                  <div>
                    <label>Limit</label>
                    <input name="limit" placeholder="optional; test defaults to 1">
                  </div>
                  <div>
                    <label>Test Recipient Override</label>
                    <input name="test_recipient" placeholder="professor@iiitd.ac.in">
                  </div>
                  <div>
                    <label>Confirm Recipient Count</label>
                    <input name="confirm_count" type="number" min="1" placeholder="required for real student send">
                  </div>
                  <div>
                    <label>Only Roll No / Test This Student</label>
                    <input name="only_roll" placeholder="optional roll number">
                  </div>
                  <div>
                    <label>Delay Between Emails (seconds)</label>
                    <input name="delay_seconds" type="number" min="0" step="0.05" value="0.25">
                  </div>
                </div>
                <button type="submit">Run Email Send</button>
              </form>
              <p class="muted">Latest: {html.escape(json.dumps(last_send) if last_send else 'No send run yet')}</p>
            </section>
            <div class="section-title"><h2>Queued Students</h2></div>
            <table>
              <thead><tr><th>Roll No</th><th>Name</th><th>Email</th><th>Release Type</th>{queue_marks_heading}<th>Preview</th></tr></thead>
              <tbody>{''.join(rows) or f'<tr><td colspan="{queue_column_count}" class="empty">No queued emails</td></tr>'}</tbody>
            </table>
            <div class="actions band">
              <a class="button secondary" href="{_asset_url(log_path)}">Download Send Log</a>
            </div>
            <h2>Tomorrow Send Commands</h2>
            <div class="band"><pre>{html.escape(commands)}</pre></div>
            """
        self._send_html("Email Release", body)

    def _serve_artifact(self, query: dict[str, list[str]]) -> None:
        value = (query.get("path") or [""])[0]
        if not value:
            self._not_found("missing artifact path")
            return
        path = Path(value).resolve()
        allowed_roots = [self.store.config.data_dir.resolve()]
        if not any(path == root or root in path.parents for root in allowed_roots):
            self._not_found("artifact path is outside SmartOMR workspace")
            return
        if not path.exists() or not path.is_file():
            self._not_found(f"artifact not found: {path}")
            return
        content_type, _encoding = mimetypes.guess_type(str(path))
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        if path.suffix.lower() in {".csv", ".pdf"}:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(data)


def run_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, data_dir: Path = DEFAULT_DATA_DIR) -> None:
    config = UiConfig(data_dir=data_dir)
    SmartOmrUiHandler.store = RunStore(config)
    server = ThreadingHTTPServer((host, port), SmartOmrUiHandler)
    print(f"SmartOMR Professor UI running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the local SmartOMR professor review UI")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args(argv)
    run_server(args.host, args.port, args.data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
