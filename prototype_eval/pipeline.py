"""Compatibility wrapper for the scan-evaluation workflow.

The implementation now lives under `omr.workflows.evaluate`; this module keeps
the original professor-demo import path working.
"""
from pathlib import Path

from omr.models import EvaluationResult
from omr.workflows.evaluate import (
    SCAN_EXTENSIONS,
    course_id_from_manifest,
    evaluate_scan,
)
from omr.workflows.evaluate import batch_evaluate as _batch_evaluate


def batch_evaluate(
    manifest_path: str | Path,
    students_path: str | Path,
    answer_key_path: str | Path,
    scans_path: str | Path,
    output_root: str | Path = "prototype_eval/data",
    dpi: float = 200,
    course_id: str | None = None,
) -> tuple[list[EvaluationResult], Path]:
    """Run the promoted workflow with the prototype's historical output root."""
    return _batch_evaluate(
        manifest_path=manifest_path,
        students_path=students_path,
        answer_key_path=answer_key_path,
        scans_path=scans_path,
        output_root=output_root,
        dpi=dpi,
        course_id=course_id,
    )


__all__ = [
    "SCAN_EXTENSIONS",
    "batch_evaluate",
    "course_id_from_manifest",
    "evaluate_scan",
]
