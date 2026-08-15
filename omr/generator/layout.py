"""Manifest-driven sheet layout engine (Section 4.2-4.4 of CLAUDE.md).

This module is the single source of truth for where every fiducial, bubble,
and answer box lives on the page. Both `pdf_gen.render_pdf` (what gets
printed) and `manifest.build_manifest` (what the parser reads) consume the
same `SheetLayout` object, so the two can never drift apart (Section 2,
principle 1).

All coordinates are millimeters measured from the TOP-LEFT of the page
(x increases right, y increases DOWN) — this matches image/pixel
conventions so the parser can convert mm -> px with a single scale factor,
independent of print DPI.

Pagination: an exam is first tried on a single page (the common case for a
typical quiz/midsem). If the MCQs and written questions don't fit together,
the sheet spills onto as many pages as it needs: MCQs fill page(s) using the
same column-packing logic as the single-page case, then the written section
always starts on a fresh page and greedily bin-packs its (variably sized)
answer boxes across as many pages as it needs. Sections never interleave
across a page break — that mirrors how a real multi-page exam paper is laid
out (Section A complete, then Section B), and keeps parsing simple (a page
is either an MCQ page or a written page, never a jumbled mix, except for the
single-page case where both sections legitimately share one page).

Only page 1 carries the roll-number identity block — repeating the full
bubble grid on every page would eat significant space for no real benefit,
since exam pages are physically stapled together and every page still
carries the exam name/course/page-number in its header for manual
reassembly if pages ever get separated. Fiducials, on the other hand, are
printed on every page (per Section 4.3) since each physical page is
independently deskewed at scan time.
"""
from __future__ import annotations

import math
import string
from dataclasses import dataclass, field

from ..contracts.geometry import A4_HEIGHT_MM, A4_WIDTH_MM

# Page size comes from the shared contract; every other constant below is a
# layout *decision* this module owns and publishes through the manifest, so
# the reader learns it at parse time instead of sharing the constant.
PAGE_WIDTH_MM = A4_WIDTH_MM
PAGE_HEIGHT_MM = A4_HEIGHT_MM
MARGIN_MM = 10.0

FIDUCIAL_INSET_MM = 10.0
FIDUCIAL_SIZE_MM = 8.0

BUBBLE_RADIUS_MM = 1.8

DIGIT_COL_PITCH_MM = 10.0
DIGIT_ROW_PITCH_MM = 6.0
ROLL_BLOCK_TOP_MM = 52.0

# Page 1 carries the header + roll-number block, so its content area starts
# lower than a continuation page, which only carries the header.
PAGE1_CONTENT_START_MM = 126.0
CONTINUATION_CONTENT_START_MM = 45.0

SECTION_HEADER_MM = 8.0
MCQ_ROW_PITCH_MM = 8.0
MCQ_OPTION_PITCH_MM = 8.0
MCQ_LABEL_OFFSET_MM = 12.0

WRITTEN_HEADER_MM = 6.0
WRITTEN_LINE_MM = 6.0
WRITTEN_BOX_PADDING_MM = 4.0
WRITTEN_GAP_MM = 6.0

BOTTOM_MARGIN_MM = 16.0  # keeps content clear of the bottom fiducials
PAGE_BOTTOM_MM = PAGE_HEIGHT_MM - BOTTOM_MARGIN_MM


@dataclass(frozen=True)
class Fiducial:
    page: int
    corner: str  # TL, TR, BL, BR
    x_mm: float
    y_mm: float


@dataclass(frozen=True)
class DigitGrid:
    columns: int
    x_mm: float
    y_mm: float
    col_pitch_mm: float
    row_pitch_mm: float


@dataclass(frozen=True)
class RollBlockLayout:
    page: int
    program_selector: dict[str, tuple[float, float]]
    btech_digits: DigitGrid
    mtech_digits: DigitGrid


@dataclass(frozen=True)
class MCQEntry:
    q_no: int
    x_mm: float
    y_mm: float
    options: list[str]
    page: int = 1


@dataclass(frozen=True)
class WrittenEntry:
    q_no: int
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    max_marks: float
    lines: int
    page: int = 1


@dataclass(frozen=True)
class SheetLayout:
    exam_id: str
    course_code: str
    exam_name: str
    fiducials: list[Fiducial]
    roll_block: RollBlockLayout
    mcq_entries: list[MCQEntry]
    written_entries: list[WrittenEntry]
    mcq_options: int = field(default=4)
    num_pages: int = field(default=1)


