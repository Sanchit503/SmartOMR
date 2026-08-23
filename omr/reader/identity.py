"""Roll-number and program-bubble reading from a canonical page image.

Identity resolution remains roster-based; this module only decodes the sheet's
declared program and roll number, with review flags whenever the evidence is
ambiguous.
"""
from __future__ import annotations

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


def _bubble_signal(gray: np.ndarray, manifest: dict, dpi: float, x_mm: float, y_mm: float) -> tuple[float, float]:
    radius_px = max(1, round(manifest["bubble_sample_radius_mm"] * px_per_mm(dpi)))
    cx, cy = mm_to_px(x_mm, y_mm, dpi)
    return fill_ratio(gray, cx, cy, radius_px), ink_density(gray, cx, cy, radius_px)


def _is_marked(ratio: float, ink: float) -> bool:
    return ratio >= DEFAULT_AMBIGUOUS_FLOOR or ink >= DEFAULT_INK_FLOOR


def _read_program_selector(gray: np.ndarray, manifest: dict, dpi: float) -> tuple[str | None, dict[str, dict[str, float]], list[str]]:
    selector = manifest["roll_number_block"]["program_selector"]
    signals: dict[str, dict[str, float]] = {}
    flags: list[str] = []
    for program, coords in selector.items():
        ratio, ink = _bubble_signal(gray, manifest, dpi, coords["x_mm"], coords["y_mm"])
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
        if marked:
            flags.append(f"{program} roll column {col + 1} has only faint mark(s): {marked}")
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
    selected_program, selector_ratios, flags = _read_program_selector(gray, manifest, dpi)
    btech_roll, btech_valid, btech_ratios, btech_flags = _read_digit_grid(gray, manifest, dpi, "BTECH")
    mtech_roll, mtech_valid, mtech_ratios, mtech_flags = _read_digit_grid(gray, manifest, dpi, "MTECH")

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
