"""The round-trip verifier has to be able to fail.

A verifier that always reports success is worse than none, because it turns
an unchecked sheet into one someone believes was checked. So alongside the
happy path, these drive it with marks too faint to read and with a manifest
that disagrees with the PDF, and assert it says so.
"""
from __future__ import annotations

import pytest

from omr.generator.config import ExamConfig, WrittenQuestionConfig
from omr.generator.generate import generate_exam
from omr.verify import verify_sheet


def build(tmp_path, **overrides):
    base = dict(
        exam_id="VERIFY_TEST",
        course_code="CS301",
        exam_name="Mid-Semester Examination",
        exam_type="midsem",
        num_mcq=20,
        mcq_options=4,
        marks_per_mcq=2,
        written_questions=[WrittenQuestionConfig(q_no=21, max_marks=5, lines=2)],
    )
    base.update(overrides)
    result = generate_exam(ExamConfig(**base), tmp_path)
    return result["manifest_path"], result["pdf_path"]


def test_a_normal_pen_mark_round_trips_completely(tmp_path):
    manifest_path, _ = build(tmp_path)
    result = verify_sheet(manifest_path)
    assert result.ok, result.format()
    assert result.recovered == result.total_questions == 20
    assert result.marks_awarded == 40  # 20 questions x 2 marks, all correct
    assert not result.flagged


def test_signal_separation_leaves_real_headroom(tmp_path):
    """The gap between the faintest deliberate fill and the darkest untouched
    bubble is the tolerance a real scan gets to eat into."""
    manifest_path, _ = build(tmp_path)
    result = verify_sheet(manifest_path)
    assert result.max_empty_ratio == pytest.approx(0.0, abs=0.02)
    assert result.separation > 0.5, result.format()


@pytest.mark.parametrize("seed", [0, 1, 7, 42])
def test_round_trip_holds_whichever_options_get_filled(tmp_path, seed):
    manifest_path, _ = build(tmp_path)
    result = verify_sheet(manifest_path, seed=seed)
    assert result.ok, result.format()


def test_it_round_trips_a_multi_page_sheet(tmp_path):
    """Each question must be read from its own page's image; a multi-page
    sheet is where that can go wrong."""
    written = [WrittenQuestionConfig(q_no=11 + i, max_marks=5, lines=3) for i in range(10)]
    manifest_path, _ = build(tmp_path, exam_id="VERIFY_MULTI", num_mcq=10, written_questions=written)
    result = verify_sheet(manifest_path)
    assert result.num_pages > 1
    assert result.ok, result.format()


def test_a_mark_too_faint_to_read_is_reported_not_passed(tmp_path):
    """This is the check that proves the verifier discriminates at all."""
    manifest_path, _ = build(tmp_path)
    result = verify_sheet(manifest_path, coverage=0.3, darkness=160)
    assert not result.ok
    assert result.recovered == 0
    assert "MISMATCH" in result.format()
    # ...but every one is still flagged for a human rather than silently lost.
    assert len(result.flagged) == result.total_questions


def test_an_mcq_free_sheet_reports_nothing_to_do_rather_than_passing(tmp_path):
    manifest_path, _ = build(
        tmp_path,
        exam_id="VERIFY_WRITTEN_ONLY",
        num_mcq=0,
        written_questions=[WrittenQuestionConfig(q_no=1, max_marks=10, lines=4)],
    )
    result = verify_sheet(manifest_path)
    assert result.total_questions == 0
    assert "nothing to round-trip" in result.format()


def test_a_manifest_whose_pdf_is_missing_fails_loudly(tmp_path):
    manifest_path, pdf_path = build(tmp_path)
    pdf_path.unlink()
    with pytest.raises(FileNotFoundError):
        verify_sheet(manifest_path)


def test_a_uniform_manifest_drift_is_explicitly_not_what_this_catches(tmp_path):
    """Documents a real limit instead of pretending it away.

    The verifier fills bubbles at the manifest's coordinates and reads them
    back at the same coordinates, so a manifest that has drifted uniformly
    away from the printed sheet still round-trips perfectly. Manifest-vs-PDF
    agreement is preflight's job — a drifted coordinate lands on printed ink
    or bare paper where a bubble should be, and its empty-bubble check sees
    that. This tool checks the generate -> read -> grade path agrees with
    itself on your config; the two are complementary, and neither alone is
    sufficient.
    """
    import json

    manifest_path, pdf_path = build(tmp_path)
    manifest = json.loads(manifest_path.read_text())
    for entry in manifest["mcq_block"]:
        entry["y_mm"] += 4.0  # half a row out of alignment with the print
    manifest_path.write_text(json.dumps(manifest))

    assert verify_sheet(manifest_path).ok, "round-trip is self-consistent by construction"

    # The same drift, pushed onto printed ink, is caught by preflight.
    from omr.generator.preflight import check_sheet

    for entry in manifest["mcq_block"]:
        entry["y_mm"] -= 8.5  # onto the option-letter header row
    report = check_sheet(pdf_path, manifest)
    assert not report.ok, report.format()
