from __future__ import annotations

from pathlib import Path

import numpy as np
import pymupdf
import pytest
from PIL import Image, ImageDraw

from omr.contracts.geometry import mm_to_px, px_per_mm
from omr.generator.config import ExamConfig, WrittenQuestionConfig
from omr.generator.generate import generate_exam
from omr.reader.digit_model import train_digit_model
from omr.reader.handwriting import (
    RollOcrResult,
    _roster_suggestion_from_cell_probabilities,
    _relative_cell_ink,
    build_roll_ocr_backend,
    normalize_handwritten_roll_text,
    read_continuation_roll_number,
)


@pytest.mark.parametrize("offset", [10, 20, 30])
def test_shifted_box_frame_is_not_handwritten_ink(tmp_path, offset):
    path = tmp_path / "cell.png"
    image = Image.new("L", (100, 100), 245)
    ImageDraw.Draw(image).rectangle((offset, 12, 92, 87), outline=30, width=2)
    image.save(path)
    assert _relative_cell_ink(path) < .008


@pytest.mark.parametrize("digit", ["1", "7"])
def test_thin_faint_digits_are_not_removed_with_box_frame(tmp_path, digit):
    path = tmp_path / "cell.png"
    image = Image.new("L", (100, 100), 245)
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 12, 92, 87), outline=30, width=2)
    if digit == "7":
        draw.line((40, 35, 68, 35), fill=190, width=2)
        draw.line((68, 35, 45, 72), fill=190, width=2)
    else:
        draw.line((55, 33, 55, 72), fill=190, width=2)
    image.save(path)
    assert _relative_cell_ink(path) >= .008


def test_roster_probability_suggestion_is_review_only():
    rows = [
        {"2": 0.92, "3": 0.03},
        {"0": 0.94, "1": 0.02},
        {"2": 0.93, "3": 0.02},
        {"3": 0.91, "8": 0.03},
        {"0": 0.93, "8": 0.02},
        {"4": 0.92, "5": 0.02},
        {"6": 0.95, "8": 0.01},
    ]
    payload = {
        "BTECH": {
            "cells": {"raw": {"cells": [{"raw": {"class_probabilities": row}} for row in rows]}}
        }
    }

    suggestion = _roster_suggestion_from_cell_probabilities(payload, {"2023046", "2023858"})

    assert suggestion is not None
    assert suggestion["roll_no"] == "2023046"
    assert suggestion["policy"] == "review_only_literal_ocr_unchanged"


DPI = 200


class FakeOcr:
    provider = "fake"

    def __init__(self, text: str, confidence: float = 0.93, digits: str | None = None) -> None:
        self.text = text
        self.confidence = confidence
        self.digits = list(digits or text)

    def read_roll(self, crop_path: Path, program: str | None = None, valid_rolls: set[str] | None = None) -> RollOcrResult:
        return RollOcrResult(self.text, self.confidence)

    def read_digit(self, crop_path: Path) -> RollOcrResult:
        digit = self.digits.pop(0) if self.digits else ""
        return RollOcrResult(digit, self.confidence)


def _sheet(tmp_path: Path, *, mark_roll_cells: bool = True) -> tuple[dict, Image.Image]:
    result = generate_exam(
        ExamConfig(
            exam_id="HANDWRITING_TEST",
            course_code="CSE202",
            exam_name="Endsem",
            exam_type="endsem",
            num_mcq=10,
            mcq_options=4,
            marks_per_mcq=1,
            written_questions=[WrittenQuestionConfig(q_no=11 + i, max_marks=2, lines=2) for i in range(8)],
        ),
        tmp_path / "exam",
    )
    with pymupdf.open(result["pdf_path"]) as doc:
        page = doc[1]
        pix = page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
    image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    if mark_roll_cells:
        draw = ImageDraw.Draw(image)
        for field in result["manifest"]["write_in_fields"]:
            if field["page"] != 2 or field["name"] != "roll_number":
                continue
            for index in range(int(field["cells"])):
                cx, cy = mm_to_px(
                    field["x_mm"] + index * field["cell_pitch_mm"] + field["cell_width_mm"] / 2,
                    field["y_mm"] + field["height_mm"] / 2,
                    DPI,
                )
                draw.ellipse([cx - 2, cy - 2, cx + 2, cy + 2], fill=0)
    return result["manifest"], image


def _fill_continuation_program(draw: ImageDraw.ImageDraw, manifest: dict, program: str) -> None:
    choice = next(
        item for item in manifest["continuation_program_choices"] if item["page"] == 2 and item["program"] == program
    )
    cx, cy = mm_to_px(choice["x_mm"], choice["y_mm"], DPI)
    radius = manifest["bubble_sample_radius_mm"] * px_per_mm(DPI) * 1.05
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=0)


def test_handwritten_roll_normalization_is_format_aware():
    assert normalize_handwritten_roll_text(" 2024 587 ", "BTECH") == "2024587"
    assert normalize_handwritten_roll_text("MT 12345", "MTECH") == "MT12345"
    assert normalize_handwritten_roll_text("12345", "MTECH") == "MT12345"
    assert normalize_handwritten_roll_text("PhD 20301", "PHD") == "PHD20301"
    assert normalize_handwritten_roll_text("20301", "PHD") == "PHD20301"
    assert normalize_handwritten_roll_text("SP24ABC", "BTECH") == "SP24ABC"
    assert normalize_handwritten_roll_text("sp24-001") == "SP24001"
    assert normalize_handwritten_roll_text("20O45B7", "BTECH") == "2004587"
    assert normalize_handwritten_roll_text("12345", "BTECH") is None


def test_no_ocr_backend_is_default():
    assert build_roll_ocr_backend("none") is None


