"""Every page must say who it belongs to, and which page it is.

A multi-page sheet is scanned as a loose batch. If a page carries nothing
that ties it to a student, the only way to attribute it is scan order — a
guess, and CLAUDE.md principle 4 says a guess is not allowed to produce a
grade. So:

  page 1     bubbled roll-number grid   machine-read
  every page handwritten roll strip     human-read, for the review queue
  every page page-index bars            machine-read, and self-checking

These tests hold that shape in place. They are also where the "roll number
on every page" requirement is enforced rather than merely intended.
"""
from __future__ import annotations

import pytest

from omr.contracts.manifest import ManifestError, validate_manifest
from omr.generator.config import ExamConfig, WrittenQuestionConfig
from omr.generator.layout import build_layout
from omr.generator.manifest import build_manifest
from omr.generator.metrics import (
    PAGE_MARK_PITCH_MM,
    PAGE_WIDTH_MM,
    content_top_mm,
    corner_keepouts,
    identity_box,
    orientation_keepout,
)

# Roll numbers that must physically fit the write-in strip (Section 4.2).
BTECH_EXAMPLE = "2024503"
MTECH_EXAMPLE = "MT25001"


def config(pages_worth: int) -> ExamConfig:
    """An exam sized to need roughly `pages_worth` pages."""
    return ExamConfig(
        exam_id="IDENT_TEST",
        course_code="CS301",
        exam_name="Mid-Semester Examination",
        exam_type="midsem",
        num_mcq=10,
        mcq_options=4,
        marks_per_mcq=1,
        written_questions=[
            WrittenQuestionConfig(q_no=11 + i, max_marks=5, lines=2) for i in range(3 + 8 * pages_worth)
        ],
    )


@pytest.fixture(params=[1, 2, 3], ids=["one_page", "two_pages", "three_pages"])
def sheet(request):
    layout = build_layout(config(request.param))
    return layout, build_manifest(layout)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_every_page_carries_a_roll_number_field(sheet):
    """The requirement, asserted directly: no page of any sheet is anonymous."""
    layout, _manifest = sheet
    for page in range(1, layout.num_pages + 1):
        fields = [f for f in layout.write_in_fields if f.page == page and f.name == "roll_number"]
        assert fields, f"page {page} has no roll-number field"


def test_every_page_carries_a_name_field(sheet):
    layout, _manifest = sheet
    for page in range(1, layout.num_pages + 1):
        assert any(
            f.page == page and f.name == "student_name" for f in layout.write_in_fields
        ), f"page {page} has no name field"


def test_the_roll_strip_holds_a_btech_and_an_mtech_roll_number(sheet):
    """BTech is 7 digits; MTech is "MT" + 5 digits, which is also 7 characters.
    One 7-cell strip serves both, which is why it is 7 and not 5."""
    layout, _manifest = sheet
    assert len(BTECH_EXAMPLE) == len(MTECH_EXAMPLE) == 7
    for page in range(2, layout.num_pages + 1):
        strip = next(f for f in layout.write_in_fields if f.page == page and f.name == "roll_number")
        assert strip.cells >= 7, f"page {page}'s roll strip has only {strip.cells} cells"


def test_only_page_one_carries_the_bubbled_grid(sheet):
    """Repeating the grid would cost ~85mm of every page and ask a student to
    bubble the same digits again — each repeat being a fresh chance to
    contradict page 1, which is a review item, not an improvement."""
    layout, manifest = sheet
    assert layout.roll_block.page == 1
    assert manifest["roll_number_block"]["page"] == 1


def test_the_identity_block_never_reaches_into_the_question_area(sheet):
    layout, _manifest = sheet
    for page in range(1, layout.num_pages + 1):
        _top, bottom = identity_box(page)
        assert bottom < content_top_mm(page)
        for e in (e for e in layout.mcq_entries if e.page == page):
            assert e.y_mm > bottom
        for e in (e for e in layout.written_entries if e.page == page):
            assert e.y_mm > bottom
        for f in (f for f in layout.write_in_fields if f.page == page):
            assert f.y_mm + f.height_mm <= bottom + 1e-6


# ---------------------------------------------------------------------------
# Page-index bars
# ---------------------------------------------------------------------------

def test_each_page_prints_one_bar_per_page_with_its_own_filled(sheet):
    layout, _manifest = sheet
    for page in range(1, layout.num_pages + 1):
        bars = [m for m in layout.page_marks if m.page == page]
        assert len(bars) == layout.num_pages
        filled = [m for m in bars if m.filled]
        assert [m.index for m in filled] == [page]


