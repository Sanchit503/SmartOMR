"""Roll-number and program-bubble reading from a canonical page image.

Identity resolution remains roster-based; this module only decodes the sheet's
declared program and roll number, with review flags whenever the evidence is
ambiguous.
"""
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
)

from omr.models import RollRead
from omr.local_registration import LocalTransform, fit_candidate_local_transform, fit_ordered_local_transform


@dataclass(frozen=True)
class _GridCalibration:
    transform: LocalTransform
    matched_anchors: int
    mean_residual_mm: float
    max_residual_mm: float

    def apply_mm(self, x_mm: float, y_mm: float, dpi: float) -> tuple[float, float]:
        return self.transform.apply(mm_to_px(x_mm, y_mm, dpi))


def _bubble_signal(gray: np.ndarray, manifest: dict, dpi: float, x_mm: float, y_mm: float) -> tuple[float, float]:
    radius_px = max(1, round(manifest["bubble_sample_radius_mm"] * px_per_mm(dpi)))
    cx, cy = mm_to_px(x_mm, y_mm, dpi)
    return student_mark_fill_ratio(gray, cx, cy, radius_px), ink_density(gray, cx, cy, radius_px)


def _bubble_signal_px(gray: np.ndarray, manifest: dict, dpi: float, cx: float, cy: float) -> tuple[float, float]:
    radius_px = max(1, round(manifest["bubble_sample_radius_mm"] * px_per_mm(dpi)))
    return student_mark_fill_ratio(gray, int(round(cx)), int(round(cy)), radius_px), ink_density(
        gray,
        int(round(cx)),
        int(round(cy)),
        radius_px,
    )


def _is_marked(ratio: float, ink: float) -> bool:
    return ratio >= DEFAULT_AMBIGUOUS_FLOOR or ink >= DEFAULT_INK_FLOOR


ROLL_NOISE_FILL_FLOOR = 0.18
ROLL_INK_EXCESS_FLOOR = 0.10
ROLL_FAINT_INK_EXCESS_FLOOR = 0.12


def _column_blank_ink_baseline(signals: dict[int, tuple[float, float]], selected: int | None = None) -> float:
    blankish_inks = [
        ink
        for digit, (ratio, ink) in signals.items()
        if digit != selected and ratio < DEFAULT_AMBIGUOUS_FLOOR
    ]
    if not blankish_inks:
        blankish_inks = [ink for digit, (_ratio, ink) in signals.items() if digit != selected]
    if not blankish_inks:
        return 0.0
    return float(np.median(blankish_inks))


def _is_meaningful_roll_mark(
    ratio: float,
    ink: float,
    baseline_ink: float,
    fill_floor: float = ROLL_NOISE_FILL_FLOOR,
) -> bool:
    if ratio >= DEFAULT_AMBIGUOUS_FLOOR:
        return True
    ink_excess = ink - baseline_ink
    if ratio >= fill_floor and ink_excess >= ROLL_INK_EXCESS_FLOOR:
        return True
    return ink >= DEFAULT_INK_FLOOR and ink_excess >= ROLL_FAINT_INK_EXCESS_FLOOR


def _extra_roll_marks(
    signals: dict[int, tuple[float, float]],
    selected: int,
) -> list[int]:
    baseline_ink = _column_blank_ink_baseline(signals, selected)
    return [
        digit
        for digit, (ratio, ink) in signals.items()
        if digit != selected and _is_meaningful_roll_mark(ratio, ink, baseline_ink)
    ]


def _marked_roll_digits(signals: dict[int, tuple[float, float]]) -> list[int]:
    baseline_ink = _column_blank_ink_baseline(signals)
    return [
        digit
        for digit, (ratio, ink) in signals.items()
        if _is_meaningful_roll_mark(ratio, ink, baseline_ink, fill_floor=0.14)
    ]


def _roll_confidence(roll_no: str | None, flags: list[str]) -> str:
    if not roll_no:
        return "low"
    severe_terms = (
        "blank",
        "could not",
        "multiple filled",
        "too close",
        "only faint",
        "also has marks",
    )
    if any(any(term in flag for term in severe_terms) for flag in flags):
        return "low"
    if flags:
        return "medium"
    return "high"


def _cv2():
    try:
        import cv2
    except ImportError:
        return None
    return cv2


def _cluster_values(values: list[float], tolerance_mm: float) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if not groups or abs((sum(groups[-1]) / len(groups[-1])) - value) > tolerance_mm:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group) / len(group) for group in groups]


