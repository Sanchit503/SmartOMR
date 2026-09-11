"""Continuous flow: place every question top-to-bottom, page after page.

There is exactly one placement algorithm here, and it is used for every
exam. That is the point of the file. The generator used to have two — a
"try to fit it all on one page" path and a separate paginating path — and
they disagreed about something important: the paginating path always started
the written section on a *fresh* page. A 10-MCQ + 10-two-liner quiz came out
as three pages with a third of page 1 left blank, purely because the layout
engine took a different branch once the exam stopped fitting on one sheet.

So: one cursor walks down page 1, then page 2, and so on. Section A (MCQs)
is placed first, numerical answers second, and written answers continue
from wherever the preceding section ended — on the same page if there is
room, on the next page if there isn't. A page break happens only when the next thing genuinely does
not fit, which makes an empty or half-empty page structurally impossible
rather than something to remember not to produce.

Two things this deliberately does NOT do:

*Interleave sections.* All MCQs precede all numerical grids, which precede
all written answers. A page break never splits one grid or answer box. That
mirrors how a real exam paper reads and keeps the parser's job to
"read the coordinates the manifest gives you".

*Vary the MCQ column count per page.* The count is chosen once for the whole
sheet, by running the full flow for each candidate and keeping the one that
needs the fewest pages — ties going to the FEWEST columns, because fewer
columns means more horizontal room per question, which keeps neighbouring
bubbles further apart and makes a stray pen mark less likely to land in
another question's read region.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .metrics import (
    BUBBLE_RADIUS_MM,
    EPS,
    MARGIN_MM,
    MCQ_COLUMN_CANDIDATES,
    MCQ_ROW_PITCH_MM,
    NUMERICAL_COLUMN_GAP_MM,
    NUMERICAL_GRID_OFFSET_X_MM,
    NUMERICAL_GRID_OFFSET_Y_MM,
    NUMERICAL_DIGIT_PITCH_MM,
    NUMERICAL_POSITION_PITCH_MM,
    NUMERICAL_QUESTION_GAP_MM,
    NUMERICAL_SECTION_HEADER_MM,
    PAGE_BOTTOM_MM,
    SECTION_GAP_MM,
    SECTION_HEADER_MM,
    WRITTEN_GAP_MM,
    WRITTEN_HEADER_MM,
    content_top_mm,
    max_written_lines_on_a_page,
    mcq_block_bottom_mm,
    mcq_first_row_y_mm,
    mcq_rows_that_fit,
    min_mcq_column_width_mm,
    numerical_slot_width_mm,
    numerical_slot_height_mm,
    usable_width_mm,
    written_box_height_mm,
    written_slot_height_mm,
)


@dataclass(frozen=True)
class MCQEntry:
    """One MCQ row. `x_mm` is the row's left edge (where "Q7" is printed);
    the option bubble centers are derived from it at draw and parse time via
    the manifest's `mcq_label_offset_mm` and `mcq_option_pitch_mm`."""

    q_no: int
    x_mm: float
    y_mm: float
    options: list[str]
    page: int = 1


@dataclass(frozen=True)
class WrittenEntry:
    """One ruled answer box. `y_mm` is its TOP edge; the "Qn [k marks]" label
    sits in the WRITTEN_HEADER_MM band above it."""

    q_no: int
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    max_marks: float
    lines: int
    page: int = 1


@dataclass(frozen=True)
class NumericalEntry:
    q_no: int
    x_mm: float
    y_mm: float
    positions: int
    digit_pitch_mm: float
    position_pitch_mm: float
    max_marks: float
    page: int = 1


@dataclass
class FlowResult:
    mcq_entries: list[MCQEntry] = field(default_factory=list)
    written_entries: list[WrittenEntry] = field(default_factory=list)
    num_pages: int = 1
    mcq_columns: int = 0
    numerical_entries: list[NumericalEntry] = field(default_factory=list)

    def pages_with_content(self) -> set[int]:
        return {e.page for e in self.mcq_entries + self.numerical_entries + self.written_entries}


class LayoutTooTight(ValueError):
    """The exam cannot be laid out on A4 at all — not "needs more pages", but
    "one of these questions does not fit on a page of its own"."""


@dataclass
class _Cursor:
    """Where the next block goes. `y` is the first free millimetre on `page`."""

    page: int = 1
    y: float = 0.0

    def next_page(self) -> None:
        self.page += 1
        self.y = content_top_mm(self.page)


def _place_mcqs(cur: _Cursor, num_mcq: int, option_letters: list[str], columns: int) -> list[MCQEntry]:
    """Fill the space left on the current page, then continue on the next.

    Within a page the fill is column-major — Q1..Q5 down the left column,
    Q6..Q10 down the right — which is the order a student reads and the order
    a printed answer sheet is conventionally packed.
    """
    entries: list[MCQEntry] = []
    if num_mcq == 0:
        return entries

    col_width = usable_width_mm() / columns
    remaining = list(range(1, num_mcq + 1))

    while remaining:
        rows_available = mcq_rows_that_fit(cur.y)
        if rows_available < 1:
            cur.next_page()
            continue

        take = min(len(remaining), rows_available * columns)
        rows_used = math.ceil(take / columns)
        y0 = mcq_first_row_y_mm(cur.y)
        for i, q_no in enumerate(remaining[:take]):
            col, row = divmod(i, rows_used)
            entries.append(
                MCQEntry(
                    q_no=q_no,
                    x_mm=MARGIN_MM + col * col_width,
                    y_mm=y0 + row * MCQ_ROW_PITCH_MM,
                    options=option_letters,
                    page=cur.page,
                )
            )
        cur.y = mcq_block_bottom_mm(cur.y, rows_used)
        remaining = remaining[take:]
        if remaining:
            cur.next_page()

    return entries


