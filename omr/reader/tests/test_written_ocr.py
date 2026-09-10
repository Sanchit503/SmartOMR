from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PIL import Image, ImageDraw

import omr.reader.written_ocr as written_ocr_module
from omr.models import WrittenCrop
from omr.reader.written_ocr import (
    EnsembleWrittenOcr,
    LineOcrResult,
    read_written_answer_texts,
    save_written_line_crops,
)


class FakeWrittenOcr:
    provider = "fake_written"

    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)

    def read_line(self, line_path: Path) -> LineOcrResult:
        text = self.lines.pop(0) if self.lines else ""
        return LineOcrResult(text=text, confidence=0.91, raw={"path": str(line_path)})


class RecordingWrittenOcr:
    provider = "recording_written"

    def __init__(self, results: list[LineOcrResult]) -> None:
        self.results = list(results)
        self.paths: list[Path] = []

    def read_line(self, line_path: Path) -> LineOcrResult:
        self.paths.append(line_path)
        return self.results.pop(0) if self.results else LineOcrResult(text="", confidence=0.0)


def _written_crop(tmp_path: Path) -> WrittenCrop:
    image_path = tmp_path / "Q11.png"
    image = Image.new("L", (520, 96), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 519, 95], outline=0, width=1)
    draw.line([0, 48, 519, 48], fill=0, width=1)
    draw.line([(34, 22), (52, 14), (72, 28), (92, 16), (116, 26)], fill=0, width=4)
    draw.line([(44, 70), (66, 60), (86, 76), (108, 64), (132, 74)], fill=0, width=4)
    image.save(image_path)
    return WrittenCrop(
        q_no=11,
        page=1,
        crop_path=str(image_path),
        max_marks=2,
        lines=2,
        x_mm=10,
        y_mm=100,
        width_mm=180,
        height_mm=14,
    )


def _written_crop_with_ink(tmp_path: Path, *, blank_ink: bool = False, lines: int = 2) -> WrittenCrop:
    crop = _written_crop(tmp_path)
    ink_path = tmp_path / "Q11_ink.png"
    image = Image.new("L", (520, 96), 255)
    if not blank_ink:
        draw = ImageDraw.Draw(image)
        draw.line([(34, 22), (52, 14), (72, 28), (92, 16), (116, 26)], fill=0, width=4)
        if lines > 1:
            draw.line([(44, 70), (66, 60), (86, 76), (108, 64), (132, 74)], fill=0, width=4)
    image.save(ink_path)
    return replace(crop, lines=lines, ocr_crop_path=str(ink_path))


def test_written_answer_line_crops_are_saved(tmp_path: Path):
    crop = _written_crop(tmp_path)

    line_paths = save_written_line_crops(crop, tmp_path / "lines")

    assert len(line_paths) == 2
    assert all(path.exists() for path in line_paths)


def test_written_answer_ocr_reads_lines_and_combines_text(tmp_path: Path):
    crop = _written_crop(tmp_path)

    reads = read_written_answer_texts(
        [crop],
        tmp_path / "ocr",
        FakeWrittenOcr(["first line", "second line"]),
    )

    read = reads[11]
    assert read.text == "first line\nsecond line"
    assert read.confidence == "high"
    assert len(read.line_results) == 2
    assert read.line_results[0].provider == "fake_written"


def test_written_ocr_uses_raw_crop_not_degraded_ink_for_recognition(tmp_path: Path):
    crop = _written_crop_with_ink(tmp_path)
    backend = RecordingWrittenOcr(
        [
            LineOcrResult("first line", confidence=0.92),
            LineOcrResult("second line", confidence=0.91),
        ]
    )

    reads = read_written_answer_texts([crop], tmp_path / "ocr", backend)

    read = reads[11]
    assert read.crop_path == crop.crop_path
    assert read.analysis_crop_path == crop.ocr_crop_path
    assert read.recognition_policy == "raw_htr_first"
    assert read.text == "first line\nsecond line"
    assert len(backend.paths) == 2
    assert all("_raw_line_" in str(path) for path in backend.paths)
    assert all("ink" not in str(path) for path in backend.paths)
    assert read.line_results[0].raw["selected_variant"] == "raw"


def test_written_ocr_retries_light_raw_variant_only_when_raw_is_weak(tmp_path: Path):
    crop = _written_crop_with_ink(tmp_path, lines=1)
    backend = RecordingWrittenOcr(
        [
            LineOcrResult("Bawma", confidence=0.31),
            LineOcrResult("Bauma is equal to Dwarf", confidence=0.93),
        ]
    )

    reads = read_written_answer_texts([crop], tmp_path / "ocr", backend)

    assert reads[11].text == "Bauma is equal to Dwarf"
    assert len(backend.paths) == 2
    assert "_raw_line_1" in str(backend.paths[0])
    assert "_light_line_1" in str(backend.paths[1])
    assert all("ink" not in str(path) for path in backend.paths)


def test_blank_detection_uses_ink_crop_without_calling_htr(tmp_path: Path):
    crop = _written_crop_with_ink(tmp_path, blank_ink=True, lines=1)
    backend = RecordingWrittenOcr([LineOcrResult("should not run", confidence=0.99)])

    reads = read_written_answer_texts([crop], tmp_path / "ocr", backend)

    assert reads[11].text == ""
    assert backend.paths == []
    assert reads[11].line_results[0].raw["blank_detection_variant"] == "ink_analysis"


def test_best_written_ocr_uses_htr_without_tesseract_voting(monkeypatch):
    calls: list[str] = []

    class FakeTrOCR:
        provider = "trocr"

        def __init__(self, *args, **kwargs) -> None:
            calls.append("trocr")

        def read_line(self, line_path: Path) -> LineOcrResult:
            return LineOcrResult("text", confidence=0.9)

    class ForbiddenTesseract:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("Tesseract should not be used by best handwritten OCR")

    monkeypatch.setattr(written_ocr_module, "TrOCRWrittenOcr", FakeTrOCR)
    monkeypatch.setattr(written_ocr_module, "LocalTesseractWrittenOcr", ForbiddenTesseract)

    backend = written_ocr_module.build_written_ocr_backend("best")

    assert backend.provider == "trocr"
    assert calls == ["trocr"]


def test_ensemble_written_ocr_prefers_cleaner_candidate(tmp_path: Path):
    line_path = tmp_path / "line.png"
    Image.new("L", (160, 40), 255).save(line_path)
    noisy = FakeWrittenOcr(["____ ==== ----"])
    noisy.provider = "noisy"
    clean = FakeWrittenOcr(["hello world"])
    clean.provider = "clean"
    backend = EnsembleWrittenOcr([noisy, clean])

    result = backend.read_line(line_path)

    assert result.text == "hello world"
    assert result.raw["selected_provider"] == "clean"
