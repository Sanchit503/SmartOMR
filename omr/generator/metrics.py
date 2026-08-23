"""Every millimetre the sheet is built from, and the geometry derived from it.

This file is the one to edit when you want the sheet to *look* different.
`flow.py` decides what goes on which page using these numbers; `layout.py`
assembles the result; `pdf_gen.py` draws it. None of them hardcode a
measurement of their own.

All coordinates are millimetres from the TOP-LEFT of the page (x right,
y DOWN), matching image conventions so the parser converts mm -> px with a
single scale factor at any DPI.

Vertical structure of a page, top to bottom:

    ---- 10mm ------------  printer-safe margin: no ink at all above here
         corner markers, orientation marker, page-index bars
    ---- 22.5mm ----------  bottom of the corner markers' quiet zones
         title / course / exam id / total marks / page x of y
         instructions
    ---- IDENTITY_BOX_TOP  bordered identity block
         page 1:  program bubbles, roll write-in cells,
                  the two 0-9 digit grids            (ends ~120mm)
         page 2+: compact program + roll boxes       (ends 50mm)
    ---- content_top_mm()   questions start here, and flow down
         Section A MCQs, then Section B written answers, continuously
    ---- PAGE_BOTTOM_MM --  top of the bottom markers' quiet zones (274.5mm)
    ---- 287mm -----------  printer-safe margin: no ink at all below here

The two identity bands are why page 1 holds fewer questions than a
continuation page: 124.5mm of page 1 is spent on the bubbled roll-number
grid, versus 55mm on a continuation page's write-in strip.
"""
from __future__ import annotations

import math

from ..contracts.geometry import A4_HEIGHT_MM, A4_WIDTH_MM

# Page size comes from the shared contract. Everything else in this file is a
# layout *decision* the generator owns and publishes through the manifest, so
# the reader learns it at parse time instead of sharing a constant that could
# drift (PROJECT_SPEC.md Section 11.1).
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
# Fiducials and orientation (PROJECT_SPEC.md Section 4.3)
# ---------------------------------------------------------------------------
FIDUCIAL_SIZE_MM = 7.0
FIDUCIAL_QUIET_MM = 3.5  # blank paper required on every side of a marker

# Derived so the marker's OUTER EDGE — not its center — clears the safe area.
FIDUCIAL_INSET_MM = FIDUCIAL_EDGE_CLEARANCE_MM + FIDUCIAL_SIZE_MM / 2

# Smaller square near the top-left, used only to tell which corner is which.
ORIENTATION_MARKER_SIZE_MM = 3.5
ORIENTATION_MARKER_X_MM = FIDUCIAL_INSET_MM + 14.0
ORIENTATION_MARKER_Y_MM = FIDUCIAL_INSET_MM

# Half-width of the square keep-out each marker owns.
CORNER_KEEPOUT_MM = FIDUCIAL_SIZE_MM / 2 + FIDUCIAL_QUIET_MM
ORIENTATION_KEEPOUT_MM = ORIENTATION_MARKER_SIZE_MM / 2 + FIDUCIAL_QUIET_MM

# ---------------------------------------------------------------------------
# Page-index bars
# ---------------------------------------------------------------------------
# A multi-page sheet is scanned as a batch, and its pages have to be regrouped
# per student afterwards. Page 1 says which student it belongs to; pages 2+
# only carry a handwritten roll number, so the machine-checkable part of
# regrouping is "are these N pages a complete sheet, in order?".
#
# Each page prints a row of `num_pages` outlined bars with its own index
# filled in solid. The reader measures ink at each bar's coordinate with the
# same primitive it uses for a bubble — no new decoder — and gets back the
# page's own index, plus a checksum: exactly one bar must read dark.
#
# They are deliberately BARS, not squares: a marker detector filters
# candidates by squareness, so a 4.0 x 1.8mm rectangle can never be mistaken
# for the 3.5mm orientation marker sitting on the same row.
PAGE_MARK_WIDTH_MM = 4.0
PAGE_MARK_HEIGHT_MM = 1.8
PAGE_MARK_PITCH_MM = 6.0
PAGE_MARK_Y_MM = FIDUCIAL_INSET_MM  # same row as the corner markers
# Mirrors the orientation marker's inset from the opposite corner.
PAGE_MARK_RIGHT_MM = PAGE_WIDTH_MM - FIDUCIAL_INSET_MM - 14.0

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
# 25.8 is the highest a 10pt centered university line can sit: it clears the
# top marker quiet zone while still leaving room for the exam title below.
HEADER_UNIVERSITY_Y_MM = 25.8
HEADER_TITLE_Y_MM = 30.0
HEADER_META_Y_MM = 34.0
HEADER_INSTRUCTION_Y_MM = 37.4
HEADER_INSTRUCTION2_Y_MM = 40.3
CONT_HEADER_Y_MM = 27.0

