"""Guards on the shared contract itself.

The dependency-direction test is the important one here: it's what stops
Phase 2's scan reader from quietly growing a dependency on the PDF
generator, which is how "one shared manifest" (Section 2, principle 1)
turns into two drifting copies.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from omr.contracts.geometry import (
    A4_HEIGHT_MM,
    A4_WIDTH_MM,
    canonical_size_px,
    mm_to_px,
    px_per_mm,
)
from omr.contracts.manifest import (
    MANIFEST_SCHEMA_VERSION,
    ManifestError,
    load_manifest,
    validate_manifest,
)
from omr.generator.config import ExamConfig, WrittenQuestionConfig
from omr.generator.layout import build_layout
from omr.generator.manifest import build_manifest

CONTRACTS_DIR = Path(__file__).resolve().parent.parent


def a_manifest(**overrides) -> dict:
    config = ExamConfig(
        exam_id="CS301_MIDSEM_2026A",
        course_code="CS301",
        exam_name="Mid-Semester Examination",
        exam_type="midsem",
        num_mcq=10,
        written_questions=[WrittenQuestionConfig(q_no=11, max_marks=5, lines=2)],
    )
    manifest = build_manifest(build_layout(config))
    manifest.update(overrides)
    return manifest


# ---- geometry ----


def test_mm_to_px_round_trips_at_known_dpi():
    # 25.4mm is exactly one inch, so at 200 DPI it must be exactly 200px.
    assert mm_to_px(25.4, 25.4, 200) == (200, 200)
    assert px_per_mm(254) == 10.0


def test_canonical_size_matches_a4_at_200dpi():
    manifest = a_manifest()
    assert manifest["page"] == {"width_mm": A4_WIDTH_MM, "height_mm": A4_HEIGHT_MM}
    # A4 at 200 DPI: 210mm -> 1654px, 297mm -> 2339px
    assert canonical_size_px(manifest, 200) == (1654, 2339)


# ---- manifest schema ----


def test_generated_manifest_is_valid_and_versioned():
    manifest = a_manifest()
    assert manifest["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert validate_manifest(manifest) is manifest


def test_manifest_missing_a_required_key_is_rejected():
    manifest = a_manifest()
    del manifest["bubble_radius_mm"]
    with pytest.raises(ManifestError, match="bubble_radius_mm"):
        validate_manifest(manifest)


def test_manifest_from_a_future_schema_version_is_refused_not_guessed():
    with pytest.raises(ManifestError, match="schema_version"):
        validate_manifest(a_manifest(schema_version=MANIFEST_SCHEMA_VERSION + 1))


def test_entry_pointing_past_the_last_page_is_rejected():
    manifest = a_manifest()
    manifest["mcq_block"][0]["page"] = manifest["num_pages"] + 1
    with pytest.raises(ManifestError, match="page"):
        validate_manifest(manifest)


def test_load_manifest_validates_on_the_way_in(tmp_path):
    path = tmp_path / "broken.manifest.json"
    manifest = a_manifest()
    del manifest["mcq_block"]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(path)


# ---- dependency direction ----


def test_contracts_never_imports_from_generator_or_grading():
    """`omr.contracts` is imported BY the generator and the reader, so it
    must not import either back. If this fails, the Phase 2 scan parser can
    no longer be used without the PDF-generation stack installed."""
    offenders = []
    for module in sorted(CONTRACTS_DIR.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                target = ("." * node.level) + (node.module or "")
                if "generator" in target or "grading" in target:
                    offenders.append(f"{module.name}: from {target} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if "generator" in alias.name or "grading" in alias.name:
                        offenders.append(f"{module.name}: import {alias.name}")
    assert not offenders, "omr.contracts must not depend on its consumers: " + "; ".join(offenders)
