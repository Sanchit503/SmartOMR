"""Continuation-page handwritten roll-number support.

Handwriting is treated as an identity-risk signal, not a guaranteed truth.
The reader crops the roll strip declared in the manifest, optionally sends
that crop to a configured OCR backend, validates the text against the
expected roll format, and returns low confidence when evidence is weak.
"""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from PIL import Image, ImageOps

from omr.contracts.geometry import mm_to_px, px_per_mm
from omr.grading.bubbles import ink_density, student_mark_fill_ratio
from omr.grading.mcq import DEFAULT_AMBIGUOUS_FLOOR, DEFAULT_FILL_THRESHOLD, DEFAULT_INK_FLOOR
from omr.models import HandwrittenRollRead


DEFAULT_RESNET_ROLL_MODEL = Path("data/models/roll_digit_resnet_omr_finetuned.pt")


@dataclass(frozen=True)
class RollOcrResult:
    text: str
    confidence: float | None = None
    raw: object | None = None


class RollOcrBackend(Protocol):
    provider: str

    def read_roll(self, crop_path: Path, program: str | None = None, valid_rolls: set[str] | None = None) -> RollOcrResult:
        ...


class LocalDigitModelRollOcr:
    """No-key OCR backend using a trained local OpenCV digit model."""

    provider = "local_digit_knn"

    def __init__(self, model_path: str | Path) -> None:
        from omr.reader.digit_model import OpenCVDigitKnn

        self.model = OpenCVDigitKnn(model_path)

    def read_roll(self, crop_path: Path, program: str | None = None, valid_rolls: set[str] | None = None) -> RollOcrResult:
        expected_digits = _expected_digit_count(program)
        if expected_digits is None:
            return RollOcrResult("", confidence=0.0, raw={"reason": "program_required_for_digit_model"})

        image = Image.open(crop_path).convert("L")
        digits = ""
        confidences: list[float] = []
        payloads: list[dict[str, object]] = []
        for cell in _segment_digit_cells(image, expected_digits):
            read = self.read_digit_image(cell)
            digit = _first_digit(read.text)
            digits += digit or "?"
            confidences.append(float(read.confidence or 0.0))
            payloads.append(_ocr_result_payload(read))

        text = _format_roll_digits(program, digits)
        confidence = min(confidences) if "?" not in digits and confidences else 0.0
        return RollOcrResult(
            text=text,
            confidence=confidence,
            raw={
                "provider": self.provider,
                "model_path": str(self.model.model_path),
                "cells": payloads,
            },
        )

    def read_digit(self, crop_path: Path) -> RollOcrResult:
        return self.read_digit_image(Image.open(crop_path).convert("L"))

    def read_digit_image(self, image: Image.Image) -> RollOcrResult:
        prediction = self.model.predict(image)
        return RollOcrResult(
            prediction.digit or "",
            confidence=prediction.confidence,
            raw={"provider": self.provider, "prediction": prediction.to_json()},
        )


class LocalResnetRollOcr:
    """Roll digit OCR backed by the project's fine-tuned handwritten-cell ResNet."""

    provider = "local_resnet_roll_digit"

    def __init__(self, model_path: str | Path) -> None:
        try:
            import torch
            from omr.datasets.train_roll_digit_resnet import LABELS, SmallRollDigitResNet
        except ImportError as exc:
            raise RuntimeError("PyTorch is required to use the fine-tuned roll digit model") from exc
        self.model_path = Path(model_path)
        if not self.model_path.is_file():
            raise FileNotFoundError(f"fine-tuned roll digit model not found: {self.model_path}")
        payload = torch.load(self.model_path, map_location="cpu")
        state = payload.get("model_state", payload) if isinstance(payload, dict) else payload
        self.model = SmallRollDigitResNet(num_classes=len(LABELS))
        self.model.load_state_dict(state)
        self.model.eval()
        self.torch = torch
        self.labels = LABELS

    def read_roll(self, crop_path: Path, program: str | None = None, valid_rolls: set[str] | None = None) -> RollOcrResult:
        expected_digits = _expected_digit_count(program)
        if expected_digits is None:
            return RollOcrResult("", confidence=0.0, raw={"reason": "program_required_for_resnet"})
        image = Image.open(crop_path).convert("L")
        reads = [self.read_digit_image(cell) for cell in _segment_digit_cells(image, expected_digits)]
        digits = "".join(_first_digit(read.text) or "?" for read in reads)
        confidence = min((float(read.confidence or 0.0) for read in reads), default=0.0) if "?" not in digits else 0.0
        return RollOcrResult(
            _format_roll_digits(program, digits),
            confidence=confidence,
            raw={"provider": self.provider, "model_path": str(self.model_path), "cells": [_ocr_result_payload(read) for read in reads]},
        )

    def read_digit(self, crop_path: Path) -> RollOcrResult:
        return self.read_digit_image(Image.open(crop_path).convert("L"))

    def read_digit_image(self, image: Image.Image) -> RollOcrResult:
        image = ImageOps.autocontrast(image)
        image = ImageOps.pad(image, (64, 64), color=255)
        array = np.asarray(image, dtype=np.float32) / 255.0
        tensor = self.torch.from_numpy((1.0 - array - 0.15) / 0.35).unsqueeze(0).unsqueeze(0)
        with self.torch.no_grad():
            probabilities = self.torch.softmax(self.model(tensor), dim=1)[0]
        confidence, index = self.torch.max(probabilities, dim=0)
        label = self.labels[int(index.item())]
        digit = "" if label == "blank" else label
        return RollOcrResult(digit, confidence=float(confidence.item()), raw={"provider": self.provider, "label": label})


