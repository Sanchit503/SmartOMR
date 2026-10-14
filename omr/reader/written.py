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
from omr.reader.enhancement import enhance_faint_ink


WRITTEN_CROP_LAYOUT_VERSION = 3


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


def _page_regions(manifest: dict, page: int) -> list[dict]:
    """Read occupied regions from the sheet's own geometry."""
    regions = []
    for kind in ("written_block", "mcq_block", "numerical_block", "write_in_fields"):
        for item in manifest.get(kind, []):
            if item.get("page", 1) != page:
                continue
            if kind in {"written_block", "write_in_fields"}:
                regions.append(item)
            elif kind == "mcq_block":
                radius = manifest["bubble_radius_mm"]
                width = (len(item["options"]) - 1) * manifest["mcq_option_pitch_mm"]
                regions.append(dict(item, x_mm=item["x_mm"]-radius, y_mm=item["y_mm"]-radius,
                                    width_mm=width+2*radius, height_mm=2*radius))
            else:
                radius = manifest["bubble_radius_mm"]
                width = (item["positions"] - 1) * item["position_pitch_mm"]
                # Numerical rows cover every decimal digit, 0 through 9.
                height = (len("0123456789") - 1) * item["digit_pitch_mm"]
                if item.get("orientation") == "horizontal":
                    width, height = height, width
                regions.append(dict(item, x_mm=item["x_mm"]-radius, y_mm=item["y_mm"]-radius,
                                    width_mm=width+2*radius, height_mm=height+2*radius))
    return regions


def _heading_top_mm(entry: dict) -> float:
    if 'heading_top_mm' in entry:
        return entry['heading_top_mm']
    # Older manifests omit heading bounds; reserve one ruled-line pitch above the box.
    if entry.get('lines') and entry.get('height_mm'):
        return entry['y_mm'] - entry['height_mm'] / entry['lines']
    return entry['y_mm']


def _context_box(entry: dict, manifest: dict, padding_mm: float = 0.0) -> dict:
    """Allocate surrounding whitespace from page and neighbouring region bounds.

    The legacy padding argument is retained for callers; crop geometry comes
    entirely from the manifest. New manifests include the printed heading.
    """
    x0, y0 = entry["x_mm"], entry["y_mm"]
    x1, y1 = x0 + entry["width_mm"], y0 + entry["height_mm"]
    page = manifest["page"]
    safe_margin = manifest.get("printer_safe_margin_mm", 0)
    left, top = safe_margin, safe_margin
    right, bottom = page["width_mm"] - safe_margin, page["height_mm"] - safe_margin
    heading_top = _heading_top_mm(entry)
    for other in _page_regions(manifest, entry.get("page", 1)):
        if other is entry:
            continue
        ox0, oy0 = other["x_mm"], other["y_mm"]
        ox1, oy1 = ox0 + other["width_mm"], oy0 + other["height_mm"]
        if ox0 < x1 and ox1 > x0:
            if oy1 <= y0:
                top = max(top, (oy1 + heading_top) / 2)
            elif oy0 >= y1:
                # Keep more below-answer context, reserving the last quarter of
                # the manifest gap before the next region's heading.
                gap = max(0, _heading_top_mm(other) - y1)
                bottom = min(bottom, y1 + gap * 3 / 4)
        if oy0 < y1 and oy1 > y0:
            if ox1 <= x0:
                left = max(left, (ox1 + x0) / 2)
            elif ox0 >= x1:
                right = min(right, (x1 + ox0) / 2)
    return dict(entry, x_mm=min(left, x0), y_mm=min(top, heading_top),
                width_mm=max(right, x1)-min(left, x0),
                height_mm=max(bottom, y1)-min(top, heading_top))


def _header_bottom_mm(manifest: dict, page: int) -> float:
    question_tops = [_heading_top_mm(item)
                     for item in _page_regions(manifest, page) if 'q_no' in item]
    if not question_tops:
        raise ValueError(f'No question geometry for written page {page}')
    first_question = min(question_tops)
    identity_bottoms = [item['y_mm'] + item['height_mm']
                        for item in manifest.get('write_in_fields', []) if item.get('page', 1) == page]
    radius = manifest.get('bubble_radius_mm', 0)
    block = manifest.get('roll_number_block', {})
    if block.get('page', 1) == page:
        for grid in block.values():
            if isinstance(grid, dict) and 'row_pitch_mm' in grid and 'y_mm' in grid:
                identity_bottoms.append(grid['y_mm'] + (len('0123456789') - 1) * grid['row_pitch_mm'] + radius)
        identity_bottoms.extend(choice['y_mm'] + radius for choice in block.get('program_selector', {}).values())
    identity_bottoms.extend(choice['y_mm'] + choice.get('radius_mm', radius)
                            for choice in manifest.get('continuation_program_choices', [])
                            if choice.get('page', 1) == page)
    if not identity_bottoms:
        return first_question
    last_identity = max(identity_bottoms)
    if last_identity >= first_question:
        raise ValueError(f'Identity and question geometry overlap on page {page}')
    # Share the gap, then keep a bubble-radius safety margin away from body titles.
    return last_identity + max(0, (first_question - last_identity) / 2 - radius)


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
        header_path = output_dir / f"page_{page}_header.png"
        if page not in {crop.page for crop in crops}:
            header_height = min(image.shape[0], round(_header_bottom_mm(manifest, page) * px_per_mm(dpi)))
            Image.fromarray(image[:header_height], mode="L").save(header_path)
        context = _context_box(entry, manifest, padding_mm)
        context["x_mm"] = 0
        context["width_mm"] = image.shape[1] / px_per_mm(dpi)
        x0, y0, x1, y1 = _crop_box_px(context, dpi)
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
            tx0, ty0, tx1, ty1 = _crop_box_px(context, dpi)
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
                x_mm=x0 / px_per_mm(dpi),
                y_mm=y0 / px_per_mm(dpi),
                width_mm=(x1 - x0) / px_per_mm(dpi),
                height_mm=(y1 - y0) / px_per_mm(dpi),
                ocr_crop_path=str(ocr_crop_path),
                page_header_path=str(header_path),
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
    # The enhanced view is used only to find faint strokes. The saved crop is
    # still based on the original scan, keeping the professor-visible evidence
    # faithful to the submitted sheet.
    normalized = enhance_faint_ink(_normalize_background(gray))
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
