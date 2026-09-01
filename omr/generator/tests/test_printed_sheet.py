"""Tests against the rasterized PDF — what actually comes out of a printer.

Everything else in this suite tests the layout engine's arithmetic. These
tests render the real PDF to pixels and look at it, because the failures
that matter most here are ones the arithmetic can't see: a glyph drawn on
top of a fiducial, a label printed inside a bubble, a marker that a
detector can't isolate. Each of those was a real defect in this generator,
and each was invisible to a coordinate assertion.
"""
from __future__ import annotations

import math

import numpy as np
import pymupdf
import pytest

from omr.contracts.geometry import mm_to_px, px_per_mm
from omr.generator.config import ExamConfig, WrittenQuestionConfig
from omr.generator.generate import generate_exam
from omr.generator.metrics import (
    BUBBLE_RADIUS_MM,
    CORNER_KEEPOUT_MM,
    CONT_BTECH_SELECTOR_X_MM,
    CONT_MTECH_SELECTOR_X_MM,
    CONT_PROGRAM_SELECTOR_Y_MM,
    MARGIN_MM,
    PAGE_HEIGHT_MM,
    PAGE_WIDTH_MM,
    corner_keepouts,
    identity_box,
)
from omr.grading.bubbles import fill_ratio, ink_density, student_mark_fill_ratio

DPI = 200
WHITE_FLOOR = 250  # anything above this is blank paper

# A stroke lands on fractional pixel boundaries and renders antialiased, so a
# few pixels either side of a drawn line belong to that line. Ignoring this is
# how a check ends up measuring its own target.
STROKE_PAD_PX = 3


def render(tmp_path, config) -> tuple[dict, dict[int, np.ndarray]]:
    """Generate a sheet and hand back its manifest plus one grayscale array
    per page, at the canonical DPI the reader would use."""
    result = generate_exam(config, tmp_path)
    doc = pymupdf.open(result["pdf_path"])
    pages = {}
    for i, page in enumerate(doc):
        pix = page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
        pages[i + 1] = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return result["manifest"], pages


def a_config(**overrides) -> ExamConfig:
    base = dict(
        exam_id="PRINT_TEST",
        course_code="CS301",
        exam_name="Mid-Semester Examination",
        exam_type="midsem",
        num_mcq=20,
        mcq_options=4,
        marks_per_mcq=1,
        written_questions=[
            WrittenQuestionConfig(q_no=21, max_marks=5, lines=2),
            WrittenQuestionConfig(q_no=22, max_marks=5, lines=2),
            WrittenQuestionConfig(q_no=23, max_marks=10, lines=4),
        ],
    )
    base.update(overrides)
    return ExamConfig(**base)


def every_bubble_center_mm(manifest: dict):
    """(page, label, x_mm, y_mm) for every bubble the sheet prints."""
    for entry in manifest["mcq_block"]:
        for i, opt in enumerate(entry["options"]):
            x = entry["x_mm"] + manifest["mcq_label_offset_mm"] + i * manifest["mcq_option_pitch_mm"]
            yield entry.get("page", 1), f"Q{entry['q_no']}{opt}", x, entry["y_mm"]

    rb = manifest["roll_number_block"]
    page = rb.get("page", 1)
    for label, pos in rb["program_selector"].items():
        yield page, f"program:{label}", pos["x_mm"], pos["y_mm"]
    for key in ("btech_digits", "mtech_digits"):
        grid = rb[key]
        for col in range(grid["columns"]):
            for digit in range(10):
                yield (
                    page,
                    f"{key}[{col}]={digit}",
                    grid["x_mm"] + col * grid["col_pitch_mm"],
                    grid["y_mm"] + digit * grid["row_pitch_mm"],
                )


# ---------------------------------------------------------------------------
# Nothing is printed inside a bubble
# ---------------------------------------------------------------------------

