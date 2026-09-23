from __future__ import annotations

from pathlib import Path

import numpy as np
import pymupdf
import pytest
from PIL import Image, ImageDraw

from omr.contracts.geometry import digit_grid_centers_mm, mm_to_px, px_per_mm
from omr.contracts.manifest import load_manifest
from omr.generator.config import ExamConfig, NumericalQuestionConfig
from omr.generator.generate import generate_exam
from omr.reader.numerical import read_numerical_responses


DPI = 200


def _sheet(tmp_path: Path, orientation: str) -> tuple[dict, Image.Image]:
    if orientation == "horizontal":
        fixture = Path(__file__).parent / "fixtures" / "numerical_v5" / "NUMERICAL_V5"
        with pymupdf.open(fixture.with_suffix(".pdf")) as document:
            pix = document[0].get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
        return load_manifest(fixture.with_suffix(".manifest.json")), Image.frombytes(
            "L", (pix.width, pix.height), pix.samples,
        )
    result = generate_exam(
        ExamConfig(
            exam_id="NUMERICAL_READER_TEST",
            course_code="CSE202",
            exam_name="Numerical Quiz",
            exam_type="quiz",
            num_mcq=0,
            numerical_questions=[
                NumericalQuestionConfig(q_no=1, max_marks=2, digits=3),
                NumericalQuestionConfig(q_no=2, max_marks=2, digits=3),
            ],
        ),
        tmp_path / "exam",
    )
    with pymupdf.open(result["pdf_path"]) as document:
        pix = document[0].get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
    return result["manifest"], Image.frombytes("L", (pix.width, pix.height), pix.samples)


def _fill(draw: ImageDraw.ImageDraw, manifest: dict, x_mm: float, y_mm: float) -> None:
    cx, cy = mm_to_px(x_mm, y_mm, DPI)
    radius = manifest["bubble_sample_radius_mm"] * px_per_mm(DPI) * 1.08
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=0)


def _fill_answer(draw: ImageDraw.ImageDraw, manifest: dict, q_no: int, digits: str) -> None:
    entry = next(item for item in manifest["numerical_block"] if item["q_no"] == q_no)
    centers = digit_grid_centers_mm(entry)
    for position, digit in enumerate(digits):
        _fill(draw, manifest, *centers[(position, int(digit))])


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
def test_reads_numerical_grid_with_leading_zeros(tmp_path: Path, orientation):
    manifest, page = _sheet(tmp_path, orientation)
    draw = ImageDraw.Draw(page)
    _fill_answer(draw, manifest, 1, "007")

    readings = read_numerical_responses({1: np.asarray(page)}, manifest, DPI)

    assert readings[0].outcome == "answered"
    assert readings[0].digits_text == "007"
    assert readings[0].value == 7
    assert readings[0].confidence == "high"
    assert not readings[0].needs_human_review
    assert readings[1].outcome == "blank"
    assert readings[1].value is None
    assert not readings[1].needs_human_review


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
def test_incomplete_numerical_grid_is_sent_to_review(tmp_path: Path, orientation):
    manifest, page = _sheet(tmp_path, orientation)
    entry = manifest["numerical_block"][0]
    draw = ImageDraw.Draw(page)
    _fill(draw, manifest, *digit_grid_centers_mm(entry)[(0, 4)])

    reading = read_numerical_responses({1: np.asarray(page)}, manifest, DPI)[0]

    assert reading.outcome == "incomplete"
    assert reading.value is None
    assert reading.needs_human_review
    assert any("leading zeros" in flag for flag in reading.review_flags)


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
def test_multiple_digits_in_one_position_are_not_guessed(tmp_path: Path, orientation):
    manifest, page = _sheet(tmp_path, orientation)
    entry = manifest["numerical_block"][0]
    centers = digit_grid_centers_mm(entry)
    draw = ImageDraw.Draw(page)
    _fill(draw, manifest, *centers[(0, 1)])
    _fill(draw, manifest, *centers[(0, 2)])
    _fill(draw, manifest, *centers[(1, 3)])
    _fill(draw, manifest, *centers[(2, 4)])

    reading = read_numerical_responses({1: np.asarray(page)}, manifest, DPI)[0]

    assert reading.outcome == "multiple"
    assert reading.value is None
    assert reading.needs_human_review
    assert any("above the fill threshold" in flag for flag in reading.review_flags)


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
def test_grid_local_registration_handles_two_mm_drift(tmp_path: Path, orientation):
    manifest, page = _sheet(tmp_path, orientation)
    _fill_answer(ImageDraw.Draw(page), manifest, 1, "907")
    shift = round(2.0 * px_per_mm(DPI))
    shifted = Image.new("L", page.size, 255)
    shifted.paste(page, (shift, shift))

    reading = read_numerical_responses({1: np.asarray(shifted)}, manifest, DPI)[0]

    assert reading.outcome == "answered"
    assert reading.digits_text == "907"
    assert reading.value == 907
    assert not reading.needs_human_review


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
def test_regular_grid_calibration_fallback_supports_both_orientations(tmp_path, monkeypatch, orientation):
    from omr.reader import identity

    manifest, page = _sheet(tmp_path, orientation)
    _fill_answer(ImageDraw.Draw(page), manifest, 1, "583")
    monkeypatch.setattr(identity, "fit_candidate_local_transform", lambda *args, **kwargs: None)
    calibration = identity._calibrate_digit_grid(np.asarray(page), manifest, DPI, manifest["numerical_block"][0])
    assert calibration is not None
    reading = read_numerical_responses({1: np.asarray(page)}, manifest, DPI)[0]
    assert reading.digits_text == "583"
    assert not reading.needs_human_review


@pytest.mark.parametrize("orientation", ["horizontal", "vertical"])
def test_simulation_verifier_keeps_legacy_and_current_sheets_readable(tmp_path, orientation):
    from omr.verify import verify_sheet

    _sheet(tmp_path, orientation)
    path = (Path(__file__).parent / "fixtures/numerical_v5/NUMERICAL_V5.manifest.json"
            if orientation == "horizontal" else tmp_path / "exam/NUMERICAL_READER_TEST.manifest.json")
    result = verify_sheet(path)
    assert result.ok, result.format()
    assert result.recovered == 2
