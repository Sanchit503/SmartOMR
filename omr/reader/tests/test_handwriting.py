from __future__ import annotations

from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageDraw

from omr.contracts.geometry import mm_to_px, px_per_mm
from omr.generator.config import ExamConfig, WrittenQuestionConfig
from omr.generator.generate import generate_exam
from omr.reader.digit_model import train_digit_model
from omr.reader.handwriting import (
    RollOcrResult,
    build_roll_ocr_backend,
    normalize_handwritten_roll_text,
    read_continuation_roll_number,
)


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


def _sheet(tmp_path: Path) -> tuple[dict, Image.Image]:
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
    return result["manifest"], Image.frombytes("L", (pix.width, pix.height), pix.samples)


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


def test_conflicting_handwriting_ocr_strategies_are_rejected(tmp_path: Path):
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

    assert result.roll_no is None
    assert result.confidence == "low"
    assert any("conflicting candidates" in flag for flag in result.review_flags)


def test_sp_roll_can_be_read_from_whole_strip_when_in_roster(tmp_path: Path):
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

    assert result.roll_no == "SP24ABC"
    assert result.confidence == "medium"
    assert any("whole-strip OCR" in flag for flag in result.review_flags)


def test_handwritten_roll_not_in_roster_is_rejected(tmp_path: Path):
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

    assert result.roll_no is None
    assert result.confidence == "low"
    assert any("roster" in flag for flag in result.review_flags)
