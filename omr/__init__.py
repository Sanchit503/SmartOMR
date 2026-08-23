"""SmartOMR — OMR-based assessment pipeline (BTP, IIIT Delhi).

Package layout mirrors the module boundaries in PROJECT_SPEC.md:

    omr.contracts   shared truth: page geometry, mm<->px, manifest schema
    omr.generator   Module 1 — OMR sheet generation (Section 4)
    omr.reader      Modules 2/3 — scan canonicalization + identity reading
    omr.grading     Modules 4/5 — MCQ and written-answer grading (Sections 7-8)
    omr.io          roster, answer-key, and result CSV helpers
    omr.workflows   file-based end-to-end workflows that compose the modules

`omr.contracts` is the only package the others share. Nothing in it imports
from them, which is what lets the Phase 2 scan reader stay independent of
the PDF-generation stack (Section 2, principle 1).
"""

__version__ = "0.3.0"
