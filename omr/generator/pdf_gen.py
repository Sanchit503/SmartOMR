"""Printable PDF rendering from a SheetLayout (Section 4 of CLAUDE.md).

Draws exactly what `manifest.build_manifest` describes, from the same
`SheetLayout` object, so print output and manifest coordinates can't drift
apart (Section 2, principle 1). Loops over `layout.num_pages`, drawing only
the fiducials/entries tagged for each page (see layout.py's module
docstring for the pagination and identity rules).

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

from .layout import (
    BUBBLE_RADIUS_MM,
    DIGIT_ROW_LABEL_DX_MM,
    MARGIN_MM,
    MCQ_LABEL_OFFSET_MM,
    MCQ_OPTION_HEADER_MM,
    MCQ_OPTION_PITCH_MM,
    NAME_FIELD_LABEL_W_MM,
    PAGE_HEIGHT_MM,
    PAGE_WIDTH_MM,
    ROLL_GRID_TITLE_Y_MM,
    WRITTEN_HEADER_MM,
    WRITTEN_LINE_MM,
    HEADER_INSTRUCTION2_Y_MM,
    HEADER_INSTRUCTION_Y_MM,
    HEADER_META_Y_MM,
    HEADER_TITLE_Y_MM,
    SheetLayout,
)

# ReportLab measures from the bottom-left; the manifest measures from the
# top-left. That flip is confined to this module — see `_y`.
HAIRLINE_PT = 0.7


def _y(y_mm: float) -> float:
    """Convert a top-down manifest y (mm) to ReportLab's bottom-up PDF points."""
    return (PAGE_HEIGHT_MM - y_mm) * mm


def render_pdf(layout: SheetLayout, output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(output_path), pagesize=A4)
    c.setTitle(f"{layout.exam_id} — OMR answer sheet")

    mcq_pages = sorted({e.page for e in layout.mcq_entries})
    written_pages = sorted({e.page for e in layout.written_entries})

    for page_no in range(1, layout.num_pages + 1):
        _draw_fiducials(c, layout, page_no)
        _draw_header(c, layout, page_no)
        if page_no == 1:
            _draw_identity_block(c, layout)
        else:
            _draw_continuation_identity(c, layout, page_no)
        _draw_mcq_block(c, layout, page_no, is_first=bool(mcq_pages) and page_no == mcq_pages[0])
        _draw_written_block(c, layout, page_no, is_first=bool(written_pages) and page_no == written_pages[0])
        c.showPage()

    c.save()
    return output_path


# ---------------------------------------------------------------------------
# Registration marks
# ---------------------------------------------------------------------------

def _draw_fiducials(c: canvas.Canvas, layout: SheetLayout, page_no: int) -> None:
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


def _draw_header(c: canvas.Canvas, layout: SheetLayout, page_no: int) -> None:
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(MARGIN_MM * mm, _y(HEADER_TITLE_Y_MM), layout.exam_name)

    c.setFont("Helvetica", 9.5)
    c.drawString(MARGIN_MM * mm, _y(HEADER_META_Y_MM), f"{layout.course_code}  |  Exam ID: {layout.exam_id}")
    if layout.num_pages > 1:
        c.drawRightString(
            (PAGE_WIDTH_MM - MARGIN_MM) * mm, _y(HEADER_META_Y_MM), f"Page {page_no} of {layout.num_pages}"
        )

    c.setFont("Helvetica", 7.5)
    c.drawString(
        MARGIN_MM * mm,
        _y(HEADER_INSTRUCTION_Y_MM),
        "Fill each bubble completely with a dark pen or pencil. Do not fold or tear the sheet, and "
        "keep all marks away from the black corner squares.",
    )
    second_line = (
        "Bubble your program, then write and bubble your roll number in THAT program's grid only."
        if page_no == 1
        else "Write your name and roll number on every page."
    )
    c.drawString(MARGIN_MM * mm, _y(HEADER_INSTRUCTION2_Y_MM), second_line)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def _draw_write_in(c: canvas.Canvas, fld) -> None:
    """A ruled line for free text, or a row of character cells."""
    c.setLineWidth(HAIRLINE_PT)
    if fld.cells <= 1:
        c.line(fld.x_mm * mm, _y(fld.y_mm + fld.height_mm),
               (fld.x_mm + fld.width_mm) * mm, _y(fld.y_mm + fld.height_mm))
        return
    cell_w = fld.width_mm / fld.cells if not fld.cell_pitch_mm else fld.cell_pitch_mm
    box_w = min(cell_w - 1.0, fld.width_mm / fld.cells)
    for i in range(fld.cells):
        x = fld.x_mm + i * cell_w
        c.rect(x * mm, _y(fld.y_mm + fld.height_mm), box_w * mm, fld.height_mm * mm, stroke=1, fill=0)