def _place_written(cur: _Cursor, written_questions, after_content: bool) -> list[WrittenEntry]:
    """Continue from wherever the preceding objective section ended.

    A written box goes on the current page if it fits
    there, and only then on the next one."""
    entries: list[WrittenEntry] = []
    if not written_questions:
        return entries

    if after_content:
        cur.y += SECTION_GAP_MM

    width = usable_width_mm()
    # The section label is reprinted at the top of every page the section
    # spans ("Section B - Written Answers (continued)"), so its band has to be
    # reserved again after each break.
    needs_section_header = True

    for wq in written_questions:
        slot_h = written_slot_height_mm(wq.lines)
        header_h = SECTION_HEADER_MM if needs_section_header else 0.0

        if cur.y + header_h + slot_h > PAGE_BOTTOM_MM + EPS:
            cur.next_page()
            needs_section_header = True
            if cur.y + SECTION_HEADER_MM + slot_h > PAGE_BOTTOM_MM + EPS:
                limit = max_written_lines_on_a_page(cur.page)
                raise LayoutTooTight(
                    f"written question Q{wq.q_no} asks for {wq.lines} lines, but at most "
                    f"{limit} fit on an A4 page. Reduce its line count, or split it into "
                    "two questions."
                )
            header_h = SECTION_HEADER_MM

        cur.y += header_h
        needs_section_header = False

        entries.append(
            WrittenEntry(
                q_no=wq.q_no,
                x_mm=MARGIN_MM,
                y_mm=cur.y + WRITTEN_HEADER_MM,
                width_mm=width,
                height_mm=written_box_height_mm(wq.lines),
                max_marks=wq.max_marks,
                lines=wq.lines,
                page=cur.page,
            )
        )
        cur.y += slot_h + WRITTEN_GAP_MM

    return entries


def _place_numerical(cur: _Cursor, questions, after_mcqs: bool) -> list[NumericalEntry]:
    entries: list[NumericalEntry] = []
    if not questions:
        return entries
    if after_mcqs:
        cur.y += SECTION_GAP_MM
    remaining = list(questions)
    needs_header = True
    slots_per_row = 2
    while remaining:
        header = NUMERICAL_SECTION_HEADER_MM if needs_header else 0.0
        row = remaining[:slots_per_row]
        row_height = max(numerical_slot_height_mm(question.digits) for question in row)
        if cur.y + header + row_height > PAGE_BOTTOM_MM + EPS:
            cur.next_page()
            needs_header = True
            continue
        cur.y += header
        needs_header = False
        for column, question in enumerate(row):
            width = numerical_slot_width_mm(question.digits)
            x = MARGIN_MM + column * (width + NUMERICAL_COLUMN_GAP_MM)
            entries.append(NumericalEntry(
                q_no=question.q_no,
                x_mm=x + NUMERICAL_GRID_OFFSET_X_MM,
                y_mm=cur.y + NUMERICAL_GRID_OFFSET_Y_MM,
                positions=question.digits,
                digit_pitch_mm=NUMERICAL_DIGIT_PITCH_MM,
                position_pitch_mm=NUMERICAL_POSITION_PITCH_MM,
                max_marks=question.max_marks,
                page=cur.page,
            ))
        remaining = remaining[len(row):]
        cur.y += row_height + NUMERICAL_QUESTION_GAP_MM
    return entries


def _flow_with(config, option_letters: list[str], columns: int) -> FlowResult | None:
    """One full pass at `columns` MCQ columns. None if the columns are too
    narrow for this many options to fit side by side."""
    if config.num_mcq and usable_width_mm() / columns < min_mcq_column_width_mm(len(option_letters)):
        return None

    cur = _Cursor(page=1, y=content_top_mm(1))
    mcq_entries = _place_mcqs(cur, config.num_mcq, option_letters, columns)
    numerical_entries = _place_numerical(cur, config.numerical_questions, after_mcqs=bool(mcq_entries))
    written_entries = _place_written(cur, config.written_questions, after_content=bool(mcq_entries or numerical_entries))
    return FlowResult(
        mcq_entries=mcq_entries,
        written_entries=written_entries,
        num_pages=cur.page,
        mcq_columns=columns if config.num_mcq else 0,
        numerical_entries=numerical_entries,
    )


def flow_sheet(config, option_letters: list[str]) -> FlowResult:
    """Lay out the whole exam, choosing the MCQ column count for it.

    Candidates are evaluated by running the real flow, not by estimating,
    because the number of pages depends on how the MCQ block's height
    interacts with the written boxes that follow it on the same page — the
    exact coupling a formula would get wrong.
    """
    candidates = list(MCQ_COLUMN_CANDIDATES)
    if not config.num_mcq:
        candidates = [candidates[0]]  # column count is irrelevant with no MCQs

    best: FlowResult | None = None
    for columns in candidates:
        result = _flow_with(config, option_letters, columns)
        if result is None:
            continue
        if best is None or (result.num_pages, columns) < (best.num_pages, best.mcq_columns):
            best = result

    if best is None:
        # Every multi-column layout was too narrow. A single column always
        # fits within A4's usable width for the option counts ExamConfig
        # permits, so this is the honest fallback rather than an error.
        best = _flow_with(config, option_letters, 1)
    if best is None:
        raise LayoutTooTight(
            f"{len(option_letters)} MCQ options are too wide to lay out on an A4 page even in a "
            "single column. Reduce mcq_options."
        )
    return best
