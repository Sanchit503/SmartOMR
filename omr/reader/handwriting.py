"""Continuation-page handwritten roll-number support.

Handwriting is treated as an identity-risk signal, not a guaranteed truth.
The reader crops the roll strip declared in the manifest, optionally sends
that crop to a configured OCR backend, validates the text against the
expected roll format, and returns low confidence when evidence is weak.
"""
from __future__ import annotations

import os
import re
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

import numpy as np
from PIL import Image, ImageOps

from omr.contracts.geometry import mm_to_px, px_per_mm
from omr.grading.bubbles import ink_density, student_mark_fill_ratio
from omr.grading.mcq import DEFAULT_AMBIGUOUS_FLOOR, DEFAULT_FILL_THRESHOLD, DEFAULT_INK_FLOOR
from omr.identity_evidence import (
    BLOCK,
    CELL_ROLL_UNREADABLE,
    EMPTY_FIELD,
    INFO,
    LOW_CELL_CONFIDENCE,
    PROGRAM_SELECTOR_AMBIGUOUS,
    PROGRAM_SELECTOR_BLANK,
    ROLL_NOT_IN_ROSTER,
    STRIP_DISAGREES,
    WARN,
    evidence,
)
from omr.models import HandwrittenRollRead
from omr.reader.enhancement import enhance_faint_ink


DEFAULT_RESNET_ROLL_MODEL = Path("data/models/roll_digit_resnet_omr_finetuned.pt")
DEFAULT_RESNET_ROLL_ENSEMBLE_MANIFEST = Path("data/models/roll_digit_ensemble_7/ensemble_manifest.json")
# Cell crops contain 1.5 mm context around the printed box. A 20% inset of
# that padded crop corresponds to about a 12% inset of the actual cell.
BLANK_CELL_INSET_FRACTION = 0.20
BLANK_CELL_INK_FRACTION = 0.005
BLANK_CELL_DARKNESS_DELTA = 25


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
    fast_cell_first = True

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
        tensor = _resnet_digit_tensor(image, self.torch).unsqueeze(0)
        with self.torch.no_grad():
            probabilities = self.torch.softmax(self.model(tensor), dim=1)[0]
        confidence, index = self.torch.max(probabilities, dim=0)
        label = self.labels[int(index.item())]
        digit = "" if label == "blank" else label
        return RollOcrResult(digit, confidence=float(confidence.item()), raw={"provider": self.provider, "label": label})


def _resnet_digit_tensor(image: Image.Image, torch: object):
    """Match the image preprocessing used by the roll-digit trainer."""
    image = ImageOps.autocontrast(image.convert("L"))
    image = ImageOps.pad(image, (64, 64), color=255)
    array = np.asarray(image, dtype=np.float32) / 255.0
    return torch.from_numpy((1.0 - array - 0.15) / 0.35).unsqueeze(0)