class LocalEnsembleRollOcr:
    """Combine local digit-model and OCR-engine evidence without using APIs."""

    provider = "local_ensemble"

    def __init__(self, backends: list[RollOcrBackend], warnings: list[str] | None = None) -> None:
        self.backends = backends
        self.warnings = warnings or []

    def read_roll(self, crop_path: Path, program: str | None = None, valid_rolls: set[str] | None = None) -> RollOcrResult:
        results = [backend.read_roll(crop_path, program=program) for backend in self.backends]
        candidates: list[dict[str, object]] = []
        for result in results:
            candidates.append(
                {
                    "text": result.text,
                    "confidence": result.confidence,
                    "provider": _provider_from_raw(result),
                    "raw": result.raw,
                }
            )
        best = _best_ocr_candidate(candidates, program)
        confidence = _ensemble_confidence(candidates, str(best.get("text", "")), program)
        return RollOcrResult(
            text=str(best.get("text", "")),
            confidence=confidence,
            raw={
                "provider": self.provider,
                "warnings": self.warnings,
                "candidates": candidates,
            },
        )

    def read_digit(self, crop_path: Path) -> RollOcrResult:
        results: list[RollOcrResult] = []
        for backend in self.backends:
            if hasattr(backend, "read_digit"):
                results.append(backend.read_digit(crop_path))  # type: ignore[attr-defined]
            else:
                results.append(backend.read_roll(crop_path, program=None))
        return _choose_digit_result(results, warnings=self.warnings)


