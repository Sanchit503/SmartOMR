"""Generator input contract (Section 4.1 of CLAUDE.md)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ExamType = Literal["quiz", "midsem", "endsem"]


class WrittenQuestionConfig(BaseModel):
    q_no: int
    max_marks: float = Field(gt=0)
    lines: int = Field(gt=0)


class ExamConfig(BaseModel):
    exam_id: str
    course_code: str
    exam_name: str
    exam_type: ExamType
    num_mcq: int = Field(ge=0)
    mcq_options: int = Field(default=4, ge=2, le=6)
    marks_per_mcq: float = 1
    written_questions: list[WrittenQuestionConfig] = Field(default_factory=list)
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
        if self.num_mcq == 0 and not self.written_questions:
            raise ValueError(
                "this exam has no questions: num_mcq is 0 and written_questions is empty"
            )
        return self
