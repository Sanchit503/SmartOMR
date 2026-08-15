"""Builds a template manifest from a SheetLayout (CLAUDE.md Section 4.4).

The manifest is the parser's ONLY source of truth for bubble/box positions
(Section 2, principle 1) — nothing here should ever be re-derived from a
hardcoded constant on the parsing side. That's why bubble radius and MCQ
option spacing are included as top-level manifest fields even though the
example in CLAUDE.md doesn't spell them out: the parser needs them to turn
a single (x_mm, y_mm) per MCQ question into per-option bubble centers.

The schema itself (what fields exist, how to load and validate one) lives
in `omr.contracts.manifest`, which the reader also imports. This module
only knows how to *produce* one.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..contracts.manifest import MANIFEST_SCHEMA_VERSION, validate_manifest
from .layout import (
    BUBBLE_RADIUS_MM,
    MCQ_LABEL_OFFSET_MM,
    MCQ_OPTION_PITCH_MM,
    PAGE_HEIGHT_MM,
    PAGE_WIDTH_MM,
    SheetLayout,
)


def build_manifest(layout: SheetLayout) -> dict:
    """Every bubble/box entry carries its own "page" — the roll-number block
    is the one exception, since it only ever appears on page 1 (see
    layout.py's module docstring for why)."""
    rb = layout.roll_block
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "exam_id": layout.exam_id,
        "num_pages": layout.num_pages,
        "page": {"width_mm": PAGE_WIDTH_MM, "height_mm": PAGE_HEIGHT_MM},
        "bubble_radius_mm": BUBBLE_RADIUS_MM,
        "mcq_option_pitch_mm": MCQ_OPTION_PITCH_MM,
        "mcq_label_offset_mm": MCQ_LABEL_OFFSET_MM,
        "fiducials": [
            {"page": f.page, "corner": f.corner, "x_mm": f.x_mm, "y_mm": f.y_mm}
            for f in layout.fiducials
        ],
        "roll_number_block": {
            "page": rb.page,
            "program_selector": {
                label: {"x_mm": x, "y_mm": y} for label, (x, y) in rb.program_selector.items()
            },
            "btech_digits": {
                "columns": rb.btech_digits.columns,
                "x_mm": rb.btech_digits.x_mm,
                "y_mm": rb.btech_digits.y_mm,
                "col_pitch_mm": rb.btech_digits.col_pitch_mm,
                "row_pitch_mm": rb.btech_digits.row_pitch_mm,
            },
            "mtech_digits": {
                "columns": rb.mtech_digits.columns,
                "x_mm": rb.mtech_digits.x_mm,
                "y_mm": rb.mtech_digits.y_mm,
                "col_pitch_mm": rb.mtech_digits.col_pitch_mm,
                "row_pitch_mm": rb.mtech_digits.row_pitch_mm,
            },
        },
        "mcq_block": [
            {"page": e.page, "q_no": e.q_no, "x_mm": e.x_mm, "y_mm": e.y_mm, "options": e.options}
            for e in layout.mcq_entries
        ],
        "written_block": [
            {
                "page": e.page,
                "q_no": e.q_no,
                "x_mm": e.x_mm,
                "y_mm": e.y_mm,
                "width_mm": e.width_mm,
                "height_mm": e.height_mm,
                "max_marks": e.max_marks,
            }
            for e in layout.written_entries
        ],
    }
    # Fail here, at generation time, rather than months later at grading
    # time against a sheet a student has already written on.
    return validate_manifest(manifest)


def save_manifest(layout: SheetLayout, output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(build_manifest(layout), indent=2), encoding="utf-8")
    return output_path
