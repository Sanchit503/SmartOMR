"""The flow engine has to keep filling a page until the page is actually full.

The defect these exist to prevent is specific and was real: the generator had
a separate paginating code path that always started the written section on a
fresh page, so a 10-MCQ + 10-two-liner quiz came out as three pages with a
third of page 1 blank. Every arithmetic assertion in the suite passed, because
each individual coordinate was fine — what was wrong was *which page* things
went on.

So these tests assert the two properties that catch that class of bug:

  - a page break happens only when the next question genuinely does not fit
  - no page is ever empty

Neither can be satisfied by a layout that reserves a page per section.
"""
from __future__ import annotations

import pytest

from omr.generator.config import ExamConfig, WrittenQuestionConfig
from omr.generator.flow import LayoutTooTight, flow_sheet
from omr.generator.layout import build_layout
from omr.generator.metrics import (
    BUBBLE_RADIUS_MM,
    PAGE_BOTTOM_MM,
    WRITTEN_GAP_MM,
    content_top_mm,
    max_written_lines_on_a_page,
    mcq_rows_that_fit,
    written_slot_height_mm,
)

OPTIONS = ["A", "B", "C", "D"]


def config(**overrides) -> ExamConfig:
    base = dict(
        exam_id="FLOW_TEST",
        course_code="CS301",
        exam_name="Mid-Semester Examination",
        exam_type="midsem",
        num_mcq=10,
        mcq_options=4,
        marks_per_mcq=1,
        written_questions=[],
    )
    base.update(overrides)
    return ExamConfig(**base)


def two_liners(count: int, start: int = 11) -> list[WrittenQuestionConfig]:
    return [WrittenQuestionConfig(q_no=start + i, max_marks=5, lines=2) for i in range(count)]


def ink_bottom_mm(layout, page: int) -> float:
    """The lowest millimetre anything occupies on a page."""
    lows = [e.y_mm + BUBBLE_RADIUS_MM for e in layout.mcq_entries if e.page == page]
    lows += [e.y_mm + e.height_mm for e in layout.written_entries if e.page == page]
    return max(lows) if lows else content_top_mm(page)


# ---------------------------------------------------------------------------
# The requirement: written answers continue from where the MCQs ended
# ---------------------------------------------------------------------------

def test_written_answers_continue_on_the_same_page_the_mcqs_finished_on():
    """The headline behaviour. Section B must start in the space left on the
    MCQ page, not on a fresh sheet."""
    layout = build_layout(config(num_mcq=10, written_questions=two_liners(10)))

    page1_mcq = [e for e in layout.mcq_entries if e.page == 1]
    page1_written = [e for e in layout.written_entries if e.page == 1]
    assert page1_mcq, "the MCQs belong on page 1"
    assert page1_written, "Section B must continue on page 1, not start on a fresh page"

    last_mcq_row = max(e.y_mm for e in page1_mcq)
    assert min(e.y_mm for e in page1_written) > last_mcq_row, "Section B must follow Section A"


def test_ten_mcqs_and_ten_two_liners_fit_on_two_pages():
    """A regression guard with a number in it. This exam used to take three
    pages: one for the MCQs, then Section B restarted on page 2."""
    layout = build_layout(config(num_mcq=10, written_questions=two_liners(10)))
    assert layout.num_pages == 2


def test_the_page_after_a_break_only_exists_because_nothing_more_fitted():
    """For every written question that opens a page, prove the previous page
    had no room for it. This is the assertion a "one page per section" layout
    cannot pass."""
    layout = build_layout(config(num_mcq=12, written_questions=two_liners(14, start=13)))
    assert layout.num_pages >= 2

    by_page: dict[int, list] = {}
    for e in layout.written_entries:
        by_page.setdefault(e.page, []).append(e)

    for page, entries in by_page.items():
        if page == min(by_page):
            continue
        opener = entries[0]
        available = PAGE_BOTTOM_MM - ink_bottom_mm(layout, page - 1) - WRITTEN_GAP_MM
        assert available < written_slot_height_mm(opener.lines), (
            f"Q{opener.q_no} was pushed to page {page} while page {page - 1} still had "
            f"{available:.1f}mm free and the box needs "
            f"{written_slot_height_mm(opener.lines):.1f}mm"
        )


