"""Written-answer cropping from canonical page images.

The reader does not grade handwritten answers. It only crops the regions the
manifest declares, saves them as images, and records enough metadata for the
Phase 3 LLM/manual-grading step to consume safely.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from omr.contracts.geometry import mm_to_px, px_per_mm
from omr.models import WrittenCrop


def _gray_array(image: object) -> np.ndarray:
    gray = np.asarray(image)
    if gray.ndim == 3:
        # OpenCV style BGR/RGB does not matter for grayscale conversion here:
        # channel mean is enough for a saved debug crop.
        gray = gray.mean(axis=2)
    return gray.astype(np.uint8, copy=False)


def isolate_student_ink(image: object, template_image: object | None = None) -> np.ndarray:
    """Return a white-background image containing mostly student-added ink."""

    cv2 = _cv2()
    gray = _gray_array(image)
    if template_image is None:
        return _remove_form_rules(gray)

    template = _gray_array(template_image)
    if template.shape != gray.shape:
        template = cv2.resize(
            template,
            (gray.shape[1], gray.shape[0]),
            interpolation=cv2.INTER_AREA if template.size > gray.size else cv2.INTER_CUBIC,
        )

    scan_norm = _normalize_background(gray)
    template_norm = _normalize_background(template)
    diff = np.clip(template_norm.astype(np.int16) - scan_norm.astype(np.int16), 0, 255).astype(np.uint8)

    scan_dark = scan_norm < 185
    template_dark = template_norm < 215
    template_dark = cv2.dilate(
        template_dark.astype(np.uint8) * 255,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        iterations=1,
    ) > 0
    changed = diff > 22
    unexplained_dark = scan_dark & ~template_dark
    ink = ((changed | unexplained_dark).astype(np.uint8)) * 255
    ink = _remove_rule_residue(ink)
    ink = _clean_student_ink_mask(ink)

    output = np.full_like(gray, 255)
    output[ink > 0] = np.minimum(gray[ink > 0], 35)
    return output


def _crop_box_px(entry: dict, dpi: float, padding_mm: float = 0.0) -> tuple[int, int, int, int]:
    scale = px_per_mm(dpi)
    x0, y0 = mm_to_px(entry["x_mm"] - padding_mm, entry["y_mm"] - padding_mm, dpi)
    width_px = round((entry["width_mm"] + 2 * padding_mm) * scale)
    height_px = round((entry["height_mm"] + 2 * padding_mm) * scale)
    return x0, y0, x0 + width_px, y0 + height_px


def crop_written_responses(
    images_by_page: dict[int, object],
    manifest: dict,
    output_dir: str | Path,
    dpi: float,
    padding_mm: float = 0.0,
    template_images_by_page: dict[int, object] | None = None,
) -> list[WrittenCrop]:
    """Crop every written answer box declared by `manifest`.

    `images_by_page` must contain canonical grayscale page images keyed by
    manifest page number. The crops are saved as `Q<q_no>.png` in `output_dir`.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    crops: list[WrittenCrop] = []
    for entry in sorted(manifest["written_block"], key=lambda item: (item.get("page", 1), item["q_no"])):
        page = entry.get("page", 1)
        if page not in images_by_page:
            raise KeyError(f"no canonical image provided for page {page} (needed for written Q{entry['q_no']})")

        image = _gray_array(images_by_page[page])
        x0, y0, x1, y1 = _crop_box_px(entry, dpi, padding_mm=padding_mm)
        x0 = max(0, min(image.shape[1], x0))
        x1 = max(0, min(image.shape[1], x1))
        y0 = max(0, min(image.shape[0], y0))
        y1 = max(0, min(image.shape[0], y1))
        if x0 >= x1 or y0 >= y1:
            raise ValueError(f"written Q{entry['q_no']} crop is outside page {page}")

        raw_crop = image[y0:y1, x0:x1]
        crop_path = output_dir / f"Q{entry['q_no']}.png"
        Image.fromarray(raw_crop, mode="L").save(crop_path)

        ocr_crop_path = output_dir / f"Q{entry['q_no']}_ink.png"
        if template_images_by_page and page in template_images_by_page:
            template = _gray_array(template_images_by_page[page])
            tx0, ty0, tx1, ty1 = _crop_box_px(entry, dpi, padding_mm=padding_mm)
            tx0 = max(0, min(template.shape[1], tx0))
            tx1 = max(0, min(template.shape[1], tx1))
            ty0 = max(0, min(template.shape[0], ty0))
            ty1 = max(0, min(template.shape[0], ty1))
            if tx0 < tx1 and ty0 < ty1:
                template_crop = template[ty0:ty1, tx0:tx1]
                ocr_crop = isolate_student_ink(raw_crop, template_crop)
            else:
                ocr_crop = isolate_student_ink(raw_crop)
        else:
            ocr_crop = isolate_student_ink(raw_crop)
        Image.fromarray(ocr_crop, mode="L").save(ocr_crop_path)

        crops.append(
            WrittenCrop(
                q_no=entry["q_no"],
                page=page,
                crop_path=str(crop_path),
                max_marks=entry["max_marks"],
                lines=entry["lines"],
                x_mm=entry["x_mm"],
                y_mm=entry["y_mm"],
                width_mm=entry["width_mm"],
                height_mm=entry["height_mm"],
                ocr_crop_path=str(ocr_crop_path),
            )
        )

    return crops


