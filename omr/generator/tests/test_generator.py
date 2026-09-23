from __future__ import annotations

import json

import pytest

from omr.generator.config import ExamConfig, NumericalQuestionConfig, WrittenQuestionConfig
from omr.generator.generate import generate_exam
from omr.generator.layout import build_layout
from omr.generator.metrics import (
    NUMERICAL_QUESTION_GAP_MM,
    PAGE_HEIGHT_MM,
    PAGE_WIDTH_MM,
    numerical_slot_height_mm,
)
from omr.generator.manifest import build_manifest


def sample_config(**overrides) -> ExamConfig:
    base = dict(
        exam_id="CS301_MIDSEM_2026A",
        course_code="CS301",
        exam_name="Mid-Semester Examination",
        exam_type="midsem",
        num_mcq=20,
        mcq_options=4,
        marks_per_mcq=1,
        written_questions=[
            WrittenQuestionConfig(q_no=21, max_marks=5, lines=2),
            WrittenQuestionConfig(q_no=22, max_marks=5, lines=2),
            WrittenQuestionConfig(q_no=23, max_marks=10, lines=4),
        ],
    )
    base.update(overrides)
    return ExamConfig(**base)


def test_generate_exam_writes_pdf_and_manifest(tmp_path):
    config = sample_config()
    result = generate_exam(config, tmp_path)

    assert result["pdf_path"].exists()
    assert result["pdf_path"].stat().st_size > 0
    assert result["manifest_path"].exists()

    manifest = json.loads(result["manifest_path"].read_text())
    assert manifest["exam_id"] == config.exam_id
    assert manifest["exam"]["university_name"] == config.university_name
    assert manifest["num_pages"] == 1
    assert manifest["page"] == {"width_mm": PAGE_WIDTH_MM, "height_mm": PAGE_HEIGHT_MM}
    assert len(manifest["mcq_block"]) == 20
    assert len(manifest["written_block"]) == 3
    assert {f["corner"] for f in manifest["fiducials"]} == {"TL", "TR", "BL", "BR"}
    assert manifest["roll_number_block"]["btech_digits"]["columns"] == 7
    assert manifest["roll_number_block"]["mtech_digits"]["columns"] == 5


def test_manifest_matches_layout_object_exactly():
    config = sample_config()
    layout = build_layout(config)
    manifest = build_manifest(layout)

    for entry, expected in zip(manifest["mcq_block"], layout.mcq_entries):
        assert entry["q_no"] == expected.q_no
        assert entry["x_mm"] == expected.x_mm
        assert entry["y_mm"] == expected.y_mm
        assert entry["options"] == expected.options


def test_mcq_options_match_config():
    config = sample_config(mcq_options=5)
    layout = build_layout(config)
    for entry in layout.mcq_entries:
        assert entry.options == ["A", "B", "C", "D", "E"]


def test_all_positions_within_page_bounds():
    config = sample_config(num_mcq=40, written_questions=[])
    layout = build_layout(config)
    for entry in layout.mcq_entries:
        assert 0 <= entry.x_mm <= PAGE_WIDTH_MM
        assert 0 <= entry.y_mm <= PAGE_HEIGHT_MM
    for entry in layout.written_entries:
        assert entry.x_mm + entry.width_mm <= PAGE_WIDTH_MM + 1e-6
        assert entry.y_mm + entry.height_mm <= PAGE_HEIGHT_MM


def test_duplicate_written_q_no_rejected():
    with pytest.raises(Exception):
        sample_config(
            written_questions=[
                WrittenQuestionConfig(q_no=21, max_marks=5, lines=2),
                WrittenQuestionConfig(q_no=21, max_marks=5, lines=2),
            ]
        )


def test_written_q_no_colliding_with_mcq_numbering_rejected():
    with pytest.raises(Exception):
        sample_config(
            num_mcq=20,
            written_questions=[WrittenQuestionConfig(q_no=5, max_marks=5, lines=2)],
        )