# ---------------------------------------------------------------------------
# Identity block — page 1 (the machine-read one)
# ---------------------------------------------------------------------------
WRITE_IN_HEIGHT_MM = 7.0
WRITE_IN_CELL_W_MM = 7.0

IDENTITY_BOX_TOP_MM = 42.5  # border around the whole identity block
IDENTITY_BOX_PAD_MM = 1.5  # blank paper between the last bubble row and the border

# The program bubble sits on the same row as its grid's title, immediately
# left of it — both so a student can't mistake which grid a selector belongs
# to, and because a separate selector row cost ~6mm of a page that has to
# fit an entire midsem.
PROGRAM_SELECTOR_Y_MM = 47.5
ROLL_GRID_TITLE_Y_MM = PROGRAM_SELECTOR_Y_MM
ROLL_WRITE_IN_TOP_MM = 51.0

DIGIT_COL_PITCH_MM = 10.0
DIGIT_ROW_PITCH_MM = 6.0  # 4mm bubble + 2mm clear between rows
ROLL_BLOCK_TOP_MM = 62.0  # center of the digit-0 row
DIGIT_ROW_LABEL_DX_MM = 7.5  # row label sits this far LEFT of column 0

BTECH_GRID_X_MM = 28.0
BTECH_GRID_COLUMNS = 7
MTECH_GRID_X_MM = 128.0
MTECH_GRID_COLUMNS = 5

# ---------------------------------------------------------------------------
# Identity block — pages 2+ (the human-read one)
# ---------------------------------------------------------------------------
# Continuation pages carry a roll-number write-in strip, not a
# second bubble grid. Repeating the grid would cost ~85mm of every page and
# ask a student to bubble the same seven digits two or three more times —
# and every extra bubbling is another chance to produce a page that
# *disagrees* with page 1, which is a review-queue item rather than an
# improvement. The strip is what a TA reads when resolving a flagged sheet
# (PROJECT_SPEC.md Section 6, step 5), and the page-index bars above are what let
# the reader verify a page group mechanically.
CONT_IDENTITY_TOP_MM = 31.0
CONT_IDENTITY_HEIGHT_MM = 19.0
CONT_PROGRAM_SELECTOR_Y_MM = 35.0
CONT_WRITE_IN_TOP_MM = 39.5
CONT_ROLL_CELL_PITCH_MM = 12.0
CONT_BTECH_ROLL_X_MM = MARGIN_MM + 6.0
CONT_MTECH_ROLL_X_MM = MARGIN_MM + 120.0
CONT_BTECH_SELECTOR_X_MM = CONT_BTECH_ROLL_X_MM + BUBBLE_RADIUS_MM
CONT_MTECH_SELECTOR_X_MM = CONT_MTECH_ROLL_X_MM + BUBBLE_RADIUS_MM

# ---------------------------------------------------------------------------
# Question blocks
# ---------------------------------------------------------------------------
CONTENT_GAP_MM = 5.0  # between the identity border and the first question

SECTION_HEADER_MM = 6.0  # "Section A - MCQs" band, repeated on every page
SECTION_GAP_MM = 7.0  # blank paper between the MCQ block and Section B

MCQ_OPTION_HEADER_MM = 4.5  # "A B C D" row above the first MCQ of a column
MCQ_ROW_PITCH_MM = 8.0
MCQ_OPTION_PITCH_MM = 8.0
MCQ_LABEL_OFFSET_MM = 12.0  # "Q123" at 8pt is 8.4mm wide, so this clears it
MCQ_COLUMN_CANDIDATES = (2, 3, 4)

WRITTEN_HEADER_MM = 6.0  # "Q21 [5 marks]" label above the box
WRITTEN_LINE_MM = 7.0  # height of one ruled writing line, same for every box
WRITTEN_GAP_MM = 5.0  # between one answer box and the next question's label

# The bottom of the usable content area: above the bottom fiducials' keep-out,
# which is itself already inside the printer-safe area.
PAGE_BOTTOM_MM = PAGE_HEIGHT_MM - FIDUCIAL_INSET_MM - CORNER_KEEPOUT_MM

EPS = 1e-6  # float slack, so a box that fits exactly is not rejected


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


def _square(cx: float, cy: float, half: float) -> tuple[float, float, float, float]:
    return (cx - half, cy - half, cx + half, cy + half)


