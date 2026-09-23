"""Source-page inventory and alignment inspection, independent of identity/grading."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from typing import Callable
import uuid

import numpy as np
from PIL import Image
import pymupdf

from omr.contracts import load_manifest
from omr.reader.quality import assess_alignment_quality, save_alignment_overlay
from omr.reader.scan import IMAGE_EXTENSIONS, align_scan_page


INSPECTION_DPI = 200.0
INDEX_NAME = "inspection/index.json"
TERMINAL_STATES = {"aligned", "needs_review", "failed"}
MAX_RENDER_PIXELS = 40_000_000


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        # Windows readers/antivirus can briefly hold the destination during a poll.
        for attempt in range(5):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(.01 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def fingerprint(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read_json(path: Path) -> dict:
    # Atomic replacement can briefly deny a new reader on Windows too.
    for attempt in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(.01 * (attempt + 1))
    raise AssertionError("unreachable")


def source_page_count(path: Path) -> int:
    if path.suffix.lower() == ".pdf":
        with pymupdf.open(path) as doc:
            if doc.needs_pass:
                raise ValueError("The scan PDF is password-protected. Upload an unlocked copy.")
            count = len(doc)
    elif path.suffix.lower() in IMAGE_EXTENSIONS:
        with Image.open(path) as image:
            count = getattr(image, "n_frames", 1)
            if count != 1:
                raise ValueError("Multi-frame images are not supported by the batch reader. Upload a PDF instead.")
            image.verify()
    else:
        raise ValueError("Upload a PDF or a single-page PNG, JPEG, TIFF or BMP image.")
    if count < 1:
        raise ValueError("The scan contains no pages.")
    return count


def create_inventory(run_dir: Path, scan_path: Path, manifest_path: Path) -> dict:
    manifest = load_manifest(manifest_path)
    total = source_page_count(scan_path)
    index = {
        "version": 1,
        "exam_id": manifest["exam_id"],
        "source_name": scan_path.name,
        "source_sha256": fingerprint(scan_path),
        "manifest_sha256": fingerprint(manifest_path),
        "template_pages": int(manifest["num_pages"]),
        "dpi": INSPECTION_DPI,
        "status": "pending",
        "error": None,
        "updated_at": now(),
        "pages": [
            {"source_index": number, "status": "pending", "page_index": None,
             "original": None, "aligned": None, "overlay": None, "report": None, "error": None}
            for number in range(1, total + 1)
        ],
    }
    save_inventory(run_dir, index)
    return index


def load_inventory(run_dir: Path) -> dict:
    return read_json(run_dir / INDEX_NAME)


def save_inventory(run_dir: Path, index: dict) -> None:
    counts = Counter(page["status"] for page in index["pages"])
    index["counts"] = {key: counts[key] for key in ("pending", "processing", "aligned", "needs_review", "failed")}
    index["total"] = len(index["pages"])
    index["processed"] = sum(counts[key] for key in TERMINAL_STATES)
    index["updated_at"] = now()
    write_json_atomic(run_dir / INDEX_NAME, index)


def _render_page(path: Path, source_index: int, dpi: float) -> np.ndarray:
    if path.suffix.lower() == ".pdf":
        with pymupdf.open(path) as doc:
            page = doc[source_index - 1]
            zoom = dpi / 72
            if page.rect.width * zoom * page.rect.height * zoom > MAX_RENDER_PIXELS:
                raise ValueError("Page dimensions exceed the inspection rendering limit.")
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
            return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
    with Image.open(path) as image:
        if image.width * image.height > MAX_RENDER_PIXELS:
            raise ValueError("Image dimensions exceed the inspection rendering limit.")
        return np.array(image.convert("L"))


def inspect_pages(
    run_dir: Path,
    scan_path: Path,
    manifest_path: Path,
    *,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Persist every page's outcome; a failed page never drops later pages."""
    index = load_inventory(run_dir)
    if (fingerprint(scan_path) != index["source_sha256"]
            or fingerprint(manifest_path) != index["manifest_sha256"]):
        raise ValueError("Run inputs have changed. Create a new run; cached inspection results cannot be reused.")
    manifest = load_manifest(manifest_path)
    index.update(status="running", error=None)

    def publish() -> None:
        save_inventory(run_dir, index)
        if progress:
            progress(index)

    publish()
    for record in index["pages"]:
        if record["status"] in {"aligned", "needs_review"}:
            continue
        # Retrying preserves source numbering, but never exposes stale aligned artifacts.
        record.update(status="processing", page_index=None, aligned=None, overlay=None, report=None, error=None)
        for key in ("quality", "alignment_confidence", "page_mark_confidence"):
            record.pop(key, None)
        publish()
        try:
            number = record["source_index"]
            page_dir = run_dir / "inspection" / f"source_{number:04d}" / uuid.uuid4().hex[:8]
            page_dir.mkdir(parents=True, exist_ok=True)
            raw = _render_page(scan_path, number, index["dpi"])
            original_path = page_dir / "original.png"
            Image.fromarray(raw).save(original_path)
            record["original"] = original_path.relative_to(run_dir).as_posix()
            publish()
            aligned = align_scan_page(raw, manifest, index["dpi"], source_index=number)
            quality = assess_alignment_quality(aligned.image, manifest, aligned.page_index, index["dpi"])
            aligned_path = page_dir / "aligned.png"
            Image.fromarray(aligned.image).save(aligned_path)
            overlay_path = save_alignment_overlay(
                aligned.image, manifest, aligned.page_index, index["dpi"], page_dir / "overlay.png",
            )
            report_path = page_dir / "quality.json"
            write_json_atomic(report_path, quality.to_dict())
            # Keep polling and progress writes small for several hundred source pages.
            quality_summary = {
                "status": quality.status, "score": quality.score,
                "warnings": quality.warnings, "review_flags": quality.review_flags,
                "metrics": {key: {"status": quality.metrics.get(key, {}).get("status")}
                            for key in ("geometry_quality", "local_quality", "image_quality")},
            }
            record.update(
                status="aligned" if quality.ok else "needs_review",
                page_index=aligned.page_index,
                aligned=aligned_path.relative_to(run_dir).as_posix(),
                overlay=overlay_path.relative_to(run_dir).as_posix(),
                report=report_path.relative_to(run_dir).as_posix(),
                alignment_confidence=aligned.alignment_confidence,
                page_mark_confidence=aligned.page_mark_confidence,
                quality=quality_summary,
            )
        except Exception as exc:
            record.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        publish()
    index["status"] = "completed"
    publish()
    return index


def inspection_asset(run_dir: Path, source_index: int, kind: str) -> Path | None:
    if kind not in {"original", "aligned", "overlay", "report"}:
        raise ValueError("Unknown page image type")
    index = load_inventory(run_dir)
    if not 1 <= source_index <= len(index["pages"]):
        raise ValueError("Source page is outside this scan")
    value = index["pages"][source_index - 1].get(kind)
    if not value:
        return None
    path = (run_dir / value).resolve()
    if not path.is_relative_to((run_dir / "inspection").resolve()):
        raise ValueError("Page image is outside this inspection")
    return path
