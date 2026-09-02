from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

from omr.models import WrittenCrop
from omr.reader.written_ocr import LineOcrResult, read_written_answer_texts, save_written_line_crops


class FakeWrittenOcr:
    provider = "fake_written"

    def __init__(self, lines: list[str]) -> None:
        self.lines = list(lines)

    def read_line(self, line_path: Path) -> LineOcrResult:
        text = self.lines.pop(0) if self.lines else ""
        return LineOcrResult(text=text, confidence=0.91, raw={"path": str(line_path)})


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
