"""Printable PDF rendering from a SheetLayout (Section 4 of PROJECT_SPEC.md).

Draws exactly what `manifest.build_manifest` describes, from the same
`SheetLayout` object, so print output and manifest coordinates can't drift
apart (Section 2, principle 1). Every measurement comes from `metrics.py`;
this file decides only fonts, stroke weights and wording.

The one rule this file must never break: **nothing is ever drawn inside a
bubble**. Every bubble leaves the printer as an empty outline, because the
reader measures ink inside it and any pre-printed glyph is noise subtracted
from a student's signal. Option letters live in a header row above each MCQ
column; roll-number digits live in a row-label column beside each grid.
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from .layout import SheetLayout
from .metrics import (
    BUBBLE_RADIUS_MM,
    CONT_HEADER_Y_MM,
    DIGIT_ROW_LABEL_DX_MM,
    HEADER_INSTRUCTION2_Y_MM,
    HEADER_INSTRUCTION_Y_MM,
    HEADER_META_Y_MM,
    HEADER_TITLE_Y_MM,
    HEADER_UNIVERSITY_Y_MM,
    MARGIN_MM,
    MCQ_LABEL_OFFSET_MM,
    MCQ_OPTION_HEADER_MM,
    MCQ_OPTION_PITCH_MM,
    NUMERICAL_GRID_OFFSET_X_MM,
    NUMERICAL_GRID_OFFSET_Y_MM,
    PAGE_HEIGHT_MM,
    PAGE_WIDTH_MM,
    WRITTEN_HEADER_MM,
    WRITTEN_LINE_MM,
    identity_box,
    numerical_slot_width_mm,
)

# ReportLab measures from the bottom-left; the manifest measures from the
# top-left. That flip is confined to this module — see `_y`.
HAIRLINE_PT = 0.7
BOX_RULE_PT = 0.9

MIN_FONT_PT = 5.5  # floor for the auto-shrink below

NUMERICAL_PLACE_VALUE_LABELS = (
    "Ones",
    "Tens",
    "Hundreds",
    "Thousands",
    "Ten thousands",
    "Hundred thousands",
    "Millions",
    "Ten millions",
)

INSTRUCTION_LINE_1 = (
    "Fill each bubble completely with a dark pen or pencil. Do not fold or tear the sheet, "
    "and keep all marks away from the black corner squares."
)
INSTRUCTION_LINE_2 = (
    "Bubble your program, then write and bubble your roll number in THAT program's grid only. "
    "Write your roll number on every page of this sheet."
)


def _y(y_mm: float) -> float:
    """Convert a top-down manifest y (mm) to ReportLab's bottom-up PDF points."""
    return (PAGE_HEIGHT_MM - y_mm) * mm


def _marks(value: float) -> str:
    return f"{int(value)}" if float(value).is_integer() else f"{value:g}"


def _draw_fitted(
    c: canvas.Canvas, x_mm: float, y_mm: float, text: str, font: str, size: float, max_width_mm: float
) -> None:
    """Draw `text`, shrinking the font if it would run past `max_width_mm`.

    Header strings carry professor-supplied values — an exam name or exam id
    can be any length — and text that overflows the right margin is exactly
    the kind of defect that survives every coordinate assertion and then
    prints into the page edge. Measuring it here is cheaper than hoping the
    string is short.
    """
    limit = max_width_mm * mm
    width = c.stringWidth(text, font, size)
    if width > limit:
        size = max(MIN_FONT_PT, size * limit / width)
    c.setFont(font, size)
    c.drawString(x_mm * mm, _y(y_mm), text)


def _draw_centered_fitted(
    c: canvas.Canvas, y_mm: float, text: str, font: str, size: float, max_width_mm: float
) -> None:
    if not text:
        return
    limit = max_width_mm * mm
    width = c.stringWidth(text, font, size)
    if width > limit:
        size = max(MIN_FONT_PT, size * limit / width)
    c.setFont(font, size)
    c.drawCentredString((PAGE_WIDTH_MM / 2) * mm, _y(y_mm), text)


def render_pdf(layout: SheetLayout, output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(output_path), pagesize=A4)
    c.setTitle(f"{layout.exam_id} - OMR answer sheet")

    mcq_pages = sorted({e.page for e in layout.mcq_entries})
    written_pages = sorted({e.page for e in layout.written_entries})
    numerical_pages = sorted({e.page for e in layout.numerical_entries})

    for page_no in range(1, layout.num_pages + 1):
        _draw_registration_marks(c, layout, page_no)
        _draw_header(c, layout, page_no)
        if page_no == 1:
            _draw_identity_block(c, layout)
        else:
            _draw_continuation_identity(c, layout, page_no)
        _draw_mcq_block(c, layout, page_no, is_first=bool(mcq_pages) and page_no == mcq_pages[0])
        _draw_numerical_block(c, layout, page_no, is_first=bool(numerical_pages) and page_no == numerical_pages[0])
        _draw_written_block(c, layout, page_no, is_first=bool(written_pages) and page_no == written_pages[0])
        c.showPage()

    c.save()
    return output_path


