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

        crop_path = output_dir / f"Q{entry['q_no']}.png"
        Image.fromarray(image[y0:y1, x0:x1], mode="L").save(crop_path)
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
            )
        )

    return crops