def test_mcqs_fill_a_page_before_spilling_onto_the_next():
    """The Section A counterpart of the test above: every page an MCQ block
    spans except the last must be packed to its row capacity."""
    layout = build_layout(config(num_mcq=90, written_questions=[]))
    assert layout.num_pages > 1

    for page in range(1, layout.num_pages):
        rows_used = len({e.y_mm for e in layout.mcq_entries if e.page == page})
        capacity = mcq_rows_that_fit(content_top_mm(page))
        assert rows_used == capacity, (
            f"page {page} used {rows_used} of the {capacity} MCQ rows that fit, then spilled "
            "onto the next page anyway"
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(num_mcq=10, written_questions=two_liners(10)),
        dict(num_mcq=1, written_questions=two_liners(1, start=2)),
        dict(num_mcq=60, written_questions=[]),
        dict(num_mcq=0, written_questions=two_liners(20, start=1)),
        dict(num_mcq=100, written_questions=two_liners(12, start=101)),
        dict(
            num_mcq=5,
            written_questions=[WrittenQuestionConfig(q_no=6 + i, max_marks=10, lines=8) for i in range(9)],
        ),
    ],
    ids=["quiz", "tiny", "mcq_only", "written_only", "big", "tall_boxes"],
)
def test_no_page_is_ever_empty(kwargs):
    """A blank page in a scan batch is indistinguishable from a misfeed, and
    the old paginating path could produce one."""
    layout = build_layout(config(**kwargs))
    expected = set(range(1, layout.num_pages + 1))
    occupied = {e.page for e in layout.mcq_entries} | {e.page for e in layout.written_entries}
    assert occupied == expected, f"pages with nothing on them: {sorted(expected - occupied)}"


# ---------------------------------------------------------------------------
# Nothing is lost, duplicated, or off the page
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("num_mcq", [0, 1, 7, 34, 35, 60, 100])
def test_every_mcq_is_placed_exactly_once(num_mcq):
    written = two_liners(2, start=num_mcq + 1)
    layout = build_layout(config(num_mcq=num_mcq, written_questions=written))
    assert sorted(e.q_no for e in layout.mcq_entries) == list(range(1, num_mcq + 1))


@pytest.mark.parametrize("count", [1, 3, 10, 25])
def test_every_written_question_is_placed_exactly_once(count):
    written = two_liners(count)
    layout = build_layout(config(num_mcq=10, written_questions=written))
    assert [e.q_no for e in layout.written_entries] == [w.q_no for w in written]


def test_questions_are_placed_in_reading_order():
    """Page by page, column by column, row by row. A student reading the sheet
    top-to-bottom must meet Q1 before Q2."""
    layout = build_layout(config(num_mcq=40, written_questions=two_liners(6, start=41)))
    keys = [(e.page, e.x_mm, e.y_mm) for e in layout.mcq_entries]
    assert keys == sorted(keys), "MCQs are not in page/column/row order"
    written_keys = [(e.page, e.y_mm) for e in layout.written_entries]
    assert written_keys == sorted(written_keys)


@pytest.mark.parametrize("num_mcq,count", [(10, 10), (60, 4), (0, 15), (34, 0), (35, 1)])
def test_nothing_crosses_the_bottom_of_its_page(num_mcq, count):
    layout = build_layout(config(num_mcq=num_mcq, written_questions=two_liners(count, start=num_mcq + 1)))
    for e in layout.mcq_entries:
        assert e.y_mm + BUBBLE_RADIUS_MM <= PAGE_BOTTOM_MM
        assert e.y_mm - BUBBLE_RADIUS_MM >= content_top_mm(e.page)
    for e in layout.written_entries:
        assert e.y_mm + e.height_mm <= PAGE_BOTTOM_MM
        assert e.y_mm >= content_top_mm(e.page)


