"""Compatibility exports for the old prototype package path.

The dataclasses now live in `omr.models`; this module remains so existing
demo commands and imports do not break during the Phase 2 promotion.
"""
from omr.models import (
    AlignedPage,
    AnswerKeyEntry,
    EvaluationResult,
    RollRead,
    Student,
)

__all__ = [
    "AlignedPage",
    "AnswerKeyEntry",
    "EvaluationResult",
    "RollRead",
    "Student",
]