def test_every_bubble_prints_completely_empty(tmp_path):
    """The defect this replaces: option letters were drawn straddling the
    bubble outline and digit labels sat dead-center inside the roll-number
    bubbles, so an untouched bubble measured 0.20-0.26 against a 0.50 fill
    threshold — half the usable signal range gone before a student had
    written anything."""
    manifest, pages = render(tmp_path, a_config())
    radius_px = max(1, round(manifest["bubble_sample_radius_mm"] * px_per_mm(DPI)))

    worst = []
    for page, label, x_mm, y_mm in every_bubble_center_mm(manifest):
        cx, cy = mm_to_px(x_mm, y_mm, DPI)
        ratio = fill_ratio(pages[page], cx, cy, radius_px)
        if ratio > 0.02:
            worst.append(f"{label}={ratio:.3f}")

    assert not worst, "bubbles are not blank on the printed sheet: " + ", ".join(worst[:10])


def test_sample_radius_stays_inside_the_printed_outline(tmp_path):
    """Sampling the full printed radius would count the bubble's own outline
    stroke as student ink. Reading the ring between the sampled radius and
    the printed one proves the outline is really out there, not inside."""
    manifest, pages = render(tmp_path, a_config())
    printed_px = manifest["bubble_radius_mm"] * px_per_mm(DPI)
    sampled_px = manifest["bubble_sample_radius_mm"] * px_per_mm(DPI)
    assert sampled_px < printed_px

    entry = manifest["mcq_block"][0]
    x = entry["x_mm"] + manifest["mcq_label_offset_mm"]
    cx, cy = mm_to_px(x, entry["y_mm"], DPI)

    inside = fill_ratio(pages[1], cx, cy, round(sampled_px))
    through_outline = fill_ratio(pages[1], cx, cy, round(printed_px) + 2)
    assert inside == pytest.approx(0.0, abs=0.02)
    assert through_outline > 0.1, "expected to find the printed outline just outside the sample radius"


def test_student_fill_signal_resists_shifted_empty_bubble_outline(tmp_path):
    manifest, pages = render(tmp_path, a_config())
    radius_px = max(1, round(manifest["bubble_sample_radius_mm"] * px_per_mm(DPI)))
    entry = manifest["mcq_block"][0]
    x = entry["x_mm"] + manifest["mcq_label_offset_mm"]
    cx, cy = mm_to_px(x, entry["y_mm"], DPI)

    shifted_x = cx + round(manifest["bubble_radius_mm"] * px_per_mm(DPI) * 0.55)
    full_ratio = fill_ratio(pages[1], shifted_x, cy, radius_px)
    student_ratio = student_mark_fill_ratio(pages[1], shifted_x, cy, radius_px)

    assert full_ratio > 0.02, "test setup should include leaked outline ink"
    assert student_ratio < 0.02


# ---------------------------------------------------------------------------
# Fiducials
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("config", [a_config(), a_config(num_mcq=10, written_questions=[
    WrittenQuestionConfig(q_no=11 + i, max_marks=5, lines=3) for i in range(10)
])], ids=["single_page", "multi_page"])
def test_fiducial_quiet_zones_are_blank_on_every_page(config, tmp_path):
    """A contour detector finds a marker by isolating a dark square. The
    title used to sit 0.45mm under the top-left marker, close enough to
    merge into one blob and drag the computed centroid off the real corner.
    Every page's markers must be surrounded by clean paper."""
    manifest, pages = render(tmp_path, config)
    scale = px_per_mm(DPI)

    for page_no, image in pages.items():
        for fid in (f for f in manifest["fiducials"] if f.get("page", 1) == page_no):
            half = fid["size_mm"] / 2
            keep = CORNER_KEEPOUT_MM
            x0, y0 = mm_to_px(fid["x_mm"] - keep, fid["y_mm"] - keep, DPI)
            x1, y1 = mm_to_px(fid["x_mm"] + keep, fid["y_mm"] + keep, DPI)
            region = image[max(0, y0):y1, max(0, x0):x1].astype(np.int16)

            # Blank out the marker itself, padded for the antialiased fringe
            # its fractional pixel boundary produces; whatever is left must
            # be paper.
            pad = 2
            mx0 = math.floor((fid["x_mm"] - half) * scale) - max(0, x0) - pad
            my0 = math.floor((fid["y_mm"] - half) * scale) - max(0, y0) - pad
            msize = math.ceil(fid["size_mm"] * scale) + 2 * pad
            ring = region.copy()
            ring[max(0, my0):my0 + msize, max(0, mx0):mx0 + msize] = 255

            darkest = int(ring.min())
            assert darkest >= WHITE_FLOOR, (
                f"page {page_no} {fid['corner']} marker has ink at {darkest} inside its "
                f"{CORNER_KEEPOUT_MM}mm quiet zone - a detector may merge it with that ink"
            )


