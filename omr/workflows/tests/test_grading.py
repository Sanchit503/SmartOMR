import json

import pytest

from omr.generator.config import ExamConfig, NumericalQuestionConfig
from omr.generator.generate import generate_exam
from omr.workflows import grading, review
from omr.workflows.tests.test_ownership import fixture_run


@pytest.fixture
def run(tmp_path, monkeypatch):
    fixture_run(tmp_path, slots=1)
    exam = generate_exam(ExamConfig(exam_id="OWNERSHIP_TEST", course_code="TEST", exam_name="Test",
        exam_type="quiz", num_mcq=0, numerical_questions=[NumericalQuestionConfig(q_no=1, max_marks=1, digits=2)]),
        tmp_path / "exam")
    key = tmp_path / "key.csv"
    key.write_text("q_no,answer,marks\n1,7,1\n", encoding="utf-8")
    monkeypatch.setattr(grading, "read_mcq_responses", lambda *args: [])
    monkeypatch.setattr(grading, "_numerical_payload", lambda *args: ([{"q_no": 1, "value": 7}], 1.0, 1.0))
    monkeypatch.setattr(grading, "crop_written_responses", lambda *args: [])
    return tmp_path, exam["manifest_path"], key


def test_grading_preserves_ownership_and_original_parser(run):
    root, manifest, key = run
    before = review._load_json(root / review.VERIFIED_INDEX_NAME)["students"][0]
    raw = (root / "parse_index.json").read_bytes()
    original_details = (root / "student.json").read_bytes()
    result = grading.grade_selected_sheets(root, manifest, key)
    after = review._load_json(root / review.VERIFIED_INDEX_NAME)["students"][0]
    assert result["graded"] == 1
    assert after["pages"] == before["pages"] and after["manual_pages"] == before["manual_pages"]
    assert after["ownership_decision"] == before["ownership_decision"]
    assert after["status"] == "auto_matched" and not after["eligible_for_email"]
    assert after["numerical_score"] == 1 and after["numerical_total"] == 1
    assert (root / "parse_index.json").read_bytes() == raw
    assert (root / "student.json").read_bytes() == original_details


def test_invalid_key_does_not_start_or_apply_grading(run):
    root, manifest, key = run
    key.write_text("q_no,answer,marks\n2,7,1\n", encoding="utf-8")
    before = (root / review.VERIFIED_INDEX_NAME).read_bytes()
    with pytest.raises(ValueError, match="missing Q1"):
        grading.grade_selected_sheets(root, manifest, key)
    assert (root / review.VERIFIED_INDEX_NAME).read_bytes() == before
    assert not (root / "grading_runs").exists()


def test_missing_image_leaves_review_unmodified(run):
    root, manifest, key = run
    (root / "1.png").unlink()
    before = (root / review.VERIFIED_INDEX_NAME).read_bytes()
    with pytest.raises(ValueError, match="Selected image missing"):
        grading.grade_selected_sheets(root, manifest, key)
    assert (root / review.VERIFIED_INDEX_NAME).read_bytes() == before


def test_changed_review_cannot_be_overwritten_by_grading(run):
    root, manifest, key = run
    def progress(_values):
        review.hold_student(root, "2024001", reviewer="human", reason="Checked a different source")
    with pytest.raises(ValueError, match="Review changed during grading"):
        grading.grade_selected_sheets(root, manifest, key, progress=progress)
    index = review._load_json(root / review.VERIFIED_INDEX_NAME)
    assert index["students"][0]["status"] == "needs_review"
    assert "grading_details_path" not in index["students"][0]


def test_unresolved_students_are_skipped_not_reassigned(run):
    root, manifest, key = run
    index = review._load_json(root / review.VERIFIED_INDEX_NAME)
    index["students"].append({"roll_no": "2024002", "status": "needs_review", "pages": [], "manual_pages": []})
    review._write_verified_index(root, index)
    result = grading.grade_selected_sheets(root, manifest, key)
    assert result["graded"] == 1
    assert result["skipped"][0]["roll_no"] == "2024002"
    assert review._load_json(root / review.VERIFIED_INDEX_NAME)["students"][1]["pages"] == []


