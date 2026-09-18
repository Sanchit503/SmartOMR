from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from pypdf import PdfReader, PdfWriter

from omr.io.csv import load_students
from omr.workflows import email as email_workflow
from omr.workflows.email import (
    prepare_email_release,
    prepare_folder_email_release,
    send_email_release,
)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_pdf(path: Path, pages: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    with path.open("wb") as handle:
        writer.write(handle)


def _write_xlsx(path: Path, rows: list[list[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sheet_rows = []
    for row_number, row in enumerate(rows, start=1):
        cells = []
        for column_number, value in enumerate(row, start=1):
            column = ""
            number = column_number
            while number:
                number, remainder = divmod(number - 1, 26)
                column = chr(ord("A") + remainder) + column
            cells.append(
                f'<c r="{column}{row_number}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>'
            )
        sheet_rows.append(f'<row r="{row_number}">{"".join(cells)}</row>')
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Marks" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
            '</Relationships>',
        )
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)


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


def test_prepare_folder_release_freezes_validated_roll_pdfs(tmp_path: Path):
    roster = tmp_path / "students.csv"
    roster.write_text(
        "roll_no,name,email,program\n"
        "2024001,A Student,a@example.edu,BTECH\n"
        "2024002,B Student,b@example.edu,BTECH\n",
        encoding="utf-8",
    )
    pdf_dir = tmp_path / "final-pdfs"
    source_pdf = pdf_dir / "2024001.pdf"
    _write_pdf(source_pdf)
    release = tmp_path / "release"

    metadata, queue_path = prepare_folder_email_release(
        pdf_dir,
        roster,
        release,
        exam_id="CSE557_QUIZ1_2026",
        sender="professor@example.edu",
        sender_name="Course Staff",
    )

    assert metadata["queued"] == 1
    assert metadata["skipped"] == 1
    with queue_path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    frozen = release / row["verified_sheet_pdf_path"]
    assert frozen == release / "attachments" / "2024001.pdf"
    assert len(PdfReader(str(frozen)).pages) == 2
    assert row["attachment_page_count"] == "2"
    assert row["attachment_sha256"] == email_workflow._sha256_file(frozen)
    assert row["release_id"] == metadata["release_id"]
    assert (release / row["preview_eml_path"]).exists()
    with (release / "email_skipped.csv").open(newline="", encoding="utf-8") as handle:
        skipped = next(csv.DictReader(handle))
    assert skipped["roll_no"] == "2024002"
    assert skipped["status"] == "missing_pdf"

    source_pdf.write_bytes(b"changed after preparation")
    rows, _ = send_email_release(
        queue_path,
        smtp_host="smtp.example.edu",
        username="professor@example.edu",
        password="",
        sender="professor@example.edu",
        dry_run=True,
    )
    assert rows[0]["status"] == "DRY_RUN"


def test_prepare_folder_release_imports_xlsx_marks_for_all_programs(tmp_path: Path):
    roster = tmp_path / "students.csv"
    roster.write_text(
        "roll_no,name,email,program\n"
        "2024001,B Student,b@example.edu,BTECH\n"
        "25007,M Student,m@example.edu,MTECH\n"
        "25111,P Student,p@example.edu,PHD\n",
        encoding="utf-8",
    )
    pdf_dir = tmp_path / "pdfs"
    for roll_no in ("2024001", "MT25007", "PhD25111"):
        _write_pdf(pdf_dir / f"{roll_no}.pdf")
    marks = tmp_path / "tentative_marks.xlsx"
    _write_xlsx(
        marks,
        [
            ["Roll No", "Tentative Marks", "Total Marks"],
            ["2024001", "17", "20"],
            ["MT25007", "14.5", "20"],
            ["PHD25111", "19", "20"],
        ],
    )
    template = (
        "Dear {display_name},\n\n"
        "Roll: {roll_no}\nTentative marks: {marks_obtained}/{max_marks}\nExam: {exam_id}\n"
    )

    metadata, queue_path = prepare_folder_email_release(
        pdf_dir,
        roster,
        tmp_path / "release",
        exam_id="CSE557_QUIZ1_2026",
        sender="professor@example.edu",
        marks_file=marks,
        body_template=template,
    )

    with queue_path.open(newline="", encoding="utf-8") as handle:
        rows = {row["roll_no"]: row for row in csv.DictReader(handle)}
    assert metadata["release_mode"] == "tentative_marks"
    assert metadata["queued"] == 3
    assert rows["2024001"]["marks_obtained"] == "17"
    assert rows["MT25007"]["marks_obtained"] == "14.5"
    assert rows["PHD25111"]["max_marks"] == "20"
    assert rows["PHD25111"]["subject"] == (
        "CSE557_QUIZ1_2026: tentative marks and evaluated response sheet"
    )
    body = (tmp_path / "release" / rows["MT25007"]["body_txt_path"]).read_text(encoding="utf-8")
    assert "Dear M Student" in body
    assert "Tentative marks: 14.5/20" in body
    assert (tmp_path / "release" / "inputs" / "marks.xlsx").exists()
    assert (tmp_path / "release" / "inputs" / "roster.csv").exists()


def test_prepare_folder_release_rejects_missing_or_invalid_marks(tmp_path: Path):
    roster = tmp_path / "students.csv"
    roster.write_text(
        "roll_no,name,email,program\n"
        "2024001,A Student,a@example.edu,BTECH\n"
        "2024002,B Student,b@example.edu,BTECH\n",
        encoding="utf-8",
    )
    pdf_dir = tmp_path / "pdfs"
    _write_pdf(pdf_dir / "2024001.pdf")
    _write_pdf(pdf_dir / "2024002.pdf")

    missing = tmp_path / "missing.csv"
    missing.write_text("roll_no,marks\n2024001,17\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing PDF recipient.*2024002"):
        prepare_folder_email_release(
            pdf_dir,
            roster,
            tmp_path / "missing-release",
            exam_id="EXAM",
            sender="professor@example.edu",
            marks_file=missing,
            default_max_marks=20,
        )

    excessive = tmp_path / "excessive.csv"
    excessive.write_text(
        "roll_no,marks,max_marks\n2024001,21,20\n2024002,18,20\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exceed maximum marks"):
        prepare_folder_email_release(
            pdf_dir,
            roster,
            tmp_path / "excessive-release",
            exam_id="EXAM",
            sender="professor@example.edu",
            marks_file=excessive,
        )


def test_prepare_folder_release_rejects_unknown_duplicate_and_wrong_page_count(tmp_path: Path):
    roster = tmp_path / "students.csv"
    roster.write_text(
        "roll_no,name,email,program\n2024001,A Student,a@example.edu,BTECH\n",
        encoding="utf-8",
    )

    unknown_dir = tmp_path / "unknown"
    _write_pdf(unknown_dir / "2024999.pdf")
    with pytest.raises(ValueError, match="does not match any roster roll"):
        prepare_folder_email_release(
            unknown_dir,
            roster,
            tmp_path / "unknown-release",
            exam_id="EXAM",
            sender="professor@example.edu",
        )

    duplicate_dir = tmp_path / "duplicate"
    _write_pdf(duplicate_dir / "2024001.pdf")
    _write_pdf(duplicate_dir / "2024001" / "sheet.pdf")
    with pytest.raises(ValueError, match="multiple PDFs"):
        prepare_folder_email_release(
            duplicate_dir,
            roster,
            tmp_path / "duplicate-release",
            exam_id="EXAM",
            sender="professor@example.edu",
        )

    wrong_pages = tmp_path / "wrong-pages"
    _write_pdf(wrong_pages / "2024001.pdf", pages=1)
    with pytest.raises(ValueError, match="expected 2 PDF pages, found 1"):
        prepare_folder_email_release(
            wrong_pages,
            roster,
            tmp_path / "wrong-pages-release",
            exam_id="EXAM",
            sender="professor@example.edu",
        )


def test_folder_release_test_redirect_confirmation_integrity_and_resume(tmp_path: Path, monkeypatch):
    roster = tmp_path / "students.csv"
    roster.write_text(
        "roll_no,name,email,program\n"
        "2024001,A Student,a@example.edu,BTECH\n"
        "2024002,B Student,b@example.edu,BTECH\n",
        encoding="utf-8",
    )
    pdf_dir = tmp_path / "pdfs"
    _write_pdf(pdf_dir / "2024001.pdf")
    _write_pdf(pdf_dir / "2024002.pdf")
    _metadata, queue_path = prepare_folder_email_release(
        pdf_dir,
        roster,
        tmp_path / "release",
        exam_id="EXAM",
        sender="professor@example.edu",
    )

    class FakeSmtp:
        def __init__(self):
            self.messages = []
            self.closed = False

        def send_message(self, message):
            self.messages.append(message)
            return {}

        def quit(self):
            self.closed = True

    sessions = []

    def fake_connect(**_kwargs):
        session = FakeSmtp()
        sessions.append(session)
        return session

    monkeypatch.setattr(email_workflow, "_connect_smtp", fake_connect)
    test_rows, _ = send_email_release(
        queue_path,
        smtp_host="smtp.example.edu",
        username="professor@example.edu",
        password="secret",
        sender="professor@example.edu",
        dry_run=False,
        test_recipient="course-staff@example.edu",
    )
    assert test_rows[0]["status"] == "TEST_SENT"
    assert sessions[0].messages[0]["To"] == "course-staff@example.edu"
    assert sessions[0].messages[0]["Subject"].startswith("[SMARTOMR TEST")

    with pytest.raises(ValueError, match="confirm_count=2"):
        send_email_release(
            queue_path,
            smtp_host="smtp.example.edu",
            username="professor@example.edu",
            password="secret",
            sender="professor@example.edu",
            dry_run=False,
        )

    sent_rows, _ = send_email_release(
        queue_path,
        smtp_host="smtp.example.edu",
        username="professor@example.edu",
        password="secret",
        sender="professor@example.edu",
        dry_run=False,
        confirm_count=2,
    )
    assert [row["status"] for row in sent_rows] == ["SENT", "SENT"]
    assert [message["To"] for message in sessions[1].messages] == [
        "a@example.edu",
        "b@example.edu",
    ]

    repeated, _ = send_email_release(
        queue_path,
        smtp_host="smtp.example.edu",
        username="professor@example.edu",
        password="secret",
        sender="professor@example.edu",
        dry_run=False,
    )
    assert [row["status"] for row in repeated] == [
        "SKIPPED_ALREADY_SENT",
        "SKIPPED_ALREADY_SENT",
    ]

    queue_path.write_text(queue_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after preparation"):
        send_email_release(
            queue_path,
            smtp_host="smtp.example.edu",
            username="professor@example.edu",
            password="",
            sender="professor@example.edu",
            dry_run=True,
        )
