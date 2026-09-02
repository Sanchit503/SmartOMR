from __future__ import annotations

import json
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

from omr.contracts.geometry import mm_to_px, px_per_mm
from omr.generator.config import ExamConfig, WrittenQuestionConfig
from omr.generator.generate import generate_exam
from omr.reader.handwriting import RollOcrResult
from omr.workflows.batch import parse_exam_bundle


DPI = 200


class FakeOcr:
    provider = "fake"

    def __init__(self, digits: str = "20245872024587") -> None:
        self.digits = list(digits)

    def read_roll(self, crop_path: Path, program: str | None = None) -> RollOcrResult:
        return RollOcrResult("2024587", confidence=0.95)

    def read_digit(self, crop_path: Path) -> RollOcrResult:
        digit = self.digits.pop(0) if self.digits else ""
        return RollOcrResult(digit, confidence=0.95)


def _render_pages(pdf_path: Path) -> dict[int, Image.Image]:
    pages = {}
    with pymupdf.open(pdf_path) as doc:
        for index, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
            pages[index] = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    return pages


def _fill_bubble(draw: ImageDraw.ImageDraw, manifest: dict, x_mm: float, y_mm: float) -> None:
    cx, cy = mm_to_px(x_mm, y_mm, DPI)
    radius = manifest["bubble_sample_radius_mm"] * px_per_mm(DPI) * 1.08
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=0)


def _fill_btech_roll(page: Image.Image, manifest: dict, roll_no: str) -> None:
    draw = ImageDraw.Draw(page)
    block = manifest["roll_number_block"]
    selector = block["program_selector"]["BTECH"]
    _fill_bubble(draw, manifest, selector["x_mm"], selector["y_mm"])
    grid = block["btech_digits"]
    for col, digit in enumerate(roll_no):
        _fill_bubble(
            draw,
            manifest,
            grid["x_mm"] + col * grid["col_pitch_mm"],
            grid["y_mm"] + int(digit) * grid["row_pitch_mm"],
        )


def _fill_continuation_program(page: Image.Image, manifest: dict, program: str) -> None:
    draw = ImageDraw.Draw(page)
    choice = next(
        item for item in manifest["continuation_program_choices"] if item["page"] == 2 and item["program"] == program
    )
    _fill_bubble(draw, manifest, choice["x_mm"], choice["y_mm"])


def test_batch_pdf_groups_unordered_pages_by_page_identity(tmp_path: Path):
    result = generate_exam(
        ExamConfig(
            exam_id="BATCH_TEST",
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
    manifest = result["manifest"]
    pages = _render_pages(result["pdf_path"])
    _fill_btech_roll(pages[1], manifest, "2024587")
    _fill_continuation_program(pages[2], manifest, "BTECH")

    bundle_path = tmp_path / "unordered_bundle.pdf"
    pages[2].save(bundle_path, save_all=True, append_images=[pages[1]], resolution=DPI)

    students, index_path = parse_exam_bundle(
        bundle_path,
        result["manifest_path"],
        output_root=tmp_path / "parsed",
        dpi=DPI,
        ocr_backend=FakeOcr(),
    )

    assert len(students) == 1
    student = students[0]
    assert student["student"]["roll_no"] == "2024587"
    assert {page["page_index"] for page in student["pages"]} == {1, 2}
    assert {source["source_index"] for source in student["source_pages"]} == {1, 2}
    assert any(read["kind"] == "handwritten" for read in student["identity_reads"])

    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "multi_student_bundle"
    assert payload["status_counts"]["unmatched_pages"] == 0
    assert Path(student["details_path"]).exists()
    assert (Path(student["details_path"]).parent / "identity" / "page_2_btech_roll_crop.png").exists()
    assert (Path(student["details_path"]).parent / "identity" / "page_2_btech_roll_cell_1.png").exists()
    assert student["roll_read"]["write_in_roll_read"]["roll_no"] == "2024587"
    assert any(read["kind"] == "bubbled" for read in student["identity_reads"])
