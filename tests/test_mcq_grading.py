"""Validates the MCQ pipeline the way the roadmap prescribes for Phase 1:
draw filled bubbles onto a manifest-sized canvas in code (no real scanner or
students needed) and confirm grading recovers the right answers.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

from omr.generator.config import ExamConfig, WrittenQuestionConfig
from omr.generator.layout import build_layout
from omr.generator.manifest import build_manifest
from omr.grading.bubbles import canonical_size_px, mm_to_px
from omr.grading.mcq import MCQOutcome, grade_mcq_responses, read_mcq_responses

DPI = 200


def make_config(num_mcq: int = 10, mcq_options: int = 4, written_questions=None) -> ExamConfig:
    if written_questions is None:
        written_questions = [WrittenQuestionConfig(q_no=num_mcq + 1, max_marks=5, lines=2)]
    return ExamConfig(
        exam_id="TEST_EXAM",
        course_code="CS999",
        exam_name="Test Exam",
        exam_type="quiz",
        num_mcq=num_mcq,
        mcq_options=mcq_options,
        marks_per_mcq=2,
        written_questions=written_questions,
    )


def blank_canonical_image(manifest: dict, dpi: float) -> Image.Image:
    w_px, h_px = canonical_size_px(manifest, dpi)
    return Image.new("L", (w_px, h_px), color=255)


def blank_images_by_page(manifest: dict, dpi: float) -> dict[int, Image.Image]:
    return {page: blank_canonical_image(manifest, dpi) for page in range(1, manifest["num_pages"] + 1)}


def fill_bubble(draw: ImageDraw.ImageDraw, manifest: dict, dpi: float, x_mm: float, y_mm: float) -> None:
    radius_px = manifest["bubble_radius_mm"] * dpi / 25.4
    cx, cy = mm_to_px(x_mm, y_mm, dpi)
    r = radius_px * 0.8  # a real pen fill rarely covers the bubble outline exactly
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=0)


def option_position(manifest: dict, entry: dict, option_index: int) -> tuple[float, float]:
    x_mm = entry["x_mm"] + manifest["mcq_label_offset_mm"] + option_index * manifest["mcq_option_pitch_mm"]
    return x_mm, entry["y_mm"]


def test_mcq_grading_recovers_correct_answers():
    config = make_config(num_mcq=10, mcq_options=4)
    manifest = build_manifest(build_layout(config))
    assert manifest["num_pages"] == 1

    images = blank_images_by_page(manifest, DPI)
    draw = ImageDraw.Draw(images[1])

    answer_key: dict[int, str] = {}
    selections: dict[int, str] = {}
    for i, entry in enumerate(manifest["mcq_block"]):
        option_index = i % config.mcq_options
        selected = entry["options"][option_index]
        selections[entry["q_no"]] = selected
        answer_key[entry["q_no"]] = selected  # every question answered correctly
        x_mm, y_mm = option_position(manifest, entry, option_index)
        fill_bubble(draw, manifest, DPI, x_mm, y_mm)

    images_by_page = {page: np.array(img) for page, img in images.items()}
    readings = read_mcq_responses(images_by_page, manifest, DPI)
    grades = grade_mcq_responses(readings, answer_key, config.marks_per_mcq)

    assert len(grades) == config.num_mcq
    for g in grades:
        assert g.outcome == MCQOutcome.ANSWERED
        assert g.selected_option == selections[g.q_no]
        assert g.marks_awarded == config.marks_per_mcq


def test_mcq_grading_distinguishes_wrong_blank_and_correct():
    config = make_config(num_mcq=3, mcq_options=4)
    manifest = build_manifest(build_layout(config))
    images = blank_images_by_page(manifest, DPI)
    draw = ImageDraw.Draw(images[1])

    # Q1: fill option B, correct answer is A -> wrong
    entry1 = manifest["mcq_block"][0]
    fill_bubble(draw, manifest, DPI, *option_position(manifest, entry1, 1))
    # Q2: leave blank
    # Q3: fill option C, correct answer is C -> correct
    entry3 = manifest["mcq_block"][2]
    fill_bubble(draw, manifest, DPI, *option_position(manifest, entry3, 2))

    answer_key = {1: "A", 2: "A", 3: "C"}
    images_by_page = {page: np.array(img) for page, img in images.items()}
    readings = read_mcq_responses(images_by_page, manifest, DPI)
    grades = {g.q_no: g for g in grade_mcq_responses(readings, answer_key, config.marks_per_mcq)}

    assert grades[1].outcome == MCQOutcome.ANSWERED
    assert grades[1].selected_option == "B"
    assert grades[1].marks_awarded == 0

    assert grades[2].outcome == MCQOutcome.BLANK
    assert grades[2].selected_option is None
    assert grades[2].marks_awarded == 0

    assert grades[3].outcome == MCQOutcome.ANSWERED
    assert grades[3].selected_option == "C"
    assert grades[3].marks_awarded == config.marks_per_mcq


def test_mcq_grading_flags_multiple_filled_as_invalid_not_wrong():
    config = make_config(num_mcq=1, mcq_options=4)
    manifest = build_manifest(build_layout(config))
    images = blank_images_by_page(manifest, DPI)
    draw = ImageDraw.Draw(images[1])

    entry = manifest["mcq_block"][0]
    fill_bubble(draw, manifest, DPI, *option_position(manifest, entry, 0))
    fill_bubble(draw, manifest, DPI, *option_position(manifest, entry, 1))

    images_by_page = {page: np.array(img) for page, img in images.items()}
    readings = read_mcq_responses(images_by_page, manifest, DPI)

    assert readings[0].outcome == MCQOutcome.MULTIPLE
    assert readings[0].selected_option is None

    grades = grade_mcq_responses(readings, {1: "A"}, config.marks_per_mcq)
    assert grades[0].marks_awarded == 0
    assert grades[0].outcome == MCQOutcome.MULTIPLE  # distinct from a genuine blank


def test_mcq_grading_at_higher_dpi_photo_like_resolution():
    config = make_config(num_mcq=6, mcq_options=4)
    manifest = build_manifest(build_layout(config))
    dpi = 300

    images = blank_images_by_page(manifest, dpi)
    draw = ImageDraw.Draw(images[1])
    answer_key = {}
    for i, entry in enumerate(manifest["mcq_block"]):
        opt_idx = (i + 1) % config.mcq_options
        answer_key[entry["q_no"]] = entry["options"][opt_idx]
        fill_bubble(draw, manifest, dpi, *option_position(manifest, entry, opt_idx))

    images_by_page = {page: np.array(img) for page, img in images.items()}
    readings = read_mcq_responses(images_by_page, manifest, dpi)
    grades = grade_mcq_responses(readings, answer_key, config.marks_per_mcq)
    assert all(g.outcome == MCQOutcome.ANSWERED and g.marks_awarded == config.marks_per_mcq for g in grades)


def test_mcq_grading_across_multi_page_sheet():
    """The exact scenario that motivated pagination: 10 MCQs + 10 written
    questions doesn't fit on one page, so this exercises grading against a
    real multi-page manifest — each MCQ read from its own page's image."""
    written = [WrittenQuestionConfig(q_no=11 + i, max_marks=5, lines=3) for i in range(10)]
    config = make_config(num_mcq=10, mcq_options=4, written_questions=written)
    manifest = build_manifest(build_layout(config))
    assert manifest["num_pages"] > 1

    images = blank_images_by_page(manifest, DPI)
    draws = {page: ImageDraw.Draw(img) for page, img in images.items()}

    answer_key: dict[int, str] = {}
    selections: dict[int, str] = {}
    for i, entry in enumerate(manifest["mcq_block"]):
        option_index = i % config.mcq_options
        selected = entry["options"][option_index]
        selections[entry["q_no"]] = selected
        answer_key[entry["q_no"]] = selected
        x_mm, y_mm = option_position(manifest, entry, option_index)
        fill_bubble(draws[entry["page"]], manifest, DPI, x_mm, y_mm)

    images_by_page = {page: np.array(img) for page, img in images.items()}
    readings = read_mcq_responses(images_by_page, manifest, DPI)
    grades = grade_mcq_responses(readings, answer_key, config.marks_per_mcq)

    assert len(grades) == 10
    for g in grades:
        assert g.outcome == MCQOutcome.ANSWERED
        assert g.selected_option == selections[g.q_no]
        assert g.marks_awarded == config.marks_per_mcq


def test_mcq_grading_raises_clearly_when_a_page_image_is_missing():
    # Enough MCQs on their own (no written questions) to force the MCQ
    # section itself across more than one page.
    config = make_config(num_mcq=100, mcq_options=4, written_questions=[])
    manifest = build_manifest(build_layout(config))
    assert manifest["num_pages"] > 1
    assert {e["page"] for e in manifest["mcq_block"]} == set(range(1, manifest["num_pages"] + 1))

    only_page_1 = {1: np.array(blank_canonical_image(manifest, DPI))}
    try:
        read_mcq_responses(only_page_1, manifest, DPI)
        assert False, "expected a KeyError for the missing page image"
    except KeyError:
        pass
