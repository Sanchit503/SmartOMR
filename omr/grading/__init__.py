"""Modules 4/5 — grading (PROJECT_SPEC.md Sections 7-8).

`bubbles` holds the manifest-driven fill-ratio reading primitives shared by
MCQ grading and roll-number digit reading. `mcq` is the Section 7 auto-grader.
`written` holds the provider-agnostic Section 8 contracts used by manual,
mock, and future LLM-assisted written grading.
"""
from .bubbles import fill_ratio, student_mark_fill_ratio
from .mcq import MCQGrade, MCQOutcome, MCQReading, grade_mcq_responses, mcq_sample_centers, read_mcq_responses
from .written import (
    MockWrittenGrader,
    WrittenGradeRequest,
    WrittenGradeResult,
    WrittenGrader,
    build_written_grader,
    grade_written_answer,
    grade_written_answers,
    validate_written_grade_result,
)

__all__ = [
    "MCQGrade",
    "MCQOutcome",
    "MCQReading",
    "MockWrittenGrader",
    "WrittenGradeRequest",
    "WrittenGradeResult",
    "WrittenGrader",
    "build_written_grader",
    "fill_ratio",
    "grade_mcq_responses",
    "grade_written_answer",
    "grade_written_answers",
    "mcq_sample_centers",
    "read_mcq_responses",
    "student_mark_fill_ratio",
    "validate_written_grade_result",
]
