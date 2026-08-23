"""Page geometry and the mm <-> px conversion (PROJECT_SPEC.md Section 4.4).

The manifest stores millimeters tied to a known page size, never pixels,
so it stays resolution-independent. Pixels only exist once a canonical
image DPI is fixed, which is what these helpers do.

Coordinate convention throughout the project: millimeters from the
TOP-LEFT of the page, x increasing right and y increasing DOWN. That
matches image/pixel conventions, so the reader converts with a single
positive scale factor and no axis flip. (ReportLab's bottom-up PDF
coordinate space is an implementation detail confined to
`omr.generator.pdf_gen`.)
"""
from __future__ import annotations

MM_PER_INCH = 25.4

A4_WIDTH_MM = 210.0
A4_HEIGHT_MM = 297.0


def px_per_mm(dpi: float) -> float:
    return dpi / MM_PER_INCH


def mm_to_px(x_mm: float, y_mm: float, dpi: float) -> tuple[int, int]:
    """Convert a top-left-origin mm coordinate to a pixel coordinate."""
    scale = px_per_mm(dpi)
    return round(x_mm * scale), round(y_mm * scale)


def canonical_size_px(manifest: dict, dpi: float) -> tuple[int, int]:
    """Pixel dimensions of the canonical image a manifest implies at `dpi`.

    Module 2 normalizes every scan/photo to exactly this size, which is
    what lets the reader treat manifest mm coordinates as directly
    scalable without knowing the capture device.
    """
    scale = px_per_mm(dpi)
    return (
        round(manifest["page"]["width_mm"] * scale),
        round(manifest["page"]["height_mm"] * scale),
    )
