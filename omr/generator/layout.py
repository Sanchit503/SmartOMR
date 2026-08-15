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

Three properties of this layout exist specifically to make Module 2's job
possible, and are worth not undoing by accident:

*Fiducial quiet zones.* Each corner marker owns a square keep-out region
(marker + `FIDUCIAL_QUIET_MM` of blank paper). Contour detection finds a
marker by looking for an isolated dark square; a glyph touching one merges
into the same blob and either fails the squareness test or drags the
centroid off. Every content band on the page is derived from these
keep-outs rather than hand-tuned around them, and a rasterizing test
asserts the zones actually come out blank.

*Orientation marker.* Four identical corner squares are symmetric under
90/180/270-degree rotation, so a sheet fed in upside down reads as a valid
upright sheet with every coordinate silently inverted. A fifth, smaller
marker near the top-left breaks that symmetry: whichever corner marker it
sits closest to is the true top-left, at any rotation and any scale.

*Nothing printed inside a bubble.* Fill-ratio reading measures ink inside
the bubble, so anything pre-printed there is noise subtracted from the
signal. Option letters go in a header row above each MCQ column, and digit
labels go in a row-label column beside each roll-number grid. On the real
generated sheet this is the difference between an empty bubble reading
~0.24 and reading ~0.00.

Pagination: an exam is first tried on a single page (the common case for a
typical quiz/midsem). If the MCQs and written questions don't fit together,
the sheet spills onto as many pages as it needs: MCQs fill page(s) using the
same column-packing logic as the single-page case, then the written section
always starts on a fresh page and greedily bin-packs its (variably sized)
answer boxes across as many pages as it needs. Sections never interleave
across a page break — that mirrors how a real multi-page exam paper is laid
out (Section A complete, then Section B), and keeps parsing simple.

Identity: page 1 carries the full roll-number bubble grid. Every page,
including continuation pages, additionally carries handwritten name and
roll-number write-in boxes. The bubble grid is what the parser reads; the
write-in boxes are what a TA reads when the parser flags a sheet for review
(Section 6, step 5) and what attributes a continuation page that got
separated from its page 1. Repeating the whole bubble grid on every page
would cost ~56mm per page and make students bubble the same number three
times; the write-in row costs ~9mm and is legible to a human, which is who
actually resolves the review queue.
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

# ---------------------------------------------------------------------------
# Printer-safe area
# ---------------------------------------------------------------------------
# Ordinary A4 printers cannot print to the edge. Their unprintable border is
# typically 4-6mm on a laser and 3-5mm at the sides on an inkjet, but the
# BOTTOM band on many inkjets runs to 12-15mm because of the paper-feed
# rollers. Anything placed inside that band is not merely faint — it is
# clipped, and a clipped fiducial is worse than a missing one: the remaining
# shape is still square-ish, so detection succeeds and returns a centroid
# that is a few millimetres off, which silently skews every coordinate
# derived from it.
#
# So the layout guarantees a safe area instead of relying on borderless
# printing. Nothing important is placed within PRINTER_SAFE_MARGIN_MM of any
# page edge, and the fiducials — the one element whose clipping corrupts the
# whole sheet — get a further 2mm on top of that. Both are enforced against
# the rasterized PDF by preflight, not merely intended here.
PRINTER_SAFE_MARGIN_MM = 10.0
FIDUCIAL_EDGE_CLEARANCE_MM = PRINTER_SAFE_MARGIN_MM + 2.0

# Content sits 2mm inside the guaranteed safe area, which absorbs stroke
# widths (a box border straddles its coordinate) and glyph overhang.
MARGIN_MM = PRINTER_SAFE_MARGIN_MM + 2.0

# ---------------------------------------------------------------------------
# Fiducials and orientation (Section 4.3)
# ---------------------------------------------------------------------------
FIDUCIAL_SIZE_MM = 7.0
FIDUCIAL_QUIET_MM = 3.5  # blank paper required on every side of a marker

# Derived so the marker's OUTER EDGE — not its center — clears the safe area.
FIDUCIAL_INSET_MM = FIDUCIAL_EDGE_CLEARANCE_MM + FIDUCIAL_SIZE_MM / 2

# Smaller square near the top-left, used only to tell which corner is which.
ORIENTATION_MARKER_SIZE_MM = 3.5
ORIENTATION_MARKER_X_MM = FIDUCIAL_INSET_MM + 14.0
ORIENTATION_MARKER_Y_MM = FIDUCIAL_INSET_MM

# Half-width of the square keep-out each corner marker owns.
CORNER_KEEPOUT_MM = FIDUCIAL_SIZE_MM / 2 + FIDUCIAL_QUIET_MM