class LocalResnetProbabilityEnsembleRollOcr:
    """Validation-weighted probability ensemble of fine-tuned roll-digit ResNets."""

    provider = "local_resnet_probability_ensemble"
    # Exact manifest cell crops are the primary ResNet input. Whole-strip
    # segmentation is retained only when the cells are not independently strong.
    fast_cell_first = True

    def __init__(self, manifest_path: str | Path) -> None:
        try:
            import torch
            from omr.datasets.train_roll_digit_resnet import LABELS, SmallRollDigitResNet
        except ImportError as exc:
            raise RuntimeError("PyTorch is required to use the roll-digit ensemble") from exc

        self.manifest_path = Path(manifest_path)
        if not self.manifest_path.is_file():
            raise FileNotFoundError(f"roll-digit ensemble manifest not found: {self.manifest_path}")
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        members = manifest.get("members")
        if manifest.get("kind") != "roll_digit_resnet_probability_ensemble" or not isinstance(members, list) or len(members) < 2:
            raise ValueError(f"invalid roll-digit ensemble manifest: {self.manifest_path}")

        self.torch = torch
        self.labels = LABELS
        self.models = []
        self.member_paths: list[str] = []
        self.members: list[dict[str, object]] = []
        weights: list[float] = []
        for member in members:
            if not isinstance(member, dict):
                raise ValueError(f"invalid member in ensemble manifest: {self.manifest_path}")
            model_path = _resolve_ensemble_model_path(self.manifest_path, member.get("model_path"))
            actual_hash = _file_sha256(model_path)
            expected_hash = str(member.get("checkpoint_sha256") or "")
            if expected_hash and actual_hash != expected_hash:
                raise ValueError(f"roll-digit checkpoint hash mismatch: {model_path}")
            payload = torch.load(model_path, map_location="cpu")
            checkpoint_labels = payload.get("labels") if isinstance(payload, dict) else None
            if checkpoint_labels is not None and list(checkpoint_labels) != list(LABELS):
                raise ValueError(f"roll-digit checkpoint labels do not match runtime labels: {model_path}")
            state = payload.get("model_state", payload) if isinstance(payload, dict) else payload
            model = SmallRollDigitResNet(num_classes=len(LABELS))
            model.load_state_dict(state)
            model.eval()
            self.models.append(model)
            self.member_paths.append(str(model_path))
            accuracy = max(0.0, float(member.get("best_val_accuracy") or 0.0))
            weights.append(accuracy)
            self.members.append(
                {
                    "member": member.get("member"),
                    "seed": member.get("seed"),
                    "best_val_accuracy": accuracy,
                    "checkpoint_path": str(model_path),
                    "checkpoint_sha256": actual_hash,
                }
            )
        if not any(weights):
            weights = [1.0] * len(self.models)
        self.weights = np.asarray(weights, dtype=np.float32)
        self.weights /= self.weights.sum()
        self.aggregation = str(manifest.get("aggregation") or "validation-weighted probability average")

    def provenance(self) -> dict[str, object]:
        return {
            "kind": "roll_digit_resnet_probability_ensemble",
            "manifest_path": str(self.manifest_path),
            "manifest_sha256": _file_sha256(self.manifest_path),
            "member_count": len(self.models),
            "aggregation": self.aggregation,
            "members": self.members,
        }

    def read_roll(self, crop_path: Path, program: str | None = None, valid_rolls: set[str] | None = None) -> RollOcrResult:
        expected_digits = _expected_digit_count(program)
        if expected_digits is None:
            return RollOcrResult("", confidence=0.0, raw={"reason": "program_required_for_resnet_ensemble"})
        image = Image.open(crop_path).convert("L")
        reads = self._read_digit_images(_segment_digit_cells(image, expected_digits))
        digits = "".join(_first_digit(read.text) or "?" for read in reads)
        confidence = min((float(read.confidence or 0.0) for read in reads), default=0.0) if "?" not in digits else 0.0
        return RollOcrResult(
            _format_roll_digits(program, digits),
            confidence=confidence,
            raw={
                "provider": self.provider,
                "ensemble_manifest": str(self.manifest_path),
                "member_count": len(self.models),
                "aggregation": self.aggregation,
                "cells": [_ocr_result_payload(read) for read in reads],
            },
        )

    def read_digit(self, crop_path: Path) -> RollOcrResult:
        return self.read_digit_image(Image.open(crop_path).convert("L"))

    def read_digit_image(self, image: Image.Image) -> RollOcrResult:
        return self._read_digit_images([image])[0]

    def _read_digit_images(self, images: list[Image.Image]) -> list[RollOcrResult]:
        combined = self._probabilities(images)
        # Retry only weak cells. Agreement can support the original read;
        # disagreement is deliberately kept as a review signal.
        weak_indexes: list[int] = []
        for position, row in enumerate(combined):
            ranked = np.sort(row)
            margin = float(ranked[-1] - ranked[-2]) if len(ranked) > 1 else 1.0
            if float(row.max()) < 0.86 or margin < 0.18:
                weak_indexes.append(position)
        variants: dict[int, np.ndarray] = {}
        if weak_indexes:
            retry_images = [Image.fromarray(enhance_faint_ink(images[position]), mode="L") for position in weak_indexes]
            for position, row in zip(weak_indexes, self._probabilities(retry_images)):
                variants[position] = row

        reads: list[RollOcrResult] = []
        for position, original in enumerate(combined):
            row = original.copy()
            variant = variants.get(position)
            enhancement = "not_needed"
            disagreement = False
            if variant is not None:
                if int(np.argmax(original)) == int(np.argmax(variant)):
                    row = original * 0.65 + variant * 0.35
                    enhancement = "contrast_retry_agreed"
                else:
                    enhancement = "contrast_retry_disagreed"
                    disagreement = True
            index = int(np.argmax(row))
            label = self.labels[index]
            ranked = np.sort(row)
            runner_up = float(ranked[-2]) if len(ranked) > 1 else 0.0
            confidence = float(row[index])
            reads.append(
                RollOcrResult(
                    "" if label == "blank" else label,
                    confidence=confidence,
                    raw={
                        "provider": self.provider,
                        "label": label,
                        "member_count": len(self.models),
                        "top_probability": float(row[index]),
                        "runner_up_probability": runner_up,
                        "probability_margin": float(row[index]) - runner_up,
                        "class_probabilities": {
                            label: round(float(probability), 6)
                            for label, probability in zip(self.labels, row)
                        },
                        "enhancement": enhancement,
                        "enhancement_disagreement": disagreement,
                    },
                )
            )
        return reads

    def _probabilities(self, images: list[Image.Image]) -> np.ndarray:
        tensors = self.torch.stack([_resnet_digit_tensor(image, self.torch) for image in images])
        combined = None
        with self.torch.no_grad():
            for weight, model in zip(self.weights, self.models):
                probabilities = self.torch.softmax(model(tensors), dim=1).cpu().numpy()
                combined = probabilities * float(weight) if combined is None else combined + probabilities * float(weight)
        assert combined is not None
        return combined