def test_written_boxes_never_overlap_each_other():
    layout = build_layout(config(num_mcq=10, written_questions=two_liners(12)))
    by_page: dict[int, list] = {}
    for e in layout.written_entries:
        by_page.setdefault(e.page, []).append(e)
    for page, entries in by_page.items():
        for a, b in zip(entries, entries[1:]):
            assert a.y_mm + a.height_mm <= b.y_mm, f"page {page}: Q{a.q_no} runs into Q{b.q_no}"


def test_sections_never_interleave_across_pages():
    """All of Section A precedes all of Section B, which is how a printed exam
    paper reads and what keeps the parser's job to "read these coordinates"."""
    layout = build_layout(config(num_mcq=60, written_questions=two_liners(8, start=61)))
    assert max(e.page for e in layout.mcq_entries) <= min(e.page for e in layout.written_entries)


# ---------------------------------------------------------------------------
# MCQ column count
# ---------------------------------------------------------------------------

def test_the_mcq_column_count_is_the_same_on_every_page():
    """A student who learns the layout on page 1 must not meet a different one
    on page 2, and the reader gets one `mcq_columns` value for the sheet."""
    layout = build_layout(config(num_mcq=80, written_questions=[]))
    assert layout.num_pages > 1
    per_page = {p: len({e.x_mm for e in layout.mcq_entries if e.page == p})
                for p in range(1, layout.num_pages + 1)}
    assert len(set(per_page.values())) == 1, f"column count differs per page: {per_page}"


def test_more_columns_are_used_when_that_saves_a_page():
    """40 MCQs need 20 rows in two columns, which does not fit on page 1, but
    only 14 rows in three columns, which does."""
    layout = build_layout(config(num_mcq=40, written_questions=[]))
    assert layout.num_pages == 1
    assert layout.mcq_columns == 3


def test_fewest_columns_wins_a_tie_for_more_room_per_question():
    """When the page count is the same either way, take the wider columns:
    more paper between neighbouring bubbles means a stray pen mark is less
    likely to land in another question's read region."""
    layout = build_layout(config(num_mcq=10, written_questions=two_liners(10)))
    assert layout.mcq_columns == 2


def test_options_too_wide_for_multiple_columns_fall_back_to_one():
    """Not reachable through ExamConfig (which caps options at 6), but the
    flow must degrade rather than refuse if that cap ever moves."""
    letters = [chr(ord("A") + i) for i in range(12)]
    result = flow_sheet(config(num_mcq=6, written_questions=[]), letters)
    assert result.mcq_columns == 1
    assert len({e.x_mm for e in result.mcq_entries}) == 1


def test_options_too_wide_for_even_one_column_is_a_clear_error():
    letters = [chr(ord("A") + i) for i in range(26)]
    with pytest.raises(LayoutTooTight, match="mcq_options"):
        flow_sheet(config(num_mcq=6, written_questions=[]), letters)


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

def test_a_written_question_taller_than_a_page_says_what_the_limit_is():
    """"Doesn't fit" is not actionable; "at most 30 lines fit" is."""
    limit = max_written_lines_on_a_page(2)
    cfg = config(
        num_mcq=0,
        written_questions=[WrittenQuestionConfig(q_no=1, max_marks=20, lines=limit + 1)],
    )
    with pytest.raises(LayoutTooTight, match=f"at most {limit}"):
        build_layout(cfg)


def test_the_tallest_box_that_does_fit_is_accepted():
    """The other side of the same boundary — an off-by-one in the fit check
    would silently cost a professor a line of answer space."""
    limit = max_written_lines_on_a_page(1)
    layout = build_layout(
        config(num_mcq=0, written_questions=[WrittenQuestionConfig(q_no=1, max_marks=20, lines=limit)])
    )
    assert layout.num_pages == 1
    assert layout.written_entries[0].lines == limit


def test_an_exam_with_no_questions_at_all_is_rejected():
    with pytest.raises(ValueError, match="no questions"):
        config(num_mcq=0, written_questions=[])