# ---------------------------------------------------------------------------
# Bubbles
# ---------------------------------------------------------------------------
BUBBLE_RADIUS_MM = 2.0  # drawn radius (4mm across — comfortable to fill by hand)

# The reader measures a smaller disc than the one printed, so the bubble's
# own outline stroke never counts as student ink. At the default 1pt stroke
# the outline straddles r=2.0mm; 0.72 keeps the sample well clear of it.
BUBBLE_SAMPLE_RATIO = 0.72
BUBBLE_SAMPLE_RADIUS_MM = BUBBLE_RADIUS_MM * BUBBLE_SAMPLE_RATIO

# ---------------------------------------------------------------------------
# Header band
# ---------------------------------------------------------------------------
# The header starts below the top corner markers' keep-out, so the title can
# never touch a fiducial (it previously cleared one by 0.45mm).
HEADER_TITLE_Y_MM = 27.0
HEADER_META_Y_MM = 32.5
HEADER_INSTRUCTION_Y_MM = 37.0
HEADER_INSTRUCTION2_Y_MM = 40.5

# ---------------------------------------------------------------------------
# Identity block
# ---------------------------------------------------------------------------
NAME_FIELD_Y_MM = 50.0  # bottom rule of the "Name" write-in
NAME_FIELD_LABEL_W_MM = 16.0

WRITE_IN_HEIGHT_MM = 7.0
WRITE_IN_CELL_W_MM = 7.0

# The program bubble sits on the same row as its grid's title, immediately
# left of it — both so a student can't mistake which grid a selector belongs
# to, and because a separate selector row cost ~6mm of a page that has to
# fit an entire midsem.
PROGRAM_SELECTOR_Y_MM = 55.5
ROLL_GRID_TITLE_Y_MM = PROGRAM_SELECTOR_Y_MM
ROLL_WRITE_IN_TOP_MM = 59.0

DIGIT_COL_PITCH_MM = 10.0
DIGIT_ROW_PITCH_MM = 6.0  # 4mm bubble + 2mm clear between rows
ROLL_BLOCK_TOP_MM = 70.0  # center of the digit-0 row
DIGIT_ROW_LABEL_DX_MM = 7.5  # row label sits this far LEFT of column 0

BTECH_GRID_X_MM = 28.0
BTECH_GRID_COLUMNS = 7
MTECH_GRID_X_MM = 128.0
MTECH_GRID_COLUMNS = 5

# Continuation pages repeat name + roll write-in, but not the bubble grid.
CONTINUATION_WRITE_IN_Y_MM = 45.0

# ---------------------------------------------------------------------------
# Question blocks
# ---------------------------------------------------------------------------
SECTION_HEADER_MM = 6.0
MCQ_OPTION_HEADER_MM = 4.5  # "A B C D" row above the first MCQ of a column
MCQ_ROW_PITCH_MM = 8.0
MCQ_OPTION_PITCH_MM = 8.0
MCQ_LABEL_OFFSET_MM = 12.0

WRITTEN_HEADER_MM = 6.0  # "Q21 [5 marks]" label above the box
WRITTEN_LINE_MM = 6.5  # height of one ruled writing line, same for every box
WRITTEN_GAP_MM = 5.0  # each box already has a 6mm labelled gap above it

# The bottom of the usable content area: above the bottom fiducials' keep-out,
# which is itself already inside the printer-safe area.
PAGE_BOTTOM_MM = PAGE_HEIGHT_MM - FIDUCIAL_INSET_MM - CORNER_KEEPOUT_MM


@dataclass(frozen=True)
class Fiducial:
    page: int
    corner: str  # TL, TR, BL, BR
    x_mm: float
    y_mm: float
    size_mm: float = FIDUCIAL_SIZE_MM


@dataclass(frozen=True)
class OrientationMarker:
    """Breaks the four corner markers' rotational symmetry. The corner
    square nearest this marker is the true top-left, whatever the sheet's
    rotation or scale."""

    page: int
    x_mm: float
    y_mm: float
    size_mm: float = ORIENTATION_MARKER_SIZE_MM
    marks_corner: str = "TL"


