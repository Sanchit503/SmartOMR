"""Shared contracts between the sheet generator and the sheet reader.

This package holds the *only* things two modules are allowed to agree on
directly: the page geometry, the mm->px conversion, and the manifest
schema. Everything else — bubble sizes, block positions, option pitch — is
a layout decision the generator makes and *publishes through the manifest*,
so the reader learns it at parse time instead of sharing a constant
(PROJECT_SPEC.md Section 2, principle 1).

The dependency direction matters: `omr.generator` and `omr.grading` both
import from here, and nothing here imports from either. That's what keeps
the Phase 2 scan parser from having to depend on ReportLab.
"""
from .geometry import (
    A4_HEIGHT_MM,
    A4_WIDTH_MM,
    MM_PER_INCH,
    canonical_size_px,
    mm_to_px,
    px_per_mm,
)
from .manifest import MANIFEST_SCHEMA_VERSION, load_manifest, validate_manifest

__all__ = [
    "A4_HEIGHT_MM",
    "A4_WIDTH_MM",
    "MM_PER_INCH",
    "MANIFEST_SCHEMA_VERSION",
    "canonical_size_px",
    "load_manifest",
    "mm_to_px",
    "px_per_mm",
    "validate_manifest",
]