class LocalTesseractRollOcr:
    """No-key OCR backend using a local Tesseract installation.

    Tesseract is not perfect for handwriting, so its output still goes
    through format validation and confidence gating before page grouping.
    """

    provider = "local_tesseract"
    fast_cell_first = True
    parallel_cell_reads = True

    def __init__(self, tesseract_cmd: str | None = None) -> None:
        try:
            import pytesseract
        except ImportError as exc:
            raise RuntimeError(
                "pytesseract is required for local handwritten roll OCR. "
                "Install dependencies, and install the Tesseract binary on the machine."
            ) from exc
        self.pytesseract = pytesseract
        command = tesseract_cmd or os.environ.get("SMARTOMR_TESSERACT_CMD")
        if not command:
            for candidate in (
                Path("C:/Program Files/Tesseract-OCR/tesseract.exe"),
                Path("C:/Program Files (x86)/Tesseract-OCR/tesseract.exe"),
            ):
                if candidate.exists():
                    command = str(candidate)
                    break
        if command:
            self.pytesseract.pytesseract.tesseract_cmd = command

        try:
            self.version = str(self.pytesseract.get_tesseract_version())
        except Exception as exc:
            raise RuntimeError(
                "Tesseract OCR binary is not available. Install Tesseract or set SMARTOMR_TESSERACT_CMD."
            ) from exc

    def read_roll(self, crop_path: Path, program: str | None = None, valid_rolls: set[str] | None = None) -> RollOcrResult:
        image = Image.open(crop_path).convert("L")
        candidates: list[dict[str, object]] = []

        prepared = dict(_prepare_ocr_variants(image))
        variant_names = [name for name in ("gray_clean", "otsu_clean") if name in prepared]
        if not variant_names:
            variant_names = list(prepared)[:1]
        variants = [(name, prepared[name]) for name in variant_names]
        with ThreadPoolExecutor(max_workers=len(variants)) as executor:
            reads = list(
                executor.map(
                    lambda item: self._read_image(item[1], "--psm 7", whitelist="0123456789MTPHDSP"),
                    variants,
                )
            )
        for (variant_name, _variant), whole in zip(variants, reads):
            candidates.append({"kind": "whole-strip", "variant": variant_name, "psm": "--psm 7", **whole})

        best = _best_ocr_candidate(candidates, program)
        return RollOcrResult(
            text=str(best.get("text", "")),
            confidence=_clamp_confidence(best.get("confidence")),
            raw={"provider": self.provider, "version": self.version, "candidates": candidates},
        )

    def read_digit(self, crop_path: Path) -> RollOcrResult:
        return self.read_digit_image(Image.open(crop_path).convert("L"))

    def read_digit_image(self, image: Image.Image) -> RollOcrResult:
        attempts: list[dict[str, object]] = []
        votes: dict[str, list[float]] = {}
        prepared = dict(_prepare_digit_ocr_variants(image))
        variant_name = "pil_gray" if "pil_gray" in prepared else next(iter(prepared))
        read = self._read_image(prepared[variant_name], "--psm 10", whitelist="0123456789")
        digit = _first_digit(read["text"])
        attempts.append({"variant": variant_name, "psm": "--psm 10", "digit": digit, **read})
        if digit is not None:
            confidence = _clamp_confidence(read.get("confidence"))
            votes.setdefault(digit, []).append(confidence if confidence is not None else 0.58)

        if not votes:
            return RollOcrResult("", confidence=0.0, raw={"attempts": attempts})

        ranked = sorted(
            votes.items(),
            key=lambda item: (len(item[1]), float(np.median(item[1])), max(item[1])),
            reverse=True,
        )
        digit, confidences = ranked[0]
        confidence = _digit_vote_confidence(confidences, total_attempts=len(attempts))
        if len(ranked) > 1 and len(ranked[1][1]) == len(confidences):
            confidence = min(confidence, 0.59)
        return RollOcrResult(digit, confidence=confidence, raw={"attempts": attempts, "votes": votes})

    def _read_image(self, image: Image.Image, psm_config: str, whitelist: str) -> dict[str, object]:
        config = f"{psm_config} -c tessedit_char_whitelist={whitelist}"
        confidences: list[float] = []
        try:
            data = self.pytesseract.image_to_data(
                image,
                config=config,
                output_type=self.pytesseract.Output.DICT,
            )
            text = "".join(str(value) for value in data.get("text", []) if str(value).strip())
            for value in data.get("conf", []):
                try:
                    confidence = float(value)
                except (TypeError, ValueError):
                    continue
                if confidence >= 0:
                    confidences.append(confidence / 100.0)
        except Exception:
            text = self.pytesseract.image_to_string(image, config=config).strip()
        compact = re.sub(r"[^0-9A-Za-z]+", "", text.upper())
        return {
            "text": compact,
            "confidence": float(np.median(confidences)) if confidences else None,
        }


def build_roll_ocr_backend(
    provider: str | None = None,
    *,
    digit_model_path: str | Path | None = None,
    resnet_model_path: str | Path | None = None,
) -> RollOcrBackend | None:
    selected = (provider or "none").strip().lower()
    if selected in {"", "none", "off", "disabled"}:
        return None
    if selected == "local":
        backends: list[RollOcrBackend] = []
        warnings: list[str] = []
        resnet_path = resnet_model_path or os.environ.get("SMARTOMR_ROLL_RESNET_MODEL")
        if resnet_path is None and digit_model_path is None:
            resnet_path = DEFAULT_RESNET_ROLL_MODEL
        if resnet_path and Path(resnet_path).is_file():
            try:
                backends.append(LocalResnetRollOcr(resnet_path))
            except RuntimeError as exc:
                warnings.append(str(exc))
        model_path = digit_model_path or os.environ.get("SMARTOMR_DIGIT_MODEL")
        if model_path:
            backends.append(LocalDigitModelRollOcr(model_path))
        if not backends:
            raise RuntimeError(
                "No local roll digit model is available. Train or provide the fine-tuned ResNet checkpoint. "
                "Tesseract is intentionally disabled for roll-number matching."
            )
        if len(backends) == 1:
            return backends[0]
        return LocalEnsembleRollOcr(backends, warnings=warnings)
    raise ValueError(f"unknown handwritten roll OCR provider {provider!r}")


