"""Preflight: prove a generated sheet is actually readable before it prints.

The layout engine can only assert things about its own arithmetic. The
failures that actually break an OMR sheet happen a layer below that — a
glyph drawn on top of a fiducial, a label landing inside a bubble, a marker
that comes out too faint to isolate. Every one of those passes a coordinate
assertion and fails a printer.

So this module does what the reader will do: rasterize the PDF that was
just written, and measure it. It runs on every generation, against the
professor's real config rather than a test fixture, because a sheet is
cheap to regenerate now and expensive to fix after 200 copies are printed
and written on.

An `error` means do not print this sheet. A `warning` means it will work
but something is tighter than it should be.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..contracts.geometry import mm_to_px, px_per_mm
from ..grading.bubbles import fill_ratio
from .layout import CORNER_KEEPOUT_MM

# The DPI a Phase 2 canonical image is expected to land at. Preflight
# measures at the same resolution the reader will, so a bubble that looks
# clean here looks clean to the grader.
PREFLIGHT_DPI = 200

MAX_EMPTY_BUBBLE_FILL = 0.05  # an unmarked bubble should read ~0.00
MIN_MARKER_SOLIDITY = 0.98
WHITE_FLOOR = 250  # below this is ink, not paper
MIN_BUBBLE_GAP_MM = 1.5  # clear paper required between two sample discs


@dataclass(frozen=True)
class PreflightIssue:
    severity: str  # "error" | "warning"
    check: str
    detail: str


@dataclass
class PreflightReport:
    dpi: float
    num_pages: int
    stats: dict = field(default_factory=dict)
    issues: list[PreflightIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[PreflightIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[PreflightIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def format(self) -> str:
        lines = [f"Preflight ({self.dpi:g} DPI, {self.num_pages} page(s)):"]
        for key, value in self.stats.items():
            lines.append(f"  {value}")
        for issue in self.issues:
            tag = "ERROR" if issue.severity == "error" else "warn "
            lines.append(f"  [{tag}] {issue.check}: {issue.detail}")
        if self.ok and not self.warnings:
            lines.append("  All checks passed - this sheet is ready to print.")
        elif self.ok:
            lines.append("  Usable, but see the warnings above.")
        else:
            lines.append("  DO NOT PRINT: fix the errors above and regenerate.")
        return "\n".join(lines)


def _render_pages(pdf_path: Path, dpi: float) -> dict[int, np.ndarray]:
    import pymupdf  # imported lazily so `--no-check` needs no rasterizer

    pages = {}
    with pymupdf.open(pdf_path) as doc:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=int(dpi), colorspace=pymupdf.csGRAY)
            pages[i + 1] = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return pages


def _all_bubbles(manifest: dict):
    """(page, label, x_mm, y_mm) for every bubble on the sheet."""
    for entry in manifest["mcq_block"]:
        for i, opt in enumerate(entry["options"]):
            x = entry["x_mm"] + manifest["mcq_label_offset_mm"] + i * manifest["mcq_option_pitch_mm"]
            yield entry.get("page", 1), f"Q{entry['q_no']}-{opt}", x, entry["y_mm"]

    rb = manifest["roll_number_block"]
    page = rb.get("page", 1)
    for label, pos in rb["program_selector"].items():
        yield page, f"program-{label}", pos["x_mm"], pos["y_mm"]
    for key, short in (("btech_digits", "BT"), ("mtech_digits", "MT")):
        grid = rb[key]
        for col in range(grid["columns"]):
            for digit in range(10):
                yield (
                    page,
                    f"{short}-col{col + 1}-{digit}",
                    grid["x_mm"] + col * grid["col_pitch_mm"],
                    grid["y_mm"] + digit * grid["row_pitch_mm"],
                )


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_bubbles_are_empty(manifest, pages, report) -> None:
    """Anything pre-printed inside a bubble is ink the reader can't tell
    apart from a student's mark."""
    radius_px = max(1, round(manifest["bubble_sample_radius_mm"] * px_per_mm(report.dpi)))
    worst_label, worst = None, 0.0
    count = 0
    for page, label, x_mm, y_mm in _all_bubbles(manifest):
        if page not in pages:
            continue
        count += 1
        cx, cy = mm_to_px(x_mm, y_mm, report.dpi)
        ratio = fill_ratio(pages[page], cx, cy, radius_px)
        if ratio > worst:
            worst_label, worst = label, ratio

    report.stats["bubbles"] = f"{count} bubbles, worst pre-printed fill {worst:.3f}"
    if worst > MAX_EMPTY_BUBBLE_FILL:
        report.issues.append(
            PreflightIssue(
                "error",
                "bubbles must print empty",
                f"'{worst_label}' already reads {worst:.3f} with nothing written in it "
                f"(limit {MAX_EMPTY_BUBBLE_FILL:.2f}). Something is being drawn inside the "
                "bubble - the reader will count it as part of a student's mark.",
            )
        )


