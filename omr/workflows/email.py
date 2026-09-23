"""Prepare and send per-student SmartOMR result emails.

The mailer is intentionally two-step:

1. ``prepare`` builds a reviewable queue and preview files from verified results.
2. ``send`` sends that queue through SMTP and writes an immutable send log.

This keeps tomorrow's short professor login window focused on sending rather
than debugging parsing or attachment generation.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import html
import json
import math
import os
import re
import shutil
import smtplib
import ssl
import sys
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from omr.io.csv import ROLL_HEADERS, load_students, normalize_roll
from omr.io.tabular import read_tabular_rows


VERIFIED_INDEX_NAME = "verified_index.json"
EMAIL_RELEASE_DIR = "email_release"
EMAIL_QUEUE_CSV = "email_queue.csv"
EMAIL_SKIPPED_CSV = "email_skipped.csv"
EMAIL_LOG_CSV = "email_send_log.csv"
EMAIL_SCHEMA_VERSION = 3
EVALUATED_SUBJECT = "{exam_id}: evaluated OMR response sheet"
VERIFICATION_SUBJECT = "{exam_id}: response sheet verification"
TENTATIVE_SUBJECT = "{exam_id}: tentative marks and evaluated response sheet"
EMAIL_PATTERN = re.compile(r"^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$")
MARKS_HEADERS = ("marks_obtained", "marks", "score", "tentative_marks", "obtained_marks")
TOTAL_HEADERS = ("max_marks", "total_marks", "maximum_marks", "out_of", "total")

QUEUE_COLUMNS = [
    "roll_no",
    "student_name",
    "student_email",
    "status",
    "eligible_for_email",
    "marks_obtained",
    "max_marks",
    "verified_sheet_pdf_path",
    "summary_txt_path",
    "body_txt_path",
    "preview_eml_path",
    "subject",
    "release_mode",
    "attachment_sha256",
    "attachment_size_bytes",
    "attachment_page_count",
    "body_sha256",
    "summary_sha256",
    "release_id",
    "message_id",
]

SKIPPED_COLUMNS = [
    "roll_no",
    "student_name",
    "student_email",
    "status",
    "eligible_for_email",
    "reason",
]

LOG_COLUMNS = [
    "created_at",
    "roll_no",
    "student_email",
    "status",
    "message",
    "subject",
    "verified_sheet_pdf_path",
    "actual_recipient",
    "attachment_sha256",
    "release_id",
    "message_id",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_email(value: str) -> bool:
    return bool(EMAIL_PATTERN.fullmatch(str(value or "").strip()))


def _safe_roll_filename(roll_no: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in roll_no)
    if not safe or safe in {".", ".."}:
        raise ValueError(f"roll number cannot be used as a filename: {roll_no!r}")
    return safe


def _pdf_roll(path: Path) -> str:
    raw = path.parent.name if path.stem.lower() == "sheet" else path.stem
    return normalize_roll(raw)


def _pdf_page_count(path: Path) -> int:
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted and reader.decrypt("") == 0:
            raise ValueError("PDF is password protected")
        count = len(reader.pages)
        if count < 1:
            raise ValueError("PDF contains no pages")
        for page in reader.pages:
            _ = page.mediabox
        return count
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"invalid or unreadable PDF: {type(exc).__name__}: {exc}") from exc


def _release_id(exam_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    entropy = os.urandom(5).hex()
    safe_exam = re.sub(r"[^A-Za-z0-9._-]+", "_", exam_id).strip("._-") or "exam"
    return f"{safe_exam}_{stamp}_{entropy}"


def _parsed_root(parsed_dir: str | Path) -> Path:
    path = Path(parsed_dir)
    if path.name == VERIFIED_INDEX_NAME:
        return path.parent.resolve()
    if path.name == EMAIL_RELEASE_DIR:
        return path.parent.resolve()
    return path.resolve()


def _verified_index_path(parsed_root: Path) -> Path:
    return parsed_root / VERIFIED_INDEX_NAME


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


def _relative(path: Path, parsed_root: Path) -> str:
    try:
        return path.relative_to(parsed_root).as_posix()
    except ValueError:
        return str(path)


def _format_number(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if numeric.is_integer():
        return str(int(numeric))
    return f"{numeric:g}"


def _header_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def _table_field(row: dict[str, str], names: tuple[str, ...], *, required: bool = True) -> str:
    values = {_header_key(name): str(value or "").strip() for name, value in row.items()}
    for name in names:
        value = values.get(_header_key(name), "")
        if value:
            return value
    if required:
        raise ValueError(f"missing one of these columns: {', '.join(names)}")
    return ""


def _positive_mark(value: Any, *, label: str, allow_zero: bool) -> float:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is not a number: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    if number < 0 or (not allow_zero and number == 0):
        comparison = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{label} must be {comparison}")
    return number


def _load_marks(
    marks_path: Path,
    roster_rolls: set[str],
    *,
    default_max_marks: float | None,
) -> dict[str, tuple[str, str]]:
    rows = read_tabular_rows(marks_path)
    marks: dict[str, tuple[str, str]] = {}
    errors: list[str] = []
    fallback_total = None
    if default_max_marks is not None:
        fallback_total = _positive_mark(default_max_marks, label="--max-marks", allow_zero=False)

    for row_number, row in enumerate(rows, start=2):
        try:
            roll_no = normalize_roll(_table_field(row, ROLL_HEADERS))
            if not roll_no:
                raise ValueError("roll number is empty")
            if roll_no not in roster_rolls:
                raise ValueError(f"roll {roll_no} is not present in the roster")
            if roll_no in marks:
                raise ValueError(f"roll {roll_no} appears more than once")
            obtained = _positive_mark(
                _table_field(row, MARKS_HEADERS),
                label=f"marks for {roll_no}",
                allow_zero=True,
            )
            total_text = _table_field(row, TOTAL_HEADERS, required=False)
            if total_text:
                total = _positive_mark(total_text, label=f"maximum marks for {roll_no}", allow_zero=False)
            elif fallback_total is not None:
                total = fallback_total
            else:
                raise ValueError(
                    f"maximum marks for {roll_no} is missing; add a max_marks column or use --max-marks"
                )
            if obtained > total:
                raise ValueError(f"marks for {roll_no} ({obtained:g}) exceed maximum marks ({total:g})")
            marks[roll_no] = (_format_number(obtained), _format_number(total))
        except ValueError as exc:
            errors.append(f"row {row_number}: {exc}")

    if errors:
        raise ValueError("marks spreadsheet validation failed:\n- " + "\n- ".join(errors))
    return marks


def _read_body_template(body_template: str | None, body_template_file: str | Path | None) -> str | None:
    if body_template and body_template_file:
        raise ValueError("use either body_template or body_template_file, not both")
    if body_template_file is None:
        return body_template
    path = Path(body_template_file)
    if not path.is_file():
        raise FileNotFoundError(f"body template file not found: {path}")
    template = path.read_text(encoding="utf-8-sig")
    if not template.strip():
        raise ValueError(f"body template file is empty: {path}")
    return template


def _load_student_details(student: dict[str, Any], parsed_root: Path) -> tuple[dict[str, Any], Path | None]:
    details_path = _resolve_path(student.get("details_path"), parsed_root)
    if details_path is None or not details_path.exists():
        return {}, None
    return _load_json(details_path), details_path.parent


def _score_from_details(details: dict[str, Any], student: dict[str, Any]) -> tuple[float | None, float | None]:
    if details.get("manual_score_override") is not None:
        total = details.get("manual_total_override")
        return float(details.get("manual_score_override") or 0.0), (
            float(total) if total is not None else None
        )
    if student.get("manual_score_override") is not None:
        total = student.get("manual_total_override")
        return float(student.get("manual_score_override") or 0.0), (
            float(total) if total is not None else None
        )
    score = 0.0
    total = 0.0
    saw_any = False
    for score_key, total_key in (("mcq_score", "mcq_total"), ("numerical_score", "numerical_total")):
        value = details.get(score_key, student.get(score_key))
        max_value = details.get(total_key, student.get(total_key))
        if value is not None:
            score += float(value)
            saw_any = True
        if max_value is not None:
            total += float(max_value)
            saw_any = True
    if not saw_any:
        return None, None
    return score, total


def _written_final_scores(parsed_root: Path) -> dict[str, dict[str, str]]:
    path = parsed_root / "written_grading" / "final_scores.csv"
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return {str(row.get("roll_no") or ""): row for row in csv.DictReader(handle)}


def _student_score(parsed_root: Path, student: dict[str, Any], details: dict[str, Any]) -> tuple[str, str]:
    final_scores = _written_final_scores(parsed_root)
    roll_no = str(student.get("roll_no") or details.get("student", {}).get("roll_no") or "")
    if roll_no in final_scores:
        row = final_scores[roll_no]
        if row.get("total_score") and row.get("total_marks"):
            return _format_number(row["total_score"]), _format_number(row["total_marks"])
    score, total = _score_from_details(details, student)
    return _format_number(score), _format_number(total)


def _answer_rows(details: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in details.get("mcq_responses", []):
        dropped = bool(item.get("dropped"))
        rows.append(
            {
                "question": f"Q{item.get('q_no')}",
                "type": "MCQ",
                "student_answer": str(item.get("selected_option") or ""),
                "correct_answer": "" if dropped else str(item.get("correct_option") or ""),
                "marks": "Dropped" if dropped else f"{_format_number(item.get('marks_awarded'))}/{_format_number(item.get('marks'))}",
                "status": "dropped" if dropped else str(item.get("outcome") or ""),
            }
        )
    for item in details.get("numerical_responses", []):
        value = item.get("value")
        correct = item.get("correct_value")
        dropped = bool(item.get("dropped"))
        rows.append(
            {
                "question": f"Q{item.get('q_no')}",
                "type": "NUMERIC",
                "student_answer": "" if value is None else str(value),
                "correct_answer": "" if dropped or correct is None else str(correct),
                "marks": "Dropped" if dropped else f"{_format_number(item.get('marks_awarded'))}/{_format_number(item.get('max_marks'))}",
                "status": "dropped" if dropped else str(item.get("outcome") or ""),
            }
        )
    return rows


def _summary_text(
    *,
    exam_id: str,
    roll_no: str,
    name: str,
    marks_obtained: str,
    max_marks: str,
    answers: list[dict[str, str]],
) -> str:
    lines = [
        f"Exam: {exam_id}",
        f"Roll no: {roll_no}",
        f"Name: {name}",
        f"Marks: {marks_obtained} / {max_marks}",
        "",
        "Question-wise summary:",
    ]
    if answers:
        for answer in answers:
            lines.append(
                "{question} [{type}] student={student_answer} correct={correct_answer} marks={marks} status={status}".format(
                    **answer
                )
            )
    else:
        lines.append("No objective answer summary is available in the parser output.")
    return "\n".join(lines) + "\n"


def _body_text(
    *,
    exam_id: str,
    roll_no: str,
    name: str,
    marks_obtained: str,
    max_marks: str,
    body_template: str | None = None,
    sheet_only: bool = False,
) -> str:
    display_name = name or roll_no
    values = {
        "exam_id": exam_id,
        "roll_no": roll_no,
        "name": name,
        "display_name": display_name,
        "marks_obtained": marks_obtained,
        "max_marks": max_marks,
    }
    if body_template and body_template.strip():
        return body_template.format(**values).replace("\r\n", "\n").rstrip() + "\n"
    if sheet_only:
        return (
            f"Dear {display_name},\n\n"
            f"Your scanned OMR response sheet for {exam_id} is attached for verification.\n\n"
            f"Roll no: {roll_no}\n\n"
            "Please contact the course staff promptly if this is not your sheet or any page is missing.\n\n"
            "Regards,\n"
            "Course Team\n"
        )
    return (
        f"Dear {display_name},\n\n"
        f"Your evaluated OMR response sheet for {exam_id} is attached.\n\n"
        f"Roll no: {roll_no}\n"
        f"Marks: {marks_obtained} / {max_marks}\n\n"
        "Please contact the course staff if you believe there is a review issue.\n\n"
        "Regards,\n"
        "Course Team\n"
    )


def _subject_text(template: str, *, exam_id: str, roll_no: str, name: str) -> str:
    try:
        subject = template.format(
            exam_id=exam_id,
            roll_no=roll_no,
            name=name,
            display_name=name or roll_no,
        ).strip()
    except KeyError as exc:
        raise ValueError(f"unknown subject placeholder: {exc.args[0]}") from exc
    if not subject:
        raise ValueError("email subject cannot be empty")
    if "\r" in subject or "\n" in subject:
        raise ValueError("email subject must be a single line")
    return subject


def _build_email_message(
    *,
    sender: str,
    sender_name: str | None,
    recipient: str,
    subject: str,
    body: str,
    attachments: list[Path],
    message_id: str | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = f"{sender_name} <{sender}>" if sender_name else sender
    message["To"] = recipient
    message["Subject"] = subject
    if message_id:
        message["Message-ID"] = message_id
    message.set_content(body)
    for attachment in attachments:
        data = attachment.read_bytes()
        maintype = "application"
        subtype = "octet-stream"
        if attachment.suffix.lower() == ".pdf":
            subtype = "pdf"
        elif attachment.suffix.lower() == ".txt":
            maintype = "text"
            subtype = "plain"
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=attachment.name)
    return message


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def prepare_email_release(
    parsed_dir: str | Path,
    *,
    sender: str = "professor@example.edu",
    sender_name: str | None = None,
    subject_template: str = EVALUATED_SUBJECT,
    body_template: str | None = None,
    include_unverified: bool = False,
    include_needs_review: bool = False,
    sheet_only: bool = False,
) -> tuple[dict[str, Any], Path]:
    parsed_root = _parsed_root(parsed_dir)
    verified_path = _verified_index_path(parsed_root)
    if not verified_path.exists():
        raise FileNotFoundError(f"{VERIFIED_INDEX_NAME} not found: {verified_path}")
    verified = _load_json(verified_path)
    if sheet_only and subject_template == EVALUATED_SUBJECT:
        subject_template = VERIFICATION_SUBJECT
    exam_id = str(verified.get("exam_id") or parsed_root.name)
    release_id = _release_id(exam_id)
    release_dir = parsed_root / EMAIL_RELEASE_DIR
    summaries_dir = release_dir / "summaries"
    bodies_dir = release_dir / "bodies"
    previews_dir = release_dir / "previews"
    for directory in (summaries_dir, bodies_dir, previews_dir):
        directory.mkdir(parents=True, exist_ok=True)

    queue: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for student in verified.get("students", []):
        roll_no = str(student.get("roll_no") or "")
        details, _details_dir = _load_student_details(student, parsed_root)
        detail_student = details.get("student", {}) if details else {}
        name = str(student.get("student_name") or detail_student.get("name") or "")
        email = str(student.get("student_email") or detail_student.get("email") or "").strip()
        status = str(student.get("status") or "")
        eligible = bool(student.get("eligible_for_email"))
        skip_reason = ""
        if not email:
            skip_reason = "missing student email"
        elif not include_unverified and not eligible:
            skip_reason = "student is not eligible_for_email; verify before release"
        elif not include_needs_review and status in {"needs_review", "missing_pages", "rejected"}:
            skip_reason = f"student status is {status}"

        sheet_path = _resolve_path(student.get("verified_sheet_pdf_path"), parsed_root)
        if not skip_reason and (sheet_path is None or not sheet_path.exists()):
            skip_reason = "verified sheet PDF is missing"

        if skip_reason:
            skipped.append(
                {
                    "roll_no": roll_no,
                    "student_name": name,
                    "student_email": email,
                    "status": status,
                    "eligible_for_email": str(eligible).lower(),
                    "reason": skip_reason,
                }
            )
            continue

        marks_obtained, max_marks = ("", "") if sheet_only else _student_score(parsed_root, student, details)
        answers = [] if sheet_only else _answer_rows(details)
        subject = subject_template.format(exam_id=exam_id, roll_no=roll_no, name=name)
        body = _body_text(
            exam_id=exam_id,
            roll_no=roll_no,
            name=name,
            marks_obtained=marks_obtained,
            max_marks=max_marks,
            body_template=body_template,
            sheet_only=sheet_only,
        )
        safe_roll = "".join(char if char.isalnum() or char in "._-" else "_" for char in roll_no) or "student"
        summary_path = summaries_dir / f"{safe_roll}_marks_summary.txt"
        body_path = bodies_dir / f"{safe_roll}_email_body.txt"
        preview_path = previews_dir / f"{safe_roll}.eml"
        attachments = [sheet_path]
        if not sheet_only:
            summary = _summary_text(
                exam_id=exam_id,
                roll_no=roll_no,
                name=name,
                marks_obtained=marks_obtained,
                max_marks=max_marks,
                answers=answers,
            )
            summary_path.write_text(summary, encoding="utf-8")
            attachments.append(summary_path)
        body_path.write_text(body, encoding="utf-8")
        message_id = make_msgid(domain=sender.rpartition("@")[2] or None)

        message = _build_email_message(
            sender=sender,
            sender_name=sender_name,
            recipient=email,
            subject=subject,
            body=body,
            attachments=attachments,
            message_id=message_id,
        )
        preview_path.write_bytes(bytes(message))
        queue.append(
            {
                "roll_no": roll_no,
                "student_name": name,
                "student_email": email,
                "status": status,
                "eligible_for_email": str(eligible).lower(),
                "marks_obtained": marks_obtained,
                "max_marks": max_marks,
                "verified_sheet_pdf_path": _relative(sheet_path, parsed_root),
                "summary_txt_path": "" if sheet_only else _relative(summary_path, parsed_root),
                "body_txt_path": _relative(body_path, parsed_root),
                "preview_eml_path": _relative(preview_path, parsed_root),
                "subject": subject,
                "release_mode": "sheet_verification" if sheet_only else "evaluated_marks",
                "attachment_sha256": _sha256_file(sheet_path),
                "attachment_size_bytes": str(sheet_path.stat().st_size),
                "attachment_page_count": "",
                "body_sha256": _sha256_file(body_path),
                "summary_sha256": "" if sheet_only else _sha256_file(summary_path),
                "release_id": release_id,
                "message_id": message_id,
            }
        )

    known_rolls = {str(student.get("roll_no") or "") for student in verified.get("students", [])}
    reconciliation = verified.get("roster_reconciliation") or {}
    for missing in reconciliation.get("missing_students", []):
        roll_no = str(missing.get("roll_no") or "")
        if roll_no in known_rolls:
            continue
        skipped.append(
            {
                "roll_no": roll_no,
                "student_name": str(missing.get("student_name") or ""),
                "student_email": str(missing.get("student_email") or ""),
                "status": "missing_sheet",
                "eligible_for_email": "false",
                "reason": "no parsed student sheet was detected for this roster entry",
            }
        )

    queue_path = release_dir / EMAIL_QUEUE_CSV
    _write_csv(queue_path, queue, QUEUE_COLUMNS)
    _write_csv(release_dir / EMAIL_SKIPPED_CSV, skipped, SKIPPED_COLUMNS)
    metadata = {
        "schema_version": EMAIL_SCHEMA_VERSION,
        "exam_id": exam_id,
        "created_at": _now(),
        "sender": sender,
        "sender_name": sender_name,
        "subject_template": subject_template,
        "body_template": body_template,
        "include_unverified": include_unverified,
        "include_needs_review": include_needs_review,
        "sheet_only": sheet_only,
        "queued": len(queue),
        "skipped": len(skipped),
        "release_id": release_id,
        "queue_sha256": _sha256_file(queue_path),
        "enforce_sender": False,
        "queue_csv": f"{EMAIL_RELEASE_DIR}/{EMAIL_QUEUE_CSV}",
        "skipped_csv": f"{EMAIL_RELEASE_DIR}/{EMAIL_SKIPPED_CSV}",
    }
    _write_json(release_dir / "email_release.json", metadata)
    return metadata, queue_path


def prepare_folder_email_release(
    pdf_dir: str | Path,
    roster_csv: str | Path,
    output_dir: str | Path,
    *,
    exam_id: str,
    sender: str,
    sender_name: str | None = None,
    subject_template: str | None = None,
    body_template: str | None = None,
    marks_file: str | Path | None = None,
    default_max_marks: float | None = None,
    expected_pages: int | None = 2,
    max_attachment_mb: float = 20.0,
) -> tuple[dict[str, Any], Path]:
    """Build a frozen, auditable email release from final per-student PDFs.

    Accepted layouts are ``<pdf_dir>/<roll>.pdf`` and
    ``<pdf_dir>/<roll>/sheet.pdf``. Matching is deliberately strict: a PDF
    must resolve to exactly one normalized roster roll number.
    """
    pdf_root = Path(pdf_dir).resolve()
    roster_path = Path(roster_csv).resolve()
    release_dir = Path(output_dir).resolve()
    marks_path = Path(marks_file).resolve() if marks_file is not None else None
    exam_id = str(exam_id or "").strip()
    sender = str(sender or "").strip()
    if not pdf_root.is_dir():
        raise NotADirectoryError(f"PDF folder not found: {pdf_root}")
    if not roster_path.is_file():
        raise FileNotFoundError(f"roster CSV not found: {roster_path}")
    if release_dir == pdf_root or pdf_root in release_dir.parents:
        raise ValueError("output_dir must be outside pdf_dir so frozen attachments are never re-imported")
    if not exam_id:
        raise ValueError("exam_id is required")
    if not _valid_email(sender):
        raise ValueError(f"invalid sender email address: {sender!r}")
    if expected_pages is not None and expected_pages < 1:
        raise ValueError("expected_pages must be positive or omitted")
    if max_attachment_mb <= 0:
        raise ValueError("max_attachment_mb must be positive")
    if marks_path is not None and not marks_path.is_file():
        raise FileNotFoundError(f"marks spreadsheet not found: {marks_path}")

    release_mode = "tentative_marks" if marks_path is not None else "sheet_verification"
    if subject_template is None:
        subject_template = TENTATIVE_SUBJECT if marks_path is not None else VERIFICATION_SUBJECT

    students = load_students(roster_path)
    email_owners: dict[str, str] = {}
    for roll_no, student in students.items():
        email = student.email.strip().lower()
        if not _valid_email(email):
            raise ValueError(f"roster roll {roll_no} has invalid email address {student.email!r}")
        owner = email_owners.get(email)
        if owner is not None:
            raise ValueError(f"roster email {email} is shared by rolls {owner} and {roll_no}")
        email_owners[email] = roll_no

    pdf_paths = sorted(
        (path for path in pdf_root.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf"),
        key=lambda path: str(path).lower(),
    )
    if not pdf_paths:
        raise ValueError(f"no PDF files found under {pdf_root}")

    by_roll: dict[str, Path] = {}
    errors: list[str] = []
    pdf_info: dict[str, tuple[int, int]] = {}
    max_bytes = int(max_attachment_mb * 1024 * 1024)
    for path in pdf_paths:
        roll_no = _pdf_roll(path)
        if not roll_no:
            errors.append(f"cannot derive a roll number from {path}")
            continue
        if roll_no not in students:
            errors.append(f"PDF does not match any roster roll: {path.name} -> {roll_no}")
            continue
        if roll_no in by_roll:
            errors.append(f"multiple PDFs resolve to roster roll {roll_no}: {by_roll[roll_no]} and {path}")
            continue
        try:
            page_count = _pdf_page_count(path)
        except ValueError as exc:
            errors.append(f"{roll_no}: {exc}")
            continue
        size = path.stat().st_size
        if expected_pages is not None and page_count != expected_pages:
            errors.append(f"{roll_no}: expected {expected_pages} PDF pages, found {page_count}")
            continue
        if size > max_bytes:
            errors.append(
                f"{roll_no}: attachment is {size / (1024 * 1024):.1f} MiB; "
                f"limit is {max_attachment_mb:g} MiB"
            )
            continue
        by_roll[roll_no] = path
        pdf_info[roll_no] = (page_count, size)

    if errors:
        raise ValueError("email release preflight failed:\n- " + "\n- ".join(errors))
    if not by_roll:
        raise ValueError("no roster-matched PDFs are available for email")

    marks_by_roll: dict[str, tuple[str, str]] = {}
    if marks_path is not None:
        marks_by_roll = _load_marks(
            marks_path,
            set(students),
            default_max_marks=default_max_marks,
        )
        missing_marks = sorted(set(by_roll) - set(marks_by_roll))
        if missing_marks:
            raise ValueError(
                "marks spreadsheet is missing PDF recipient(s): " + ", ".join(missing_marks)
            )

    for roll_no in by_roll:
        student = students[roll_no]
        marks_obtained, max_marks = marks_by_roll.get(roll_no, ("", ""))
        _subject_text(subject_template, exam_id=exam_id, roll_no=roll_no, name=student.name)
        try:
            _body_text(
                exam_id=exam_id,
                roll_no=roll_no,
                name=student.name,
                marks_obtained=marks_obtained,
                max_marks=max_marks,
                body_template=body_template,
                sheet_only=marks_path is None,
            )
        except KeyError as exc:
            raise ValueError(f"unknown body-template placeholder: {exc.args[0]}") from exc

    if release_dir.exists() and any(release_dir.iterdir()):
        raise FileExistsError(f"email release directory is not empty: {release_dir}")

    attachments_dir = release_dir / "attachments"
    bodies_dir = release_dir / "bodies"
    previews_dir = release_dir / "previews"
    inputs_dir = release_dir / "inputs"
    for directory in (attachments_dir, bodies_dir, previews_dir, inputs_dir):
        directory.mkdir(parents=True, exist_ok=True)

    frozen_roster = inputs_dir / f"roster{roster_path.suffix.lower()}"
    shutil.copy2(roster_path, frozen_roster)
    frozen_marks = None
    if marks_path is not None:
        frozen_marks = inputs_dir / f"marks{marks_path.suffix.lower()}"
        shutil.copy2(marks_path, frozen_marks)

    release_id = _release_id(exam_id)
    queue: list[dict[str, Any]] = []
    for roll_no in sorted(by_roll):
        student = students[roll_no]
        source_pdf = by_roll[roll_no]
        marks_obtained, max_marks = marks_by_roll.get(roll_no, ("", ""))
        safe_roll = _safe_roll_filename(roll_no)
        frozen_pdf = attachments_dir / f"{safe_roll}.pdf"
        shutil.copy2(source_pdf, frozen_pdf)
        if _sha256_file(source_pdf) != _sha256_file(frozen_pdf):
            raise OSError(f"attachment copy verification failed for {roll_no}")

        subject = _subject_text(subject_template, exam_id=exam_id, roll_no=roll_no, name=student.name)
        body = _body_text(
            exam_id=exam_id,
            roll_no=roll_no,
            name=student.name,
            marks_obtained=marks_obtained,
            max_marks=max_marks,
            body_template=body_template,
            sheet_only=marks_path is None,
        )
        body_path = bodies_dir / f"{safe_roll}_email_body.txt"
        preview_path = previews_dir / f"{safe_roll}.eml"
        body_path.write_text(body, encoding="utf-8")
        message_id = make_msgid(domain=sender.rpartition("@")[2] or None)
        message = _build_email_message(
            sender=sender,
            sender_name=sender_name,
            recipient=student.email,
            subject=subject,
            body=body,
            attachments=[frozen_pdf],
            message_id=message_id,
        )
        preview_path.write_bytes(bytes(message))
        page_count, size = pdf_info[roll_no]
        queue.append(
            {
                "roll_no": roll_no,
                "student_name": student.name,
                "student_email": student.email.strip().lower(),
                "status": "folder_verified",
                "eligible_for_email": "true",
                "marks_obtained": marks_obtained,
                "max_marks": max_marks,
                "verified_sheet_pdf_path": frozen_pdf.relative_to(release_dir).as_posix(),
                "summary_txt_path": "",
                "body_txt_path": body_path.relative_to(release_dir).as_posix(),
                "preview_eml_path": preview_path.relative_to(release_dir).as_posix(),
                "subject": subject,
                "release_mode": release_mode,
                "attachment_sha256": _sha256_file(frozen_pdf),
                "attachment_size_bytes": str(size),
                "attachment_page_count": str(page_count),
                "body_sha256": _sha256_file(body_path),
                "summary_sha256": "",
                "release_id": release_id,
                "message_id": message_id,
            }
        )

    skipped = [
        {
            "roll_no": roll_no,
            "student_name": student.name,
            "student_email": student.email.strip().lower(),
            "status": "missing_pdf",
            "eligible_for_email": "false",
            "reason": "no PDF for this roster entry was supplied",
        }
        for roll_no, student in students.items()
        if roll_no not in by_roll
    ]
    queue_path = release_dir / EMAIL_QUEUE_CSV
    _write_csv(queue_path, queue, QUEUE_COLUMNS)
    _write_csv(release_dir / EMAIL_SKIPPED_CSV, skipped, SKIPPED_COLUMNS)
    metadata = {
        "schema_version": EMAIL_SCHEMA_VERSION,
        "release_id": release_id,
        "exam_id": exam_id,
        "created_at": _now(),
        "source_pdf_dir": str(pdf_root),
        "source_roster_csv": str(roster_path),
        "source_marks_file": str(marks_path) if marks_path is not None else None,
        "frozen_roster_sha256": _sha256_file(frozen_roster),
        "frozen_marks_sha256": _sha256_file(frozen_marks) if frozen_marks is not None else None,
        "sender": sender,
        "sender_name": sender_name,
        "subject_template": subject_template,
        "body_template": body_template,
        "release_mode": release_mode,
        "expected_pages": expected_pages,
        "max_attachment_mb": max_attachment_mb,
        "roster_students": len(students),
        "queued": len(queue),
        "skipped": len(skipped),
        "queue_sha256": _sha256_file(queue_path),
        "enforce_sender": True,
        "queue_csv": EMAIL_QUEUE_CSV,
        "skipped_csv": EMAIL_SKIPPED_CSV,
    }
    _write_json(release_dir / "email_release.json", metadata)
    return metadata, queue_path


def _read_queue(queue_csv: Path) -> list[dict[str, str]]:
    with queue_csv.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _append_log(path: Path, rows: list[dict[str, str]]) -> None:
    exists = path.exists()
    if exists:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            existing_fields = list(reader.fieldnames or [])
            existing_rows = list(reader)
        if existing_fields != LOG_COLUMNS:
            _write_csv(path, existing_rows + rows, LOG_COLUMNS)
            return
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_COLUMNS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def _sent_recipients(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return {
            (str(row.get("roll_no") or ""), str(row.get("student_email") or "").lower())
            for row in csv.DictReader(handle)
            if row.get("status") == "SENT"
        }


def _release_metadata(release_dir: Path) -> dict[str, Any]:
    path = release_dir / "email_release.json"
    return _load_json(path) if path.exists() else {}


def _validate_queue_rows(rows: list[dict[str, str]], metadata: dict[str, Any]) -> None:
    seen_rolls: set[str] = set()
    seen_emails: set[str] = set()
    expected_release_id = str(metadata.get("release_id") or "")
    for line_no, row in enumerate(rows, start=2):
        roll_no = str(row.get("roll_no") or "").strip()
        email = str(row.get("student_email") or "").strip().lower()
        if not roll_no:
            raise ValueError(f"email queue line {line_no} has no roll number")
        if roll_no in seen_rolls:
            raise ValueError(f"email queue duplicates roll number {roll_no}")
        seen_rolls.add(roll_no)
        if not _valid_email(email):
            raise ValueError(f"email queue roll {roll_no} has invalid recipient {email!r}")
        if email in seen_emails:
            raise ValueError(f"email queue duplicates recipient address {email}")
        seen_emails.add(email)
        if str(row.get("eligible_for_email") or "").lower() not in {"true", "1", "yes"}:
            raise ValueError(f"email queue roll {roll_no} is not eligible_for_email")
        for field in ("verified_sheet_pdf_path", "body_txt_path", "subject"):
            if not str(row.get(field) or "").strip():
                raise ValueError(f"email queue roll {roll_no} is missing {field}")
        row_release_id = str(row.get("release_id") or "")
        if expected_release_id and row_release_id != expected_release_id:
            raise ValueError(f"email queue roll {roll_no} has the wrong release_id")


def _queue_file(
    row: dict[str, str], field: str, *, parsed_root: Path, release_dir: Path, frozen: bool,
) -> Path | None:
    path = _resolve_path(row.get(field), parsed_root, base_dir=release_dir)
    if path is None:
        return None
    resolved = path.resolve()
    if frozen and resolved != release_dir and release_dir not in resolved.parents:
        raise ValueError(f"queue field {field} escapes the frozen release directory: {resolved}")
    return resolved


def _validate_selected_files(
    rows: list[dict[str, str]], *, parsed_root: Path, release_dir: Path, frozen: bool,
) -> None:
    for row in rows:
        roll_no = row["roll_no"]
        sheet_path = _queue_file(
            row, "verified_sheet_pdf_path", parsed_root=parsed_root, release_dir=release_dir, frozen=frozen,
        )
        body_path = _queue_file(
            row, "body_txt_path", parsed_root=parsed_root, release_dir=release_dir, frozen=frozen,
        )
        summary_path = _queue_file(
            row, "summary_txt_path", parsed_root=parsed_root, release_dir=release_dir, frozen=frozen,
        )
        if sheet_path is None or not sheet_path.is_file():
            raise FileNotFoundError(f"{roll_no}: sheet attachment not found: {sheet_path}")
        if body_path is None or not body_path.is_file():
            raise FileNotFoundError(f"{roll_no}: email body not found: {body_path}")
        if row.get("summary_txt_path") and (summary_path is None or not summary_path.is_file()):
            raise FileNotFoundError(f"{roll_no}: summary attachment not found: {summary_path}")

        checks = (
            (sheet_path, "attachment_sha256"),
            (body_path, "body_sha256"),
            (summary_path, "summary_sha256"),
        )
        for path, hash_field in checks:
            expected_hash = str(row.get(hash_field) or "")
            if path is not None and expected_hash and _sha256_file(path) != expected_hash:
                raise ValueError(f"{roll_no}: {path.name} changed after the email queue was prepared")
        expected_size = str(row.get("attachment_size_bytes") or "")
        if expected_size and sheet_path.stat().st_size != int(expected_size):
            raise ValueError(f"{roll_no}: attachment size changed after preparation")
        expected_pages = str(row.get("attachment_page_count") or "")
        if expected_pages and _pdf_page_count(sheet_path) != int(expected_pages):
            raise ValueError(f"{roll_no}: attachment page count changed after preparation")


@contextmanager
def _real_send_lock(release_dir: Path):
    lock_path = release_dir / "email_send.lock"
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(
            f"another real send may be running, or a previous send stopped unexpectedly: {lock_path}"
        ) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "created_at": _now()}))
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _connect_smtp(
    *, host: str, port: int, security: str, username: str, password: str,
) -> smtplib.SMTP:
    context = ssl.create_default_context()
    if security == "ssl":
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=60, context=context)
    elif security == "starttls":
        smtp = smtplib.SMTP(host, port, timeout=60)
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.ehlo()
    else:
        raise ValueError("smtp_security must be 'starttls' or 'ssl'")
    smtp.login(username, password)
    return smtp


@contextmanager
def _smtp_session(
    *, enabled: bool, host: str, port: int, security: str, username: str, password: str,
):
    smtp = None
    try:
        if enabled:
            smtp = _connect_smtp(
                host=host,
                port=port,
                security=security,
                username=username,
                password=password,
            )
        yield smtp
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except smtplib.SMTPException:
                pass


def send_email_release(
    queue_csv: str | Path,
    *,
    smtp_host: str,
    smtp_port: int = 587,
    username: str,
    password: str,
    sender: str,
    sender_name: str | None = None,
    dry_run: bool = True,
    limit: int | None = None,
    only_roll: str | None = None,
    resend: bool = False,
    delay_seconds: float = 0.0,
    smtp_security: str = "starttls",
    test_recipient: str | None = None,
    confirm_count: int | None = None,
) -> tuple[list[dict[str, str]], Path]:
    if delay_seconds < 0:
        raise ValueError("delay_seconds must be non-negative")
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive")
    if not _valid_email(sender):
        raise ValueError(f"invalid sender email address: {sender!r}")
    if test_recipient and not _valid_email(test_recipient):
        raise ValueError(f"invalid test recipient email address: {test_recipient!r}")
    if smtp_security not in {"starttls", "ssl"}:
        raise ValueError("smtp_security must be 'starttls' or 'ssl'")
    queue_path = Path(queue_csv).resolve()
    if not queue_path.is_file():
        raise FileNotFoundError(f"email queue not found: {queue_path}")
    release_dir = queue_path.parent
    parsed_root = release_dir.parent
    metadata = _release_metadata(release_dir)
    expected_queue_hash = str(metadata.get("queue_sha256") or "")
    if expected_queue_hash and _sha256_file(queue_path) != expected_queue_hash:
        raise ValueError("email_queue.csv changed after preparation; prepare a new release")
    if metadata.get("enforce_sender") and sender.lower() != str(metadata.get("sender") or "").lower():
        raise ValueError(
            f"sender {sender!r} does not match the frozen release sender {metadata.get('sender')!r}"
        )
    rows = _read_queue(queue_path)
    _validate_queue_rows(rows, metadata)
    if not rows:
        raise ValueError("email queue contains no recipients")
    if only_roll:
        rows = [row for row in rows if str(row.get("roll_no")) == str(only_roll)]
        if not rows:
            raise ValueError(f"roll number {only_roll!r} is not present in the email queue")
    log_path = release_dir / EMAIL_LOG_CSV
    already_sent = (
        _sent_recipients(log_path)
        if not dry_run and not resend and not test_recipient
        else set()
    )
    skipped_rows = [
        row
        for row in rows
        if (str(row.get("roll_no") or ""), str(row.get("student_email") or "").lower()) in already_sent
    ]
    if already_sent:
        rows = [
            row
            for row in rows
            if (str(row.get("roll_no") or ""), str(row.get("student_email") or "").lower()) not in already_sent
        ]
    if limit is not None:
        rows = rows[:limit]
    if test_recipient:
        rows = rows[:1]
    if not dry_run and rows and not password:
        raise ValueError("SMTP password/app password is required for real sending")
    if not dry_run and rows and not test_recipient and confirm_count != len(rows):
        raise ValueError(
            f"bulk send requires confirm_count={len(rows)} for the currently unsent recipients; "
            f"received {confirm_count!r}"
        )

    frozen = bool(metadata.get("enforce_sender"))
    _validate_selected_files(
        rows, parsed_root=parsed_root, release_dir=release_dir, frozen=frozen,
    )

    log_rows: list[dict[str, str]] = [
        {
            "created_at": _now(),
            "roll_no": row.get("roll_no", ""),
            "student_email": row.get("student_email", ""),
            "status": "SKIPPED_ALREADY_SENT",
            "message": "previous SENT entry exists; use --resend to override",
            "subject": row.get("subject", ""),
            "verified_sheet_pdf_path": row.get("verified_sheet_pdf_path", ""),
            "actual_recipient": row.get("student_email", ""),
            "attachment_sha256": row.get("attachment_sha256", ""),
            "release_id": row.get("release_id", ""),
            "message_id": row.get("message_id", ""),
        }
        for row in skipped_rows
    ]
    if log_rows:
        _append_log(log_path, log_rows)

    lock = _real_send_lock(release_dir) if not dry_run and rows else nullcontext()
    with lock, _smtp_session(
        enabled=not dry_run and bool(rows),
        host=smtp_host,
        port=smtp_port,
        security=smtp_security,
        username=username,
        password=password,
    ) as smtp:
        for row in rows:
            actual_recipient = test_recipient or row.get("student_email", "")
            message_id = row.get("message_id") or make_msgid(
                domain=sender.rpartition("@")[2] or None
            )
            try:
                sheet_path = _queue_file(
                    row,
                    "verified_sheet_pdf_path",
                    parsed_root=parsed_root,
                    release_dir=release_dir,
                    frozen=frozen,
                )
                summary_path = _queue_file(
                    row,
                    "summary_txt_path",
                    parsed_root=parsed_root,
                    release_dir=release_dir,
                    frozen=frozen,
                )
                body_path = _queue_file(
                    row,
                    "body_txt_path",
                    parsed_root=parsed_root,
                    release_dir=release_dir,
                    frozen=frozen,
                )
                assert sheet_path is not None and body_path is not None
                body = body_path.read_text(encoding="utf-8")
                subject = row["subject"]
                if test_recipient:
                    subject = f"[SMARTOMR TEST for {row['roll_no']}] {subject}"
                    body = (
                        "SMARTOMR TEST EMAIL - no student has been contacted.\n"
                        f"Intended recipient: {row['student_email']}\n"
                        f"Roll number: {row['roll_no']}\n\n"
                        + body
                    )
                    message_id = make_msgid(domain=sender.rpartition("@")[2] or None)
                message = _build_email_message(
                    sender=sender,
                    sender_name=sender_name,
                    recipient=actual_recipient,
                    subject=subject,
                    body=body,
                    attachments=[sheet_path] + ([summary_path] if summary_path is not None else []),
                    message_id=message_id,
                )
                if dry_run:
                    status = "DRY_RUN"
                    message_text = "prepared but not sent"
                else:
                    assert smtp is not None
                    refused = smtp.send_message(message)
                    if refused:
                        raise smtplib.SMTPRecipientsRefused(refused)
                    status = "TEST_SENT" if test_recipient else "SENT"
                    message_text = (
                        f"test sent to {test_recipient}; intended recipient was {row['student_email']}"
                        if test_recipient
                        else "sent"
                    )
                    if delay_seconds > 0:
                        time.sleep(delay_seconds)
            except Exception as exc:  # Continue sending other students.
                status = "FAILED"
                message_text = f"{type(exc).__name__}: {exc}"
            log_row = {
                "created_at": _now(),
                "roll_no": row.get("roll_no", ""),
                "student_email": row.get("student_email", ""),
                "status": status,
                "message": message_text,
                "subject": row.get("subject", ""),
                "verified_sheet_pdf_path": row.get("verified_sheet_pdf_path", ""),
                "actual_recipient": actual_recipient,
                "attachment_sha256": row.get("attachment_sha256", ""),
                "release_id": row.get("release_id", ""),
                "message_id": message_id,
            }
            log_rows.append(log_row)
            _append_log(log_path, [log_row])

    return log_rows, log_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smartomr-email",
        description="Prepare and send per-student SmartOMR result emails.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Build queue, summaries, and preview emails")
    prepare.add_argument("--parsed-dir", required=True, type=Path)
    prepare.add_argument("--sender", default="professor@example.edu")
    prepare.add_argument("--sender-name", default=None)
    prepare.add_argument("--subject", default=EVALUATED_SUBJECT)
    prepare.add_argument("--body-template", default=None)
    prepare.add_argument("--include-unverified", action="store_true")
    prepare.add_argument("--include-needs-review", action="store_true")
    prepare.add_argument("--sheet-only", action="store_true", help="Attach only the verified sheet; omit marks and answer summary")

    folder = subparsers.add_parser(
        "prepare-folder",
        help="Build a frozen email release from <roll>.pdf files, a roster, and optional marks",
    )
    folder.add_argument("--pdf-dir", required=True, type=Path)
    folder.add_argument("--roster", required=True, type=Path)
    folder.add_argument("--output-dir", required=True, type=Path)
    folder.add_argument("--exam-id", required=True)
    folder.add_argument("--sender", required=True)
    folder.add_argument("--sender-name", default=None)
    folder.add_argument("--subject", default=None)
    template_group = folder.add_mutually_exclusive_group()
    template_group.add_argument("--body-template", default=None)
    template_group.add_argument(
        "--body-template-file",
        type=Path,
        default=None,
        help="UTF-8 text file supporting {display_name}, {roll_no}, {exam_id}, {marks_obtained}, {max_marks}",
    )
    folder.add_argument("--marks-file", type=Path, default=None, help="CSV/XLSX containing roll and marks columns")
    folder.add_argument(
        "--max-marks",
        type=float,
        default=None,
        help="Maximum marks used when the marks file has no max_marks/total column",
    )
    folder.add_argument("--expected-pages", type=int, default=2)
    folder.add_argument("--max-attachment-mb", type=float, default=20.0)

    send = subparsers.add_parser("send", help="Send a prepared email queue through SMTP")
    send.add_argument("--queue-csv", required=True, type=Path)
    send.add_argument("--smtp-host", default="smtp.gmail.com")
    send.add_argument("--smtp-port", type=int, default=587)
    send.add_argument("--smtp-security", choices=("starttls", "ssl"), default="starttls")
    send.add_argument("--username", required=True)
    send.add_argument("--sender", required=True)
    send.add_argument("--sender-name", default=None)
    send.add_argument("--password-env", default="SMARTOMR_SMTP_PASSWORD")
    send.add_argument("--password-stdin", action="store_true")
    send.add_argument("--send", action="store_true", help="Actually send. Without this, only a dry-run log is written.")
    send.add_argument("--limit", type=int, default=None)
    send.add_argument("--only-roll", default=None)
    send.add_argument("--resend", action="store_true", help="Allow sending again to recipients already logged as SENT")
    send.add_argument("--delay-seconds", type=float, default=0.25, help="Pause between real emails")
    send.add_argument(
        "--test-recipient",
        default=None,
        help="Send exactly one queue item to this address without marking the student as sent",
    )
    send.add_argument(
        "--confirm-count",
        type=int,
        default=None,
        help="Required for a real non-test send; must equal the currently unsent recipient count",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            metadata, queue_path = prepare_email_release(
                args.parsed_dir,
                sender=args.sender,
                sender_name=args.sender_name,
                subject_template=args.subject,
                body_template=args.body_template,
                include_unverified=args.include_unverified,
                include_needs_review=args.include_needs_review,
                sheet_only=args.sheet_only,
            )
            print(f"Wrote {queue_path}")
            print(f"queued={metadata['queued']} skipped={metadata['skipped']}")
            print(f"skipped_csv={queue_path.parent / EMAIL_SKIPPED_CSV}")
            print(f"previews_dir={queue_path.parent / 'previews'}")
            return 0

        if args.command == "prepare-folder":
            body_template = _read_body_template(args.body_template, args.body_template_file)
            metadata, queue_path = prepare_folder_email_release(
                args.pdf_dir,
                args.roster,
                args.output_dir,
                exam_id=args.exam_id,
                sender=args.sender,
                sender_name=args.sender_name,
                subject_template=args.subject,
                body_template=body_template,
                marks_file=args.marks_file,
                default_max_marks=args.max_marks,
                expected_pages=args.expected_pages,
                max_attachment_mb=args.max_attachment_mb,
            )
            print(f"Wrote {queue_path}")
            print(f"release_id={metadata['release_id']}")
            print(f"queued={metadata['queued']} skipped={metadata['skipped']}")
            print(f"skipped_csv={queue_path.parent / EMAIL_SKIPPED_CSV}")
            print(f"previews_dir={queue_path.parent / 'previews'}")
            return 0

        password = ""
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            password = os.environ.get(args.password_env, "")
            if not password and args.send:
                password = getpass.getpass(f"SMTP password ({args.password_env}): ")
        if args.send and not password:
            raise ValueError(
                f"SMTP password is required. Set {args.password_env}, use --password-stdin, or type it at prompt."
            )

        rows, log_path = send_email_release(
            args.queue_csv,
            smtp_host=args.smtp_host,
            smtp_port=args.smtp_port,
            username=args.username,
            password=password,
            sender=args.sender,
            sender_name=args.sender_name,
            dry_run=not args.send,
            limit=args.limit,
            only_roll=args.only_roll,
            resend=args.resend,
            delay_seconds=args.delay_seconds,
            smtp_security=args.smtp_security,
            test_recipient=args.test_recipient,
            confirm_count=args.confirm_count,
        )
        sent = sum(1 for row in rows if row["status"] == "SENT")
        test_sent = sum(1 for row in rows if row["status"] == "TEST_SENT")
        dry = sum(1 for row in rows if row["status"] == "DRY_RUN")
        failed = sum(1 for row in rows if row["status"] == "FAILED")
        skipped = sum(1 for row in rows if row["status"] == "SKIPPED_ALREADY_SENT")
        print(f"Wrote {log_path}")
        print(
            f"sent={sent} test_sent={test_sent} dry_run={dry} "
            f"failed={failed} already_sent={skipped}"
        )
        if not args.send:
            print("dry_run=true add --send only after reviewing the queue/previews")
        return 0 if failed == 0 else 1
    except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError, smtplib.SMTPException) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
