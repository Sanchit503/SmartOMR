"""Offline OCR support for cropped handwritten written-answer regions.

This layer is intentionally optional. SmartOMR can always save answer crops;
OCR is an extra extraction step that produces text, confidence, and review
flags for downstream grading/review.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from PIL import Image, ImageOps

from omr.models import WrittenCrop


DEFAULT_TROCR_MODEL = "microsoft/trocr-base-handwritten"
LINE_TARGET_HEIGHT_PX = 72
MIN_LINE_INK_FRACTION = 0.0025


@dataclass(frozen=True)
class WrittenLineOcr:
    line_index: int
    crop_path: str
    text: str
    confidence: float | None
    provider: str
    review_flags: list[str]
    raw: object | None = None


@dataclass(frozen=True)
class WrittenAnswerOcr:
    q_no: int
    crop_path: str
    text: str
    confidence: str
    provider: str
    line_results: list[WrittenLineOcr]
    review_flags: list[str]

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class LineOcrResult:
    text: str
    confidence: float | None = None
    raw: object | None = None


class WrittenOcrBackend(Protocol):
    provider: str

    def read_line(self, line_path: Path) -> LineOcrResult:
        ...


class LocalTesseractWrittenOcr:
    """Offline Tesseract fallback for written-answer line crops."""

    provider = "local_tesseract"

    def __init__(self, tesseract_cmd: str | None = None) -> None:
        try:
            import pytesseract
        except ImportError as exc:
            raise RuntimeError(
                "pytesseract is required for local written OCR. Install dependencies, "
                "and install the Tesseract binary on the machine."
            ) from exc
        self.pytesseract = pytesseract
        command = tesseract_cmd or os.environ.get("SMARTOMR_TESSERACT_CMD")
        if command:
            self.pytesseract.pytesseract.tesseract_cmd = command
        try:
            self.version = str(self.pytesseract.get_tesseract_version())
        except Exception as exc:
            raise RuntimeError(
                "Tesseract OCR binary is not available. Install Tesseract or set SMARTOMR_TESSERACT_CMD."
            ) from exc

    def read_line(self, line_path: Path) -> LineOcrResult:
        image = Image.open(line_path).convert("L")
        config = "--oem 1 --psm 7"
        text = self.pytesseract.image_to_string(image, config=config).strip()
        confidences: list[float] = []
        try:
            data = self.pytesseract.image_to_data(
                image,
                config=config,
                output_type=self.pytesseract.Output.DICT,
            )
            for value in data.get("conf", []):
                try:
                    confidence = float(value)
                except (TypeError, ValueError):
                    continue
                if confidence >= 0:
                    confidences.append(confidence / 100.0)
        except Exception:
            pass
        return LineOcrResult(
            text=_clean_text(text),
            confidence=float(np.median(confidences)) if confidences else None,
            raw={"provider": self.provider, "version": self.version},
        )


class TrOCRWrittenOcr:
    """Offline Transformer OCR backend for handwritten answer lines."""

    provider = "trocr"

    def __init__(
        self,
        model_name: str = DEFAULT_TROCR_MODEL,
        *,
        device: str | None = None,
        local_files_only: bool = False,
        max_new_tokens: int = 96,
        num_beams: int = 4,
    ) -> None:
        try:
            import torch
            from transformers import TrOCRProcessor, VisionEncoderDecoderModel
        except ImportError as exc:
            raise RuntimeError(
                "TrOCR written OCR needs optional dependencies. Install with "
                "`python -m pip install .[htr]`, then run again."
            ) from exc

        self.torch = torch
        self.model_name = model_name
        self.processor = TrOCRProcessor.from_pretrained(model_name, local_files_only=local_files_only)
        self.model = VisionEncoderDecoderModel.from_pretrained(
            model_name,
            local_files_only=local_files_only,
        )
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.model.to(self.device)
        self.model.eval()
        self.max_new_tokens = max_new_tokens
        self.num_beams = num_beams

    def read_line(self, line_path: Path) -> LineOcrResult:
        image = Image.open(line_path).convert("RGB")
        inputs = self.processor(images=image, return_tensors="pt")
        pixel_values = inputs.pixel_values.to(self.device)
        with self.torch.no_grad():
            generated = self.model.generate(
                pixel_values,
                max_new_tokens=self.max_new_tokens,
                num_beams=self.num_beams,
                return_dict_in_generate=True,
                output_scores=True,
            )
        text = self.processor.batch_decode(generated.sequences, skip_special_tokens=True)[0]
        confidence = _generation_confidence(self.torch, getattr(generated, "scores", None))
        return LineOcrResult(
            text=_clean_text(text),
            confidence=confidence,
            raw={
                "provider": self.provider,
                "model": self.model_name,
                "device": self.device,
                "num_beams": self.num_beams,
            },
        )


def build_written_ocr_backend(
    provider: str | None = None,
    *,
    tesseract_cmd: str | None = None,
    model_name: str | None = None,
    device: str | None = None,
    local_files_only: bool = False,
) -> WrittenOcrBackend | None:
    selected = (provider or "none").strip().lower()
    if selected in {"", "none", "off", "disabled"}:
        return None
    if selected in {"tesseract", "local", "local-tesseract", "local_tesseract"}:
        return LocalTesseractWrittenOcr(tesseract_cmd=tesseract_cmd)
    if selected in {"trocr", "transformer", "htr"}:
        return TrOCRWrittenOcr(
            model_name=model_name or DEFAULT_TROCR_MODEL,
            device=device,
            local_files_only=local_files_only,
        )
    raise ValueError(f"unknown written-answer OCR provider {provider!r}")


def save_written_line_crops(
    crop: WrittenCrop,
    output_dir: str | Path,
) -> list[Path]:
    """Split one written-answer crop into OCR-friendly line images."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    gray = np.asarray(Image.open(crop.crop_path).convert("L"))
    gray = _trim_answer_box_border(gray)
    expected_lines = max(1, int(crop.lines))
    line_paths: list[Path] = []
    for index in range(expected_lines):
        y0, y1 = _line_band(gray.shape[0], expected_lines, index)
        line = gray[y0:y1, :]
        prepared = prepare_written_line_for_ocr(line)
        path = output_dir / f"Q{crop.q_no}_line_{index + 1}.png"
        Image.fromarray(prepared, mode="L").save(path)
        line_paths.append(path)
    return line_paths