def normalize_handwritten_roll_text(text: str, program: str | None = None) -> str | None:
    """Normalize OCR text into a roll number if it matches a safe format."""
    compact = re.sub(r"[^0-9A-Z]+", "", text.upper())
    sp_match = re.search(r"SP(\d{2}[A-Z0-9]{3,6})(?![A-Z0-9])", compact)
    if sp_match:
        return f"SP{sp_match.group(1)}"

    cleaned = compact
    cleaned = cleaned.replace("O", "0").replace("Q", "0")
    cleaned = cleaned.replace("I", "1").replace("L", "1").replace("|", "1")
    cleaned = cleaned.replace("S", "5").replace("Z", "2").replace("G", "6").replace("B", "8")

    expected = (program or "").upper()
    if expected == "BTECH":
        match = re.search(r"(?<!\d)(\d{7})(?!\d)", cleaned)
        return match.group(1) if match else None
    if expected == "MTECH":
        match = re.search(r"MT(\d{5})(?!\d)", cleaned)
        if match:
            return f"MT{match.group(1)}"
        match = re.search(r"(?<!\d)(\d{5})(?!\d)", cleaned)
        return f"MT{match.group(1)}" if match else None
    if expected == "PHD":
        match = re.search(r"PHD(\d{5})(?!\d)", cleaned)
        if match:
            return f"PHD{match.group(1)}"
        match = re.search(r"(?<!\d)(\d{5})(?!\d)", cleaned)
        return f"PHD{match.group(1)}" if match else None

    match = re.search(r"PHD(\d{5})(?!\d)", cleaned)
    if match:
        return f"PHD{match.group(1)}"
    match = re.search(r"MT(\d{5})(?!\d)", cleaned)
    if match:
        return f"MT{match.group(1)}"
    match = re.search(r"(?<!\d)(\d{7})(?!\d)", cleaned)
    if match:
        return match.group(1)
    return None


def save_roll_number_crops(
    image: object,
    manifest: dict,
    page_index: int,
    output_dir: str | Path,
    dpi: float,
    padding_mm: float = 1.5,
) -> dict[str, str]:
    """Save handwritten roll-number strip crops for one canonical page."""
    strip_paths, _cell_paths = save_roll_number_crop_sets(
        image,
        manifest,
        page_index,
        output_dir,
        dpi,
        padding_mm=padding_mm,
    )
    return strip_paths