def _best_regular_run(clusters: list[float], count: int, pitch_mm: float) -> list[float] | None:
    if len(clusters) < count:
        return None
    clusters = sorted(clusters)
    best: tuple[float, list[float]] | None = None
    for start in range(0, len(clusters) - count + 1):
        run = clusters[start : start + count]
        pitch_error = sum(abs((run[i + 1] - run[i]) - pitch_mm) for i in range(count - 1))
        if best is None or pitch_error < best[0]:
            best = (pitch_error, run)
    if best is None:
        return None
    return best[1]


def _calibrate_digit_grid(gray: np.ndarray, manifest: dict, dpi: float, grid: dict) -> _GridCalibration | None:
    """Find actual printed circle centers for any manifest digit grid.

    The global page warp is anchored by the four fiducials, but phone photos
    and old printed copies can still leave a millimetre-scale local drift in
    a bubble block. Both roll grids and numerical grids in either orientation
    contain a regular array of printed circles, so use those outlines as local
    registration marks before deciding which digit is filled.
    """
    cv2 = _cv2()
    if cv2 is None:
        return None

    scale = px_per_mm(dpi)
    centers = digit_grid_centers_mm(grid)
    expected_mm = [centers[key] for key in sorted(centers)]
    min_x = min(point[0] for point in expected_mm)
    max_x = max(point[0] for point in expected_mm)
    min_y = min(point[1] for point in expected_mm)
    max_y = max(point[1] for point in expected_mm)
    x0, y0 = mm_to_px(min_x - 8.0, min_y - 5.0, dpi)
    x1, y1 = mm_to_px(max_x + 8.0, max_y + 7.0, dpi)
    x0, x1 = max(0, x0), min(gray.shape[1], x1)
    y0, y1 = max(0, y0), min(gray.shape[0], y1)
    if x0 >= x1 or y0 >= y1:
        return None

    roi = gray[y0:y1, x0:x1]
    _threshold, binary = cv2.threshold(roi, 210, 255, cv2.THRESH_BINARY_INV)
    contours, _hierarchy = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    points: list[tuple[float, float]] = []
    min_side = 2.4 * scale
    max_side = 5.4 * scale
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < 30:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if not (min_side <= w <= max_side and min_side <= h <= max_side):
            continue
        aspect = w / h if h else 0
        if not 0.55 <= aspect <= 1.8:
            continue
        moments = cv2.moments(contour)
        if moments["m00"]:
            cx = x0 + moments["m10"] / moments["m00"]
            cy = y0 + moments["m01"] / moments["m00"]
        else:
            cx = x0 + x + w / 2
            cy = y0 + y + h / 2
        points.append((cx, cy))

    min_matches = max(10, round(len(expected_mm) * 0.60))
    if len(points) < min_matches:
        return None

    expected_points = [(x * scale, y * scale) for x, y in expected_mm]
    transform = fit_candidate_local_transform(
        expected_points,
        points,
        max_distance_px=3.4 * scale,
        min_matches=min_matches,
        vote_tolerance_px=1.25 * scale,
        ransac_reproj_threshold_px=1.10 * scale,
    )
    if transform is not None:
        return _GridCalibration(
            transform=transform,
            matched_anchors=transform.matched_count,
            mean_residual_mm=transform.mean_residual_px / scale,
            max_residual_mm=transform.max_residual_px / scale,
        )

    horizontal = grid.get("orientation") == "horizontal"
    if "positions" in grid:
        x_count = 10 if horizontal else grid["positions"]
        y_count = grid["positions"] if horizontal else 10
        x_pitch = grid["digit_pitch_mm"] if horizontal else grid["position_pitch_mm"]
        y_pitch = grid["position_pitch_mm"] if horizontal else grid["digit_pitch_mm"]
    else:
        x_count, y_count = grid["columns"], 10
        x_pitch, y_pitch = grid["col_pitch_mm"], grid["row_pitch_mm"]
    x_clusters = _cluster_values([p[0] / scale for p in points], tolerance_mm=min(2.5, x_pitch * 0.4))
    y_clusters = _cluster_values([p[1] / scale for p in points], tolerance_mm=min(2.5, y_pitch * 0.4))
    x_centers = _best_regular_run(x_clusters, x_count, x_pitch)
    y_centers = _best_regular_run(y_clusters, y_count, y_pitch)
    if x_centers is None or y_centers is None:
        return None

    observed_points = [
        (x_centers[digit if horizontal else position] * scale,
         y_centers[position if horizontal else digit] * scale)
        for position, digit in sorted(centers)
    ]
    transform = fit_ordered_local_transform(
        expected_points,
        observed_points,
        min_points=3,
        ransac_reproj_threshold_px=1.6 * scale,
    )
    if transform is None:
        return None
    return _GridCalibration(
        transform=transform,
        matched_anchors=transform.matched_count,
        mean_residual_mm=transform.mean_residual_px / scale,
        max_residual_mm=transform.max_residual_px / scale,
    )


