"""Assembles a complete SheetLayout — the single source of truth for where
every mark on the sheet goes (PROJECT_SPEC.md Sections 4.2-4.4).

Both `pdf_gen.render_pdf` (what gets printed) and `manifest.build_manifest`
(what the parser reads) consume the same `SheetLayout` object, so the two can
never drift apart (Section 2, principle 1).

    metrics.py   every millimetre, and the geometry derived from it
    flow.py      which question lands on which page, and where
    layout.py    this file: wraps the flow in markers and identity fields
    pdf_gen.py   draws it
    manifest.py  publishes it

Four properties of this sheet exist specifically to make Module 2's job
possible, and are worth not undoing by accident:

*Fiducial quiet zones.* Each corner marker owns a square keep-out region
(marker + `FIDUCIAL_QUIET_MM` of blank paper), and so does the orientation
marker. Contour detection finds a marker by looking for an isolated dark
square; a glyph touching one merges into the same blob and either fails the
squareness test or drags the centroid off. Every content band on the page is
derived from these keep-outs rather than hand-tuned around them, and a
rasterizing test asserts the zones actually come out blank.

*Orientation marker.* Four identical corner squares are symmetric under
90/180/270-degree rotation, so a sheet fed in upside down reads as a valid
upright sheet with every coordinate silently inverted. A fifth, smaller
marker near the top-left breaks that symmetry: whichever corner marker it
sits closest to is the true top-left, at any rotation and any scale.

*Nothing printed inside a bubble.* Fill-ratio reading measures ink inside the
bubble, so anything pre-printed there is noise subtracted from the signal.
Option letters go in a header row above each MCQ column, and digit labels go
in a row-label column beside each roll-number grid. On the real generated
sheet this is the difference between an empty bubble reading ~0.24 and
reading ~0.00.

*Identity on every page.* Page 1 carries the bubbled roll-number grid the
parser reads. EVERY page — page 1 included — additionally carries a
compact BTECH and shared MTECH/PHD identity blocks with write-in boxes, and a row of
page-index bars with its own index printed solid. So a continuation page
that gets separated from its page 1 is attributable by a human from those
blocks, and verifiable by the machine from the bars ("this is page 2 of 3,
and exactly one bar is filled").
See `metrics.py` for why the bubble grid itself is not repeated.
"""
from __future__ import annotations

import string
from dataclasses import dataclass, field

from .flow import FlowResult, LayoutTooTight, MCQEntry, NumericalEntry, WrittenEntry, flow_sheet
from .metrics import (
    BTECH_GRID_COLUMNS,
    BTECH_GRID_X_MM,
    BUBBLE_RADIUS_MM,
    CONT_BTECH_SELECTOR_X_MM,
    CONT_MTECH_SELECTOR_X_MM,
    CONT_PHD_SELECTOR_X_MM,
    CONT_PROGRAM_SELECTOR_Y_MM,
    CONT_BTECH_ROLL_X_MM,
    CONT_MTECH_ROLL_X_MM,
    CONT_ROLL_CELL_PITCH_MM,
    CONT_WRITE_IN_TOP_MM,
    DIGIT_COL_PITCH_MM,
    DIGIT_ROW_PITCH_MM,
    FIDUCIAL_INSET_MM,
    FIDUCIAL_SIZE_MM,
    MARGIN_MM,
    MTECH_GRID_COLUMNS,
    MTECH_GRID_X_MM,
    PHD_SELECTOR_X_MM,
    ORIENTATION_MARKER_SIZE_MM,
    ORIENTATION_MARKER_X_MM,
    ORIENTATION_MARKER_Y_MM,
    PAGE_HEIGHT_MM,
    PAGE_MARK_HEIGHT_MM,
    PAGE_MARK_WIDTH_MM,
    PAGE_MARK_Y_MM,
    PAGE_WIDTH_MM,
    PROGRAM_SELECTOR_Y_MM,
    ROLL_BLOCK_TOP_MM,
    ROLL_WRITE_IN_TOP_MM,
    WRITE_IN_CELL_W_MM,
    WRITE_IN_HEIGHT_MM,
    page_mark_x_mm,
)

