"""Local professor review UI for SmartOMR.

This is intentionally file-based. It wraps the existing batch parser and review
artifacts so the professor can upload a scanned bundle, watch progress, inspect
student-wise marks, and open full-sheet overlays without requiring a database.
"""
from __future__ import annotations

import csv
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
    assign_unmatched_page,
    hold_student,
    ignore_unmatched_page,
    initialize_verification_index,
    load_or_initialize_verified_index,
    reject_student,
    verify_student,
)


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
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


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
            if question["kind"] == "numerical" and not re.fullmatch(r"[0-9]+", answer):
                raise ValueError(f"numerical Q{q_no} answer must be digits only")
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
    table {{ width: 100%; border-collapse: separate; border-spacing: 0; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; overflow: hidden; }}
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
    .actions {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
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
    @media (max-width: 1040px) {{ .student-layout, .split {{ grid-template-columns: 1fr; }} }}
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
    if lowered in {"ready", "verified", "auto_graded", "auto graded", "correct"}:
        cls = "ready"
    elif "review" in lowered or "pending" in lowered or "missing" in lowered:
        cls = "review"
    elif "error" in lowered or "failed" in lowered or "wrong" in lowered or "rejected" in lowered:
        cls = "error"
    return f'<span class="badge {cls}">{text}</span>'


def _professor_status(parser_status: str | None, verified_status: str | None = None) -> str:
    if verified_status == "verified":
        return "MANUALLY_CHECKED"
    if verified_status in {"needs_review", "missing_pages", "rejected"}:
        return "NEEDS_REVIEW"
    if parser_status == "ready":
        return "AUTO_GRADED"
    if parser_status == "needs_review":
        return "NEEDS_REVIEW"
    if parser_status == "error":
        return "FAILED"
    return str(parser_status or "NEEDS_REVIEW")


def _score(student: dict[str, Any]) -> tuple[float, float]:
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
    return score, total


def _fmt_num(value: float | int | None) -> str:
    if value is None:
        return ""
    value = float(value)
    return str(int(value)) if value.is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


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
    return backend, {
        "enabled": backend is not None,
        "provider": getattr(backend, "provider", "local") if backend is not None else "none",
        "warning": None,
    }


def _require_ui_roll_ocr_backend() -> tuple[object, dict[str, Any]]:
    backend, state = _build_ui_roll_ocr_backend()
    if backend is None:
        detail = str(state.get("warning") or "local roll OCR could not be initialized")
        raise RuntimeError(
            "Roll-number OCR is required for professor UI processing. "
            f"{detail} Ensure data/models/roll_digit_resnet_omr_finetuned.pt is present, then start the run again."
        )
    return backend, state


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
        details_path = _resolve_output_path(student["details_path"], parse_dir)
        details = _read_json(details_path)
        score, total = _score(details)
        rows.append(
            {
                "roll_no": details.get("student", {}).get("roll_no") or student.get("roll_no") or "",
                "name": details.get("student", {}).get("name") or student.get("student_name") or "",
                "email": details.get("student", {}).get("email") or student.get("student_email") or "",
                "status": details.get("status") or student.get("status") or "",
                "marks_obtained": _fmt_num(score),
                "max_marks": _fmt_num(total),
                "manual_override": "yes" if details.get("manual_score_override") is not None else "",
                "manual_note": details.get("manual_score_note") or "",
                "review_flag_count": len(details.get("review_flags", [])),
                "review_flags": " | ".join(str(flag) for flag in details.get("review_flags", [])),
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
    result.setdefault("review_flags", []).append("student sheet was re-uploaded and re-evaluated from professor UI")
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
    result.setdefault("review_flags", []).append(
        f"page {target_page} was replaced from professor UI and the student was re-evaluated"
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
    marks_csv = _write_marks_csv(run_dir, parse_dir, index)
    counts = index.get("status_counts", {})
    store.write_state(
        run_id,
        marks_csv_path=str(marks_csv),
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

    def run_dir(self, run_id: str) -> Path:
        return self.config.runs_dir / _safe_id(run_id)

    def state_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / RUN_STATE_NAME

    def read_state(self, run_id: str) -> dict[str, Any]:
        return _read_json(self.state_path(run_id))

    def write_state(self, run_id: str, **updates: Any) -> dict[str, Any]:
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

    def create_run(self, exam_id: str, files: dict[str, UploadedFile], fields: dict[str, str] | None = None) -> dict[str, Any]:
        fields = fields or {}
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
            target = inputs / _safe_id(Path(upload.filename).name)
            shutil.copy2(upload.path, target)
            saved[field] = str(target)

        manifest_path = Path(str(saved.get("manifest") or ""))
        if not manifest_path.exists():
            raise ValueError("manifest.json is required")
        scan_path = Path(str(saved.get("scan_pdf") or ""))
        if not scan_path.exists():
            raise ValueError("student OMR PDF/image is required")

        manifest = load_manifest(manifest_path)
        manifest_exam_id = str(manifest.get("exam_id") or exam_id).strip()
        if not exam_id:
            exam_id = manifest_exam_id

        answer_key_csv = None
        if saved.get("answer_key"):
            answer_key_csv = inputs / "answer_key.csv"
            _normalize_tabular_upload(Path(str(saved["answer_key"])), answer_key_csv)
        else:
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
            "status": "queued",
            "stage": "Queued",
            "created_at": _now(),
            "updated_at": _now(),
            "inputs": {
                "manifest_path": str(manifest_path),
                "scan_path": str(scan_path),
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

    def start_run(self, run_id: str) -> None:
        thread = threading.Thread(target=self._run_batch, args=(run_id,), daemon=True)
        thread.start()

    def _run_batch(self, run_id: str) -> None:
        try:
            state = self.write_state(run_id, status="running", stage="Loading inputs", error=None)
            inputs = state["inputs"]
            run_dir = self.run_dir(run_id)
            parsed_root = run_dir / "parsed"
            self.write_state(run_id, stage="Preparing roll OCR cross-check")
            roll_ocr_backend, roll_ocr_state = _require_ui_roll_ocr_backend()
            self.write_state(run_id, roll_ocr=roll_ocr_state)
            self.write_state(run_id, stage="Rendering PDF and aligning pages")

            def update_progress(progress: dict[str, Any]) -> None:
                phase = str(progress.get("phase") or "")
                processed = int(progress.get("processed") or 0)
                total = int(progress.get("total") or 0)
                errors = int(progress.get("errors") or 0)
                if phase == "reading_pages":
                    stage = f"Reading and aligning page {processed}/{total}" if total else f"Reading page {processed}"
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
            )
            parse_dir = index_path.parent
            self.write_state(run_id, stage="Preparing review dashboard")
            initialize_verification_index(
                parse_dir,
                force=True,
                auto_verify_ready=True,
                reviewer="professor-ui",
                note="Auto-graded with zero parser flags; automatically marked checked.",
            )
            index = _read_json(index_path)
            marks_csv = _write_marks_csv(run_dir, parse_dir, index)
            counts = index.get("status_counts", {})
            self.write_state(
                run_id,
                status="completed",
                stage="Completed",
                parse_dir=str(parse_dir),
                parse_index_path=str(index_path),
                marks_csv_path=str(marks_csv),
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

    def _send_html(self, title: str, body: str, *, status: HTTPStatus = HTTPStatus.OK, refresh: int | None = None) -> None:
        payload = _html_page(title, body, refresh_seconds=refresh)
        self.send_response(status.value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
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
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/":
                self._page_runs()
            elif path == "/new":
                self._page_new()
            elif path == "/generate":
                self._page_generate()
            elif path == "/artifact":
                self._serve_artifact(query)
            elif path.startswith("/runs/"):
                parts = [part for part in path.split("/") if part]
                if len(parts) == 2:
                    self._page_run(parts[1])
                elif len(parts) == 4 and parts[2] == "students":
                    self._page_student(parts[1], urllib.parse.unquote(parts[3]))
                elif len(parts) == 3 and parts[2] == "review":
                    self._page_review(parts[1])
                elif len(parts) == 3 and parts[2] == "email":
                    self._page_email(parts[1])
                else:
                    self._not_found()
            else:
                self._not_found()
        except Exception as exc:
            body = f"<h1>UI Error</h1><p>{html.escape(str(exc))}</p><pre>{html.escape(traceback.format_exc())}</pre>"
            self._send_html("Error", body, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/runs":
                self._create_run()
            elif path == "/generate":
                self._generate_omr()
            elif path.startswith("/runs/"):
                parts = [part for part in path.split("/") if part]
                if len(parts) == 5 and parts[2] == "students" and parts[4] in {"verify", "hold", "reject"}:
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
                elif len(parts) == 4 and parts[2] == "email" and parts[3] == "prepare":
                    self._prepare_email(parts[1])
                elif len(parts) == 4 and parts[2] == "email" and parts[3] == "send":
                    self._send_email(parts[1])
                else:
                    self._not_found()
            else:
                self._not_found()
        except Exception as exc:
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
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
        self.store.start_run(state["run_id"])
        self._redirect(f"/runs/{state['run_id']}")

    def _student_decision(self, run_id: str, roll_no: str, action: str) -> None:
        state = self.store.read_state(run_id)
        parse_dir = Path(str(state.get("parse_dir") or ""))
        form = self._urlencoded_form()
        note = (form.get("note") or "").strip()
        if action == "verify":
            verify_student(
                parse_dir,
                roll_no,
                reviewer="professor-ui",
                note=note or "Marked manually checked from UI.",
                allow_missing=True,
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
        self._redirect(f"/runs/{run_id}/students/{urllib.parse.quote(roll_no)}")

    def _update_student_marks(self, run_id: str, roll_no: str) -> None:
        state = self.store.read_state(run_id)
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
                if verified_student.get("status") == "verified":
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
                    "note": note or f"Uploaded replacement sheet {saved_upload.name} and re-evaluated only this student.",
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
            marks_csv = _write_marks_csv(run_dir, parse_dir, index)
            counts = index.get("status_counts", {})
            self.store.write_state(
                run_id,
                marks_csv_path=str(marks_csv),
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
                    "note": note or f"Replaced page {target_page} with {saved_upload.name} and re-evaluated this student.",
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
            assign_unmatched_page(
                parse_dir,
                source_index,
                roll_no,
                page_index=int(page_value) if page_value else None,
                reviewer="professor-ui",
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
            summary = state.get("summary") or {}
            rows.append(
                "<tr>"
                f"<td><a href=\"/runs/{html.escape(state['run_id'])}\">{html.escape(state.get('exam_id',''))}</a></td>"
                f"<td>{_badge(state.get('status'))}</td>"
                f"<td>{html.escape(str(summary.get('students','')))}</td>"
                f"<td>{html.escape(str(summary.get('ready','')))}</td>"
                f"<td>{html.escape(str(summary.get('needs_review','')))}</td>"
                f"<td>{html.escape(str(state.get('created_at','')))}</td>"
                "</tr>"
            )
        table = """
        <table>
          <thead><tr><th>Exam</th><th>Status</th><th>Students</th><th>Ready</th><th>Needs Review</th><th>Created</th></tr></thead>
          <tbody>{}</tbody>
        </table>
        """.format("".join(rows) or '<tr><td colspan="6" class="empty">No runs yet</td></tr>')
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
        <h1>Check Copies</h1>
        <form method="post" action="/runs" enctype="multipart/form-data" class="band">
          <div class="grid">
            <div>
              <label>Exam ID</label>
              <input name="exam_id" placeholder="CSE557_QUIZ1_2026">
            </div>
            <div>
              <label>Scanned filled OMR PDF / image</label>
              <input type="file" name="scan_pdf" required accept=".pdf,.png,.jpg,.jpeg,.tif,.tiff">
            </div>
            <div>
              <label>Manifest JSON</label>
              <input id="manifest-file" type="file" name="manifest" required accept=".json">
            </div>
            <div>
              <label>Answer key Excel / CSV fallback</label>
              <input type="file" name="answer_key" accept=".csv,.xlsx">
            </div>
            <div>
              <label>Master student list Excel / CSV</label>
              <input type="file" name="master_list" accept=".csv,.xlsx">
            </div>
          </div>
          <section class="band" style="margin-top:16px">
            <h2>Answer Key</h2>
            <input type="hidden" name="answer_key_mode" value="manifest_form">
            <p class="muted">Choose the manifest above and the objective questions will appear here. Mark a question dropped when it should not count in the total.</p>
            <div id="answer-key-summary" class="muted">Waiting for manifest.json...</div>
            <div id="answer-key-table"></div>
          </section>
          <p class="muted">Uploaded answer key CSV/XLSX takes priority over the table above. Master list columns: roll_no, name, email, program. First-page written roll OCR cross-check runs automatically when local Tesseract is available.</p>
          <button type="submit">Start Evaluation</button>
        </form>
        <script>
        const manifestInput = document.getElementById("manifest-file");
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
              : `<input name="answer_q${q.q_no}" pattern="[0-9]+" inputmode="numeric" placeholder="${"0".repeat(Math.max(1, q.digits || 1))}">`;
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

        manifestInput?.addEventListener("change", async () => {
          const file = manifestInput.files?.[0];
          if (!file) return;
          try {
            renderAnswerKey(JSON.parse(await file.text()));
          } catch (error) {
            summary.textContent = `Could not read manifest: ${error}`;
            target.innerHTML = "";
          }
        });
        </script>
        """
        self._send_html("New Run", body)

    def _page_run(self, run_id: str) -> None:
        state = self.store.read_state(run_id)
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
        if state.get("status") != "completed":
            body = f"<h1>{html.escape(state.get('exam_id',''))}</h1><div class=\"grid\">{metric_html}</div>{roll_ocr_warning}<p class=\"muted\">This page refreshes while the evaluation runs.</p>"
            self._send_html("Run Progress", body, refresh=refresh)
            return

        parse_dir = Path(str(state["parse_dir"]))
        index = _read_json(Path(str(state["parse_index_path"])))
        verified_by_roll: dict[str, str] = {}
        verified_path = parse_dir / "verified_index.json"
        if verified_path.exists():
            verified_index = _read_json(verified_path)
            verified_by_roll = {
                str(student.get("roll_no")): str(student.get("status"))
                for student in verified_index.get("students", [])
            }
        rows = []
        for student in index.get("students", []):
            details = _read_json(_resolve_output_path(student["details_path"], parse_dir))
            score, total = _score(details)
            roll = str(details.get("student", {}).get("roll_no") or student.get("roll_no") or "")
            name = str(details.get("student", {}).get("name") or student.get("student_name") or "")
            email = str(details.get("student", {}).get("email") or student.get("student_email") or "")
            flags = details.get("review_flags", [])
            rows.append(
                "<tr>"
                f"<td><a href=\"/runs/{html.escape(run_id)}/students/{urllib.parse.quote(roll)}\">{html.escape(roll)}</a></td>"
                f"<td>{html.escape(name)}</td>"
                f"<td>{html.escape(email)}</td>"
                f"<td>{_fmt_num(score)} / {_fmt_num(total)}</td>"
                f"<td>{_badge(_professor_status(details.get('status'), verified_by_roll.get(roll)))}</td>"
                f"<td>{len(flags)}</td>"
                f"<td class=\"flags\">{html.escape(' | '.join(str(flag) for flag in flags[:3]))}</td>"
                "</tr>"
            )
        marks_link = _asset_url(state.get("marks_csv_path"))
        review_link = f"/runs/{html.escape(run_id)}/review"
        body = f"""
        <h1>{html.escape(state.get('exam_id',''))}</h1>
        <div class="grid">{metric_html}</div>
        {roll_ocr_warning}
        <div class="actions band">
          <a class="button" href="{marks_link}">Download Marks CSV</a>
          <a class="button secondary" href="{review_link}">Review Cases</a>
          <a class="button secondary" href="/runs/{html.escape(run_id)}/email">Email Release</a>
          <a class="button secondary" href="{_asset_url(parse_dir / 'review_report.html')}">Open Raw Review Report</a>
        </div>
        <div class="section-title"><h2>Students</h2></div>
        <table>
          <thead><tr><th>Roll No</th><th>Name</th><th>Email</th><th>Marks</th><th>Status</th><th>Flags</th><th>Review Notes</th></tr></thead>
          <tbody>{''.join(rows) or '<tr><td colspan="7" class="empty">No students detected</td></tr>'}</tbody>
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
        if student_index is None:
            raise ValueError(f"student {roll_no} not found")
        details = _read_json(_resolve_output_path(student_index["details_path"], parse_dir))
        verified = None
        verified_path = parse_dir / "verified_index.json"
        if verified_path.exists():
            verified_index = _read_json(verified_path)
            verified = next(
                (student for student in verified_index.get("students", []) if str(student.get("roll_no")) == str(roll_no)),
                None,
            )
        return state, parse_dir, details, verified

    def _page_student(self, run_id: str, roll_no: str) -> None:
        state, parse_dir, details, verified = self._student_details(run_id, roll_no)
        student = details.get("student", {})
        score, total = _score(details)
        status = verified.get("status") if verified else details.get("status")
        display_status = _professor_status(details.get("status"), status if verified else None)
        details_dir = _resolve_output_path(details.get("details_path"), parse_dir).parent
        pages = []
        for page in details.get("pages", []):
            page_no = page.get("page_index")
            page_base = details_dir
            for label, key in (
                ("Aligned Page", "canonical_image_path"),
                ("Alignment Overlay", "alignment_overlay_path"),
                ("Sampling Overlay", "sampling_overlay_path"),
            ):
                if page.get(key):
                    pages.append(
                        f"""<figure>
                          <figcaption>{html.escape(label)} {html.escape(str(page_no))}</figcaption>
                          <a href="{_asset_url(page[key], page_base)}" target="_blank"><img class="sheet" src="{_asset_url(page[key], page_base)}" alt="{html.escape(label)}"></a>
                        </figure>"""
                    )
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
        flags = details.get("review_flags", [])
        missing_pages = list((verified or {}).get("missing_pages", []))
        missing_note = (
            f'<span class="muted">Missing page(s) {html.escape(", ".join(str(page) for page in missing_pages))}; '
            "manual check will keep this warning.</span>"
            if missing_pages
            else ""
        )
        verify_action = (
            f'<form method="post" action="/runs/{html.escape(run_id)}/students/{urllib.parse.quote(roll_no)}/verify">'
            '<button type="submit">Mark Manually Checked</button></form>'
            f"{missing_note}"
        )
        decision_actions = f"""
        {verify_action}
        <form method="post" action="/runs/{html.escape(run_id)}/students/{urllib.parse.quote(roll_no)}/hold"><button class="secondary" type="submit">Keep Needs Review</button></form>
        <form method="post" action="/runs/{html.escape(run_id)}/students/{urllib.parse.quote(roll_no)}/reject"><button class="secondary" type="submit">Reject Grouping</button></form>
        """
        body = f"""
        <h1>Student {html.escape(str(student.get('roll_no') or roll_no))}</h1>
        <div class="grid">
          <div class="metric"><span>Name</span><strong>{html.escape(str(student.get('name') or ''))}</strong></div>
          <div class="metric"><span>Email</span><strong>{html.escape(str(student.get('email') or ''))}</strong></div>
          <div class="metric"><span>Marks</span><strong>{_fmt_num(score)} / {_fmt_num(total)}</strong></div>
          <div class="metric"><span>Status</span><strong>{_badge(display_status)}</strong></div>
        </div>
        <div class="actions band toolbar">
          <div class="actions">{decision_actions}</div>
          <div class="actions">
            <a class="button secondary" href="/runs/{html.escape(run_id)}">Back to Exam</a>
            <a class="button secondary" href="{_asset_url(details.get('sheet_pdf_path'), details_dir)}">Open Student PDF</a>
          </div>
        </div>
        <div class="student-layout">
          <section>
            <div class="section-title"><h2>Full Sheet Images</h2></div>
            <div class="pages">{''.join(pages) or '<p class="empty">No page images found</p>'}</div>
            <div class="section-title"><h2>Answers And Marks</h2></div>
            <table>
              <thead><tr><th>Question</th><th>Type</th><th>Student Answer</th><th>Correct Answer</th><th>Marks</th><th>Status</th><th>Confidence</th><th>Notes</th></tr></thead>
              <tbody>{''.join(answer_rows) or '<tr><td colspan="8" class="empty">No objective answers found</td></tr>'}</tbody>
            </table>
          </section>
          <aside class="side-stack">
            <section class="band">
              <h2>Review Flags</h2>
              <div>{'<br>'.join(html.escape(str(flag)) for flag in flags) if flags else '<span class="muted">No review flags.</span>'}</div>
            </section>
            <section class="band">
              <h2>Review Operations</h2>
              <div class="operation-grid">
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
                <div class="operation">
                  <h3>Correct Roll Mapping</h3>
                  <form method="post" action="/runs/{html.escape(run_id)}/students/{urllib.parse.quote(roll_no)}/correct-roll">
                    <div class="form-grid">
                      <div><label>Correct Roll No</label><input name="correct_roll_no" placeholder="2023118" required></div>
                      <div><label>Program</label><select name="program"><option value="BTECH">BTECH</option><option value="MTECH">MTECH</option><option value="PHD">PHD</option></select></div>
                      <div><label>Page</label><input name="page_index" type="number" min="1" value="1" required></div>
                      <div class="wide"><label>Note</label><input name="note" placeholder="Model read wrong roll"></div>
                    </div>
                    <p><button type="submit">Save Training Feedback</button></p>
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
                    <p><button type="submit">Upload And Re-evaluate</button></p>
                  </form>
                </div>
              </div>
            </section>
            <section class="band">
              <h2>Source Pages</h2>
              <table><thead><tr><th>Source</th><th>Page</th><th>Used</th></tr></thead><tbody>
              {''.join(f"<tr><td>{html.escape(str(src.get('source_index')))}</td><td>{html.escape(str(src.get('page_index')))}</td><td>{html.escape(str(src.get('used')))}</td></tr>" for src in details.get('source_pages', []))}
              </tbody></table>
            </section>
          </aside>
        </div>
        """
        self._send_html("Student Detail", body)

    def _page_review(self, run_id: str) -> None:
        state = self.store.read_state(run_id)
        parse_dir = Path(str(state["parse_dir"]))
        index, _verified_path = load_or_initialize_verified_index(parse_dir)
        rows = []
        for student in index.get("students", []):
            status = str(student.get("status") or "needs_review")
            flags = list(student.get("review_flags", []))
            if status == "verified":
                continue
            roll = str(student.get("roll_no") or "")
            score, total = _score(student)
            missing = ", ".join(str(page) for page in student.get("missing_pages", []))
            decisions = student.get("decision_log", [])
            latest_note = str(decisions[-1].get("note") or "") if decisions else ""
            rows.append(
                "<tr>"
                f"<td><a href=\"/runs/{html.escape(run_id)}/students/{urllib.parse.quote(roll)}\">{html.escape(roll)}</a></td>"
                f"<td>{_fmt_num(score)} / {_fmt_num(total)}</td>"
                f"<td>{_badge(status)}</td>"
                f"<td>{html.escape(missing)}</td>"
                f"<td>{len(flags)}</td>"
                f"<td class=\"flags\">{html.escape(' | '.join(str(flag) for flag in flags))}</td>"
                f"<td>{html.escape(latest_note)}</td>"
                "</tr>"
            )
        unmatched_rows = []
        for page in index.get("unmatched_pages", []):
            source_index = int(page.get("source_index") or 0)
            page_index = page.get("page_index") or ""
            page_status = str(page.get("status") or "needs_review")
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
                f"<td>{source_index}</td>"
                f"<td>{html.escape(str(page_index))}</td>"
                f"<td>{_badge(page_status)}</td>"
                f"<td class=\"flags\">{html.escape(' | '.join(str(flag) for flag in page.get('review_flags', [])))}</td>"
                f"<td><a href=\"{_asset_url(page.get('details_path'), parse_dir)}\">details</a></td>"
                f"<td>{actions}</td>"
                "</tr>"
            )
        page_error_rows = []
        for error in index.get("page_errors", []):
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
        for student in reconciliation.get("missing_students", []):
            missing_roster_rows.append(
                "<tr>"
                f"<td>{html.escape(str(student.get('roll_no') or ''))}</td>"
                f"<td>{html.escape(str(student.get('student_name') or ''))}</td>"
                f"<td>{html.escape(str(student.get('student_email') or ''))}</td>"
                f"<td>{html.escape(str(student.get('program') or ''))}</td>"
                "</tr>"
            )
        roll_options = "".join(
            f'<option value="{html.escape(str(student.get("roll_no") or ""))}">'
            for student in index.get("students", [])
        )
        body = f"""
        <h1>Review Cases</h1>
        <div class="actions band"><a class="button secondary" href="/runs/{html.escape(run_id)}">Back to Exam</a></div>
        <datalist id="student-rolls">{roll_options}</datalist>
        <div class="section-title"><h2>Students Needing Review</h2></div>
        <table><thead><tr><th>Roll No</th><th>Marks</th><th>Status</th><th>Missing Pages</th><th>Flags</th><th>Parser Notes</th><th>Latest Decision</th></tr></thead>
        <tbody>{''.join(rows) or '<tr><td colspan="7" class="empty">No student review cases</td></tr>'}</tbody></table>
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
        self._send_html("Review Cases", body)

    def _page_email(self, run_id: str) -> None:
        state = self.store.read_state(run_id)
        parse_dir = Path(str(state.get("parse_dir") or ""))
        release_dir = parse_dir / "email_release"
        queue_path = release_dir / EMAIL_QUEUE_CSV
        skipped_path = release_dir / EMAIL_SKIPPED_CSV
        body = f"""
        <h1>Email Release</h1>
        <div class="actions band"><a class="button secondary" href="/runs/{html.escape(run_id)}">Back to Exam</a></div>
        <section class="band">
          <h2>Prepare Queue</h2>
          <p class="muted">This creates preview emails and queues only verified/eligible students. It does not send anything.</p>
          <form method="post" action="/runs/{html.escape(run_id)}/email/prepare">
            <div class="grid">
              <div>
                <label>Release Type</label>
                <select name="release_mode">
                  <option value="sheet_verification">Sheet Verification - no marks</option>
                  <option value="evaluated_marks">Evaluated Sheet + Marks</option>
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
            <p class="muted">Available placeholders: {{exam_id}}, {{roll_no}}, {{name}}, {{display_name}}, {{marks_obtained}}, {{max_marks}}</p>
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
            rows = "".join(
                "<tr>"
                f"<td>{html.escape(row.get('roll_no',''))}</td>"
                f"<td>{html.escape(row.get('student_name',''))}</td>"
                f"<td>{html.escape(row.get('student_email',''))}</td>"
                f"<td>{html.escape('Sheet verification' if row.get('release_mode') == 'sheet_verification' else 'Evaluated marks')}</td>"
                f"<td>{html.escape('Not included' if row.get('release_mode') == 'sheet_verification' else str(row.get('marks_obtained', '')) + ' / ' + str(row.get('max_marks', '')))}</td>"
                f"<td><a href=\"{_asset_url(row.get('preview_eml_path'), parse_dir)}\">preview</a></td>"
                "</tr>"
                for row in queued[:50]
            )
            body += f"""
            <div class="grid">
              <div class="metric"><span>Queued</span><strong>{len(queued)}</strong></div>
              <div class="metric"><span>Skipped</span><strong>{len(skipped)}</strong></div>
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
              <thead><tr><th>Roll No</th><th>Name</th><th>Email</th><th>Release Type</th><th>Marks</th><th>Preview</th></tr></thead>
              <tbody>{rows or '<tr><td colspan="6" class="empty">No queued emails</td></tr>'}</tbody>
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
        allowed_roots = [Path.cwd().resolve(), (Path.cwd() / "data").resolve()]
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
