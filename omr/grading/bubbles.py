"""Generic manifest-driven bubble reading primitives.

Shared by MCQ grading (Section 7) and, from Phase 2 on, roll-number digit
reading (Section 6) — both are "fill-ratio thresholding per bubble" against
manifest mm coordinates (Section 2, principle 1). Nothing here hardcodes a
position; callers always pass coordinates read out of a manifest dict.
"""
from __future__ import annotations

import numpy as np

MM_PER_INCH = 25.4


def mm_to_px(x_mm: float, y_mm: float, dpi: float) -> tuple[int, int]:
    scale = dpi / MM_PER_INCH
    return round(x_mm * scale), round(y_mm * scale)


def canonical_size_px(manifest: dict, dpi: float) -> tuple[int, int]:
    """Pixel dimensions of the canonical image implied by a manifest at a given DPI."""
    scale = dpi / MM_PER_INCH
    width_px = round(manifest["page"]["width_mm"] * scale)
    height_px = round(manifest["page"]["height_mm"] * scale)
    return width_px, height_px


def fill_ratio(gray: np.ndarray, cx_px: int, cy_px: int, radius_px: int, dark_threshold: int = 150) -> float:
    """Fraction of pixels darker than `dark_threshold` within a circular bubble region."""
    h, w = gray.shape[:2]
    x0, x1 = max(0, cx_px - radius_px), min(w, cx_px + radius_px + 1)
    y0, y1 = max(0, cy_px - radius_px), min(h, cy_px + radius_px + 1)
    if x0 >= x1 or y0 >= y1:
        return 0.0
    patch = gray[y0:y1, x0:x1]
    yy, xx = np.ogrid[y0:y1, x0:x1]
    mask = (xx - cx_px) ** 2 + (yy - cy_px) ** 2 <= radius_px ** 2
    total = int(mask.sum())
    if total == 0:
        return 0.0
    dark = int((patch[mask] < dark_threshold).sum())
    return dark / total
