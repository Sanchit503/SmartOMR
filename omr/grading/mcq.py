"""MCQ auto-grading (Section 7 of CLAUDE.md).

Reads fill ratios purely from manifest coordinates (never hardcoded
positions), then grades against a supplied answer key. Blank vs. multiple-
filled are kept as distinct outcomes so a professor can tell a scanning
issue apart from a genuine blank (Section 7, step 3).

Two decisions here exist because the output of this module is a grade:

*The reader measures a smaller disc than was printed.* The manifest's
`bubble_sample_radius_mm` is inset from `bubble_radius_mm` so the bubble's
own printed outline never counts as student ink. Measured on a real
generated sheet, sampling the full radius makes an empty bubble read 0.20
against a 0.50 threshold; sampling the inset radius takes it to ~0.00.

*A read that is merely "probably right" says so.* An absolute threshold on
its own can't tell a confident fill from a smudge sitting just over the
line, and CLAUDE.md principle 4 is explicit that a low-confidence step
queues for a human rather than guessing. So a reading also carries the
margin between the darkest bubble and its runner-up, and sets
`needs_human_review` when the evidence is thin — an erased-and-rebubbled
answer, a faint pencil fill, or two bubbles close in darkness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from ..contracts.geometry import mm_to_px, px_per_mm
from .bubbles import fill_ratio, ink_density

# Above this, a bubble counts as deliberately filled.
DEFAULT_FILL_THRESHOLD = 0.5

# A bubble this dark isn't blank paper, but isn't a confident fill either —
# a partial erase, a faint pencil, or a stray mark lands in here.
DEFAULT_AMBIGUOUS_FLOOR = 0.22

# The darkest bubble must beat its runner-up by at least this much, or the
# read is treated as too close to call.
DEFAULT_MIN_MARGIN = 0.18

# Mean-darkness floor for "someone put something here". Blank paper on a
# phone photo sits under ~0.05; a light pencil fill covering the whole
# bubble reaches ~0.25 while still scoring 0.0 on the hard fill threshold.
DEFAULT_INK_FLOOR = 0.12


class MCQOutcome(str, Enum):
    ANSWERED = "answered"
    BLANK = "blank"
    MULTIPLE = "multiple"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class MCQReading:
    q_no: int
    outcome: MCQOutcome
    selected_option: str | None
    fill_ratios: dict[str, float]
    ink_densities: dict[str, float] = field(default_factory=dict)
    confidence: Confidence = Confidence.HIGH
    needs_human_review: bool = False
    review_reason: str | None = None

    @property
    def margin(self) -> float:
        """Gap between the darkest bubble and the next darkest."""
        ranked = sorted(self.fill_ratios.values(), reverse=True)
        if len(ranked) < 2:
            return ranked[0] if ranked else 0.0
        return ranked[0] - ranked[1]


@dataclass(frozen=True)
class MCQGrade:
    q_no: int
    outcome: MCQOutcome
    selected_option: str | None
    correct_option: str | None
    marks_awarded: float
    confidence: Confidence = Confidence.HIGH
    needs_human_review: bool = False
    review_reason: str | None = None


def _assess(
    ratios: dict[str, float],
    inks: dict[str, float],
    fill_threshold: float,
    ambiguous_floor: float,
    min_margin: float,
    ink_floor: float,
) -> tuple[MCQOutcome, str | None, Confidence, bool, str | None]:
    """Turn per-option measurements into an outcome plus how much to trust it.

    `ratios` decides *what* the answer is; `inks` only ever adds doubt. A
    bubble is "marked" if either signal says so, which is what catches a
    pencil fill too light to cross the hard threshold.
    """
    def is_marked(opt: str) -> bool:
        """Did a student put something here? Either signal is enough — a
        light pencil fill scores 0.00 on `ratios` but shows up in `inks`."""
        return ratios[opt] >= ambiguous_floor or inks.get(opt, 0.0) >= ink_floor

    def describe(opt: str) -> str:
        return f"'{opt}' (fill {ratios[opt]:.2f}, ink {inks.get(opt, 0.0):.2f})"

    filled = [opt for opt, r in ratios.items() if r >= fill_threshold]
    ranked = sorted(ratios, key=lambda o: ratios[o], reverse=True)
    top_opt = ranked[0]
    runner_up_ratio = ratios[ranked[1]] if len(ranked) > 1 else 0.0
    margin = ratios[top_opt] - runner_up_ratio

    if len(filled) > 1:
        return (
            MCQOutcome.MULTIPLE,
            None,
            Confidence.LOW,
            True,
            f"{len(filled)} bubbles above the fill threshold ({', '.join(sorted(filled))})",
        )

    if len(filled) == 1:
        selected = filled[0]
        if margin < min_margin:
            return (
                MCQOutcome.ANSWERED,
                selected,
                Confidence.LOW,
                True,
                f"{describe(selected)} only beats the next bubble by {margin:.2f} "
                f"(needs {min_margin:.2f}) - too close to call between two marks",
            )
        # Any OTHER marked bubble means an erasure or a stray pen mark. Scan
        # all of them, not just the runner-up by fill ratio: a rubbed-out
        # pencil mark can be the faintest bubble by that measure and still be
        # the one that matters.
        others = [o for o in ratios if o != selected and is_marked(o)]
        if others:
            worst = max(others, key=lambda o: max(ratios[o], inks.get(o, 0.0)))
            return (
                MCQOutcome.ANSWERED,
                selected,
                Confidence.MEDIUM,
                True,
                f"'{selected}' is clear, but {describe(worst)} is also marked - "
                "possible erasure the student meant to remove",
            )
        return MCQOutcome.ANSWERED, selected, Confidence.HIGH, False, None

    # Nothing crossed the fill threshold. Genuinely blank paper and a
    # too-faint answer look the same to a threshold but not to a professor,
    # so they are reported differently (Section 7, step 3).
    marked = [o for o in ratios if is_marked(o)]
    if marked:
        worst = max(marked, key=lambda o: max(ratios[o], inks.get(o, 0.0)))
        return (
            MCQOutcome.BLANK,
            None,
            Confidence.LOW,
            True,
            f"{describe(worst)} is marked but below the {fill_threshold:.2f} fill threshold, "
            "so this may be a faint or erased answer rather than a blank",
        )
    return MCQOutcome.BLANK, None, Confidence.HIGH, False, None


def read_mcq_responses(
    images_by_page: dict[int, np.ndarray],
    manifest: dict,
    dpi: float,
    fill_threshold: float = DEFAULT_FILL_THRESHOLD,
    ambiguous_floor: float = DEFAULT_AMBIGUOUS_FLOOR,
    min_margin: float = DEFAULT_MIN_MARGIN,
    ink_floor: float = DEFAULT_INK_FLOOR,
) -> list[MCQReading]:
    """`images_by_page` maps page number -> canonical grayscale image for
    that physical page. A multi-page manifest scatters MCQ entries across
    pages, so grading a single question always reads its own page's image —
    never guessing from whichever image happens to be at hand (Section 2,
    principle 4)."""
    label_offset_mm = manifest["mcq_label_offset_mm"]
    option_pitch_mm = manifest["mcq_option_pitch_mm"]
    # Measure the inset disc, not the printed one — see the module docstring.
    radius_px = max(1, round(manifest["bubble_sample_radius_mm"] * px_per_mm(dpi)))

    readings: list[MCQReading] = []
    for entry in manifest["mcq_block"]:
        page = entry.get("page", 1)
        if page not in images_by_page:
            raise KeyError(f"no canonical image provided for page {page} (needed for Q{entry['q_no']})")
        image = images_by_page[page]

        ratios: dict[str, float] = {}
        inks: dict[str, float] = {}
        for i, opt in enumerate(entry["options"]):
            ox_mm = entry["x_mm"] + label_offset_mm + i * option_pitch_mm
            cx, cy = mm_to_px(ox_mm, entry["y_mm"], dpi)
            ratios[opt] = fill_ratio(image, cx, cy, radius_px)
            inks[opt] = ink_density(image, cx, cy, radius_px)

        outcome, selected, confidence, needs_review, reason = _assess(
            ratios, inks, fill_threshold, ambiguous_floor, min_margin, ink_floor
        )
        readings.append(
            MCQReading(
                q_no=entry["q_no"],
                outcome=outcome,
                selected_option=selected,
                fill_ratios=ratios,
                ink_densities=inks,
                confidence=confidence,
                needs_human_review=needs_review,
                review_reason=reason,
            )
        )
    return readings


def grade_mcq_responses(
    readings: list[MCQReading],
    answer_key: dict[int, str],
    marks_per_mcq: float,
) -> list[MCQGrade]:
    """Award marks, carrying each reading's confidence through untouched.

    A flagged reading still produces a provisional mark — the review queue
    needs something to show the professor — but `needs_human_review` travels
    with it so Module 6 won't auto-send a sheet that contains one
    (Section 9).
    """
    grades = []
    for r in readings:
        correct = answer_key.get(r.q_no)
        is_correct = (
            r.outcome == MCQOutcome.ANSWERED and correct is not None and r.selected_option == correct
        )
        grades.append(
            MCQGrade(
                q_no=r.q_no,
                outcome=r.outcome,
                selected_option=r.selected_option,
                correct_option=correct,
                marks_awarded=marks_per_mcq if is_correct else 0.0,
                confidence=r.confidence,
                needs_human_review=r.needs_human_review,
                review_reason=r.review_reason,
            )
        )
    return grades
