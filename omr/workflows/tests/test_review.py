from __future__ import annotations

import csv
import json
from pathlib import Path

import pymupdf
from PIL import Image

from omr.workflows.review import (
    assign_unmatched_page,
    initialize_verification_index,
    reject_student,
    verify_student,
)


def _write_page(path: Path, shade: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (80, 120), (shade, shade, shade)).save(path)


def _write_pdf(path: Path, pages: list[Path]) -> None:
    images = [Image.open(page).convert("RGB") for page in pages]
    first, rest = images[0], images[1:]
    first.save(path, save_all=True, append_images=rest, resolution=300.0)
    for image in images:
        image.close()


def _student_artifacts(parsed_dir: Path, roll_no: str, source_indices: list[int]) -> dict[str, Path]:
    student_dir = parsed_dir / "students" / roll_no
    page_paths = []
    for page_no, source_index in enumerate(source_indices, start=1):
        page_path = student_dir / "pages" / f"page_{page_no}.png"
        _write_page(page_path, 220 - source_index)
        page_paths.append(page_path)
    sheet_pdf = student_dir / "sheet.pdf"
    _write_pdf(sheet_pdf, page_paths)
    student_json = student_dir / "student.json"
    student_json.write_text(
        json.dumps(
            {
                "student": {
                    "roll_no": roll_no,
                    "program": "BTECH",
                    "name": f"Student {roll_no[-1]}",
                    "email": f"{roll_no}@example.edu",
                }
            }
        ),
        encoding="utf-8",
    )
    return {"dir": student_dir, "sheet_pdf": sheet_pdf, "student_json": student_json}


def _write_parsed_batch(tmp_path: Path) -> Path:
    parsed_dir = tmp_path / "parsed" / "EXAM_REVIEW"
    parsed_dir.mkdir(parents=True)
    student_a = _student_artifacts(parsed_dir, "2024001", [1, 2])
    student_b = _student_artifacts(parsed_dir, "2024002", [4])

    unmatched_dir = parsed_dir / "unmatched_pages" / "source_0003"
    unmatched_page = unmatched_dir / "pages" / "page_2.png"
    _write_page(unmatched_page, 170)
    unmatched_json = unmatched_dir / "page.json"
    unmatched_json.write_text(json.dumps({"source_index": 3}), encoding="utf-8")

    parse_index = {
        "exam_id": "EXAM_REVIEW",
        "mode": "multi_student_bundle",
        "grouping_mode": "identity",
        "detected_page_sequence": [1, 2, 2, 1],
        "expected_pages": 2,
        "students": [
            {
                "roll_no": "2024001",
                "status": "ready",
                "student_name": "Student 1",
                "sheet_pdf_path": str(student_a["sheet_pdf"]),
                "mcq_score": 8,
                "mcq_total": 10,
                "pages": [
                    {"page": 1, "source_index": 1, "canonical_image_path": "pages/page_1.png"},
                    {"page": 2, "source_index": 2, "canonical_image_path": "pages/page_2.png"},
                ],
                "review_flags": [],
                "details_path": str(student_a["student_json"]),
            },
            {
                "roll_no": "2024002",
                "status": "needs_review",
                "student_name": "Student 2",
                "sheet_pdf_path": str(student_b["sheet_pdf"]),
                "mcq_score": 5,
                "mcq_total": 10,
                "pages": [
                    {"page": 1, "source_index": 4, "canonical_image_path": "pages/page_1.png"},
                ],
                "review_flags": ["partial scan: missing page(s): 2"],
                "details_path": str(student_b["student_json"]),
            },
        ],
        "unmatched_pages": [
            {
                "source_index": 3,
                "status": "needs_review",
                "page_index": 2,
                "identity_kind": "handwritten",
                "pages": [
                    {"page_index": 2, "source_index": 3, "canonical_image_path": "pages/page_2.png"},
                ],
                "review_flags": ["unmatched continuation page"],
                "details_path": str(unmatched_json),
            }
        ],
        "page_errors": [],
    }
    (parsed_dir / "parse_index.json").write_text(json.dumps(parse_index), encoding="utf-8")
    return parsed_dir


def test_initialize_verification_index_writes_reports(tmp_path: Path):
    parsed_dir = _write_parsed_batch(tmp_path)

    index, index_path = initialize_verification_index(parsed_dir, force=True)

    assert index_path == parsed_dir / "verified_index.json"
    assert index_path.exists()
    assert index["status_counts"]["students"]["pending_verification"] == 1
    assert index["status_counts"]["students"]["missing_pages"] == 1
    assert index["status_counts"]["unmatched_pages"]["needs_review"] == 1
    assert index["reports"] == {"csv": "verification_report.csv", "html": "verification_report.html"}
    assert (parsed_dir / "verification_report.html").exists()
    csv_path = parsed_dir / "verification_report.csv"
    assert csv_path.exists()
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [(row["item_type"], row["status"]) for row in rows] == [
        ("student", "pending_verification"),
        ("student", "missing_pages"),
        ("unmatched_page", "needs_review"),
    ]


def test_verify_and_reject_student_update_verified_index(tmp_path: Path):
    parsed_dir = _write_parsed_batch(tmp_path)
    initialize_verification_index(parsed_dir, force=True)

    index, _index_path = verify_student(
        parsed_dir,
        "2024001",
        reviewer="TA",
        note="Checked roll and both pages.",
    )
    student = next(item for item in index["students"] if item["roll_no"] == "2024001")

    assert student["status"] == "verified"
    assert student["eligible_for_email"] is True
    assert student["verified_sheet_pdf_path"] == "verified/students/2024001/sheet.pdf"
    with pymupdf.open(parsed_dir / student["verified_sheet_pdf_path"]) as doc:
        assert len(doc) == 2

    index, _index_path = reject_student(
        parsed_dir,
        "2024002",
        reviewer="TA",
        reason="Wrong student grouping.",
    )
    rejected = next(item for item in index["students"] if item["roll_no"] == "2024002")
    assert rejected["status"] == "rejected"
    assert rejected["eligible_for_email"] is False
    assert index["status_counts"]["students"]["verified"] == 1
    assert index["status_counts"]["students"]["rejected"] == 1


def test_assign_unmatched_page_then_verify_student(tmp_path: Path):
    parsed_dir = _write_parsed_batch(tmp_path)
    initialize_verification_index(parsed_dir, force=True)

    index, _index_path = assign_unmatched_page(
        parsed_dir,
        3,
        "2024002",
        page_index=2,
        reviewer="TA",
        note="Source 3 is the missing second page.",
    )
    student = next(item for item in index["students"] if item["roll_no"] == "2024002")
    unmatched = next(item for item in index["unmatched_pages"] if item["source_index"] == 3)
    assert student["status"] == "needs_review"
    assert student["missing_pages"] == []
    assert student["source_indices"] == [3, 4]
    assert unmatched["status"] == "assigned"
    assert unmatched["assigned_to_roll_no"] == "2024002"

    index, _index_path = verify_student(
        parsed_dir,
        "2024002",
        reviewer="TA",
        note="Verified after assigning page 2.",
    )
    verified = next(item for item in index["students"] if item["roll_no"] == "2024002")
    assert verified["status"] == "verified"
    assert verified["eligible_for_email"] is True
    with pymupdf.open(parsed_dir / verified["verified_sheet_pdf_path"]) as doc:
        assert len(doc) == 2