def read_written_answer_texts(
    crops: list[WrittenCrop],
    output_dir: str | Path,
    backend: WrittenOcrBackend | None,
) -> dict[int, WrittenAnswerOcr]:
    """OCR written-answer crops, returning results keyed by question number."""

    if backend is None:
        return {}
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    answers: dict[int, WrittenAnswerOcr] = {}
    for crop in crops:
        line_dir = output_dir / f"Q{crop.q_no}_lines"
        line_paths = save_written_line_crops(crop, line_dir)
        line_results: list[WrittenLineOcr] = []
        review_flags: list[str] = []

        for index, line_path in enumerate(line_paths, start=1):
            line_flags: list[str] = []
            if _line_is_blank(line_path):
                line_flags.append("line appears blank after rule removal")
                line_result = WrittenLineOcr(
                    line_index=index,
                    crop_path=str(line_path),
                    text="",
                    confidence=0.0,
                    provider=backend.provider,
                    review_flags=line_flags,
                    raw={"reason": "blank_line"},
                )
            else:
                read = backend.read_line(line_path)
                if not read.text:
                    line_flags.append("OCR returned empty text for a non-blank line")
                if read.confidence is None:
                    line_flags.append("OCR did not report confidence")
                elif read.confidence < 0.60:
                    line_flags.append(f"low OCR confidence {read.confidence:.2f}")
                line_result = WrittenLineOcr(
                    line_index=index,
                    crop_path=str(line_path),
                    text=read.text,
                    confidence=read.confidence,
                    provider=backend.provider,
                    review_flags=line_flags,
                    raw=read.raw,
                )
            line_results.append(line_result)
            review_flags.extend(f"line {index}: {flag}" for flag in line_flags)

        text = "\n".join(result.text for result in line_results if result.text).strip()
        if not text:
            review_flags.append("no written text extracted")
        answers[crop.q_no] = WrittenAnswerOcr(
            q_no=crop.q_no,
            crop_path=crop.crop_path,
            text=text,
            confidence=_answer_confidence(line_results, review_flags),
            provider=backend.provider,
            line_results=line_results,
            review_flags=review_flags,
        )
    return answers


