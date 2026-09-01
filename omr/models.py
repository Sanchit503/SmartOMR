"""Small data records shared across reader, I/O, and workflow layers.

These are deliberately plain dataclasses, not database models. The persistent
`Exam`, `ScannedSheet`, `Grade`, and re-eval tables from PROJECT_SPEC.md Section 3
will land later behind the backend/database layer; these records keep the
current file-based scanner workflow typed without pulling in that future stack.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Student:
    roll_no: str
    name: str
    email: str
    program: str
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AnswerKeyEntry:
    q_no: int
    answer: str
    marks: float


@dataclass(frozen=True)
class RollRead:
    program: str | None
    roll_no: str | None
    confidence: str
    ratios: dict[str, dict[str, float]]
    review_flags: list[str]


@dataclass(frozen=True)
class AlignedPage:
    page_index: int
    source_index: int
    image: object
    alignment_confidence: float
    page_mark_confidence: float
    debug_image: object | None = None


@dataclass(frozen=True)
class EvaluationResult:
    scan_path: str
    status: str
    roll_no: str | None
    program: str | None
    student_name: str | None
    student_email: str | None
    score: float
    total: float
    answers: dict[int, str | None]
    review_flags: list[str]
    details_path: str | None = None


@dataclass(frozen=True)
class ParsedPage:
    page_index: int
    source_index: int
    canonical_image_path: str | None
    debug_image_path: str | None
    alignment_confidence: float
    page_mark_confidence: float
    alignment_quality_status: str = "unknown"
    alignment_quality_score: float | None = None
    alignment_report_path: str | None = None
    alignment_overlay_path: str | None = None
    sampling_overlay_path: str | None = None


@dataclass(frozen=True)
class WrittenCrop:
    q_no: int
    page: int
    crop_path: str
    max_marks: float
    lines: int
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float


__all__ = [
    "AlignedPage",
    "AnswerKeyEntry",
    "EvaluationResult",
    "ParsedPage",
    "RollRead",
    "Student",
    "WrittenCrop",
]
