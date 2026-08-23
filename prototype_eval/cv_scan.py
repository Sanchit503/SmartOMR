"""Compatibility wrapper for scan reading promoted into `omr.reader.scan`."""
from omr.reader.scan import (
    IMAGE_EXTENSIONS,
    ScanError,
    align_scan_page,
    align_scan_pages,
    detect_page_index,
    load_scan_pages,
)

__all__ = [
    "IMAGE_EXTENSIONS",
    "ScanError",
    "align_scan_page",
    "align_scan_pages",
    "detect_page_index",
    "load_scan_pages",
]