def test_bars_read_left_to_right_so_a_human_can_count_them(sheet):
    layout, _manifest = sheet
    bars = sorted((m for m in layout.page_marks if m.page == 1), key=lambda m: m.index)
    xs = [m.x_mm for m in bars]
    assert xs == sorted(xs), "bar 1 must be leftmost"
    for a, b in zip(bars, bars[1:]):
        assert b.x_mm - a.x_mm == pytest.approx(PAGE_MARK_PITCH_MM)


def test_bars_stay_clear_of_every_marker_keepout(sheet):
    """The bars sit on the same row as the corner markers. If one strayed into
    a keep-out, a detector could merge it with a marker and compute the wrong
    corner — which skews every coordinate on the sheet."""
    layout, _manifest = sheet
    zones = corner_keepouts() + [orientation_keepout()]
    for m in layout.page_marks:
        x0, x1 = m.x_mm - m.width_mm / 2, m.x_mm + m.width_mm / 2
        y0, y1 = m.y_mm - m.height_mm / 2, m.y_mm + m.height_mm / 2
        for (zx0, zy0, zx1, zy1) in zones:
            overlaps = x0 < zx1 and x1 > zx0 and y0 < zy1 and y1 > zy0
            assert not overlaps, f"page-index bar {m.index} overlaps a marker keep-out"
        assert 0 < x0 and x1 < PAGE_WIDTH_MM


def test_bars_are_bars_not_squares(sheet):
    """A marker detector rejects candidates by squareness. If a bar were
    square it would become a fifth "orientation marker" candidate sitting on
    the same row as the real one."""
    _layout, manifest = sheet
    for m in manifest["page_marks"]:
        assert m["width_mm"] / m["height_mm"] >= 2.0


def test_a_long_sheet_does_not_run_its_bars_into_the_content(sheet):
    """The strip grows leftward from a fixed right anchor, so its length is
    bounded by how far left it may reach."""
    layout = build_layout(config(6))
    assert layout.num_pages >= 5
    leftmost = min(m.x_mm - m.width_mm / 2 for m in layout.page_marks)
    _ox0, _oy0, ox1, _oy1 = orientation_keepout()
    assert leftmost > ox1, "the bar strip has grown into the orientation marker's quiet zone"


# ---------------------------------------------------------------------------
# The schema refuses a sheet that breaks any of this
# ---------------------------------------------------------------------------

def test_a_page_with_no_roll_field_is_rejected(sheet):
    _layout, manifest = sheet
    if manifest["num_pages"] < 2:
        pytest.skip("needs a continuation page to strip")
    manifest["write_in_fields"] = [
        f for f in manifest["write_in_fields"] if not (f["page"] == 2 and f["name"] == "roll_number")
    ]
    with pytest.raises(ManifestError, match="unattributable"):
        validate_manifest(manifest)


def test_a_page_with_no_questions_on_it_is_rejected(sheet):
    """Emptying the last page is the shape the old paginating layout could
    actually produce: registration marks and an identity strip, and nothing
    to answer."""
    _layout, manifest = sheet
    last = manifest["num_pages"]
    manifest["mcq_block"] = [e for e in manifest["mcq_block"] if e["page"] != last]
    manifest["written_block"] = [e for e in manifest["written_block"] if e["page"] != last]
    with pytest.raises(ManifestError, match="no questions"):
        validate_manifest(manifest)


def test_a_page_whose_bar_reports_the_wrong_index_is_rejected(sheet):
    _layout, manifest = sheet
    if manifest["num_pages"] < 2:
        pytest.skip("a one-page sheet has only one bar to mislabel")
    for m in manifest["page_marks"]:
        if m["page"] == 1:
            m["filled"] = m["index"] == 2
    with pytest.raises(ManifestError, match="misreports its own number"):
        validate_manifest(manifest)


def test_a_page_with_two_filled_bars_is_rejected(sheet):
    _layout, manifest = sheet
    if manifest["num_pages"] < 2:
        pytest.skip("needs at least two bars")
    for m in manifest["page_marks"]:
        if m["page"] == 1:
            m["filled"] = True
    with pytest.raises(ManifestError, match="checksum"):
        validate_manifest(manifest)


def test_a_page_missing_its_bars_entirely_is_rejected(sheet):
    _layout, manifest = sheet
    manifest["page_marks"] = [m for m in manifest["page_marks"] if m["page"] != 1]
    with pytest.raises(ManifestError, match="page-index bars"):
        validate_manifest(manifest)
