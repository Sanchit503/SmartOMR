"""Preflight has to fail on a bad sheet, not just pass on a good one.

A check that only ever returns "ok" is worse than no check, because it
converts an unexamined sheet into a sheet someone believes was examined.
So each test here deliberately damages a manifest in the way a real layout
regression would, and asserts preflight catches it.
"""
from __future__ import annotations

import pytest

from omr.generator.config import ExamConfig, WrittenQuestionConfig
from omr.generator.generate import generate_exam
from omr.generator.preflight import PREFLIGHT_DPI, check_sheet


@pytest.fixture
def sheet(tmp_path):
    config = ExamConfig(
        exam_id="PREFLIGHT_TEST",
        course_code="CS301",
        exam_name="Mid-Semester Examination",
        exam_type="midsem",
        num_mcq=20,
        mcq_options=4,
        marks_per_mcq=1,
        written_questions=[
            WrittenQuestionConfig(q_no=21, max_marks=5, lines=2),
            WrittenQuestionConfig(q_no=22, max_marks=10, lines=4),
        ],
    )
    result = generate_exam(config, tmp_path)
    return result["pdf_path"], result["manifest"]


def test_a_freshly_generated_sheet_passes_cleanly(sheet):
    pdf, manifest = sheet
    report = check_sheet(pdf, manifest)
    assert report.ok, report.format()
    assert not report.warnings, report.format()
    assert "ready to print" in report.format()


def test_report_measures_at_the_dpi_the_reader_will_use(sheet):
    pdf, manifest = sheet
    report = check_sheet(pdf, manifest)
    assert report.dpi == PREFLIGHT_DPI
    assert report.num_pages == manifest["num_pages"]


def test_a_bubble_moved_onto_printed_text_is_caught(sheet):
    """The exact regression preflight exists for: a coordinate change that
    parks a bubble on top of something already inked. Every arithmetic
    assertion in the suite still passes; only the pixels disagree."""
    pdf, manifest = sheet
    manifest["mcq_block"][0]["y_mm"] = 24.0  # onto the title text
    report = check_sheet(pdf, manifest)
    assert not report.ok
    assert any("print empty" in e.check for e in report.errors), report.format()


def test_content_drifting_into_a_fiducial_quiet_zone_is_caught(sheet):
    pdf, manifest = sheet
    # Claim a fiducial sits in the middle of the MCQ block, where there is
    # plenty of ink — same effect as content drifting onto a real marker.
    manifest["fiducials"][0]["x_mm"] = manifest["mcq_block"][0]["x_mm"] + 14
    manifest["fiducials"][0]["y_mm"] = manifest["mcq_block"][0]["y_mm"]
    report = check_sheet(pdf, manifest)
    assert not report.ok
    assert any("quiet zone" in e.check for e in report.errors), report.format()


def test_a_missing_orientation_marker_is_an_error(sheet):
    """Without it, a page scanned upside down reads as a valid upright page
    with every coordinate inverted — silently, and on every sheet."""
    pdf, manifest = sheet
    manifest["orientation_marker"] = []
    report = check_sheet(pdf, manifest)
    assert not report.ok
    assert any("orientation" in e.check for e in report.errors)


def test_an_orientation_marker_at_the_wrong_corner_is_an_error(sheet):
    pdf, manifest = sheet
    manifest["orientation_marker"][0]["marks_corner"] = "BR"
    report = check_sheet(pdf, manifest)
    assert not report.ok
    assert any("nearest" in e.detail for e in report.errors)


def test_overlapping_bubbles_are_an_error(sheet):
    pdf, manifest = sheet
    first, second = manifest["mcq_block"][0], manifest["mcq_block"][1]
    second["x_mm"], second["y_mm"] = first["x_mm"], first["y_mm"]
    report = check_sheet(pdf, manifest)
    assert not report.ok
    assert any("spacing" in e.check for e in report.errors), report.format()


def test_crowded_but_not_overlapping_bubbles_only_warn(sheet):
    """A tight sheet still prints and still grades — it just has less
    tolerance for a heavy pen. That's a warning, not a refusal."""
    pdf, manifest = sheet
    gap = 2 * manifest["bubble_sample_radius_mm"] + 0.5
    manifest["mcq_block"][1]["x_mm"] = manifest["mcq_block"][0]["x_mm"]
    manifest["mcq_block"][1]["y_mm"] = manifest["mcq_block"][0]["y_mm"] + gap
    report = check_sheet(pdf, manifest)
    assert report.ok, report.format()
    assert any("spacing" in w.check for w in report.warnings), report.format()


def test_a_pdf_page_count_that_disagrees_with_the_manifest_is_an_error(sheet):
    """If these drift, the grader reads question coordinates against the
    wrong physical page."""
    pdf, manifest = sheet
    manifest["num_pages"] += 1
    report = check_sheet(pdf, manifest)
    assert not report.ok
    assert any("page count" in e.check for e in report.errors)


def test_a_bubble_off_the_page_is_an_error(sheet):
    pdf, manifest = sheet
    manifest["mcq_block"][0]["x_mm"] = 250.0
    report = check_sheet(pdf, manifest)
    assert not report.ok
    assert any("page bounds" in e.check for e in report.errors)