def save_roll_number_crop_sets(
    image: object,
    manifest: dict,
    page_index: int,
    output_dir: str | Path,
    dpi: float,
    padding_mm: float = 1.5,
    cell_padding_mm: float = 0.5,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Save whole roll strips and exact per-cell crops from manifest geometry."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    gray = _gray_array(image)
    crop_paths: dict[str, str] = {}
    cell_crop_paths: dict[str, list[str]] = {}
    for field in _roll_write_in_fields(manifest, page_index):
        programs = _field_programs(field) or ("UNKNOWN",)
        stem = "_".join(program.lower() for program in programs)
        x0, y0, x1, y1 = _crop_box_px(field, gray.shape, dpi, padding_mm=padding_mm)
        crop = gray[y0:y1, x0:x1]
        path = output_dir / f"page_{page_index}_{stem}_roll_crop.png"
        Image.fromarray(crop, mode="L").save(path)

        cells: list[str] = []
        for index, (cx0, cy0, cx1, cy1) in enumerate(_cell_crop_boxes_px(field, gray.shape, dpi, cell_padding_mm)):
            cell = gray[cy0:cy1, cx0:cx1]
            cell_path = output_dir / f"page_{page_index}_{stem}_roll_cell_{index + 1}.png"
            Image.fromarray(cell, mode="L").save(cell_path)
            cells.append(str(cell_path))
        for program in programs:
            crop_paths[program] = str(path)
            cell_crop_paths[program] = cells
    return crop_paths, cell_crop_paths


def read_continuation_roll_number(
    image: object,
    manifest: dict,
    dpi: float,
    page_index: int,
    output_dir: str | Path,
    ocr_backend: RollOcrBackend | None = None,
    padding_mm: float = 1.5,
    valid_rolls: set[str] | None = None,
) -> HandwrittenRollRead:
    """Read a continuation-page handwritten roll field when possible."""
    gray = _gray_array(image)
    selected_program, selector_signals, selector_flags = _read_continuation_program(gray, manifest, dpi, page_index)
    read = _read_write_in_roll_number(
        gray,
        manifest,
        dpi,
        page_index,
        output_dir,
        selected_program=selected_program,
        selector_flags=selector_flags,
        ocr_backend=ocr_backend,
        padding_mm=padding_mm,
        valid_rolls=valid_rolls,
    )
    if selector_signals:
        read.ocr_results.setdefault("_program_selector", {"signals": selector_signals})
    return read


def read_write_in_roll_number(
    image: object,
    manifest: dict,
    dpi: float,
    page_index: int,
    output_dir: str | Path,
    ocr_backend: RollOcrBackend | None = None,
    selected_program: str | None = None,
    padding_mm: float = 1.5,
    valid_rolls: set[str] | None = None,
) -> HandwrittenRollRead:
    """Read a manifest-declared write-in roll field on any page."""
    return _read_write_in_roll_number(
        _gray_array(image),
        manifest,
        dpi,
        page_index,
        output_dir,
        selected_program=selected_program,
        selector_flags=[],
        ocr_backend=ocr_backend,
        padding_mm=padding_mm,
        valid_rolls=valid_rolls,
    )


def _read_write_in_roll_number(
    gray: np.ndarray,
    manifest: dict,
    dpi: float,
    page_index: int,
    output_dir: str | Path,
    *,
    selected_program: str | None,
    selector_flags: list[str],
    ocr_backend: RollOcrBackend | None,
    padding_mm: float,
    valid_rolls: set[str] | None,
) -> HandwrittenRollRead:
    review_flags = list(selector_flags)
    crop_paths, cell_crop_paths = save_roll_number_crop_sets(
        gray,
        manifest,
        page_index,
        output_dir,
        dpi,
        padding_mm=padding_mm,
    )
    if not crop_paths:
        review_flags.append(f"page {page_index} has no roll-number write-in field in manifest")

    provider = ocr_backend.provider if ocr_backend is not None else "none"
    ocr_results: dict[str, dict[str, object]] = {}
    if ocr_backend is None:
        if crop_paths:
            review_flags.append("handwritten roll OCR is not configured; saved roll crop(s) for manual review")
        return HandwrittenRollRead(
            page_index=page_index,
            program=selected_program,
            roll_no=None,
            confidence="low",
            provider=provider,
            raw_text=None,
            crop_paths=crop_paths,
            cell_crop_paths=cell_crop_paths,
            ocr_results=ocr_results,
            review_flags=review_flags,
        )

    programs_to_read = [selected_program] if selected_program and selected_program in crop_paths else list(crop_paths)
    candidates: list[tuple[str, str, float | None, str, str]] = []
    for program in programs_to_read:
        crop_path = Path(crop_paths[program])
        cell_result = _read_roll_from_cells(ocr_backend, [Path(path) for path in cell_crop_paths.get(program, [])], program)
        cell_normalized = normalize_handwritten_roll_text(cell_result.text, program=program)
        trusted_cells = bool(cell_normalized) and (valid_rolls is None or cell_normalized in valid_rolls)
        if getattr(ocr_backend, "fast_cell_first", False) and trusted_cells:
            strip_result = RollOcrResult(
                "",
                confidence=None,
                raw={"skipped": "validated cell OCR succeeded"},
            )
        else:
            strip_result = ocr_backend.read_roll(crop_path, program=program, valid_rolls=valid_rolls)
        strip_normalized = normalize_handwritten_roll_text(strip_result.text, program=program)
        ocr_results[program] = {
            "strip": _ocr_result_payload(strip_result),
            "cells": _ocr_result_payload(cell_result),
            "text": cell_result.text or strip_result.text,
            "normalized_roll": cell_normalized or strip_normalized,
            "confidence": _combined_confidence(cell_result.confidence, strip_result.confidence),
            "cell_crop_paths": cell_crop_paths.get(program, []),
        }
        if cell_normalized:
            candidates.append((program, cell_normalized, cell_result.confidence, cell_result.text, "cells"))
        if strip_normalized:
            candidates.append((program, strip_normalized, strip_result.confidence, strip_result.text, "strip"))

    if not candidates:
        review_flags.append("handwritten roll OCR did not produce a valid roll number")
        return HandwrittenRollRead(
            page_index=page_index,
            program=selected_program,
            roll_no=None,
            confidence="low",
            provider=provider,
            raw_text=_joined_raw_text(ocr_results),
            crop_paths=crop_paths,
            cell_crop_paths=cell_crop_paths,
            ocr_results=ocr_results,
            review_flags=review_flags,
        )

    if valid_rolls is not None:
        roster_candidates = [candidate for candidate in candidates if normalize_handwritten_roll_text(candidate[1]) in valid_rolls]
        if roster_candidates:
            candidates = roster_candidates
        else:
            review_flags.append("handwritten roll OCR produced no roll number present in the roster")
            return HandwrittenRollRead(
                page_index=page_index,
                program=selected_program,
                roll_no=None,
                confidence="low",
                provider=provider,
                raw_text=_joined_raw_text(ocr_results),
                crop_paths=crop_paths,
                cell_crop_paths=cell_crop_paths,
                ocr_results=ocr_results,
                review_flags=review_flags,
            )

    unique_rolls = sorted({roll for _program, roll, _confidence, _text, _source in candidates})
    if len(unique_rolls) > 1:
        review_flags.append(f"handwritten roll OCR produced conflicting candidates: {', '.join(unique_rolls)}")
        return HandwrittenRollRead(
            page_index=page_index,
            program=selected_program,
            roll_no=None,
            confidence="low",
            provider=provider,
            raw_text=_joined_raw_text(ocr_results),
            crop_paths=crop_paths,
            cell_crop_paths=cell_crop_paths,
            ocr_results=ocr_results,
            review_flags=review_flags,
        )

    program, roll_no, confidence_value, raw_text, source = max(
        candidates,
        key=lambda item: ((item[2] if item[2] is not None else 0.65), 1 if item[4] == "cells" else 0),
    )
    if selected_program is not None and selected_program != program:
        review_flags.append(
            f"program selector reads {selected_program}, but OCR candidate came from {program} field"
        )

    confidence = _roll_confidence(confidence_value, review_flags)
    if source == "strip":
        confidence = "medium" if confidence == "high" else confidence
        review_flags.append("handwritten roll was read from whole-strip OCR without full cell confirmation")
    return HandwrittenRollRead(
        page_index=page_index,
        program=selected_program or program,
        roll_no=roll_no,
        confidence=confidence,
        provider=provider,
        raw_text=raw_text,
        crop_paths=crop_paths,
        cell_crop_paths=cell_crop_paths,
        ocr_results=ocr_results,
        review_flags=review_flags,
    )


def _gray_array(image: object) -> np.ndarray:
    gray = np.asarray(image)
    if gray.ndim == 3:
        gray = gray.mean(axis=2)
    return gray.astype(np.uint8, copy=False)


def _read_roll_from_cells(
    backend: RollOcrBackend,
    cell_paths: list[Path],
    program: str | None,
    valid_rolls: set[str] | None = None,
) -> RollOcrResult:
    expected_count = _expected_digit_count(program)
    if expected_count is None or len(cell_paths) != expected_count:
        return RollOcrResult("", confidence=0.0, raw={"reason": "missing exact cell geometry"})

    def read_cell(cell_path: Path) -> RollOcrResult:
        if hasattr(backend, "read_digit"):
            return backend.read_digit(cell_path)  # type: ignore[attr-defined]
        return backend.read_roll(cell_path, program=program)

    if getattr(backend, "parallel_cell_reads", False) and len(cell_paths) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(cell_paths))) as executor:
            results = list(executor.map(read_cell, cell_paths))
    else:
        results = [read_cell(cell_path) for cell_path in cell_paths]

    digits = ""
    confidences: list[float] = []
    cell_payloads: list[dict[str, object]] = []
    for result in results:
        digit = _first_digit(result.text)
        digits += digit or "?"
        if result.confidence is not None:
            confidences.append(float(result.confidence))
        cell_payloads.append(_ocr_result_payload(result))

    if "?" in digits:
        return RollOcrResult(digits, confidence=0.0, raw={"cells": cell_payloads})
    text = _format_roll_digits(program, digits)
    confidence = float(np.median(confidences)) if confidences else 0.62
    return RollOcrResult(text, confidence=confidence, raw={"cells": cell_payloads})