def corner_keepouts() -> list[tuple[float, float, float, float]]:
    """The four (x0, y0, x1, y1) squares that must stay blank so the corner
    markers stay individually detectable."""
    k = CORNER_KEEPOUT_MM
    right = PAGE_WIDTH_MM - FIDUCIAL_INSET_MM
    bottom = PAGE_HEIGHT_MM - FIDUCIAL_INSET_MM
    return [
        _square(FIDUCIAL_INSET_MM, FIDUCIAL_INSET_MM, k),
        _square(right, FIDUCIAL_INSET_MM, k),
        _square(FIDUCIAL_INSET_MM, bottom, k),
        _square(right, bottom, k),
    ]


def orientation_keepout() -> tuple[float, float, float, float]:
    """The orientation marker needs its own clean surround for the same
    reason the corner markers do — it is detected the same way."""
    return _square(ORIENTATION_MARKER_X_MM, ORIENTATION_MARKER_Y_MM, ORIENTATION_KEEPOUT_MM)


def page_mark_x_mm(index: int, num_pages: int) -> float:
    """Center x of the bar for page `index` (1-based).

    The strip is anchored at its RIGHT end, a fixed and safe distance from the
    top-right marker's quiet zone whatever the page count, and reads
    left-to-right — bar 1 leftmost — so a human counts them the way they'd
    count anything else.
    """
    return PAGE_MARK_RIGHT_MM - (num_pages - index) * PAGE_MARK_PITCH_MM


# ---------------------------------------------------------------------------
# Page frames — what the top of each page kind costs
# ---------------------------------------------------------------------------

def roll_block_bottom_mm() -> float:
    """Lowest ink in page 1's digit grids, bubble outline included."""
    return ROLL_BLOCK_TOP_MM + 9 * DIGIT_ROW_PITCH_MM + BUBBLE_RADIUS_MM


def identity_box(page: int) -> tuple[float, float]:
    """(top, bottom) of the bordered identity block on this page kind."""
    if page == 1:
        return IDENTITY_BOX_TOP_MM, roll_block_bottom_mm() + IDENTITY_BOX_PAD_MM
    return CONT_IDENTITY_TOP_MM, CONT_IDENTITY_TOP_MM + CONT_IDENTITY_HEIGHT_MM


def content_top_mm(page: int) -> float:
    """First y a question may occupy. Derived from the identity block rather
    than hand-tuned, so moving a grid row shifts the question area with it
    instead of silently overlapping."""
    return identity_box(page)[1] + CONTENT_GAP_MM


def usable_width_mm() -> float:
    return PAGE_WIDTH_MM - 2 * MARGIN_MM


# ---------------------------------------------------------------------------
# Block sizing
# ---------------------------------------------------------------------------

def min_mcq_column_width_mm(num_options: int) -> float:
    """Label + option bubbles + breathing room, so two columns of MCQs can
    never overlap horizontally."""
    return MCQ_LABEL_OFFSET_MM + (num_options - 1) * MCQ_OPTION_PITCH_MM + 10.0


def mcq_first_row_y_mm(band_top: float) -> float:
    """Center of the first MCQ row in a block whose band starts at `band_top`
    (section label, then the option-letter header, then the rows)."""
    return band_top + SECTION_HEADER_MM + MCQ_OPTION_HEADER_MM


def mcq_rows_that_fit(band_top: float) -> int:
    """How many MCQ rows still fit between `band_top` and the page bottom."""
    room = PAGE_BOTTOM_MM - mcq_first_row_y_mm(band_top) - BUBBLE_RADIUS_MM
    if room < -EPS:
        return 0
    return int(room // MCQ_ROW_PITCH_MM) + 1


def mcq_block_bottom_mm(band_top: float, rows: int) -> float:
    """Lowest ink in an MCQ block — the last row's bubble outline."""
    return mcq_first_row_y_mm(band_top) + (rows - 1) * MCQ_ROW_PITCH_MM + BUBBLE_RADIUS_MM


def written_box_height_mm(lines: int) -> float:
    """Every writing line is exactly WRITTEN_LINE_MM tall regardless of how
    many there are, so a 2-line box and a 6-line box give a student the same
    room per line."""
    return lines * WRITTEN_LINE_MM


def written_slot_height_mm(lines: int) -> float:
    """Box plus the "Qn [k marks]" label above it."""
    return WRITTEN_HEADER_MM + written_box_height_mm(lines)


def max_written_lines_on_a_page(page: int) -> int:
    """The tallest single answer box that can exist on this page kind. Used to
    turn "doesn't fit" into an error message a professor can act on."""
    room = PAGE_BOTTOM_MM - content_top_mm(page) - SECTION_HEADER_MM - WRITTEN_HEADER_MM
    return max(0, math.floor(room / WRITTEN_LINE_MM))
