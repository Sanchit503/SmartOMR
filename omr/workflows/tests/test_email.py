from __future__ import annotations

import csv
import json
from pathlib import Path

from omr.io.csv import load_students
from omr.workflows.email import prepare_email_release, send_email_release


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_roster_loader_accepts_professor_excel_headers(tmp_path: Path):
    roster = tmp_path / "master.csv"
    roster.write_text(
        "Sl.No.,Roll No.,Class Type,Student Name,Student's Current Term,Registered For,Email Id\n"
        "1,2024001,BTECH,A Student,2026 Monsoon,CSE557,a@example.edu\n"
        "2,24001,MTECH,M Student,2026 Monsoon,CSE557,m@example.edu\n"
        "3,20301,PHD,P Student,2026 Monsoon,CSE557,p@example.edu\n"
        "4,24ABC,SP,S Student,2026 Monsoon,CSE557,s@example.edu\n",
        encoding="utf-8",
    )

    students = load_students(roster)

    assert students["2024001"].name == "A Student"
    assert students["2024001"].email == "a@example.edu"
    assert students["2024001"].program == "BTECH"
    assert students["MT24001"].email == "m@example.edu"
    assert students["PHD20301"].email == "p@example.edu"
    assert students["SP24ABC"].email == "s@example.edu"


def test_roster_loader_accepts_real_portal_export_shape(tmp_path: Path):
    roster = tmp_path / "portal-export.csv"
    roster.write_text(
        "Sl.No.,Roll No.,Class Type,Student Name,Student's Current Term,Registered For,\n"
        ",,,,,,\n"
        "1,PhD25111,Lecture,Tushar  Kumar ,July 2025/PhD/ECE-IIITD/Semester 3,,phd@example.edu\n"
        "2,MT25007,Lecture,Aastha   ,July 2025/MTech (CSE)/Gen-IIITD/Semester 3,,mt@example.edu\n"
        "3,2024479,Lecture,Rohit  Gola ,July 2024/BTech/CSAI-IIITD/Semester 5,,bt@example.edu\n",
        encoding="utf-8",
    )

    students = load_students(roster)

    assert list(students) == ["PHD25111", "MT25007", "2024479"]
    assert students["PHD25111"].program == "PHD"
    assert students["MT25007"].program == "MTECH"
    assert students["2024479"].program == "BTECH"
    assert students["PHD25111"].name == "Tushar Kumar"
    assert students["MT25007"].email == "mt@example.edu"


def test_prepare_email_release_queues_only_verified_eligible_students(tmp_path: Path):
    parsed = tmp_path / "parsed" / "CSE557_QUIZ1_2026"
    verified_sheet = parsed / "verified" / "students" / "2024001" / "sheet.pdf"
    verified_sheet.parent.mkdir(parents=True)
    verified_sheet.write_bytes(b"%PDF-1.4\n% test attachment\n")
    student_dir = parsed / "students" / "2024001"
    _write_json(
        student_dir / "student.json",
        {
            "student": {
                "roll_no": "2024001",
                "name": "A Student",
                "email": "a@example.edu",
            },
            "mcq_score": 4,
            "mcq_total": 5,
            "numerical_score": 8,
            "numerical_total": 10,
            "mcq_responses": [
                {
                    "q_no": 1,
                    "selected_option": "B",
                    "correct_option": "B",
                    "marks_awarded": 1,
                    "marks": 1,
                    "outcome": "answered",
                }
            ],
            "numerical_responses": [],
        },
    )
    _write_json(
        parsed / "verified_index.json",
        {
            "exam_id": "CSE557_QUIZ1_2026",
            "students": [
                {
                    "roll_no": "2024001",
                    "status": "verified",
                    "student_name": "A Student",
                    "student_email": "a@example.edu",
                    "eligible_for_email": True,
                    "verified_sheet_pdf_path": "verified/students/2024001/sheet.pdf",
                    "details_path": "students/2024001/student.json",
                },
                {
                    "roll_no": "2024002",
                    "status": "needs_review",
                    "student_name": "B Student",
                    "student_email": "b@example.edu",
                    "eligible_for_email": False,
                    "verified_sheet_pdf_path": "",
                    "details_path": "students/2024002/student.json",
                },
            ],
        },
    )

    metadata, queue_path = prepare_email_release(
        parsed,
        sender="professor@example.edu",
        sender_name="Course Staff",
        subject_template="{exam_id}: score for {roll_no}",
        body_template="Hi {display_name},\nScore: {marks_obtained}/{max_marks}\nExam: {exam_id}\n",
    )

    assert metadata["queued"] == 1
    assert metadata["skipped"] == 1
    with queue_path.open(newline="", encoding="utf-8") as handle:
        queue_rows = list(csv.DictReader(handle))
    assert queue_rows[0]["roll_no"] == "2024001"
    assert queue_rows[0]["student_email"] == "a@example.edu"
    assert queue_rows[0]["subject"] == "CSE557_QUIZ1_2026: score for 2024001"
    assert queue_rows[0]["marks_obtained"] == "12"
    assert queue_rows[0]["max_marks"] == "15"
    assert (parsed / queue_rows[0]["preview_eml_path"]).exists()
    assert "Score: 12/15" in (parsed / queue_rows[0]["body_txt_path"]).read_text(encoding="utf-8")

    rows, log_path = send_email_release(
        queue_path,
        smtp_host="smtp.example.edu",
        username="professor@example.edu",
        password="unused",
        sender="professor@example.edu",
        dry_run=True,
    )
    assert rows[0]["status"] == "DRY_RUN"
    assert log_path.exists()

    with log_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "2026-09-17T00:00:00+00:00",
                "2024001",
                "a@example.edu",
                "SENT",
                "sent",
                queue_rows[0]["subject"],
                queue_rows[0]["verified_sheet_pdf_path"],
            ]
        )

    repeated, _log_path = send_email_release(
        queue_path,
        smtp_host="smtp.example.edu",
        username="professor@example.edu",
        password="unused",
        sender="professor@example.edu",
        dry_run=False,
    )
    assert repeated[0]["status"] == "SKIPPED_ALREADY_SENT"

    sheet_metadata, sheet_queue_path = prepare_email_release(
        parsed,
        sender="professor@example.edu",
        sheet_only=True,
    )
    assert sheet_metadata["sheet_only"] is True
    with sheet_queue_path.open(newline="", encoding="utf-8") as handle:
        sheet_row = next(csv.DictReader(handle))
    assert sheet_row["release_mode"] == "sheet_verification"
    assert sheet_row["summary_txt_path"] == ""
    assert sheet_row["marks_obtained"] == ""
    body = (parsed / sheet_row["body_txt_path"]).read_text(encoding="utf-8")
    assert "attached for verification" in body
    assert "Marks:" not in body


def test_prepare_email_release_reports_roster_students_without_sheets(tmp_path: Path):
    parsed = tmp_path / "parsed" / "CSE557_QUIZ1_2026"
    _write_json(
        parsed / "verified_index.json",
        {
            "exam_id": "CSE557_QUIZ1_2026",
            "students": [],
            "roster_reconciliation": {
                "missing_students": [
                    {
                        "roll_no": "2024999",
                        "student_name": "Missing Student",
                        "student_email": "missing@example.edu",
                        "program": "BTECH",
                    }
                ]
            },
        },
    )

    metadata, queue_path = prepare_email_release(parsed)

    assert metadata["queued"] == 0
    assert metadata["skipped"] == 1
    with (queue_path.parent / "email_skipped.csv").open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert row["roll_no"] == "2024999"
    assert row["status"] == "missing_sheet"
    assert "no parsed student sheet" in row["reason"]
