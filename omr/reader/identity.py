"""Roll-number and program-bubble reading from a canonical page image.

Identity resolution remains roster-based; this module only decodes the sheet's
declared program and roll number, with review flags whenever the evidence is
ambiguous.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from omr.contracts.geometry import mm_to_px, px_per_mm
from omr.grading.bubbles import fill_ratio, ink_density
from omr.grading.mcq import (
    DEFAULT_AMBIGUOUS_FLOOR,
    DEFAULT_FILL_THRESHOLD,
    DEFAULT_INK_FLOOR,
    DEFAULT_MIN_MARGIN,
)

from omr.models import RollRead


@dataclass(frozen=True)
class _GridCalibration:
    x_centers_mm: list[float]
    y_centers_mm: list[float]


def _bubble_signal(gray: np.ndarray, manifest: dict, dpi: float, x_mm: float, y_mm: float) -> tuple[float, float]:
    radius_px = max(1, round(manifest["bubble_sample_radius_mm"] * px_per_mm(dpi)))
    cx, cy = mm_to_px(x_mm, y_mm, dpi)
    return fill_ratio(gray, cx, cy, radius_px), ink_density(gray, cx, cy, radius_px)


def _is_marked(ratio: float, ink: float) -> bool:
    return ratio >= DEFAULT_AMBIGUOUS_FLOOR or ink >= DEFAULT_INK_FLOOR


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
    """Find the actual printed circle centers for a roll-number grid.

    The global page warp is anchored by the four fiducials, but phone photos
    and old printed copies can still leave a millimetre-scale local drift in
    the identity block. The roll grid itself contains a clean 10 x N array of
    printed circles, so use those outlines as local registration marks before
    deciding which digit is filled.
    """
    cv2 = _cv2()
    if cv2 is None:
        return None

    scale = px_per_mm(dpi)
    x0, y0 = mm_to_px(grid["x_mm"] - 5.0, grid["y_mm"] - 7.0, dpi)
    x1, y1 = mm_to_px(
        grid["x_mm"] + (grid["columns"] - 1) * grid["col_pitch_mm"] + 5.0,
        grid["y_mm"] + 9 * grid["row_pitch_mm"] + 7.0,
        dpi,
    )
    x0, x1 = max(0, x0), min(gray.shape[1], x1)
    y0, y1 = max(0, y0), min(gray.shape[0], y1)
    if x0 >= x1 or y0 >= y1:
        return None

    roi = gray[y0:y1, x0:x1]
    _threshold, binary = cv2.threshold(roi, 210, 255, cv2.THRESH_BINARY_INV)
    contours, _hierarchy = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    points: list[tuple[float, float]] = []
    min_side = 2.4 * scale
    max_side = 6.2 * scale
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
        points.append((cx / scale, cy / scale))

    if len(points) < grid["columns"] * 8:
        return None

    x_clusters = _cluster_values([p[0] for p in points], tolerance_mm=2.5)
    y_clusters = _cluster_values([p[1] for p in points], tolerance_mm=2.5)
    x_centers = _best_regular_run(x_clusters, grid["columns"], grid["col_pitch_mm"])
    y_centers = _best_regular_run(y_clusters, 10, grid["row_pitch_mm"])
    if x_centers is None or y_centers is None:
        return None
    return _GridCalibration(x_centers_mm=x_centers, y_centers_mm=y_centers)


def _read_program_selector(
    gray: np.ndarray,
    manifest: dict,
    dpi: float,
    calibrations: dict[str, _GridCalibration | None],
) -> tuple[str | None, dict[str, dict[str, float]], list[str]]:
    selector = manifest["roll_number_block"]["program_selector"]
    block = manifest["roll_number_block"]
    signals: dict[str, dict[str, float]] = {}
    flags: list[str] = []
    for program, coords in selector.items():
        x_mm, y_mm = coords["x_mm"], coords["y_mm"]
        calibration = calibrations.get(program)
        if calibration is not None:
            grid_key = "btech_digits" if program == "BTECH" else "mtech_digits"
            grid = block[grid_key]
            x_mm += calibration.x_centers_mm[0] - grid["x_mm"]
            y_mm += calibration.y_centers_mm[0] - grid["y_mm"]
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
    program: str,
    calibration: _GridCalibration | None,
) -> tuple[str | None, int, dict[str, dict[str, float]], list[str]]:
    block = manifest["roll_number_block"]
    grid_key = "btech_digits" if program == "BTECH" else "mtech_digits"
    grid = block[grid_key]
    digits: list[str] = []
    valid_columns = 0
    ratios_by_col: dict[str, dict[str, float]] = {}
    flags: list[str] = []

    for col in range(grid["columns"]):
        col_label = str(col + 1)
        signals: dict[int, tuple[float, float]] = {}
        for digit in range(10):
            if calibration is not None:
                x_mm = calibration.x_centers_mm[col]
                y_mm = calibration.y_centers_mm[digit]
            else:
                x_mm = grid["x_mm"] + col * grid["col_pitch_mm"]
                y_mm = grid["y_mm"] + digit * grid["row_pitch_mm"]
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
            other_marks = [
                digit
                for digit, (ratio, ink) in signals.items()
                if digit != selected and _is_marked(ratio, ink)
            ]
            if margin < DEFAULT_MIN_MARGIN:
                flags.append(
                    f"{program} roll column {col + 1} is too close to call "
                    f"(top margin {margin:.2f}, needs {DEFAULT_MIN_MARGIN:.2f})"
                )
            elif other_marks:
                flags.append(
                    f"{program} roll column {col + 1} has extra faint mark(s): "
                    f"{', '.join(str(d) for d in other_marks)}"
                )
            continue

        if len(filled) > 1:
            flags.append(f"{program} roll column {col + 1} has multiple filled digits: {filled}")
            digits.append("?")
            continue

        marked = [digit for digit, (ratio, ink) in signals.items() if _is_marked(ratio, ink)]
        if len(marked) == 1:
            selected = marked[0]
            digits.append(str(selected))
            valid_columns += 1
            flags.append(f"{program} roll column {col + 1} accepted as faint digit {selected}")
        elif marked:
            flags.append(f"{program} roll column {col + 1} has only faint mark(s): {marked}")
            digits.append("?")
        else:
            flags.append(f"{program} roll column {col + 1} is blank")
            digits.append("?")

    roll_digits = "".join(digits)
    if "?" in roll_digits:
        return None, valid_columns, ratios_by_col, flags
    if program == "MTECH":
        return f"MT{roll_digits}", valid_columns, ratios_by_col, flags
    return roll_digits, valid_columns, ratios_by_col, flags


def read_roll_number(gray: np.ndarray, manifest: dict, dpi: float) -> RollRead:
    block = manifest["roll_number_block"]
    calibrations = {
        "BTECH": _calibrate_digit_grid(gray, manifest, dpi, block["btech_digits"]),
        "MTECH": _calibrate_digit_grid(gray, manifest, dpi, block["mtech_digits"]),
    }
    selected_program, selector_ratios, flags = _read_program_selector(gray, manifest, dpi, calibrations)
    btech_roll, btech_valid, btech_ratios, btech_flags = _read_digit_grid(
        gray, manifest, dpi, "BTECH", calibrations["BTECH"]
    )
    mtech_roll, mtech_valid, mtech_ratios, mtech_flags = _read_digit_grid(
        gray, manifest, dpi, "MTECH", calibrations["MTECH"]
    )

    program = selected_program
    roll_no: str | None = None
    ratios = {
        "program_selector": selector_ratios,
        "BTECH": btech_ratios,
        "MTECH": mtech_ratios,
    }

    if program == "BTECH":
        roll_no = btech_roll
        flags.extend(btech_flags)
        if mtech_valid:
            flags.append("BTECH selector is marked, but MTECH roll grid also has marks")
    elif program == "MTECH":
        roll_no = mtech_roll
        flags.extend(mtech_flags)
        if btech_valid:
            flags.append("MTECH selector is marked, but BTECH roll grid also has marks")
    else:
        if btech_roll and not mtech_valid:
            program = "BTECH"
            roll_no = btech_roll
            flags.append("program inferred as BTECH from completed digit grid")
            flags.extend(btech_flags)
        elif mtech_roll and not btech_valid:
            program = "MTECH"
            roll_no = mtech_roll
            flags.append("program inferred as MTECH from completed digit grid")
            flags.extend(mtech_flags)
        else:
            flags.extend(btech_flags if btech_valid >= mtech_valid else mtech_flags)
            flags.append("could not infer a single roll-number grid")

    if roll_no:
        roll_no = "".join(roll_no.upper().split())
    confidence = "high" if not flags else "low"
    return RollRead(program=program, roll_no=roll_no, confidence=confidence, ratios=ratios, review_flags=flags)