# ---------------------------------------------------------------------------
# Registration marks
# ---------------------------------------------------------------------------

def _draw_registration_marks(c: canvas.Canvas, layout: SheetLayout, page_no: int) -> None:
    c.setFillColorRGB(0, 0, 0)
    for f in layout.fiducials:
        if f.page != page_no:
            continue
        half = f.size_mm / 2
        c.rect((f.x_mm - half) * mm, _y(f.y_mm + half), f.size_mm * mm, f.size_mm * mm, fill=1, stroke=0)

    for om in layout.orientation_markers:
        if om.page != page_no:
            continue
        half = om.size_mm / 2
        c.rect((om.x_mm - half) * mm, _y(om.y_mm + half), om.size_mm * mm, om.size_mm * mm, fill=1, stroke=0)

    # Page-index bars: this page's own bar solid, the rest outlined so a human
    # can count them too. Wide-and-thin on purpose — a marker detector's
    # squareness filter must never mistake one for the orientation marker
    # sitting on the same row.
    c.setLineWidth(HAIRLINE_PT)
    page_marks = [pm for pm in layout.page_marks if pm.page == page_no]
    for pm in page_marks:
        c.rect(
            (pm.x_mm - pm.width_mm / 2) * mm,
            _y(pm.y_mm + pm.height_mm / 2),
            pm.width_mm * mm,
            pm.height_mm * mm,
            fill=1 if pm.filled else 0,
            stroke=0 if pm.filled else 1,
        )


def _draw_header(c: canvas.Canvas, layout: SheetLayout, page_no: int) -> None:
    """Page 1 gets a full masthead. Continuation pages compress it to a single
    line, because every millimetre spent here is a millimetre of answer space
    — and the identity strip immediately below starts at 31mm.
    """
    c.setFillColorRGB(0, 0, 0)
    usable = PAGE_WIDTH_MM - 2 * MARGIN_MM
    total = _marks(layout.total_marks)

    # Right-aligned page counter, on the same line as the leftmost header text.
    page_label = f"Page {page_no} of {layout.num_pages}" if layout.num_pages > 1 else ""
    counter_y = HEADER_META_Y_MM if page_no == 1 else CONT_HEADER_Y_MM
    reserved = 0.0
    if page_label:
        reserved = c.stringWidth(page_label, "Helvetica", 9.5) / mm + 6.0
        c.setFont("Helvetica", 9.5)
        c.drawRightString((PAGE_WIDTH_MM - MARGIN_MM) * mm, _y(counter_y), page_label)

    if page_no > 1:
        one_line = f"{layout.exam_name}   |   {layout.course_code}   |   {layout.exam_id}   |   {total} marks"
        _draw_fitted(c, MARGIN_MM, CONT_HEADER_Y_MM, one_line, "Helvetica-Bold", 10.5, usable - reserved)
        return

    _draw_centered_fitted(c, HEADER_UNIVERSITY_Y_MM, layout.university_name, "Helvetica-Bold", 10.5, usable)
    _draw_fitted(c, MARGIN_MM, HEADER_TITLE_Y_MM, layout.exam_name, "Helvetica-Bold", 11.5, usable)
    meta = f"{layout.course_code}  |  Exam ID: {layout.exam_id}  |  Total: {total} marks"
    _draw_fitted(c, MARGIN_MM, HEADER_META_Y_MM, meta, "Helvetica", 9.0, usable - reserved)
    _draw_fitted(c, MARGIN_MM, HEADER_INSTRUCTION_Y_MM, INSTRUCTION_LINE_1, "Helvetica", 7.0, usable)
    _draw_fitted(c, MARGIN_MM, HEADER_INSTRUCTION2_Y_MM, INSTRUCTION_LINE_2, "Helvetica", 7.0, usable)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def _draw_identity_border(c: canvas.Canvas, page_no: int) -> None:
    top, bottom = identity_box(page_no)
    c.setLineWidth(HAIRLINE_PT)
    c.rect(
        MARGIN_MM * mm,
        _y(bottom),
        (PAGE_WIDTH_MM - 2 * MARGIN_MM) * mm,
        (bottom - top) * mm,
        stroke=1,
        fill=0,
    )


def _draw_write_in(c: canvas.Canvas, fld) -> None:
    """A ruled line for free text, or a row of character cells."""
    c.setLineWidth(HAIRLINE_PT)
    if fld.cells <= 1:
        c.line(fld.x_mm * mm, _y(fld.y_mm + fld.height_mm),
               (fld.x_mm + fld.width_mm) * mm, _y(fld.y_mm + fld.height_mm))
        return
    for i in range(fld.cells):
        x = fld.x_mm + i * fld.cell_pitch_mm
        c.rect(x * mm, _y(fld.y_mm + fld.height_mm), fld.cell_width_mm * mm, fld.height_mm * mm,
               stroke=1, fill=0)


