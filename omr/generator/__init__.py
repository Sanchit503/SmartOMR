"""Module 1 — OMR sheet generation (CLAUDE.md Section 4).

Everything needed to turn a professor's exam requirements into a printable
sheet lives in this folder: the input contract (`config`), the layout
engine (`layout`), the two renderers that consume it (`pdf_gen` for what
gets printed, `manifest` for what the parser reads), the entry point
(`main`), the desktop form (`gui`), worked example configs (`configs/`),
and its own tests (`tests/`).

Run it with:  python -m omr.generator.main
"""
from .config import ExamConfig, WrittenQuestionConfig
from .generate import generate_exam
from .layout import SheetLayout, build_layout
from .manifest import build_manifest, save_manifest
from .pdf_gen import render_pdf

__all__ = [
    "ExamConfig",
    "SheetLayout",
    "WrittenQuestionConfig",
    "build_layout",
    "build_manifest",
    "generate_exam",
    "render_pdf",
    "save_manifest",
]
