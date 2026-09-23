"""The template manifest schema (PROJECT_SPEC.md Section 4.4).

The manifest is the generator's output *and* the reader's only input for
bubble/box positions. Both sides go through this module so neither can
quietly assume a field the other doesn't emit — that assumption is exactly
the drift Section 2, principle 1 exists to prevent.

`omr.generator.manifest` builds and saves manifests; this module owns what
a manifest *is*, plus loading and validating one.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

# Bump whenever the manifest layout changes in a way a previously written
# manifest would no longer satisfy. A reader that loads a manifest with an
# unexpected version should refuse it rather than silently misread a sheet.
#
# v2 added: bubble_sample_radius_mm, fiducial_size_mm, orientation_marker,
#           write_in_fields, exam metadata, written_block[].lines. Sheets
#           generated under v1 have no orientation marker printed on them,
#           so they genuinely cannot be read by a v2 reader — hence a bump
#           rather than an optional field.
# v3 added: page_marks (pre-printed page-index bars) and
#           write_in_fields[].cell_width_mm. Again a print change, not just a
#           metadata one: a v2 sheet has no bars on it, so a v3 reader looking
#           for them would find blank paper and conclude the page index is
#           unreadable.
# v4 added: continuation_program_choices, so continuation pages carry compact
#           BTECH/MTECH selectors beside their write-in roll-number boxes
#           without repeating the full digit grid.
# v5 adds numerical digit grids and a PHD selector sharing the five-digit
# postgraduate grid. Continue accepting v4 sheets; old readers reject v5
# rather than silently dropping those printed features.
# v6 adds vertical numerical grids. Keep v4/v5 readable with their original
# geometry; an older reader must reject a new sheet rather than swap axes.
MANIFEST_SCHEMA_VERSION = 6

_TOP_LEVEL_KEYS = (
    "exam_id",
    "exam",
    "num_pages",
    "page",
    "bubble_radius_mm",
    "bubble_sample_radius_mm",
    "mcq_option_pitch_mm",
    "mcq_label_offset_mm",
    "fiducial_size_mm",
    "fiducials",
    "orientation_marker",
    "page_marks",
    "continuation_program_choices",
    "write_in_fields",
    "roll_number_block",
    "mcq_block",
    "written_block",
)


class ManifestError(ValueError):
    """A manifest is missing required structure, or is a version this code
    can't safely read."""


def _validate_page_marks(manifest: dict, page: int, num_pages: int) -> None:
    """The page-index bars must say what page they are on, unambiguously.

    A scan batch arrives as a pile of images. Grouping them back into
    per-student sheets means knowing which page each image is, and the bars
    are the only pre-printed thing that says so. Exactly one filled bar, at
    the index matching the page, is what makes that read self-checking rather
    than a guess (Section 2, principle 4).
    """
    marks = [m for m in manifest["page_marks"] if m.get("page", 1) == page]
    if len(marks) != num_pages:
        raise ManifestError(
            f"page {page} has {len(marks)} page-index bars but the sheet has {num_pages} page(s); "
            "every page prints one bar per page so the reader can count them"
        )
    filled = [m for m in marks if m.get("filled")]
    if len(filled) != 1:
        raise ManifestError(
            f"page {page} has {len(filled)} filled page-index bars, expected exactly 1 — "
            "the reader uses 'exactly one is dark' as its checksum"
        )
    if filled[0].get("index") != page:
        raise ManifestError(
            f"page {page}'s filled page-index bar claims index {filled[0].get('index')}; "
            "a page that misreports its own number puts answers on the wrong question"
        )


def _program_grid_keys(manifest: dict) -> dict[str, str]:
    block = manifest["roll_number_block"]
    return block.get("program_grid_keys", {"BTECH": "btech_digits", "MTECH": "mtech_digits"})