def _program_grid_keys(block: dict) -> dict[str, str]:
    """Return the explicit v5 mapping, with the v4 mapping as fallback."""
    return block.get("program_grid_keys", {"BTECH": "btech_digits", "MTECH": "mtech_digits"})


def _format_roll(program: str, digits: str) -> str:
    prefixes = {"BTECH": "", "MTECH": "MT", "PHD": "PHD"}
    return f"{prefixes[program]}{digits}"


def _read_program_selector(
    gray: np.ndarray,
    manifest: dict,
    dpi: float,
    calibrations: dict[str, _GridCalibration | None],
) -> tuple[str | None, dict[str, dict[str, float]], list[str]]:
    selector = manifest["roll_number_block"]["program_selector"]
    signals: dict[str, dict[str, float]] = {}
    flags: list[str] = []
    for program, coords in selector.items():
        x_mm, y_mm = coords["x_mm"], coords["y_mm"]
        calibration = calibrations.get(program)
        if calibration is not None:
            cx, cy = calibration.apply_mm(x_mm, y_mm, dpi)
            ratio, ink = _bubble_signal_px(gray, manifest, dpi, cx, cy)
        else:
            ratio, ink = _bubble_signal(gray, manifest, dpi, x_mm, y_mm)
        signals[program] = {"fill": ratio, "ink": ink}

    filled = [program for program, signal in signals.items() if signal["fill"] >= DEFAULT_FILL_THRESHOLD]
    marked = [program for program, signal in signals.items() if _is_marked(signal["fill"], signal["ink"])]
    if len(filled) == 1:
        return filled[0], signals, flags
    if len(filled) > 1:
        flags.append(f"multiple program bubbles filled: {', '.join(sorted(filled))}")
        return None, signals, flags
    if marked:
        flags.append(f"program selector is faint/ambiguous: {', '.join(sorted(marked))}")
    else:
        flags.append("program selector is blank")
    return None, signals, flags


def _read_digit_grid(
    gray: np.ndarray,
    manifest: dict,
    dpi: float,
    grid_key: str,
    label: str,
    calibration: _GridCalibration | None,
) -> tuple[str | None, int, dict[str, dict[str, float]], list[str]]:
    block = manifest["roll_number_block"]
    grid = block[grid_key]
    digits: list[str] = []
    valid_columns = 0
    ratios_by_col: dict[str, dict[str, float]] = {}
    flags: list[str] = []

    for col in range(grid["columns"]):
        col_label = str(col + 1)
        signals: dict[int, tuple[float, float]] = {}
        for digit in range(10):
            x_mm = grid["x_mm"] + col * grid["col_pitch_mm"]
            y_mm = grid["y_mm"] + digit * grid["row_pitch_mm"]
            if calibration is not None:
                cx, cy = calibration.apply_mm(x_mm, y_mm, dpi)
                signals[digit] = _bubble_signal_px(gray, manifest, dpi, cx, cy)
            else:
                signals[digit] = _bubble_signal(gray, manifest, dpi, x_mm, y_mm)
        ratios_by_col[col_label] = {str(digit): ratio for digit, (ratio, _ink) in signals.items()}

        filled = [digit for digit, (ratio, _ink) in signals.items() if ratio >= DEFAULT_FILL_THRESHOLD]
        ranked = sorted(signals, key=lambda digit: signals[digit][0], reverse=True)
        top = ranked[0]
        runner_up_ratio = signals[ranked[1]][0]
        margin = signals[top][0] - runner_up_ratio

        if len(filled) == 1:
            selected = filled[0]
            digits.append(str(selected))
            valid_columns += 1
            other_marks = _extra_roll_marks(signals, selected)
            if margin < DEFAULT_MIN_MARGIN:
                flags.append(
                    f"{label} roll column {col + 1} is too close to call "
                    f"(top margin {margin:.2f}, needs {DEFAULT_MIN_MARGIN:.2f})"
                )
            elif other_marks:
                flags.append(
                    f"{label} roll column {col + 1} has extra faint mark(s): "
                    f"{', '.join(str(d) for d in other_marks)}"
                )
            continue

        if len(filled) > 1:
            flags.append(f"{label} roll column {col + 1} has multiple filled digits: {filled}")
            digits.append("?")
            continue

        marked = _marked_roll_digits(signals)
        if len(marked) == 1:
            selected = marked[0]
            digits.append(str(selected))
            valid_columns += 1
            flags.append(f"{label} roll column {col + 1} accepted as faint digit {selected}")
        elif marked:
            flags.append(f"{label} roll column {col + 1} has only faint mark(s): {marked}")
            digits.append("?")
        else:
            flags.append(f"{label} roll column {col + 1} is blank")
            digits.append("?")

    roll_digits = "".join(digits)
    if "?" in roll_digits:
        return None, valid_columns, ratios_by_col, flags
    return roll_digits, valid_columns, ratios_by_col, flags


