"""Read unsigned integer answers from manifest-defined decimal digit grids."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from omr.contracts.geometry import digit_grid_centers_mm, mm_to_px, px_per_mm
from omr.grading.bubbles import ink_density, student_mark_fill_ratio
from omr.grading.mcq import (
    DEFAULT_AMBIGUOUS_FLOOR,
    DEFAULT_FILL_THRESHOLD,
    DEFAULT_INK_FLOOR,
    DEFAULT_MIN_MARGIN,
    MCQOutcome,
    _assess,
)
from omr.reader.identity import _calibrate_digit_grid


@dataclass(frozen=True)
class NumericalReading:
    q_no: int
    page: int
    outcome: str
    digits_text: str | None
    value: int | None
    confidence: str
    needs_human_review: bool
    review_flags: list[str]
    columns: list[dict]


def numerical_sample_centers(
    gray: np.ndarray, manifest: dict, entry: dict, dpi: float,
) -> dict[tuple[int, int], tuple[int, int]]:
    calibration = _calibrate_digit_grid(gray, manifest, dpi, entry)
    centers = {}
    for key, (x, y) in digit_grid_centers_mm(entry).items():
        cx, cy = calibration.apply_mm(x, y, dpi) if calibration else mm_to_px(x, y, dpi)
        centers[key] = (round(cx), round(cy))
    return centers


def read_numerical_responses(
    images_by_page: dict[int, np.ndarray], manifest: dict, dpi: float,
) -> list[NumericalReading]:
    radius = max(1, round(manifest["bubble_sample_radius_mm"] * px_per_mm(dpi)))
    results = []
    for entry in manifest.get("numerical_block", []):
        page = entry["page"]
        if page not in images_by_page:
            raise KeyError(f"no canonical image for numerical Q{entry['q_no']} on page {page}")
        gray = np.asarray(images_by_page[page])
        if gray.ndim == 3:
            gray = gray.mean(axis=2).astype(np.uint8)
        centers = numerical_sample_centers(gray, manifest, entry, dpi)
        columns = []
        flags = []
        position_name = "column" if entry["orientation"] == "vertical" else "row"
        for column in range(entry["positions"]):
            ratios, inks = {}, {}
            for digit in range(10):
                cx, cy = centers[(column, digit)]
                ratios[str(digit)] = student_mark_fill_ratio(gray, cx, cy, radius)
                inks[str(digit)] = ink_density(gray, cx, cy, radius)
            outcome, selected, confidence, review, reason = _assess(
                ratios, inks, DEFAULT_FILL_THRESHOLD, DEFAULT_AMBIGUOUS_FLOOR,
                DEFAULT_MIN_MARGIN, DEFAULT_INK_FLOOR,
            )
            columns.append({
                "column": column + 1, "outcome": outcome.value, "selected_digit": selected,
                "confidence": confidence.value, "needs_human_review": review,
                "review_reason": reason, "fill_ratios": ratios, "ink_densities": inks,
            })
            if review:
                flags.append(f"place-value {position_name} {column + 1}: {reason}")

        outcomes = [column["outcome"] for column in columns]
        if MCQOutcome.MULTIPLE.value in outcomes:
            outcome = "multiple"
        elif flags:
            outcome = "ambiguous"
        elif all(value == MCQOutcome.BLANK.value for value in outcomes):
            outcome = "blank"
        elif MCQOutcome.BLANK.value in outcomes:
            outcome = "incomplete"
            flags.append(f"some place-value {position_name}s are blank; fill every {position_name} including leading zeros")
        else:
            outcome = "answered"
        text = "".join(column["selected_digit"] for column in columns) if outcome == "answered" else None
        results.append(NumericalReading(
            q_no=entry["q_no"], page=page, outcome=outcome,
            digits_text=text, value=int(text) if text is not None else None,
            confidence="low" if flags else "high", needs_human_review=bool(flags),
            review_flags=flags, columns=columns,
        ))
    return results