def test_fiducials_and_orientation_marker_actually_print_solid(tmp_path):
    manifest, pages = render(tmp_path, a_config())
    for fid in manifest["fiducials"]:
        cx, cy = mm_to_px(fid["x_mm"], fid["y_mm"], DPI)
        r = round(fid["size_mm"] / 2 * px_per_mm(DPI) * 0.7)
        assert fill_ratio(pages[fid["page"]], cx, cy, r) > 0.99

    for om in manifest["orientation_marker"]:
        cx, cy = mm_to_px(om["x_mm"], om["y_mm"], DPI)
        r = max(1, round(om["size_mm"] / 2 * px_per_mm(DPI) * 0.6))
        assert fill_ratio(pages[om["page"]], cx, cy, r) > 0.99


def test_orientation_marker_is_unambiguously_nearest_the_top_left(tmp_path):
    """Four identical corner squares are symmetric under 90/180/270-degree
    rotation, so an upside-down sheet reads as a valid upright one with
    every coordinate inverted. The detector resolves this by finding the
    corner marker nearest the small fifth marker — which only works if that
    nearest-ness is decisive, and survives any rotation."""
    manifest, _pages = render(tmp_path, a_config())
    om = manifest["orientation_marker"][0]
    corners = {f["corner"]: (f["x_mm"], f["y_mm"]) for f in manifest["fiducials"] if f["page"] == 1}

    def dist(corner):
        x, y = corners[corner]
        return math.hypot(x - om["x_mm"], y - om["y_mm"])

    assert om["marks_corner"] == "TL"
    others = sorted(dist(c) for c in ("TR", "BL", "BR"))
    assert dist("TL") < others[0] / 2, (
        "the orientation marker must be decisively nearest its corner, not marginally so"
    )

    # Rotation is a rigid transform, so distances are preserved: whichever
    # corner square is nearest stays nearest at 90/180/270 degrees.
    for rotate in (
        lambda x, y: (PAGE_WIDTH_MM - x, PAGE_HEIGHT_MM - y),      # 180
        lambda x, y: (y, PAGE_WIDTH_MM - x),                        # 90
        lambda x, y: (PAGE_HEIGHT_MM - y, x),                       # 270
    ):
        ox, oy = rotate(om["x_mm"], om["y_mm"])
        rotated = {c: rotate(*p) for c, p in corners.items()}
        nearest = min(rotated, key=lambda c: math.hypot(rotated[c][0] - ox, rotated[c][1] - oy))
        assert nearest == "TL"


def test_orientation_marker_is_distinguishable_from_a_corner_marker(tmp_path):
    """The detector separates the fifth marker from the four corner ones by
    size, so the gap has to be wide enough to survive perspective foreshortening
    in a phone photo."""
    manifest, _pages = render(tmp_path, a_config())
    corner_area = manifest["fiducial_size_mm"] ** 2
    om_area = manifest["orientation_marker"][0]["size_mm"] ** 2
    assert corner_area / om_area >= 3.0


# ---------------------------------------------------------------------------
# Content stays out of the corners
# ---------------------------------------------------------------------------

MULTI_PAGE = a_config(
    exam_id="PRINT_MULTI",
    num_mcq=10,
    written_questions=[WrittenQuestionConfig(q_no=11 + i, max_marks=5, lines=2) for i in range(10)],
)


