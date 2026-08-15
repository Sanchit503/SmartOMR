"""Printable PDF rendering from a SheetLayout (Section 4 of CLAUDE.md).

Draws exactly what `manifest.build_manifest` describes, from the same
`SheetLayout` object, so print output and manifest coordinates can't drift
apart (Section 2, principle 1). Loops over `layout.num_pages`, drawing only
the fiducials/entries tagged for each page (see layout.py's module
docstring for the pagination rules).
"""
from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from .layout import (
    BUBBLE_RADIUS_MM,
    FIDUCIAL_SIZE_MM,
    MARGIN_MM,
    MCQ_LABEL_OFFSET_MM,
    MCQ_OPTION_PITCH_MM,
    PAGE_HEIGHT_MM,
    WRITTEN_HEADER_MM,
    SheetLayout,
)


def _y(y_mm: float) -> float:
    """Convert a top-down manifest y (mm) to ReportLab's bottom-up PDF points."""
    return (PAGE_HEIGHT_MM - y_mm) * mm


def render_pdf(layout: SheetLayout, output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(output_path), pagesize=A4)

    mcq_pages = sorted({e.page for e in layout.mcq_entries})
    written_pages = sorted({e.page for e in layout.written_entries})

    for page_no in range(1, layout.num_pages + 1):
        _draw_fiducials(c, layout, page_no)
        _draw_header(c, layout, page_no)
        if page_no == 1:
            _draw_roll_block(c, layout)
        _draw_mcq_block(c, layout, page_no, is_first=(mcq_pages and page_no == mcq_pages[0]))
        _draw_written_block(c, layout, page_no, is_first=(written_pages and page_no == written_pages[0]))
        c.showPage()

    c.save()
    return output_path


def _draw_fiducials(c: canvas.Canvas, layout: SheetLayout, page_no: int) -> None:
    half = FIDUCIAL_SIZE_MM / 2
    c.setFillColorRGB(0, 0, 0)
    for f in layout.fiducials:
        if f.page != page_no:
            continue
        c.rect(
            (f.x_mm - half) * mm,
            _y(f.y_mm + half),
            FIDUCIAL_SIZE_MM * mm,
            FIDUCIAL_SIZE_MM * mm,
            fill=1,
            stroke=0,
        )


def _draw_header(c: canvas.Canvas, layout: SheetLayout, page_no: int) -> None:
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(MARGIN_MM * mm, _y(18), layout.exam_name)
    c.setFont("Helvetica", 10)
    c.drawString(MARGIN_MM * mm, _y(24), f"{layout.course_code}  |  Exam ID: {layout.exam_id}")
    if layout.num_pages > 1:
        c.drawRightString((210 - MARGIN_MM) * mm, _y(24), f"Page {page_no} of {layout.num_pages}")
    c.setFont("Helvetica", 8)
    c.drawString(MARGIN_MM * mm, _y(29), "Fill bubbles completely with a dark pen/pencil. Do not fold this sheet.")


def _draw_roll_block(c: canvas.Canvas, layout: SheetLayout) -> None:
    rb = layout.roll_block
    c.setFont("Helvetica", 9)
    for label, (x, yv) in rb.program_selector.items():
        c.circle(x * mm, _y(yv), BUBBLE_RADIUS_MM * mm, stroke=1, fill=0)
        c.drawString((x + 4) * mm, _y(yv) - 3, label)

    for grid, title in ((rb.btech_digits, "BTech Roll No."), (rb.mtech_digits, "MTech Roll No.")):
        c.setFont("Helvetica-Bold", 8)
        c.drawString(grid.x_mm * mm, _y(grid.y_mm - 4), title)
        for col in range(grid.columns):
            cx = grid.x_mm + col * grid.col_pitch_mm
            for digit in range(10):
                cy = grid.y_mm + digit * grid.row_pitch_mm
                c.circle(cx * mm, _y(cy), BUBBLE_RADIUS_MM * mm, stroke=1, fill=0)
                c.setFont("Helvetica", 6)
                c.drawCentredString(cx * mm, _y(cy) - 2, str(digit))


def _draw_mcq_block(c: canvas.Canvas, layout: SheetLayout, page_no: int, is_first: bool) -> None:
    entries = [e for e in layout.mcq_entries if e.page == page_no]
    if not entries:
        return
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 10)
    label = "Section A -- MCQs" if is_first else "Section A -- MCQs (continued)"
    c.drawString(MARGIN_MM * mm, _y(entries[0].y_mm - 8), label)
    for entry in entries:
        c.setFont("Helvetica", 8)
        c.drawString(entry.x_mm * mm, _y(entry.y_mm) - 2, f"Q{entry.q_no}")
        for i, opt in enumerate(entry.options):
            ox = entry.x_mm + MCQ_LABEL_OFFSET_MM + i * MCQ_OPTION_PITCH_MM
            c.circle(ox * mm, _y(entry.y_mm), BUBBLE_RADIUS_MM * mm, stroke=1, fill=0)
            c.setFont("Helvetica", 6)
            c.drawCentredString(ox * mm, _y(entry.y_mm) + 3, opt)


def _draw_written_block(c: canvas.Canvas, layout: SheetLayout, page_no: int, is_first: bool) -> None:
    entries = [e for e in layout.written_entries if e.page == page_no]
    if not entries:
        return
    c.setFillColorRGB(0, 0, 0)
    c.setFont("Helvetica-Bold", 10)
    label = "Section B -- Written Answers" if is_first else "Section B -- Written Answers (continued)"
    c.drawString(MARGIN_MM * mm, _y(entries[0].y_mm - WRITTEN_HEADER_MM - 4), label)
    for entry in entries:
        c.setFont("Helvetica", 8)
        c.drawString(entry.x_mm * mm, _y(entry.y_mm - 2), f"Q{entry.q_no}  [{entry.max_marks} marks]")
        box_bottom_mm = entry.y_mm + entry.height_mm
        c.rect(
            entry.x_mm * mm,
            _y(box_bottom_mm),
            entry.width_mm * mm,
            entry.height_mm * mm,
            stroke=1,
            fill=0,
        )
        if entry.lines > 1:
            for li in range(1, entry.lines):
                ly = entry.y_mm + li * (entry.height_mm / entry.lines)
                c.line(entry.x_mm * mm, _y(ly), (entry.x_mm + entry.width_mm) * mm, _y(ly))
