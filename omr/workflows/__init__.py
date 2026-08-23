"""End-to-end workflows that compose contracts, reader, grading, and I/O."""

__all__ = [
    "batch_evaluate",
    "course_id_from_manifest",
    "evaluate_scan",
    "parse_scan",
    "parse_scans",
]


def __getattr__(name: str):
    if name in {"batch_evaluate", "course_id_from_manifest", "evaluate_scan"}:
        from . import evaluate

        return getattr(evaluate, name)
    if name in {"parse_scan", "parse_scans"}:
        from . import parse

        return getattr(parse, name)
    raise AttributeError(f"module 'omr.workflows' has no attribute {name!r}")
