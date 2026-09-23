"""Builds a template manifest from a SheetLayout (PROJECT_SPEC.md Section 4.4).

The manifest is the parser's ONLY source of truth for bubble/box positions
(Section 2, principle 1) — nothing here should ever be re-derived from a
hardcoded constant on the parsing side. Which is why this file emits rather
more than the example in PROJECT_SPEC.md spells out:

  bubble_radius_mm         what was printed
  bubble_sample_radius_mm  what the reader should MEASURE — smaller, so the
                           bubble's own outline never counts as student ink
  mcq_option_pitch_mm      turns one (x,y) per question into per-option centers
  mcq_label_offset_mm      "
  fiducial_size_mm         Module 2 needs the expected marker size to filter
                           candidate contours
  orientation_marker       which corner is really the top-left, so a sheet fed
                           in rotated can't be read upside down
  page_marks               the pre-printed page-index bars: which page a sheet
                           in a scan batch actually is, read with the same
                           ink-measuring primitive as a bubble
  continuation_program_choices
                           compact BTECH/MTECH/PHD selectors on continuation
                           pages, paired with write-in boxes instead of the
                           full digit grid
  write_in_fields          handwritten roll-number regions to crop for the
                           review queue when a bubble read is ambiguous, and to
                           reattach a continuation page to its page 1
  written_block[].lines    the LLM grading prompt interpolates it (Section 8)
  exam                     course/name/type/marks/total, so a grader has the
                           exam's own metadata without a second file

The schema itself (what fields exist, how to load and validate one) lives
in `omr.contracts.manifest`, which the reader also imports. This module
only knows how to *produce* one.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ..contracts.manifest import MANIFEST_SCHEMA_VERSION, validate_manifest
from .layout import SheetLayout
from .metrics import (
    BUBBLE_RADIUS_MM,
    BUBBLE_SAMPLE_RADIUS_MM,
    MCQ_LABEL_OFFSET_MM,
    MCQ_OPTION_PITCH_MM,
    ORIENTATION_KEEPOUT_MM,
    PAGE_HEIGHT_MM,
    PAGE_WIDTH_MM,
    PRINTER_SAFE_MARGIN_MM,
    corner_keepouts,
    orientation_keepout,
)


def build_manifest(layout: SheetLayout) -> dict:
    """Every bubble/box entry carries its own "page" — the roll-number
    bubble grid is the one exception, since it only ever appears on page 1
    (see layout.py's module docstring for why)."""
    rb = layout.roll_block
    ox0, oy0, ox1, oy1 = orientation_keepout()
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "exam_id": layout.exam_id,
        "exam": {
            "university_name": layout.university_name,
            "course_code": layout.course_code,
            "exam_name": layout.exam_name,
            "exam_type": layout.exam_type,
            "marks_per_mcq": layout.marks_per_mcq,
            "mcq_options": layout.mcq_options,
            "mcq_columns": layout.mcq_columns,
            "total_marks": layout.total_marks,
        },
        "num_pages": layout.num_pages,
        "page": {"width_mm": PAGE_WIDTH_MM, "height_mm": PAGE_HEIGHT_MM},
        # The region an ordinary A4 printer reproduces at 100% scale. Nothing
        # important is placed outside it, so no borderless printing is needed.
        "printer_safe_margin_mm": PRINTER_SAFE_MARGIN_MM,
        "bubble_radius_mm": BUBBLE_RADIUS_MM,
        "bubble_sample_radius_mm": BUBBLE_SAMPLE_RADIUS_MM,
        "mcq_option_pitch_mm": MCQ_OPTION_PITCH_MM,
        "mcq_label_offset_mm": MCQ_LABEL_OFFSET_MM,
        "fiducial_size_mm": layout.fiducials[0].size_mm if layout.fiducials else None,
        "fiducial_keepouts_mm": [
            {"x0_mm": x0, "y0_mm": y0, "x1_mm": x1, "y1_mm": y1}
            for (x0, y0, x1, y1) in corner_keepouts()
        ],
        # The orientation marker is detected the same way the corner markers
        # are, so it needs — and gets — its own clean surround.
        "orientation_keepout_mm": {
            "half_width_mm": ORIENTATION_KEEPOUT_MM,
            "x0_mm": ox0,
            "y0_mm": oy0,
            "x1_mm": ox1,
            "y1_mm": oy1,
        },
        "fiducials": [
            {"page": f.page, "corner": f.corner, "x_mm": f.x_mm, "y_mm": f.y_mm, "size_mm": f.size_mm}
            for f in layout.fiducials
        ],
        "orientation_marker": [
            {
                "page": om.page,
                "x_mm": om.x_mm,
                "y_mm": om.y_mm,
                "size_mm": om.size_mm,
                "marks_corner": om.marks_corner,
            }
            for om in layout.orientation_markers
        ],
        # Exactly one bar per page reads dark, and its index is that page's own
        # number: enough for the reader to confirm a scan batch is a complete
        # sheet in the right order instead of trusting the feeder.
        "page_marks": [
            {
                "page": m.page,
                "index": m.index,
                "x_mm": m.x_mm,
                "y_mm": m.y_mm,
                "width_mm": m.width_mm,
                "height_mm": m.height_mm,
                "filled": m.filled,
            }
            for m in layout.page_marks
        ],
        "continuation_program_choices": [
            {
                "page": c.page,
                "program": c.program,
                "x_mm": c.x_mm,
                "y_mm": c.y_mm,
                "radius_mm": c.radius_mm,
            }
            for c in layout.continuation_program_choices
        ],
        "write_in_fields": [
            {
                "page": w.page,
                "name": w.name,
                "program": w.program,
                "programs": list(w.programs or ((w.program,) if w.program else ())),
                "x_mm": w.x_mm,
                "y_mm": w.y_mm,
                "width_mm": w.width_mm,
                "height_mm": w.height_mm,
                "cells": w.cells,
                "cell_pitch_mm": w.cell_pitch_mm,
                "cell_width_mm": w.cell_width_mm,
            }
            for w in layout.write_in_fields
        ],
        "roll_number_block": {
            "page": rb.page,
            "program_selector": {
                label: {"x_mm": x, "y_mm": y} for label, (x, y) in rb.program_selector.items()
            },
            "program_grid_keys": {
                "BTECH": "btech_digits",
                "MTECH": "mtech_digits",
                "PHD": "mtech_digits",
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
                "lines": e.lines,
            }
            for e in layout.written_entries
        ],
        "numerical_block": [
            {**asdict(e), "orientation": "vertical", "answer_type": "unsigned_integer",
             "leading_zeros": "required"}
            for e in layout.numerical_entries
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