def _check_fiducial_quiet_zones(manifest, pages, report) -> None:
    """A contour detector isolates a marker by finding a dark square with
    clean paper around it. Ink inside the quiet zone merges into the same
    blob and drags the computed corner off."""
    scale = px_per_mm(report.dpi)
    dirtiest = None
    for fid in manifest["fiducials"]:
        page = fid.get("page", 1)
        if page not in pages:
            continue
        image = pages[page]
        half = fid["size_mm"] / 2
        x0, y0 = mm_to_px(fid["x_mm"] - CORNER_KEEPOUT_MM, fid["y_mm"] - CORNER_KEEPOUT_MM, report.dpi)
        x1, y1 = mm_to_px(fid["x_mm"] + CORNER_KEEPOUT_MM, fid["y_mm"] + CORNER_KEEPOUT_MM, report.dpi)
        region = image[max(0, y0):y1, max(0, x0):x1].copy()

        mx0 = round((fid["x_mm"] - half) * scale) - max(0, x0)
        my0 = round((fid["y_mm"] - half) * scale) - max(0, y0)
        msize = math.ceil(fid["size_mm"] * scale)
        region[max(0, my0):my0 + msize, max(0, mx0):mx0 + msize] = 255

        darkest = int(region.min()) if region.size else 255
        if dirtiest is None or darkest < dirtiest[1]:
            dirtiest = (f"page {page} {fid['corner']}", darkest)

    if dirtiest is None:
        return
    verdict = "clear" if dirtiest[1] >= WHITE_FLOOR else "NOT clear"
    report.stats["quiet_zones"] = (
        f"fiducial quiet zones {verdict} (darkest pixel {dirtiest[1]}/255 at {dirtiest[0]})"
    )
    if dirtiest[1] < WHITE_FLOOR:
        report.issues.append(
            PreflightIssue(
                "error",
                "fiducial quiet zone",
                f"{dirtiest[0]} has ink at brightness {dirtiest[1]} inside its "
                f"{CORNER_KEEPOUT_MM}mm quiet zone. Marker detection may merge that ink into "
                "the marker and compute the wrong corner.",
            )
        )


def _check_markers_print_solid(manifest, pages, report) -> None:
    faintest = None
    for marker, kind in (
        [(f, "fiducial") for f in manifest["fiducials"]]
        + [(m, "orientation marker") for m in manifest["orientation_marker"]]
    ):
        page = marker.get("page", 1)
        if page not in pages:
            continue
        cx, cy = mm_to_px(marker["x_mm"], marker["y_mm"], report.dpi)
        r = max(1, round(marker["size_mm"] / 2 * px_per_mm(report.dpi) * 0.6))
        solidity = fill_ratio(pages[page], cx, cy, r)
        name = f"page {page} {marker.get('corner', kind)}"
        if faintest is None or solidity < faintest[1]:
            faintest = (name, solidity)

    if faintest is None:
        return
    if faintest[1] < MIN_MARKER_SOLIDITY:
        report.issues.append(
            PreflightIssue(
                "error",
                "registration markers",
                f"{faintest[0]} printed at only {faintest[1]:.2f} solidity - it may not be "
                "detectable as a marker.",
            )
        )


def _check_orientation_is_recoverable(manifest, report) -> None:
    """Four identical corner squares are symmetric under 90/180/270-degree
    rotation. Without a decisive fifth marker, an upside-down sheet reads as
    a valid upright one with every coordinate inverted."""
    for page in range(1, manifest["num_pages"] + 1):
        markers = [m for m in manifest["orientation_marker"] if m.get("page", 1) == page]
        if len(markers) != 1:
            report.issues.append(
                PreflightIssue(
                    "error",
                    "orientation",
                    f"page {page} has {len(markers)} orientation markers, expected exactly 1 - "
                    "a rotated scan of this page cannot be corrected.",
                )
            )
            continue
        om = markers[0]
        corners = {
            f["corner"]: (f["x_mm"], f["y_mm"])
            for f in manifest["fiducials"]
            if f.get("page", 1) == page
        }
        distances = {
            c: math.hypot(p[0] - om["x_mm"], p[1] - om["y_mm"]) for c, p in corners.items()
        }
        nearest = min(distances, key=distances.get)
        others = sorted(d for c, d in distances.items() if c != nearest)
        if nearest != om["marks_corner"]:
            report.issues.append(
                PreflightIssue(
                    "error",
                    "orientation",
                    f"page {page}: the marker claims to mark {om['marks_corner']} but sits "
                    f"nearest {nearest}.",
                )
            )
        elif others and distances[nearest] > others[0] / 2:
            report.issues.append(
                PreflightIssue(
                    "warning",
                    "orientation",
                    f"page {page}: the orientation marker is only {distances[nearest]:.0f}mm from "
                    f"{nearest} versus {others[0]:.0f}mm from the next corner - a heavily skewed "
                    "photo could make that call ambiguous.",
                )
            )
    report.stats["orientation"] = "orientation marker present and decisive on every page"