@pytest.mark.parametrize("config", [a_config(), MULTI_PAGE], ids=["single_page", "multi_page"])
def test_the_header_never_collides_with_the_identity_block(config, tmp_path):
    """The header sits directly above the identity block on every page, and the
    two bands are positioned independently. On continuation pages they once
    overlapped by 1.5mm, so the box outline printed straight through the middle
    of the course/exam-id line — legible enough to miss in a thumbnail, wrong
    enough to look broken on paper.

    The assertion is on the MEASURED clearance rather than on the constants,
    because a glyph's descender reaches below the baseline the constant names.
    """
    manifest, pages = render(tmp_path, config)
    scale = px_per_mm(DPI)

    for page_no, image in pages.items():
        top, _bottom = identity_box(page_no)
        _, border_row = mm_to_px(0, top, DPI)
        band = range(max(0, border_row - int(9 * scale)), border_row - STROKE_PAD_PX)
        inked = [y for y in band if image[y].min() < WHITE_FLOOR]
        assert inked, f"page {page_no}: expected header text above the identity block"
        clearance = (border_row - max(inked)) / scale
        assert clearance >= 1.0, (
            f"page {page_no}: header ink is only {clearance:.2f}mm above the identity block's "
            "border - one font change from printing through it"
        )


def test_university_name_prints_centered_in_the_header(tmp_path):
    university = "Indraprastha Institute of Information Technology Delhi"
    result = generate_exam(a_config(university_name=university), tmp_path)
    doc = pymupdf.open(result["pdf_path"])
    page = doc[0]
    spans = [
        span
        for block in page.get_text("dict")["blocks"]
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        if university in span["text"]
    ]
    assert spans, "university name was not printed"
    x0, _y0, x1, _y1 = spans[0]["bbox"]
    assert abs(((x0 + x1) / 2) - (page.rect.width / 2)) < 1.0


@pytest.mark.parametrize("config", [a_config(), MULTI_PAGE], ids=["single_page", "multi_page"])
def test_every_page_prints_a_roll_number_strip_a_human_can_read(config, tmp_path):
    """"Roll number on every page" has to be true of the paper, not just the
    manifest. Each cell must print as an empty box: visible border, blank
    middle."""
    manifest, pages = render(tmp_path, config)
    scale = px_per_mm(DPI)

    for page_no in pages:
        strips = [
            f for f in manifest["write_in_fields"]
            if f["page"] == page_no and f["name"] == "roll_number"
        ]
        assert strips, f"page {page_no} printed no roll-number strip"
        for strip in strips:
            for i in range(strip["cells"]):
                cx = strip["x_mm"] + i * strip["cell_pitch_mm"] + strip["cell_width_mm"] / 2
                cy = strip["y_mm"] + strip["height_mm"] / 2
                px, py = mm_to_px(cx, cy, DPI)
                inside = ink_density(pages[page_no], px, py, max(1, round(1.5 * scale)))
                assert inside < 0.02, f"page {page_no} roll cell {i} is not blank ({inside:.3f})"
                # ...and the cell's own border really is there to write inside.
                x0, y0 = mm_to_px(strip["x_mm"] + i * strip["cell_pitch_mm"], strip["y_mm"], DPI)
                x1 = x0 + round(strip["cell_width_mm"] * scale)
                y1 = y0 + round(strip["height_mm"] * scale)
                assert pages[page_no][y0:y1 + 1, x0:x1 + 1].min() < WHITE_FLOOR