def prepare_written_line_for_ocr(line: np.ndarray) -> np.ndarray:
    """Return a cleaned grayscale line image while preserving handwriting."""

    cv2 = _cv2()
    gray = line.astype(np.uint8, copy=False)
    if gray.size == 0:
        return gray
    flattened = _normalize_illumination(gray)
    cleaned = _remove_rules_from_gray(flattened)
    cleaned = ImageOps.autocontrast(Image.fromarray(cleaned, mode="L"))
    cleaned_array = np.asarray(cleaned)
    h, w = cleaned_array.shape[:2]
    if h < LINE_TARGET_HEIGHT_PX:
        scale = LINE_TARGET_HEIGHT_PX / max(1, h)
        cleaned_array = cv2.resize(
            cleaned_array,
            (max(1, round(w * scale)), LINE_TARGET_HEIGHT_PX),
            interpolation=cv2.INTER_CUBIC,
        )
    return cv2.copyMakeBorder(cleaned_array, 10, 10, 18, 18, cv2.BORDER_CONSTANT, value=255)


def _line_band(height: int, lines: int, index: int) -> tuple[int, int]:
    top = round(index * height / lines)
    bottom = round((index + 1) * height / lines)
    pad = max(2, round((bottom - top) * 0.12))
    return max(0, top - pad), min(height, bottom + pad)


def _trim_answer_box_border(gray: np.ndarray) -> np.ndarray:
    h, w = gray.shape[:2]
    if h < 12 or w < 12:
        return gray
    trim_y = max(2, round(h * 0.04))
    trim_x = max(3, round(w * 0.015))
    return gray[trim_y : h - trim_y, trim_x : w - trim_x]


def _normalize_illumination(gray: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    source = gray.astype(np.uint8, copy=False)
    kernel = max(17, (min(source.shape[:2]) // 2) | 1)
    background = cv2.GaussianBlur(source, (kernel, kernel), 0)
    flattened = cv2.divide(source, background, scale=255)
    clahe = cv2.createCLAHE(clipLimit=1.8, tileGridSize=(8, 2))
    return clahe.apply(flattened)


def _remove_rules_from_gray(gray: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _threshold, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    h, w = binary.shape[:2]
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(24, w // 5), 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(10, h // 2)))
    rules = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel)
    rules = cv2.bitwise_or(rules, cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel))
    dilated_rules = cv2.dilate(rules, cv2.getStructuringElement(cv2.MORPH_RECT, (3, 2)), iterations=1)
    cleaned = gray.copy()
    cleaned[dilated_rules > 0] = 255
    return cleaned


def _line_is_blank(line_path: Path) -> bool:
    cv2 = _cv2()
    gray = np.asarray(Image.open(line_path).convert("L"))
    _threshold, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(ink, connectivity=8)
    cleaned = np.zeros_like(ink)
    min_area = max(8, round(ink.size * 0.00035))
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] >= min_area:
            cleaned[labels == label] = 255
    ink_fraction = float(np.count_nonzero(cleaned) / max(1, cleaned.size))
    return ink_fraction < MIN_LINE_INK_FRACTION


def _answer_confidence(line_results: list[WrittenLineOcr], review_flags: list[str]) -> str:
    values = [result.confidence for result in line_results if result.confidence is not None and result.text]
    if not values:
        return "low"
    value = float(np.median(values))
    if review_flags:
        return "medium" if value >= 0.82 else "low"
    if value >= 0.82:
        return "high"
    if value >= 0.60:
        return "medium"
    return "low"


def _clean_text(text: str) -> str:
    compact = re.sub(r"[ \t\r\f\v]+", " ", text)
    compact = re.sub(r"\n{3,}", "\n\n", compact)
    return compact.strip()


def _generation_confidence(torch, scores: object | None) -> float | None:
    if not scores:
        return None
    try:
        probabilities = []
        for token_scores in scores:
            probs = torch.softmax(token_scores, dim=-1)
            probabilities.append(float(torch.max(probs).detach().cpu()))
        if not probabilities:
            return None
        return max(0.0, min(1.0, float(np.median(probabilities))))
    except Exception:
        return None


def _cv2():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("opencv-python-headless is required for written-answer OCR preprocessing") from exc
    return cv2