def test_answer_warnings_do_not_change_page_ownership(run, monkeypatch):
    root, manifest, key = run
    before = review._load_json(root / review.VERIFIED_INDEX_NAME)["students"][0]["pages"]
    def numerical(images, manifest, dpi, key, flags):
        flags.append("Q1: faint answer")
        return [], 0, 1
    monkeypatch.setattr(grading, "_numerical_payload", numerical)
    summary = grading.grade_selected_sheets(root, manifest, key)
    student = review._load_json(root / review.VERIFIED_INDEX_NAME)["students"][0]
    assert student["pages"] == before
    assert student["status"] == "needs_review" and student["answer_review_flags"] == ["Q1: faint answer"]
    assert summary["needs_answer_review"] == 1


def test_changed_selection_invalidates_marks_and_release(run):
    root, manifest, key = run
    grading.grade_selected_sheets(root, manifest, key)
    index = review._load_json(root / review.VERIFIED_INDEX_NAME)
    student = index["students"][0]
    student["pages"][0]["source_index"] = 99
    student["status"] = "verified"
    student["verified_sheet_pdf_path"] = "sheet.pdf"
    review._write_verified_index(root, index)
    student = review._load_json(root / review.VERIFIED_INDEX_NAME)["students"][0]
    assert student["grading_status"] == "stale" and student["numerical_score"] is None
    assert not student["eligible_for_email"]


def test_dropped_question_does_not_require_an_answer(run):
    root, manifest, key = run
    key.write_text("q_no,answer,marks\n1,DROP,0\n", encoding="utf-8")
    assert grading.grade_selected_sheets(root, manifest, key)["graded"] == 1


def test_real_two_page_numerical_grading_reuses_grouped_images(tmp_path, monkeypatch):
    from PIL import ImageDraw
    from omr.reader.tests.test_numerical import _fill_answer
    from omr.workflows.tests.test_batch import _render_pages
    from omr.workflows import batch, email

    fixture_run(tmp_path, slots=2)
    exam = generate_exam(ExamConfig(exam_id="OWNERSHIP_TEST", course_code="TEST", exam_name="Test",
        exam_type="quiz", num_mcq=0,
        numerical_questions=[NumericalQuestionConfig(q_no=q, max_marks=1, digits=2) for q in range(1, 16)]),
        tmp_path / "exam")
    assert exam["manifest"]["num_pages"] == 2
    images = _render_pages(exam["pdf_path"])
    for entry in exam["manifest"]["numerical_block"]:
        _fill_answer(ImageDraw.Draw(images[entry["page"]]), exam["manifest"], entry["q_no"], "07")
    for page, image in images.items():
        image.save(tmp_path / f"{page}.png")
    key = tmp_path / "key.csv"
    key.write_text("q_no,answer,marks\n" + "".join(
        f"{q},{'DROP' if q == 2 else '7|8' if q == 3 else '7'},{0 if q == 2 else 1}\n"
        for q in range(1, 16)), encoding="utf-8")
    monkeypatch.setattr(batch, "align_scan_page", lambda *args, **kwargs: pytest.fail("No realignment during grading"))
    monkeypatch.setattr(batch, "_page_identity", lambda *args, **kwargs: pytest.fail("No OCR during grading"))
    before = review._load_json(tmp_path / review.VERIFIED_INDEX_NAME)["students"][0]["pages"]
    grading.grade_selected_sheets(tmp_path, exam["manifest_path"], key)
    student = review._load_json(tmp_path / review.VERIFIED_INDEX_NAME)["students"][0]
    assert student["pages"] == before
    assert student["numerical_score"] == 14 and student["numerical_total"] == 14
    assert not student["answer_review_flags"]
    details, _path = email._load_student_details(student, tmp_path)
    assert email._score_from_details(details, student) == (14, 14)
