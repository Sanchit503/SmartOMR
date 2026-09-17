from __future__ import annotations

import csv
import json
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

from omr.contracts.geometry import mm_to_px, px_per_mm
from omr.generator.config import ExamConfig, WrittenQuestionConfig
from omr.generator.generate import generate_exam
from omr.models import Student
from omr.reader.handwriting import RollOcrResult
from omr.workflows.batch import _reconcile_roster, parse_exam_bundle


DPI = 200


class FakeOcr:
    provider = "fake"

    def __init__(self, digits: str = "20245872024587", roll_text: str = "2024587") -> None:
        self.digits = list(digits)
        self.roll_text = roll_text

    def read_roll(self, crop_path: Path, program: str | None = None) -> RollOcrResult:
        return RollOcrResult(self.roll_text, confidence=0.95)

    def read_digit(self, crop_path: Path) -> RollOcrResult:
        digit = self.digits.pop(0) if self.digits else ""
        return RollOcrResult(digit, confidence=0.95)


def test_roster_reconciliation_exposes_missing_and_unexpected_rolls():
    roster = {
        "2024001": Student("2024001", "One", "one@example.edu", "BTECH"),
        "MT25007": Student("MT25007", "Two", "two@example.edu", "MTECH"),
    }

    result = _reconcile_roster(
        roster,
        [
            {"student": {"roll_no": "2024001"}},
            {"student": {"roll_no": "PHD25111"}},
        ],
    )

    assert result is not None
    assert result["roster_total"] == 2
    assert result["detected_roster_students"] == 1
    assert result["missing_students"] == [
        {
            "roll_no": "MT25007",
            "student_name": "Two",
            "student_email": "two@example.edu",
            "program": "MTECH",
        }
    ]
    assert result["unexpected_rolls"] == ["PHD25111"]


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


def _fill_continuation_program(page: Image.Image, manifest: dict, program: str, page_index: int = 2) -> None:
    draw = ImageDraw.Draw(page)
    choice = next(
        item
        for item in manifest["continuation_program_choices"]
        if item["page"] == page_index and item["program"] == program
    )
    _fill_bubble(draw, manifest, choice["x_mm"], choice["y_mm"])


_DIGIT_SEGMENTS = {
    "0": "abcfed",
    "1": "bc",
    "2": "abged",
    "3": "abgcd",
    "4": "fgbc",
    "5": "afgcd",
    "6": "afgecd",
    "7": "abc",
    "8": "abcdefg",
    "9": "abfgcd",
}


def _write_digit(draw: ImageDraw.ImageDraw, x0: int, y0: int, width: int, height: int, digit: str) -> None:
    thickness = max(2, round(min(width, height) * 0.14))
    left = x0 + round(width * 0.18)
    right = x0 + round(width * 0.82)
    top = y0 + round(height * 0.14)
    mid = y0 + round(height * 0.50)
    bottom = y0 + round(height * 0.86)
    upper_mid = y0 + round(height * 0.32)
    lower_mid = y0 + round(height * 0.68)
    segments = {
        "a": (left, top, right, top),
        "b": (right, top, right, mid),
        "c": (right, mid, right, bottom),
        "d": (left, bottom, right, bottom),
        "e": (left, mid, left, bottom),
        "f": (left, top, left, mid),
        "g": (left, mid, right, mid),
    }
    for segment in _DIGIT_SEGMENTS[digit]:
        line = segments[segment]
        draw.line(line, fill=0, width=thickness)
    if digit == "0":
        draw.line((left, upper_mid, right, lower_mid), fill=0, width=max(1, thickness - 1))


