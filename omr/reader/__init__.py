"""Scan ingestion, canonicalization, page-index reading, and identity reading.

This package is the promoted home for the real-scan pieces that started in
`prototype_eval`: loading scan images/PDFs, finding registration markers,
warping pages to manifest coordinates, reading page-index bars, and decoding
the page-1 roll-number block.
"""
from .identity import read_roll_number
from .scan import (
    IMAGE_EXTENSIONS,
    ScanError,
    align_scan_page,
    align_scan_pages,
    detect_page_index,
    load_scan_pages,
)
from .written import crop_written_responses

__all__ = [
    "IMAGE_EXTENSIONS",
    "ScanError",
    "align_scan_page",
    "align_scan_pages",
    "detect_page_index",
    "load_scan_pages",
    "crop_written_responses",
    "read_roll_number",
]
