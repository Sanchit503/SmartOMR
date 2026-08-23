from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

import numpy as np

from omr.contracts.geometry import canonical_size_px, mm_to_px, px_per_mm

from .models import AlignedPage


class ScanError(RuntimeError):
    """The scanned page cannot be read safely enough for auto-evaluation."""


def _cv2():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError(
            "OpenCV is required for prototype scan evaluation. Install dependencies with "
            "`pip install -r requirements.txt` from the SmartOMR folder."
        ) from exc
    return cv2


@dataclass(frozen=True)
class _SquareCandidate:
    center: np.ndarray
    side: float
    area: float
    ink_fraction: float
    bbox: tuple[int, int, int, int]


CORNER_ORDER = ("TL", "TR", "BR", "BL")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


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


def _order_points(points: np.ndarray) -> np.ndarray:
    points = np.asarray(points, dtype=np.float32)
    s = points.sum(axis=1)
    diff = points[:, 0] - points[:, 1]
    ordered = np.zeros((4, 2), dtype=np.float32)
    ordered[0] = points[np.argmin(s)]
    ordered[2] = points[np.argmax(s)]
    ordered[1] = points[np.argmax(diff)]
    ordered[3] = points[np.argmin(diff)]
    return ordered


def _select_fiducials(gray: np.ndarray) -> tuple[np.ndarray, float]:
    cv2 = _cv2()
    candidates = sorted(_find_dark_square_candidates(gray), key=lambda c: c.area, reverse=True)[:20]
    if len(candidates) < 4:
        raise ScanError(
            f"found only {len(candidates)} likely filled square marker(s); "
            "the scan may be cropped, half-page, too faint, or badly blurred"
        )

    best_points: np.ndarray | None = None
    best_score = -1.0
    best_size_spread = 1.0
    for combo in combinations(candidates, 4):
        sides = np.array([c.side for c in combo], dtype=np.float32)
        if sides.max() / sides.min() > 1.45:
            continue
        centers = np.array([c.center for c in combo], dtype=np.float32)
        ordered = _order_points(centers)
        quad_area = abs(float(cv2.contourArea(ordered)))
        if quad_area <= 0:
            continue
        polygon = ordered.reshape(-1, 1, 2)
        if not cv2.isContourConvex(polygon.astype(np.float32)):
            continue
        size_spread = float(sides.std() / max(sides.mean(), 1.0))
        ink_score = min(c.ink_fraction for c in combo)
        score = quad_area * ink_score * (1.0 - min(size_spread, 0.9))
        if score > best_score:
            best_score = score
            best_points = ordered
            best_size_spread = size_spread

    if best_points is None:
        raise ScanError(
            "could not form four reliable corner markers; the scan may be cropped, "
            "half-page, or mixed with heavy dark marks"
        )
    return best_points, max(0.0, 1.0 - best_size_spread)


def _expected_fiducials_px(manifest: dict, dpi: float) -> np.ndarray:
    by_corner = {f["corner"]: f for f in manifest["fiducials"] if f.get("page", 1) == 1}
    return np.array([mm_to_px(by_corner[corner]["x_mm"], by_corner[corner]["y_mm"], dpi) for corner in CORNER_ORDER],
                    dtype=np.float32)


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
    source_points, marker_confidence = _select_fiducials(gray)
    canonical, orientation_confidence = _warp_with_best_orientation(gray, source_points, manifest, dpi)
    page_index, page_confidence, _scores = detect_page_index(canonical, manifest, dpi)
    confidence = min(marker_confidence, orientation_confidence)
    return AlignedPage(
        page_index=page_index,
        source_index=source_index,
        image=canonical,
        alignment_confidence=confidence,
        page_mark_confidence=page_confidence,
    )


def align_scan_pages(raw_pages: list[np.ndarray], manifest: dict, dpi: float) -> dict[int, AlignedPage]:
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
    if missing:
        raise ScanError(f"scan is missing page(s): {', '.join(str(p) for p in sorted(missing))}")
    return aligned
