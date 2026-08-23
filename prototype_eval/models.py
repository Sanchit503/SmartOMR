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
