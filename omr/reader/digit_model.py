"""Local digit model support for handwritten roll-number cells.

The continuation pages do not need general handwriting OCR. The roll number
is written into fixed boxes, so the reliable path is:

1. crop each box from the canonical page,
2. remove field borders and lighting variation,
3. normalize the remaining glyph into a stable 28x28 feature,
4. classify the isolated digit with a small local model.

This module intentionally uses a local KNN model file instead of a hosted OCR
API. A stronger CNN/Transformer recognizer can be added behind the same
interface later without changing the batch workflow.
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
import numpy as np
from PIL import Image, ImageOps


MODEL_VERSION = 1
DIGIT_CANVAS_SIZE = 28
DIGIT_INNER_SIZE = 20
DEFAULT_K = 5
MIN_DIGIT_FOREGROUND_FRACTION = 0.006
MAX_DIGIT_FOREGROUND_FRACTION = 0.70


@dataclass(frozen=True)
class DigitFeature:
    feature: np.ndarray
    normalized_image: np.ndarray
    foreground_fraction: float
    is_blank: bool


@dataclass(frozen=True)
class DigitPrediction:
    digit: str | None
    confidence: float
    foreground_fraction: float
    votes: dict[str, int]
    distances: list[float]
    reason: str | None = None

    def to_json(self) -> dict[str, object]:
        payload = asdict(self)
        payload["distances"] = [float(value) for value in self.distances]
        return payload


class OpenCVDigitKnn:
    """KNN recognizer for isolated handwritten digits.

    OpenCV is used for the image processing path. The neighbour search itself
    is NumPy-based so the model also works with OpenCV builds that do not ship
    the optional `cv2.ml` module.
    """

    def __init__(self, model_path: str | Path, k: int = DEFAULT_K) -> None:
        self.model_path = Path(model_path)
        if not self.model_path.exists():
            raise FileNotFoundError(f"digit model not found: {self.model_path}")

        payload = np.load(self.model_path, allow_pickle=False)
        samples = np.asarray(payload["samples"], dtype=np.float32)
        labels = np.asarray(payload["labels"], dtype=np.float32).reshape(-1, 1)
        if samples.ndim != 2 or labels.ndim != 2 or samples.shape[0] != labels.shape[0]:
            raise ValueError(f"invalid digit model shape in {self.model_path}")
        if samples.shape[1] != DIGIT_CANVAS_SIZE * DIGIT_CANVAS_SIZE:
            raise ValueError(
                f"digit model feature size {samples.shape[1]} does not match "
                f"{DIGIT_CANVAS_SIZE * DIGIT_CANVAS_SIZE}"
            )
        if labels.shape[0] < 1:
            raise ValueError(f"digit model contains no samples: {self.model_path}")

        self.k = max(1, min(int(k), labels.shape[0]))
        self.samples = samples
        self.labels = labels.astype(np.int32).reshape(-1)

    def predict(self, image: Image.Image | np.ndarray) -> DigitPrediction:
        digit_feature = extract_digit_feature(image)
        if digit_feature.is_blank:
            return DigitPrediction(
                digit=None,
                confidence=0.0,
                foreground_fraction=digit_feature.foreground_fraction,
                votes={},
                distances=[],
                reason="blank_or_too_little_ink",
            )

        sample = digit_feature.feature.astype(np.float32)
        distances_all = np.sum((self.samples - sample.reshape(1, -1)) ** 2, axis=1)
        neighbour_indices = np.argsort(distances_all)[: self.k]
        neighbour_digits = [str(int(self.labels[index])) for index in neighbour_indices]
        distance_values = [float(distances_all[index]) for index in neighbour_indices]
        votes: dict[str, int] = {}
        for digit in neighbour_digits:
            votes[digit] = votes.get(digit, 0) + 1
        if not votes:
            return DigitPrediction(
                digit=None,
                confidence=0.0,
                foreground_fraction=digit_feature.foreground_fraction,
                votes={},
                distances=distance_values,
                reason="model_returned_no_neighbours",
            )

        best_digit = max(votes, key=lambda item: (votes[item], -neighbour_digits.index(item)))
        vote_fraction = votes[best_digit] / max(1, len(neighbour_digits))
        best_distances = [
            distance
            for digit, distance in zip(neighbour_digits, distance_values, strict=False)
            if digit == best_digit
        ]
        other_distances = [
            distance
            for digit, distance in zip(neighbour_digits, distance_values, strict=False)
            if digit != best_digit
        ]
        best_distance = float(np.median(best_distances)) if best_distances else 0.0
        if other_distances:
            other_distance = float(np.median(other_distances))
            margin = (other_distance - best_distance) / (other_distance + best_distance + 1e-6)
            margin = max(0.0, min(1.0, margin))
        else:
            margin = 1.0

        density_ok = (
            MIN_DIGIT_FOREGROUND_FRACTION
            <= digit_feature.foreground_fraction
            <= MAX_DIGIT_FOREGROUND_FRACTION
        )
        confidence = 0.18 + 0.62 * vote_fraction + 0.20 * margin
        if not density_ok:
            confidence = min(confidence, 0.58)
        return DigitPrediction(
            digit=best_digit,
            confidence=max(0.0, min(0.99, confidence)),
            foreground_fraction=digit_feature.foreground_fraction,
            votes=votes,
            distances=distance_values,
            reason=None if density_ok else "unusual_foreground_fraction",
        )


def extract_digit_feature(image: Image.Image | np.ndarray) -> DigitFeature:
    """Convert a digit cell crop into a 28x28 float feature vector."""

    cv2 = _cv2()
    gray = _as_gray_array(image)
    gray = _trim_outer_cell_margin(gray)
    if gray.size == 0:
        blank = np.zeros((DIGIT_CANVAS_SIZE, DIGIT_CANVAS_SIZE), dtype=np.uint8)
        return DigitFeature(blank.reshape(-1).astype(np.float32), blank, 0.0, True)

    scale = 4 if max(gray.shape[:2]) < 96 else 2
    resized = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    normalized = _normalize_illumination(resized)
    ink = _threshold_digit_ink(normalized)
    ink = _remove_cell_rules(ink)
    ink = _clean_small_components(ink)

    foreground_fraction = float(np.count_nonzero(ink) / max(1, ink.size))
    if foreground_fraction < MIN_DIGIT_FOREGROUND_FRACTION:
        blank = np.zeros((DIGIT_CANVAS_SIZE, DIGIT_CANVAS_SIZE), dtype=np.uint8)
        return DigitFeature(blank.reshape(-1).astype(np.float32), blank, foreground_fraction, True)

    ys, xs = np.where(ink > 0)
    glyph = ink[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
    canvas = _fit_glyph_to_canvas(glyph)
    canvas = _deskew(canvas)
    feature = (canvas.reshape(-1).astype(np.float32) / 255.0)
    return DigitFeature(feature, canvas, foreground_fraction, False)


def train_digit_model(
    labels_csv: str | Path,
    model_path: str | Path,
    *,
    root_dir: str | Path | None = None,
) -> dict[str, object]:
    """Train and save a small local KNN digit model from labelled cell crops.

    The CSV must contain `image_path,label`, where label is one digit from 0-9.
    Paths may be absolute, relative to `root_dir`, or relative to the CSV file.
    """

    labels_path = Path(labels_csv)
    base_dir = Path(root_dir) if root_dir is not None else labels_path.parent
    samples: list[np.ndarray] = []
    labels: list[int] = []
    skipped: list[dict[str, str]] = []

    with labels_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if "image_path" not in (reader.fieldnames or []) or "label" not in (reader.fieldnames or []):
            raise ValueError("digit label CSV must contain image_path,label columns")
        for row in reader:
            label = str(row.get("label", "")).strip()
            if not label.isdigit() or len(label) != 1:
                skipped.append({"image_path": str(row.get("image_path", "")), "reason": "invalid_label"})
                continue
            image_path = _resolve_image_path(str(row.get("image_path", "")), labels_path.parent, base_dir)
            try:
                image = Image.open(image_path).convert("L")
                feature = extract_digit_feature(image)
            except Exception as exc:
                skipped.append({"image_path": str(image_path), "reason": f"{type(exc).__name__}: {exc}"})
                continue
            if feature.is_blank:
                skipped.append({"image_path": str(image_path), "reason": "blank_or_too_little_ink"})
                continue
            samples.append(feature.feature)
            labels.append(int(label))

    if len(samples) < 10:
        raise ValueError(
            f"need at least 10 labelled digit crops to train a useful model, got {len(samples)}"
        )

    model_path = Path(model_path)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    samples_array = np.vstack(samples).astype(np.float32)
    labels_array = np.asarray(labels, dtype=np.int32)
    np.savez_compressed(
        model_path,
        version=np.asarray([MODEL_VERSION], dtype=np.int32),
        samples=samples_array,
        labels=labels_array,
        canvas_size=np.asarray([DIGIT_CANVAS_SIZE], dtype=np.int32),
        inner_size=np.asarray([DIGIT_INNER_SIZE], dtype=np.int32),
    )
    return {
        "model_path": str(model_path),
        "samples": int(samples_array.shape[0]),
        "skipped": skipped,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smartomr-train-digits",
        description="Train a local KNN digit recognizer from labelled roll-number cell crops.",
    )
    parser.add_argument("--labels", required=True, type=Path, help="CSV with image_path,label columns")
    parser.add_argument("--model", required=True, type=Path, help="Output .npz model path")
    parser.add_argument(
        "--root-dir",
        type=Path,
        default=None,
        help="Optional base directory for relative image paths in the CSV",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = train_digit_model(args.labels, args.model, root_dir=args.root_dir)
    print(json.dumps(result, indent=2))
    return 0


def _as_gray_array(image: Image.Image | np.ndarray) -> np.ndarray:
    if isinstance(image, Image.Image):
        gray = ImageOps.grayscale(image)
        return np.asarray(gray).astype(np.uint8, copy=False)
    array = np.asarray(image)
    if array.ndim == 3:
        cv2 = _cv2()
        array = cv2.cvtColor(array.astype(np.uint8, copy=False), cv2.COLOR_BGR2GRAY)
    return array.astype(np.uint8, copy=False)


def _trim_outer_cell_margin(gray: np.ndarray) -> np.ndarray:
    h, w = gray.shape[:2]
    if h < 8 or w < 8:
        return gray
    x_pad = max(1, round(w * 0.08))
    y_pad = max(1, round(h * 0.08))
    return gray[y_pad : h - y_pad, x_pad : w - x_pad]


def _normalize_illumination(gray: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    source = gray.astype(np.uint8, copy=False)
    kernel = max(15, (min(source.shape[:2]) // 2) | 1)
    background = cv2.GaussianBlur(source, (kernel, kernel), 0)
    flattened = cv2.divide(source, background, scale=255)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
    return clahe.apply(flattened)


def _threshold_digit_ink(gray: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    adaptive = cv2.adaptiveThreshold(
        blurred,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        31,
        9,
    )
    _threshold, otsu = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    combined = cv2.bitwise_or(adaptive, otsu)
    return combined


def _remove_cell_rules(ink: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    h, w = ink.shape[:2]
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(12, w // 2), 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(12, h // 2)))
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, horizontal_kernel)
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, vertical_kernel)

    edge_mask = np.zeros_like(ink)
    edge = max(3, round(min(h, w) * 0.18))
    edge_mask[:edge, :] = 255
    edge_mask[h - edge :, :] = 255
    edge_mask[:, :edge] = 255
    edge_mask[:, w - edge :] = 255
    removable = cv2.bitwise_and(cv2.bitwise_or(horizontal, vertical), edge_mask)
    cleaned = cv2.bitwise_and(ink, cv2.bitwise_not(removable))
    return cleaned


def _clean_small_components(ink: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(ink, connectivity=8)
    if count <= 1:
        return ink
    h, w = ink.shape[:2]
    min_area = max(6, round(ink.size * 0.0015))
    cleaned = np.zeros_like(ink)
    for label in range(1, count):
        x, y, component_w, component_h, area = stats[label]
        if area < min_area:
            continue
        if component_w > 0.82 * w and component_h <= max(3, h * 0.08):
            continue
        if component_h > 0.82 * h and component_w <= max(3, w * 0.08):
            continue
        cleaned[labels == label] = 255

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2, 2))
    return cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel)


def _fit_glyph_to_canvas(glyph: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    h, w = glyph.shape[:2]
    if h == 0 or w == 0:
        return np.zeros((DIGIT_CANVAS_SIZE, DIGIT_CANVAS_SIZE), dtype=np.uint8)
    scale = DIGIT_INNER_SIZE / max(h, w)
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    resized = cv2.resize(glyph, (new_w, new_h), interpolation=interpolation)
    canvas = np.zeros((DIGIT_CANVAS_SIZE, DIGIT_CANVAS_SIZE), dtype=np.uint8)
    x0 = (DIGIT_CANVAS_SIZE - new_w) // 2
    y0 = (DIGIT_CANVAS_SIZE - new_h) // 2
    canvas[y0 : y0 + new_h, x0 : x0 + new_w] = resized
    return canvas


def _deskew(image: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    moments = cv2.moments(image)
    if abs(moments.get("mu02", 0.0)) < 1e-2:
        return image
    skew = moments["mu11"] / moments["mu02"]
    matrix = np.float32([[1, skew, -0.5 * image.shape[0] * skew], [0, 1, 0]])
    return cv2.warpAffine(
        image,
        matrix,
        (image.shape[1], image.shape[0]),
        flags=cv2.WARP_INVERSE_MAP | cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )


def _resolve_image_path(value: str, csv_dir: Path, root_dir: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    root_candidate = root_dir / path
    if root_candidate.exists():
        return root_candidate
    return csv_dir / path


def _cv2():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("opencv-python-headless is required for the local digit model") from exc
    return cv2


if __name__ == "__main__":
    raise SystemExit(main())
