from __future__ import annotations

import csv
import json
from pathlib import Path

from PIL import Image

from omr.io import load_written_question_metadata
from omr.workflows.written import (
    auto_grade_written_answers,
    export_written_grading_packet,
    import_written_marks,
    load_written_summary,
)


def _write_crop(path: Path, shade: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("L", (160, 48), shade).save(path)


def _write_student(
    parsed_dir: Path,
    roll_no: str,
    *,
    status: str,
    verified: bool,
    mcq_score: float | None = 8,
    mcq_total: float | None = 10,
) -> dict:
    student_dir = parsed_dir / "students" / roll_no
    written = []
    for offset, q_no in enumerate((11, 12), start=1):
        crop_path = student_dir / "written" / f"Q{q_no}.png"
        _write_crop(crop_path, 210 - offset)
        written.append(
            {
                "q_no": q_no,
                "page": offset,
                "crop_path": f"written/Q{q_no}.png",
                "max_marks": 2.0,
                "lines": 2,
                "ocr": {
                    "text": f"answer {q_no}",
                    "confidence": "medium",
                    "provider": "fake",
                    "review_flags": [],
                },
            }
        )
    details_path = student_dir / "student.json"
    details_path.write_text(
        json.dumps(
            {
                "student": {
                    "roll_no": roll_no,
                    "program": "BTECH",
                    "name": f"Student {roll_no[-1]}",
                    "email": f"{roll_no}@example.edu",
                },
                "written_responses": written,
            }
        ),
        encoding="utf-8",
    )
    return {
        "roll_no": roll_no,
        "status": status,
        "parser_status": "ready" if verified else "needs_review",
        "program": "BTECH",
        "student_name": f"Student {roll_no[-1]}",
        "student_email": f"{roll_no}@example.edu",
        "mcq_score": mcq_score,
        "mcq_total": mcq_total,
        "pages_found": [1, 2],
        "missing_pages": [],
        "source_indices": [1, 2],
        "sheet_pdf_path": f"students/{roll_no}/sheet.pdf",
        "verified_sheet_pdf_path": f"verified/students/{roll_no}/sheet.pdf" if verified else None,
        "details_path": f"students/{roll_no}/student.json",
        "review_flags": [],
        "decision_log": [],
        "eligible_for_email": verified,
    }


def _write_verified_index(tmp_path: Path) -> Path:
    parsed_dir = tmp_path / "parsed" / "WRITTEN_EXAM"
    parsed_dir.mkdir(parents=True)
    verified_student = _write_student(parsed_dir, "2024001", status="verified", verified=True)
    review_student = _write_student(parsed_dir, "2024002", status="needs_review", verified=False)
    payload = {
        "schema_version": 1,
        "exam_id": "WRITTEN_EXAM",
        "mode": "verification_review",
        "expected_pages": 2,
        "students": [verified_student, review_student],
        "unmatched_pages": [],
        "page_errors": [],
        "status_counts": {},
        "reports": {},
    }
    (parsed_dir / "verified_index.json").write_text(json.dumps(payload), encoding="utf-8")
    return parsed_dir


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_export_written_grading_packet_uses_verified_students_by_default(tmp_path: Path):
    parsed_dir = _write_verified_index(tmp_path)

    packet, packet_path = export_written_grading_packet(parsed_dir)

    assert packet_path == parsed_dir / "written_grading" / "written_packet.json"
    assert packet_path.exists()
    assert len(packet["students"]) == 1
    assert packet["students"][0]["roll_no"] == "2024001"
    assert [(answer["roll_no"], answer["q_no"]) for answer in packet["answers"]] == [
        ("2024001", 11),
        ("2024001", 12),
    ]
    assert (parsed_dir / "written_grading" / "written_review.html").exists()
    template_rows = _csv_rows(parsed_dir / "written_grading" / "manual_marks_template.csv")
    assert len(template_rows) == 2
    assert template_rows[0]["marks_awarded"] == ""
    assert template_rows[0]["crop_path"] == "students/2024001/written/Q11.png"
    answer_rows = _csv_rows(parsed_dir / "written_grading" / "written_answer_index.csv")
    assert answer_rows[0]["ocr_text"] == "answer 11"


def test_export_written_grading_packet_accepts_utf8_bom_json(tmp_path: Path):
    parsed_dir = _write_verified_index(tmp_path)
    verified_index_path = parsed_dir / "verified_index.json"
    verified_index = json.loads(verified_index_path.read_text(encoding="utf-8"))
    verified_index_path.write_text(json.dumps(verified_index), encoding="utf-8-sig")

    packet, _packet_path = export_written_grading_packet(parsed_dir)

    assert len(packet["answers"]) == 2


def test_export_can_include_unverified_for_debugging(tmp_path: Path):
    parsed_dir = _write_verified_index(tmp_path)

    packet, _packet_path = export_written_grading_packet(parsed_dir, include_unverified=True)

    assert {student["roll_no"] for student in packet["students"]} == {"2024001", "2024002"}
    assert len(packet["answers"]) == 4


def test_export_written_packet_includes_rubric_metadata(tmp_path: Path):
    parsed_dir = _write_verified_index(tmp_path)
    rubric_path = parsed_dir / "rubric.csv"
    rubric_path.write_text(
        "q_no,question_text,rubric,model_answer,max_marks\n"
        "11,Define OMR,Award one point for definition and one for use,A scanner-readable form,2\n"
        "12,Explain alignment,Award partial credit for fiducials,Fiducials correct perspective,2\n",
        encoding="utf-8",
    )

    packet, _packet_path = export_written_grading_packet(parsed_dir)

    assert packet["question_metadata"]["path"] == "rubric.csv"
    assert packet["question_metadata"]["questions_loaded"] == 2
    assert packet["question_metadata"]["missing_q_nos"] == []
    first = packet["answers"][0]
    assert first["question_text"] == "Define OMR"
    assert first["rubric"].startswith("Award one point")
    assert first["model_answer"] == "A scanner-readable form"
    template_rows = _csv_rows(parsed_dir / "written_grading" / "manual_marks_template.csv")
    assert template_rows[0]["question_text"] == "Define OMR"
    assert template_rows[0]["rubric"].startswith("Award one point")
    answer_rows = _csv_rows(parsed_dir / "written_grading" / "written_answer_index.csv")
    assert answer_rows[1]["question_text"] == "Explain alignment"
    html_text = (parsed_dir / "written_grading" / "written_review.html").read_text(encoding="utf-8")
    assert "Define OMR" in html_text
    assert "Fiducials correct perspective" in html_text


def test_import_written_marks_writes_grades_and_final_scores(tmp_path: Path):
    parsed_dir = _write_verified_index(tmp_path)
    rubric_path = parsed_dir / "rubric.csv"
    rubric_path.write_text(
        "q_no,question_text,rubric,model_answer,max_marks\n"
        "11,Define OMR,Award one point for definition and one for use,A scanner-readable form,2\n"
        "12,Explain alignment,Award partial credit for fiducials,Fiducials correct perspective,2\n",
        encoding="utf-8",
    )
    export_written_grading_packet(parsed_dir, rubric_path=rubric_path)
    marks_csv = parsed_dir / "marks.csv"
    marks_csv.write_text(
        "roll_no,q_no,marks_awarded,needs_human_review,grader_comment\n"
        "2024001,11,2,no,Good\n"
        "2024001,12,1.5,no,Partial\n",
        encoding="utf-8",
    )

    payload, grades_path = import_written_marks(parsed_dir, marks_csv, grader="TA")

    assert grades_path == parsed_dir / "written_grading" / "written_grades.json"
    assert payload["status_counts"]["answers"] == {"graded": 2, "pending": 0, "needs_review": 0}
    assert payload["grades"][0]["question_text"] == "Define OMR"
    assert payload["grades"][1]["rubric"].startswith("Award partial")
    student = payload["students"][0]
    assert student["written_status"] == "complete"
    assert student["written_score"] == 3.5
    assert student["written_total"] == 4.0
    assert student["total_score"] == 11.5
    assert student["total_marks"] == 14.0
    final_rows = _csv_rows(parsed_dir / "written_grading" / "final_scores.csv")
    assert final_rows[0]["total_score"] == "11.5"
    grade_rows = _csv_rows(parsed_dir / "written_grading" / "written_grades_report.csv")
    assert grade_rows[1]["marks_awarded"] == "1.5"
    assert "answers_graded=2" in load_written_summary(parsed_dir)


def test_auto_grade_mock_is_review_only_by_default(tmp_path: Path):
    parsed_dir = _write_verified_index(tmp_path)
    export_written_grading_packet(parsed_dir)

    payload, grades_path = auto_grade_written_answers(
        parsed_dir,
        provider="mock",
        mock_marks_fraction=0.5,
        mock_confidence="medium",
    )

    assert grades_path == parsed_dir / "written_grading" / "written_grades.json"
    assert payload["mode"] == "written_provider_grades"
    assert payload["provider"] == "mock"
    assert payload["status_counts"]["answers"] == {"graded": 0, "pending": 0, "needs_review": 2}
    assert payload["status_counts"]["students"] == {"complete": 0, "pending": 0, "needs_review": 1}
    first = payload["grades"][0]
    assert first["marks_awarded"] == 1.0
    assert first["status"] == "needs_review"
    assert first["transcribed_answer"] == "answer 11"
    assert "not be used as final marks" in first["review_flags"][0]
    assert "answers_needs_review=2" in load_written_summary(parsed_dir)


def test_auto_grade_mock_final_mode_writes_combined_scores_for_tests(tmp_path: Path):
    parsed_dir = _write_verified_index(tmp_path)
    rubric_path = parsed_dir / "rubric.csv"
    rubric_path.write_text(
        "q_no,question_text,rubric,model_answer,max_marks\n"
        "11,Define OMR,Award one point for definition and one for use,A scanner-readable form,2\n"
        "12,Explain alignment,Award partial credit for fiducials,Fiducials correct perspective,2\n",
        encoding="utf-8",
    )
    export_written_grading_packet(parsed_dir, rubric_path=rubric_path)

    payload, _grades_path = auto_grade_written_answers(
        parsed_dir,
        provider="mock",
        mock_marks_fraction=0.5,
        mock_confidence="high",
        mock_final=True,
    )

    assert payload["status_counts"]["answers"] == {"graded": 2, "pending": 0, "needs_review": 0}
    assert payload["grades"][0]["question_text"] == "Define OMR"
    assert payload["grades"][0]["provider"] == "mock"
    assert payload["grades"][0]["method"] == "mock_written"
    student = payload["students"][0]
    assert student["written_status"] == "complete"
    assert student["written_score"] == 2.0
    assert student["written_total"] == 4.0
    assert student["total_score"] == 10.0
    assert student["total_marks"] == 14.0
    final_rows = _csv_rows(parsed_dir / "written_grading" / "final_scores.csv")
    assert final_rows[0]["grading_complete"] == "true"
    grade_rows = _csv_rows(parsed_dir / "written_grading" / "written_grades_report.csv")
    assert grade_rows[0]["provider"] == "mock"
    assert "not be used as final marks" in grade_rows[0]["review_flags"]
    html_text = (parsed_dir / "written_grading" / "written_review.html").read_text(encoding="utf-8")
    assert "mock_written" in html_text or "Mock written grader" in html_text


def test_import_written_marks_rejects_over_max_marks(tmp_path: Path):
    parsed_dir = _write_verified_index(tmp_path)
    export_written_grading_packet(parsed_dir)
    marks_csv = parsed_dir / "bad_marks.csv"
    marks_csv.write_text(
        "roll_no,q_no,marks_awarded,needs_human_review,grader_comment\n"
        "2024001,11,3,no,Too much\n",
        encoding="utf-8",
    )

    try:
        import_written_marks(parsed_dir, marks_csv, grader="TA")
    except ValueError as exc:
        assert "must be between 0 and 2" in str(exc)
    else:
        raise AssertionError("over-max written marks should fail validation")


def test_written_question_metadata_rejects_duplicate_questions(tmp_path: Path):
    rubric_path = tmp_path / "rubric.csv"
    rubric_path.write_text(
        "q_no,question_text,rubric,max_marks\n"
        "11,Define OMR,Award one point,2\n"
        "11,Duplicate,Award another point,2\n",
        encoding="utf-8",
    )

    try:
        load_written_question_metadata(rubric_path)
    except ValueError as exc:
        assert "duplicates Q11" in str(exc)
    else:
        raise AssertionError("duplicate written rubric rows should fail validation")
