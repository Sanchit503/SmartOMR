from __future__ import annotations

from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageDraw

from omr.contracts.geometry import mm_to_px, px_per_mm
from omr.generator.config import ExamConfig
from omr.generator.generate import generate_exam
from omr.reader.identity import read_roll_number


DPI = 200


def _page(tmp_path: Path) -> tuple[dict, Image.Image]:
    result = generate_exam(
        ExamConfig(
            exam_id="IDENTITY_TEST",
            course_code="CSE202",
            exam_name="Quiz - 1",
            exam_type="quiz",
            num_mcq=4,
            mcq_options=4,
            marks_per_mcq=1,
            written_questions=[],
        ),
        tmp_path / "exam",
    )
    with pymupdf.open(result["pdf_path"]) as doc:
        pix = doc[0].get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
    return result["manifest"], Image.frombytes("L", (pix.width, pix.height), pix.samples)


def _fill_bubble(draw: ImageDraw.ImageDraw, manifest: dict, x_mm: float, y_mm: float, scale: float = 1.08) -> None:
    cx, cy = mm_to_px(x_mm, y_mm, DPI)
    radius = manifest["bubble_sample_radius_mm"] * px_per_mm(DPI) * scale
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=0)


def _fill_btech_roll(draw: ImageDraw.ImageDraw, manifest: dict, roll_no: str) -> None:
    block = manifest["roll_number_block"]
    selector = block["program_selector"]["BTECH"]
    _fill_bubble(draw, manifest, selector["x_mm"], selector["y_mm"])
    grid = block["btech_digits"]
    for col, digit_char in enumerate(roll_no):
        x_mm = grid["x_mm"] + col * grid["col_pitch_mm"]
        y_mm = grid["y_mm"] + int(digit_char) * grid["row_pitch_mm"]
        _fill_bubble(draw, manifest, x_mm, y_mm)


def _fill_postgraduate_roll(draw: ImageDraw.ImageDraw, manifest: dict, program: str, digits: str) -> None:
    block = manifest["roll_number_block"]
    selector = block["program_selector"][program]
    _fill_bubble(draw, manifest, selector["x_mm"], selector["y_mm"])
    grid = block[block["program_grid_keys"][program]]
    for col, digit_char in enumerate(digits):
        _fill_bubble(
            draw,
            manifest,
            grid["x_mm"] + col * grid["col_pitch_mm"],
            grid["y_mm"] + int(digit_char) * grid["row_pitch_mm"],
        )


def _add_tiny_roll_noise(draw: ImageDraw.ImageDraw, manifest: dict, col: int, digit: int) -> None:
    grid = manifest["roll_number_block"]["btech_digits"]
    cx, cy = mm_to_px(
        grid["x_mm"] + col * grid["col_pitch_mm"],
        grid["y_mm"] + digit * grid["row_pitch_mm"],
        DPI,
    )
    radius = max(1, round(manifest["bubble_sample_radius_mm"] * px_per_mm(DPI) * 0.18))
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=25)


def test_clear_roll_ignores_tiny_extra_noise(tmp_path: Path):
    manifest, page = _page(tmp_path)
    draw = ImageDraw.Draw(page)
    _fill_btech_roll(draw, manifest, "2024587")
    for col, digits in {2: [0], 3: [0, 1], 4: [0, 1, 2], 5: [0, 1, 2], 6: [0, 1, 2]}.items():
        for digit in digits:
            _add_tiny_roll_noise(draw, manifest, col, digit)

    roll = read_roll_number(np.asarray(page), manifest, DPI)

    assert roll.roll_no == "2024587"
    assert roll.program == "BTECH"
    assert roll.confidence == "high"
    assert not any("extra faint mark" in flag for flag in roll.review_flags)


def test_double_filled_roll_column_still_needs_review(tmp_path: Path):
    manifest, page = _page(tmp_path)
    draw = ImageDraw.Draw(page)
    _fill_btech_roll(draw, manifest, "2024587")
    grid = manifest["roll_number_block"]["btech_digits"]
    _fill_bubble(draw, manifest, grid["x_mm"] + grid["col_pitch_mm"], grid["y_mm"] + grid["row_pitch_mm"])

    roll = read_roll_number(np.asarray(page), manifest, DPI)

    assert roll.confidence == "low"
    assert any("multiple filled digits" in flag for flag in roll.review_flags)


def test_phd_selector_reads_shared_five_digit_grid(tmp_path: Path):
    manifest, page = _page(tmp_path)
    _fill_postgraduate_roll(ImageDraw.Draw(page), manifest, "PHD", "20301")

    roll = read_roll_number(np.asarray(page), manifest, DPI)

    assert roll.program == "PHD"
    assert roll.roll_no == "PHD20301"
    assert roll.confidence == "high"


def test_shared_grid_without_program_selector_is_not_guessed(tmp_path: Path):
    manifest, page = _page(tmp_path)
    block = manifest["roll_number_block"]
    grid = block[block["program_grid_keys"]["PHD"]]
    draw = ImageDraw.Draw(page)
    for col, digit_char in enumerate("20301"):
        _fill_bubble(
            draw,
            manifest,
            grid["x_mm"] + col * grid["col_pitch_mm"],
            grid["y_mm"] + int(digit_char) * grid["row_pitch_mm"],
        )

    roll = read_roll_number(np.asarray(page), manifest, DPI)

    assert roll.program is None
    assert roll.roll_no is None
    assert roll.confidence == "low"
    assert any("shared by MTECH and PHD" in flag for flag in roll.review_flags)