def _page_fiducials(page: int) -> list[Fiducial]:
    return [
        Fiducial(page, "TL", FIDUCIAL_INSET_MM, FIDUCIAL_INSET_MM),
        Fiducial(page, "TR", PAGE_WIDTH_MM - FIDUCIAL_INSET_MM, FIDUCIAL_INSET_MM),
        Fiducial(page, "BL", FIDUCIAL_INSET_MM, PAGE_HEIGHT_MM - FIDUCIAL_INSET_MM),
        Fiducial(page, "BR", PAGE_WIDTH_MM - FIDUCIAL_INSET_MM, PAGE_HEIGHT_MM - FIDUCIAL_INSET_MM),
    ]


def _build_roll_block() -> RollBlockLayout:
    return RollBlockLayout(
        page=1,
        program_selector={"BTECH": (25.0, 42.0), "MTECH": (70.0, 42.0)},
        btech_digits=DigitGrid(7, 25.0, ROLL_BLOCK_TOP_MM, DIGIT_COL_PITCH_MM, DIGIT_ROW_PITCH_MM),
        mtech_digits=DigitGrid(5, 120.0, ROLL_BLOCK_TOP_MM, DIGIT_COL_PITCH_MM, DIGIT_ROW_PITCH_MM),
    )


def _written_box_height(lines: int) -> float:
    return WRITTEN_HEADER_MM + lines * WRITTEN_LINE_MM + WRITTEN_BOX_PADDING_MM


def _min_col_width(num_options: int) -> float:
    return MCQ_LABEL_OFFSET_MM + (num_options - 1) * MCQ_OPTION_PITCH_MM + 10.0


def _content_start(page: int) -> float:
    return PAGE1_CONTENT_START_MM if page == 1 else CONTINUATION_CONTENT_START_MM


def _try_single_page(config, option_letters: list[str], usable_width: float):
    """Attempt the original all-on-page-1 layout. Returns (mcq_entries,
    written_entries) on success, or None if it doesn't fit — the caller
    falls back to pagination rather than erroring."""
    written_box_heights = [_written_box_height(wq.lines) for wq in config.written_questions]
    written_total_height = sum(h + WRITTEN_GAP_MM for h in written_box_heights)
    if config.written_questions:
        written_total_height += SECTION_HEADER_MM

    total_available = PAGE_BOTTOM_MM - PAGE1_CONTENT_START_MM
    mcq_available = total_available - written_total_height
    if config.num_mcq:
        mcq_available -= SECTION_HEADER_MM

    mcq_entries: list[MCQEntry] = []
    mcq_block_bottom = PAGE1_CONTENT_START_MM

    if config.num_mcq:
        chosen_columns = None
        rows_needed = 0
        for num_columns in (2, 3, 4):
            rows_needed = math.ceil(config.num_mcq / num_columns)
            block_height = rows_needed * MCQ_ROW_PITCH_MM
            col_width = usable_width / num_columns
            if block_height <= mcq_available and col_width >= _min_col_width(len(option_letters)):
                chosen_columns = num_columns
                break
        if chosen_columns is None:
            return None
        col_width = usable_width / chosen_columns
        y0 = PAGE1_CONTENT_START_MM + SECTION_HEADER_MM
        for i in range(config.num_mcq):
            q_no = i + 1
            col = i // rows_needed
            row = i % rows_needed
            x = MARGIN_MM + col * col_width
            y = y0 + row * MCQ_ROW_PITCH_MM
            mcq_entries.append(MCQEntry(q_no, x, y, option_letters, page=1))
        mcq_block_bottom = y0 + rows_needed * MCQ_ROW_PITCH_MM

    written_entries: list[WrittenEntry] = []
    if config.written_questions:
        y = mcq_block_bottom + SECTION_HEADER_MM
        for wq, box_h in zip(config.written_questions, written_box_heights):
            written_entries.append(
                WrittenEntry(
                    q_no=wq.q_no,
                    x_mm=MARGIN_MM,
                    y_mm=y + WRITTEN_HEADER_MM,
                    width_mm=usable_width,
                    height_mm=box_h - WRITTEN_HEADER_MM,
                    max_marks=wq.max_marks,
                    lines=wq.lines,
                    page=1,
                )
            )
            y += box_h + WRITTEN_GAP_MM
        if y - WRITTEN_GAP_MM > PAGE_BOTTOM_MM + 1e-6:
            return None

    return mcq_entries, written_entries


