"""Numeric-answer bubble reading.

Numeric quiz questions are not MCQs: each answer has one or more digit
places, and every place is a 0-9 bubble row. The manifest owns the locations;
this reader only measures those locations and combines the selected digits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import numpy as np

from omr.contracts.geometry import mm_to_px, px_per_mm
from omr.grading.bubbles import ink_density, student_mark_fill_ratio
from omr.grading.mcq import DEFAULT_AMBIGUOUS_FLOOR, DEFAULT_FILL_THRESHOLD, DEFAULT_MIN_MARGIN


class NumericOutcome(str, Enum):
    ANSWERED = "answered"
    BLANK = "blank"
    MULTIPLE = "multiple"
    PARTIAL = "partial"


class NumericConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class NumericPlaceReading:
    place_index: int
    selected_digit: str | None
    outcome: NumericOutcome
    fill_ratios: dict[str, float]
    ink_densities: dict[str, float] = field(default_factory=dict)
    confidence: NumericConfidence = NumericConfidence.HIGH
    review_reason: str | None = None


@dataclass(frozen=True)
class NumericReading:
    q_no: int
    outcome: NumericOutcome
    answer: str | None
    places: list[NumericPlaceReading]
    confidence: NumericConfidence = NumericConfidence.HIGH
    needs_human_review: bool = False
    review_reason: str | None = None


def _as_gray_array(image: np.ndarray) -> np.ndarray:
    gray = np.asarray(image)
    if gray.ndim == 3:
        return gray.mean(axis=2).astype(np.uint8)
    return gray


def numeric_digit_center(entry: dict, manifest: dict, place_index: int, digit: int, dpi: float) -> tuple[int, int]:
    return mm_to_px(
        entry["x_mm"] + manifest["numeric_label_offset_mm"] + digit * manifest["numeric_digit_pitch_mm"],
        entry["y_mm"] + place_index * manifest["numeric_place_row_pitch_mm"],
        dpi,
    )


def numeric_sample_centers(
    _gray: np.ndarray,
    entries: list[dict],
    manifest: dict,
    dpi: float,
) -> dict[tuple[int, int, str], tuple[int, int]]:
    centers: dict[tuple[int, int, str], tuple[int, int]] = {}
    for entry in entries:
        for place_index in range(int(entry["digits"])):
            for digit in range(10):
                centers[(int(entry["q_no"]), place_index, str(digit))] = numeric_digit_center(
                    entry,
                    manifest,
                    place_index,
                    digit,
                    dpi,
                )
    return centers


def _assess_place(
    ratios: dict[str, float],
    inks: dict[str, float],
) -> tuple[NumericOutcome, str | None, NumericConfidence, str | None]:
    filled = [digit for digit, ratio in ratios.items() if ratio >= DEFAULT_FILL_THRESHOLD]
    ranked = sorted(ratios, key=lambda digit: ratios[digit], reverse=True)
    top = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else top
    margin = ratios[top] - ratios[runner_up]

    if len(filled) == 1:
        selected = filled[0]
        if margin < DEFAULT_MIN_MARGIN:
            return (
                NumericOutcome.ANSWERED,
                selected,
                NumericConfidence.LOW,
                f"digit {selected} only beats next digit by {margin:.2f}",
            )
        return NumericOutcome.ANSWERED, selected, NumericConfidence.HIGH, None

    if len(filled) > 1:
        return NumericOutcome.MULTIPLE, None, NumericConfidence.LOW, f"multiple digits filled: {', '.join(filled)}"

    marked = [
        digit
        for digit, ratio in ratios.items()
        if ratio >= DEFAULT_AMBIGUOUS_FLOOR or inks.get(digit, 0.0) >= 0.24
    ]
    if len(marked) == 1:
        return NumericOutcome.PARTIAL, marked[0], NumericConfidence.LOW, f"only faint digit {marked[0]} detected"
    if len(marked) > 1:
        return NumericOutcome.MULTIPLE, None, NumericConfidence.LOW, f"ambiguous faint digits: {', '.join(marked)}"
    return NumericOutcome.BLANK, None, NumericConfidence.HIGH, None


def _question_outcome(places: list[NumericPlaceReading]) -> tuple[NumericOutcome, str | None, NumericConfidence, bool, str | None]:
    reasons = [place.review_reason for place in places if place.review_reason]
    if any(place.outcome == NumericOutcome.MULTIPLE for place in places):
        return NumericOutcome.MULTIPLE, None, NumericConfidence.LOW, True, "; ".join(reasons)
    if all(place.outcome == NumericOutcome.BLANK for place in places):
        return NumericOutcome.BLANK, None, NumericConfidence.HIGH, False, None
    if any(place.selected_digit is None for place in places):
        return NumericOutcome.PARTIAL, None, NumericConfidence.LOW, True, "; ".join(reasons)

    answer = "".join(str(place.selected_digit) for place in places)
    confidence = NumericConfidence.LOW if any(place.confidence == NumericConfidence.LOW for place in places) else NumericConfidence.HIGH
    return (
        NumericOutcome.ANSWERED,
        answer,
        confidence,
        confidence == NumericConfidence.LOW,
        "; ".join(reasons) or None,
    )


def read_numeric_responses(
    images_by_page: dict[int, np.ndarray],
    manifest: dict,
    dpi: float,
) -> list[NumericReading]:
    radius_px = max(1, round(manifest["bubble_sample_radius_mm"] * px_per_mm(dpi)))
    gray_by_page = {page: _as_gray_array(image) for page, image in images_by_page.items()}
    entries = sorted(manifest.get("numeric_block", []), key=lambda entry: (entry.get("page", 1), entry["q_no"]))
    readings: list[NumericReading] = []

    for entry in entries:
        page = entry.get("page", 1)
        if page not in gray_by_page:
            raise KeyError(f"no canonical image provided for page {page} (needed for Q{entry['q_no']})")
        image = gray_by_page[page]
        places: list[NumericPlaceReading] = []
        for place_index in range(int(entry["digits"])):
            ratios: dict[str, float] = {}
            inks: dict[str, float] = {}
            for digit in range(10):
                cx, cy = numeric_digit_center(entry, manifest, place_index, digit, dpi)
                key = str(digit)
                ratios[key] = student_mark_fill_ratio(image, cx, cy, radius_px)
                inks[key] = ink_density(image, cx, cy, radius_px)
            outcome, selected, confidence, reason = _assess_place(ratios, inks)
            places.append(
                NumericPlaceReading(
                    place_index=place_index,
                    selected_digit=selected,
                    outcome=outcome,
                    fill_ratios=ratios,
                    ink_densities=inks,
                    confidence=confidence,
                    review_reason=reason,
                )
            )
        outcome, answer, confidence, needs_review, reason = _question_outcome(places)
        readings.append(
            NumericReading(
                q_no=int(entry["q_no"]),
                outcome=outcome,
                answer=answer,
                places=places,
                confidence=confidence,
                needs_human_review=needs_review,
                review_reason=reason,
            )
        )
    return readings


def normalize_numeric_answer(answer: str, digits: int) -> str:
    compact = "".join(ch for ch in str(answer).strip() if ch.isdigit())
    if not compact:
        return ""
    return compact.zfill(digits)[-digits:]