def _normalize_background(gray: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    source = gray.astype(np.uint8, copy=False)
    kernel = max(17, (min(source.shape[:2]) // 2) | 1)
    background = cv2.GaussianBlur(source, (kernel, kernel), 0)
    flattened = cv2.divide(source, background, scale=255)
    return cv2.normalize(flattened, None, 0, 255, cv2.NORM_MINMAX)


def _remove_form_rules(gray: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    normalized = _normalize_background(gray)
    blurred = cv2.GaussianBlur(normalized, (3, 3), 0)
    _threshold, ink = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    h, w = ink.shape[:2]
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(24, w // 5), 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(12, h // 2)))
    rules = cv2.morphologyEx(ink, cv2.MORPH_OPEN, horizontal_kernel)
    rules = cv2.bitwise_or(rules, cv2.morphologyEx(ink, cv2.MORPH_OPEN, vertical_kernel))
    cleaned = cv2.bitwise_and(ink, cv2.bitwise_not(cv2.dilate(rules, np.ones((2, 2), dtype=np.uint8))))
    cleaned = _remove_rule_residue(cleaned)
    cleaned = _clean_student_ink_mask(cleaned)
    output = np.full_like(gray, 255)
    output[cleaned > 0] = 0
    return output


def _remove_rule_residue(ink: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    h, w = ink.shape[:2]
    if h < 8 or w < 8:
        return ink.astype(np.uint8, copy=False)
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(40, w // 9), 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(12, h // 2)))
    horizontal = cv2.morphologyEx(ink.astype(np.uint8, copy=False), cv2.MORPH_OPEN, horizontal_kernel)
    vertical = cv2.morphologyEx(ink.astype(np.uint8, copy=False), cv2.MORPH_OPEN, vertical_kernel)
    rules = cv2.bitwise_or(horizontal, vertical)
    rules = cv2.dilate(rules, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), iterations=1)
    return cv2.bitwise_and(ink.astype(np.uint8, copy=False), cv2.bitwise_not(rules))


def _clean_student_ink_mask(ink: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    cleaned = np.zeros_like(ink, dtype=np.uint8)
    h, w = ink.shape[:2]
    min_area = max(8, round(ink.size * 0.00018))
    for label in range(1, count):
        x, y, component_w, component_h, area = stats[label]
        if area < min_area:
            continue
        if component_w > 0.92 * w and component_h <= max(4, h * 0.10):
            continue
        if component_h > 0.88 * h and component_w <= max(4, w * 0.02):
            continue
        cleaned[labels == label] = 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2))
    return cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel)


def _cv2():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("opencv-python-headless is required for written-answer preprocessing") from exc
    return cv2
