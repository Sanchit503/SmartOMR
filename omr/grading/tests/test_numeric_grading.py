from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from omr.contracts.geometry import canonical_size_px
from omr.generator.config import ExamConfig
from omr.generator.layout import build_layout
from omr.generator.manifest import build_manifest
from omr.grading.numeric import NumericOutcome, read_numeric_responses


DPI = 200


def _numeric_config() -> ExamConfig:
    return ExamConfig(
        exam_id="NUMERIC_QUIZ",
        course_code="CSE222",
        exam_name="Numeric Quiz",
        exam_type="quiz",
        num_mcq=0,
        num_numeric=3,
        numeric_digits=2,
        marks_per_numeric=1,
        written_questions=[],
    )


def _blank_pages(manifest: dict) -> dict[int, Image.Image]:
    w_px, h_px = canonical_size_px(manifest, DPI)
    return {page: Image.new("L", (w_px, h_px), 255) for page in range(1, manifest["num_pages"] + 1)}


def _fill_digit(draw: ImageDraw.ImageDraw, manifest: dict, entry: dict, place_index: int, digit: int) -> None:
    scale = DPI / 25.4
    cx = (entry["x_mm"] + manifest["numeric_label_offset_mm"] + digit * manifest["numeric_digit_pitch_mm"]) * scale
    cy = (entry["y_mm"] + place_index * manifest["numeric_place_row_pitch_mm"]) * scale
    radius = manifest["bubble_radius_mm"] * scale * 0.82
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=0)


def test_numeric_grading_reads_two_digit_answers():
    manifest = build_manifest(build_layout(_numeric_config()))
    pages = _blank_pages(manifest)
    answers = {1: "07", 2: "14", 3: "99"}

    for entry in manifest["numeric_block"]:
        draw = ImageDraw.Draw(pages[entry["page"]])
        answer = answers[entry["q_no"]]
        for place_index, digit in enumerate(answer):
            _fill_digit(draw, manifest, entry, place_index, int(digit))

    readings = read_numeric_responses({page: np.asarray(image) for page, image in pages.items()}, manifest, DPI)

    assert {reading.q_no: reading.answer for reading in readings} == answers
    assert all(reading.outcome == NumericOutcome.ANSWERED for reading in readings)
    assert all(not reading.needs_human_review for reading in readings)
