from __future__ import annotations

import csv
import json
from pathlib import Path

import pymupdf
from PIL import Image

from omr.workflows.review import (
    assign_unmatched_page,
    decide_identity_suggestion,
    initialize_verification_index,
    reject_student,
    verify_student,
)
from omr.workflows.identity_resolution import resolution_digest


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


def _write_identity_suggestion(parsed_dir: Path, *, blocked: bool = False) -> str:
    candidate_id = "path-b-2024002-source-3"
    resolution = {
        "schema_version": 1,
        "policy_version": "test-policy",
        "mode": "shadow_only",
        "candidates": [
            {
                "candidate_id": candidate_id,
                "tier": "suggested",
                "path": "B",
                "reason_code": "ANCHOR_VIA_PAGE2",
                "roll_no": "2024002",
                "program": "BTECH",
                "sheet_page": 2,
                "page_one_source_index": 4,
                "continuation_source_index": 3,
                "source_indices": [4, 3],
                "page_one": {
                    "source_index": 4,
                    "sheet_page": 1,
                    "page_image_path": "students/2024002/pages/page_1.png",
                    "bubble_roll": "2024002",
                    "cell_roll": "2024002",
                    "cell_min_probability": 0.96,
                    "cell_probabilities": [],
                    "cell_crop_paths": [],
                },
                "continuation": {
                    "source_index": 3,
                    "sheet_page": 2,
                    "page_image_path": "unmatched_pages/source_0003/pages/page_2.png",
                    "cell_roll": "2024002",
                    "cell_min_probability": 0.94,
                    "cell_probabilities": [],
                    "cell_crop_paths": [],
                    "selector_state": "clear",
                },
                "near_neighbours": [],
                "evidence_flags": [],
                "blocked": blocked,
                "shadow_auto_eligible": not blocked,
                "automatic_attachment_enabled": False,
                "validation_gate": "disabled_pending_blind_labels",
            }
        ],
    }
    resolution["digest"] = resolution_digest(resolution)
    (parsed_dir / "identity_resolution.json").write_text(json.dumps(resolution), encoding="utf-8")
    parse_path = parsed_dir / "parse_index.json"
    parse_index = json.loads(parse_path.read_text(encoding="utf-8"))
    parse_index["identity_resolution_path"] = "identity_resolution.json"
    parse_index["identity_resolution"] = {
        "digest": resolution["digest"],
        "suggested_candidates": 1,
    }
    parse_path.write_text(json.dumps(parse_index), encoding="utf-8")
    return candidate_id


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


def test_approve_identity_suggestion_rebuilds_review_bundle_without_changing_parse_index(tmp_path: Path):
    parsed_dir = _write_parsed_batch(tmp_path)
    candidate_id = _write_identity_suggestion(parsed_dir)
    original_parse_index = (parsed_dir / "parse_index.json").read_bytes()
    initialize_verification_index(parsed_dir, force=True)

    index, _index_path = decide_identity_suggestion(
        parsed_dir,
        candidate_id,
        action="approve",
        reviewer="TA",
        note="Compared both full pages and all seven digit cells.",
    )

    student = next(item for item in index["students"] if item["roll_no"] == "2024002")
    unmatched = next(item for item in index["unmatched_pages"] if item["source_index"] == 3)
    assert student["status"] == "needs_review"
    assert student["eligible_for_email"] is False
    assert student["pages_found"] == [1, 2]
    assert student["source_indices"] == [3, 4]
    assert student["verified_sheet_pdf_path"] is None
    assert unmatched["status"] == "assigned"
    assert unmatched["assigned_to_roll_no"] == "2024002"
    assert index["identity_suggestion_decisions"][0]["suggestion_action"] == "approve"
    assert (parsed_dir / "parse_index.json").read_bytes() == original_parse_index

    index, _index_path = verify_student(
        parsed_dir,
        "2024002",
        reviewer="TA",
        note="Verified the rebuilt two-page sheet.",
    )
    verified = next(item for item in index["students"] if item["roll_no"] == "2024002")
    assert verified["eligible_for_email"] is True
    with pymupdf.open(parsed_dir / verified["verified_sheet_pdf_path"]) as doc:
        assert len(doc) == 2