def _validate_roll_number_block(manifest: dict, version: int) -> None:
    block = manifest["roll_number_block"]
    selectors = block.get("program_selector")
    if not isinstance(selectors, dict):
        raise ManifestError("roll_number_block.program_selector must be an object")

    expected_programs = {"BTECH", "MTECH", "PHD"} if version >= 5 else {"BTECH", "MTECH"}
    if set(selectors) != expected_programs:
        raise ManifestError(
            f"roll-number program selectors are {sorted(selectors)}, expected {sorted(expected_programs)}"
        )
    for program, selector in selectors.items():
        if not isinstance(selector, dict) or any(key not in selector for key in ("x_mm", "y_mm")):
            raise ManifestError(f"{program} program selector is missing geometry")
        if any(type(selector[key]) not in (int, float) or not math.isfinite(selector[key]) for key in ("x_mm", "y_mm")):
            raise ManifestError(f"{program} program selector has invalid geometry")

    mapping = _program_grid_keys(manifest)
    if set(mapping) != expected_programs:
        raise ManifestError(
            f"roll-number program grid mapping is for {sorted(mapping)}, expected {sorted(expected_programs)}"
        )
    for program, grid_key in mapping.items():
        grid = block.get(grid_key)
        if not isinstance(grid, dict):
            raise ManifestError(f"{program} maps to missing roll-number grid {grid_key!r}")
        required = ("columns", "x_mm", "y_mm", "col_pitch_mm", "row_pitch_mm")
        if any(key not in grid for key in required):
            raise ManifestError(f"roll-number grid {grid_key!r} is missing geometry")
        if type(grid["columns"]) is not int or grid["columns"] <= 0:
            raise ManifestError(f"roll-number grid {grid_key!r} has an invalid column count")
        for key in ("x_mm", "y_mm", "col_pitch_mm", "row_pitch_mm"):
            value = grid[key]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ManifestError(f"roll-number grid {grid_key!r} has invalid {key}")

    if version >= 5 and mapping["MTECH"] != mapping["PHD"]:
        raise ManifestError("v5 MTECH and PHD selectors must share the compact five-digit grid")
    if block[mapping["BTECH"]]["columns"] != 7:
        raise ManifestError("BTECH roll-number grid must contain 7 digit positions")
    if block[mapping["MTECH"]]["columns"] != 5:
        raise ManifestError("MTECH/PHD roll-number grid must contain 5 digit positions")


def _validate_page_identity(manifest: dict, page: int, version: int) -> None:
    """Every page must carry something that ties it to a student.

    Page 1 has the bubbled roll-number grid; continuation pages have the
    handwritten roll-number strip. Either way a page that gets separated from
    the rest of its sheet must not be anonymous, because the alternative is
    attributing an answer by scan order — a guess.
    """
    has_write_in = any(
        w.get("page", 1) == page and w.get("name") == "roll_number"
        for w in manifest["write_in_fields"]
    )
    has_grid = manifest["roll_number_block"].get("page", 1) == page
    if not (has_write_in or has_grid):
        raise ManifestError(
            f"page {page} carries no roll-number field at all — a page separated from its sheet "
            "would be unattributable to any student"
        )
    expected_programs = {"BTECH", "MTECH", "PHD"} if version >= 5 else {"BTECH", "MTECH"}
    write_in_programs = set()
    for field in manifest["write_in_fields"]:
        if field.get("page", 1) != page or field.get("name") != "roll_number":
            continue
        if version >= 5:
            write_in_programs.update(str(program) for program in field.get("programs", []))
        if field.get("program"):
            write_in_programs.add(field["program"])
    if write_in_programs != expected_programs:
        raise ManifestError(
            f"page {page} has roll-number write-in fields for {sorted(write_in_programs)}, expected "
            f"{sorted(expected_programs)}"
        )
    if page > 1:
        programs = {
            c.get("program")
            for c in manifest["continuation_program_choices"]
            if c.get("page", 1) == page
        }
        if programs != expected_programs:
            raise ManifestError(
                f"page {page} has continuation program choices {sorted(programs)}, expected "
                f"{', '.join(sorted(expected_programs))} so a separated page can be matched to the right "
                "roll-number format"
            )


