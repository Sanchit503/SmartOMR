from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageDraw

from omr.contracts.geometry import MM_PER_INCH, mm_to_px, px_per_mm
from omr.generator.config import ExamConfig, WrittenQuestionConfig
from omr.generator.generate import generate_exam
from omr.workflows.parse import parse_scan, parse_scans

DPI = 200


def _config() -> ExamConfig:
    return ExamConfig(
        exam_id="PARSE_TEST",
        university_name="IIIT Delhi",
        course_code="CSE202",
        exam_name="Quiz - 1",
        exam_type="quiz",
        num_mcq=10,
        mcq_options=4,
        marks_per_mcq=1,
        written_questions=[WrittenQuestionConfig(q_no=11 + i, max_marks=1, lines=2) for i in range(10)],
    )


def _render_pages(pdf_path: Path) -> dict[int, Image.Image]:
    pages = {}
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
            pages[i] = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    return pages


def _fill_bubble(draw: ImageDraw.ImageDraw, manifest: dict, x_mm: float, y_mm: float) -> None:
    cx, cy = mm_to_px(x_mm, y_mm, DPI)
    radius = manifest["bubble_sample_radius_mm"] * px_per_mm(DPI) * 1.08
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=0)


def _fill_identity(draw: ImageDraw.ImageDraw, manifest: dict, roll_no: str = "2026001") -> None:
    rb = manifest["roll_number_block"]
    program = rb["program_selector"]["BTECH"]
    _fill_bubble(draw, manifest, program["x_mm"], program["y_mm"])

    grid = rb["btech_digits"]
    for col, digit_char in enumerate(roll_no):
        digit = int(digit_char)
        x_mm = grid["x_mm"] + col * grid["col_pitch_mm"]
        y_mm = grid["y_mm"] + digit * grid["row_pitch_mm"]
        _fill_bubble(draw, manifest, x_mm, y_mm)


def _fill_mcqs(draws: dict[int, ImageDraw.ImageDraw], manifest: dict) -> dict[int, str]:
    selections = {}
    for i, entry in enumerate(manifest["mcq_block"]):
        option = entry["options"][i % len(entry["options"])]
        selections[entry["q_no"]] = option
        option_index = entry["options"].index(option)
        x_mm = entry["x_mm"] + manifest["mcq_label_offset_mm"] + option_index * manifest["mcq_option_pitch_mm"]
        _fill_bubble(draws[entry.get("page", 1)], manifest, x_mm, entry["y_mm"])
    return selections


def _write_answers(draws: dict[int, ImageDraw.ImageDraw], manifest: dict) -> None:
    scale = px_per_mm(DPI)
    for entry in manifest["written_block"]:
        draw = draws[entry.get("page", 1)]
        x0, y0 = mm_to_px(entry["x_mm"] + 3, entry["y_mm"] + 4, DPI)
        x1 = x0 + round(30 * scale)
        y1 = y0 + round(2 * scale)
        draw.line([x0, y0, x1, y1], fill=0, width=3)
        draw.line([x0, y0 + round(4 * scale), x1, y1 + round(4 * scale)], fill=0, width=3)


def _save_scan_pdf(images: dict[int, Image.Image], manifest: dict, path: Path) -> Path:
    width_pt = manifest["page"]["width_mm"] / MM_PER_INCH * 72
    height_pt = manifest["page"]["height_mm"] / MM_PER_INCH * 72
    doc = pymupdf.open()
    for page_no in sorted(images):
        page = doc.new_page(width=width_pt, height=height_pt)
        buf = io.BytesIO()
        images[page_no].save(buf, format="PNG")
        page.insert_image(page.rect, stream=buf.getvalue())
    doc.save(path)
    return path


def _write_csvs(tmp_path: Path, selections: dict[int, str]) -> tuple[Path, Path]:
    students = tmp_path / "students.csv"
    answer_key = tmp_path / "answer_key.csv"
    students.write_text("roll_no,name,email,program\n2026001,Aryan,aryan@example.com,BTECH\n", encoding="utf-8")
    lines = ["q_no,answer,marks"]
    lines.extend(f"{q_no},{answer},1" for q_no, answer in sorted(selections.items()))
    answer_key.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return students, answer_key


def _filled_scan(tmp_path: Path):
    result = generate_exam(_config(), tmp_path / "exam")
    manifest = result["manifest"]
    pages = _render_pages(result["pdf_path"])
    draws = {page: ImageDraw.Draw(image) for page, image in pages.items()}
    _fill_identity(draws[1], manifest)
    selections = _fill_mcqs(draws, manifest)
    _write_answers(draws, manifest)
    scan_path = _save_scan_pdf(pages, manifest, tmp_path / "filled_scan.pdf")
    students, answer_key = _write_csvs(tmp_path, selections)
    return result["manifest_path"], manifest, scan_path, students, answer_key


