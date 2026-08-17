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
from ..grading.bubbles import fill_ratio, ink_density
from .metrics import CORNER_KEEPOUT_MM, ORIENTATION_KEEPOUT_MM

# The DPI a Phase 2 canonical image is expected to land at. Preflight
# measures at the same resolution the reader will, so a bubble that looks
# clean here looks clean to the grader.
PREFLIGHT_DPI = 200

MAX_EMPTY_BUBBLE_FILL = 0.05  # an unmarked bubble should read ~0.00
MIN_MARKER_SOLIDITY = 0.98
WHITE_FLOOR = 250  # below this is ink, not paper
MIN_BUBBLE_GAP_MM = 1.5  # clear paper required between two sample discs
MIN_PAGE_MARK_CONTRAST = 0.5  # gap between a filled bar and an outlined one
RULE_MASK_MM = 0.8  # how much of a printed rule/border counts as that rule


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


def _darkest_outside_marker(image, marker, keepout_mm: float, dpi: float) -> int:
    """The darkest pixel in a marker's quiet zone, with the marker itself
    masked out."""
    scale = px_per_mm(dpi)
    half = marker["size_mm"] / 2
    x0, y0 = mm_to_px(marker["x_mm"] - keepout_mm, marker["y_mm"] - keepout_mm, dpi)
    x1, y1 = mm_to_px(marker["x_mm"] + keepout_mm, marker["y_mm"] + keepout_mm, dpi)
    region = image[max(0, y0):y1, max(0, x0):x1].copy()
    if not region.size:
        return 255

    # Blank out the marker itself, padded by a couple of pixels: the
    # rectangle's edges land on fractional pixel boundaries and render
    # antialiased, and that grey fringe belongs to the marker, not to
    # whatever else might be in the zone.
    pad = 2
    mx0 = math.floor((marker["x_mm"] - half) * scale) - max(0, x0) - pad
    my0 = math.floor((marker["y_mm"] - half) * scale) - max(0, y0) - pad
    msize = math.ceil(marker["size_mm"] * scale) + 2 * pad
    region[max(0, my0):my0 + msize, max(0, mx0):mx0 + msize] = 255
    return int(region.min())


