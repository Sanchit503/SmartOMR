from __future__ import annotations

import numpy as np
import pymupdf
import pytest

from omr.contracts.geometry import canonical_size_px, mm_to_px, px_per_mm
from omr.generator.config import ExamConfig
from omr.generator.generate import generate_exam
from omr.generator.layout import build_layout
from omr.generator.manifest import build_manifest
from omr.reader import scan as scan_module
from omr.reader.scan import ScanError, align_scan_page, detect_page_index, iter_scan_pages


DPI = 200


def _manifest() -> dict:
    config = ExamConfig(
        exam_id="SCAN_TEST",
        course_code="CSE101",
        exam_name="Scan Test",
        exam_type="quiz",
        num_mcq=4,
        mcq_options=4,
        marks_per_mcq=1,
        written_questions=[],
    )
    return build_manifest(build_layout(config))


def _draw_rect_mm(
    image: np.ndarray,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
    fill: bool = True,
) -> None:
    import cv2

    scale = px_per_mm(DPI)
    cx, cy = mm_to_px(x_mm, y_mm, DPI)
    half_w = round(width_mm * scale / 2)
    half_h = round(height_mm * scale / 2)
    thickness = -1 if fill else 2
    cv2.rectangle(image, (cx - half_w, cy - half_h), (cx + half_w, cy + half_h), 0, thickness)


def test_plain_white_image_is_rejected_as_not_smartomr():
    image = np.full((900, 700), 255, dtype=np.uint8)

    with pytest.raises(ScanError, match="SmartOMR"):
        align_scan_page(image, _manifest(), dpi=DPI)


def test_paper_shaped_photo_without_markers_is_rejected():
    import cv2

    image = np.full((900, 700), 225, dtype=np.uint8)
    paper = np.array([[105, 75], [610, 130], [555, 805], [70, 730]], dtype=np.int32)
    cv2.fillConvexPoly(image, paper, 255)
    cv2.polylines(image, [paper], isClosed=True, color=155, thickness=2)

    with pytest.raises(ScanError, match="SmartOMR"):
        align_scan_page(image, _manifest(), dpi=DPI)


def test_corner_markers_without_sheet_identity_are_rejected():
    manifest = _manifest()
    width, height = canonical_size_px(manifest, DPI)
    image = np.full((height, width), 255, dtype=np.uint8)
    for fiducial in manifest["fiducials"]:
        _draw_rect_mm(
            image,
            fiducial["x_mm"],
            fiducial["y_mm"],
            fiducial["size_mm"],
            fiducial["size_mm"],
        )

    with pytest.raises(ScanError, match="SmartOMR"):
        align_scan_page(image, manifest, dpi=DPI)


def test_four_random_black_squares_are_not_enough_to_be_an_omr():
    import cv2

    image = np.full((500, 700), 255, dtype=np.uint8)
    for x, y in [(80, 80), (620, 80), (80, 360), (620, 360)]:
        cv2.rectangle(image, (x - 14, y - 14), (x + 14, y + 14), 0, -1)

    with pytest.raises(ScanError, match="SmartOMR"):
        align_scan_page(image, _manifest(), dpi=DPI)


def test_dark_background_photo_still_finds_inner_marker_contours(tmp_path):
    result = generate_exam(
        ExamConfig(
            exam_id="DARK_BACKGROUND_SCAN_TEST",
            course_code="CSE101",
            exam_name="Scan Test",
            exam_type="quiz",
            num_mcq=4,
            mcq_options=4,
            marks_per_mcq=1,
            written_questions=[],
        ),
        tmp_path,
    )
    with pymupdf.open(result["pdf_path"]) as doc:
        pix = doc[0].get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
    page = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
    photo = np.full((page.shape[0] + 260, page.shape[1] + 180), 92, dtype=np.uint8)
    photo[120 : 120 + page.shape[0], 80 : 80 + page.shape[1]] = page

    aligned = align_scan_page(photo, result["manifest"], dpi=DPI)

    assert aligned.page_index == 1


def test_scanner_frame_recovers_when_handwriting_touches_corner_marker(tmp_path):
    import cv2

    result = generate_exam(
        ExamConfig(
            exam_id="TOUCHED_MARKER_SCAN_TEST",
            course_code="CSE101",
            exam_name="Scan Test",
            exam_type="quiz",
            num_mcq=4,
            mcq_options=4,
            marks_per_mcq=1,
            written_questions=[],
        ),
        tmp_path,
    )
    with pymupdf.open(result["pdf_path"]) as doc:
        pix = doc[0].get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
    page = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
    top_left = next(marker for marker in result["manifest"]["fiducials"] if marker["corner"] == "TL")
    cx, cy = mm_to_px(top_left["x_mm"], top_left["y_mm"], DPI)
    marker_half_size = round(top_left["size_mm"] * px_per_mm(DPI) / 2)
    extension = round(4 * px_per_mm(DPI))
    cv2.rectangle(
        page,
        (cx - marker_half_size - extension // 2, cy - marker_half_size - extension // 2),
        (cx + marker_half_size + extension // 2, cy + marker_half_size + extension // 2),
        0,
        -1,
    )

    aligned = align_scan_page(page, result["manifest"], dpi=DPI)

    assert aligned.page_index == 1


def test_page_index_uses_ocr_when_bars_are_weak(monkeypatch):
    manifest = _manifest()
    width, height = canonical_size_px(manifest, DPI)
    page = np.full((height, width), 255, dtype=np.uint8)

    monkeypatch.setattr(
        scan_module,
        "_ocr_page_index_from_text_region",
        lambda _gray, _manifest, _dpi: (1, 0.35, "Page 1 of 1"),
    )

    page_index, confidence, scores = detect_page_index(page, manifest, DPI)

    assert page_index == 1
    assert confidence == pytest.approx(0.35)
    assert scores[1] == 0.0


def test_page_index_rejects_when_bars_and_ocr_are_both_weak(monkeypatch):
    manifest = _manifest()
    width, height = canonical_size_px(manifest, DPI)
    page = np.full((height, width), 255, dtype=np.uint8)

    monkeypatch.setattr(scan_module, "_ocr_page_index_from_text_region", lambda *_args: None)

    with pytest.raises(ScanError, match="OCR fallback"):
        detect_page_index(page, manifest, DPI)


def test_pdf_pages_can_be_rendered_incrementally(tmp_path):
    pdf_path = tmp_path / "three-pages.pdf"
    with pymupdf.open() as document:
        for _ in range(3):
            document.new_page(width=210, height=297)
        document.save(pdf_path)

    pages = iter_scan_pages(pdf_path, dpi=72)

    first = next(pages)
    assert first.shape == (297, 210)
    assert sum(1 for _page in pages) == 2
