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
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
from email.parser import BytesHeaderParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO
from xml.etree import ElementTree

from omr.contracts import load_manifest
from omr.io.csv import load_students
from omr.reader.handwriting import build_roll_ocr_backend
from omr.workflows.email import (
    EMAIL_LOG_CSV,
    EMAIL_QUEUE_CSV,
    EMAIL_SKIPPED_CSV,
    prepare_email_release,
    send_email_release,
)
from omr.workflows.batch import parse_exam_bundle
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


def _asset_url(path: str | Path | None, base: Path | None = None) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.is_absolute() and base is not None:
        p = base / p
    return "/artifact?path=" + urllib.parse.quote(str(p.resolve()), safe="")


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
      font-family: Arial, Helvetica, sans-serif;
      color: #172033;
      background: #f6f7f9;
    }}
    body {{ margin: 0; }}
    header {{
      background: #ffffff;
      border-bottom: 1px solid #d9dee8;
      padding: 12px 20px;
      display: flex;
      gap: 18px;
      align-items: center;
    }}
    header a {{ color: #1f5fbf; text-decoration: none; font-weight: 700; }}
    main {{ max-width: 1320px; margin: 0 auto; padding: 20px; }}
    h1 {{ font-size: 22px; margin: 0 0 14px; }}
    h2 {{ font-size: 16px; margin: 22px 0 10px; }}
    p {{ line-height: 1.45; }}
    .band {{
      background: #fff;
      border: 1px solid #d9dee8;
      padding: 14px;
      margin-bottom: 14px;
    }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }}
    .metric {{ background: #fff; border: 1px solid #d9dee8; padding: 10px; }}
    .metric span {{ display: block; font-size: 12px; color: #667085; margin-bottom: 4px; }}
    .metric strong {{ display: block; font-size: 18px; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #d9dee8; }}
    th, td {{ padding: 8px 9px; border-bottom: 1px solid #edf0f5; text-align: left; vertical-align: top; font-size: 13px; }}
    th {{ background: #f0f3f8; color: #344054; font-size: 12px; white-space: nowrap; }}
    tr:hover td {{ background: #fafcff; }}
    label {{ display: block; font-size: 12px; font-weight: 700; margin: 10px 0 4px; }}
    input, select, textarea {{
      width: 100%;
      box-sizing: border-box;
      border: 1px solid #cbd2df;
      background: #fff;
      padding: 8px;
      font: inherit;
    }}
    button, .button {{
      display: inline-block;
      border: 1px solid #1f5fbf;
      background: #1f5fbf;
      color: #fff;
      padding: 8px 12px;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
      border-radius: 4px;
    }}
    .button.secondary, button.secondary {{ background: #fff; color: #1f5fbf; }}
    .actions {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
    .badge {{ display: inline-block; border-radius: 999px; padding: 2px 8px; font-size: 12px; font-weight: 700; }}
    .ready, .auto, .verified {{ background: #e8f5ee; color: #166534; }}
    .review, .pending, .missing {{ background: #fff7df; color: #8a4b08; }}
    .error, .failed, .rejected {{ background: #feeceb; color: #b42318; }}
    .neutral {{ background: #eef2f7; color: #344054; }}
    .split {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(360px, 0.8fr); gap: 16px; align-items: start; }}
    .pages {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 12px; }}
    figure {{ margin: 0; border: 1px solid #d9dee8; background: #fff; padding: 8px; }}
    figcaption {{ font-size: 12px; color: #667085; margin-bottom: 6px; }}
    img.sheet {{ width: 100%; height: auto; display: block; background: #fff; }}
    .flags {{ max-width: 520px; overflow-wrap: anywhere; }}
    .muted {{ color: #667085; }}
    .mono {{ font-family: Consolas, monospace; }}
    .empty {{ color: #667085; text-align: center; padding: 20px; }}
    @media (max-width: 860px) {{ .split {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <header>
    <strong>{APP_TITLE}</strong>
    <a href="/">Exams</a>
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
            f"{detail} Install Tesseract or set SMARTOMR_TESSERACT_CMD, then start the run again."
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
        details_path = parse_dir / student["details_path"]
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
                "review_flag_count",
                "review_flags",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    return marks_path


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

    def create_run(self, exam_id: str, files: dict[str, UploadedFile]) -> dict[str, Any]:
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
            initialize_verification_index(parse_dir, force=True)
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
            elif path.startswith("/runs/"):
                parts = [part for part in path.split("/") if part]
                if len(parts) == 5 and parts[2] == "students" and parts[4] in {"verify", "hold", "reject"}:
                    self._student_decision(parts[1], urllib.parse.unquote(parts[3]), parts[4])
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
            state = self.store.create_run(exam_id, files)
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
        only_roll = (form.get("only_roll") or "").strip() or None
        password = form.get("password") or ""
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
        )
        sent = sum(1 for row in rows if row["status"] == "SENT")
        dry = sum(1 for row in rows if row["status"] == "DRY_RUN")
        failed = sum(1 for row in rows if row["status"] == "FAILED")
        skipped = sum(1 for row in rows if row["status"] == "SKIPPED_ALREADY_SENT")
        self.store.write_state(
            run_id,
            last_email_send={
                "created_at": _now(),
                "mode": mode,
                "sent": sent,
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
        <div class="actions"><a class="button" href="/new">New Evaluation Run</a></div>
        <h2>Recent Runs</h2>
        {table}
        """
        self._send_html("Exam Runs", body)

    def _page_new(self) -> None:
        body = """
        <h1>New Evaluation Run</h1>
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
              <input type="file" name="manifest" required accept=".json">
            </div>
            <div>
              <label>Answer key Excel / CSV</label>
              <input type="file" name="answer_key" accept=".csv,.xlsx">
            </div>
            <div>
              <label>Master student list Excel / CSV</label>
              <input type="file" name="master_list" accept=".csv,.xlsx">
            </div>
          </div>
          <p class="muted">Answer key columns: q_no, answer, marks. Master list columns: roll_no, name, email, program. First-page written roll OCR cross-check runs automatically when local Tesseract is available.</p>
          <button type="submit">Start Evaluation</button>
        </form>
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
            details = _read_json(parse_dir / student["details_path"])
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
        <h2>Students</h2>
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
        details = _read_json(parse_dir / student_index["details_path"])
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
        details_dir = Path(details["details_path"]).parent
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
        verify_action = (
            f'<form method="post" action="/runs/{html.escape(run_id)}/students/{urllib.parse.quote(roll_no)}/verify">'
            '<button type="submit">Mark Manually Checked</button></form>'
            if not missing_pages
            else '<span class="muted">Assign every missing page before verification.</span>'
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
        <div class="actions band">
          {decision_actions}
          <a class="button secondary" href="/runs/{html.escape(run_id)}">Back to Exam</a>
          <a class="button secondary" href="{_asset_url(details.get('sheet_pdf_path'), details_dir)}">Open Student PDF</a>
        </div>
        <div class="split">
          <section>
            <h2>Full Sheet Images</h2>
            <div class="pages">{''.join(pages) or '<p class="empty">No page images found</p>'}</div>
          </section>
          <aside>
            <h2>Review Flags</h2>
            <div class="band">{'<br>'.join(html.escape(str(flag)) for flag in flags) if flags else '<span class="muted">No review flags.</span>'}</div>
            <h2>Source Pages</h2>
            <table><thead><tr><th>Source</th><th>Page</th><th>Used</th></tr></thead><tbody>
            {''.join(f"<tr><td>{html.escape(str(src.get('source_index')))}</td><td>{html.escape(str(src.get('page_index')))}</td><td>{html.escape(str(src.get('used')))}</td></tr>" for src in details.get('source_pages', []))}
            </tbody></table>
          </aside>
        </div>
        <h2>Answers And Marks</h2>
        <table>
          <thead><tr><th>Question</th><th>Type</th><th>Student Answer</th><th>Correct Answer</th><th>Marks</th><th>Status</th><th>Confidence</th><th>Notes</th></tr></thead>
          <tbody>{''.join(answer_rows) or '<tr><td colspan="8" class="empty">No objective answers found</td></tr>'}</tbody>
        </table>
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
        <h2>Students Needing Review</h2>
        <table><thead><tr><th>Roll No</th><th>Marks</th><th>Status</th><th>Missing Pages</th><th>Flags</th><th>Parser Notes</th><th>Latest Decision</th></tr></thead>
        <tbody>{''.join(rows) or '<tr><td colspan="7" class="empty">No student review cases</td></tr>'}</tbody></table>
        <h2>Unmatched Pages</h2>
        <table><thead><tr><th>Source Index</th><th>Detected Page</th><th>Status</th><th>Flags</th><th>Details</th><th>Resolution</th></tr></thead>
        <tbody>{''.join(unmatched_rows) or '<tr><td colspan="6" class="empty">No unmatched pages</td></tr>'}</tbody></table>
        <h2>Unreadable Pages</h2>
        <table><thead><tr><th>Source Index</th><th>Status</th><th>Error</th><th>Notes</th><th>Details</th></tr></thead>
        <tbody>{''.join(page_error_rows) or '<tr><td colspan="5" class="empty">No unreadable pages</td></tr>'}</tbody></table>
        <h2>Roster Students Without A Detected Sheet</h2>
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

# real send after dry-run and one-email test
$env:SMARTOMR_SMTP_PASSWORD = "GMAIL_APP_PASSWORD"
.\\.venv\\Scripts\\python.exe -m omr.workflows.email send `
  --queue-csv "{queue_path}" `
  --smtp-host smtp.gmail.com `
  --smtp-port 587 `
  --username "professor@gmail.com" `
  --sender "professor@gmail.com" `
  --sender-name "Course Staff" `
  --limit 1 `
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
              <p class="muted">Start with Dry Run, then send one test using Limit = 1. Real Send requires an SMTP/app password and will email students.</p>
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
                    <input name="limit" placeholder="1 for test send">
                  </div>
                  <div>
                    <label>Only Roll No</label>
                    <input name="only_roll" placeholder="optional">
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
            <h2>Queued Students</h2>
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
