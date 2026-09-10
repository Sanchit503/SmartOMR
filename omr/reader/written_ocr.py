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
LINE_TARGET_HEIGHT_PX = 96
MIN_LINE_INK_FRACTION = 0.0025
RAW_RETRY_CONFIDENCE = 0.72
RAW_ACCEPT_SCORE = 0.74


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
    analysis_crop_path: str | None = None
    recognition_policy: str = "raw_htr_first"

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class LineOcrResult:
    text: str
    confidence: float | None = None
    raw: object | None = None


@dataclass(frozen=True)
class _LineCropVariant:
    name: str
    line_index: int
    crop_path: Path
    role: str


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
        max_new_tokens: int = 48,
        num_beams: int = 4,
        num_return_sequences: int = 3,
        no_repeat_ngram_size: int = 3,
        repetition_penalty: float = 1.08,
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
        self.num_return_sequences = max(1, min(num_return_sequences, num_beams))
        self.no_repeat_ngram_size = no_repeat_ngram_size
        self.repetition_penalty = repetition_penalty

    def read_line(self, line_path: Path) -> LineOcrResult:
        image = Image.open(line_path).convert("RGB")
        inputs = self.processor(images=image, return_tensors="pt")
        pixel_values = inputs.pixel_values.to(self.device)
        with self.torch.no_grad():
            generated = self.model.generate(
                pixel_values,
                max_new_tokens=self.max_new_tokens,
                num_beams=self.num_beams,
                num_return_sequences=self.num_return_sequences,
                no_repeat_ngram_size=self.no_repeat_ngram_size,
                repetition_penalty=self.repetition_penalty,
                return_dict_in_generate=True,
                output_scores=True,
            )
        decoded = self.processor.batch_decode(generated.sequences, skip_special_tokens=True)
        alternatives = _generation_alternatives(decoded, getattr(generated, "sequences_scores", None))
        text = alternatives[0]["text"] if alternatives else ""
        confidence = _generation_confidence(self.torch, getattr(generated, "scores", None))
        return LineOcrResult(
            text=_clean_text(text),
            confidence=confidence,
            raw={
                "provider": self.provider,
                "model": self.model_name,
                "device": self.device,
                "num_beams": self.num_beams,
                "num_return_sequences": self.num_return_sequences,
                "max_new_tokens": self.max_new_tokens,
                "no_repeat_ngram_size": self.no_repeat_ngram_size,
                "repetition_penalty": self.repetition_penalty,
                "alternatives": alternatives,
            },
        )