def test_parse_scan_saves_canonical_pages_mcqs_and_written_crops(tmp_path: Path):
    manifest_path, manifest, scan_path, students, answer_key = _filled_scan(tmp_path)
    output_dir = tmp_path / "parsed" / "sheet_1"

    payload = parse_scan(
        scan_path,
        manifest_path=manifest_path,
        output_dir=output_dir,
        students_path=students,
        answer_key_path=answer_key,
        dpi=DPI,
    )

    assert payload["status"] == "ready", payload["review_flags"]
    assert payload["student"] == {
        "roll_no": "2026001",
        "program": "BTECH",
        "name": "Aryan",
        "email": "aryan@example.com",
    }
    assert payload["mcq_score"] == 10
    assert payload["mcq_total"] == 10
    assert len(payload["mcq_responses"]) == 10
    assert len(payload["written_responses"]) == 10
    assert {page["page_index"] for page in payload["pages"]} == set(range(1, manifest["num_pages"] + 1))

    for page in payload["pages"]:
        assert (output_dir / page["canonical_image_path"]).exists()
        assert (output_dir / page["debug_image_path"]).exists()
        assert page["alignment_quality_status"] == "ready"
        assert page["alignment_quality_score"] > 0.80
        assert (output_dir / page["alignment_report_path"]).exists()
        assert (output_dir / page["alignment_overlay_path"]).exists()
        assert (output_dir / page["sampling_overlay_path"]).exists()
    for written in payload["written_responses"]:
        crop_path = output_dir / written["crop_path"]
        assert crop_path.exists()
        assert np.asarray(Image.open(crop_path)).min() < 200
        ocr_crop_path = output_dir / written["ocr_crop_path"]
        assert ocr_crop_path.exists()
        assert np.asarray(Image.open(ocr_crop_path)).min() < 200
    assert any(written["page"] == 2 for written in payload["written_responses"])
    assert (output_dir / "parse.json").exists()


def test_parse_scans_writes_an_index_for_a_folder(tmp_path: Path):
    manifest_path, _manifest, scan_path, students, answer_key = _filled_scan(tmp_path)
    scans_dir = tmp_path / "scans"
    scans_dir.mkdir()
    copied_scan = scans_dir / scan_path.name
    copied_scan.write_bytes(scan_path.read_bytes())

    results, index_path = parse_scans(
        manifest_path=manifest_path,
        scans_path=scans_dir,
        students_path=students,
        answer_key_path=answer_key,
        output_root=tmp_path / "out",
        dpi=DPI,
    )

    assert len(results) == 1
    assert results[0]["status"] == "ready", results[0]["review_flags"]
    assert index_path == tmp_path / "out" / "PARSE_TEST" / "parse_index.json"
    assert index_path.exists()
    index = json.loads(index_path.read_text(encoding="utf-8"))
    answers = index["results"][0]["mcq_answers"]
    assert len(answers) == 10
    assert {answer["q_no"] for answer in answers} == set(range(1, 11))
    assert all("answer" in answer and "outcome" in answer for answer in answers)


def test_parse_scans_records_non_omr_upload_as_error(tmp_path: Path):
    result = generate_exam(_config(), tmp_path / "exam")
    scans_dir = tmp_path / "scans"
    scans_dir.mkdir()
    Image.new("L", (700, 900), 255).save(scans_dir / "plain_paper.png")

    results, index_path = parse_scans(
        manifest_path=result["manifest_path"],
        scans_path=scans_dir,
        output_root=tmp_path / "out",
        dpi=DPI,
    )

    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert "SmartOMR" in results[0]["review_flags"][0]
    index = json.loads(index_path.read_text(encoding="utf-8"))
    assert index["status_counts"] == {"ready": 0, "needs_review": 0, "error": 1}


def test_parse_scan_scores_available_pages_by_default(tmp_path: Path):
    result = generate_exam(_config(), tmp_path / "exam")
    manifest = result["manifest"]
    pages = _render_pages(result["pdf_path"])
    draws = {page: ImageDraw.Draw(image) for page, image in pages.items()}
    _fill_identity(draws[1], manifest)
    selections = _fill_mcqs(draws, manifest)
    _write_answers(draws, manifest)

    page_1_only = _save_scan_pdf({1: pages[1]}, manifest, tmp_path / "page_1_only.pdf")
    students, answer_key = _write_csvs(tmp_path, selections)

    payload = parse_scan(
        page_1_only,
        manifest_path=result["manifest_path"],
        output_dir=tmp_path / "partial" / "sheet_1",
        students_path=students,
        answer_key_path=answer_key,
        dpi=DPI,
    )

    assert payload["status"] == "needs_review"
    assert payload["student"]["roll_no"] == "2026001"
    assert payload["mcq_score"] == 10
    assert payload["mcq_total"] == 10
    assert {page["page_index"] for page in payload["pages"]} == {1}
    assert any("missing page(s): 2" in flag for flag in payload["review_flags"])
    assert all(written["page"] == 1 for written in payload["written_responses"])


def test_parse_scan_can_still_fail_on_missing_pages_in_strict_mode(tmp_path: Path):
    result = generate_exam(_config(), tmp_path / "exam")
    manifest = result["manifest"]
    pages = _render_pages(result["pdf_path"])
    draws = {page: ImageDraw.Draw(image) for page, image in pages.items()}
    _fill_identity(draws[1], manifest)
    selections = _fill_mcqs(draws, manifest)

    page_1_only = _save_scan_pdf({1: pages[1]}, manifest, tmp_path / "page_1_only.pdf")
    students, answer_key = _write_csvs(tmp_path, selections)

    try:
        parse_scan(
            page_1_only,
            manifest_path=result["manifest_path"],
            output_dir=tmp_path / "strict" / "sheet_1",
            students_path=students,
            answer_key_path=answer_key,
            dpi=DPI,
            allow_partial=False,
        )
        assert False, "strict mode should reject a missing page"
    except Exception as exc:
        assert "missing page(s): 2" in str(exc)