def _validate_page_has_content(manifest: dict, page: int) -> None:
    """No blank pages. A page with no question on it is a layout bug, and in a
    scan batch it is indistinguishable from a page the feeder pulled twice."""
    if any(e.get("page", 1) == page for e in manifest["mcq_block"]):
        return
    if any(e.get("page", 1) == page for e in manifest["written_block"]):
        return
    if any(e.get("page", 1) == page for e in manifest.get("numerical_block", [])):
        return
    raise ManifestError(
        f"page {page} has no questions on it. A blank page in a scan batch cannot be told apart "
        "from a misfeed, so the layout must not produce one."
    )


def _validate_numerical_block(manifest: dict) -> None:
    entries = manifest.get("numerical_block", [])
    if not isinstance(entries, list):
        raise ManifestError("numerical_block must be a list")
    seen = {e.get("q_no") for e in manifest["mcq_block"] + manifest["written_block"]}
    for entry in entries:
        required = ("q_no", "page", "positions", "x_mm", "y_mm", "digit_pitch_mm",
                    "position_pitch_mm", "max_marks", "orientation", "answer_type", "leading_zeros")
        if not isinstance(entry, dict) or any(key not in entry for key in required):
            raise ManifestError("numerical_block entry is missing required grid/answer fields")
        q_no = entry["q_no"]
        if type(q_no) is not int or q_no <= 0 or q_no in seen:
            raise ManifestError("numerical question numbers must be positive and unique across all sections")
        seen.add(q_no)
        if type(entry["page"]) is not int or not 1 <= entry["page"] <= manifest["num_pages"]:
            raise ManifestError(f"numerical Q{q_no} has an invalid page")
        if type(entry["positions"]) is not int or not 1 <= entry["positions"] <= 8:
            raise ManifestError(f"numerical Q{q_no} must have 1 to 8 place-value positions")
        orientations = ("horizontal", "vertical") if manifest.get("schema_version", 4) >= 6 else ("horizontal",)
        if (entry["orientation"] not in orientations or entry["answer_type"] != "unsigned_integer"
                or entry["leading_zeros"] != "required"):
            raise ManifestError(f"numerical Q{q_no} uses an unsupported answer format")
        for key in ("x_mm", "y_mm", "digit_pitch_mm", "position_pitch_mm", "max_marks"):
            value = entry[key]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ManifestError(f"numerical Q{q_no} has invalid {key}")
        radius = manifest["bubble_radius_mm"]
        if min(entry["digit_pitch_mm"], entry["position_pitch_mm"]) <= 2 * radius:
            raise ManifestError(f"numerical Q{q_no} has overlapping bubbles")
        digit_span = 9 * entry["digit_pitch_mm"]
        position_span = (entry["positions"] - 1) * entry["position_pitch_mm"]
        horizontal = entry["orientation"] == "horizontal"
        right = entry["x_mm"] + (digit_span if horizontal else position_span) + radius
        bottom = entry["y_mm"] + (position_span if horizontal else digit_span) + radius
        if (min(entry["x_mm"], entry["y_mm"]) < radius
                or right > manifest["page"]["width_mm"] or bottom > manifest["page"]["height_mm"]):
            raise ManifestError(f"numerical Q{q_no} extends beyond the page")


