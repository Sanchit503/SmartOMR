"""Write visual diagnostics for scan pages that fail SmartOMR alignment."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw

from omr.contracts import load_manifest
from omr.reader.quality import save_alignment_overlay, save_sampling_overlay
from omr.reader.scan import (
    _alignment_sources,
    _as_gray_array,
    _as_image_array,
    _denoise_for_detection,
    _map_points_to_original,
    _rect_dark_fraction,
    _select_fiducials,
    _warp_with_best_orientation,
    detect_page_index,
    load_scan_pages,
)


def _safe_label(value: str) -> str:
    return value.replace(" ", "_").replace("/", "_")


def _draw_marker_overlay(gray, points, path: Path) -> None:
    image = Image.fromarray(gray).convert("RGB")
    draw = ImageDraw.Draw(image)
    labels = ("TL", "TR", "BR", "BL")
    colors = ("blue", "green", "orange", "red")
    for index, (x, y) in enumerate(points):
        draw.rectangle([x - 18, y - 18, x + 18, y + 18], outline=colors[index], width=5)
        draw.text((x + 20, y + 20), labels[index], fill=colors[index])
    image.save(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Debug failed SmartOMR scan pages")
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--pages", required=True, help="Comma-separated physical PDF pages, 1-based")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--dpi", type=float, default=200.0)
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    raw_pages = load_scan_pages(args.pdf, args.dpi)
    page_numbers = [int(part.strip()) for part in args.pages.split(",") if part.strip()]
    args.out.mkdir(parents=True, exist_ok=True)
    page_marks = sorted(manifest["page_marks"], key=lambda mark: mark["index"])
    results = []

    for source_index in page_numbers:
        raw = raw_pages[source_index - 1]
        original = _as_image_array(raw)
        gray_raw = _as_gray_array(original)
        Image.fromarray(gray_raw).save(args.out / f"source_{source_index:04d}_raw.png")
        attempts = []

        for source in _alignment_sources(gray_raw, _denoise_for_detection(gray_raw), manifest, args.dpi):
            item = {"source": source.label}
            label = _safe_label(source.label)
            try:
                source_points, marker_confidence = _select_fiducials(
                    source.detection_image,
                    manifest,
                    allow_inferred=True,
                    marker_estimates=source.marker_estimates,
                )
                original_points = _map_points_to_original(source_points, source.points_to_original)
                item["marker_confidence"] = float(marker_confidence)
                item["source_points_px"] = original_points.round(2).tolist()
                _draw_marker_overlay(
                    gray_raw,
                    original_points,
                    args.out / f"source_{source_index:04d}_{label}_marker_overlay.png",
                )

                canonical, orientation_confidence = _warp_with_best_orientation(
                    original,
                    original_points,
                    manifest,
                    args.dpi,
                )
                canonical_gray = _as_gray_array(canonical)
                item["orientation_confidence"] = float(orientation_confidence)
                Image.fromarray(canonical_gray).save(args.out / f"source_{source_index:04d}_{label}_canonical.png")

                scores = {
                    int(mark["index"]): _rect_dark_fraction(
                        canonical_gray,
                        mark["x_mm"],
                        mark["y_mm"],
                        mark["width_mm"],
                        mark["height_mm"],
                        args.dpi,
                        shrink=0.52,
                        threshold=150,
                    )
                    for mark in page_marks
                }
                item["page_mark_scores"] = {str(key): round(value, 4) for key, value in scores.items()}

                try:
                    page_index, page_confidence, _scores = detect_page_index(canonical_gray, manifest, args.dpi)
                    item["detected_page"] = int(page_index)
                    item["page_confidence"] = float(page_confidence)
                    save_alignment_overlay(
                        canonical,
                        manifest,
                        page_index,
                        args.dpi,
                        args.out / f"source_{source_index:04d}_{label}_alignment_overlay.png",
                    )
                    save_sampling_overlay(
                        canonical,
                        manifest,
                        page_index,
                        args.dpi,
                        args.out / f"source_{source_index:04d}_{label}_sampling_overlay.png",
                    )
                except Exception as exc:
                    item["page_index_error"] = str(exc)
                    save_alignment_overlay(
                        canonical,
                        manifest,
                        1,
                        args.dpi,
                        args.out / f"source_{source_index:04d}_{label}_forced_page1_alignment_overlay.png",
                    )
                    save_sampling_overlay(
                        canonical,
                        manifest,
                        1,
                        args.dpi,
                        args.out / f"source_{source_index:04d}_{label}_forced_page1_sampling_overlay.png",
                    )
            except Exception as exc:
                item["error"] = str(exc)
            attempts.append(item)
        results.append({"source_index": source_index, "attempts": attempts})

    (args.out / "diagnostics.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(args.out)
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
