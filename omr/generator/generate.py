"""Top-level entry point: ExamConfig -> printable PDF + template manifest.

Always produces both together (Section 4.1) — never just the PDF.
"""
from __future__ import annotations

from pathlib import Path

from .config import ExamConfig
from .layout import build_layout
from .manifest import build_manifest, save_manifest
from .pdf_gen import render_pdf


def generate_exam(config: ExamConfig, output_dir: str | Path) -> dict:
    output_dir = Path(output_dir)
    layout = build_layout(config)
    pdf_path = render_pdf(layout, output_dir / f"{config.exam_id}.pdf")
    manifest_path = save_manifest(layout, output_dir / f"{config.exam_id}.manifest.json")
    return {
        "pdf_path": pdf_path,
        "manifest_path": manifest_path,
        "manifest": build_manifest(layout),
    }
