"""Vertical numerical packing and the PDF/manifest contract."""
from __future__ import annotations

from itertools import combinations

import pymupdf
import pytest

from omr.generator.config import ExamConfig, NumericalQuestionConfig, WrittenQuestionConfig
from omr.generator.generate import generate_exam
from omr.generator.layout import build_layout
from omr.generator.manifest import build_manifest
from omr.generator.metrics import (
    MARGIN_MM, NUMERICAL_COLUMN_GAP_MM, NUMERICAL_GRID_OFFSET_Y_MM,
    PAGE_BOTTOM_MM, PAGE_WIDTH_MM, content_top_mm, numerical_grid_offset_x_mm,
    numerical_slot_height_mm, numerical_slot_width_mm,
)
from omr.generator.preflight import check_sheet
from omr.verify import verify_sheet


def config_for(digits, *, mcqs=0, written=0):
    return ExamConfig(
        exam_id="VERTICAL_NUMERICAL_TEST", course_code="TEST", exam_name="Numerical Quiz",
        exam_type="quiz", num_mcq=mcqs,
        numerical_questions=[
            NumericalQuestionConfig(q_no=mcqs + i, digits=count, max_marks=1)
            for i, count in enumerate(digits, 1)
        ],
        written_questions=[
            WrittenQuestionConfig(q_no=mcqs + len(digits) + i, max_marks=2, lines=2)
            for i in range(1, written + 1)
        ],
    )


@pytest.mark.parametrize("digits", [1, 2, 3])
def test_four_questions_across_and_eight_on_first_page(digits):
    layout = build_layout(config_for([digits] * 9))
    entries = layout.numerical_entries
    assert [e.page for e in entries] == [1] * 8 + [2]
    assert len({e.y_mm for e in entries[:4]}) == 1
    assert len({e.y_mm for e in entries[4:8]}) == 1
    for first, second in zip(entries[:3], entries[1:4]):
        assert second.x_mm - first.x_mm == pytest.approx(42 + NUMERICAL_COLUMN_GAP_MM)


@pytest.mark.parametrize("digits", [list(range(1, 9)), list(range(8, 0, -1))] + [[d] * 25 for d in range(1, 9)])
def test_variable_widths_do_not_overlap_or_split_questions(digits):
    layout = build_layout(config_for(digits, mcqs=10, written=3))
    manifest = build_manifest(layout)
    assert len(manifest["numerical_block"]) == len(digits)
    assert [e.q_no for e in layout.numerical_entries] == list(range(11, 11 + len(digits)))
    assert {e.page for e in layout.mcq_entries + layout.numerical_entries + layout.written_entries} == set(range(1, layout.num_pages + 1))
    slots = []
    for e in layout.numerical_entries:
        left = e.x_mm - numerical_grid_offset_x_mm(e.positions)
        top = e.y_mm - NUMERICAL_GRID_OFFSET_Y_MM
        right = left + numerical_slot_width_mm(e.positions)
        bottom = top + numerical_slot_height_mm(e.positions)
        assert left >= MARGIN_MM
        assert right <= PAGE_WIDTH_MM - MARGIN_MM + 1e-6
        assert top >= content_top_mm(e.page)
        assert bottom <= PAGE_BOTTOM_MM + 1e-6
        slots.append((e.page, left, top, right, bottom))
    for a, b in combinations(slots, 2):
        if a[0] == b[0]:
            assert a[3] <= b[1] or b[3] <= a[1] or a[4] <= b[2] or b[4] <= a[2]
    last = layout.numerical_entries[-1]
    first_written = layout.written_entries[0]
    assert first_written.page >= last.page
    if first_written.page == last.page:
        assert first_written.y_mm > last.y_mm + 9 * last.digit_pitch_mm


@pytest.mark.parametrize("digits", [[2] * 9, list(range(1, 9))])
def test_printed_vertical_grids_preflight_and_round_trip(tmp_path, digits):
    result = generate_exam(config_for(digits, mcqs=10, written=3), tmp_path)
    assert result["manifest"]["bubble_radius_mm"] == 1.75
    assert result["manifest"]["bubble_sample_radius_mm"] == pytest.approx(1.26)
    report = check_sheet(result["pdf_path"], result["manifest"])
    assert report.ok, report.format()
    for seed in (0, 42):
        verification = verify_sheet(result["manifest_path"], seed=seed)
        assert verification.ok, verification.format()
        assert verification.recovered == 10 + len(digits)


def test_place_headers_fit_columns_without_changing_bubble_size_or_pitch(tmp_path):
    result = generate_exam(config_for(list(range(1, 9))), tmp_path)
    labels_by_place = [
        ["Ones"], ["Tens"], ["Hundreds"], ["Thous."],
        ["Ten", "thous."], ["Hundred", "thous."], ["Millions"], ["Ten", "millions"],
    ]
    scale = 72 / 25.4
    with pymupdf.open(result["pdf_path"]) as document:
        for entry in result["manifest"]["numerical_block"]:
            page = document[entry["page"] - 1]
            x, y, positions = entry["x_mm"], entry["y_mm"], entry["positions"]
            top = y - NUMERICAL_GRID_OFFSET_Y_MM
            for position in range(positions):
                cx = x + position * entry["position_pitch_mm"]
                region = pymupdf.Rect((cx - 4.5) * scale, (top + 3) * scale,
                                      (cx + 4.5) * scale, (y - 2) * scale)
                words = page.get_text("words", clip=region)
                assert [word[4] for word in words] == labels_by_place[positions - position - 1]
                assert all(word[0] > (cx - 4.5) * scale and word[2] < (cx + 4.5) * scale for word in words)
                assert all(word[3] < (y - 2) * scale for word in words), "header touches a bubble"
            assert entry["position_pitch_mm"] == 9.0
            assert NUMERICAL_GRID_OFFSET_Y_MM == 12.0
            label_lane = pymupdf.Rect((x - 9) * scale, (y - 2) * scale,
                                     (x - 3) * scale, (y + 9 * entry["digit_pitch_mm"] + 2) * scale)
            assert [word[4] for word in page.get_text("words", clip=label_lane)] == list("0123456789")
            circles = [d for d in page.get_drawings() if d["items"] and all(i[0] == "c" for i in d["items"])
                       and x - 2 <= d["rect"].x0 / scale <= x + (positions - 1) * entry["position_pitch_mm"]
                       and y - 2 <= d["rect"].y0 / scale <= y + 9 * entry["digit_pitch_mm"]]
            assert len(circles) == positions * 10
            for circle in circles:
                assert circle["rect"].width / scale == pytest.approx(3.5, abs=0.01)
                assert circle["rect"].height / scale == pytest.approx(3.5, abs=0.01)
