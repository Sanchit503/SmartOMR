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
MANIFEST_SCHEMA_VERSION = 1

_TOP_LEVEL_KEYS = (
    "exam_id",
    "num_pages",
    "page",
    "bubble_radius_mm",
    "mcq_option_pitch_mm",
    "mcq_label_offset_mm",
    "fiducials",
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
        for key in ("q_no", "x_mm", "y_mm", "width_mm", "height_mm", "max_marks"):
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
