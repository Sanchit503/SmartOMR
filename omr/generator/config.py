"""Generator input contract (Section 4.1 of PROJECT_SPEC.md)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ExamType = Literal["quiz", "midsem", "endsem"]
DEFAULT_UNIVERSITY_NAME = "IIIT Delhi"


class WrittenQuestionConfig(BaseModel):
    q_no: int
    max_marks: float = Field(gt=0)
    lines: int = Field(gt=0)


class NumericalQuestionConfig(BaseModel):
    q_no: int = Field(gt=0)
    max_marks: float = Field(gt=0, allow_inf_nan=False)
    digits: int = Field(ge=1, le=8, strict=True)


class ExamConfig(BaseModel):
    exam_id: str
    university_name: str = DEFAULT_UNIVERSITY_NAME
    course_code: str
    exam_name: str
    exam_type: ExamType
    num_mcq: int = Field(ge=0)
    mcq_options: int = Field(default=4, ge=2, le=6)
    marks_per_mcq: float = 1
    written_questions: list[WrittenQuestionConfig] = Field(default_factory=list)
    numerical_questions: list[NumericalQuestionConfig] = Field(default_factory=list)
    roster_csv: str | None = None

    @field_validator("written_questions")
    @classmethod
    def _unique_q_no(cls, v: list[WrittenQuestionConfig]) -> list[WrittenQuestionConfig]:
        q_nos = [w.q_no for w in v]
        if len(q_nos) != len(set(q_nos)):
            raise ValueError("written_questions q_no values must be unique")
        return v

    @field_validator("written_questions")
    @classmethod
    def _q_no_after_mcqs(cls, v: list[WrittenQuestionConfig], info) -> list[WrittenQuestionConfig]:
        num_mcq = info.data.get("num_mcq")
        if num_mcq is not None:
            for w in v:
                if w.q_no <= num_mcq:
                    raise ValueError(
                        f"written question q_no={w.q_no} collides with MCQ numbering "
                        f"(num_mcq={num_mcq}); written q_no must be > num_mcq"
                    )
        return v

    @model_validator(mode="after")
    def _has_at_least_one_question(self) -> ExamConfig:
        """An answer sheet with nothing to answer is always a mistake, and it
        would otherwise generate a valid-looking single page of identity
        fields — the kind of output that gets printed 200 times before anyone
        notices."""
        if self.num_mcq == 0 and not self.written_questions and not self.numerical_questions:
            raise ValueError(
                "this exam has no questions: MCQ, numerical, and written sections are empty"
            )
        numbers = [q.q_no for q in self.numerical_questions]
        if len(numbers) != len(set(numbers)):
            raise ValueError("numerical_questions q_no values must be unique")
        if any(number <= self.num_mcq for number in numbers):
            raise ValueError("numerical question numbering collides with MCQs")
        if numbers and any(q.q_no <= max(numbers) for q in self.written_questions):
            raise ValueError("written question numbers must follow all numerical questions")
        return self