def _draw_identity_block(c: canvas.Canvas, layout: SheetLayout) -> None:
    rb = layout.roll_block
    c.setFillColorRGB(0, 0, 0)
    _draw_identity_border(c, 1)

    # BTech has seven numeric digits. MTech and PhD each have a textual
    # prefix plus five numeric digits, so their selectors share one grid.
    for program in ("BTECH", "MTECH", "PHD"):
        sx, sy = rb.program_selector[program]
        c.setLineWidth(1)
        c.circle(sx * mm, _y(sy), BUBBLE_RADIUS_MM * mm, stroke=1, fill=0)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString((sx + BUBBLE_RADIUS_MM + 2.5) * mm, _y(sy) - 3, program)

    c.setFont("Helvetica-Bold", 8.0)
    c.drawString(49.0 * mm, _y(rb.program_selector["BTECH"][1]) - 3, "BTech Roll No. (7 digits)")
    c.drawString(169.0 * mm, _y(rb.program_selector["MTECH"][1]) - 3, "5 digits")

    for grid in (rb.btech_digits, rb.mtech_digits):
        # Digit labels go in a column to the LEFT of the grid, never inside a
        # bubble — one label serves the whole row, since every column of a
        # roll grid shares the same digit ordering.
        c.setFont("Helvetica", 6.5)
        for digit in range(10):
            cy = grid.y_mm + digit * grid.row_pitch_mm
            c.drawCentredString((grid.x_mm - DIGIT_ROW_LABEL_DX_MM) * mm, _y(cy) - 2.2, str(digit))

        c.setLineWidth(1)
        for col in range(grid.columns):
            cx = grid.x_mm + col * grid.col_pitch_mm
            for digit in range(10):
                cy = grid.y_mm + digit * grid.row_pitch_mm
                c.circle(cx * mm, _y(cy), BUBBLE_RADIUS_MM * mm, stroke=1, fill=0)

    for fld in layout.write_in_fields:
        if fld.page == 1 and fld.name == "roll_number":
            _draw_write_in(c, fld)


def _draw_continuation_identity(c: canvas.Canvas, layout: SheetLayout, page_no: int) -> None:
    """Continuation pages carry no bubble grid, but they do carry the
    program choice and handwritten roll number for human review if a page is
    separated."""
    c.setFillColorRGB(0, 0, 0)
    _draw_identity_border(c, page_no)

    fields = [fld for fld in layout.write_in_fields if fld.page == page_no]
    choices = [choice for choice in layout.continuation_program_choices if choice.page == page_no]

    if choices:
        c.setLineWidth(1)
        for choice in sorted(choices, key=lambda c: c.x_mm):
            c.circle(choice.x_mm * mm, _y(choice.y_mm), choice.radius_mm * mm, stroke=1, fill=0)
            text_x = choice.x_mm + choice.radius_mm + 2.0
            text_y = _y(choice.y_mm) - 3
            c.setFont("Helvetica-Bold", 7.8)
            c.drawString(text_x * mm, text_y, choice.program)
        c.setFont("Helvetica-Bold", 7.5)
        c.drawString(47.0 * mm, _y(choices[0].y_mm) - 3, "BTech Roll No. (7 digits)")
        c.drawString(174.0 * mm, _y(choices[0].y_mm) - 3, "5 digits")

    seen_fields = set()
    for fld in fields:
        geometry = (fld.x_mm, fld.y_mm, fld.width_mm, fld.height_mm, fld.cells, fld.cell_pitch_mm)
        if geometry in seen_fields:
            continue
        seen_fields.add(geometry)
        _draw_write_in(c, fld)


# ---------------------------------------------------------------------------
# Question blocks
# ---------------------------------------------------------------------------