@dataclass(frozen=True)
class WriteInField:
    """A handwritten field. Not machine-read — cropped and shown to a human
    when the bubble read needs resolving (Section 6, step 5)."""

    page: int
    name: str  # "student_name" | "roll_number"
    x_mm: float
    y_mm: float  # top edge
    width_mm: float
    height_mm: float
    cells: int = 1  # >1 draws separate character boxes
    cell_pitch_mm: float = 0.0


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
    exam_type: str
    marks_per_mcq: float
    fiducials: list[Fiducial]
    orientation_markers: list[OrientationMarker]
    write_in_fields: list[WriteInField]
    roll_block: RollBlockLayout
    mcq_entries: list[MCQEntry]
    written_entries: list[WrittenEntry]
    mcq_options: int = field(default=4)
    num_pages: int = field(default=1)


# ---------------------------------------------------------------------------
# Keep-out geometry
# ---------------------------------------------------------------------------

def printer_safe_area() -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1) of the region an ordinary A4 printer will reproduce.

    Nothing that matters — bubble, marker, box or label — is placed outside
    this rectangle, so a sheet printed at 100% scale on a normal office
    printer loses nothing. Preflight checks the rendered pixels against it.
    """
    m = PRINTER_SAFE_MARGIN_MM
    return (m, m, PAGE_WIDTH_MM - m, PAGE_HEIGHT_MM - m)


def corner_keepouts() -> list[tuple[float, float, float, float]]:
    """The four (x0, y0, x1, y1) squares that must stay blank so the corner
    markers stay individually detectable. The orientation marker sits
    outside all of them, far enough that it can't merge with the top-left."""
    k = CORNER_KEEPOUT_MM
    return [
        (FIDUCIAL_INSET_MM - k, FIDUCIAL_INSET_MM - k, FIDUCIAL_INSET_MM + k, FIDUCIAL_INSET_MM + k),
        (PAGE_WIDTH_MM - FIDUCIAL_INSET_MM - k, FIDUCIAL_INSET_MM - k,
         PAGE_WIDTH_MM - FIDUCIAL_INSET_MM + k, FIDUCIAL_INSET_MM + k),
        (FIDUCIAL_INSET_MM - k, PAGE_HEIGHT_MM - FIDUCIAL_INSET_MM - k,
         FIDUCIAL_INSET_MM + k, PAGE_HEIGHT_MM - FIDUCIAL_INSET_MM + k),
        (PAGE_WIDTH_MM - FIDUCIAL_INSET_MM - k, PAGE_HEIGHT_MM - FIDUCIAL_INSET_MM - k,
         PAGE_WIDTH_MM - FIDUCIAL_INSET_MM + k, PAGE_HEIGHT_MM - FIDUCIAL_INSET_MM + k),
    ]


def _page_fiducials(page: int) -> list[Fiducial]:
    return [
        Fiducial(page, "TL", FIDUCIAL_INSET_MM, FIDUCIAL_INSET_MM),
        Fiducial(page, "TR", PAGE_WIDTH_MM - FIDUCIAL_INSET_MM, FIDUCIAL_INSET_MM),
        Fiducial(page, "BL", FIDUCIAL_INSET_MM, PAGE_HEIGHT_MM - FIDUCIAL_INSET_MM),
        Fiducial(page, "BR", PAGE_WIDTH_MM - FIDUCIAL_INSET_MM, PAGE_HEIGHT_MM - FIDUCIAL_INSET_MM),
    ]


# ---------------------------------------------------------------------------
# Identity block
# ---------------------------------------------------------------------------

def _build_roll_block() -> RollBlockLayout:
    return RollBlockLayout(
        page=1,
        program_selector={
            "BTECH": (BTECH_GRID_X_MM, PROGRAM_SELECTOR_Y_MM),
            "MTECH": (MTECH_GRID_X_MM, PROGRAM_SELECTOR_Y_MM),
        },
        btech_digits=DigitGrid(
            BTECH_GRID_COLUMNS, BTECH_GRID_X_MM, ROLL_BLOCK_TOP_MM, DIGIT_COL_PITCH_MM, DIGIT_ROW_PITCH_MM
        ),
        mtech_digits=DigitGrid(
            MTECH_GRID_COLUMNS, MTECH_GRID_X_MM, ROLL_BLOCK_TOP_MM, DIGIT_COL_PITCH_MM, DIGIT_ROW_PITCH_MM
        ),
    )


def _grid_write_in(page: int, grid: DigitGrid, name: str) -> WriteInField:
    """A row of character cells sitting directly above a digit grid, one per
    column, so a student writes the digit and then bubbles it underneath."""
    return WriteInField(
        page=page,
        name=name,
        x_mm=grid.x_mm - WRITE_IN_CELL_W_MM / 2,
        y_mm=ROLL_WRITE_IN_TOP_MM,
        width_mm=(grid.columns - 1) * grid.col_pitch_mm + WRITE_IN_CELL_W_MM,
        height_mm=WRITE_IN_HEIGHT_MM,
        cells=grid.columns,
        cell_pitch_mm=grid.col_pitch_mm,
    )


