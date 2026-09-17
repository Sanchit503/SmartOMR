"""Extract bubble crops for cancelled/crossed-bubble training.

The extractor uses the same manifest coordinates as the production reader.
Each crop includes context around the printed bubble so slash/X cancellation
marks outside the circle are still visible to CV features or an ML model.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image

from omr.contracts import load_manifest
from omr.contracts.geometry import digit_grid_centers_mm, mm_to_px, px_per_mm
from omr.grading.mcq import mcq_sample_centers
from omr.reader.identity import roll_sample_centers
from omr.reader.numerical import numerical_sample_centers
from omr.reader.scan import ScanError, align_scan_page, load_scan_pages


DEFAULT_DATASET_ROOT = Path("data") / "bubble_cancellation_dataset"
DEFAULT_DPI = 200.0
DEFAULT_CROP_SCALE = 2.2
LABELS = ("empty", "filled", "cancelled")


@dataclass(frozen=True)
class BubbleTarget:
    page: int
    bubble_kind: str
    q_no: str
    row: str
    value: str
    x_mm: float
    y_mm: float
    pitch_x_mm: float | None = None
    pitch_y_mm: float | None = None
    sample_key: object | None = None


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "sheet"


def _manifest_path(exam_id: str, explicit: Path | None, data_dir: Path) -> Path:
    if explicit is not None:
        return explicit
    candidates = [
        data_dir / "generated" / f"{exam_id}.manifest.json",
        data_dir / "parsed" / exam_id / "manifest.json",
    ]
    candidates.extend(sorted((data_dir / "parsed").glob(f"**/{exam_id}.manifest.json")))
    for path in candidates:
        if path.exists():
            return path
    searched = ", ".join(str(path) for path in candidates[:2])
    raise FileNotFoundError(f"could not find manifest for {exam_id}; searched {searched}")


def _iter_scan_files(scans: Path) -> list[Path]:
    if scans.is_file():
        return [scans]
    if not scans.exists():
        raise FileNotFoundError(f"scan path does not exist: {scans}")
    suffixes = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".pdf"}
    return sorted(path for path in scans.iterdir() if path.is_file() and path.suffix.lower() in suffixes)


def _bubble_targets(manifest: dict, include: set[str]) -> Iterable[BubbleTarget]:
    if "program" in include:
        for program, point in manifest.get("roll_number_block", {}).get("program_selector", {}).items():
            yield BubbleTarget(
                1, "program", "program", program, program, point["x_mm"], point["y_mm"],
                sample_key=f"program.{program}",
            )

    if "roll" in include:
        roll = manifest.get("roll_number_block", {})
        for grid_name in ("btech_digits", "mtech_digits"):
            grid = roll.get(grid_name)
            if not grid:
                continue
            for (column, digit), (x_mm, y_mm) in digit_grid_centers_mm(grid).items():
                yield BubbleTarget(
                    roll.get("page", 1), "roll_digit", grid_name,
                    f"col_{column + 1}", str(digit), x_mm, y_mm,
                    pitch_x_mm=grid["col_pitch_mm"],
                    pitch_y_mm=grid["row_pitch_mm"],
                    sample_key=f"{grid_name}.{column + 1}.{digit}",
                )

    if "mcq" in include:
        pitch = float(manifest["mcq_option_pitch_mm"])
        for entry in manifest.get("mcq_block", []):
            for index, option in enumerate(entry["options"]):
                yield BubbleTarget(
                    entry.get("page", 1), "mcq", str(entry["q_no"]),
                    "option", str(option), entry["x_mm"] + index * pitch, entry["y_mm"],
                    pitch_x_mm=pitch,
                    sample_key=(int(entry["q_no"]), str(option)),
                )

    if "numerical" in include:
        for entry in manifest.get("numerical_block", []):
            for (position, digit), (x_mm, y_mm) in digit_grid_centers_mm(entry).items():
                yield BubbleTarget(
                    entry.get("page", 1), "numerical", str(entry["q_no"]),
                    f"place_{position + 1}", str(digit), x_mm, y_mm,
                    pitch_x_mm=entry["digit_pitch_mm"],
                    pitch_y_mm=entry["position_pitch_mm"],
                    sample_key=(position, digit),
                )


def _crop(gray: np.ndarray, cx: int, cy: int, half: int) -> Image.Image:
    h, w = gray.shape[:2]
    x0, y0 = max(0, cx - half), max(0, cy - half)
    x1, y1 = min(w, cx + half + 1), min(h, cy + half + 1)
    patch = np.full((2 * half + 1, 2 * half + 1), 255, dtype=np.uint8)
    src = gray[y0:y1, x0:x1]
    dx0, dy0 = x0 - (cx - half), y0 - (cy - half)
    patch[dy0:dy0 + src.shape[0], dx0:dx0 + src.shape[1]] = src
    return Image.fromarray(patch, mode="L")


def _crop_half_size_px(target: BubbleTarget, manifest: dict, dpi: float, crop_scale: float) -> int:
    radius_mm = float(manifest["bubble_radius_mm"])
    half_mm = radius_mm * crop_scale
    pitch_limits = [pitch * 0.42 for pitch in (target.pitch_x_mm, target.pitch_y_mm) if pitch]
    if pitch_limits:
        half_mm = min(half_mm, min(pitch_limits))
    return max(4, math.ceil(half_mm * px_per_mm(dpi)))


def _target_center_px(gray: np.ndarray, manifest: dict, target: BubbleTarget, dpi: float) -> tuple[int, int]:
    if target.bubble_kind == "mcq":
        entries = [entry for entry in manifest.get("mcq_block", []) if entry.get("page", 1) == target.page]
        center = mcq_sample_centers(gray, entries, manifest, dpi).get(target.sample_key)
        if center is not None:
            return center
    if target.bubble_kind in {"program", "roll_digit"}:
        center = roll_sample_centers(gray, manifest, dpi, target.page).get(str(target.sample_key))
        if center is not None:
            return center
    if target.bubble_kind == "numerical":
        for entry in manifest.get("numerical_block", []):
            if entry.get("page", 1) == target.page and int(entry["q_no"]) == int(target.q_no):
                center = numerical_sample_centers(gray, manifest, entry, dpi).get(target.sample_key)
                if center is not None:
                    return center
    return mm_to_px(target.x_mm, target.y_mm, dpi)


def _page_sample_centers(
    gray: np.ndarray,
    manifest: dict,
    page_index: int,
    dpi: float,
) -> dict[tuple[str, object], tuple[int, int]]:
    centers: dict[tuple[str, object], tuple[int, int]] = {}
    entries = [entry for entry in manifest.get("mcq_block", []) if entry.get("page", 1) == page_index]
    for key, center in mcq_sample_centers(gray, entries, manifest, dpi).items():
        centers[("mcq", key)] = center

    for key, center in roll_sample_centers(gray, manifest, dpi, page_index).items():
        centers[("program", key)] = center
        centers[("roll_digit", key)] = center

    for entry in manifest.get("numerical_block", []):
        if entry.get("page", 1) != page_index:
            continue
        for key, center in numerical_sample_centers(gray, manifest, entry, dpi).items():
            centers[("numerical", (int(entry["q_no"]), key))] = center
    return centers


def _cached_target_center_px(
    centers: dict[tuple[str, object], tuple[int, int]],
    target: BubbleTarget,
    dpi: float,
) -> tuple[int, int]:
    if target.bubble_kind == "numerical":
        center = centers.get((target.bubble_kind, (int(target.q_no), target.sample_key)))
    else:
        center = centers.get((target.bubble_kind, target.sample_key))
    if center is not None:
        return center
    return mm_to_px(target.x_mm, target.y_mm, dpi)


def _otsu_binary(gray: np.ndarray) -> np.ndarray:
    try:
        import cv2
    except ImportError:
        threshold = float(gray.mean() - gray.std() * 0.25)
        return gray < threshold
    _threshold, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    return binary > 0


def extract_features(patch: np.ndarray, bubble_radius_px: float) -> dict[str, float]:
    gray = np.asarray(patch)
    if gray.ndim == 3:
        gray = gray.mean(axis=2).astype(np.uint8)
    ink = _otsu_binary(gray)
    h, w = ink.shape
    yy, xx = np.mgrid[:h, :w]
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    inner = dist <= bubble_radius_px * 0.85
    ring = (dist > bubble_radius_px * 0.85) & (dist <= bubble_radius_px * 1.25)
    outside = dist > bubble_radius_px * 1.25

    def ratio(mask: np.ndarray) -> float:
        return float(ink[mask].mean()) if np.any(mask) else 0.0

    diag_width = max(1.5, bubble_radius_px * 0.35)
    diag_45 = np.abs((yy - cy) - (xx - cx)) <= diag_width
    diag_135 = np.abs((yy - cy) + (xx - cx)) <= diag_width
    horizontal = np.abs(yy - cy) <= diag_width
    vertical = np.abs(xx - cx) <= diag_width

    features = {
        "total_ink_ratio": float(ink.mean()),
        "inner_ink_ratio": ratio(inner),
        "ring_ink_ratio": ratio(ring),
        "outside_circle_ink_ratio": ratio(outside),
        "diagonal_45_score": ratio(diag_45),
        "diagonal_135_score": ratio(diag_135),
        "horizontal_line_score": ratio(horizontal),
        "vertical_line_score": ratio(vertical),
    }
    ys, xs = np.nonzero(ink)
    if len(xs):
        features.update({
            "ink_distance_from_center_mean": float(dist[ink].mean() / max(1.0, bubble_radius_px)),
            "ink_distance_from_center_max": float(dist[ink].max() / max(1.0, bubble_radius_px)),
            "bounding_box_width": float((xs.max() - xs.min() + 1) / max(1.0, bubble_radius_px)),
            "bounding_box_height": float((ys.max() - ys.min() + 1) / max(1.0, bubble_radius_px)),
        })
    else:
        features.update({
            "ink_distance_from_center_mean": 0.0,
            "ink_distance_from_center_max": 0.0,
            "bounding_box_width": 0.0,
            "bounding_box_height": 0.0,
        })
    features["aspect_ratio"] = features["bounding_box_width"] / max(0.001, features["bounding_box_height"])
    return features


def _write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_dataset(
    *,
    exam_id: str,
    scans: Path,
    output_dir: Path,
    data_dir: Path,
    manifest_path: Path | None,
    dpi: float,
    crop_scale: float,
    include: set[str],
    allow_partial: bool,
    page_from: int | None = None,
    page_to: int | None = None,
    skip_errors: bool = False,
) -> tuple[Path, int]:
    manifest_file = _manifest_path(exam_id, manifest_path, data_dir)
    manifest = load_manifest(manifest_file)
    dataset_dir = output_dir / _safe_id(exam_id)
    crop_dir = dataset_dir / "raw_crops"
    crop_dir.mkdir(parents=True, exist_ok=True)

    targets = list(_bubble_targets(manifest, include))
    radius_px = manifest["bubble_radius_mm"] * px_per_mm(dpi)
    metadata_rows: list[dict[str, object]] = []
    feature_rows: list[dict[str, object]] = []
    labels_rows: list[dict[str, object]] = []
    error_rows: list[dict[str, object]] = []

    for scan_path in _iter_scan_files(scans):
        source_stem = _safe_id(scan_path.stem)
        raw_pages = load_scan_pages(scan_path, dpi)
        selected_pages = [
            (source_index, raw_page)
            for source_index, raw_page in enumerate(raw_pages, start=1)
            if (page_from is None or source_index >= page_from)
            and (page_to is None or source_index <= page_to)
        ]
        for source_index, raw_page in selected_pages:
            try:
                aligned_page = align_scan_page(raw_page, manifest, dpi, source_index=source_index)
            except ScanError as exc:
                if not skip_errors:
                    raise
                error_rows.append({
                    "source_path": str(scan_path),
                    "source_index": source_index,
                    "error": str(exc),
                })
                continue
            sheet_id = f"{source_stem}_source_{source_index:04d}"
            image = np.asarray(aligned_page.image)
            if image.ndim == 3:
                image = image.mean(axis=2).astype(np.uint8)
            page_centers = _page_sample_centers(image, manifest, aligned_page.page_index, dpi)
            for target in targets:
                if target.page != aligned_page.page_index:
                    continue
                cx, cy = _cached_target_center_px(page_centers, target, dpi)
                half = _crop_half_size_px(target, manifest, dpi, crop_scale)
                patch = _crop(image.astype(np.uint8, copy=False), cx, cy, half)
                crop_id = "_".join([
                    sheet_id, f"p{target.page}", target.bubble_kind,
                    f"q{target.q_no}", target.row, target.value,
                ])
                crop_rel = Path("raw_crops") / f"{crop_id}.png"
                patch.save(dataset_dir / crop_rel)

                base = {
                    "crop_id": crop_id,
                    "exam_id": exam_id,
                    "sheet_id": sheet_id,
                    "source_path": str(scan_path),
                    "source_index": source_index,
                    "detected_page": aligned_page.page_index,
                    "page": target.page,
                    "bubble_kind": target.bubble_kind,
                    "q_no": target.q_no,
                    "row": target.row,
                    "value": target.value,
                    "x_mm": f"{target.x_mm:.3f}",
                    "y_mm": f"{target.y_mm:.3f}",
                    "center_x_px": cx,
                    "center_y_px": cy,
                    "bubble_radius_px": f"{radius_px:.3f}",
                    "crop_half_size_px": half,
                    "crop_path": crop_rel.as_posix(),
                }
                metadata_rows.append(base)
                labels_rows.append({**base, "label": ""})
                feature_rows.append({**base, **extract_features(np.asarray(patch), radius_px)})
        if allow_partial:
            continue
        detected_pages = set()
        for index, raw_page in selected_pages:
            try:
                detected_pages.add(align_scan_page(raw_page, manifest, dpi, source_index=index).page_index)
            except ScanError:
                if not skip_errors:
                    raise
        missing_pages = set(range(1, manifest["num_pages"] + 1)) - detected_pages
        if missing_pages and not skip_errors:
            raise ScanError(f"scan is missing page(s): {', '.join(str(page) for page in sorted(missing_pages))}")

    metadata_fields = list(metadata_rows[0].keys()) if metadata_rows else [
        "crop_id", "exam_id", "sheet_id", "source_path", "source_index", "detected_page",
        "page", "bubble_kind", "q_no", "row", "value", "x_mm", "y_mm", "center_x_px",
        "center_y_px", "bubble_radius_px", "crop_half_size_px", "crop_path",
    ]
    feature_fields = list(feature_rows[0].keys()) if feature_rows else metadata_fields
    _write_csv(dataset_dir / "metadata.csv", metadata_rows, metadata_fields)
    _write_csv(dataset_dir / "features.csv", feature_rows, feature_fields)
    _write_csv(dataset_dir / "labels.csv", labels_rows, metadata_fields + ["label"])
    _write_csv(dataset_dir / "errors.csv", error_rows, ["source_path", "source_index", "error"])
    (dataset_dir / "README.txt").write_text(
        "Label values: empty, filled, cancelled.\n"
        "Keep labels blank until manually reviewed. Train/test split must be by sheet_id.\n",
        encoding="utf-8",
    )
    (dataset_dir / "manifest_used.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return dataset_dir, len(metadata_rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract bubble crops for cancelled-bubble training")
    parser.add_argument("--exam-id", required=True)
    parser.add_argument("--scans", required=True, type=Path, help="Scanned image/PDF or folder of scans")
    parser.add_argument("--manifest", type=Path, help="Explicit manifest path")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--dpi", type=float, default=DEFAULT_DPI)
    parser.add_argument(
        "--crop-scale",
        type=float,
        default=DEFAULT_CROP_SCALE,
        help="Half crop size as a multiple of bubble radius before neighbor-pitch clamping",
    )
    parser.add_argument("--include", default="mcq,numerical,roll,program")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--page-from", type=int, help="1-based first physical PDF page to process")
    parser.add_argument("--page-to", type=int, help="1-based last physical PDF page to process")
    parser.add_argument("--skip-errors", action="store_true", help="Log pages that fail alignment and keep extracting the rest")
    args = parser.parse_args(argv)

    include = {item.strip().lower() for item in args.include.split(",") if item.strip()}
    try:
        dataset_dir, count = build_dataset(
            exam_id=args.exam_id,
            scans=args.scans,
            output_dir=args.output_dir,
            data_dir=args.data_dir,
            manifest_path=args.manifest,
            dpi=args.dpi,
            crop_scale=args.crop_scale,
            include=include,
            allow_partial=args.allow_partial,
            page_from=args.page_from,
            page_to=args.page_to,
            skip_errors=args.skip_errors,
        )
    except (FileNotFoundError, ScanError, ValueError, KeyError) as exc:
        print(f"error: {exc}")
        return 1
    print(f"Wrote {count} bubble crops to {dataset_dir}")
    print(f"Label file: {dataset_dir / 'labels.csv'}")
    print(f"Feature file: {dataset_dir / 'features.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
