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


# ---------------------------------------------------------------------------
# Answer boxes, orientation, page index
# ---------------------------------------------------------------------------

def test_something_printed_inside_an_answer_box_is_caught(sheet):
    """The box is the crop that goes to the vision grader. Anything in it other
    than its own writing rules competes with the student's handwriting.

    A mean-darkness test could not catch this: a whole row of MCQ bubbles
    inside a 186x14mm box is under 1% of its area. So the check masks the rules
    the box is supposed to have and asserts the rest is bare paper.
    """
    pdf, manifest = sheet
    manifest["written_block"][0]["y_mm"] = 70.0  # onto the roll-number grid
    report = check_sheet(pdf, manifest)
    assert not report.ok
    assert any("answer box must be blank" in e.check for e in report.errors), report.format()


def test_a_row_of_bubbles_inside_an_answer_box_is_caught(sheet):
    """The specific case mean darkness would miss."""
    pdf, manifest = sheet
    first_mcq = manifest["mcq_block"][0]
    box = manifest["written_block"][0]
    box["y_mm"] = first_mcq["y_mm"] - box["height_mm"] / 2
    report = check_sheet(pdf, manifest)
    assert not report.ok
    assert any("answer box must be blank" in e.check for e in report.errors), report.format()


def test_ink_in_the_orientation_markers_quiet_zone_is_caught(sheet):
    """The orientation marker is found the same way the corner markers are. If
    a glyph merges with it, the sheet loses its only defence against being read
    upside down — so it gets a quiet zone too, and that zone is checked."""
    pdf, manifest = sheet
    om = manifest["orientation_marker"][0]
    om["x_mm"] = manifest["mcq_block"][0]["x_mm"] + 14
    om["y_mm"] = manifest["mcq_block"][0]["y_mm"]
    report = check_sheet(pdf, manifest)
    assert not report.ok
    quiet = [e for e in report.errors if "quiet zone" in e.check]
    assert quiet and "orientation" in quiet[0].detail, report.format()


def test_a_sheet_that_prints_the_wrong_page_bar_is_caught(tmp_path):
    """End-to-end: render a PDF whose page 2 fills page 1's bar, then check it
    against the honest manifest. A page that misreports its own number puts a
    student's answers on the wrong questions."""
    import dataclasses

    from omr.generator.layout import build_layout
    from omr.generator.manifest import build_manifest
    from omr.generator.pdf_gen import render_pdf

    config = ExamConfig(
        exam_id="PREFLIGHT_BARS",
        course_code="CS301",
        exam_name="Mid-Semester Examination",
        exam_type="midsem",
        num_mcq=10,
        written_questions=[WrittenQuestionConfig(q_no=11 + i, max_marks=5, lines=2) for i in range(10)],
    )
    layout = build_layout(config)
    manifest = build_manifest(layout)
    assert manifest["num_pages"] > 1

    mislabelled = [
        dataclasses.replace(m, filled=(m.index == 1)) if m.page == 2 else m for m in layout.page_marks
    ]
    pdf = render_pdf(dataclasses.replace(layout, page_marks=mislabelled), tmp_path / "bars.pdf")

    report = check_sheet(pdf, manifest)
    assert not report.ok
    assert any("page index" in e.check for e in report.errors), report.format()


def test_bars_looked_for_where_they_were_not_printed_are_caught(sheet):
    pdf, manifest = sheet
    for m in manifest["page_marks"]:
        m["x_mm"] -= 25.0
    report = check_sheet(pdf, manifest)
    assert not report.ok
    assert any("page index" in e.check for e in report.errors), report.format()


# ---------------------------------------------------------------------------
# Printer-safe area
# ---------------------------------------------------------------------------

def test_nothing_is_printed_inside_the_printer_safe_margin(sheet):
    """Ordinary A4 printers can't reach the page edge, and the sheet must not
    depend on borderless printing."""
    pdf, manifest = sheet
    report = check_sheet(pdf, manifest)
    assert manifest["printer_safe_margin_mm"] >= 10.0
    assert not any("safe margin" in i.check for i in report.issues), report.format()


def test_ink_inside_the_safe_margin_is_caught(sheet):
    """Widening the claimed safe area brings the fiducials inside it, which
    is exactly the geometry of a printer whose unprintable border is deeper
    than the sheet assumed — the case that clips a corner marker."""
    pdf, manifest = sheet
    manifest["printer_safe_margin_mm"] = 20.0
    report = check_sheet(pdf, manifest)
    assert not report.ok
    violation = next(e for e in report.errors if "safe margin" in e.check)
    assert "from the" in violation.detail and "edge" in violation.detail


@pytest.mark.parametrize("page_count", ["single_page", "multi_page"])
def test_measured_edge_clearance_covers_common_office_printers(sheet, page_count, tmp_path):
    """Office lasers and MFPs — what a university actually prints exams on —
    have a 4-6mm unprintable border. This asserts the real measured figure
    clears that with room to spare, on every page rather than just page 1.

    It is deliberately an assertion about the *measured* clearance, not about
    the constant, because glyph overhang and stroke width both put ink
    outside the coordinate that nominally placed it.
    """
    if page_count == "multi_page":
        config = ExamConfig(
            exam_id="PREFLIGHT_MULTI",
            course_code="CS601",
            exam_name="End-Semester Examination",
            exam_type="endsem",
            num_mcq=10,
            written_questions=[WrittenQuestionConfig(q_no=11 + i, max_marks=5, lines=3) for i in range(10)],
        )
        result = generate_exam(config, tmp_path / "multi")
        pdf, manifest = result["pdf_path"], result["manifest"]
        assert manifest["num_pages"] > 1
    else:
        pdf, manifest = sheet

    report = check_sheet(pdf, manifest)
    clearance = float(report.stats["safe_area"].split("mm")[0].split(":")[1])
    assert clearance >= 8.0, (
        f"only {clearance:.1f}mm of clearance to the nearest page edge - too close for a "
        f"printer with a 4-6mm unprintable border plus feed tolerance. {report.format()}"
    )


def test_fiducials_clear_the_safe_area_by_a_further_margin(sheet):
    """A clipped fiducial is worse than a missing one — the remnant is still
    square enough to detect, and returns a centroid that is quietly wrong.
    So markers sit further in than the general guarantee."""
    _pdf, manifest = sheet
    safe = manifest["printer_safe_margin_mm"]
    w, h = manifest["page"]["width_mm"], manifest["page"]["height_mm"]
    for fid in manifest["fiducials"]:
        half = fid["size_mm"] / 2
        clearance = min(
            fid["x_mm"] - half, fid["y_mm"] - half, w - (fid["x_mm"] + half), h - (fid["y_mm"] + half)
        )
        assert clearance >= safe + 1.5, (
            f"{fid['corner']} outer edge is only {clearance:.1f}mm from the page edge"
        )