def test_continuation_pages_print_program_choices_without_digit_grid(tmp_path):
    manifest, pages = render(tmp_path, MULTI_PAGE)
    if manifest["num_pages"] < 2:
        pytest.skip("needs a continuation page")
    scale = px_per_mm(DPI)

    for page_no in range(2, manifest["num_pages"] + 1):
        for label, x_mm in (
            ("BTECH", CONT_BTECH_SELECTOR_X_MM),
            ("MTECH", CONT_MTECH_SELECTOR_X_MM),
        ):
            cx, cy = mm_to_px(x_mm, CONT_PROGRAM_SELECTOR_Y_MM, DPI)
            inside = fill_ratio(pages[page_no], cx, cy, max(1, round(BUBBLE_RADIUS_MM * 0.6 * scale)))
            through_outline = fill_ratio(pages[page_no], cx, cy, round(BUBBLE_RADIUS_MM * scale) + 2)
            assert inside == pytest.approx(0.0, abs=0.02), (
                f"page {page_no} {label} selector has ink inside it"
            )
            assert through_outline > 0.1, f"page {page_no} {label} selector outline did not print"


@pytest.mark.parametrize("config", [a_config(), MULTI_PAGE], ids=["single_page", "multi_page"])
def test_each_page_prints_exactly_one_solid_page_index_bar(config, tmp_path):
    """The reader recovers a page's own index from these bars. Exactly one dark
    per page is also the checksum that says the read is trustworthy."""
    manifest, pages = render(tmp_path, config)
    scale = px_per_mm(DPI)

    for page_no, image in pages.items():
        bars = [m for m in manifest["page_marks"] if m["page"] == page_no]
        dark = []
        for m in bars:
            cx, cy = mm_to_px(m["x_mm"], m["y_mm"], DPI)
            r = max(1, round(m["height_mm"] / 2 * scale) - 2)
            if ink_density(image, cx, cy, r) > 0.5:
                dark.append(m["index"])
        assert dark == [page_no], f"page {page_no}'s bars read as {dark}"


@pytest.mark.parametrize("config", [a_config(), MULTI_PAGE], ids=["single_page", "multi_page"])
def test_header_text_never_runs_past_the_content_margin(config, tmp_path):
    """Header strings interpolate professor-supplied values, so their width is
    not knowable from the layout. `pdf_gen` shrinks them to fit; this proves
    the shrink actually happens on the page."""
    long_config = config.model_copy(
        update={
            "exam_id": "CS301_MIDSEM_MONSOON_2026_SECTION_A_SET_2_REVISED",
            "exam_name": "Mid-Semester Examination in Advanced Topics in Computer Systems and Networks",
        }
    )
    _manifest, pages = render(tmp_path, long_config)
    scale = px_per_mm(DPI)
    right_limit = round((PAGE_WIDTH_MM - MARGIN_MM) * scale) + STROKE_PAD_PX

    for page_no, image in pages.items():
        header = image[: round(42 * scale), :]
        cols = np.flatnonzero(np.any(header < WHITE_FLOOR, axis=0))
        # The corner markers and page bars legitimately sit outside the content
        # margin; text must not.
        text = image[round(24 * scale): round(42 * scale), :]
        text_cols = np.flatnonzero(np.any(text < WHITE_FLOOR, axis=0))
        assert cols.size and text_cols.size
        assert text_cols[-1] <= right_limit, (
            f"page {page_no}: header text reaches {text_cols[-1] / scale:.1f}mm, past the "
            f"{PAGE_WIDTH_MM - MARGIN_MM:g}mm content margin"
        )
        assert text_cols[0] >= round(MARGIN_MM * scale) - STROKE_PAD_PX


def test_no_layout_element_is_placed_inside_a_corner_keepout(tmp_path):
    manifest, _pages = render(tmp_path, a_config(num_mcq=60, written_questions=[]))
    boxes = corner_keepouts()

    def inside_any(x, y):
        return any(x0 <= x <= x1 and y0 <= y <= y1 for (x0, y0, x1, y1) in boxes)

    for _page, label, x_mm, y_mm in every_bubble_center_mm(manifest):
        assert not inside_any(x_mm, y_mm), f"{label} sits in a fiducial keep-out"
    for e in manifest["written_block"]:
        assert not inside_any(e["x_mm"], e["y_mm"])
        assert not inside_any(e["x_mm"] + e["width_mm"], e["y_mm"] + e["height_mm"])
