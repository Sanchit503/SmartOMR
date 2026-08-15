"""MCQ auto-grading (Section 7 of CLAUDE.md).

Reads fill ratios purely from manifest coordinates (never hardcoded
positions), then grades against a supplied answer key. Blank vs. multiple-
filled are kept as distinct outcomes so a professor can tell a scanning
issue apart from a genuine blank (Section 7, step 3).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np

from .bubbles import fill_ratio, mm_to_px

DEFAULT_FILL_THRESHOLD = 0.5


class MCQOutcome(str, Enum):
    ANSWERED = "answered"
    BLANK = "blank"
    MULTIPLE = "multiple"


@dataclass(frozen=True)
class MCQReading:
    q_no: int
    outcome: MCQOutcome
    selected_option: str | None
    fill_ratios: dict[str, float]


@dataclass(frozen=True)
class MCQGrade:
    q_no: int
    outcome: MCQOutcome
    selected_option: str | None
    correct_option: str | None
    marks_awarded: float


def read_mcq_responses(
    images_by_page: dict[int, np.ndarray],
    manifest: dict,
    dpi: float,
    fill_threshold: float = DEFAULT_FILL_THRESHOLD,
) -> list[MCQReading]:
    """`images_by_page` maps page number -> canonical grayscale image for
    that physical page. A multi-page manifest scatters MCQ entries across
    pages, so grading a single question always reads its own page's image —
    never guessing from whichever image happens to be at hand (Section 2,
    principle 4)."""
    radius_mm = manifest["bubble_radius_mm"]
    label_offset_mm = manifest["mcq_label_offset_mm"]
    option_pitch_mm = manifest["mcq_option_pitch_mm"]
    scale = dpi / 25.4
    radius_px = max(1, round(radius_mm * scale))

    readings: list[MCQReading] = []
    for entry in manifest["mcq_block"]:
        page = entry.get("page", 1)
        if page not in images_by_page:
            raise KeyError(f"no canonical image provided for page {page} (needed for Q{entry['q_no']})")
        image = images_by_page[page]

        ratios: dict[str, float] = {}
        for i, opt in enumerate(entry["options"]):
            ox_mm = entry["x_mm"] + label_offset_mm + i * option_pitch_mm
            oy_mm = entry["y_mm"]
            cx, cy = mm_to_px(ox_mm, oy_mm, dpi)
            ratios[opt] = fill_ratio(image, cx, cy, radius_px)

        filled = [opt for opt, r in ratios.items() if r >= fill_threshold]
        if len(filled) == 1:
            outcome, selected = MCQOutcome.ANSWERED, filled[0]
        elif len(filled) == 0:
            outcome, selected = MCQOutcome.BLANK, None
        else:
            outcome, selected = MCQOutcome.MULTIPLE, None

        readings.append(MCQReading(entry["q_no"], outcome, selected, ratios))
    return readings


def grade_mcq_responses(
    readings: list[MCQReading],
    answer_key: dict[int, str],
    marks_per_mcq: float,
) -> list[MCQGrade]:
    grades = []
    for r in readings:
        correct = answer_key.get(r.q_no)
        is_correct = r.outcome == MCQOutcome.ANSWERED and correct is not None and r.selected_option == correct
        marks = marks_per_mcq if is_correct else 0.0
        grades.append(MCQGrade(r.q_no, r.outcome, r.selected_option, correct, marks))
    return grades
