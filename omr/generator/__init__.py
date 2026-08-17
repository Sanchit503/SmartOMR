"""Module 1 — OMR sheet generation (CLAUDE.md Section 4).

Everything needed to turn a professor's exam requirements into a printable
sheet lives in this folder:

    config.py     the input contract — what a professor supplies
    metrics.py    every millimetre of the sheet, and the geometry derived
                  from it. Edit this to change how the sheet looks.
    flow.py       which question lands on which page, and where
    layout.py     assembles a SheetLayout: flow + markers + identity fields
    pdf_gen.py    what gets printed
    manifest.py   what the parser reads (the same SheetLayout, published)
    preflight.py  rasterizes the result and proves it is machine-readable
    main.py       the entry point; gui.py the desktop form
    configs/      worked example configs; tests/ its own tests

Run it with:  python -m omr.generator.main
"""
from .config import ExamConfig, WrittenQuestionConfig
from .generate import generate_exam
from .layout import SheetLayout, build_layout
from .manifest import build_manifest, save_manifest
from .pdf_gen import render_pdf
from .preflight import check_sheet

__all__ = [
    "ExamConfig",
    "SheetLayout",
    "WrittenQuestionConfig",
    "build_layout",
    "build_manifest",
    "check_sheet",
    "generate_exam",
    "render_pdf",
    "save_manifest",
]