class EnsembleWrittenOcr:
    """Run multiple offline OCR engines and keep the strongest line candidate."""

    provider = "ensemble"

    def __init__(self, backends: list[WrittenOcrBackend], startup_errors: list[str] | None = None) -> None:
        if not backends:
            detail = "; ".join(startup_errors or []) or "no OCR backends configured"
            raise RuntimeError(f"no written OCR backend is available for ensemble mode: {detail}")
        self.backends = backends
        self.startup_errors = startup_errors or []

    def read_line(self, line_path: Path) -> LineOcrResult:
        candidates: list[dict[str, object]] = []
        errors: list[str] = list(self.startup_errors)
        for backend in self.backends:
            try:
                result = backend.read_line(line_path)
            except Exception as exc:
                errors.append(f"{backend.provider}: {type(exc).__name__}: {exc}")
                continue
            candidates.append(
                {
                    "provider": backend.provider,
                    "text": result.text,
                    "confidence": result.confidence,
                    "quality": round(_text_quality_score(result.text), 4),
                    "score": round(_line_candidate_score(result), 4),
                    "raw": result.raw,
                }
            )

        if not candidates:
            return LineOcrResult(
                text="",
                confidence=0.0,
                raw={"provider": self.provider, "errors": errors, "candidates": candidates},
            )

        selected = max(candidates, key=lambda item: (float(item["score"]), len(str(item["text"]))))
        confidence = selected.get("confidence")
        return LineOcrResult(
            text=str(selected["text"]),
            confidence=float(confidence) if confidence is not None else None,
            raw={
                "provider": self.provider,
                "selected_provider": selected["provider"],
                "errors": errors,
                "candidates": candidates,
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
    if selected in {"ensemble", "best", "auto"}:
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

    return _save_written_line_crops_from_path(
        Path(crop.crop_path),
        crop,
        output_dir,
        preparation="raw_htr",
    )


def _save_written_line_crops_from_path(
    source_path: Path,
    crop: WrittenCrop,
    output_dir: str | Path,
    suffix: str | None = None,
    preparation: str = "raw_htr",
) -> list[Path]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    gray = np.asarray(Image.open(source_path).convert("L"))
    gray = _trim_answer_box_border(gray)
    expected_lines = max(1, int(crop.lines))
    line_paths: list[Path] = []
    for index in range(expected_lines):
        y0, y1 = _line_band(gray.shape[0], expected_lines, index)
        line = gray[y0:y1, :]
        if preparation == "analysis":
            prepared = prepare_written_line_for_blank_detection(line)
        else:
            prepared = prepare_written_line_for_htr(line, normalize=preparation == "light_htr")
        middle = f"_{suffix}" if suffix else ""
        path = output_dir / f"Q{crop.q_no}{middle}_line_{index + 1}.png"
        Image.fromarray(prepared, mode="L").save(path)
        line_paths.append(path)
    return line_paths


def _save_written_line_crop_variants(
    crop: WrittenCrop,
    output_dir: str | Path,
) -> dict[int, list[_LineCropVariant]]:
    sources: list[tuple[str, Path, str, str]] = []
    raw_path = Path(crop.crop_path)
    sources.append(("raw", raw_path, "recognition", "raw_htr"))
    sources.append(("light", raw_path, "recognition", "light_htr"))
    if crop.ocr_crop_path:
        sources.append(("ink_analysis", Path(crop.ocr_crop_path), "analysis", "analysis"))

    variants: dict[int, list[_LineCropVariant]] = {}
    for name, source_path, role, preparation in sources:
        suffix = name if len(sources) > 1 else None
        for index, path in enumerate(
            _save_written_line_crops_from_path(
                source_path,
                crop,
                output_dir,
                suffix=suffix,
                preparation=preparation,
            ),
            start=1,
        ):
            variants.setdefault(index, []).append(
                _LineCropVariant(name=name, line_index=index, crop_path=path, role=role)
            )
    return variants


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
        line_variants_by_index = _save_written_line_crop_variants(crop, line_dir)
        expected_lines = max(1, int(crop.lines))
        line_results: list[WrittenLineOcr] = []
        review_flags: list[str] = []

        for index in range(1, expected_lines + 1):
            line_flags: list[str] = []
            variants = line_variants_by_index.get(index, [])
            blank_variant = _preferred_blank_variant(variants)
            if blank_variant is not None and _line_is_blank(
                blank_variant.crop_path,
                line_index=index,
                total_lines=expected_lines,
            ):
                line_flags.append("line appears blank after rule removal")
                line_result = WrittenLineOcr(
                    line_index=index,
                    crop_path=str(blank_variant.crop_path),
                    text="",
                    confidence=0.0,
                    provider=backend.provider,
                    review_flags=line_flags,
                    raw={
                        "reason": "blank_line",
                        "blank_detection_variant": blank_variant.name,
                        "blank_detection_role": blank_variant.role,
                    },
                )
            else:
                candidates = _read_line_candidates(
                    variants,
                    backend,
                    line_index=index,
                    total_lines=expected_lines,
                )
                usable_candidates = [candidate for candidate in candidates if candidate["text"]]
                if usable_candidates:
                    selected = max(
                        usable_candidates,
                        key=lambda item: (float(item["score"]), len(str(item["text"]))),
                    )
                    line_flags.extend(str(flag) for flag in selected["review_flags"])
                    line_result = WrittenLineOcr(
                        line_index=index,
                        crop_path=str(selected["crop_path"]),
                        text=str(selected["text"]),
                        confidence=selected["confidence"],  # type: ignore[arg-type]
                        provider=backend.provider,
                        review_flags=line_flags,
                        raw={
                            "selected_variant": selected["variant"],
                            "selected_raw": selected["raw"],
                            "candidates": candidates,
                        },
                    )
                else:
                    line_flags.append("OCR returned empty text for a non-blank line")
                    line_result = WrittenLineOcr(
                        line_index=index,
                        crop_path=str(variants[0].crop_path) if variants else "",
                        text="",
                        confidence=0.0,
                        provider=backend.provider,
                        review_flags=line_flags,
                        raw={"reason": "no_usable_candidate", "candidates": candidates},
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
            analysis_crop_path=crop.ocr_crop_path,
        )
    return answers


def prepare_written_line_for_ocr(line: np.ndarray) -> np.ndarray:
    """Compatibility wrapper for the raw HTR recognition image."""

    return prepare_written_line_for_htr(line)


def prepare_written_line_for_htr(line: np.ndarray, *, normalize: bool = False) -> np.ndarray:
    """Return a model-sized line image while preserving handwriting strokes."""

    cv2 = _cv2()
    gray = line.astype(np.uint8, copy=False)
    if gray.size == 0:
        return gray
    if normalize:
        prepared = _normalize_illumination(gray)
        prepared = np.asarray(ImageOps.autocontrast(Image.fromarray(prepared, mode="L")))
    else:
        prepared = np.asarray(ImageOps.autocontrast(Image.fromarray(gray, mode="L"), cutoff=1))
    h, w = prepared.shape[:2]
    if h < LINE_TARGET_HEIGHT_PX:
        scale = LINE_TARGET_HEIGHT_PX / max(1, h)
        prepared = cv2.resize(
            prepared,
            (max(1, round(w * scale)), LINE_TARGET_HEIGHT_PX),
            interpolation=cv2.INTER_CUBIC,
        )
    return cv2.copyMakeBorder(prepared, 12, 12, 24, 24, cv2.BORDER_CONSTANT, value=255)


def prepare_written_line_for_blank_detection(line: np.ndarray) -> np.ndarray:
    """Return a destructive analysis image used only for blank/ink checks."""

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
    return cv2.copyMakeBorder(cleaned_array, 12, 12, 24, 24, cv2.BORDER_CONSTANT, value=255)


def _preferred_blank_variant(variants: list[_LineCropVariant]) -> _LineCropVariant | None:
    for variant in variants:
        if variant.role == "analysis":
            return variant
    return variants[0] if variants else None


def _read_line_candidates(
    variants: list[_LineCropVariant],
    backend: WrittenOcrBackend,
    *,
    line_index: int,
    total_lines: int,
) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    recognition_variants = [variant for variant in variants if variant.role == "recognition"]
    has_analysis_variant = any(variant.role == "analysis" for variant in variants)
    for variant in recognition_variants:
        flags: list[str] = []
        if not has_analysis_variant and _line_is_blank(
            variant.crop_path,
            line_index=line_index,
            total_lines=total_lines,
        ):
            candidates.append(
                {
                    "variant": variant.name,
                    "role": variant.role,
                    "crop_path": str(variant.crop_path),
                    "text": "",
                    "confidence": 0.0,
                    "quality": 0.0,
                    "score": 0.0,
                    "review_flags": ["line appears blank after rule removal"],
                    "raw": {"reason": "blank_line"},
                }
            )
            continue

        read = backend.read_line(variant.crop_path)
        text = read.text
        confidence = read.confidence
        raw = read.raw
        if _looks_like_hallucinated_noise(text):
            flags.append("OCR output looked like hallucinated noise and was suppressed")
            text = ""
            confidence = 0.0
            raw = {"suppressed": read.raw}
        if not text:
            flags.append("OCR returned empty text for a non-blank line")
        if confidence is None:
            flags.append("OCR did not report confidence")
        elif confidence < 0.60:
            flags.append(f"low OCR confidence {confidence:.2f}")

        result = LineOcrResult(text=text, confidence=confidence, raw=raw)
        candidates.append(
            {
                "variant": variant.name,
                "role": variant.role,
                "crop_path": str(variant.crop_path),
                "text": text,
                "confidence": confidence,
                "quality": round(_text_quality_score(text), 4),
                "score": round(_line_candidate_score(result), 4),
                "review_flags": flags,
                "raw": raw,
            }
        )
        score = _line_candidate_score(LineOcrResult(text=text, confidence=confidence, raw=raw))
        if text and confidence is not None and confidence >= RAW_RETRY_CONFIDENCE and score >= RAW_ACCEPT_SCORE:
            break
    return candidates


def _line_band(height: int, lines: int, index: int) -> tuple[int, int]:
    top = round(index * height / lines)
    bottom = round((index + 1) * height / lines)
    pad = max(2, round((bottom - top) * 0.12))
    band_top = max(0, top - pad) if index == 0 else top
    return band_top, min(height, bottom + pad)


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


def _line_is_blank(line_path: Path, line_index: int = 1, total_lines: int = 1) -> bool:
    cv2 = _cv2()
    gray = np.asarray(Image.open(line_path).convert("L"))
    _threshold, ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(ink, connectivity=8)
    cleaned = np.zeros_like(ink)
    kept_components = 0
    min_area = max(8, round(ink.size * 0.00035))
    for label in range(1, count):
        if stats[label, cv2.CC_STAT_AREA] >= min_area:
            cleaned[labels == label] = 255
            kept_components += 1
    ink_fraction = float(np.count_nonzero(cleaned) / max(1, cleaned.size))
    if ink_fraction < MIN_LINE_INK_FRACTION or kept_components == 0:
        return True
    if line_index > 1 and total_lines > 1:
        horizontal_coverage = float(np.count_nonzero(np.any(cleaned > 0, axis=0)) / max(1, cleaned.shape[1]))
        if ink_fraction < 0.008 and horizontal_coverage < 0.055:
            return True
    return False


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


def _line_candidate_score(result: LineOcrResult) -> float:
    confidence = result.confidence if result.confidence is not None else 0.58
    return max(0.0, min(1.0, 0.62 * confidence + 0.38 * _text_quality_score(result.text)))


def _text_quality_score(text: str) -> float:
    compact = text.strip()
    if not compact:
        return 0.0
    chars = [char for char in compact if not char.isspace()]
    if not chars:
        return 0.0

    alnum_fraction = sum(1 for char in chars if char.isalnum()) / len(chars)
    symbol_fraction = sum(1 for char in chars if not char.isalnum()) / len(chars)
    repeated_noise = len(re.findall(r"[_=\-]{3,}|[^\w\s]{4,}", compact))
    length_score = min(1.0, len(chars) / 14.0)
    score = 0.20 + 0.48 * alnum_fraction + 0.22 * length_score - 0.35 * symbol_fraction
    score -= min(0.30, repeated_noise * 0.12)
    return max(0.0, min(1.0, score))


def _looks_like_hallucinated_noise(text: str) -> bool:
    compact = re.sub(r"\s+", "", text.strip())
    if not compact:
        return False
    if re.search(r"(.)\1{10,}", compact):
        return True
    alnum = [char for char in compact if char.isalnum()]
    if len(compact) >= 24 and alnum:
        dominant_fraction = max(alnum.count(char) for char in set(alnum)) / len(alnum)
        if dominant_fraction >= 0.70:
            return True
    if len(alnum) >= 5:
        dominant_fraction = max(alnum.count(char) for char in set(alnum)) / len(alnum)
        symbol_fraction = sum(1 for char in compact if not char.isalnum()) / len(compact)
        if dominant_fraction >= 0.85 and symbol_fraction >= 0.25:
            return True
    symbol_fraction = sum(1 for char in compact if not char.isalnum()) / len(compact)
    if len(compact) >= 18 and symbol_fraction >= 0.55:
        return True
    return False


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


def _generation_alternatives(decoded: list[str], sequence_scores: object | None) -> list[dict[str, object]]:
    alternatives: list[dict[str, object]] = []
    scores: list[float | None] = [None] * len(decoded)
    if sequence_scores is not None:
        try:
            scores = [float(score.detach().cpu()) for score in sequence_scores]
        except Exception:
            scores = [None] * len(decoded)
    for index, text in enumerate(decoded):
        item: dict[str, object] = {"rank": index + 1, "text": _clean_text(text)}
        score = scores[index] if index < len(scores) else None
        if score is not None:
            item["sequence_score"] = score
        alternatives.append(item)
    return alternatives


def _cv2():
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("opencv-python-headless is required for written-answer OCR preprocessing") from exc
    return cv2
