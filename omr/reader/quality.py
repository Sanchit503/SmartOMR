"""Post-alignment quality checks for canonical SmartOMR pages.

Alignment is not a yes/no event. The perspective warp can succeed enough to
produce a page image while still leaving local drift, weak markers, or a page
bar that is barely separable from the empty bars. This module measures those
signals after alignment and turns weak evidence into review flags before any
marks are treated as final.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from omr.contracts.geometry import canonical_size_px, mm_to_px, px_per_mm


@dataclass(frozen=True)
class AlignmentQualityReport:
    page_index: int
    status: str
    score: float
    metrics: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    review_flags: list[str] = field(default_factory=list)
    report_path: str | None = None
    overlay_path: str | None = None

    @property
    def ok(self) -> bool:
        return not self.review_flags

    def with_paths(
        self,
        report_path: str | None = None,
        overlay_path: str | None = None,
    ) -> "AlignmentQualityReport":
        return replace(self, report_path=report_path, overlay_path=overlay_path)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cv2():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is required for alignment quality checks. Install dependencies with "
            "`pip install -r requirements.txt` from the SmartOMR folder."
        ) from exc
    return cv2


def _gray_array(image: object) -> np.ndarray:
    gray = np.asarray(image)
    if gray.ndim == 3:
        gray = gray.mean(axis=2)
    return gray.astype(np.uint8, copy=False)


def _rect_bounds(
    gray: np.ndarray,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
    dpi: float,
    shrink: float = 1.0,
) -> tuple[int, int, int, int]:
    scale = px_per_mm(dpi)
    cx, cy = mm_to_px(x_mm, y_mm, dpi)
    half_w = max(1, int(round(width_mm * scale * shrink / 2)))
    half_h = max(1, int(round(height_mm * scale * shrink / 2)))
    x0, x1 = max(0, cx - half_w), min(gray.shape[1], cx + half_w + 1)
    y0, y1 = max(0, cy - half_h), min(gray.shape[0], cy + half_h + 1)
    return x0, y0, x1, y1


def _rect_dark_fraction(
    gray: np.ndarray,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
    dpi: float,
    shrink: float = 1.0,
    threshold: int = 150,
) -> float:
    x0, y0, x1, y1 = _rect_bounds(gray, x_mm, y_mm, width_mm, height_mm, dpi, shrink)
    if x0 >= x1 or y0 >= y1:
        return 0.0
    roi = gray[y0:y1, x0:x1]
    return float((roi < threshold).sum()) / float(roi.size)


def _nearest_marker_center(
    gray: np.ndarray,
    x_mm: float,
    y_mm: float,
    size_mm: float,
    dpi: float,
) -> tuple[float, float] | None:
    cv2 = _cv2()
    scale = px_per_mm(dpi)
    expected_x, expected_y = mm_to_px(x_mm, y_mm, dpi)
    search = round((size_mm / 2 + 4.0) * scale)
    x0, x1 = max(0, expected_x - search), min(gray.shape[1], expected_x + search + 1)
    y0, y1 = max(0, expected_y - search), min(gray.shape[0], expected_y + search + 1)
    if x0 >= x1 or y0 >= y1:
        return None

    roi = gray[y0:y1, x0:x1]
    expected_side = size_mm * scale
    candidates: list[tuple[float, float, float]] = []
    for threshold in (135, 160, 190):
        _ignored, binary = cv2.threshold(roi, threshold, 255, cv2.THRESH_BINARY_INV)
        contours, _hierarchy = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < max(12.0, expected_side * expected_side * 0.15):
                continue
            x, y, w, h = cv2.boundingRect(contour)
            if not (expected_side * 0.45 <= w <= expected_side * 1.85):
                continue
            if not (expected_side * 0.45 <= h <= expected_side * 1.85):
                continue
            aspect = w / h if h else 0.0
            if not 0.65 <= aspect <= 1.55:
                continue
            moments = cv2.moments(contour)
            if moments["m00"]:
                cx = x0 + moments["m10"] / moments["m00"]
                cy = y0 + moments["m01"] / moments["m00"]
            else:
                cx = x0 + x + w / 2
                cy = y0 + y + h / 2
            distance = math.hypot(cx - expected_x, cy - expected_y)
            candidates.append((distance, cx, cy))

    if not candidates:
        return None
    _distance, cx, cy = min(candidates, key=lambda item: item[0])
    return cx, cy


def _nearest_bubble_center(
    gray: np.ndarray,
    x_mm: float,
    y_mm: float,
    dpi: float,
) -> tuple[float, float] | None:
    cv2 = _cv2()
    scale = px_per_mm(dpi)
    expected_x, expected_y = mm_to_px(x_mm, y_mm, dpi)
    search = round(5.0 * scale)
    x0, x1 = max(0, expected_x - search), min(gray.shape[1], expected_x + search + 1)
    y0, y1 = max(0, expected_y - search), min(gray.shape[0], expected_y + search + 1)
    if x0 >= x1 or y0 >= y1:
        return None

    roi = gray[y0:y1, x0:x1]
    min_side = 2.2 * scale
    max_side = 7.5 * scale
    candidates: list[tuple[float, float, float, float]] = []
    for threshold in (165, 190, 215, 235):
        _ignored, binary = cv2.threshold(roi, threshold, 255, cv2.THRESH_BINARY_INV)
        contours, _hierarchy = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < 12.0:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            if not (min_side <= w <= max_side and min_side <= h <= max_side):
                continue
            aspect = w / h if h else 0.0
            if not 0.45 <= aspect <= 2.20:
                continue
            moments = cv2.moments(contour)
            if moments["m00"]:
                cx = x0 + moments["m10"] / moments["m00"]
                cy = y0 + moments["m01"] / moments["m00"]
            else:
                cx = x0 + x + w / 2
                cy = y0 + y + h / 2
            distance = math.hypot(cx - expected_x, cy - expected_y)
            if distance <= 4.2 * scale:
                candidates.append((distance, -area, cx, cy))

    if not candidates:
        return None
    _distance, _area, cx, cy = min(candidates, key=lambda item: (item[0], item[1]))
    return cx, cy


def _page_fiducials(manifest: dict, page_index: int) -> list[dict]:
    return [f for f in manifest["fiducials"] if f.get("page", 1) == page_index]


def _page_orientation_marker(manifest: dict, page_index: int) -> dict | None:
    markers = [m for m in manifest["orientation_marker"] if m.get("page", 1) == page_index]
    return markers[0] if markers else None


def _page_marks(manifest: dict, page_index: int) -> list[dict]:
    return sorted(
        [m for m in manifest["page_marks"] if m.get("page", 1) == page_index],
        key=lambda item: int(item["index"]),
    )


def _expected_bubble_anchors(manifest: dict, page_index: int) -> list[tuple[str, float, float]]:
    anchors: list[tuple[str, float, float]] = []
    label_offset = manifest["mcq_label_offset_mm"]
    option_pitch = manifest["mcq_option_pitch_mm"]

    for entry in manifest["mcq_block"]:
        if entry.get("page", 1) != page_index:
            continue
        for option_index, option in enumerate(entry["options"]):
            anchors.append(
                (
                    f"Q{entry['q_no']}.{option}",
                    entry["x_mm"] + label_offset + option_index * option_pitch,
                    entry["y_mm"],
                )
            )

    if manifest["roll_number_block"].get("page", 1) == page_index:
        block = manifest["roll_number_block"]
        for program, coords in block["program_selector"].items():
            anchors.append((f"program.{program}", coords["x_mm"], coords["y_mm"]))
        for grid_name in ("btech_digits", "mtech_digits"):
            grid = block[grid_name]
            for col in range(grid["columns"]):
                for digit in range(10):
                    anchors.append(
                        (
                            f"{grid_name}.{col + 1}.{digit}",
                            grid["x_mm"] + col * grid["col_pitch_mm"],
                            grid["y_mm"] + digit * grid["row_pitch_mm"],
                        )
                    )

    for choice in manifest["continuation_program_choices"]:
        if choice.get("page", 1) == page_index:
            anchors.append((f"continuation_program.{choice['program']}", choice["x_mm"], choice["y_mm"]))

    return anchors


def _measure_marker_quality(
    gray: np.ndarray,
    manifest: dict,
    page_index: int,
    dpi: float,
    review_flags: list[str],
) -> dict[str, Any]:
    scale = px_per_mm(dpi)
    dark_scores: dict[str, float] = {}
    residuals_px: list[float] = []
    missing_centers: list[str] = []

    for fiducial in _page_fiducials(manifest, page_index):
        label = f"fiducial_{fiducial['corner']}"
        dark = _rect_dark_fraction(
            gray,
            fiducial["x_mm"],
            fiducial["y_mm"],
            fiducial["size_mm"],
            fiducial["size_mm"],
            dpi,
            shrink=0.70,
        )
        dark_scores[label] = round(dark, 3)
        center = _nearest_marker_center(
            gray,
            fiducial["x_mm"],
            fiducial["y_mm"],
            fiducial["size_mm"],
            dpi,
        )
        if center is None:
            missing_centers.append(label)
            continue
        expected = mm_to_px(fiducial["x_mm"], fiducial["y_mm"], dpi)
        residuals_px.append(math.hypot(center[0] - expected[0], center[1] - expected[1]))
        if dark < 0.74:
            review_flags.append(f"{label} is weak after alignment (dark fraction {dark:.2f})")

    marker = _page_orientation_marker(manifest, page_index)
    if marker is not None:
        dark = _rect_dark_fraction(
            gray,
            marker["x_mm"],
            marker["y_mm"],
            marker["size_mm"],
            marker["size_mm"],
            dpi,
            shrink=0.70,
        )
        dark_scores["orientation_marker"] = round(dark, 3)
        center = _nearest_marker_center(gray, marker["x_mm"], marker["y_mm"], marker["size_mm"], dpi)
        if center is None:
            missing_centers.append("orientation_marker")
        else:
            expected = mm_to_px(marker["x_mm"], marker["y_mm"], dpi)
            residuals_px.append(math.hypot(center[0] - expected[0], center[1] - expected[1]))
        if dark < 0.58:
            review_flags.append(f"orientation marker is weak after alignment (dark fraction {dark:.2f})")

    if missing_centers:
        review_flags.append("could not re-detect aligned marker center(s): " + ", ".join(missing_centers))

    max_residual_px = max(residuals_px, default=0.0)
    mean_residual_px = float(np.mean(residuals_px)) if residuals_px else 0.0
    if residuals_px and max_residual_px / scale > 0.90:
        review_flags.append(
            "registration marker residual is high "
            f"(max {max_residual_px / scale:.2f}mm after warp)"
        )

    return {
        "dark_fractions": dark_scores,
        "matched_centers": len(residuals_px),
        "missing_centers": missing_centers,
        "mean_residual_px": round(mean_residual_px, 2),
        "max_residual_px": round(max_residual_px, 2),
        "mean_residual_mm": round(mean_residual_px / scale, 3),
        "max_residual_mm": round(max_residual_px / scale, 3),
    }


def _measure_page_marks(
    gray: np.ndarray,
    manifest: dict,
    page_index: int,
    dpi: float,
    review_flags: list[str],
) -> dict[str, Any]:
    scores = {
        str(mark["index"]): _rect_dark_fraction(
            gray,
            mark["x_mm"],
            mark["y_mm"],
            mark["width_mm"],
            mark["height_mm"],
            dpi,
            shrink=0.52,
        )
        for mark in _page_marks(manifest, page_index)
    }
    if not scores:
        review_flags.append("page-index bars are missing from the manifest for this page")
        return {"scores": {}, "detected_index": None, "contrast": 0.0}

    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    detected_index = int(ranked[0][0])
    top_score = ranked[0][1]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    expected_score = scores.get(str(page_index), 0.0)
    contrast = expected_score - max(
        [score for index, score in scores.items() if index != str(page_index)] or [0.0]
    )

    if detected_index != page_index:
        review_flags.append(
            f"page-index bars read page {detected_index}, expected page {page_index}"
        )
    if expected_score < 0.45:
        review_flags.append(
            f"page {page_index} index bar is too faint after alignment (score {expected_score:.2f})"
        )
    if contrast < 0.18 and len(scores) > 1:
        review_flags.append(
            f"page {page_index} index bar contrast is weak ({contrast:.2f}); page order should be reviewed"
        )

    return {
        "scores": {index: round(score, 3) for index, score in sorted(scores.items())},
        "detected_index": detected_index,
        "top_score": round(top_score, 3),
        "runner_up_score": round(runner_up, 3),
        "expected_score": round(expected_score, 3),
        "contrast": round(contrast, 3),
    }


def _measure_bubble_anchors(
    gray: np.ndarray,
    manifest: dict,
    page_index: int,
    dpi: float,
    review_flags: list[str],
) -> dict[str, Any]:
    scale = px_per_mm(dpi)
    anchors = _expected_bubble_anchors(manifest, page_index)
    residuals_px: list[float] = []
    missing: list[str] = []

    for label, x_mm, y_mm in anchors:
        center = _nearest_bubble_center(gray, x_mm, y_mm, dpi)
        if center is None:
            missing.append(label)
            continue
        expected_x, expected_y = mm_to_px(x_mm, y_mm, dpi)
        residuals_px.append(math.hypot(center[0] - expected_x, center[1] - expected_y))

    expected_count = len(anchors)
    matched_count = len(residuals_px)
    matched_fraction = matched_count / expected_count if expected_count else 1.0
    mean_residual_px = float(np.mean(residuals_px)) if residuals_px else 0.0
    max_residual_px = max(residuals_px, default=0.0)

    if expected_count >= 8 and matched_fraction < 0.72:
        review_flags.append(
            "bubble anchor detection is weak "
            f"({matched_count}/{expected_count} printed bubbles re-detected)"
        )
    if residuals_px and mean_residual_px / scale > 0.85:
        review_flags.append(
            "bubble grid is locally shifted "
            f"(mean residual {mean_residual_px / scale:.2f}mm)"
        )
    if residuals_px and max_residual_px / scale > 1.75:
        review_flags.append(
            "at least one bubble anchor is far from its manifest position "
            f"(max residual {max_residual_px / scale:.2f}mm)"
        )

    return {
        "expected": expected_count,
        "matched": matched_count,
        "matched_fraction": round(matched_fraction, 3),
        "missing_sample": missing[:12],
        "missing_count": len(missing),
        "mean_residual_px": round(mean_residual_px, 2),
        "max_residual_px": round(max_residual_px, 2),
        "mean_residual_mm": round(mean_residual_px / scale, 3),
        "max_residual_mm": round(max_residual_px / scale, 3),
    }


def _measure_image_quality(gray: np.ndarray, warnings: list[str], review_flags: list[str]) -> dict[str, Any]:
    cv2 = _cv2()
    p5, p50, p95 = [float(x) for x in np.percentile(gray, [5, 50, 95])]
    contrast = p95 - p5
    laplacian_variance = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    dark_fraction = float((gray < 150).sum()) / float(gray.size)

    if laplacian_variance < 5.0:
        review_flags.append(f"page image is very blurry (laplacian variance {laplacian_variance:.1f})")
    elif laplacian_variance < 18.0:
        warnings.append(f"page image is somewhat blurry (laplacian variance {laplacian_variance:.1f})")
    if p50 < 135:
        review_flags.append(f"page background is unusually dark (median pixel {p50:.0f})")
    if contrast < 35:
        warnings.append(f"page contrast is low (p95-p5 {contrast:.0f})")

    return {
        "p5": round(p5, 1),
        "median": round(p50, 1),
        "p95": round(p95, 1),
        "contrast": round(contrast, 1),
        "laplacian_variance": round(laplacian_variance, 1),
        "dark_fraction": round(dark_fraction, 4),
    }


def _score_report(metrics: dict[str, Any], review_flags: list[str], warnings: list[str]) -> float:
    score = 1.0
    score -= min(0.60, 0.18 * len(review_flags))
    score -= min(0.20, 0.05 * len(warnings))

    anchors = metrics.get("bubble_anchors", {})
    score -= max(0.0, 1.0 - float(anchors.get("matched_fraction", 1.0))) * 0.25
    score -= min(0.20, float(anchors.get("mean_residual_mm", 0.0)) * 0.08)

    markers = metrics.get("registration_markers", {})
    score -= min(0.18, float(markers.get("max_residual_mm", 0.0)) * 0.08)

    page_marks = metrics.get("page_marks", {})
    score -= max(0.0, 0.35 - float(page_marks.get("contrast", 0.35))) * 0.25

    return round(max(0.0, min(1.0, score)), 3)


def assess_alignment_quality(
    image: object,
    manifest: dict,
    page_index: int,
    dpi: float,
) -> AlignmentQualityReport:
    """Measure whether an aligned page is reliable enough for auto-processing."""
    gray = _gray_array(image)
    warnings: list[str] = []
    review_flags: list[str] = []
    metrics: dict[str, Any] = {}

    expected_size = canonical_size_px(manifest, dpi)
    actual_size = (gray.shape[1], gray.shape[0])
    metrics["page_size"] = {"expected_px": list(expected_size), "actual_px": list(actual_size)}
    if abs(actual_size[0] - expected_size[0]) > 2 or abs(actual_size[1] - expected_size[1]) > 2:
        review_flags.append(
            f"canonical page size is {actual_size[0]}x{actual_size[1]}px, "
            f"expected {expected_size[0]}x{expected_size[1]}px"
        )

    metrics["registration_markers"] = _measure_marker_quality(gray, manifest, page_index, dpi, review_flags)
    metrics["page_marks"] = _measure_page_marks(gray, manifest, page_index, dpi, review_flags)
    metrics["bubble_anchors"] = _measure_bubble_anchors(gray, manifest, page_index, dpi, review_flags)
    metrics["image_quality"] = _measure_image_quality(gray, warnings, review_flags)

    score = _score_report(metrics, review_flags, warnings)
    status = "ready" if not review_flags else "needs_review"
    return AlignmentQualityReport(
        page_index=page_index,
        status=status,
        score=score,
        metrics=metrics,
        warnings=warnings,
        review_flags=review_flags,
    )


def _draw_rect_mm(
    draw: ImageDraw.ImageDraw,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
    dpi: float,
    color: tuple[int, int, int],
    width: int = 3,
) -> None:
    cx, cy = mm_to_px(x_mm, y_mm, dpi)
    scale = px_per_mm(dpi)
    half_w = int(round(width_mm * scale / 2))
    half_h = int(round(height_mm * scale / 2))
    draw.rectangle([cx - half_w, cy - half_h, cx + half_w, cy + half_h], outline=color, width=width)


def _draw_circle_mm(
    draw: ImageDraw.ImageDraw,
    x_mm: float,
    y_mm: float,
    radius_mm: float,
    dpi: float,
    color: tuple[int, int, int],
    width: int = 2,
) -> None:
    cx, cy = mm_to_px(x_mm, y_mm, dpi)
    radius = int(round(radius_mm * px_per_mm(dpi)))
    draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], outline=color, width=width)


def save_alignment_overlay(
    image: object,
    manifest: dict,
    page_index: int,
    dpi: float,
    path: str | Path,
) -> Path:
    """Save a visual overlay of the manifest's expected alignment anchors."""
    gray = _gray_array(image)
    base = Image.fromarray(gray, mode="L").convert("RGB")
    overlay = Image.new("RGBA", base.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)

    for fiducial in _page_fiducials(manifest, page_index):
        _draw_rect_mm(
            draw,
            fiducial["x_mm"],
            fiducial["y_mm"],
            fiducial["size_mm"],
            fiducial["size_mm"],
            dpi,
            (0, 112, 255, 235),
            width=4,
        )

    marker = _page_orientation_marker(manifest, page_index)
    if marker is not None:
        _draw_rect_mm(
            draw,
            marker["x_mm"],
            marker["y_mm"],
            marker["size_mm"],
            marker["size_mm"],
            dpi,
            (170, 60, 210, 235),
            width=4,
        )

    for mark in _page_marks(manifest, page_index):
        color = (0, 150, 70, 235) if mark.get("filled") else (245, 135, 0, 235)
        _draw_rect_mm(
            draw,
            mark["x_mm"],
            mark["y_mm"],
            mark["width_mm"],
            mark["height_mm"],
            dpi,
            color,
            width=3,
        )

    bubble_radius = manifest["bubble_radius_mm"]
    for _label, x_mm, y_mm in _expected_bubble_anchors(manifest, page_index):
        _draw_circle_mm(draw, x_mm, y_mm, bubble_radius, dpi, (220, 30, 40, 180), width=2)

    composed = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    composed.save(path)
    return path


def save_alignment_report(report: AlignmentQualityReport, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    return path
