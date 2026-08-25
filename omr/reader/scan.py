"""Scan/PDF loading, fiducial alignment, and page-index detection.

This is Module 2's first concrete home: both scanned pages and phone photos
are normalized to canonical manifest coordinates before any identity or MCQ
reading happens downstream.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, permutations
from math import hypot
from pathlib import Path

import numpy as np

from omr.contracts.geometry import canonical_size_px, mm_to_px, px_per_mm
from omr.models import AlignedPage


class ScanError(RuntimeError):
    """The scanned page cannot be read safely enough for auto-evaluation."""


def _cv2():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is required for scan evaluation. Install dependencies with "
            "`pip install -r requirements.txt` from the SmartOMR folder."
        ) from exc
    return cv2


def _as_gray_array(image: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    gray = np.asarray(image)
    if gray.ndim == 3:
        return cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    return gray


@dataclass(frozen=True)
class _SquareCandidate:
    center: np.ndarray
    side: float
    area: float
    ink_fraction: float
    bbox: tuple[int, int, int, int]


CORNER_ORDER = ("TL", "TR", "BR", "BL")
CORNER_INDEX = {corner: index for index, corner in enumerate(CORNER_ORDER)}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}
SMARTOMR_VALIDATION_PREFIX = "could not validate this as a SmartOMR sheet"


def load_scan_pages(path: str | Path, dpi: float) -> list[np.ndarray]:
    """Load a scanned image or PDF as grayscale page arrays."""
    cv2 = _cv2()
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError("PyMuPDF is required to read scanned PDFs") from exc
        doc = fitz.open(path)
        pages: list[np.ndarray] = []
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        for page in doc:
            pix = page.get_pixmap(matrix=matrix, colorspace=fitz.csGRAY, alpha=False)
            pages.append(np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy())
        if not pages:
            raise ScanError(f"{path} has no pages")
        return pages

    if suffix in IMAGE_EXTENSIONS:
        data = np.fromfile(path, dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)
        if image is None:
            raise ScanError(f"could not decode image {path}")
        return [image]

    raise ScanError(f"unsupported scan type {suffix!r}; use an image or PDF")


def _binary_dark(gray: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    _threshold, binary = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    return binary


def _edge_map(gray: np.ndarray) -> np.ndarray:
    """Edges are for locating sheet/marker geometry, not for reading marks."""
    cv2 = _cv2()
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5, 5), 0)
    median = float(np.median(blur))
    low = max(30, int(0.35 * median))
    high = min(220, max(low + 40, int(0.85 * median)))
    edges = cv2.Canny(blur, low, high)
    kernel = np.ones((3, 3), dtype=np.uint8)
    return cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)


def _find_dark_square_candidates(gray: np.ndarray) -> list[_SquareCandidate]:
    cv2 = _cv2()
    binary = _binary_dark(gray)
    kernel = np.ones((3, 3), dtype=np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    contours, _hierarchy = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    image_area = gray.shape[0] * gray.shape[1]
    min_area = max(50.0, image_area * 0.00002)
    max_area = image_area * 0.02
    candidates: list[_SquareCandidate] = []
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < min_area or area > max_area:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if w <= 0 or h <= 0:
            continue
        aspect = w / h
        if not 0.72 <= aspect <= 1.38:
            continue
        roi = binary[y : y + h, x : x + w]
        ink_fraction = float((roi > 0).sum()) / float(roi.size)
        if ink_fraction < 0.55:
            continue
        moments = cv2.moments(contour)
        if moments["m00"] == 0:
            continue
        center = np.array([moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]], dtype=np.float32)
        candidates.append(
            _SquareCandidate(
                center=center,
                side=float(max(w, h)),
                area=area,
                ink_fraction=ink_fraction,
                bbox=(x, y, w, h),
            )
        )
    return candidates


def _find_edge_square_candidates(gray: np.ndarray) -> list[_SquareCandidate]:
    """Find black square marker candidates from edges/contours.

    The dark-threshold detector is excellent for clean scans. The edge path
    adds resilience for phone images where illumination shifts make a single
    global threshold less trustworthy. Candidates still need real dark ink in
    their bounding box, so empty roll boxes and text strokes do not become
    fiducials.
    """
    cv2 = _cv2()
    edges = _edge_map(gray)
    binary = _binary_dark(gray)
    contours, _hierarchy = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    image_area = gray.shape[0] * gray.shape[1]
    min_area = max(50.0, image_area * 0.000015)
    max_area = image_area * 0.02
    candidates: list[_SquareCandidate] = []
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < min_area or area > max_area:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if w <= 0 or h <= 0:
            continue
        aspect = w / h
        if not 0.70 <= aspect <= 1.42:
            continue
        roi = binary[y : y + h, x : x + w]
        ink_fraction = float((roi > 0).sum()) / float(roi.size)
        if ink_fraction < 0.42:
            continue
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.06 * perimeter, True)
        if len(approx) < 4:
            continue
        moments = cv2.moments(contour)
        if moments["m00"]:
            center = np.array([moments["m10"] / moments["m00"], moments["m01"] / moments["m00"]], dtype=np.float32)
        else:
            center = np.array([x + w / 2, y + h / 2], dtype=np.float32)
        candidates.append(
            _SquareCandidate(
                center=center,
                side=float(max(w, h)),
                area=max(area, float(w * h) * ink_fraction),
                ink_fraction=ink_fraction,
                bbox=(x, y, w, h),
            )
        )
    return candidates


def _dedupe_square_candidates(candidates: list[_SquareCandidate]) -> list[_SquareCandidate]:
    unique: list[_SquareCandidate] = []
    ranked = sorted(candidates, key=lambda c: c.area * c.ink_fraction, reverse=True)
    for candidate in ranked:
        duplicate = False
        for existing in unique:
            distance = float(np.linalg.norm(candidate.center - existing.center))
            if distance < max(4.0, min(candidate.side, existing.side) * 0.65):
                duplicate = True
                break
        if not duplicate:
            unique.append(candidate)
    return unique


def _order_points(points: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float32)
    center = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
    return points[np.argsort(angles)]


def _expected_marker_aspect(manifest: dict) -> float:
    by_corner = {f["corner"]: f for f in manifest["fiducials"] if f.get("page", 1) == 1}
    tl, tr, bl = by_corner["TL"], by_corner["TR"], by_corner["BL"]
    width = hypot(tr["x_mm"] - tl["x_mm"], tr["y_mm"] - tl["y_mm"])
    height = hypot(bl["x_mm"] - tl["x_mm"], bl["y_mm"] - tl["y_mm"])
    return width / height


def _quad_aspect(ordered: np.ndarray) -> float:
    tl, tr, br, bl = ordered
    width = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2.0
    height = (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2.0
    if height <= 0:
        return 0.0
    return float(width / height)


def _quad_matches_manifest(ordered: np.ndarray, manifest: dict) -> bool:
    expected = _expected_marker_aspect(manifest)
    actual = _quad_aspect(ordered)
    # Broad enough for phone perspective, narrow enough to reject four random
    # dark squares that do not look like this OMR's corner-marker rectangle.
    # A 90-degree rotated photo swaps width/height, so accept the reciprocal
    # aspect too; orientation is resolved later by the orientation marker.
    reciprocal = 1.0 / expected if expected > 0 else 0.0
    return (
        expected * 0.55 <= actual <= expected * 1.65
        or reciprocal * 0.55 <= actual <= reciprocal * 1.65
    )


def _candidate_size_stats(candidates: tuple[_SquareCandidate, ...]) -> tuple[float, float]:
    sides = np.array([candidate.side for candidate in candidates], dtype=np.float32)
    ratio = float(sides.max() / max(sides.min(), 1.0))
    spread = float(sides.std() / max(sides.mean(), 1.0))
    return ratio, spread


def _usable_marker_quad(ordered: np.ndarray, manifest: dict, image_area: float) -> float | None:
    cv2 = _cv2()
    quad_area = abs(float(cv2.contourArea(ordered)))
    if quad_area <= 0:
        return None
    if quad_area < image_area * 0.04:
        return None
    polygon = ordered.reshape(-1, 1, 2)
    if not cv2.isContourConvex(polygon.astype(np.float32)):
        return None
    if not _quad_matches_manifest(ordered, manifest):
        return None
    return quad_area


def _find_four_marker_quad(
    candidates: list[_SquareCandidate],
    gray_shape: tuple[int, ...],
    manifest: dict,
) -> tuple[np.ndarray, float] | None:
    best_points: np.ndarray | None = None
    best_score = -1.0
    best_size_spread = 1.0
    image_area = gray_shape[0] * gray_shape[1]
    for combo in combinations(candidates, 4):
        size_ratio, size_spread = _candidate_size_stats(combo)
        if size_ratio > 1.60:
            continue
        centers = np.array([c.center for c in combo], dtype=np.float32)
        ordered = _order_points(centers)
        quad_area = _usable_marker_quad(ordered, manifest, image_area)
        if quad_area is None:
            continue
        ink_score = min(c.ink_fraction for c in combo)
        aspect_error = abs(_quad_aspect(ordered) - _expected_marker_aspect(manifest))
        score = quad_area * ink_score * (1.0 - min(size_spread, 0.9)) / (1.0 + aspect_error)
        if score > best_score:
            best_score = score
            best_points = ordered
            best_size_spread = size_spread

    if best_points is None:
        return None
    return best_points, max(0.0, 1.0 - best_size_spread)


def _infer_missing_corner(known: dict[int, np.ndarray]) -> tuple[np.ndarray, int]:
    missing = next(index for index in range(4) if index not in known)
    ordered = np.zeros((4, 2), dtype=np.float32)
    for index, point in known.items():
        ordered[index] = point

    # Corner markers form a rectangle on paper. In an oblique photo the true
    # mapping is perspective, but this parallelogram estimate is good enough
    # to seed the final perspective warp when only one marker is clipped.
    if missing == CORNER_INDEX["TL"]:
        ordered[missing] = ordered[CORNER_INDEX["TR"]] + ordered[CORNER_INDEX["BL"]] - ordered[CORNER_INDEX["BR"]]
    elif missing == CORNER_INDEX["TR"]:
        ordered[missing] = ordered[CORNER_INDEX["TL"]] + ordered[CORNER_INDEX["BR"]] - ordered[CORNER_INDEX["BL"]]
    elif missing == CORNER_INDEX["BR"]:
        ordered[missing] = ordered[CORNER_INDEX["TR"]] + ordered[CORNER_INDEX["BL"]] - ordered[CORNER_INDEX["TL"]]
    else:
        ordered[missing] = ordered[CORNER_INDEX["TL"]] + ordered[CORNER_INDEX["BR"]] - ordered[CORNER_INDEX["TR"]]
    return ordered, missing


def _inferred_corner_is_plausible(point: np.ndarray, gray_shape: tuple[int, ...]) -> bool:
    height, width = gray_shape[:2]
    margin = max(width, height) * 0.20
    x, y = float(point[0]), float(point[1])
    return -margin <= x <= width + margin and -margin <= y <= height + margin


def _find_three_marker_quad(
    candidates: list[_SquareCandidate],
    gray_shape: tuple[int, ...],
    manifest: dict,
    marker_estimates: np.ndarray | None = None,
) -> tuple[np.ndarray, float] | None:
    if len(candidates) < 3:
        return None

    image_area = gray_shape[0] * gray_shape[1]
    best_points: np.ndarray | None = None
    best_score = -1.0
    best_size_spread = 1.0
    best_variant_confidence = 1.0
    for combo in combinations(candidates, 3):
        size_ratio, size_spread = _candidate_size_stats(combo)
        if size_ratio > 1.60:
            continue
        for corner_indices in combinations(range(4), 3):
            for ordered_combo in permutations(combo):
                known = {
                    corner_index: candidate.center
                    for corner_index, candidate in zip(corner_indices, ordered_combo)
                }
                inferred, missing = _infer_missing_corner(known)
                variants = [(inferred, 0.74)]
                if marker_estimates is not None:
                    estimated = inferred.copy()
                    estimated[missing] = marker_estimates[missing]
                    variants.append((estimated, 0.92))

                for ordered, variant_confidence in variants:
                    if not np.isfinite(ordered).all():
                        continue
                    if not _inferred_corner_is_plausible(ordered[missing], gray_shape):
                        continue
                    quad_area = _usable_marker_quad(ordered, manifest, image_area)
                    if quad_area is None:
                        continue
                    ink_score = min(c.ink_fraction for c in combo)
                    aspect_error = abs(_quad_aspect(ordered) - _expected_marker_aspect(manifest))
                    score = (
                        quad_area
                        * ink_score
                        * variant_confidence
                        * (1.0 - min(size_spread, 0.9))
                        / (1.25 + aspect_error)
                    )
                    if score > best_score:
                        best_score = score
                        best_points = ordered
                        best_size_spread = size_spread
                        best_variant_confidence = variant_confidence

    if best_points is None:
        return None
    confidence = best_variant_confidence * (1.0 - best_size_spread)
    return best_points, max(0.25, min(0.86, confidence))


def _select_fiducials(
    gray: np.ndarray,
    manifest: dict,
    allow_inferred: bool = True,
    marker_estimates: np.ndarray | None = None,
) -> tuple[np.ndarray, float]:
    candidates = _dedupe_square_candidates(
        _find_dark_square_candidates(gray) + _find_edge_square_candidates(gray)
    )[:30]
    if len(candidates) < 3:
        raise ScanError(
            f"found only {len(candidates)} likely filled square marker(s); "
            "this does not look like a full SmartOMR page, or the scan may be cropped, too faint, or blurred"
        )

    four_marker_quad = _find_four_marker_quad(candidates, gray.shape, manifest)
    if four_marker_quad is not None:
        return four_marker_quad

    if allow_inferred:
        three_marker_quad = _find_three_marker_quad(candidates, gray.shape, manifest, marker_estimates)
        if three_marker_quad is not None:
            return three_marker_quad

    raise ScanError(
        "could not form reliable SmartOMR corner markers; the image may not be this OMR, "
        "or it may be cropped, half-page, or mixed with heavy dark marks"
    )


def _page_aspect(manifest: dict) -> float:
    page = manifest.get("page", {})
    width_mm = float(page.get("width_mm", 210.0))
    height_mm = float(page.get("height_mm", 297.0))
    return width_mm / max(height_mm, 1.0)


def _page_target_corners_px(manifest: dict, dpi: float) -> np.ndarray:
    width, height = canonical_size_px(manifest, dpi)
    return np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )


def _quad_touches_image_border(ordered: np.ndarray, gray_shape: tuple[int, ...]) -> bool:
    height, width = gray_shape[:2]
    x_min, y_min = ordered.min(axis=0)
    x_max, y_max = ordered.max(axis=0)
    return (
        x_min <= 2
        and y_min <= 2
        and x_max >= width - 3
        and y_max >= height - 3
    )


def _expand_quad(ordered: np.ndarray, scale: float = 1.05) -> np.ndarray:
    center = ordered.mean(axis=0)
    return (center + (ordered - center) * scale).astype(np.float32)


def _find_page_quad(gray: np.ndarray, manifest: dict) -> tuple[np.ndarray, float] | None:
    """Find the visible paper boundary for a rough first-pass warp."""
    cv2 = _cv2()
    expected_aspect = _page_aspect(manifest)
    image_area = float(gray.shape[0] * gray.shape[1])
    best_quad: np.ndarray | None = None
    best_score = -1.0

    edges = _edge_map(gray)
    for kernel_size in (7, 11, 15, 21):
        kernel = np.ones((kernel_size, kernel_size), dtype=np.uint8)
        closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
        dilated = cv2.dilate(closed, kernel, iterations=1)
        contours, _hierarchy = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for contour in contours:
            contour_area = float(cv2.contourArea(contour))
            if contour_area < image_area * 0.05:
                continue

            perimeter = cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
            variants: list[np.ndarray] = []
            if len(approx) == 4:
                variants.append(approx.reshape(4, 2).astype(np.float32))
            rect = cv2.minAreaRect(contour)
            rect_width, rect_height = rect[1]
            if rect_width > 0 and rect_height > 0:
                variants.append(cv2.boxPoints(rect).astype(np.float32))

            for variant in variants:
                ordered = _order_points(variant)
                if _quad_touches_image_border(ordered, gray.shape):
                    continue
                quad_area = abs(float(cv2.contourArea(ordered)))
                if quad_area < image_area * 0.08:
                    continue
                if quad_area > image_area * 0.92:
                    continue
                aspect = _quad_aspect(ordered)
                aspect_error = min(abs(aspect - expected_aspect), abs(aspect - (1.0 / expected_aspect)))
                if aspect_error > 0.75:
                    continue
                score = quad_area / (1.0 + aspect_error)
                if score > best_score:
                    best_score = score
                    best_quad = ordered

    if best_quad is None:
        return None

    area_fraction = abs(float(cv2.contourArea(best_quad))) / image_area
    confidence = max(0.35, min(0.80, 0.35 + area_fraction))
    return _expand_quad(best_quad), confidence


def _rough_page_warp(
    gray: np.ndarray,
    manifest: dict,
    dpi: float,
    page_quad: tuple[np.ndarray, float] | None = None,
) -> tuple[np.ndarray, float] | None:
    cv2 = _cv2()
    page_quad = page_quad if page_quad is not None else _find_page_quad(gray, manifest)
    if page_quad is None:
        return None
    source_points, confidence = page_quad
    target_points = _page_target_corners_px(manifest, dpi)
    width, height = canonical_size_px(manifest, dpi)
    transform = cv2.getPerspectiveTransform(source_points.astype(np.float32), target_points)
    warped = cv2.warpPerspective(
        gray,
        transform,
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )
    return warped, confidence


def _alignment_sources(
    gray: np.ndarray,
    manifest: dict,
    dpi: float,
) -> list[tuple[str, np.ndarray, float, np.ndarray | None]]:
    sources: list[tuple[str, np.ndarray, float, np.ndarray | None]] = []
    page_quad = _find_page_quad(gray, manifest)
    if page_quad is not None:
        marker_estimates = _project_expected_fiducials_from_page_quad(page_quad[0], manifest, dpi)
        sources.append(("marker geometry plus page boundary", gray, 1.0, marker_estimates))

    rough = _rough_page_warp(gray, manifest, dpi, page_quad=page_quad)
    if rough is not None:
        rough_image, confidence = rough
        sources.append(("rough page contour", rough_image, confidence, _expected_fiducials_px(manifest, dpi)))
    sources.append(("marker geometry", gray, 1.0, None))
    return sources


def _alignment_error(errors: list[str]) -> ScanError:
    if errors:
        return ScanError(f"{SMARTOMR_VALIDATION_PREFIX}: {'; '.join(errors)}")
    return ScanError(f"{SMARTOMR_VALIDATION_PREFIX}: no alignment strategy was able to run")


def _expected_fiducials_px(manifest: dict, dpi: float) -> np.ndarray:
    by_corner = {f["corner"]: f for f in manifest["fiducials"] if f.get("page", 1) == 1}
    return np.array([mm_to_px(by_corner[corner]["x_mm"], by_corner[corner]["y_mm"], dpi) for corner in CORNER_ORDER],
                    dtype=np.float32)


def _project_expected_fiducials_from_page_quad(
    page_quad: np.ndarray,
    manifest: dict,
    dpi: float,
) -> np.ndarray:
    cv2 = _cv2()
    page_target = _page_target_corners_px(manifest, dpi)
    transform = cv2.getPerspectiveTransform(page_target, page_quad.astype(np.float32))
    expected = _expected_fiducials_px(manifest, dpi).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(expected, transform).reshape(4, 2).astype(np.float32)


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
    cx, cy = mm_to_px(x_mm, y_mm, dpi)
    half_w = max(1, int(round(width_mm * px_per_mm(dpi) * shrink / 2)))
    half_h = max(1, int(round(height_mm * px_per_mm(dpi) * shrink / 2)))
    x0, x1 = max(0, cx - half_w), min(gray.shape[1], cx + half_w + 1)
    y0, y1 = max(0, cy - half_h), min(gray.shape[0], cy + half_h + 1)
    if x0 >= x1 or y0 >= y1:
        return 0.0
    roi = gray[y0:y1, x0:x1]
    return float((roi < threshold).sum()) / float(roi.size)


def _orientation_score(gray: np.ndarray, manifest: dict, dpi: float) -> float:
    marker = next(m for m in manifest["orientation_marker"] if m.get("page", 1) == 1)
    return _rect_dark_fraction(
        gray,
        marker["x_mm"],
        marker["y_mm"],
        marker["size_mm"],
        marker["size_mm"],
        dpi,
        shrink=0.72,
        threshold=150,
    )


def _warp_with_best_orientation(gray: np.ndarray, source_points: np.ndarray, manifest: dict, dpi: float) -> tuple[np.ndarray, float]:
    cv2 = _cv2()
    width, height = canonical_size_px(manifest, dpi)
    target_points = _expected_fiducials_px(manifest, dpi)
    best_image: np.ndarray | None = None
    best_score = -1.0

    for shift in range(4):
        rotated_source = np.roll(source_points, shift, axis=0).astype(np.float32)
        transform = cv2.getPerspectiveTransform(rotated_source, target_points)
        warped = cv2.warpPerspective(
            gray,
            transform,
            (width, height),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=255,
        )
        score = _orientation_score(warped, manifest, dpi)
        if score > best_score:
            best_score = score
            best_image = warped

    if best_image is None or best_score < 0.35:
        raise ScanError(
            "orientation marker is unreadable after alignment; rescan the full page with all corner markers visible"
        )
    return best_image, best_score


def _page_mark_templates(manifest: dict) -> list[dict]:
    by_index: dict[int, dict] = {}
    for mark in manifest["page_marks"]:
        by_index.setdefault(int(mark["index"]), mark)
    return [by_index[index] for index in sorted(by_index)]


def detect_page_index(gray: np.ndarray, manifest: dict, dpi: float) -> tuple[int, float, dict[int, float]]:
    scores = {
        int(mark["index"]): _rect_dark_fraction(
            gray,
            mark["x_mm"],
            mark["y_mm"],
            mark["width_mm"],
            mark["height_mm"],
            dpi,
            shrink=0.52,
            threshold=150,
        )
        for mark in _page_mark_templates(manifest)
    }
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_index, top_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    filled = [index for index, score in scores.items() if score >= 0.45]
    if len(filled) != 1:
        raise ScanError(
            "page-index bars are ambiguous "
            f"(scores: {', '.join(f'{i}={s:.2f}' for i, s in sorted(scores.items()))}); "
            "rescan or inspect manually"
        )
    return top_index, max(0.0, top_score - runner_up), scores


def align_scan_page(gray: np.ndarray, manifest: dict, dpi: float, source_index: int = 1) -> AlignedPage:
    gray = _as_gray_array(gray)
    errors: list[str] = []
    best_page: AlignedPage | None = None
    best_quality = (-1.0, -1.0)
    for label, candidate_image, source_confidence, marker_estimates in _alignment_sources(gray, manifest, dpi):
        try:
            source_points, marker_confidence = _select_fiducials(
                candidate_image,
                manifest,
                allow_inferred=True,
                marker_estimates=marker_estimates,
            )
            canonical, orientation_confidence = _warp_with_best_orientation(
                candidate_image,
                source_points,
                manifest,
                dpi,
            )
            page_index, page_confidence, _scores = detect_page_index(canonical, manifest, dpi)
            confidence = min(source_confidence, marker_confidence, orientation_confidence)
            aligned_page = AlignedPage(
                page_index=page_index,
                source_index=source_index,
                image=canonical,
                alignment_confidence=confidence,
                page_mark_confidence=page_confidence,
            )
            quality = (confidence, page_confidence)
            if quality > best_quality:
                best_quality = quality
                best_page = aligned_page
        except ScanError as exc:
            errors.append(f"{label}: {exc}")

    if best_page is not None:
        return best_page
    raise _alignment_error(errors)


def align_scan_pages(
    raw_pages: list[np.ndarray],
    manifest: dict,
    dpi: float,
    allow_partial: bool = False,
) -> dict[int, AlignedPage]:
    aligned: dict[int, AlignedPage] = {}
    for source_index, raw in enumerate(raw_pages, start=1):
        page = align_scan_page(raw, manifest, dpi, source_index=source_index)
        if page.page_index in aligned:
            raise ScanError(
                f"duplicate page index {page.page_index} detected in scan pages "
                f"{aligned[page.page_index].source_index} and {source_index}"
            )
        aligned[page.page_index] = page

    expected = set(range(1, manifest["num_pages"] + 1))
    missing = expected - set(aligned)
    if missing and not allow_partial:
        raise ScanError(f"scan is missing page(s): {', '.join(str(p) for p in sorted(missing))}")
    return aligned