def validate_manifest(manifest: dict) -> dict:
    """Check a manifest carries everything a reader needs, and return it.

    Deliberately strict: a half-valid manifest read against a real answer
    sheet produces wrong grades rather than an obvious failure, so this
    raises instead of filling in defaults (Section 2, principle 4).
    """
    missing = [k for k in _TOP_LEVEL_KEYS if k not in manifest]
    if missing:
        raise ManifestError(f"manifest is missing required keys: {', '.join(missing)}")

    version = manifest.get("schema_version", 4)
    if version not in (4, 5, MANIFEST_SCHEMA_VERSION):
        raise ManifestError(
            f"manifest schema_version is {version}, but this code reads version "
            f"4, 5 or {MANIFEST_SCHEMA_VERSION}. Regenerate the sheet, or use a matching "
            "version of SmartOMR to grade it."
        )
    if version >= 5 and "numerical_block" not in manifest:
        raise ManifestError(f"v{version} manifest is missing numerical_block")
    if version == 4 and manifest.get("numerical_block"):
        raise ManifestError("numerical grids require manifest schema_version 5 or later")

    _validate_roll_number_block(manifest, version)

    for axis in ("width_mm", "height_mm"):
        if axis not in manifest["page"]:
            raise ManifestError(f"manifest['page'] is missing '{axis}'")

    num_pages = manifest["num_pages"]
    if not isinstance(num_pages, int) or num_pages < 1:
        raise ManifestError(f"manifest['num_pages'] must be a positive integer, got {num_pages!r}")

    # The reader measures a smaller disc than was printed so the bubble's own
    # outline isn't counted as student ink. If that ever inverts, every empty
    # bubble starts reading as partly filled.
    if not 0 < manifest["bubble_sample_radius_mm"] < manifest["bubble_radius_mm"]:
        raise ManifestError(
            f"bubble_sample_radius_mm ({manifest['bubble_sample_radius_mm']}) must be greater than "
            f"zero and smaller than bubble_radius_mm ({manifest['bubble_radius_mm']}), otherwise "
            "the printed outline is counted as a fill."
        )

    _validate_numerical_block(manifest)

    # Every physical page is deskewed independently, so every page needs its
    # own full set of registration marks (Section 4.3), its own way to say
    # which page it is, and its own way to say which student it belongs to.
    for page in range(1, num_pages + 1):
        corners = {f["corner"] for f in manifest["fiducials"] if f.get("page", 1) == page}
        if corners != {"TL", "TR", "BL", "BR"}:
            raise ManifestError(
                f"page {page} has fiducials {sorted(corners)}; every page needs all four corners"
            )
        if sum(1 for m in manifest["orientation_marker"] if m.get("page", 1) == page) != 1:
            raise ManifestError(
                f"page {page} needs exactly one orientation marker — without it a sheet fed in "
                "rotated reads as a valid upright sheet with inverted coordinates"
            )
        _validate_page_marks(manifest, page, num_pages)
        _validate_page_identity(manifest, page, version)
        _validate_page_has_content(manifest, page)

    for entry in manifest["mcq_block"]:
        for key in ("q_no", "x_mm", "y_mm", "options"):
            if key not in entry:
                raise ManifestError(f"mcq_block entry is missing '{key}': {entry!r}")
        if entry.get("page", 1) > num_pages:
            raise ManifestError(
                f"mcq_block Q{entry['q_no']} is on page {entry['page']}, "
                f"but the manifest declares only {num_pages} page(s)"
            )

    for entry in manifest["written_block"]:
        # "lines" is required because Section 8's grading prompt interpolates
        # it ("max {lines} lines") — a manifest without it can't be graded.
        for key in ("q_no", "x_mm", "y_mm", "width_mm", "height_mm", "max_marks", "lines"):
            if key not in entry:
                raise ManifestError(f"written_block entry is missing '{key}': {entry!r}")
        if entry.get("page", 1) > num_pages:
            raise ManifestError(
                f"written_block Q{entry['q_no']} is on page {entry['page']}, "
                f"but the manifest declares only {num_pages} page(s)"
            )

    return manifest


def load_manifest(path: str | Path) -> dict:
    """Load and validate a manifest written by the generator."""
    return validate_manifest(json.loads(Path(path).read_text(encoding="utf-8")))
