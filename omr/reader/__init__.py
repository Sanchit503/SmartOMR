"""Scan ingestion, canonicalization, page-index reading, and identity reading.

This package is the promoted home for the real-scan pieces that started in
`prototype_eval`: loading scan images/PDFs, finding registration markers,
warping pages to manifest coordinates, reading page-index bars, and decoding
the page-1 roll-number block.
"""
from .identity import read_roll_number
from .handwriting import (
    LocalDigitModelRollOcr,
    LocalEnsembleRollOcr,
    LocalTesseractRollOcr,
    RollOcrResult,
    build_roll_ocr_backend,
    normalize_handwritten_roll_text,
    read_continuation_roll_number,
    read_write_in_roll_number,
    save_roll_number_crop_sets,
    save_roll_number_crops,
)
from .scan import (
    IMAGE_EXTENSIONS,
    ScanError,
    align_scan_page,
    align_scan_pages,
    detect_page_index,
    iter_scan_pages,
    load_scan_pages,
)
from .quality import assess_alignment_quality, save_alignment_overlay, save_alignment_report, save_sampling_overlay
from .numerical import NumericalReading, read_numerical_responses
from .written import crop_written_responses
from .written_ocr import build_written_ocr_backend, read_written_answer_texts, save_written_line_crops

__all__ = [
    "IMAGE_EXTENSIONS",
    "LocalDigitModelRollOcr",
    "LocalEnsembleRollOcr",
    "LocalTesseractRollOcr",
    "NumericalReading",
    "RollOcrResult",
    "ScanError",
    "assess_alignment_quality",
    "align_scan_page",
    "align_scan_pages",
    "build_roll_ocr_backend",
    "build_written_ocr_backend",
    "detect_page_index",
    "iter_scan_pages",
    "load_scan_pages",
    "normalize_handwritten_roll_text",
    "read_numerical_responses",
    "read_continuation_roll_number",
    "read_write_in_roll_number",
    "save_alignment_overlay",
    "save_alignment_report",
    "save_roll_number_crop_sets",
    "save_roll_number_crops",
    "save_sampling_overlay",
    "save_written_line_crops",
    "crop_written_responses",
    "read_written_answer_texts",
    "read_roll_number",
]
