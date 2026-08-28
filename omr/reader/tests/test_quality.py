from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image

from omr.generator.config import ExamConfig, WrittenQuestionConfig
from omr.generator.generate import generate_exam
from omr.reader.quality import (
    assess_alignment_quality,
    save_alignment_overlay,
    save_alignment_report,
)


DPI = 200


def _sheet(tmp_path: Path):
    config = ExamConfig(
        exam_id="QUALITY_TEST",
        course_code="CSE202",
        exam_name="Quiz - 1",
        exam_type="quiz",
        num_mcq=10,
        mcq_options=4,
        marks_per_mcq=1,
        written_questions=[WrittenQuestionConfig(q_no=11 + i, max_marks=1, lines=2) for i in range(4)],
    )
    result = generate_exam(config, tmp_path / "exam")
    with pymupdf.open(result["pdf_path"]) as doc:
        pix = doc[0].get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
        page = np.asarray(Image.frombytes("L", (pix.width, pix.height), pix.samples))
    return result["manifest"], page


def test_clean_generated_page_passes_alignment_quality_gate(tmp_path: Path):
    manifest, page = _sheet(tmp_path)

    report = assess_alignment_quality(page, manifest, page_index=1, dpi=DPI)

    assert report.ok, report.to_dict()
    assert report.status == "ready"
    assert report.score > 0.80
    assert report.metrics["bubble_anchors"]["matched_fraction"] > 0.90
    assert report.metrics["page_marks"]["detected_index"] == 1


def test_shifted_canonical_page_is_flagged_for_review(tmp_path: Path):
    manifest, page = _sheet(tmp_path)
    shifted = np.full_like(page, 255)
    shifted[18:, 20:] = page[:-18, :-20]

    report = assess_alignment_quality(shifted, manifest, page_index=1, dpi=DPI)

    assert not report.ok
    assert report.status == "needs_review"
    assert any("residual" in flag or "locally shifted" in flag for flag in report.review_flags)


def test_alignment_debug_artifacts_are_written(tmp_path: Path):
    manifest, page = _sheet(tmp_path)
    report = assess_alignment_quality(page, manifest, page_index=1, dpi=DPI)
    report = report.with_paths("debug/page_1_alignment.json", "debug/page_1_alignment_overlay.png")

    overlay = save_alignment_overlay(page, manifest, page_index=1, dpi=DPI, path=tmp_path / "overlay.png")
    saved_report = save_alignment_report(report, tmp_path / "alignment.json")

    assert overlay.exists()
    assert saved_report.exists()
    payload = json.loads(saved_report.read_text(encoding="utf-8"))
    assert payload["status"] == "ready"
    assert payload["overlay_path"] == "debug/page_1_alignment_overlay.png"
