"""The template manifest schema (CLAUDE.md Section 4.4).

The manifest is the generator's output *and* the reader's only input for
bubble/box positions. Both sides go through this module so neither can
quietly assume a field the other doesn't emit — that assumption is exactly
the drift Section 2, principle 1 exists to prevent.

`omr.generator.manifest` builds and saves manifests; this module owns what
a manifest *is*, plus loading and validating one.
"""
from __future__ import annotations

import json
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
MANIFEST_SCHEMA_VERSION = 2

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
    "write_in_fields",
    "roll_number_block",
    "mcq_block",
    "written_block",
)


class ManifestError(ValueError):
    """A manifest is missing required structure, or is a version this code
    can't safely read."""


def validate_manifest(manifest: dict) -> dict:
    """Check a manifest carries everything a reader needs, and return it.

    Deliberately strict: a half-valid manifest read against a real answer
    sheet produces wrong grades rather than an obvious failure, so this
    raises instead of filling in defaults (Section 2, principle 4).
    """
    missing = [k for k in _TOP_LEVEL_KEYS if k not in manifest]
    if missing:
        raise ManifestError(f"manifest is missing required keys: {', '.join(missing)}")

    version = manifest.get("schema_version", MANIFEST_SCHEMA_VERSION)
    if version != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(
            f"manifest schema_version is {version}, but this code reads version "
            f"{MANIFEST_SCHEMA_VERSION}. Regenerate the sheet, or use a matching "
            "version of SmartOMR to grade it."
        )

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
            f"zero and smaller than bubble_radius_mm ({manifest['bubble_radius_mm']}) — otherwise "
            "the printed outline is counted as a fill."
        )

    # Every physical page is deskewed independently, so every page needs its
    # own full set of registration marks (Section 4.3).
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
