"""Generic manifest-driven bubble reading primitives.

Shared by MCQ grading (Section 7) and, from Phase 2 on, roll-number digit
reading (Section 6) — both are "fill-ratio thresholding per bubble" against
manifest mm coordinates (Section 2, principle 1). Nothing here hardcodes a
position; callers always pass coordinates read out of a manifest dict.

The mm->px conversion itself lives in `omr.contracts.geometry`, since the
generator and the reader must agree on it exactly. This module is the
reading half only.
"""
from __future__ import annotations

import numpy as np

from ..contracts.geometry import MM_PER_INCH, canonical_size_px, mm_to_px, px_per_mm

__all__ = [
    "MM_PER_INCH",
    "canonical_size_px",
    "fill_ratio",
    "ink_density",
    "mm_to_px",
    "px_per_mm",
    "STUDENT_MARK_CORE_RATIO",
    "student_mark_fill_ratio",
    "enhanced_student_mark_fill_ratio",
]

DARK_THRESHOLD = 150
STUDENT_MARK_CORE_RATIO = 0.45


def _as_gray_array(image: np.ndarray) -> np.ndarray:
    gray = np.asarray(image)
    if gray.ndim == 3:
        return gray.mean(axis=2).astype(np.uint8)
    return gray


def _disc(gray: np.ndarray, cx_px: int, cy_px: int, radius_px: int) -> np.ndarray | None:
    """The pixels inside a circular bubble region, or None if it's off-image."""
    gray = _as_gray_array(gray)
    h, w = gray.shape[:2]
    x0, x1 = max(0, cx_px - radius_px), min(w, cx_px + radius_px + 1)
    y0, y1 = max(0, cy_px - radius_px), min(h, cy_px + radius_px + 1)
    if x0 >= x1 or y0 >= y1:
        return None
    yy, xx = np.ogrid[y0:y1, x0:x1]
    mask = (xx - cx_px) ** 2 + (yy - cy_px) ** 2 <= radius_px ** 2
    if not mask.any():
        return None
    return gray[y0:y1, x0:x1][mask]


def fill_ratio(
    gray: np.ndarray, cx_px: int, cy_px: int, radius_px: int, dark_threshold: int = DARK_THRESHOLD
) -> float:
    """Fraction of pixels darker than `dark_threshold` within a bubble region.

    This is the primary fill signal and it is deliberately a hard threshold:
    it is stable across scanner exposure and ignores the grey haze a phone
    photo puts on white paper. Its blind spot is a fill that covers the
    whole bubble but never gets dark — see `ink_density`.
    """
    pixels = _disc(gray, cx_px, cy_px, radius_px)
    if pixels is None:
        return 0.0
    return float((pixels < dark_threshold).sum()) / pixels.size


def student_mark_fill_ratio(
    gray: np.ndarray, cx_px: int, cy_px: int, radius_px: int, dark_threshold: int = DARK_THRESHOLD
) -> float:
    """Boundary-resistant fill signal for answer/roll bubbles.

    Printed bubble outlines live near the edge. If alignment is off by even a
    little, the outline can leak into the sample disc and inflate a normal
    fill ratio. A real student fill should be visible in the whole sampled
    disc and in the inner core, so use the weaker of those two signals.
    """
    full = fill_ratio(gray, cx_px, cy_px, radius_px, dark_threshold)
    core = fill_ratio(gray, cx_px, cy_px, max(1, round(radius_px * STUDENT_MARK_CORE_RATIO)), dark_threshold)
    return min(full, core)


def ink_density(gray: np.ndarray, cx_px: int, cy_px: int, radius_px: int) -> float:
    """Mean darkness in a bubble region: 0.0 is blank paper, 1.0 solid black.

    Complements `fill_ratio` rather than replacing it. A light pencil fill
    covering the entire bubble can sit at grey ~170 and never cross the hard
    dark threshold, so `fill_ratio` reports 0.0 — indistinguishable from a
    student who skipped the question. `ink_density` reports ~0.33 for the
    same bubble, which is what lets the reader say "something was written
    here, a human should look" instead of silently scoring it zero.
    """
    # Use the core for mean darkness. The printed bubble outline sits near
    # the edge and camera blur can pull that outline into the sample area,
    # so the centre is the cleaner signal for "student filled this".
    pixels = _disc(gray, cx_px, cy_px, max(1, round(radius_px * 0.70)))
    if pixels is None:
        return 0.0
    return float((255.0 - pixels.astype(np.float32)).mean()) / 255.0


def enhanced_student_mark_fill_ratio(
    gray: np.ndarray,
    cx_px: int,
    cy_px: int,
    radius_px: int,
    *,
    enhanced_gray: np.ndarray | None = None,
) -> float:
    """Faint-pencil retry ratio using darkness relative to nearby paper.

    A faint mark may be grey rather than black, so a fixed intensity cutoff
    misses it. Here the cutoff is the local annulus median minus a modest
    contrast amount. The core-and-full rule remains in place to reject the
    printed bubble outline. This is a review-only recovery signal.
    """
    enhanced = enhance_faint_marks_page(gray) if enhanced_gray is None else enhanced_gray
    source = _as_gray_array(gray)

    def locally_contrasted_ratio(image: np.ndarray) -> float:
        h, w = image.shape[:2]
        outer = max(radius_px + 2, round(radius_px * 1.8))
        x0, x1 = max(0, cx_px - outer), min(w, cx_px + outer + 1)
        y0, y1 = max(0, cy_px - outer), min(h, cy_px + outer + 1)
        if x0 >= x1 or y0 >= y1:
            return 0.0
        yy, xx = np.ogrid[y0:y1, x0:x1]
        distance2 = (xx - cx_px) ** 2 + (yy - cy_px) ** 2
        annulus = (distance2 >= max(radius_px + 1, round(radius_px * 1.15)) ** 2) & (distance2 <= outer**2)
        nearby_paper = image[y0:y1, x0:x1][annulus]
        if nearby_paper.size < 8:
            return 0.0
        # The ring crosses the printed bubble outline. Paper is represented by
        # its bright portion, not the median of a region contaminated by that
        # dark outline or nearby printed labels.
        paper_reference = float(np.percentile(nearby_paper, 80))
        bright_paper = nearby_paper[nearby_paper >= np.percentile(nearby_paper, 55)]
        paper_mad = float(np.median(np.abs(bright_paper.astype(np.float32) - paper_reference)))
        contrast_gap = int(np.clip(round(8.0 + 1.5 * paper_mad), 8, 18))
        threshold = max(0, int(paper_reference) - contrast_gap)
        full_pixels = _disc(image, cx_px, cy_px, radius_px)
        core_pixels = _disc(image, cx_px, cy_px, max(1, round(radius_px * STUDENT_MARK_CORE_RATIO)))
        if full_pixels is None or core_pixels is None:
            return 0.0
        full = float((full_pixels < threshold).sum()) / full_pixels.size
        core = float((core_pixels < threshold).sum()) / core_pixels.size
        return min(full, core)

    # A page-level enhancement can flatten a tiny light mark if its blur
    # background includes the bubble. Preserve the raw local-paper result as
    # a second view; both are later gated by neighbouring raw-bubble ink.
    return max(locally_contrasted_ratio(source), locally_contrasted_ratio(enhanced))


def enhance_faint_marks_page(gray: np.ndarray) -> np.ndarray:
    """Create one reusable faint-mark diagnostic image for an aligned page."""
    from omr.reader.enhancement import enhance_faint_ink

    return enhance_faint_ink(gray)