def _draw_mcq_block(c: canvas.Canvas, layout: SheetLayout, page_no: int, is_first: bool) -> None:
    entries = [e for e in layout.mcq_entries if e.page == page_no]
    if not entries:
        return
    c.setFillColorRGB(0, 0, 0)

    top_y = min(e.y_mm for e in entries)
    c.setFont("Helvetica-Bold", 10)
    suffix = "" if is_first else "  (continued)"
    marks = _marks(layout.marks_per_mcq)
    c.drawString(
        MARGIN_MM * mm,
        _y(top_y - MCQ_OPTION_HEADER_MM - 4),
        f"Section A - Multiple Choice  [{marks} mark each]{suffix}",
    )

    # One option-letter header per column, above that column's first row.
    # This is what keeps the bubbles themselves empty.
    c.setFont("Helvetica-Bold", 7)
    for col_x in sorted({e.x_mm for e in entries}):
        for i, opt in enumerate(entries[0].options):
            ox = col_x + MCQ_LABEL_OFFSET_MM + i * MCQ_OPTION_PITCH_MM
            c.drawCentredString(ox * mm, _y(top_y - MCQ_OPTION_HEADER_MM) - 2, opt)

    c.setLineWidth(1)
    for entry in entries:
        c.setFont("Helvetica", 8)
        c.drawString(entry.x_mm * mm, _y(entry.y_mm) - 2.5, f"Q{entry.q_no}")
        for i, _opt in enumerate(entry.options):
            ox = entry.x_mm + MCQ_LABEL_OFFSET_MM + i * MCQ_OPTION_PITCH_MM
            c.circle(ox * mm, _y(entry.y_mm), BUBBLE_RADIUS_MM * mm, stroke=1, fill=0)


def _draw_numerical_block(c: canvas.Canvas, layout: SheetLayout, page_no: int, is_first: bool) -> None:
    entries = [e for e in layout.numerical_entries if e.page == page_no]
    if not entries:
        return
    top = entries[0].y_mm - NUMERICAL_GRID_OFFSET_Y_MM
    section = "B" if layout.mcq_entries else "A"
    suffix = "" if is_first else "  (continued)"
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(MARGIN_MM * mm, _y(top - 8), f"Section {section} - Numerical Answers{suffix}")
    c.setFont("Helvetica", 7.5)
    c.drawString(MARGIN_MM * mm, _y(top - 3),
                 "Whole numbers only. Fill one bubble in every place-value row; use leading zeros "
                 "(e.g. 7 as 007 in a 3-digit grid).")
    for entry in entries:
        left = entry.x_mm - NUMERICAL_GRID_OFFSET_X_MM
        slot_top = entry.y_mm - NUMERICAL_GRID_OFFSET_Y_MM
        digit_label = "digit" if entry.positions == 1 else "digits"
        _draw_fitted(c, left, slot_top + 2,
                     f"Q{entry.q_no}  [{_marks(entry.max_marks)} marks]  {entry.positions} {digit_label}",
                     "Helvetica-Bold", 8, numerical_slot_width_mm(entry.positions))
        c.setLineWidth(HAIRLINE_PT)
        c.setFont("Helvetica", 6.5)
        for digit in range(10):
            x = entry.x_mm + digit * entry.digit_pitch_mm
            c.drawCentredString(x * mm, _y(entry.y_mm - 5.0) - 2.2, str(digit))
        for position in range(entry.positions):
            y = entry.y_mm + position * entry.position_pitch_mm
            c.setFont("Helvetica", 6.5)
            place_power = entry.positions - position - 1
            c.drawRightString(
                (entry.x_mm - BUBBLE_RADIUS_MM - 2.5) * mm,
                _y(y) - 2.2,
                NUMERICAL_PLACE_VALUE_LABELS[place_power],
            )
            c.setLineWidth(1)
            for digit in range(10):
                c.circle((entry.x_mm + digit * entry.digit_pitch_mm) * mm, _y(y),
                         BUBBLE_RADIUS_MM * mm, stroke=1, fill=0)


def _draw_written_block(c: canvas.Canvas, layout: SheetLayout, page_no: int, is_first: bool) -> None:
    entries = [e for e in layout.written_entries if e.page == page_no]
    if not entries:
        return
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 10)
    suffix = "" if is_first else "  (continued)"
    section = "C" if layout.numerical_entries and layout.mcq_entries else "B"
    c.drawString(
        MARGIN_MM * mm, _y(entries[0].y_mm - WRITTEN_HEADER_MM - 4), f"Section {section} - Written Answers{suffix}"
    )

    for entry in entries:
        c.setFont("Helvetica", 8)
        c.drawString(
            entry.x_mm * mm,
            _y(entry.y_mm - 2),
            f"Q{entry.q_no}  [{_marks(entry.max_marks)} marks]  -  answer in {entry.lines} line"
            f"{'s' if entry.lines != 1 else ''}",
        )

        c.setLineWidth(BOX_RULE_PT)
        c.rect(
            entry.x_mm * mm,
            _y(entry.y_mm + entry.height_mm),
            entry.width_mm * mm,
            entry.height_mm * mm,
            stroke=1,
            fill=0,
        )
        # Uniform writing lines: every ruled row is exactly WRITTEN_LINE_MM,
        # so a 2-line box and a 6-line box give the same room per line.
        c.setLineWidth(HAIRLINE_PT)
        for li in range(1, entry.lines):
            ly = entry.y_mm + li * WRITTEN_LINE_MM
            c.line(entry.x_mm * mm, _y(ly), (entry.x_mm + entry.width_mm) * mm, _y(ly))
