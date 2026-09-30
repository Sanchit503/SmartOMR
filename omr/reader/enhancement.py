"""Conservative scan enhancement helpers for weak pencil and handwriting ink.

The original scan remains the source of truth. These helpers produce a second
diagnostic view for retrying weak reads; callers must record when it was used.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageOps


def as_gray(image: object) -> np.ndarray:
    gray = np.asarray(image)
    if gray.ndim == 3:
        gray = gray.mean(axis=2)
    return gray.astype(np.uint8, copy=False)


def enhance_faint_ink(image: object) -> np.ndarray:
    """Flatten illumination and gently improve local pencil contrast.

    It intentionally does not binarize: light pencil information is lost when
    a global black/white threshold is applied too early.
    """
    gray = as_gray(image)
    try:
        import cv2

        side = max(31, (min(gray.shape[:2]) // 12) | 1)
        background = cv2.GaussianBlur(gray, (side, side), 0)
        flattened = cv2.divide(gray, background, scale=255)
        return cv2.createCLAHE(clipLimit=1.5, tileGridSize=(16, 16)).apply(flattened)
    except ImportError:
        return np.asarray(ImageOps.autocontrast(Image.fromarray(gray), cutoff=1), dtype=np.uint8)


def adaptive_ink_mask(image: object) -> np.ndarray:
    """Diagnostic adaptive mask; it must never be the sole scoring signal."""
    gray = enhance_faint_ink(image)
    try:
        import cv2

        return cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 7
        )
    except ImportError:
        return ((gray < int(np.percentile(gray, 35))).astype(np.uint8)) * 255