def _paginate_mcqs(num_mcq: int, option_letters: list[str], usable_width: float) -> tuple[list[MCQEntry], int]:
    """Fill page 1's MCQ area first, then continuation pages, choosing the
    column count (2/3/4) that minimizes the number of pages needed."""
    if num_mcq == 0:
        return [], 0

    min_col_width = _min_col_width(len(option_letters))
    page1_available = PAGE_BOTTOM_MM - PAGE1_CONTENT_START_MM - SECTION_HEADER_MM
    cont_available = PAGE_BOTTOM_MM - CONTINUATION_CONTENT_START_MM - SECTION_HEADER_MM

    best = None  # (columns, pages_needed, page1_capacity, cont_capacity)
    for columns in (2, 3, 4):
        col_width = usable_width / columns
        if col_width < min_col_width:
            continue
        page1_capacity = max(0, math.floor(page1_available / MCQ_ROW_PITCH_MM)) * columns
        cont_capacity = max(1, math.floor(cont_available / MCQ_ROW_PITCH_MM)) * columns
        if num_mcq <= page1_capacity:
            pages_needed = 1
        else:
            pages_needed = 1 + math.ceil((num_mcq - page1_capacity) / cont_capacity)
        candidate = (columns, pages_needed, page1_capacity, cont_capacity)
        if best is None or pages_needed < best[1]:
            best = candidate

    if best is None:
        raise ValueError(
            f"{len(option_letters)} MCQ options are too wide to lay out on an A4 page "
            "even in a single column. Reduce mcq_options."
        )

    columns, _pages_needed, page1_capacity, cont_capacity = best
    col_width = usable_width / columns

    entries: list[MCQEntry] = []
    remaining = list(range(1, num_mcq + 1))
    page_no = 1
    while remaining:
        capacity = page1_capacity if page_no == 1 else cont_capacity
        this_page = remaining[:capacity]
        remaining = remaining[capacity:]
        rows_needed = math.ceil(len(this_page) / columns)
        y0 = _content_start(page_no) + SECTION_HEADER_MM
        for i, q_no in enumerate(this_page):
            col = i // rows_needed
            row = i % rows_needed
            x = MARGIN_MM + col * col_width
            y = y0 + row * MCQ_ROW_PITCH_MM
            entries.append(MCQEntry(q_no, x, y, option_letters, page=page_no))
        page_no += 1

    return entries, page_no - 1


def _paginate_written(written_questions, usable_width: float, start_page: int) -> tuple[list[WrittenEntry], int]:
    """Greedily bin-pack written answer boxes across pages starting at
    `start_page`, moving to a new page whenever a box wouldn't fit."""
    if not written_questions:
        return [], 0

    entries: list[WrittenEntry] = []
    page_no = start_page
    y = _content_start(page_no) + SECTION_HEADER_MM

    for wq in written_questions:
        box_h = _written_box_height(wq.lines)
        if y + box_h > PAGE_BOTTOM_MM + 1e-6:
            page_no += 1
            y = _content_start(page_no) + SECTION_HEADER_MM
            if y + box_h > PAGE_BOTTOM_MM + 1e-6:
                raise ValueError(
                    f"Written question Q{wq.q_no} needs {wq.lines} lines, which is too tall "
                    "to fit on its own page. Reduce its line count or split it into multiple "
                    "questions."
                )
        entries.append(
            WrittenEntry(
                q_no=wq.q_no,
                x_mm=MARGIN_MM,
                y_mm=y + WRITTEN_HEADER_MM,
                width_mm=usable_width,
                height_mm=box_h - WRITTEN_HEADER_MM,
                max_marks=wq.max_marks,
                lines=wq.lines,
                page=page_no,
            )
        )
        y += box_h + WRITTEN_GAP_MM

    return entries, page_no - start_page + 1


def build_layout(config) -> SheetLayout:  # config: ExamConfig, typed loosely to avoid a circular import
    option_letters = list(string.ascii_uppercase[: config.mcq_options])
    usable_width = PAGE_WIDTH_MM - 2 * MARGIN_MM
    roll_block = _build_roll_block()

    single = _try_single_page(config, option_letters, usable_width)
    if single is not None:
        mcq_entries, written_entries = single
        num_pages = 1
    else:
        mcq_entries, mcq_pages_used = _paginate_mcqs(config.num_mcq, option_letters, usable_width)
        written_start_page = mcq_pages_used + 1 if config.num_mcq else 1
        written_entries, written_pages_used = _paginate_written(
            config.written_questions, usable_width, written_start_page
        )
        if config.written_questions:
            num_pages = written_start_page + written_pages_used - 1
        else:
            num_pages = max(mcq_pages_used, 1)

    return SheetLayout(
        exam_id=config.exam_id,
        course_code=config.course_code,
        exam_name=config.exam_name,
        fiducials=[f for page in range(1, num_pages + 1) for f in _page_fiducials(page)],
        roll_block=roll_block,
        mcq_entries=mcq_entries,
        written_entries=written_entries,
        mcq_options=config.mcq_options,
        num_pages=num_pages,
    )