def _write_btech_roll_boxes(page: Image.Image, manifest: dict, page_index: int, roll_no: str) -> None:
    draw = ImageDraw.Draw(page)
    field = next(
        item
        for item in manifest["write_in_fields"]
        if item["page"] == page_index and item["program"] == "BTECH"
    )
    scale = px_per_mm(DPI)
    for index, digit in enumerate(roll_no):
        x0, y0 = mm_to_px(field["x_mm"] + index * field["cell_pitch_mm"], field["y_mm"], DPI)
        width = round(field["cell_width_mm"] * scale)
        height = round(field["height_mm"] * scale)
        _write_digit(draw, x0, y0, width, height, digit)


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
    assert payload["requested_grouping_mode"] == "auto"
    assert payload["grouping_mode"] == "identity"
    assert payload["expected_pages"] == 2
    assert payload["status_counts"]["unmatched_pages"] == 0
    assert Path(student["details_path"]).exists()
    sheet_pdf_path = Path(student["details_path"]).parent / student["sheet_pdf_path"]
    assert sheet_pdf_path.exists()
    with pymupdf.open(sheet_pdf_path) as doc:
        assert len(doc) == 2
    assert (Path(student["details_path"]).parent / "identity" / "page_2_btech_roll_crop.png").exists()
    assert (Path(student["details_path"]).parent / "identity" / "page_2_btech_roll_cell_1.png").exists()
    assert student["roll_read"]["write_in_roll_read"]["roll_no"] == "2024587"
    assert any(read["kind"] == "bubbled" for read in student["identity_reads"])
    assert payload["reports"] == {"csv": "review_report.csv", "html": "review_report.html"}
    csv_path = index_path.parent / payload["review_report_csv_path"]
    html_path = index_path.parent / payload["review_report_html_path"]
    assert csv_path.exists()
    assert html_path.exists()
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["item_type"] == "student"
    assert rows[0]["roll_no"] == "2024587"
    assert rows[0]["pages_found"] == "1,2"
    assert rows[0]["sheet_pdf_path"] == "students/2024587/sheet.pdf"
    assert rows[0]["details_path"] == "students/2024587/student.json"
    html_text = html_path.read_text(encoding="utf-8")
    assert "2024587" in html_text
    assert "students/2024587/sheet.pdf" in html_text


def test_batch_flags_first_page_write_in_roll_conflict(tmp_path: Path):
    result = generate_exam(
        ExamConfig(
            exam_id="BATCH_ROLL_CONFLICT_TEST",
            course_code="CSE202",
            exam_name="Quiz",
            exam_type="quiz",
            num_mcq=5,
            mcq_options=4,
            marks_per_mcq=1,
        ),
        tmp_path / "exam",
    )
    manifest = result["manifest"]
    pages = _render_pages(result["pdf_path"])
    _fill_btech_roll(pages[1], manifest, "2024587")

    bundle_path = tmp_path / "roll_conflict_bundle.pdf"
    pages[1].save(bundle_path, resolution=DPI)

    students, index_path = parse_exam_bundle(
        bundle_path,
        result["manifest_path"],
        output_root=tmp_path / "parsed",
        dpi=DPI,
        ocr_backend=FakeOcr(digits="20249992024999", roll_text="2024999"),
    )

    assert len(students) == 1
    student = students[0]
    assert student["student"]["roll_no"] == "2024587"
    assert student["status"] == "needs_review"
    assert student["roll_read"]["write_in_roll_read"]["roll_no"] == "2024999"
    assert any(
        "page-1 write-in roll 2024999 conflicts with grouped roll 2024587" in flag
        for flag in student["review_flags"]
    )
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["status_counts"]["needs_review"] == 1