def _build_write_in_fields(roll_block: RollBlockLayout, num_pages: int) -> list[WriteInField]:
    fields = [
        WriteInField(
            page=1,
            name="student_name",
            x_mm=MARGIN_MM + NAME_FIELD_LABEL_W_MM,
            y_mm=NAME_FIELD_Y_MM - WRITE_IN_HEIGHT_MM,  # 40.0mm, clear of the header
            width_mm=PAGE_WIDTH_MM - MARGIN_MM - (MARGIN_MM + NAME_FIELD_LABEL_W_MM),
            height_mm=WRITE_IN_HEIGHT_MM,
        ),
        _grid_write_in(1, roll_block.btech_digits, "roll_number"),
        _grid_write_in(1, roll_block.mtech_digits, "roll_number"),
    ]
    for page in range(2, num_pages + 1):
        fields.append(
            WriteInField(
                page=page,
                name="student_name",
                x_mm=MARGIN_MM + NAME_FIELD_LABEL_W_MM,
                y_mm=CONTINUATION_WRITE_IN_Y_MM,
                width_mm=80.0,
                height_mm=WRITE_IN_HEIGHT_MM,
            )
        )
        fields.append(
            WriteInField(
                page=page,
                name="roll_number",
                x_mm=PAGE_WIDTH_MM - MARGIN_MM - 7 * WRITE_IN_CELL_W_MM,
                y_mm=CONTINUATION_WRITE_IN_Y_MM,
                width_mm=7 * WRITE_IN_CELL_W_MM,
                height_mm=WRITE_IN_HEIGHT_MM,
                cells=7,
                cell_pitch_mm=WRITE_IN_CELL_W_MM,
            )
        )
    return fields


def roll_block_bottom_mm() -> float:
    """Lowest ink in page 1's identity block, bubble outline included."""
    return ROLL_BLOCK_TOP_MM + 9 * DIGIT_ROW_PITCH_MM + BUBBLE_RADIUS_MM


def page1_content_start_mm() -> float:
    """Derived, not hand-tuned: page 1's question area begins below the
    identity block. Moving `ROLL_BLOCK_TOP_MM` or the row pitch shifts this
    automatically instead of silently overlapping the grid."""
    return roll_block_bottom_mm() + 5.0


def continuation_content_start_mm() -> float:
    return CONTINUATION_WRITE_IN_Y_MM + WRITE_IN_HEIGHT_MM + 6.0


PAGE1_CONTENT_START_MM = page1_content_start_mm()
CONTINUATION_CONTENT_START_MM = continuation_content_start_mm()


def _content_start(page: int) -> float:
    return PAGE1_CONTENT_START_MM if page == 1 else CONTINUATION_CONTENT_START_MM


# ---------------------------------------------------------------------------
# Question blocks
# ---------------------------------------------------------------------------

def _written_box_height(lines: int) -> float:
    """The ruled box itself. Every writing line is exactly `WRITTEN_LINE_MM`
    tall regardless of how many there are, so a 2-line box and a 6-line box
    give a student the same amount of room per line."""
    return lines * WRITTEN_LINE_MM


def _written_slot_height(lines: int) -> float:
    """Box plus the question label above it."""
    return WRITTEN_HEADER_MM + _written_box_height(lines)


def _min_col_width(num_options: int) -> float:
    return MCQ_LABEL_OFFSET_MM + (num_options - 1) * MCQ_OPTION_PITCH_MM + 10.0


def _mcq_block_height(rows: int) -> float:
    """Option-letter header row plus the question rows underneath."""
    return MCQ_OPTION_HEADER_MM + rows * MCQ_ROW_PITCH_MM


