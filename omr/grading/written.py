"""Provider-agnostic written-answer grading contracts.

Real LLM integrations should plug in behind this small interface. The workflow
layer should receive validated `WrittenGradeResult` records regardless of
whether the source was manual marks, a test provider, or a future vision model.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol


GRADE_CONFIDENCES = {"high", "medium", "low"}


@dataclass(frozen=True)
class WrittenGradeRequest:
    roll_no: str
    q_no: int
    crop_path: Path
    max_marks: float
    question_text: str = ""
    rubric: str = ""
    model_answer: str = ""
    ocr_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["crop_path"] = str(self.crop_path)
        return payload


@dataclass(frozen=True)
class WrittenGradeResult:
    q_no: int
    transcribed_answer: str
    marks_awarded: float | None
    max_marks: float
    justification: str
    confidence: str
    needs_human_review: bool
    method: str
    provider: str
    review_flags: list[str] = field(default_factory=list)
    raw: Any | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class WrittenGrader(Protocol):
    provider: str
    method: str

    def grade(self, request: WrittenGradeRequest) -> WrittenGradeResult:
        ...


class MockWrittenGrader:
    """Deterministic dry-run grader for tests and workflow plumbing.

    This provider does not inspect handwriting. It defaults to
    `needs_human_review=True` so accidental CLI use cannot produce final marks.
    """

    provider = "mock"
    method = "mock_written"

    def __init__(
        self,
        *,
        marks_fraction: float = 0.0,
        confidence: str = "low",
        needs_human_review: bool = True,
    ) -> None:
        if marks_fraction < 0 or marks_fraction > 1:
            raise ValueError("mock marks_fraction must be between 0 and 1")
        if confidence not in GRADE_CONFIDENCES:
            raise ValueError(f"unknown mock confidence: {confidence}")
        self.marks_fraction = marks_fraction
        self.confidence = confidence
        self.needs_human_review = needs_human_review

    def grade(self, request: WrittenGradeRequest) -> WrittenGradeResult:
        marks = round(float(request.max_marks) * self.marks_fraction, 4)
        transcript = request.ocr_text.strip() or "[mock transcription unavailable]"
        return WrittenGradeResult(
            q_no=request.q_no,
            transcribed_answer=transcript,
            marks_awarded=marks,
            max_marks=float(request.max_marks),
            justification="Mock written grader for workflow testing; not a real academic grade.",
            confidence=self.confidence,
            needs_human_review=self.needs_human_review,
            method=self.method,
            provider=self.provider,
            review_flags=["mock provider output must not be used as final marks"],
            raw={"marks_fraction": self.marks_fraction},
        )


def validate_written_grade_result(
    result: WrittenGradeResult,
    request: WrittenGradeRequest,
) -> WrittenGradeResult:
    if result.q_no != request.q_no:
        raise ValueError(f"written grade Q{result.q_no} does not match request Q{request.q_no}")
    if abs(float(result.max_marks) - float(request.max_marks)) > 0.001:
        raise ValueError(
            f"written grade Q{request.q_no} max_marks {result.max_marks:g} "
            f"does not match request max_marks {request.max_marks:g}"
        )
    if result.confidence not in GRADE_CONFIDENCES:
        raise ValueError(f"written grade Q{request.q_no} has unknown confidence {result.confidence!r}")

    marks = result.marks_awarded
    flags = list(result.review_flags)
    needs_review = bool(result.needs_human_review)
    if marks is None:
        needs_review = True
        flags.append("written grader returned no marks")
    else:
        marks = float(marks)
        if marks < 0 or marks > float(request.max_marks):
            raise ValueError(
                f"written grade Q{request.q_no} marks_awarded {marks:g} "
                f"must be between 0 and {request.max_marks:g}"
            )

    if result.confidence == "low" and not needs_review:
        needs_review = True
        flags.append("low-confidence written grade requires human review")
    if not result.justification.strip():
        needs_review = True
        flags.append("written grader returned an empty justification")

    return replace(
        result,
        marks_awarded=marks,
        needs_human_review=needs_review,
        review_flags=flags,
    )


def grade_written_answer(
    request: WrittenGradeRequest,
    grader: WrittenGrader,
) -> WrittenGradeResult:
    return validate_written_grade_result(grader.grade(request), request)


def grade_written_answers(
    requests: list[WrittenGradeRequest],
    grader: WrittenGrader,
) -> list[WrittenGradeResult]:
    return [grade_written_answer(request, grader) for request in requests]


def build_written_grader(
    provider: str,
    *,
    mock_marks_fraction: float = 0.0,
    mock_confidence: str = "low",
    mock_final: bool = False,
) -> WrittenGrader:
    selected = provider.strip().lower()
    if selected == "mock":
        return MockWrittenGrader(
            marks_fraction=mock_marks_fraction,
            confidence=mock_confidence,
            needs_human_review=not mock_final,
        )
    raise ValueError(f"unknown written grader provider {provider!r}; available providers: mock")


__all__ = [
    "MockWrittenGrader",
    "WrittenGradeRequest",
    "WrittenGradeResult",
    "WrittenGrader",
    "build_written_grader",
    "grade_written_answer",
    "grade_written_answers",
    "validate_written_grade_result",
]
