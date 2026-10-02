"""Validated, per-source-page roll evidence shared by preview and matching."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from omr.ui.inspection import fingerprint, read_json, write_json_atomic


CACHE_VERSION = 1


def identity_context(inputs: dict, backend_state: dict, dpi: float) -> str:
    package = Path(__file__).resolve().parents[1]
    reader_paths = sorted((package / "reader").glob("*.py")) + [
        package / "grading" / "bubbles.py",
        package / "workflows" / "batch.py",
        package / "datasets" / "train_roll_digit_resnet.py",
    ]
    context = {
        "version": CACHE_VERSION,
        "scan": fingerprint(Path(inputs["scan_path"])),
        "manifest": fingerprint(Path(inputs["manifest_path"])),
        "roster": fingerprint(Path(inputs["students_path"])) if inputs.get("students_path") else None,
        "dpi": dpi,
        "backend": backend_state,
        "reader": {path.relative_to(package).as_posix(): fingerprint(path) for path in reader_paths},
    }
    return hashlib.sha256(json.dumps(context, sort_keys=True).encode("utf-8")).hexdigest()


def _map_paths(value: Any, run_dir: Path, *, relative: bool, paths: bool = False) -> Any:
    if isinstance(value, dict):
        return {
            key: _map_paths(item, run_dir, relative=relative, paths=paths or key in {"crop_paths", "cell_crop_paths"})
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_map_paths(item, run_dir, relative=relative, paths=paths) for item in value]
    if paths and isinstance(value, str) and value:
        path = Path(value).resolve() if relative else (run_dir / value).resolve()
        if not path.is_relative_to(run_dir.resolve()):
            raise ValueError("Roll evidence asset is outside its run")
        if not relative and not path.is_file():
            raise ValueError("Roll evidence asset is missing")
        return path.relative_to(run_dir.resolve()).as_posix() if relative else str(path)
    return value


def relative_evidence(payload: dict | None, run_dir: Path) -> dict | None:
    return _map_paths(payload, run_dir, relative=True)


def absolute_evidence(payload: dict | None, run_dir: Path) -> dict | None:
    return _map_paths(payload, run_dir, relative=False)


def load_page_evidence(run_dir: Path, source_index: int, page_index: int,
                       image_hash: str, context: str) -> dict | None:
    path = run_dir / "identity_preview" / f"source_{source_index:04d}" / "evidence.json"
    try:
        cached = read_json(path)
        if not isinstance(cached, dict) or cached.get("context") != context or cached.get("aligned_sha256") != image_hash:
            return None
        row = cached["page"]
        if not isinstance(row, dict) or not isinstance(row.get("identity"), (dict, type(None))):
            return None
        if row["source_index"] != source_index or row["sheet_page"] != page_index:
            return None
        if row["identity_kind"] not in {"bubbled", "write_in", "handwritten"}:
            return None
        if row["confidence"] not in {"high", "medium", "low"} or not isinstance(row["review_flags"], list):
            return None
        # Validate all saved crops before a cache hit is exposed to the viewer.
        absolute_evidence(row["identity"], run_dir)
        return row
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_page_evidence(run_dir: Path, row: dict, image_hash: str, context: str) -> None:
    path = run_dir / "identity_preview" / f"source_{row['source_index']:04d}" / "evidence.json"
    write_json_atomic(path, {"context": context, "aligned_sha256": image_hash, "page": row})