def test_reject_identity_suggestion_records_audit_without_moving_pages(tmp_path: Path):
    parsed_dir = _write_parsed_batch(tmp_path)
    candidate_id = _write_identity_suggestion(parsed_dir, blocked=True)
    initialize_verification_index(parsed_dir, force=True)

    index, _index_path = decide_identity_suggestion(
        parsed_dir,
        candidate_id,
        action="reject",
        reviewer="TA",
        note="The handwriting does not match the proposed roll.",
    )

    student = next(item for item in index["students"] if item["roll_no"] == "2024002")
    unmatched = next(item for item in index["unmatched_pages"] if item["source_index"] == 3)
    assert student["pages_found"] == [1]
    assert student["manual_pages"] == []
    assert unmatched["status"] == "needs_review"
    assert index["identity_suggestion_decisions"][0]["candidate_was_blocked"] is True
    assert index["identity_suggestion_decisions"][0]["suggestion_action"] == "reject"


def test_identity_suggestion_cannot_be_decided_twice(tmp_path: Path):
    parsed_dir = _write_parsed_batch(tmp_path)
    candidate_id = _write_identity_suggestion(parsed_dir)
    initialize_verification_index(parsed_dir, force=True)
    decide_identity_suggestion(
        parsed_dir,
        candidate_id,
        action="reject",
        reviewer="TA",
        note="Rejected once.",
    )

    try:
        decide_identity_suggestion(
            parsed_dir,
            candidate_id,
            action="approve",
            reviewer="TA",
            note="Attempted second decision.",
        )
    except ValueError as exc:
        assert "already has a recorded decision" in str(exc)
    else:
        raise AssertionError("a second identity-suggestion decision must be rejected")


def test_identity_suggestion_can_be_assigned_to_a_different_known_roster_roll(tmp_path: Path):
    parsed_dir = _write_parsed_batch(tmp_path)
    parse_path = parsed_dir / "parse_index.json"
    parse_index = json.loads(parse_path.read_text(encoding="utf-8"))
    parse_index["roster_reconciliation"] = {
        "missing_students": [
            {
                "roll_no": "2024003",
                "program": "BTECH",
                "student_name": "Student 3",
                "student_email": "2024003@example.edu",
            }
        ]
    }
    parse_path.write_text(json.dumps(parse_index), encoding="utf-8")
    candidate_id = _write_identity_suggestion(parsed_dir)
    initialize_verification_index(parsed_dir, force=True)

    index, _index_path = decide_identity_suggestion(
        parsed_dir,
        candidate_id,
        action="assign",
        assigned_roll_no="2024003",
        reviewer="TA",
        note="Both full-page handwritten rolls read 2024003.",
    )

    target = next(item for item in index["students"] if item["roll_no"] == "2024003")
    old_owner = next(item for item in index["students"] if item["roll_no"] == "2024002")
    assert target["pages_found"] == [1, 2]
    assert target["source_indices"] == [3, 4]
    assert target["eligible_for_email"] is False
    assert old_owner["pages_found"] == []
    assert old_owner["missing_pages"] == [1, 2]
    decision = index["identity_suggestion_decisions"][0]
    assert decision["suggestion_action"] == "assign"
    assert decision["proposed_roll_no"] == "2024002"
    assert decision["resolved_roll_no"] == "2024003"


def test_identity_suggestion_rejects_manual_assignment_outside_known_roster(tmp_path: Path):
    parsed_dir = _write_parsed_batch(tmp_path)
    candidate_id = _write_identity_suggestion(parsed_dir)
    initialize_verification_index(parsed_dir, force=True)

    try:
        decide_identity_suggestion(
            parsed_dir,
            candidate_id,
            action="assign",
            assigned_roll_no="2024999",
            reviewer="TA",
            note="Attempting an unknown roll.",
        )
    except ValueError as exc:
        assert "not present in the review roster" in str(exc)
    else:
        raise AssertionError("manual identity assignment must be limited to the known roster")