def _check_marker_quiet_zones(manifest, pages, report) -> None:
    """A contour detector isolates a marker by finding a dark square with
    clean paper around it. Ink inside the quiet zone merges into the same
    blob and drags the computed corner off.

    This covers the orientation marker too, not just the four corners: it is
    found the same way, and if it merges with a glyph the sheet loses its only
    defence against being read upside down.
    """
    checked = [(f, CORNER_KEEPOUT_MM, f["corner"]) for f in manifest["fiducials"]]
    checked += [(m, ORIENTATION_KEEPOUT_MM, "orientation") for m in manifest["orientation_marker"]]

    dirtiest = None
    for marker, keepout, name in checked:
        page = marker.get("page", 1)
        if page not in pages:
            continue
        darkest = _darkest_outside_marker(pages[page], marker, keepout, report.dpi)
        if dirtiest is None or darkest < dirtiest[1]:
            dirtiest = (f"page {page} {name}", darkest, keepout)

    if dirtiest is None:
        return
    where, darkest, keepout = dirtiest
    verdict = "clear" if darkest >= WHITE_FLOOR else "NOT clear"
    report.stats["quiet_zones"] = (
        f"marker quiet zones {verdict} (darkest pixel {darkest}/255 at {where})"
    )
    if darkest < WHITE_FLOOR:
        report.issues.append(
            PreflightIssue(
                "error",
                "fiducial quiet zone",
                f"{where} has ink at brightness {darkest} inside its {keepout}mm quiet zone. "
                "Marker detection may merge that ink into the marker and compute the wrong corner.",
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


def _check_printer_safe_margins(manifest, pages, report) -> None:
    """No ink at all in the border band an ordinary A4 printer can't reach.

    This is a pixel check rather than a coordinate one because it has to
    catch everything that lands on the page, including glyph overhang and
    stroke width straddling a nominal coordinate. A clipped fiducial is the
    failure that matters most: the surviving shape is still roughly square,
    so detection succeeds and hands back a centroid a few millimetres off,
    skewing every coordinate derived from it — silently, on every sheet.
    """
    margin_mm = manifest.get("printer_safe_margin_mm")
    if not margin_mm:
        return
    scale = px_per_mm(report.dpi)

    worst = None  # (clearance_mm, page, edge)
    for page, image in sorted(pages.items()):
        h, w = image.shape[:2]
        ink_rows = np.flatnonzero(np.any(image < WHITE_FLOOR, axis=1))
        ink_cols = np.flatnonzero(np.any(image < WHITE_FLOOR, axis=0))
        if ink_rows.size == 0 or ink_cols.size == 0:
            continue
        for edge, clearance in (
            ("top", ink_rows[0] / scale),
            ("bottom", (h - 1 - ink_rows[-1]) / scale),
            ("left", ink_cols[0] / scale),
            ("right", (w - 1 - ink_cols[-1]) / scale),
        ):
            if worst is None or clearance < worst[0]:
                worst = (clearance, page, edge)

    if worst is None:
        return
    clearance, page, edge = worst
    # Report the measured number, not just a verdict: printers state their
    # unprintable border in their spec sheet, and this is the figure to
    # compare it against.
    report.stats["safe_area"] = (
        f"closest ink to a page edge: {clearance:.1f}mm ({edge}, page {page}); "
        f"safe area is {margin_mm:g}mm"
    )
    if clearance < margin_mm:
        report.issues.append(
            PreflightIssue(
                "error",
                "printer-safe margin",
                f"page {page} has ink only {clearance:.1f}mm from the {edge} edge, inside the "
                f"{margin_mm:g}mm safe area. An ordinary A4 printer will clip it at 100% scale.",
            )
        )


def _check_page_bars_identify_their_page(manifest, pages, report) -> None:
    """Every page must be able to say which page it is, from ink alone.

    The manifest already asserts the arithmetic (exactly one bar filled, at
    the right index). This checks the pixels actually came out that way, and
    that a filled bar is separable from an outlined one by a wide margin —
    if the two read close together, a slightly grey scan turns page 3 into
    page 1 and puts a student's answers on the wrong questions.
    """
    scale = px_per_mm(report.dpi)
    worst = None  # (contrast, page)
    for page in sorted(pages):
        marks = [m for m in manifest["page_marks"] if m.get("page", 1) == page]
        if not marks:
            continue
        readings = {}
        for m in marks:
            cx, cy = mm_to_px(m["x_mm"], m["y_mm"], report.dpi)
            # Sample inside the bar's short axis so its own outline is excluded.
            r = max(1, round(m["height_mm"] / 2 * scale) - 2)
            readings[m["index"]] = ink_density(pages[page], cx, cy, r)

        dark = [i for i, v in readings.items() if v > 0.5]
        if dark != [page]:
            report.issues.append(
                PreflightIssue(
                    "error",
                    "page index",
                    f"page {page}'s bars read as {dark or 'nothing'} filled, expected exactly "
                    f"[{page}]. The reader could not tell which page this is.",
                )
            )
            continue
        others = [v for i, v in readings.items() if i != page]
        contrast = readings[page] - (max(others) if others else 0.0)
        if worst is None or contrast < worst[0]:
            worst = (contrast, page)

    if worst is None:
        return
    contrast, page = worst
    report.stats["page_index"] = f"page-index bars readable, weakest contrast {contrast:.2f} (page {page})"
    if contrast < MIN_PAGE_MARK_CONTRAST:
        report.issues.append(
            PreflightIssue(
                "warning",
                "page index",
                f"page {page}'s filled bar is only {contrast:.2f} darker than an empty one "
                f"(prefer {MIN_PAGE_MARK_CONTRAST:.2f}) - a grey scan could confuse them.",
            )
        )


def _check_written_boxes_are_clean(manifest, pages, report) -> None:
    """A written box is the crop that gets sent to a vision LLM (Section 8).
    It should hold the student's handwriting and the box's own ruled lines, and
    nothing else — a stray label, bubble or section header inside the crop is
    noise competing with the answer.

    Mean darkness would not catch that: a whole row of MCQ bubbles inside a
    186x14mm box is under 1% of its area. So this masks out the border and the
    rule rows the box is *supposed* to have, and asserts what is left is bare
    paper — the same technique as the fiducial quiet-zone check.
    """
    scale = px_per_mm(report.dpi)
    pad = max(2, round(RULE_MASK_MM * scale))
    dirtiest = None  # (darkest, q_no, page)

    for e in manifest["written_block"]:
        page = e.get("page", 1)
        if page not in pages:
            continue
        x0, y0 = mm_to_px(e["x_mm"], e["y_mm"], report.dpi)
        x1 = x0 + max(1, round(e["width_mm"] * scale))
        y1 = y0 + max(1, round(e["height_mm"] * scale))
        region = pages[page][max(0, y0):y1 + 1, max(0, x0):x1 + 1].copy()
        if region.size == 0:
            continue

        # Mask the box's own border...
        region[:pad, :] = 255
        region[-pad:, :] = 255
        region[:, :pad] = 255
        region[:, -pad:] = 255
        # ...and each writing rule it is supposed to have inside it.
        pitch_px = (e["height_mm"] / e["lines"]) * scale
        for i in range(1, e["lines"]):
            ry = round(i * pitch_px)
            region[max(0, ry - pad):ry + pad, :] = 255

        darkest = int(region.min())
        if dirtiest is None or darkest < dirtiest[0]:
            dirtiest = (darkest, e["q_no"], page)

    if dirtiest is None:
        return
    darkest, q_no, page = dirtiest
    verdict = "clean" if darkest >= WHITE_FLOOR else "NOT clean"
    report.stats["written_boxes"] = (
        f"{len(manifest['written_block'])} answer boxes {verdict} "
        f"(darkest non-rule pixel {darkest}/255 in Q{q_no})"
    )
    if darkest < WHITE_FLOOR:
        report.issues.append(
            PreflightIssue(
                "error",
                "answer box must be blank",
                f"page {page}: Q{q_no}'s answer box already contains ink at brightness {darkest} "
                "that is not one of its writing rules. Whatever that is will be cropped and sent "
                "to the grader alongside the student's handwriting.",
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

    _check_printer_safe_margins(manifest, pages, report)
    _check_bubbles_are_empty(manifest, pages, report)
    _check_written_boxes_are_clean(manifest, pages, report)
    _check_marker_quiet_zones(manifest, pages, report)
    _check_markers_print_solid(manifest, pages, report)
    _check_orientation_is_recoverable(manifest, report)
    _check_page_bars_identify_their_page(manifest, pages, report)
    _check_bubbles_do_not_crowd(manifest, report)
    _check_content_stays_on_the_page(manifest, report)
    return report