def _ocr_result_payload(result: RollOcrResult) -> dict[str, object]:
    return {
        "text": result.text,
        "confidence": result.confidence,
        "raw": result.raw,
    }


def _provider_from_raw(result: RollOcrResult) -> str:
    raw = result.raw if isinstance(result.raw, dict) else {}
    provider = raw.get("provider")
    return str(provider) if provider else "unknown"


def _choose_digit_result(results: list[RollOcrResult], warnings: list[str] | None = None) -> RollOcrResult:
    candidates: list[dict[str, object]] = []
    votes: dict[str, list[float]] = {}
    for result in results:
        digit = _first_digit(result.text)
        confidence = _clamp_confidence(result.confidence) or 0.0
        candidates.append(
            {
                "text": result.text,
                "digit": digit,
                "confidence": confidence,
                "provider": _provider_from_raw(result),
                "raw": result.raw,
            }
        )
        if digit is not None:
            votes.setdefault(digit, []).append(confidence)

    if not votes:
        return RollOcrResult(
            "",
            confidence=0.0,
            raw={"provider": "local_ensemble", "warnings": warnings or [], "candidates": candidates},
        )

    ranked = sorted(
        votes.items(),
        key=lambda item: (len(item[1]), float(np.median(item[1])), max(item[1])),
        reverse=True,
    )
    digit, confidences = ranked[0]
    confidence = _digit_vote_confidence(confidences, total_attempts=max(1, len(results)))
    if len(ranked) > 1:
        runner_up = ranked[1]
        if len(runner_up[1]) == len(confidences):
            confidence = min(confidence, 0.58)
        else:
            confidence = min(confidence, 0.72)
    return RollOcrResult(
        digit,
        confidence=confidence,
        raw={
            "provider": "local_ensemble",
            "warnings": warnings or [],
            "votes": votes,
            "candidates": candidates,
        },
    )


def _ensemble_confidence(
    candidates: list[dict[str, object]],
    selected_text: str,
    program: str | None,
) -> float:
    selected_roll = normalize_handwritten_roll_text(selected_text, program)
    if selected_roll is None:
        return 0.0
    agreeing = [
        _clamp_confidence(candidate.get("confidence")) or 0.0
        for candidate in candidates
        if normalize_handwritten_roll_text(str(candidate.get("text", "")), program) == selected_roll
    ]
    conflicting = [
        candidate
        for candidate in candidates
        if normalize_handwritten_roll_text(str(candidate.get("text", "")), program) not in {None, selected_roll}
    ]
    if not agreeing:
        return 0.0
    confidence = max(agreeing)
    if len(agreeing) > 1:
        confidence = min(0.99, 0.08 + confidence)
    if conflicting:
        confidence = min(confidence, 0.58)
    return confidence


