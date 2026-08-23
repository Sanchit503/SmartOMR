"""Backward-compatible demo entry point for scan evaluation.

The implementation has been promoted into `omr.reader`, `omr.io`, and
`omr.workflows`; this package keeps the original `python -m prototype_eval`
workflow available for professor demos.
"""

from .pipeline import batch_evaluate, evaluate_scan

__all__ = ["batch_evaluate", "evaluate_scan"]