__all__ = [
    "DigitGrid",
    "Fiducial",
    "LayoutTooTight",
    "MCQEntry",
    "OrientationMarker",
    "PageMark",
    "ProgramChoice",
    "RollBlockLayout",
    "SheetLayout",
    "WriteInField",
    "WrittenEntry",
    "build_layout",
]


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
class PageMark:
    """One bar in the page-index strip. Exactly one bar per page is `filled`,
    and its `index` is that page's own number — so the reader recovers the
    page index from ink alone, and gets a checksum for free."""

    page: int
    index: int
    x_mm: float
    y_mm: float
    width_mm: float
    height_mm: float
    filled: bool


@dataclass(frozen=True)
class WriteInField:
    """A handwritten field. Not machine-read — cropped and shown to a human
    when the bubble read needs resolving (Section 6, step 5), or when a
    continuation page has to be reattached to its page 1."""

    page: int
    name: str  # "roll_number"
    program: str | None
    x_mm: float
    y_mm: float  # top edge
    width_mm: float
    height_mm: float
    cells: int = 1  # >1 draws separate character boxes
    cell_pitch_mm: float = 0.0
    cell_width_mm: float = 0.0
    programs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProgramChoice:
    """Program selector printed on continuation pages. Page 1's selector is
    part of the machine-read roll block; these compact choices are a human
    fallback beside the handwritten roll-number boxes."""

    page: int
    program: str
    x_mm: float
    y_mm: float
    radius_mm: float = BUBBLE_RADIUS_MM


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
class SheetLayout:
    exam_id: str
    university_name: str
    course_code: str
    exam_name: str
    exam_type: str
    marks_per_mcq: float
    total_marks: float
    fiducials: list[Fiducial]
    orientation_markers: list[OrientationMarker]
    page_marks: list[PageMark]
    continuation_program_choices: list[ProgramChoice]
    write_in_fields: list[WriteInField]
    roll_block: RollBlockLayout
    mcq_entries: list[MCQEntry]
    written_entries: list[WrittenEntry]
    mcq_options: int = field(default=4)
    mcq_columns: int = field(default=0)
    num_pages: int = field(default=1)
    numerical_entries: list[NumericalEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Registration marks
# ---------------------------------------------------------------------------

def _page_fiducials(page: int) -> list[Fiducial]:
    right = PAGE_WIDTH_MM - FIDUCIAL_INSET_MM
    bottom = PAGE_HEIGHT_MM - FIDUCIAL_INSET_MM
    return [
        Fiducial(page, "TL", FIDUCIAL_INSET_MM, FIDUCIAL_INSET_MM),
        Fiducial(page, "TR", right, FIDUCIAL_INSET_MM),
        Fiducial(page, "BL", FIDUCIAL_INSET_MM, bottom),
        Fiducial(page, "BR", right, bottom),
    ]


def _page_marks(page: int, num_pages: int) -> list[PageMark]:
    return [
        PageMark(
            page=page,
            index=index,
            x_mm=page_mark_x_mm(index, num_pages),
            y_mm=PAGE_MARK_Y_MM,
            width_mm=PAGE_MARK_WIDTH_MM,
            height_mm=PAGE_MARK_HEIGHT_MM,
            filled=(index == page),
        )
        for index in range(1, num_pages + 1)
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
            "PHD": (PHD_SELECTOR_X_MM, PROGRAM_SELECTOR_Y_MM),
        },
        btech_digits=DigitGrid(
            BTECH_GRID_COLUMNS, BTECH_GRID_X_MM, ROLL_BLOCK_TOP_MM, DIGIT_COL_PITCH_MM, DIGIT_ROW_PITCH_MM
        ),
        mtech_digits=DigitGrid(
            MTECH_GRID_COLUMNS, MTECH_GRID_X_MM, ROLL_BLOCK_TOP_MM, DIGIT_COL_PITCH_MM, DIGIT_ROW_PITCH_MM
        ),
    )


def _grid_write_in(grid: DigitGrid, *programs: str) -> WriteInField:
    """A row of character cells sitting directly above a digit grid, one per
    column and centered on it, so a student writes the digit and then bubbles
    that same digit in the column underneath."""
    return WriteInField(
        page=1,
        name="roll_number",
        program=programs[0] if len(programs) == 1 else None,
        x_mm=grid.x_mm - WRITE_IN_CELL_W_MM / 2,
        y_mm=ROLL_WRITE_IN_TOP_MM,
        width_mm=(grid.columns - 1) * grid.col_pitch_mm + WRITE_IN_CELL_W_MM,
        height_mm=WRITE_IN_HEIGHT_MM,
        cells=grid.columns,
        cell_pitch_mm=grid.col_pitch_mm,
        cell_width_mm=WRITE_IN_CELL_W_MM,
        programs=tuple(programs),
    )