def _combined_confidence(first: float | None, second: float | None) -> float | None:
    values = [value for value in (first, second) if value is not None]
    if not values:
        return None
    return max(values)


def _digit_vote_confidence(confidences: list[float], total_attempts: int) -> float:
    vote_fraction = len(confidences) / max(total_attempts, 1)
    median_confidence = float(np.median(confidences)) if confidences else 0.0
    return max(0.0, min(0.99, 0.30 * vote_fraction + 0.70 * median_confidence))


def _prepare_ocr_image(image: Image.Image) -> Image.Image:
    return _prepare_ocr_variants(image)[0][1]


def _prepare_ocr_variants(image: Image.Image) -> list[tuple[str, Image.Image]]:
    gray = ImageOps.autocontrast(image.convert("L"))
    try:
        import cv2
    except ImportError:
        resized = gray.resize((gray.width * 4, gray.height * 4), Image.Resampling.LANCZOS)
        return [("resized_autocontrast", ImageOps.autocontrast(resized))]

    array = np.asarray(gray)
    resized = cv2.resize(array, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    blur = cv2.GaussianBlur(resized, (3, 3), 0)
    adaptive = cv2.adaptiveThreshold(
        blur,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        12,
    )
    _threshold, otsu = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    normalized = cv2.normalize(blur, None, 0, 255, cv2.NORM_MINMAX)
    variants = {
        "adaptive_clean": _remove_box_lines(adaptive),
        "otsu_clean": _remove_box_lines(otsu),
        "gray_clean": _remove_box_lines(normalized),
    }
    return [(name, Image.fromarray(value, mode="L")) for name, value in variants.items()]


def _prepare_digit_ocr_variants(image: Image.Image) -> list[tuple[str, Image.Image]]:
    """Prepare a boxed digit without morphology that can erase straight strokes."""
    gray = ImageOps.autocontrast(image.convert("L"))
    width, height = gray.size
    margin_x = max(1, round(width * 0.08))
    margin_y = max(1, round(height * 0.08))
    if width > 2 * margin_x and height > 2 * margin_y:
        gray = gray.crop((margin_x, margin_y, width - margin_x, height - margin_y))
    pil_resized = ImageOps.expand(
        ImageOps.autocontrast(
            gray.resize((gray.width * 4, gray.height * 4), Image.Resampling.LANCZOS)
        ),
        border=16,
        fill=255,
    )
    return [("pil_gray", pil_resized)]


def _remove_box_lines(binary_or_gray: np.ndarray) -> np.ndarray:
    try:
        import cv2
    except ImportError:
        return binary_or_gray

    source = binary_or_gray.astype(np.uint8, copy=False)
    if source.ndim == 3:
        source = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
    if np.unique(source).size > 2:
        _threshold, binary = cv2.threshold(source, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    else:
        binary = source
    ink = 255 - binary
    h, w = ink.shape[:2]
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(18, w // 14), 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(18, h // 2)))
    lines = cv2.morphologyEx(ink, cv2.MORPH_OPEN, horizontal_kernel)
    lines = cv2.bitwise_or(lines, cv2.morphologyEx(ink, cv2.MORPH_OPEN, vertical_kernel))
    cleaned_ink = cv2.bitwise_and(ink, cv2.bitwise_not(lines))
    cleaned = 255 - cleaned_ink
    return cv2.copyMakeBorder(cleaned, 16, 16, 16, 16, cv2.BORDER_CONSTANT, value=255)


def _expected_digit_count(program: str | None) -> int | None:
    expected = (program or "").upper()
    if expected == "BTECH":
        return 7
    if expected in {"MTECH", "PHD"}:
        return 5
    return None


def _format_roll_digits(program: str | None, digits: str) -> str:
    expected = (program or "").upper()
    if expected == "MTECH":
        return f"MT{digits}"
    if expected == "PHD":
        return f"PHD{digits}"
    return digits


def _segment_digit_cells(image: Image.Image, count: int) -> list[Image.Image]:
    width, height = image.size
    cells: list[Image.Image] = []
    for index in range(count):
        x0 = round(index * width / count)
        x1 = round((index + 1) * width / count)
        cell = image.crop((x0, 0, x1, height))
        cw, ch = cell.size
        cells.append(
            cell.crop(
                (
                    round(cw * 0.13),
                    round(ch * 0.10),
                    round(cw * 0.87),
                    round(ch * 0.90),
                )
            )
        )
    return cells


def _cell_crop_boxes_px(
    field: dict,
    image_shape: tuple[int, ...],
    dpi: float,
    padding_mm: float,
) -> list[tuple[int, int, int, int]]:
    h, w = image_shape[:2]
    boxes = []
    for index in range(int(field["cells"])):
        x0, y0 = mm_to_px(
            field["x_mm"] + index * field["cell_pitch_mm"] - padding_mm,
            field["y_mm"] - padding_mm,
            dpi,
        )
        width_px = round((field["cell_width_mm"] + 2 * padding_mm) * px_per_mm(dpi))
        height_px = round((field["height_mm"] + 2 * padding_mm) * px_per_mm(dpi))
        boxes.append((max(0, x0), max(0, y0), min(w, x0 + width_px), min(h, y0 + height_px)))
    return boxes


def _first_digit(text: object) -> str | None:
    match = re.search(r"\d", str(text))
    return match.group(0) if match else None


def _best_ocr_candidate(candidates: list[dict[str, object]], program: str | None) -> dict[str, object]:
    def score(candidate: dict[str, object]) -> tuple[int, float, int]:
        text = str(candidate.get("text", ""))
        normalized = normalize_handwritten_roll_text(text, program)
        confidence = _clamp_confidence(candidate.get("confidence")) or 0.0
        return (1 if normalized else 0, confidence, len(text))

    return max(candidates, key=score) if candidates else {"text": "", "confidence": 0.0}


def _roll_write_in_fields(manifest: dict, page_index: int) -> list[dict]:
    return [
        field
        for field in manifest.get("write_in_fields", [])
        if field.get("page", 1) == page_index and field.get("name") == "roll_number"
    ]


def _field_programs(field: dict) -> tuple[str, ...]:
    programs = field.get("programs")
    if isinstance(programs, list) and programs:
        return tuple(str(program).upper() for program in programs)
    program = str(field.get("program") or "").upper()
    return (program,) if program else ()


def _crop_box_px(
    field: dict,
    image_shape: tuple[int, ...],
    dpi: float,
    padding_mm: float,
) -> tuple[int, int, int, int]:
    scale = px_per_mm(dpi)
    x0, y0 = mm_to_px(field["x_mm"] - padding_mm, field["y_mm"] - padding_mm, dpi)
    width_px = round((field["width_mm"] + 2 * padding_mm) * scale)
    height_px = round((field["height_mm"] + 2 * padding_mm) * scale)
    x1 = x0 + width_px
    y1 = y0 + height_px
    h, w = image_shape[:2]
    return max(0, x0), max(0, y0), min(w, x1), min(h, y1)


def _read_continuation_program(
    gray: np.ndarray,
    manifest: dict,
    dpi: float,
    page_index: int,
) -> tuple[str | None, dict[str, dict[str, float]], list[str]]:
    choices = [
        choice for choice in manifest.get("continuation_program_choices", []) if choice.get("page", 1) == page_index
    ]
    if not choices:
        return None, {}, [f"page {page_index} has no continuation program selector in manifest"]

    scale = px_per_mm(dpi)
    radius_px = max(1, round(manifest["bubble_sample_radius_mm"] * scale))
    signals: dict[str, dict[str, float]] = {}
    for choice in choices:
        program = str(choice["program"]).upper()
        cx, cy = mm_to_px(choice["x_mm"], choice["y_mm"], dpi)
        fill = student_mark_fill_ratio(gray, cx, cy, radius_px)
        ink = ink_density(gray, cx, cy, radius_px)
        signals[program] = {"fill": fill, "ink": ink}

    filled = [program for program, signal in signals.items() if signal["fill"] >= DEFAULT_FILL_THRESHOLD]
    if len(filled) == 1:
        return filled[0], signals, []
    if len(filled) > 1:
        return None, signals, [f"page {page_index} has multiple continuation program bubbles: {filled}"]

    marked = [
        program
        for program, signal in signals.items()
        if signal["fill"] >= DEFAULT_AMBIGUOUS_FLOOR or signal["ink"] >= DEFAULT_INK_FLOOR
    ]
    if marked:
        return None, signals, [f"page {page_index} continuation program selector is ambiguous: {marked}"]
    return None, signals, [f"page {page_index} continuation program selector is blank"]


def _roll_confidence(confidence: float | None, review_flags: list[str]) -> str:
    value = confidence if confidence is not None else 0.65
    if review_flags:
        return "medium" if value >= 0.82 else "low"
    if value >= 0.82:
        return "high"
    if value >= 0.60:
        return "medium"
    return "low"


def _joined_raw_text(results: dict[str, dict[str, object]]) -> str | None:
    parts = [str(result.get("text", "")).strip() for result in results.values()]
    text = " | ".join(part for part in parts if part)
    return text or None


def _clamp_confidence(value: object) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, numeric))
