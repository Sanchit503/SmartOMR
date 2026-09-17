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
import html
import json
import os
import smtplib
import ssl
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any


VERIFIED_INDEX_NAME = "verified_index.json"
EMAIL_RELEASE_DIR = "email_release"
EMAIL_QUEUE_CSV = "email_queue.csv"
EMAIL_SKIPPED_CSV = "email_skipped.csv"
EMAIL_LOG_CSV = "email_send_log.csv"
EMAIL_SCHEMA_VERSION = 2
EVALUATED_SUBJECT = "{exam_id}: evaluated OMR response sheet"
VERIFICATION_SUBJECT = "{exam_id}: response sheet verification"

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
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


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


def _load_student_details(student: dict[str, Any], parsed_root: Path) -> tuple[dict[str, Any], Path | None]:
    details_path = _resolve_path(student.get("details_path"), parsed_root)
    if details_path is None or not details_path.exists():
        return {}, None
    return _load_json(details_path), details_path.parent


def _score_from_details(details: dict[str, Any], student: dict[str, Any]) -> tuple[float | None, float | None]:
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
        rows.append(
            {
                "question": f"Q{item.get('q_no')}",
                "type": "MCQ",
                "student_answer": str(item.get("selected_option") or ""),
                "correct_answer": str(item.get("correct_option") or ""),
                "marks": f"{_format_number(item.get('marks_awarded'))}/{_format_number(item.get('marks'))}",
                "status": str(item.get("outcome") or ""),
            }
        )
    for item in details.get("numerical_responses", []):
        value = item.get("value")
        correct = item.get("correct_value")
        rows.append(
            {
                "question": f"Q{item.get('q_no')}",
                "type": "NUMERIC",
                "student_answer": "" if value is None else str(value),
                "correct_answer": "" if correct is None else str(correct),
                "marks": f"{_format_number(item.get('marks_awarded'))}/{_format_number(item.get('max_marks'))}",
                "status": str(item.get("outcome") or ""),
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


def _build_email_message(
    *,
    sender: str,
    sender_name: str | None,
    recipient: str,
    subject: str,
    body: str,
    attachments: list[Path],
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = f"{sender_name} <{sender}>" if sender_name else sender
    message["To"] = recipient
    message["Subject"] = subject
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

        message = _build_email_message(
            sender=sender,
            sender_name=sender_name,
            recipient=email,
            subject=subject,
            body=body,
            attachments=attachments,
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

    _write_csv(release_dir / EMAIL_QUEUE_CSV, queue, QUEUE_COLUMNS)
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
        "queue_csv": f"{EMAIL_RELEASE_DIR}/{EMAIL_QUEUE_CSV}",
        "skipped_csv": f"{EMAIL_RELEASE_DIR}/{EMAIL_SKIPPED_CSV}",
    }
    _write_json(release_dir / "email_release.json", metadata)
    return metadata, release_dir / EMAIL_QUEUE_CSV


def _read_queue(queue_csv: Path) -> list[dict[str, str]]:
    with queue_csv.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _append_log(path: Path, rows: list[dict[str, str]]) -> None:
    exists = path.exists()
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
) -> tuple[list[dict[str, str]], Path]:
    if delay_seconds < 0:
        raise ValueError("delay_seconds must be non-negative")
    queue_path = Path(queue_csv).resolve()
    release_dir = queue_path.parent
    parsed_root = release_dir.parent
    rows = _read_queue(queue_path)
    if only_roll:
        rows = [row for row in rows if str(row.get("roll_no")) == str(only_roll)]
    log_path = release_dir / EMAIL_LOG_CSV
    already_sent = _sent_recipients(log_path) if not dry_run and not resend else set()
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

    log_rows: list[dict[str, str]] = [
        {
            "created_at": _now(),
            "roll_no": row.get("roll_no", ""),
            "student_email": row.get("student_email", ""),
            "status": "SKIPPED_ALREADY_SENT",
            "message": "previous SENT entry exists; use --resend to override",
            "subject": row.get("subject", ""),
            "verified_sheet_pdf_path": row.get("verified_sheet_pdf_path", ""),
        }
        for row in skipped_rows
    ]
    smtp: smtplib.SMTP | None = None
    if not dry_run and rows:
        smtp = smtplib.SMTP(smtp_host, smtp_port, timeout=60)
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(username, password)
    try:
        for row in rows:
            try:
                sheet_path = _resolve_path(row["verified_sheet_pdf_path"], parsed_root)
                summary_path = _resolve_path(row.get("summary_txt_path"), parsed_root)
                body_path = _resolve_path(row["body_txt_path"], parsed_root)
                if sheet_path is None or not sheet_path.exists():
                    raise FileNotFoundError(f"sheet attachment not found: {sheet_path}")
                if row.get("summary_txt_path") and (summary_path is None or not summary_path.exists()):
                    raise FileNotFoundError(f"summary attachment not found: {summary_path}")
                if body_path is None or not body_path.exists():
                    raise FileNotFoundError(f"email body not found: {body_path}")
                body = body_path.read_text(encoding="utf-8")
                message = _build_email_message(
                    sender=sender,
                    sender_name=sender_name,
                    recipient=row["student_email"],
                    subject=row["subject"],
                    body=body,
                    attachments=[sheet_path] + ([summary_path] if summary_path is not None else []),
                )
                if dry_run:
                    status = "DRY_RUN"
                    message_text = "prepared but not sent"
                else:
                    assert smtp is not None
                    smtp.send_message(message)
                    status = "SENT"
                    message_text = "sent"
                    if delay_seconds > 0:
                        time.sleep(delay_seconds)
            except Exception as exc:  # Continue sending other students.
                status = "FAILED"
                message_text = f"{type(exc).__name__}: {exc}"
            log_rows.append(
                {
                    "created_at": _now(),
                    "roll_no": row.get("roll_no", ""),
                    "student_email": row.get("student_email", ""),
                    "status": status,
                    "message": message_text,
                    "subject": row.get("subject", ""),
                    "verified_sheet_pdf_path": row.get("verified_sheet_pdf_path", ""),
                }
            )
    finally:
        if smtp is not None:
            smtp.quit()

    _append_log(log_path, log_rows)
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

    send = subparsers.add_parser("send", help="Send a prepared email queue through SMTP")
    send.add_argument("--queue-csv", required=True, type=Path)
    send.add_argument("--smtp-host", default="smtp.gmail.com")
    send.add_argument("--smtp-port", type=int, default=587)
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
        )
        sent = sum(1 for row in rows if row["status"] == "SENT")
        dry = sum(1 for row in rows if row["status"] == "DRY_RUN")
        failed = sum(1 for row in rows if row["status"] == "FAILED")
        skipped = sum(1 for row in rows if row["status"] == "SKIPPED_ALREADY_SENT")
        print(f"Wrote {log_path}")
        print(f"sent={sent} dry_run={dry} failed={failed} already_sent={skipped}")
        if not args.send:
            print("dry_run=true add --send only after reviewing the queue/previews")
        return 0 if failed == 0 else 1
    except (OSError, ValueError, json.JSONDecodeError, smtplib.SMTPException) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