def test_batch_pdf_groups_page_major_scanner_order_without_continuation_ocr(tmp_path: Path):
    result = generate_exam(
        ExamConfig(
            exam_id="PAGE_MAJOR_BATCH_TEST",
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
    assert manifest["num_pages"] == 2
    pages = _render_pages(result["pdf_path"])
    student_a_pages = {page_no: page.copy() for page_no, page in pages.items()}
    student_b_pages = {page_no: page.copy() for page_no, page in pages.items()}
    _fill_btech_roll(student_a_pages[1], manifest, "2024001")
    _fill_btech_roll(student_b_pages[1], manifest, "2024002")
    _fill_continuation_program(student_a_pages[2], manifest, "BTECH")
    _fill_continuation_program(student_b_pages[2], manifest, "BTECH")

    bundle_pages = [
        student_a_pages[1],
        student_b_pages[1],
        student_a_pages[2],
        student_b_pages[2],
    ]
    bundle_path = tmp_path / "page_major_bundle.pdf"
    bundle_pages[0].save(bundle_path, save_all=True, append_images=bundle_pages[1:], resolution=DPI)

    students, index_path = parse_exam_bundle(
        bundle_path,
        result["manifest_path"],
        output_root=tmp_path / "parsed",
        dpi=DPI,
        min_group_confidence="high",
    )

    assert {student["student"]["roll_no"] for student in students} == {"2024001", "2024002"}
    for student in students:
        assert {page["page_index"] for page in student["pages"]} == {1, 2}
        assert len(student["source_pages"]) == 2
        assert any(read["kind"] == "page_major_order" and read["page_index"] == 2 for read in student["identity_reads"])
        identity_dir = Path(student["details_path"]).parent / "identity"
        assert (identity_dir / "page_2_btech_roll_crop.png").exists()

    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["requested_grouping_mode"] == "auto"
    assert payload["grouping_mode"] == "page-major"
    assert payload["status_counts"]["unmatched_pages"] == 0
    assert payload["status_counts"]["page_errors"] == 0


def test_batch_pdf_groups_sheet_major_scanner_order_without_continuation_ocr(tmp_path: Path):
    result = generate_exam(
        ExamConfig(
            exam_id="SHEET_MAJOR_BATCH_TEST",
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
    assert manifest["num_pages"] == 2
    pages = _render_pages(result["pdf_path"])
    student_a_pages = {page_no: page.copy() for page_no, page in pages.items()}
    student_b_pages = {page_no: page.copy() for page_no, page in pages.items()}
    _fill_btech_roll(student_a_pages[1], manifest, "2024003")
    _fill_btech_roll(student_b_pages[1], manifest, "2024004")
    _fill_continuation_program(student_a_pages[2], manifest, "BTECH")
    _fill_continuation_program(student_b_pages[2], manifest, "BTECH")

    bundle_pages = [
        student_a_pages[1],
        student_a_pages[2],
        student_b_pages[1],
        student_b_pages[2],
    ]
    bundle_path = tmp_path / "sheet_major_bundle.pdf"
    bundle_pages[0].save(bundle_path, save_all=True, append_images=bundle_pages[1:], resolution=DPI)

    students, index_path = parse_exam_bundle(
        bundle_path,
        result["manifest_path"],
        output_root=tmp_path / "parsed",
        dpi=DPI,
        min_group_confidence="high",
    )

    assert {student["student"]["roll_no"] for student in students} == {"2024003", "2024004"}
    for student in students:
        assert {page["page_index"] for page in student["pages"]} == {1, 2}
        assert len(student["source_pages"]) == 2
        assert any(read["kind"] == "sheet_major_order" and read["page_index"] == 2 for read in student["identity_reads"])

    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["requested_grouping_mode"] == "auto"
    assert payload["grouping_mode"] == "sheet-major"
    assert payload["status_counts"]["unmatched_pages"] == 0
    assert payload["status_counts"]["page_errors"] == 0


def test_batch_auto_groups_irregular_order_by_write_in_similarity_without_continuation_ocr(tmp_path: Path):
    result = generate_exam(
        ExamConfig(
            exam_id="WRITE_IN_MATCH_BATCH_TEST",
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
    assert manifest["num_pages"] == 2
    pages = _render_pages(result["pdf_path"])
    student_a_pages = {page_no: page.copy() for page_no, page in pages.items()}
    student_b_pages = {page_no: page.copy() for page_no, page in pages.items()}
    _fill_btech_roll(student_a_pages[1], manifest, "2024503")
    _fill_btech_roll(student_b_pages[1], manifest, "2024544")
    _fill_continuation_program(student_a_pages[2], manifest, "BTECH")
    _fill_continuation_program(student_b_pages[2], manifest, "BTECH")
    _write_btech_roll_boxes(student_a_pages[1], manifest, 1, "2024503")
    _write_btech_roll_boxes(student_a_pages[2], manifest, 2, "2024503")
    _write_btech_roll_boxes(student_b_pages[1], manifest, 1, "2024544")
    _write_btech_roll_boxes(student_b_pages[2], manifest, 2, "2024544")

    bundle_pages = [
        student_a_pages[1],
        student_b_pages[2],
        student_a_pages[2],
        student_b_pages[1],
    ]
    bundle_path = tmp_path / "irregular_write_in_bundle.pdf"
    bundle_pages[0].save(bundle_path, save_all=True, append_images=bundle_pages[1:], resolution=DPI)

    students, index_path = parse_exam_bundle(
        bundle_path,
        result["manifest_path"],
        output_root=tmp_path / "parsed",
        dpi=DPI,
        min_group_confidence="high",
    )

    assert {student["student"]["roll_no"] for student in students} == {"2024503", "2024544"}
    for student in students:
        assert {page["page_index"] for page in student["pages"]} == {1, 2}
        sheet_pdf_path = Path(student["details_path"]).parent / student["sheet_pdf_path"]
        with pymupdf.open(sheet_pdf_path) as doc:
            assert len(doc) == 2
        assert any(read["kind"] == "write_in_similarity" for read in student["identity_reads"])

    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["requested_grouping_mode"] == "auto"
    assert payload["grouping_mode"] == "write-in-similarity"
    assert payload["detected_page_sequence"] == [1, 2, 2, 1]
    assert payload["status_counts"]["unmatched_pages"] == 0
    assert payload["status_counts"]["page_errors"] == 0


def test_batch_auto_groups_three_page_arbitrary_order_by_write_in_similarity(tmp_path: Path):
    result = generate_exam(
        ExamConfig(
            exam_id="THREE_PAGE_WRITE_IN_MATCH_TEST",
            course_code="CSE222",
            exam_name="Endsem",
            exam_type="endsem",
            num_mcq=10,
            mcq_options=4,
            marks_per_mcq=1,
            written_questions=[WrittenQuestionConfig(q_no=11 + i, max_marks=2, lines=3) for i in range(10)],
        ),
        tmp_path / "exam",
    )
    manifest = result["manifest"]
    assert manifest["num_pages"] == 3
    base_pages = _render_pages(result["pdf_path"])
    students = {
        "A": {"roll": "2024503", "pages": {page_no: page.copy() for page_no, page in base_pages.items()}},
        "B": {"roll": "2037618", "pages": {page_no: page.copy() for page_no, page in base_pages.items()}},
        "C": {"roll": "2048921", "pages": {page_no: page.copy() for page_no, page in base_pages.items()}},
    }
    for student in students.values():
        pages = student["pages"]
        roll_no = student["roll"]
        _fill_btech_roll(pages[1], manifest, roll_no)
        for page_index in range(1, manifest["num_pages"] + 1):
            _write_btech_roll_boxes(pages[page_index], manifest, page_index, roll_no)
            if page_index > 1:
                _fill_continuation_program(pages[page_index], manifest, "BTECH", page_index=page_index)

    order = [
        ("C", 2),
        ("A", 3),
        ("B", 1),
        ("C", 3),
        ("A", 2),
        ("A", 1),
        ("C", 1),
        ("B", 3),
        ("B", 2),
    ]
    bundle_pages = [students[student]["pages"][page_index] for student, page_index in order]
    bundle_path = tmp_path / "three_page_arbitrary_order.pdf"
    bundle_pages[0].save(bundle_path, save_all=True, append_images=bundle_pages[1:], resolution=DPI)

    parsed_students, index_path = parse_exam_bundle(
        bundle_path,
        result["manifest_path"],
        output_root=tmp_path / "parsed",
        dpi=DPI,
        min_group_confidence="high",
    )

    assert {student["student"]["roll_no"] for student in parsed_students} == {
        "2024503",
        "2037618",
        "2048921",
    }
    for student in parsed_students:
        assert {page["page_index"] for page in student["pages"]} == {1, 2, 3}
        sheet_pdf_path = Path(student["details_path"]).parent / student["sheet_pdf_path"]
        with pymupdf.open(sheet_pdf_path) as doc:
            assert len(doc) == 3
        assert sum(read["kind"] == "write_in_similarity" for read in student["identity_reads"]) == 2

    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["requested_grouping_mode"] == "auto"
    assert payload["grouping_mode"] == "write-in-similarity"
    assert payload["expected_pages"] == 3
    assert payload["detected_page_sequence"] == [2, 3, 1, 3, 2, 1, 1, 3, 2]
    assert payload["status_counts"]["unmatched_pages"] == 0
    assert payload["status_counts"]["page_errors"] == 0


def test_batch_unmatched_late_page_saves_written_crops(tmp_path: Path):
    result = generate_exam(
        ExamConfig(
            exam_id="BATCH_UNMATCHED_TEST",
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
    assert any(entry.get("page", 1) == 2 for entry in manifest["written_block"])

    pages = _render_pages(result["pdf_path"])
    _fill_continuation_program(pages[2], manifest, "BTECH")
    bundle_path = tmp_path / "late_page_only.pdf"
    pages[2].save(bundle_path, resolution=DPI)

    students, index_path = parse_exam_bundle(
        bundle_path,
        result["manifest_path"],
        output_root=tmp_path / "parsed",
        dpi=DPI,
    )

    assert students == []
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert payload["status_counts"]["unmatched_pages"] == 1
    unmatched = payload["unmatched_pages"][0]
    assert unmatched["page_index"] == 2
    assert unmatched["written_responses"]
    assert all(written["page"] == 2 for written in unmatched["written_responses"])

    details_dir = Path(unmatched["details_path"]).parent
    for written in unmatched["written_responses"]:
        assert (details_dir / written["crop_path"]).exists()
        assert (details_dir / written["ocr_crop_path"]).exists()