def _resolve_ensemble_model_path(manifest_path: Path, value: object) -> Path:
    if not value:
        raise ValueError(f"ensemble member has no model_path: {manifest_path}")
    candidate = Path(str(value))
    if candidate.is_file():
        return candidate
    relative_to_manifest = manifest_path.parent / candidate
    if relative_to_manifest.is_file():
        return relative_to_manifest
    raise FileNotFoundError(f"fine-tuned ensemble member not found: {candidate}")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    ensemble_manifest_path: str | Path | None = None,
) -> RollOcrBackend | None:
    selected = (provider or "none").strip().lower()
    if selected in {"", "none", "off", "disabled"}:
        return None
    if selected == "local":
        backends: list[RollOcrBackend] = []
        warnings: list[str] = []
        resnet_path = resnet_model_path or os.environ.get("SMARTOMR_ROLL_RESNET_MODEL")
        manifest_path = ensemble_manifest_path or os.environ.get("SMARTOMR_ROLL_ENSEMBLE_MANIFEST")
        if manifest_path is None and resnet_path is None and digit_model_path is None:
            manifest_path = DEFAULT_RESNET_ROLL_ENSEMBLE_MANIFEST
        if manifest_path and Path(manifest_path).is_file():
            try:
                backends.append(LocalResnetProbabilityEnsembleRollOcr(manifest_path))
            except (RuntimeError, ValueError, FileNotFoundError) as exc:
                warnings.append(str(exc))
        if not backends and resnet_path is None and digit_model_path is None:
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
                "No local roll digit ensemble or fine-tuned ResNet checkpoint is available. "
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
    cell_padding_mm: float = 1.5,
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
    return replace(
        read,
        evidence_flags=_structured_handwritten_evidence(
            read,
            selector_signals=selector_signals,
            valid_rolls=valid_rolls,
        ),
    )


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
    read = _read_write_in_roll_number(
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
    return replace(
        read,
        evidence_flags=_structured_handwritten_evidence(read, valid_rolls=valid_rolls),
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

    if selected_program and selected_program in crop_paths:
        programs_to_read = [selected_program]
    elif selector_flags:
        # MTech and PhD share the five-digit layout. With no reliable selector,
        # only the distinct BTech field can be used as a possible exact-anchor
        # match; the grouping layer never infers MTech/PhD from this path.
        programs_to_read = ["BTECH"] if "BTECH" in crop_paths else []
        if programs_to_read:
            review_flags.append("continuation selector unresolved; read BTECH field only")
    else:
        programs_to_read = list(crop_paths)
    candidates: list[tuple[str, str, float | None, str, str]] = []
    empty_field = False
    for program in programs_to_read:
        crop_path = Path(crop_paths[program])
        cell_result = _read_roll_from_cells(ocr_backend, [Path(path) for path in cell_crop_paths.get(program, [])], program)
        cell_normalized = normalize_handwritten_roll_text(cell_result.text, program=program)
        cell_raw = cell_result.raw if isinstance(cell_result.raw, dict) else {}
        empty_field = empty_field or bool(cell_raw.get("empty_field"))
        # Strip segmentation is deliberately diagnostic only. It may be useful
        # to a reviewer, but it cannot veto a literal exact-cell result.
        strip_result = (RollOcrResult("", confidence=0.0, raw={"reason": "empty_field"})
                        if cell_raw.get("empty_field") else
                        ocr_backend.read_roll(crop_path, program=program, valid_rolls=valid_rolls))
        strip_normalized = normalize_handwritten_roll_text(strip_result.text, program=program)
        ocr_results[program] = {
            "strip": _ocr_result_payload(strip_result),
            "cells": _ocr_result_payload(cell_result),
            "text": cell_result.text or strip_result.text,
            "normalized_roll": cell_normalized,
            "confidence": _combined_confidence(cell_result.confidence, strip_result.confidence),
            "cell_crop_paths": cell_crop_paths.get(program, []),
        }
        if cell_normalized:
            candidates.append((program, cell_normalized, cell_result.confidence, cell_result.text, "cells"))
        if cell_normalized and strip_normalized and cell_normalized != strip_normalized:
            review_flags.append(f"STRIP_DISAGREES: {program} cells={cell_normalized}, strip={strip_normalized}")
        if not cell_normalized and strip_normalized:
            review_flags.append("whole-strip OCR is diagnostic only; incomplete digit cells cannot establish identity")

    if empty_field:
        review_flags.append("EMPTY_FIELD: handwritten roll field has blank digit cells")
        candidates = []

    # The roster can help a professor inspect a likely correction, but it must
    # never replace the literal OCR result or establish page ownership. The
    # grouping workflow consumes only the literal read above.
    if valid_rolls:
        suggestion = _roster_suggestion_from_cell_probabilities(ocr_results, valid_rolls)
        if suggestion is not None and not empty_field:
            ocr_results["_roster_suggestion"] = suggestion
            literal_rolls = {roll for _program, roll, _confidence, _text, _source in candidates}
            if bool(suggestion["clear"]) and suggestion["roll_no"] not in literal_rolls:
                review_flags.append(
                    "roster suggests "
                    f"{suggestion['roll_no']} from ResNet cell probabilities "
                    f"(score {suggestion['score']:.2f}, margin {suggestion['margin']:.2f}); "
                    "literal OCR remains unchanged and manual review is required"
                )

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
    if valid_rolls is not None and roll_no not in valid_rolls:
        review_flags.append(f"handwritten roll OCR read {roll_no}, which is not present in the roster")
        confidence = "low"
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


def _selector_state_from_signals(signals: dict[str, dict[str, float]] | None) -> str:
    values = list((signals or {}).values())
    if not values:
        return "unavailable"
    filled = [signal for signal in values if float(signal.get("fill") or 0.0) >= DEFAULT_FILL_THRESHOLD]
    if len(filled) == 1:
        return "clear"
    if len(filled) > 1:
        return "ambiguous"
    marked = [
        signal
        for signal in values
        if float(signal.get("fill") or 0.0) >= DEFAULT_AMBIGUOUS_FLOOR
        or float(signal.get("ink") or 0.0) >= DEFAULT_INK_FLOOR
    ]
    return "ambiguous" if marked else "blank"


def _structured_handwritten_evidence(
    read: HandwrittenRollRead,
    *,
    selector_signals: dict[str, dict[str, float]] | None = None,
    valid_rolls: set[str] | None = None,
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for program, payload in read.ocr_results.items():
        if str(program).startswith("_") or not isinstance(payload, dict):
            continue
        cells = payload.get("cells")
        strip = payload.get("strip")
        cells = cells if isinstance(cells, dict) else {}
        strip = strip if isinstance(strip, dict) else {}
        cell_raw = cells.get("raw") if isinstance(cells.get("raw"), dict) else {}
        cell_roll = normalize_handwritten_roll_text(str(cells.get("text") or ""), program=program)
        strip_roll = normalize_handwritten_roll_text(str(strip.get("text") or ""), program=program)
        if bool(cell_raw.get("empty_field")):
            items.append(evidence(EMPTY_FIELD, BLOCK, program=program))
        if cell_roll and strip_roll and cell_roll != strip_roll:
            items.append(
                evidence(
                    STRIP_DISAGREES,
                    INFO,
                    program=program,
                    cell_roll=cell_roll,
                    strip_roll=strip_roll,
                )
            )
        try:
            cell_confidence = float(cells.get("confidence") or 0.0)
        except (TypeError, ValueError):
            cell_confidence = 0.0
        if cell_roll and cell_confidence < 0.80:
            items.append(
                evidence(
                    LOW_CELL_CONFIDENCE,
                    WARN,
                    program=program,
                    roll_no=cell_roll,
                    confidence=round(cell_confidence, 6),
                    threshold=0.80,
                )
            )
        if not cell_roll and not bool(cell_raw.get("empty_field")):
            items.append(evidence(CELL_ROLL_UNREADABLE, BLOCK, program=program))

    selector_state = _selector_state_from_signals(selector_signals)
    if selector_state == "blank":
        severity = WARN if read.program == "BTECH" else BLOCK
        items.append(evidence(PROGRAM_SELECTOR_BLANK, severity, program=read.program or ""))
    elif selector_state == "ambiguous":
        items.append(evidence(PROGRAM_SELECTOR_AMBIGUOUS, BLOCK, program=read.program or ""))
    if read.roll_no and valid_rolls is not None and read.roll_no not in valid_rolls:
        items.append(evidence(ROLL_NOT_IN_ROSTER, BLOCK, roll_no=read.roll_no))
    return items


def _roster_suggestion_from_cell_probabilities(
    ocr_results: dict[str, dict[str, object]],
    valid_rolls: set[str],
) -> dict[str, object] | None:
    """Rank roster values for review without changing an OCR prediction.

    This is deliberately an advisory function. It is useful when a student
    made a one-digit error, but acceptance would make the roster a hidden OCR
    decoder and could attach another student's page.
    """
    ranked: list[tuple[float, str, str]] = []
    for program, payload in ocr_results.items():
        if program.startswith("_"):
            continue
        cell_payload = payload.get("cells")
        if not isinstance(cell_payload, dict):
            continue
        raw = cell_payload.get("raw")
        if not isinstance(raw, dict) or not isinstance(raw.get("cells"), list):
            continue
        probability_rows: list[dict[str, float]] = []
        for cell in raw["cells"]:
            if not isinstance(cell, dict) or not isinstance(cell.get("raw"), dict):
                probability_rows = []
                break
            probabilities = cell["raw"].get("class_probabilities")
            if not isinstance(probabilities, dict):
                probability_rows = []
                break
            probability_rows.append({str(label): float(value) for label, value in probabilities.items()})
        if not probability_rows:
            continue
        for roll_no in valid_rolls:
            labels = _roster_roll_labels(roll_no, program)
            if labels is None or len(labels) != len(probability_rows):
                continue
            score = float(np.exp(np.mean(np.log([max(1e-6, row.get(label, 0.0)) for row, label in zip(probability_rows, labels)]))))
            ranked.append((score, roll_no, program))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    score, roll_no, program = ranked[0]
    runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
    margin = score - runner_up
    return {
        "roll_no": roll_no,
        "program": program,
        "score": round(score, 6),
        "margin": round(margin, 6),
        "clear": score >= 0.62 and margin >= 0.12,
        "candidate_count": len(ranked),
        "policy": "review_only_literal_ocr_unchanged",
    }


def _roster_roll_labels(roll_no: str, program: str) -> list[str] | None:
    """Return digit labels only for a roster value matching this field."""
    value = normalize_handwritten_roll_text(roll_no, program=program)
    if value is None:
        return None
    normalized_program = program.upper()
    if normalized_program == "BTECH":
        return list(value) if value.isdigit() and len(value) == 7 else None
    if normalized_program == "MTECH" and value.startswith("MT"):
        return list(value[2:])
    if normalized_program == "PHD" and value.startswith("PHD"):
        return list(value[3:])
    return None


def _gray_array(image: object) -> np.ndarray:
    gray = np.asarray(image)
    if gray.ndim == 3:
        gray = gray.mean(axis=2)
    return gray.astype(np.uint8, copy=False)


def _relative_cell_ink(cell_path: Path) -> float:
    """Measure handwritten ink away from the printed box border."""
    gray = np.asarray(Image.open(cell_path).convert("L"), dtype=np.uint8)
    height, width = gray.shape
    inset_y = max(1, round(height * BLANK_CELL_INSET_FRACTION))
    inset_x = max(1, round(width * BLANK_CELL_INSET_FRACTION))
    interior = gray[inset_y : height - inset_y, inset_x : width - inset_x]
    # A locally shifted printed frame can survive the fixed crop inset. Locate
    # all four frame edges before measuring ink; recognition crops are unchanged.
    paper = float(np.percentile(gray, 90))
    dark = gray <= paper - BLANK_CELL_DARKNESS_DELTA
    rows = np.mean(dark, axis=1)
    columns = np.mean(dark, axis=0)
    top = np.flatnonzero(rows[:max(1, round(height * .35))] >= .55)
    bottom = np.flatnonzero(rows[min(height - 1, round(height * .65)):] >= .55)
    left = np.flatnonzero(columns[:max(1, round(width * .35))] >= .55)
    right = np.flatnonzero(columns[min(width - 1, round(width * .65)):] >= .55)
    if all(len(edges) for edges in (top, bottom, left, right)):
        # Use the outermost contiguous bands, not a digit's inner horizontal
        # stroke (notably 7) or vertical stroke (notably 1).
        top_edge, left_edge = int(top[0]), int(left[0])
        bottom_edge = int(bottom[-1]) + round(height * .65)
        right_edge = int(right[-1]) + round(width * .65)
        while top_edge + 1 < height and rows[top_edge + 1] >= .55:
            top_edge += 1
        while left_edge + 1 < width and columns[left_edge + 1] >= .55:
            left_edge += 1
        while bottom_edge > 0 and rows[bottom_edge - 1] >= .55:
            bottom_edge -= 1
        while right_edge > 0 and columns[right_edge - 1] >= .55:
            right_edge -= 1
        y0, y1 = top_edge + 2, bottom_edge - 1
        x0, x1 = left_edge + 2, right_edge - 1
        if y1 - y0 >= height * .30 and x1 - x0 >= width * .30:
            interior = gray[y0:y1, x0:x1]
    if interior.size == 0:
        return 0.0
    paper_level = float(np.percentile(interior, 90))
    return float(np.mean(interior <= paper_level - BLANK_CELL_DARKNESS_DELTA))


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
        relative_ink = _relative_cell_ink(cell_path)
        if relative_ink < BLANK_CELL_INK_FRACTION:
            return RollOcrResult(
                "",
                confidence=1.0,
                raw={"blank_cell": True, "relative_ink": relative_ink},
            )
        if hasattr(backend, "read_digit"):
            read = backend.read_digit(cell_path)  # type: ignore[attr-defined]
        else:
            read = backend.read_roll(cell_path, program=program)
        raw = dict(read.raw) if isinstance(read.raw, dict) else {}
        raw["blank_cell"] = False
        raw["relative_ink"] = relative_ink
        return RollOcrResult(read.text, confidence=read.confidence, raw=raw)

    if getattr(backend, "parallel_cell_reads", False) and len(cell_paths) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(cell_paths))) as executor:
            results = list(executor.map(read_cell, cell_paths))
    else:
        results = [read_cell(cell_path) for cell_path in cell_paths]

    digits = ""
    confidences: list[float] = []
    cell_payloads: list[dict[str, object]] = []
    blank_count = 0
    low_ink_digits: list[str] = []
    for result in results:
        digit = _first_digit(result.text)
        digits += digit or "?"
        if result.confidence is not None:
            confidences.append(float(result.confidence))
        cell_payloads.append(_ocr_result_payload(result))
        raw = result.raw if isinstance(result.raw, dict) else {}
        if bool(raw.get("blank_cell")):
            blank_count += 1
        if digit in {"1", "7"} and float(raw.get("relative_ink", 1.0)) < 0.012:
            low_ink_digits.append(digit)

    empty_field = blank_count >= 2 or (len(low_ink_digits) == len(results) and bool(low_ink_digits))
    raw_payload = {
        "cells": cell_payloads,
        "blank_cell_count": blank_count,
        "empty_field": empty_field,
    }
    if "?" in digits:
        return RollOcrResult(digits, confidence=0.0, raw=raw_payload)
    text = _format_roll_digits(program, digits)
    # One wrong digit changes the entire student identity. Roll confidence is
    # bounded by the weakest cell instead of being hidden by the median.
    confidence = min(confidences) if confidences else 0.0
    return RollOcrResult(text, confidence=confidence, raw=raw_payload)


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
    disagreement = len(ranked) > 1
    return RollOcrResult(
        digit,
        confidence=confidence,
        raw={
            "provider": "local_ensemble",
            "warnings": warnings or [],
            "disagreement": disagreement,
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
    if any(flag.startswith("EMPTY_FIELD") for flag in review_flags):
        return "low"
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
