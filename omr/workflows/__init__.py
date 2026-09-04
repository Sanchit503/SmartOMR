"""End-to-end workflows that compose contracts, reader, grading, and I/O."""

__all__ = [
    "batch_evaluate",
    "course_id_from_manifest",
    "evaluate_scan",
    "parse_scan",
    "parse_scans",
    "initialize_verification_index",
    "verify_student",
    "export_written_grading_packet",
    "import_written_marks",
]


def __getattr__(name: str):
    if name in {"batch_evaluate", "course_id_from_manifest", "evaluate_scan"}:
        from . import evaluate

        return getattr(evaluate, name)
    if name in {"parse_scan", "parse_scans"}:
        from . import parse

        return getattr(parse, name)
    if name in {"initialize_verification_index", "verify_student"}:
        from . import review

        return getattr(review, name)
    if name in {"export_written_grading_packet", "import_written_marks"}:
        from . import written

        return getattr(written, name)
    raise AttributeError(f"module 'omr.workflows' has no attribute {name!r}")