def test_local_digit_model_backend_reads_isolated_digit(tmp_path: Path):
    labels_csv = tmp_path / "labels.csv"
    model_path = tmp_path / "digit_model.npz"
    rows = ["image_path,label"]
    for index in range(10):
        image_path = tmp_path / f"seven_{index}.png"
        image = Image.new("L", (48, 56), 255)
        draw = ImageDraw.Draw(image)
        dx = (index % 3) - 1
        dy = (index % 2) - 1
        draw.line([(10 + dx, 12 + dy), (35 + dx, 12 + dy), (18 + dx, 42 + dy)], fill=0, width=5)
        image.save(image_path)
        rows.append(f"{image_path.name},7")
    labels_csv.write_text("\n".join(rows), encoding="utf-8")

    train_digit_model(labels_csv, model_path)
    backend = build_roll_ocr_backend("local", digit_model_path=model_path)

    assert backend is not None
    assert backend.read_digit(tmp_path / "seven_0.png").text == "7"  # type: ignore[attr-defined]


def test_continuation_roll_read_saves_crop_and_validates_ocr(tmp_path: Path):
    manifest, page = _sheet(tmp_path)
    draw = ImageDraw.Draw(page)
    _fill_continuation_program(draw, manifest, "BTECH")

    result = read_continuation_roll_number(
        np.asarray(page),
        manifest,
        DPI,
        2,
        tmp_path / "identity",
        ocr_backend=FakeOcr("2024587", digits="2024587"),
    )

    assert result.roll_no == "2024587"
    assert result.program == "BTECH"
    assert result.confidence == "high"
    assert Path(result.crop_paths["BTECH"]).exists()
    assert len(result.cell_crop_paths["BTECH"]) == 7
    assert all(Path(path).exists() for path in result.cell_crop_paths["BTECH"])


def test_phd_continuation_roll_uses_shared_five_cell_crop(tmp_path: Path):
    manifest, page = _sheet(tmp_path)
    _fill_continuation_program(ImageDraw.Draw(page), manifest, "PHD")

    result = read_continuation_roll_number(
        np.asarray(page),
        manifest,
        DPI,
        2,
        tmp_path / "identity",
        ocr_backend=FakeOcr("PHD20301", digits="20301"),
    )

    assert result.roll_no == "PHD20301"
    assert result.program == "PHD"
    assert result.confidence == "high"
    assert result.crop_paths["PHD"] == result.crop_paths["MTECH"]
    assert len(result.cell_crop_paths["PHD"]) == 5


def test_strip_disagreement_is_diagnostic_when_cells_are_valid(tmp_path: Path):
    manifest, page = _sheet(tmp_path)
    draw = ImageDraw.Draw(page)
    _fill_continuation_program(draw, manifest, "BTECH")

    result = read_continuation_roll_number(
        np.asarray(page),
        manifest,
        DPI,
        2,
        tmp_path / "identity",
        ocr_backend=FakeOcr("9999999", digits="2024587"),
    )

    assert result.roll_no == "2024587"
    assert result.confidence == "high"
    assert any(flag.startswith("STRIP_DISAGREES") for flag in result.review_flags)


def test_blank_continuation_selector_reads_btech_field_only(tmp_path: Path):
    manifest, page = _sheet(tmp_path)

    result = read_continuation_roll_number(
        np.asarray(page),
        manifest,
        DPI,
        2,
        tmp_path / "identity",
        ocr_backend=FakeOcr("2024587", digits="2024587"),
    )

    assert result.program == "BTECH"
    assert result.roll_no == "2024587"
    assert {key for key in result.ocr_results if not key.startswith("_")} == {"BTECH"}
    assert any("read BTECH field only" in flag for flag in result.review_flags)


def test_empty_roll_field_is_not_sent_to_resnet_candidates(tmp_path: Path):
    manifest, page = _sheet(tmp_path, mark_roll_cells=False)
    draw = ImageDraw.Draw(page)
    _fill_continuation_program(draw, manifest, "BTECH")

    result = read_continuation_roll_number(
        np.asarray(page),
        manifest,
        DPI,
        2,
        tmp_path / "identity",
        ocr_backend=FakeOcr("2024587", digits="2024587"),
    )

    assert result.roll_no is None
    assert any(flag.startswith("EMPTY_FIELD") for flag in result.review_flags)


def test_whole_strip_cannot_establish_identity_when_cells_are_incomplete(tmp_path: Path):
    manifest, page = _sheet(tmp_path)
    draw = ImageDraw.Draw(page)
    _fill_continuation_program(draw, manifest, "BTECH")

    result = read_continuation_roll_number(
        np.asarray(page),
        manifest,
        DPI,
        2,
        tmp_path / "identity",
        ocr_backend=FakeOcr("SP24ABC", digits="???????"),
        valid_rolls={"SP24ABC"},
    )

    assert result.roll_no is None
    assert result.confidence == "low"
    assert result.ocr_results["BTECH"]["strip"]["text"] == "SP24ABC"
    assert any("whole-strip OCR" in flag for flag in result.review_flags)


def test_handwritten_roll_not_in_roster_is_preserved_for_review(tmp_path: Path):
    manifest, page = _sheet(tmp_path)
    draw = ImageDraw.Draw(page)
    _fill_continuation_program(draw, manifest, "BTECH")

    result = read_continuation_roll_number(
        np.asarray(page),
        manifest,
        DPI,
        2,
        tmp_path / "identity",
        ocr_backend=FakeOcr("9999999", digits="9999999"),
        valid_rolls={"2024587"},
    )

    assert result.roll_no == "9999999"
    assert result.confidence == "low"
    assert any("roster" in flag for flag in result.review_flags)