def roll_sample_centers(
    gray: np.ndarray,
    manifest: dict,
    dpi: float,
    page_index: int,
) -> dict[str, tuple[int, int]]:
    """Return the exact roll/program centers the identity reader will sample."""
    block = manifest["roll_number_block"]
    if block.get("page", 1) != page_index:
        return {}

    mapping = _program_grid_keys(block)
    calibrations = {
        grid_key: _calibrate_digit_grid(gray, manifest, dpi, block[grid_key])
        for grid_key in set(mapping.values())
    }
    centers: dict[str, tuple[int, int]] = {}

    for program, coords in block["program_selector"].items():
        calibration = calibrations.get(mapping[program])
        if calibration is not None:
            cx, cy = calibration.apply_mm(coords["x_mm"], coords["y_mm"], dpi)
            centers[f"program.{program}"] = (int(round(cx)), int(round(cy)))
        else:
            centers[f"program.{program}"] = mm_to_px(coords["x_mm"], coords["y_mm"], dpi)

    for grid_name in dict.fromkeys(mapping.values()):
        grid = block[grid_name]
        calibration = calibrations.get(grid_name)
        for col in range(grid["columns"]):
            for digit in range(10):
                x_mm = grid["x_mm"] + col * grid["col_pitch_mm"]
                y_mm = grid["y_mm"] + digit * grid["row_pitch_mm"]
                label = f"{grid_name}.{col + 1}.{digit}"
                if calibration is not None:
                    cx, cy = calibration.apply_mm(x_mm, y_mm, dpi)
                    centers[label] = (int(round(cx)), int(round(cy)))
                else:
                    centers[label] = mm_to_px(x_mm, y_mm, dpi)

    return centers


def read_roll_number(gray: np.ndarray, manifest: dict, dpi: float) -> RollRead:
    block = manifest["roll_number_block"]
    mapping = _program_grid_keys(block)
    grid_programs = {
        grid_key: [program for program, mapped_grid in mapping.items() if mapped_grid == grid_key]
        for grid_key in dict.fromkeys(mapping.values())
    }
    grid_calibrations = {
        grid_key: _calibrate_digit_grid(gray, manifest, dpi, block[grid_key])
        for grid_key in grid_programs
    }
    selector_calibrations = {
        program: grid_calibrations[grid_key] for program, grid_key in mapping.items()
    }
    selected_program, selector_ratios, flags = _read_program_selector(
        gray, manifest, dpi, selector_calibrations
    )

    grid_reads = {}
    for grid_key, programs in grid_programs.items():
        label = "/".join(programs)
        grid_reads[grid_key] = _read_digit_grid(
            gray, manifest, dpi, grid_key, label, grid_calibrations[grid_key]
        )

    program = selected_program
    roll_no: str | None = None
    ratios = {"program_selector": selector_ratios}
    for mapped_program, grid_key in mapping.items():
        ratios[mapped_program] = grid_reads[grid_key][2]

    if program is not None:
        selected_grid = mapping[program]
        digits, _valid, _grid_ratios, grid_flags = grid_reads[selected_grid]
        flags.extend(grid_flags)
        if digits is not None:
            roll_no = _format_roll(program, digits)
        for other_grid, (_digits, valid, _ratios, _flags) in grid_reads.items():
            if other_grid != selected_grid and valid:
                other_labels = "/".join(grid_programs[other_grid])
                flags.append(f"{program} selector is marked, but {other_labels} roll grid also has marks")
    else:
        completed = [grid_key for grid_key, read in grid_reads.items() if read[0] is not None]
        if len(completed) == 1:
            grid_key = completed[0]
            programs = grid_programs[grid_key]
            flags.extend(grid_reads[grid_key][3])
            if len(programs) == 1:
                program = programs[0]
                roll_no = _format_roll(program, grid_reads[grid_key][0])
                flags.append(f"program inferred as {program} from completed digit grid")
            else:
                flags.append(
                    f"completed digit grid is shared by {' and '.join(programs)}; "
                    "a program selector is required"
                )
        else:
            best_grid = max(grid_reads, key=lambda key: grid_reads[key][1])
            flags.extend(grid_reads[best_grid][3])
            flags.append("could not infer a single roll-number grid")

    if roll_no:
        roll_no = "".join(roll_no.upper().split())
    confidence = _roll_confidence(roll_no, flags)
    return RollRead(program=program, roll_no=roll_no, confidence=confidence, ratios=ratios, review_flags=flags)