def _try_single_page(config, option_letters: list[str], usable_width: float):
    """Attempt the all-on-page-1 layout. Returns (mcq_entries,
    written_entries) on success, or None if it doesn't fit — the caller
    falls back to pagination rather than erroring.

    Column count is tried fewest-first on purpose: fewer columns means more
    horizontal room per question, which keeps neighbouring bubbles further
    apart and makes a stray pen mark less likely to land in the wrong
    question's read region. Only take more columns when fewer won't fit.
    """
    written_slots = [_written_slot_height(wq.lines) for wq in config.written_questions]
    written_total_height = sum(h + WRITTEN_GAP_MM for h in written_slots)
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
            col_width = usable_width / num_columns
            if (
                _mcq_block_height(rows_needed) <= mcq_available
                and col_width >= _min_col_width(len(option_letters))
            ):
                chosen_columns = num_columns
                break
        if chosen_columns is None:
            return None
        col_width = usable_width / chosen_columns
        y0 = PAGE1_CONTENT_START_MM + SECTION_HEADER_MM + MCQ_OPTION_HEADER_MM
        for i in range(config.num_mcq):
            col = i // rows_needed
            row = i % rows_needed
            mcq_entries.append(
                MCQEntry(i + 1, MARGIN_MM + col * col_width, y0 + row * MCQ_ROW_PITCH_MM, option_letters, page=1)
            )
        mcq_block_bottom = y0 + rows_needed * MCQ_ROW_PITCH_MM

    written_entries: list[WrittenEntry] = []
    if config.written_questions:
        y = mcq_block_bottom + SECTION_HEADER_MM
        for wq in config.written_questions:
            written_entries.append(
                WrittenEntry(
                    q_no=wq.q_no,
                    x_mm=MARGIN_MM,
                    y_mm=y + WRITTEN_HEADER_MM,
                    width_mm=usable_width,
                    height_mm=_written_box_height(wq.lines),
                    max_marks=wq.max_marks,
                    lines=wq.lines,
                    page=1,
                )
            )
            y += _written_slot_height(wq.lines) + WRITTEN_GAP_MM
        if y - WRITTEN_GAP_MM > PAGE_BOTTOM_MM + 1e-6:
            return None

    return mcq_entries, written_entries


def _paginate_mcqs(num_mcq: int, option_letters: list[str], usable_width: float) -> tuple[list[MCQEntry], int]:
    """Fill page 1's MCQ area first, then continuation pages, choosing the
    column count (2/3/4) that minimizes the number of pages needed."""
    if num_mcq == 0:
        return [], 0

    min_col_width = _min_col_width(len(option_letters))
    page1_available = PAGE_BOTTOM_MM - PAGE1_CONTENT_START_MM - SECTION_HEADER_MM - MCQ_OPTION_HEADER_MM
    cont_available = PAGE_BOTTOM_MM - CONTINUATION_CONTENT_START_MM - SECTION_HEADER_MM - MCQ_OPTION_HEADER_MM

    best = None  # (columns, pages_needed, page1_capacity, cont_capacity)
    for columns in (2, 3, 4):
        if usable_width / columns < min_col_width:
            continue
        page1_capacity = max(0, math.floor(page1_available / MCQ_ROW_PITCH_MM)) * columns
        cont_capacity = max(1, math.floor(cont_available / MCQ_ROW_PITCH_MM)) * columns
        if num_mcq <= page1_capacity:
            pages_needed = 1
        else:
            pages_needed = 1 + math.ceil((num_mcq - page1_capacity) / cont_capacity)
        if best is None or pages_needed < best[1]:
            best = (columns, pages_needed, page1_capacity, cont_capacity)

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
        this_page, remaining = remaining[:capacity], remaining[capacity:]
        rows_needed = math.ceil(len(this_page) / columns) if this_page else 0
        y0 = _content_start(page_no) + SECTION_HEADER_MM + MCQ_OPTION_HEADER_MM
        for i, q_no in enumerate(this_page):
            col = i // rows_needed
            row = i % rows_needed
            entries.append(
                MCQEntry(q_no, MARGIN_MM + col * col_width, y0 + row * MCQ_ROW_PITCH_MM, option_letters, page=page_no)
            )
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
        slot_h = _written_slot_height(wq.lines)
        if y + slot_h > PAGE_BOTTOM_MM + 1e-6:
            page_no += 1
            y = _content_start(page_no) + SECTION_HEADER_MM
            if y + slot_h > PAGE_BOTTOM_MM + 1e-6:
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
                height_mm=_written_box_height(wq.lines),
                max_marks=wq.max_marks,
                lines=wq.lines,
                page=page_no,
            )
        )
        y += slot_h + WRITTEN_GAP_MM

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
        exam_type=config.exam_type,
        marks_per_mcq=config.marks_per_mcq,
        fiducials=[f for page in range(1, num_pages + 1) for f in _page_fiducials(page)],
        orientation_markers=[
            OrientationMarker(page, ORIENTATION_MARKER_X_MM, ORIENTATION_MARKER_Y_MM)
            for page in range(1, num_pages + 1)
        ],
        write_in_fields=_build_write_in_fields(roll_block, num_pages),
        roll_block=roll_block,
        mcq_entries=mcq_entries,
        written_entries=written_entries,
        mcq_options=config.mcq_options,
        num_pages=num_pages,
    )
