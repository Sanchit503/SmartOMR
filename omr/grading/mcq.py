"""MCQ auto-grading (Section 7 of PROJECT_SPEC.md).

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
line, and PROJECT_SPEC.md principle 4 is explicit that a low-confidence step
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
DEFAULT_AMBIGUOUS_FLOOR = 0.28

# The darkest bubble must beat its runner-up by at least this much, or the
# read is treated as too close to call.
DEFAULT_MIN_MARGIN = 0.18

# Mean-darkness floor for "someone put something here". Blank paper on a
# phone photo sits under ~0.05; a light pencil fill covering the whole
# bubble reaches ~0.25 while still scoring 0.0 on the hard fill threshold.
DEFAULT_INK_FLOOR = 0.24


def _cv2():
    try:
        import cv2
    except ImportError:
        return None
    return cv2


def _as_gray_array(image: np.ndarray) -> np.ndarray:
    gray = np.asarray(image)
    if gray.ndim == 3:
        return gray.mean(axis=2).astype(np.uint8)
    return gray


def _cluster_values(values: list[float], tolerance_px: float) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if not groups or abs((sum(groups[-1]) / len(groups[-1])) - value) > tolerance_px:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group) / len(group) for group in groups]


def _best_matching_run(
    clusters: list[float],
    expected: list[float],
    max_distance_px: float,
) -> list[float] | None:
    count = len(expected)
    if len(clusters) < count:
        return None

    clusters = sorted(clusters)
    best: tuple[float, list[float]] | None = None
    for start in range(0, len(clusters) - count + 1):
        run = clusters[start : start + count]
        distances = [abs(actual - wanted) for actual, wanted in zip(run, expected)]
        if max(distances) > max_distance_px:
            continue
        score = sum(distances)
        if count > 1:
            expected_pitch = (expected[-1] - expected[0]) / (count - 1)
            pitch_error = sum(abs((run[i + 1] - run[i]) - expected_pitch) for i in range(count - 1))
            score += 0.35 * pitch_error
        if best is None or score < best[0]:
            best = (score, run)
    if best is None:
        return None
    return best[1]


def _detect_bubble_center_candidates(
    gray: np.ndarray,
    bbox: tuple[int, int, int, int],
    dpi: float,
) -> list[tuple[float, float]]:
    cv2 = _cv2()
    if cv2 is None:
        return []

    x0, y0, x1, y1 = bbox
    roi = gray[y0:y1, x0:x1]
    if roi.size == 0:
        return []

    scale = px_per_mm(dpi)
    min_side = 2.5 * scale
    max_side = 7.5 * scale
    candidates: list[tuple[float, float, float, float]] = []
    for threshold in (160, 180, 200, 220, 235):
        _ignored, binary = cv2.threshold(roi, threshold, 255, cv2.THRESH_BINARY_INV)
        contours, _hierarchy = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < 20:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            if not (min_side <= w <= max_side and min_side <= h <= max_side):
                continue
            aspect = w / h if h else 0.0
            if not 0.45 <= aspect <= 2.20:
                continue
            moments = cv2.moments(contour)
            if moments["m00"]:
                cx = x0 + moments["m10"] / moments["m00"]
                cy = y0 + moments["m01"] / moments["m00"]
            else:
                cx = x0 + x + w / 2
                cy = y0 + y + h / 2
            candidates.append((cx, cy, float(max(w, h)), area))

    unique: list[tuple[float, float, float, float]] = []
    for candidate in sorted(candidates, key=lambda item: item[3], reverse=True):
        duplicate = False
        for existing in unique:
            distance = ((candidate[0] - existing[0]) ** 2 + (candidate[1] - existing[1]) ** 2) ** 0.5
            if distance < max(5.0, min(candidate[2], existing[2]) * 0.65):
                duplicate = True
                break
        if not duplicate:
            unique.append(candidate)
    return [(cx, cy) for cx, cy, _side, _area in unique]


def _entry_option_centers(entry: dict, manifest: dict, dpi: float) -> dict[str, tuple[int, int]]:
    return {
        option: mm_to_px(
            entry["x_mm"] + manifest["mcq_label_offset_mm"] + index * manifest["mcq_option_pitch_mm"],
            entry["y_mm"],
            dpi,
        )
        for index, option in enumerate(entry["options"])
    }


def _calibrate_mcq_centers(
    gray: np.ndarray,
    entries: list[dict],
    manifest: dict,
    dpi: float,
) -> dict[tuple[int, str], tuple[int, int]]:
    if not entries:
        return {}

    scale = px_per_mm(dpi)
    calibrated: dict[tuple[int, str], tuple[int, int]] = {}
    groups: dict[tuple[float, tuple[str, ...]], list[dict]] = {}
    for entry in entries:
        groups.setdefault((float(entry["x_mm"]), tuple(entry["options"])), []).append(entry)

    for (_x_mm, options), group_entries in groups.items():
        group_entries = sorted(group_entries, key=lambda entry: (entry["y_mm"], entry["q_no"]))
        expected_by_entry = {
            entry["q_no"]: _entry_option_centers(entry, manifest, dpi)
            for entry in group_entries
        }
        expected_x = [expected_by_entry[group_entries[0]["q_no"]][option][0] for option in options]
        expected_y = [expected_by_entry[entry["q_no"]][options[0]][1] for entry in group_entries]
        all_x = [center[0] for centers in expected_by_entry.values() for center in centers.values()]
        all_y = [center[1] for centers in expected_by_entry.values() for center in centers.values()]

        pad = round(8.0 * scale)
        x0 = max(0, int(min(all_x) - pad))
        x1 = min(gray.shape[1], int(max(all_x) + pad))
        y0 = max(0, int(min(all_y) - pad))
        y1 = min(gray.shape[0], int(max(all_y) + pad))
        candidates = _detect_bubble_center_candidates(gray, (x0, y0, x1, y1), dpi)
        if len(candidates) < max(len(options), len(group_entries)):
            continue

        tolerance = 2.4 * scale
        max_distance = 6.5 * scale
        x_clusters = _cluster_values([candidate[0] for candidate in candidates], tolerance)
        y_clusters = _cluster_values([candidate[1] for candidate in candidates], tolerance)
        x_run = _best_matching_run(x_clusters, expected_x, max_distance)
        y_run = _best_matching_run(y_clusters, expected_y, max_distance)
        if x_run is None or y_run is None:
            continue

        for row_index, entry in enumerate(group_entries):
            for option_index, option in enumerate(options):
                calibrated[(entry["q_no"], option)] = (
                    int(round(x_run[option_index])),
                    int(round(y_run[row_index])),
                )
    return calibrated


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
    gray_by_page = {page: _as_gray_array(image) for page, image in images_by_page.items()}
    entries_by_page: dict[int, list[dict]] = {}
    for entry in manifest["mcq_block"]:
        entries_by_page.setdefault(entry.get("page", 1), []).append(entry)
    calibrated_by_page = {
        page: _calibrate_mcq_centers(gray_by_page[page], entries, manifest, dpi)
        for page, entries in entries_by_page.items()
        if page in gray_by_page
    }

    for entry in manifest["mcq_block"]:
        page = entry.get("page", 1)
        if page not in gray_by_page:
            raise KeyError(f"no canonical image provided for page {page} (needed for Q{entry['q_no']})")
        image = gray_by_page[page]
        calibrated_centers = calibrated_by_page.get(page, {})

        ratios: dict[str, float] = {}
        inks: dict[str, float] = {}
        for i, opt in enumerate(entry["options"]):
            ox_mm = entry["x_mm"] + label_offset_mm + i * option_pitch_mm
            cx, cy = calibrated_centers.get((entry["q_no"], opt), mm_to_px(ox_mm, entry["y_mm"], dpi))
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
