from __future__ import annotations

from pathlib import Path

import pytest

from omr.grading.written import (
    MockWrittenGrader,
    WrittenGradeRequest,
    WrittenGradeResult,
    build_written_grader,
    grade_written_answer,
    validate_written_grade_result,
)


def _request(tmp_path: Path) -> WrittenGradeRequest:
    crop_path = tmp_path / "Q11.png"
    crop_path.write_bytes(b"not-a-real-image")
    return WrittenGradeRequest(
        roll_no="2024001",
        q_no=11,
        crop_path=crop_path,
        max_marks=2.0,
        question_text="Define OMR",
        rubric="Award marks only for the expected definition.",
        model_answer="Scanner-readable marked sheet",
        ocr_text="scanner readable answer",
    )


def test_mock_written_grader_is_safe_by_default(tmp_path: Path):
    request = _request(tmp_path)
    grader = MockWrittenGrader(marks_fraction=0.5, confidence="medium")

    result = grade_written_answer(request, grader)

    assert result.provider == "mock"
    assert result.method == "mock_written"
    assert result.marks_awarded == 1.0
    assert result.needs_human_review is True
    assert result.transcribed_answer == "scanner readable answer"
    assert "not be used as final marks" in result.review_flags[0]


def test_low_confidence_written_grade_is_forced_to_review(tmp_path: Path):
    request = _request(tmp_path)
    result = WrittenGradeResult(
        q_no=11,
        transcribed_answer="text",
        marks_awarded=1.5,
        max_marks=2.0,
        justification="Enough for partial credit.",
        confidence="low",
        needs_human_review=False,
        method="unit",
        provider="unit",
    )

    validated = validate_written_grade_result(result, request)

    assert validated.needs_human_review is True
    assert "low-confidence written grade requires human review" in validated.review_flags


def test_written_grade_validation_rejects_over_max_marks(tmp_path: Path):
    request = _request(tmp_path)
    result = WrittenGradeResult(
        q_no=11,
        transcribed_answer="text",
        marks_awarded=2.5,
        max_marks=2.0,
        justification="Too high.",
        confidence="high",
        needs_human_review=False,
        method="unit",
        provider="unit",
    )

    with pytest.raises(ValueError, match="must be between 0 and 2"):
        validate_written_grade_result(result, request)


def test_build_written_grader_rejects_unknown_provider():
    with pytest.raises(ValueError, match="available providers: mock"):
        build_written_grader("unknown")