def _continuation_write_ins(page: int) -> list[WriteInField]:
    return [
        WriteInField(
            page=page,
            name="roll_number",
            program="BTECH",
            x_mm=CONT_BTECH_ROLL_X_MM,
            y_mm=CONT_WRITE_IN_TOP_MM,
            width_mm=(BTECH_GRID_COLUMNS - 1) * CONT_ROLL_CELL_PITCH_MM + WRITE_IN_CELL_W_MM,
            height_mm=WRITE_IN_HEIGHT_MM,
            cells=BTECH_GRID_COLUMNS,
            cell_pitch_mm=CONT_ROLL_CELL_PITCH_MM,
            cell_width_mm=WRITE_IN_CELL_W_MM,
            programs=("BTECH",),
        ),
        WriteInField(
            page=page,
            name="roll_number",
            program="MTECH",
            x_mm=CONT_MTECH_ROLL_X_MM,
            y_mm=CONT_WRITE_IN_TOP_MM,
            width_mm=(MTECH_GRID_COLUMNS - 1) * CONT_ROLL_CELL_PITCH_MM + WRITE_IN_CELL_W_MM,
            height_mm=WRITE_IN_HEIGHT_MM,
            cells=MTECH_GRID_COLUMNS,
            cell_pitch_mm=CONT_ROLL_CELL_PITCH_MM,
            cell_width_mm=WRITE_IN_CELL_W_MM,
            programs=("MTECH", "PHD"),
        ),
    ]


def _continuation_program_choices(page: int) -> list[ProgramChoice]:
    return [
        ProgramChoice(page=page, program="BTECH", x_mm=CONT_BTECH_SELECTOR_X_MM, y_mm=CONT_PROGRAM_SELECTOR_Y_MM),
        ProgramChoice(page=page, program="MTECH", x_mm=CONT_MTECH_SELECTOR_X_MM, y_mm=CONT_PROGRAM_SELECTOR_Y_MM),
        ProgramChoice(page=page, program="PHD", x_mm=CONT_PHD_SELECTOR_X_MM, y_mm=CONT_PROGRAM_SELECTOR_Y_MM),
    ]


def _build_write_in_fields(roll_block: RollBlockLayout, num_pages: int) -> list[WriteInField]:
    fields = [
        _grid_write_in(roll_block.btech_digits, "BTECH"),
        _grid_write_in(roll_block.mtech_digits, "MTECH", "PHD"),
    ]
    for page in range(2, num_pages + 1):
        fields.extend(_continuation_write_ins(page))
    return fields


# ---------------------------------------------------------------------------

def _total_marks(config) -> float:
    return (config.num_mcq * config.marks_per_mcq
            + sum(w.max_marks for w in config.written_questions)
            + sum(q.max_marks for q in config.numerical_questions))


def build_layout(config) -> SheetLayout:  # config: ExamConfig, typed loosely to avoid a circular import
    option_letters = list(string.ascii_uppercase[: config.mcq_options])
    flow: FlowResult = flow_sheet(config, option_letters)
    roll_block = _build_roll_block()
    pages = range(1, flow.num_pages + 1)

    return SheetLayout(
        exam_id=config.exam_id,
        university_name=config.university_name,
        course_code=config.course_code,
        exam_name=config.exam_name,
        exam_type=config.exam_type,
        marks_per_mcq=config.marks_per_mcq,
        total_marks=_total_marks(config),
        fiducials=[f for page in pages for f in _page_fiducials(page)],
        orientation_markers=[
            OrientationMarker(page, ORIENTATION_MARKER_X_MM, ORIENTATION_MARKER_Y_MM) for page in pages
        ],
        page_marks=[m for page in pages for m in _page_marks(page, flow.num_pages)],
        continuation_program_choices=[
            choice for page in range(2, flow.num_pages + 1) for choice in _continuation_program_choices(page)
        ],
        write_in_fields=_build_write_in_fields(roll_block, flow.num_pages),
        roll_block=roll_block,
        mcq_entries=flow.mcq_entries,
        written_entries=flow.written_entries,
        numerical_entries=flow.numerical_entries,
        mcq_options=config.mcq_options,
        mcq_columns=flow.mcq_columns,
        num_pages=flow.num_pages,
    )
