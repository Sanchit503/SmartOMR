"""Modules 4/5 — grading (PROJECT_SPEC.md Sections 7-8).

`bubbles` holds the manifest-driven fill-ratio reading primitives shared by
MCQ grading and (from Phase 2) roll-number digit reading. `mcq` is the
Section 7 auto-grader. Written-answer grading (Section 8) lands here in
Phase 3, behind the provider-agnostic `grade_written()` interface.
"""
from .bubbles import fill_ratio, student_mark_fill_ratio
from .mcq import MCQGrade, MCQOutcome, MCQReading, grade_mcq_responses, mcq_sample_centers, read_mcq_responses
from .numeric import NumericOutcome, NumericReading, normalize_numeric_answer, numeric_sample_centers, read_numeric_responses

__all__ = [
    "MCQGrade",
    "MCQOutcome",
    "MCQReading",
    "NumericOutcome",
    "NumericReading",
    "fill_ratio",
    "grade_mcq_responses",
    "mcq_sample_centers",
    "normalize_numeric_answer",
    "numeric_sample_centers",
    "read_mcq_responses",
    "read_numeric_responses",
    "student_mark_fill_ratio",
]