def _draw_identity_block(c: canvas.Canvas, layout: SheetLayout) -> None:
    rb = layout.roll_block
    c.setFillColorRGB(0, 0, 0)

    name_field = next(
        f for f in layout.write_in_fields if f.page == 1 and f.name == "student_name"
    )
    c.setFont("Helvetica-Bold", 9)
    c.drawString(MARGIN_MM * mm, _y(name_field.y_mm + name_field.height_mm - 1.5), "Name")
    _draw_write_in(c, name_field)

    c.setFont("Helvetica-Bold", 9)
    for label, (x, yv) in rb.program_selector.items():
        c.setLineWidth(1)
        c.circle(x * mm, _y(yv), BUBBLE_RADIUS_MM * mm, stroke=1, fill=0)
        c.drawString((x + BUBBLE_RADIUS_MM + 2.5) * mm, _y(yv) - 3, label)

    grids = (
        (rb.btech_digits, "BTech Roll No.  (7 digits)"),
        (rb.mtech_digits, "MTech Roll No.  (MT + 5 digits)"),
    )
    for grid, title in grids:
        c.setFont("Helvetica-Bold", 8)
        c.drawString(grid.x_mm * mm, _y(ROLL_GRID_TITLE_Y_MM), title)

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
    handwritten name and roll number — a page that gets separated from its
    page 1 is otherwise unattributable to any student."""
    c.setFillColorRGB(0, 0, 0)
    for fld in layout.write_in_fields:
        if fld.page != page_no:
            continue
        c.setFont("Helvetica-Bold", 9)
        label = "Name" if fld.name == "student_name" else "Roll No."
        label_w = NAME_FIELD_LABEL_W_MM if fld.name == "student_name" else 15.0
        c.drawString((fld.x_mm - label_w) * mm, _y(fld.y_mm + fld.height_mm - 1.5), label)
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
    label = "Section A - MCQs" if is_first else "Section A - MCQs (continued)"
    c.drawString(MARGIN_MM * mm, _y(top_y - MCQ_OPTION_HEADER_MM - 4), label)

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


def _draw_written_block(c: canvas.Canvas, layout: SheetLayout, page_no: int, is_first: bool) -> None:
    entries = [e for e in layout.written_entries if e.page == page_no]
    if not entries:
        return
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 10)
    label = "Section B - Written Answers" if is_first else "Section B - Written Answers (continued)"
    c.drawString(MARGIN_MM * mm, _y(entries[0].y_mm - WRITTEN_HEADER_MM - 4), label)

    for entry in entries:
        c.setFont("Helvetica", 8)
        marks = int(entry.max_marks) if float(entry.max_marks).is_integer() else entry.max_marks
        c.drawString(entry.x_mm * mm, _y(entry.y_mm - 2), f"Q{entry.q_no}  [{marks} marks]")

        c.setLineWidth(HAIRLINE_PT)
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
        for li in range(1, entry.lines):
            ly = entry.y_mm + li * WRITTEN_LINE_MM
            c.line(entry.x_mm * mm, _y(ly), (entry.x_mm + entry.width_mm) * mm, _y(ly))
