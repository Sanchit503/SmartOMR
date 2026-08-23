"""Prototype scan evaluator for the current SmartOMR sheet format.

This folder is intentionally separate from `omr.generator`: it is the demo
pipeline for scanned student sheets, professor answer keys, and roster data.
"""

from .pipeline import batch_evaluate, evaluate_scan

__all__ = ["batch_evaluate", "evaluate_scan"]