def _check_bubbles_do_not_crowd(manifest, report) -> None:
    """Two sample discs that nearly touch let a heavy pen mark bleed from one
    bubble into its neighbour's read region."""
    sample_r = manifest["bubble_sample_radius_mm"]
    by_page: dict[int, list] = {}
    for page, label, x_mm, y_mm in _all_bubbles(manifest):
        by_page.setdefault(page, []).append((label, x_mm, y_mm))

    tightest = None
    for page, bubbles in by_page.items():
        # Sorting by x makes the sweep below O(n * k) instead of O(n^2); a
        # dense sheet has a few hundred bubbles per page.
        bubbles.sort(key=lambda b: b[1])
        for i, (label_a, ax, ay) in enumerate(bubbles):
            for label_b, bx, by in bubbles[i + 1:]:
                if bx - ax > 12.0:
                    break
                gap = math.hypot(bx - ax, by - ay) - 2 * sample_r
                if tightest is None or gap < tightest[0]:
                    tightest = (gap, f"{label_a} / {label_b}", page)

    if tightest is None:
        return
    gap, pair, page = tightest
    report.stats["spacing"] = f"tightest bubble gap {gap:.1f}mm ({pair})"
    if gap < 0:
        report.issues.append(
            PreflightIssue(
                "error",
                "bubble spacing",
                f"page {page}: {pair} overlap by {-gap:.1f}mm - one mark would be read as two.",
            )
        )
    elif gap < MIN_BUBBLE_GAP_MM:
        report.issues.append(
            PreflightIssue(
                "warning",
                "bubble spacing",
                f"page {page}: only {gap:.1f}mm of paper between {pair} "
                f"(prefer {MIN_BUBBLE_GAP_MM:.1f}mm) - a heavy mark could bleed across.",
            )
        )


def _check_content_stays_on_the_page(manifest, report) -> None:
    w, h = manifest["page"]["width_mm"], manifest["page"]["height_mm"]
    r = manifest["bubble_radius_mm"]
    for page, label, x_mm, y_mm in _all_bubbles(manifest):
        if not (r <= x_mm <= w - r and r <= y_mm <= h - r):
            report.issues.append(
                PreflightIssue(
                    "error", "page bounds", f"bubble {label} at ({x_mm:.1f}, {y_mm:.1f})mm is off the page."
                )
            )
    for e in manifest["written_block"]:
        if e["x_mm"] + e["width_mm"] > w + 1e-6 or e["y_mm"] + e["height_mm"] > h + 1e-6:
            report.issues.append(
                PreflightIssue("error", "page bounds", f"written box Q{e['q_no']} runs off the page.")
            )


# ---------------------------------------------------------------------------

def check_sheet(pdf_path: str | Path, manifest: dict, dpi: float = PREFLIGHT_DPI) -> PreflightReport:
    """Rasterize a generated sheet and verify it is machine-readable."""
    report = PreflightReport(dpi=dpi, num_pages=manifest["num_pages"])
    pages = _render_pages(Path(pdf_path), dpi)

    if len(pages) != manifest["num_pages"]:
        report.issues.append(
            PreflightIssue(
                "error",
                "page count",
                f"the PDF has {len(pages)} page(s) but the manifest describes "
                f"{manifest['num_pages']} - the grader would read the wrong page.",
            )
        )

    _check_bubbles_are_empty(manifest, pages, report)
    _check_fiducial_quiet_zones(manifest, pages, report)
    _check_markers_print_solid(manifest, pages, report)
    _check_orientation_is_recoverable(manifest, report)
    _check_bubbles_do_not_crowd(manifest, report)
    _check_content_stays_on_the_page(manifest, report)
    return report
