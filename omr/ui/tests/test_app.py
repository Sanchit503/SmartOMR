from __future__ import annotations

import io

from omr.io.csv import load_students
from omr.ui.app import UPLOAD_READ_SIZE, _canonicalize_roster, parse_multipart_upload


def _multipart_body(boundary: str, parts: list[tuple[str, str | None, bytes]]) -> bytes:
    body = bytearray()
    for name, filename, payload in parts:
        body.extend(f"--{boundary}\r\n".encode())
        disposition = f'Content-Disposition: form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        body.extend((disposition + "\r\n\r\n").encode())
        body.extend(payload)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    return bytes(body)


def test_multipart_upload_streams_large_file_to_staging(tmp_path):
    boundary = "smartomr-test-boundary"
    pdf = b"%PDF-1.7\n" + b"x" * (UPLOAD_READ_SIZE + 137)
    body = _multipart_body(
        boundary,
        [
            ("exam_id", None, b"CSE557_ENDSEM_2026"),
            ("scan_pdf", "batch.pdf", pdf),
        ],
    )

    fields, files = parse_multipart_upload(
        io.BytesIO(body),
        content_type=f"multipart/form-data; boundary={boundary}",
        content_length=len(body),
        staging_dir=tmp_path / "staging",
    )

    assert fields == {"exam_id": "CSE557_ENDSEM_2026"}
    assert files["scan_pdf"].filename == "batch.pdf"
    assert files["scan_pdf"].path.read_bytes() == pdf


def test_canonicalize_roster_repairs_portal_export(tmp_path):
    roster = tmp_path / "students.csv"
    roster.write_text(
        "Sl.No.,Roll No.,Class Type,Student Name,Student's Current Term,Registered For,\n"
        ",,,,,,\n"
        "1,PhD25111,Lecture,Tushar  Kumar ,July 2025/PhD/ECE-IIITD/Semester 3,,phd@example.edu\n"
        "2,MT25007,Lecture,Aastha   ,July 2025/MTech (CSE)/Gen-IIITD/Semester 3,,mt@example.edu\n"
        "3,2024479,Lecture,Rohit  Gola ,July 2024/BTech/CSAI-IIITD/Semester 5,,bt@example.edu\n",
        encoding="utf-8",
    )

    summary = _canonicalize_roster(roster)
    students = load_students(roster)

    assert summary == {"total": 3, "PHD": 1, "MTECH": 1, "BTECH": 1}
    assert list(students) == ["PHD25111", "MT25007", "2024479"]
    assert students["PHD25111"].email == "phd@example.edu"
    assert roster.read_text(encoding="utf-8").splitlines()[0] == "roll_no,name,email,program"