def test_numerical_questions_are_vertical_and_manifest_driven():
    config = sample_config(
        num_mcq=5,
        numerical_questions=[
            NumericalQuestionConfig(q_no=6, max_marks=2, digits=2),
            NumericalQuestionConfig(q_no=7, max_marks=3, digits=4),
        ],
        written_questions=[WrittenQuestionConfig(q_no=8, max_marks=5, lines=2)],
    )
    layout = build_layout(config)
    manifest = build_manifest(layout)

    assert layout.total_marks == 15
    assert [entry["q_no"] for entry in manifest["numerical_block"]] == [6, 7]
    assert all(entry["orientation"] == "vertical" for entry in manifest["numerical_block"])
    assert all(entry["answer_type"] == "unsigned_integer" for entry in manifest["numerical_block"])
    assert all(entry["leading_zeros"] == "required" for entry in manifest["numerical_block"])
    first, second = manifest["numerical_block"]
    assert first["y_mm"] == second["y_mm"]
    assert first["x_mm"] < second["x_mm"]


def test_numerical_question_rows_have_clear_vertical_separation():
    config = sample_config(
        num_mcq=0,
        numerical_questions=[
            NumericalQuestionConfig(q_no=q_no, max_marks=1, digits=2)
            for q_no in range(1, 9)
        ],
        written_questions=[],
    )
    entries = build_layout(config).numerical_entries

    assert len({e.y_mm for e in entries[:4]}) == 1
    assert len({e.y_mm for e in entries[4:]}) == 1
    assert entries[4].y_mm - entries[0].y_mm == pytest.approx(
        numerical_slot_height_mm(2) + NUMERICAL_QUESTION_GAP_MM
    )


def test_numerical_question_numbering_and_digit_limits_are_validated():
    with pytest.raises(Exception, match="collides with MCQs"):
        sample_config(
            num_mcq=5,
            numerical_questions=[NumericalQuestionConfig(q_no=5, max_marks=1, digits=2)],
        )
    with pytest.raises(Exception):
        NumericalQuestionConfig(q_no=1, max_marks=1, digits=9)


# ---- Pagination (an exam too big for one page spills onto more, cleanly) ----


def test_overflowing_exam_paginates_instead_of_erroring():
    written = [WrittenQuestionConfig(q_no=11 + i, max_marks=5, lines=3) for i in range(10)]
    config = sample_config(num_mcq=10, mcq_options=4, written_questions=written)
    layout = build_layout(config)

    assert layout.num_pages > 1

    mcq_q_nos = sorted(e.q_no for e in layout.mcq_entries)
    assert mcq_q_nos == list(range(1, 11))
    written_q_nos = sorted(e.q_no for e in layout.written_entries)
    assert written_q_nos == [w.q_no for w in written]

    for entry in layout.mcq_entries:
        assert 1 <= entry.page <= layout.num_pages
        assert 0 <= entry.x_mm <= PAGE_WIDTH_MM
        assert 0 <= entry.y_mm <= PAGE_HEIGHT_MM
    for entry in layout.written_entries:
        assert 1 <= entry.page <= layout.num_pages
        assert entry.x_mm + entry.width_mm <= PAGE_WIDTH_MM + 1e-6
        assert entry.y_mm + entry.height_mm <= PAGE_HEIGHT_MM

    # sections don't interleave: every MCQ page precedes every written page
    assert max(e.page for e in layout.mcq_entries) <= min(e.page for e in layout.written_entries)


def test_manifest_includes_page_tags_for_multi_page_exam():
    written = [WrittenQuestionConfig(q_no=11 + i, max_marks=5, lines=3) for i in range(10)]
    config = sample_config(num_mcq=10, mcq_options=4, written_questions=written)
    layout = build_layout(config)
    manifest = build_manifest(layout)

    assert manifest["num_pages"] == layout.num_pages
    assert manifest["num_pages"] > 1
    assert manifest["roll_number_block"]["page"] == 1
    assert all("page" in f for f in manifest["fiducials"])
    assert all("page" in e for e in manifest["mcq_block"])
    assert all("page" in e for e in manifest["written_block"])
    assert len(manifest["fiducials"]) == 4 * manifest["num_pages"]


def test_pdf_page_count_matches_layout(tmp_path):
    from pypdf import PdfReader

    written = [WrittenQuestionConfig(q_no=11 + i, max_marks=5, lines=3) for i in range(10)]
    config = sample_config(exam_id="OVERFLOW_TEST", num_mcq=10, mcq_options=4, written_questions=written)
    result = generate_exam(config, tmp_path)

    reader = PdfReader(str(result["pdf_path"]))
    assert len(reader.pages) == result["manifest"]["num_pages"]


def test_single_question_too_tall_for_any_page_still_raises():
    config = sample_config(
        num_mcq=0,
        written_questions=[WrittenQuestionConfig(q_no=1, max_marks=20, lines=60)],
    )
    with pytest.raises(ValueError):
        build_layout(config)
