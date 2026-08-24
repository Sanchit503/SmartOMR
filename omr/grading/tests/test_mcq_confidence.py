"""How the MCQ reader behaves on marks that aren't textbook-clean.

The Phase 1 test strategy (draw perfect discs, check they read back) proves
the coordinate plumbing works but says nothing about the case that actually
decides whether a grade is right: a student who shaded lightly, who erased
and re-bubbled, or whose pen slipped into the next option. PROJECT_SPEC.md
principle 4 says those queue for a human instead of being guessed at, so
that's what these pin down.

Bubbles are drawn onto the *real rendered sheet* rather than a blank
canvas, so the printed outline is present exactly as it would be on a scan.
"""
from __future__ import annotations

import numpy as np
import pymupdf
import pytest
from PIL import Image, ImageDraw

from omr.contracts.geometry import mm_to_px, px_per_mm
from omr.generator.config import ExamConfig
from omr.generator.generate import generate_exam
from omr.grading.mcq import Confidence, MCQOutcome, grade_mcq_responses, read_mcq_responses

DPI = 200


@pytest.fixture
def sheet(tmp_path):
    """A rendered 5-MCQ sheet plus a draw handle onto page 1."""
    config = ExamConfig(
        exam_id="CONFIDENCE_TEST",
        course_code="CS301",
        exam_name="Quiz",
        exam_type="quiz",
        num_mcq=5,
        mcq_options=4,
        marks_per_mcq=1,
    )
    result = generate_exam(config, tmp_path)
    page = pymupdf.open(result["pdf_path"])[0]
    pix = page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    return result["manifest"], img


def option_xy(manifest: dict, q_no: int, option_index: int) -> tuple[float, float]:
    entry = next(e for e in manifest["mcq_block"] if e["q_no"] == q_no)
    x = entry["x_mm"] + manifest["mcq_label_offset_mm"] + option_index * manifest["mcq_option_pitch_mm"]
    return x, entry["y_mm"]


def shade(img, manifest, q_no, option_index, coverage=1.0, darkness=0):
    """Shade a bubble the way a pen does: a disc covering `coverage` of the
    sampled area, at grey level `darkness` (0 = solid black)."""
    draw = ImageDraw.Draw(img)
    x_mm, y_mm = option_xy(manifest, q_no, option_index)
    cx, cy = mm_to_px(x_mm, y_mm, DPI)
    r = manifest["bubble_sample_radius_mm"] * px_per_mm(DPI) * (coverage ** 0.5)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=darkness)


def read(img, manifest):
    return {r.q_no: r for r in read_mcq_responses({1: np.array(img)}, manifest, DPI)}


# ---------------------------------------------------------------------------

def test_a_realistically_imperfect_fill_still_reads_as_answered(sheet):
    """Nobody fills a bubble edge to edge. 70% coverage in dark pen is a
    perfectly normal mark and must not need a human."""
    manifest, img = sheet
    shade(img, manifest, 1, 2, coverage=0.7)
    r = read(img, manifest)[1]
    assert r.outcome == MCQOutcome.ANSWERED
    assert r.selected_option == "C"
    assert r.confidence == Confidence.HIGH
    assert not r.needs_human_review


def test_an_untouched_question_reads_blank_with_confidence(sheet):
    manifest, img = sheet
    r = read(img, manifest)[3]
    assert r.outcome == MCQOutcome.BLANK
    assert r.confidence == Confidence.HIGH
    assert not r.needs_human_review
    assert max(r.fill_ratios.values()) == pytest.approx(0.0, abs=0.02)


def test_shifted_printed_grid_is_locally_calibrated(sheet):
    """A perspective correction can leave the MCQ block a few millimetres off
    even after the page is canonical. The reader should use the printed
    bubble grid near the manifest coordinates instead of sampling blank
    space at the old coordinate."""
    manifest, img = sheet
    shade(img, manifest, 1, 2, coverage=0.7)
    shade(img, manifest, 2, 0, coverage=0.7)

    shifted = Image.new("L", img.size, color=255)
    shifted.paste(img, (24, 32))
    readings = read(shifted, manifest)

    assert readings[1].outcome == MCQOutcome.ANSWERED
    assert readings[1].selected_option == "C"
    assert readings[2].outcome == MCQOutcome.ANSWERED
    assert readings[2].selected_option == "A"


def test_a_light_pencil_fill_is_flagged_not_silently_blanked(sheet):
    """The dangerous failure, and the reason `ink_density` exists: a light
    pencil answer covers the whole bubble but never gets dark enough to
    cross the hard fill threshold. On `fill_ratio` alone it scores 0.00 —
    byte-identical to a student who skipped the question — and the student
    silently loses a mark they answered."""
    manifest, img = sheet
    shade(img, manifest, 2, 1, coverage=1.0, darkness=185)
    r = read(img, manifest)[2]

    assert r.fill_ratios["B"] == pytest.approx(0.0, abs=0.01), "expected the hard threshold to miss this"
    assert r.ink_densities["B"] > 0.2, "but mean darkness should still see it"

    assert r.outcome == MCQOutcome.BLANK
    assert r.needs_human_review
    assert r.confidence == Confidence.LOW
    assert "faint or erased" in r.review_reason


def test_erased_and_rebubbled_answer_is_flagged(sheet):
    """Section 13 calls this out explicitly. A rubbed-out pencil mark leaves
    residue behind, so two bubbles read as marked and the reader must not
    just take the darker one and move on."""
    manifest, img = sheet
    shade(img, manifest, 4, 0, coverage=0.30, darkness=90)  # residue of the erased original
    shade(img, manifest, 4, 3, coverage=1.0, darkness=0)   # the intended answer
    r = read(img, manifest)[4]
    assert r.outcome == MCQOutcome.ANSWERED
    assert r.selected_option == "D"
    assert r.needs_human_review
    assert r.confidence in (Confidence.LOW, Confidence.MEDIUM)
    assert "erasure" in r.review_reason


def test_two_fully_filled_bubbles_are_multiple_not_a_pick(sheet):
    manifest, img = sheet
    shade(img, manifest, 5, 0)
    shade(img, manifest, 5, 1)
    r = read(img, manifest)[5]
    assert r.outcome == MCQOutcome.MULTIPLE
    assert r.selected_option is None
    assert r.needs_human_review
    assert r.confidence == Confidence.LOW


def test_confidence_and_review_flag_survive_into_the_grade(sheet):
    """Module 6 refuses to auto-send a sheet carrying a flagged item
    (Section 9), so the flag has to travel with the marks, not stop at the
    reading."""
    manifest, img = sheet
    shade(img, manifest, 1, 0)                              # clean answer
    shade(img, manifest, 2, 0, coverage=0.30, darkness=90)   # erasure residue...
    shade(img, manifest, 2, 2, coverage=1.0)                # ...plus the real answer
    readings = read_mcq_responses({1: np.array(img)}, manifest, DPI)
    grades = {g.q_no: g for g in grade_mcq_responses(readings, {1: "A", 2: "C"}, 1)}

    assert grades[1].marks_awarded == 1
    assert not grades[1].needs_human_review

    # Still scored — the review queue needs a provisional mark to show —
    # but flagged so nothing auto-sends.
    assert grades[2].marks_awarded == 1
    assert grades[2].needs_human_review
    assert grades[2].review_reason

    assert any(g.needs_human_review for g in grades.values())


def test_margin_reports_how_close_the_call_was(sheet):
    manifest, img = sheet
    shade(img, manifest, 1, 0, coverage=1.0)
    r = read(img, manifest)[1]
    assert r.margin > 0.9  # one solid fill against three blanks
