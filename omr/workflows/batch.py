"""Batch parse one uploaded PDF/folder that may contain many students.

Unlike `parse.py`, this workflow does not assume one input file is one
student. Every scanned page is aligned independently, identified, then
grouped into `students/<roll_no>/` before scoring/cropping.
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np
from PIL import Image

from omr.contracts import load_manifest
from omr.contracts.geometry import MM_PER_INCH
from omr.grading.mcq import read_mcq_responses
from omr.io.csv import load_answer_key, load_students, normalize_roll
from omr.identity_evidence import (
    BLOCK,
    SOURCES_CONTRADICT,
    evidence,
    merge_evidence,
)
from omr.models import AlignedPage, Student
from omr.reader.handwriting import (
    RollOcrBackend,
    build_roll_ocr_backend,
    read_continuation_roll_number,
    read_write_in_roll_number,
    save_roll_number_crop_sets,
)
from omr.reader.identity import read_roll_number
from omr.reader.scan import ScanError, align_scan_page, iter_scan_pages
from omr.reader.written import crop_written_responses
from omr.workflows.parse import (
    DEFAULT_OUTPUT_ROOT,
    _json_path,
    _manifest_for_pages,
    _match_student,
    _mcq_answer_summary,
    _mcq_payload,
    _numerical_payload,
    _missing_pages,
    _page_artifacts,
    _safe_id,
    _scan_files,
    _student_payload,
    _validate_numerical_answer_key,
    _written_payload,
)
from omr.reader.written_ocr import WrittenOcrBackend, build_written_ocr_backend, read_written_answer_texts
from omr.workflows.identity_resolution import (
    resolve_identity_proposals,
    resolution_digest,
)
from omr.workflows.ownership import assess_ownership, add_order_suggestions


CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
GROUPING_MODES = {"auto", "identity", "page-major", "sheet-major"}
HANDWRITTEN_OCR_NOT_CONFIGURED_FLAG = "handwritten roll OCR is not configured; saved roll crop(s) for manual review"
PAGE_MAJOR_IDENTITY_KIND = "page_major_order"
SHEET_MAJOR_IDENTITY_KIND = "sheet_major_order"
WRITE_IN_SIMILARITY_IDENTITY_KIND = "write_in_similarity"
EXACT_CROSS_PAGE_ROLL_IDENTITY_KIND = "exact_cross_page_roll"
AUTO_ORDER_MIN_OBSERVATIONS = 8
AUTO_ORDER_MIN_SCORE = 0.98
AUTO_ORDER_MIN_MARGIN = 0.15
GROUPED_IDENTITY_KINDS = {
    PAGE_MAJOR_IDENTITY_KIND,
    SHEET_MAJOR_IDENTITY_KIND,
    WRITE_IN_SIMILARITY_IDENTITY_KIND,
    EXACT_CROSS_PAGE_ROLL_IDENTITY_KIND,
}
WRITE_IN_SIMILARITY_MIN_SCORE = 0.88
WRITE_IN_SIMILARITY_REVIEW_MARGIN = 0.03
REVIEW_REPORT_COLUMNS = [
    "item_type",
    "status",
    "roll_no",
    "program",
    "student_name",
    "student_email",
    "pages_found",
    "expected_pages",
    "missing_pages",
    "source_indices",
    "grouping_mode",
    "identity_kinds",
    "mcq_score",
    "mcq_total",
    "numerical_score",
    "numerical_total",
    "review_flag_count",
    "review_flags",
    "sheet_pdf_path",
    "details_path",
    "source_path",
    "error_type",
]


@dataclass
class _PageRecord:
    source_path: Path
    source_index: int
    aligned_page: AlignedPage
    identity_kind: str
    roll_no: str | None
    program: str | None
    confidence: str
    identity_payload: dict[str, Any] | None
    review_flags: list[str]




def _resolve_manifest_path(exam_id: str, data_dir: Path) -> Path:
    manifest_path = data_dir / "exams" / f"{_safe_id(exam_id)}.manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest not found for exam id {exam_id}: {manifest_path}")
    return manifest_path


def _resolve_answer_key_path(exam_id: str, data_dir: Path, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    candidate = data_dir / "answer_keys" / f"{_safe_id(exam_id)}_answer_key.csv"
    return candidate if candidate.exists() else None


def _scan_page_count(scan_files: list[Path]) -> int:
    total = 0
    for scan_path in scan_files:
        if scan_path.suffix.lower() != ".pdf":
            total += 1
            continue
        try:
            import pymupdf

            with pymupdf.open(scan_path) as document:
                total += document.page_count
        except Exception:
            return 0
    return total


def _relative_identity_payload(payload: dict[str, Any] | None, output_dir: Path) -> dict[str, Any] | None:
    if payload is None:
        return None
    copied = dict(payload)
    crop_paths = {
        key: _json_path(Path(value), output_dir)
        for key, value in dict(copied.get("crop_paths", {})).items()
    }
    copied["crop_paths"] = crop_paths
    cell_crop_paths = {
        key: [_json_path(Path(value), output_dir) for value in values]
        for key, values in dict(copied.get("cell_crop_paths", {})).items()
    }
    copied["cell_crop_paths"] = cell_crop_paths
    if "write_in_roll_read" in copied and isinstance(copied["write_in_roll_read"], dict):
        copied["write_in_roll_read"] = _relative_identity_payload(copied["write_in_roll_read"], output_dir)
    return copied


def _page_identity(
    aligned_page: AlignedPage,
    manifest: dict,
    dpi: float,
    identity_dir: Path,
    ocr_backend: RollOcrBackend | None,
    valid_rolls: set[str] | None = None,
) -> tuple[str, str | None, str | None, str, dict[str, Any] | None, list[str]]:
    page_no = aligned_page.page_index
    image = np.asarray(aligned_page.image)
    if page_no == manifest["roll_number_block"].get("page", 1):
        roll = read_roll_number(image, manifest, dpi)
        roll_no = normalize_roll(roll.roll_no or "") if roll.roll_no else None
        payload = asdict(roll)
        flags = list(roll.review_flags)
        if ocr_backend is not None:
            write_in = read_write_in_roll_number(
                image,
                manifest,
                dpi,
                page_no,
                identity_dir,
                ocr_backend=ocr_backend,
                selected_program=roll.program,
                valid_rolls=valid_rolls,
            )
            write_in_payload = asdict(write_in)
            payload["write_in_roll_read"] = write_in_payload
            payload["evidence_flags"] = merge_evidence(
                payload.get("evidence_flags"),
                write_in_payload.get("evidence_flags"),
            )
            write_roll_no = normalize_roll(write_in.roll_no or "") if write_in.roll_no else None
            if not write_roll_no:
                flags.append(
                    "page-1 write-in roll OCR could not be decoded confidently; "
                    "the bubbled roll was not independently confirmed"
                )
            elif write_in.confidence == "low":
                flags.append(
                    f"page-1 write-in roll OCR {write_roll_no} has low confidence; "
                    "the bubbled roll requires manual confirmation"
                )
            elif roll_no and roll_no != write_roll_no:
                flags.append(f"page-1 write-in roll {write_roll_no} conflicts with bubbled roll {roll_no}")
                payload["evidence_flags"] = merge_evidence(
                    payload.get("evidence_flags"),
                    [
                        evidence(
                            SOURCES_CONTRADICT,
                            BLOCK,
                            bubble_roll=roll_no,
                            cell_roll=write_roll_no,
                        )
                    ],
                )
            if not roll_no and write_roll_no:
                return "write_in", write_roll_no, write_in.program, write_in.confidence, payload, flags
        return "bubbled", roll_no, roll.program, roll.confidence, payload, flags

    read = read_continuation_roll_number(
        image,
        manifest,
        dpi,
        page_no,
        identity_dir,
        ocr_backend=ocr_backend,
        valid_rolls=valid_rolls,
    )
    roll_no = normalize_roll(read.roll_no or "") if read.roll_no else None
    return "handwritten", roll_no, read.program, read.confidence, asdict(read), list(read.review_flags)




def _best_page(existing: _PageRecord, challenger: _PageRecord) -> _PageRecord:
    existing_score = (
        CONFIDENCE_RANK.get(existing.confidence, 0),
        existing.aligned_page.alignment_confidence,
        existing.aligned_page.page_mark_confidence,
    )
    challenger_score = (
        CONFIDENCE_RANK.get(challenger.confidence, 0),
        challenger.aligned_page.alignment_confidence,
        challenger.aligned_page.page_mark_confidence,
    )
    return challenger if challenger_score > existing_score else existing


def _group_records_by_identity(
    records: list[_PageRecord], min_group_confidence: str, page_one_index: int = 1,
    valid_rolls: set[str] | None = None, program_by_roll: Mapping[str, str] | None = None,
) -> tuple[dict[str, list[_PageRecord]], list[_PageRecord]]:
    return _group_records_by_safe_ownership(
        records, {"num_pages": max((record.aligned_page.page_index for record in records), default=1)},
        valid_rolls=valid_rolls, program_by_roll=program_by_roll,
    )


def _reconcile_exact_cross_page_roll_pairs(
    records: list[_PageRecord], grouped: dict[str, list[_PageRecord]],
    unmatched: list[_PageRecord], *, page_one_index: int,
    valid_rolls: set[str] | None, program_by_roll: Mapping[str, str] | None = None,
) -> tuple[dict[str, list[_PageRecord]], list[_PageRecord]]:
    # Recovery uses the same gates; weak exact text is not promoted by another path.
    return _group_records_by_safe_ownership(
        records, {"num_pages": max((record.aligned_page.page_index for record in records), default=1)},
        valid_rolls=valid_rolls, program_by_roll=program_by_roll,
    )


def _page_sequence(records: list[_PageRecord]) -> list[int]:
    return [record.aligned_page.page_index for record in sorted(records, key=lambda item: item.source_index)]


def _matches_page_major_sequence(page_sequence: list[int], num_pages: int) -> bool:
    if num_pages <= 1 or not page_sequence:
        return False
    student_count = page_sequence.count(1)
    if student_count == 0:
        return False
    expected = [page_index for page_index in range(1, num_pages + 1) for _ in range(student_count)]
    return page_sequence == expected


def _matches_sheet_major_sequence(page_sequence: list[int], num_pages: int) -> bool:
    if num_pages <= 1 or not page_sequence or len(page_sequence) % num_pages != 0:
        return False
    student_count = len(page_sequence) // num_pages
    expected = list(range(1, num_pages + 1)) * student_count
    return page_sequence == expected


def _source_order_inference(
    records: list[_PageRecord],
    manifest: dict,
    total_source_pages: int,
) -> dict[str, Any]:
    """Infer scanner collation from source slots while tolerating isolated failures.

    Source indices are retained even when a page fails alignment.  Comparing a
    detected template page against its expected source slot therefore lets one
    unreadable scan remain local instead of disabling order evidence for the
    entire bundle.
    """
    num_pages = int(manifest["num_pages"])
    observed = len(records)
    result: dict[str, Any] = {
        "mode": "identity",
        "observed_pages": observed,
        "total_source_pages": int(total_source_pages),
        "minimum_score": AUTO_ORDER_MIN_SCORE,
        "minimum_margin": AUTO_ORDER_MIN_MARGIN,
        "scores": {"sheet-major": 0.0, "page-major": 0.0},
        "reason": "insufficient page-order evidence",
    }
    if num_pages <= 1 or observed < max(AUTO_ORDER_MIN_OBSERVATIONS, num_pages * 3):
        return result

    sheet_matches = sum(
        record.aligned_page.page_index == ((record.source_index - 1) % num_pages) + 1
        for record in records
    )
    sheet_score = sheet_matches / observed
    page_score = 0.0
    page_matches = 0
    student_count = 0
    if total_source_pages > 0 and total_source_pages % num_pages == 0:
        student_count = total_source_pages // num_pages
        page_matches = sum(
            record.aligned_page.page_index == ((record.source_index - 1) // student_count) + 1
            for record in records
        )
        page_score = page_matches / observed

    scores = {"sheet-major": sheet_score, "page-major": page_score}
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    best_mode, best_score = ranked[0]
    runner_up_score = ranked[1][1]
    margin = best_score - runner_up_score
    result.update(
        {
            "scores": {key: round(value, 6) for key, value in scores.items()},
            "matches": {
                "sheet-major": sheet_matches,
                "page-major": page_matches,
            },
            "student_count": student_count or None,
            "best_mode": best_mode,
            "best_score": round(best_score, 6),
            "margin": round(margin, 6),
        }
    )
    if best_score < AUTO_ORDER_MIN_SCORE:
        result["reason"] = "detected page codes do not consistently match a scanner order"
        return result
    if margin < AUTO_ORDER_MIN_MARGIN:
        result["reason"] = "scanner order is ambiguous"
        return result
    result["mode"] = best_mode
    result["reason"] = (
        f"{best_mode} order matched {result['matches'][best_mode]}/{observed} readable source pages"
    )
    return result






def _group_records_by_inferred_order(
    records: list[_PageRecord],
    manifest: dict,
    grouping_mode: str,
    *,
    total_source_pages: int,
    valid_rolls: set[str] | None,
) -> tuple[dict[str, list[_PageRecord]], list[_PageRecord]]:
    """Retained API: scanner order never establishes ownership."""
    return _group_records_by_safe_ownership(records, manifest, valid_rolls=valid_rolls)


def _group_records_by_safe_ownership(
    records: list[_PageRecord], manifest: dict, *, valid_rolls: set[str] | None,
    program_by_roll: Mapping[str, str] | None = None,
) -> tuple[dict[str, list[_PageRecord]], list[_PageRecord]]:
    assessment = assess_ownership(
        [{"source_index": record.source_index, "sheet_page": record.aligned_page.page_index,
          "identity_kind": record.identity_kind, "literal_roll_no": record.roll_no,
          "program": record.program, "confidence": record.confidence,
          "identity": record.identity_payload or {}} for record in records],
        expected_pages=int(manifest["num_pages"]), valid_rolls=valid_rolls,
        program_by_roll=program_by_roll, require_quality=False,
    )
    by_source = {record.source_index: record for record in records}
    for decision in assessment["pages"]:
        record = by_source[decision["source_index"]]
        payload = dict(record.identity_payload or {})
        payload["ownership_evidence_flags"] = decision["evidence_flags"]
        record.identity_payload = payload
        record.review_flags = list(dict.fromkeys(record.review_flags + [
            flag["message"] for flag in decision["evidence_flags"] if flag["severity"] == BLOCK
        ]))
    grouped = {roll: [by_source[source] for source in student["source_indices"]]
               for roll, student in assessment["students"].items()}
    assigned = {int(source) for source in assessment["assignments"]}
    return grouped, [record for record in records if record.source_index not in assigned]




def _group_records_by_page_major(
    records: list[_PageRecord], manifest: dict, min_group_confidence: str,
) -> tuple[dict[str, list[_PageRecord]], list[_PageRecord]]:
    return _group_records_by_safe_ownership(records, manifest, valid_rolls=None)


def _group_records_by_sheet_major(
    records: list[_PageRecord], manifest: dict, min_group_confidence: str,
) -> tuple[dict[str, list[_PageRecord]], list[_PageRecord]]:
    return _group_records_by_safe_ownership(records, manifest, valid_rolls=None)


def _write_page_error(root: Path, source_path: Path, source_index: int, error: Exception) -> dict[str, Any]:
    error_dir = root / "page_errors"
    error_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_path": str(source_path),
        "source_index": source_index,
        "status": "error",
        "error_type": type(error).__name__,
        "review_flags": [str(error)],
    }
    path = error_dir / f"source_{source_index:04d}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    payload["details_path"] = str(path)
    return payload


def _write_unmatched_page(
    record: _PageRecord,
    manifest: dict,
    root: Path,
    dpi: float,
    grouping_only: bool = False,
) -> dict[str, Any]:
    output_dir = root / "unmatched_pages" / f"source_{record.source_index:04d}"
    aligned = {record.aligned_page.page_index: record.aligned_page}
    images_by_page, page_records, quality_reports = _page_artifacts(aligned, manifest, output_dir, dpi)
    identity_payload = record.identity_payload
    if record.identity_kind == "handwritten":
        crops, cell_crops = save_roll_number_crop_sets(
            record.aligned_page.image,
            manifest,
            record.aligned_page.page_index,
            output_dir / "identity",
            dpi,
        )
        if identity_payload is not None:
            identity_payload = {**identity_payload, "crop_paths": crops, "cell_crop_paths": cell_crops}
    review_flags = list(record.review_flags)
    for report in quality_reports:
        review_flags.extend(f"page {report.page_index}: {flag}" for flag in report.review_flags)
    page_manifest = _manifest_for_pages(manifest, set(images_by_page))
    written_crops = [] if grouping_only else [
        replace(crop, ocr_crop_path=crop.ocr_crop_path or crop.crop_path)
        for crop in crop_written_responses(
            images_by_page,
            page_manifest,
            output_dir / "written",
            dpi,
        )
    ]
    written_payload = _written_payload(written_crops, output_dir)
    payload = {
        "source_path": str(record.source_path),
        "source_index": record.source_index,
        "status": "needs_review",
        "page_index": record.aligned_page.page_index,
        "identity_kind": record.identity_kind,
        "identity": _relative_identity_payload(identity_payload, output_dir),
        "pages": [asdict(page) for page in page_records],
        "written_responses": written_payload,
        "review_flags": review_flags,
    }
    details_path = output_dir / "page.json"
    details_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    payload["details_path"] = str(details_path)
    return payload


def _save_student_pdf(
    images_by_page: dict[int, np.ndarray],
    manifest: dict,
    output_dir: Path,
) -> Path | None:
    if not images_by_page:
        return None

    import pymupdf

    page = manifest["page"]
    width_pt = float(page["width_mm"]) / MM_PER_INCH * 72
    height_pt = float(page["height_mm"]) / MM_PER_INCH * 72
    pdf_path = output_dir / "sheet.pdf"
    doc = pymupdf.open()
    try:
        for page_no in sorted(images_by_page):
            pdf_page = doc.new_page(width=width_pt, height=height_pt)
            gray = np.asarray(images_by_page[page_no])
            if gray.ndim == 3:
                gray = gray.mean(axis=2)
            buffer = io.BytesIO()
            Image.fromarray(gray.astype(np.uint8, copy=False), mode="L").save(buffer, format="PNG")
            pdf_page.insert_image(pdf_page.rect, stream=buffer.getvalue())
        doc.save(pdf_path)
    finally:
        doc.close()
    return pdf_path


def _write_student_group(
    roll_no: str,
    records: list[_PageRecord],
    manifest: dict,
    root: Path,
    students: dict[str, Student] | None,
    answer_key: dict[int, Any] | None,
    dpi: float,
    written_padding_mm: float,
    allow_partial: bool,
    roll_ocr_backend: RollOcrBackend | None,
    written_ocr_backend: WrittenOcrBackend | None,
    grouping_only: bool = False,
) -> dict[str, Any]:
    output_dir = root / "students" / _safe_id(roll_no)
    selected_by_page: dict[int, _PageRecord] = {}
    duplicate_pages: dict[int, list[int]] = {}
    review_flags: list[str] = []

    for record in records:
        page_no = record.aligned_page.page_index
        if page_no in selected_by_page:
            duplicate_pages.setdefault(page_no, [selected_by_page[page_no].source_index]).append(record.source_index)
            selected_by_page[page_no] = _best_page(selected_by_page[page_no], record)
        else:
            selected_by_page[page_no] = record
        review_flags.extend(record.review_flags)

    for page_no, source_indices in sorted(duplicate_pages.items()):
        review_flags.append(f"duplicate page {page_no} found in source pages {source_indices}; best page was used")

    aligned_pages = {page_no: record.aligned_page for page_no, record in selected_by_page.items()}
    images_by_page, page_records, quality_reports = _page_artifacts(aligned_pages, manifest, output_dir, dpi)
    sheet_pdf_path = _save_student_pdf(images_by_page, manifest, output_dir)
    for report in quality_reports:
        review_flags.extend(f"page {report.page_index}: {flag}" for flag in report.review_flags)

    missing_pages = _missing_pages(manifest, images_by_page)
    if missing_pages:
        review_flags.append(f"partial scan: missing page(s): {', '.join(str(page) for page in missing_pages)}")
        if not allow_partial:
            raise ScanError(f"student {roll_no} is missing page(s): {', '.join(str(page) for page in missing_pages)}")

    program = next((record.program for record in records if record.program), None)
    roll_read = None
    if 1 in images_by_page:
        roll = read_roll_number(images_by_page[1], manifest, dpi)
        roll_read = asdict(roll)
        if roll.roll_no and normalize_roll(roll.roll_no) != roll_no:
            review_flags.append(f"page-1 bubbled roll {roll.roll_no} conflicts with grouped roll {roll_no}")
        if roll.program:
            program = roll.program
        if roll_ocr_backend is not None:
            page_one_identity = selected_by_page[1].identity_payload or {}
            cached_write_in = page_one_identity.get("write_in_roll_read")
            if isinstance(cached_write_in, dict):
                write_in_payload = dict(cached_write_in)
                crops, cell_crops = save_roll_number_crop_sets(
                    images_by_page[1],
                    manifest,
                    1,
                    output_dir / "identity",
                    dpi,
                )
                write_in_payload["crop_paths"] = crops
                write_in_payload["cell_crop_paths"] = cell_crops
            else:
                write_in = read_write_in_roll_number(
                    images_by_page[1],
                    manifest,
                    dpi,
                    1,
                    output_dir / "identity",
                    ocr_backend=roll_ocr_backend,
                    selected_program=roll.program or program,
                    valid_rolls=set(students) if students else None,
                )
                write_in_payload = asdict(write_in)
            roll_read["write_in_roll_read"] = write_in_payload
            write_roll_no = normalize_roll(str(write_in_payload.get("roll_no") or "")) or None
            write_in_confidence = str(write_in_payload.get("confidence") or "low")
            if write_roll_no and write_roll_no != roll_no and write_in_confidence != "low":
                review_flags.append(f"page-1 write-in roll {write_roll_no} conflicts with grouped roll {roll_no}")

    student = _match_student(roll_no, program, students, review_flags)
    available_pages = set(images_by_page)
    parse_manifest = _manifest_for_pages(manifest, available_pages) if allow_partial else manifest

    answer_review_flags: list[str] = []
    readings = [] if grouping_only else read_mcq_responses(images_by_page, parse_manifest, dpi)
    mcq_responses, mcq_score, mcq_total = _mcq_payload(readings, parse_manifest, answer_key, answer_review_flags)
    if grouping_only:
        numerical_responses, numerical_score, numerical_total = [], None, None
    else:
        numerical_responses, numerical_score, numerical_total = _numerical_payload(
            images_by_page, manifest, dpi, answer_key, answer_review_flags,
        )

    written_crops = [] if grouping_only else crop_written_responses(
        images_by_page,
        parse_manifest,
        output_dir / "written",
        dpi,
        padding_mm=written_padding_mm,
    )
    written_ocr_reads = read_written_answer_texts(
        written_crops,
        output_dir / "written_ocr",
        written_ocr_backend,
    )
    written_payload = _written_payload(written_crops, output_dir, written_ocr_reads)

    identity_reads = []
    for record in sorted(records, key=lambda item: item.source_index):
        identity_payload = record.identity_payload
        if record.aligned_page.page_index in images_by_page and (
            record.identity_kind in {"handwritten", "write_in"} | GROUPED_IDENTITY_KINDS
            or (identity_payload is not None and "write_in_roll_read" in identity_payload)
        ):
            crops, cell_crops = save_roll_number_crop_sets(
                images_by_page[record.aligned_page.page_index],
                manifest,
                record.aligned_page.page_index,
                output_dir / "identity",
                dpi,
            )
            if identity_payload is not None:
                identity_payload = dict(identity_payload)
                if record.identity_kind in {"handwritten", "write_in"} | GROUPED_IDENTITY_KINDS:
                    identity_payload = {**identity_payload, "crop_paths": crops, "cell_crop_paths": cell_crops}
                if "write_in_roll_read" in identity_payload and isinstance(
                    identity_payload["write_in_roll_read"],
                    dict,
                ):
                    identity_payload["write_in_roll_read"] = {
                        **identity_payload["write_in_roll_read"],
                        "crop_paths": crops,
                        "cell_crop_paths": cell_crops,
                    }
        identity_reads.append(
            {
                "source_index": record.source_index,
                "page_index": record.aligned_page.page_index,
                "kind": record.identity_kind,
                "roll_no": record.roll_no,
                "program": record.program,
                "confidence": record.confidence,
                "payload": _relative_identity_payload(identity_payload, output_dir),
            }
        )
    observations = list(review_flags)
    quality_by_page = {report.page_index: report.to_dict() for report in quality_reports}
    ownership = assess_ownership(
        [{"source_index": record.source_index, "sheet_page": record.aligned_page.page_index,
          "identity_kind": record.identity_kind, "literal_roll_no": record.roll_no,
          "program": record.program, "confidence": record.confidence,
          "identity": record.identity_payload or {}, "quality": quality_by_page.get(record.aligned_page.page_index)}
         for record in records], expected_pages=int(manifest["num_pages"]),
        valid_rolls=set(students) if students is not None else None,
        program_by_roll={roll: student.program for roll, student in (students or {}).items()},
    )
    decision = ownership["students"].get(roll_no)
    complete = bool(decision and decision["status"] == "auto_matched")
    ownership_flags = [str(flag["message"]) for page in ownership["pages"] for flag in page["evidence_flags"]
                       if flag.get("severity") == BLOCK]
    if missing_pages:
        ownership_flags.append(f"partial scan: missing page(s): {', '.join(map(str, missing_pages))}")
    review_flags = list(dict.fromkeys(ownership_flags + ([] if grouping_only else answer_review_flags)))
    if not complete and not review_flags:
        review_flags.append("Ownership is not confirmed by independent identity evidence.")
    status = "ready" if complete and not review_flags else "needs_review"
    details_path = output_dir / "student.json"
    payload = {
        "exam_id": manifest["exam_id"],
        "status": status,
        "grouping_only": grouping_only,
        "ownership_decision": {"policy_version": ownership["policy_version"],
                               "status": "auto_matched" if complete else "needs_review",
                               "source_indices": decision["source_indices"] if decision else [],
                               "evidence_digest": ownership["digest"]},
        "answer_review_flags": answer_review_flags,
        "observations": observations,
        "student": _student_payload(student, roll_no, program),
        "roll_read": roll_read,
        "identity_reads": identity_reads,
        "source_pages": [
            {
                "source_path": str(record.source_path),
                "source_index": record.source_index,
                "page_index": record.aligned_page.page_index,
                "used": selected_by_page.get(record.aligned_page.page_index) is record,
            }
            for record in sorted(records, key=lambda item: item.source_index)
        ],
        "sheet_pdf_path": _json_path(sheet_pdf_path, output_dir) if sheet_pdf_path is not None else None,
        "pages": [asdict(page) for page in page_records],
        "mcq_score": mcq_score,
        "mcq_total": mcq_total,
        "numerical_responses": numerical_responses,
        "numerical_score": numerical_score,
        "numerical_total": numerical_total,
        "mcq_responses": mcq_responses,
        "written_responses": written_payload,
        "review_flags": review_flags,
        "details_path": str(details_path),
    }
    details_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _student_artifact_path(result: dict[str, Any], key: str) -> Path | None:
    value = result.get(key)
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return Path(result["details_path"]).parent / path


def _report_path(path: Path | str | None, root: Path) -> str:
    if path is None:
        return ""
    return _json_path(Path(path), root)


def _join_values(values: list[Any]) -> str:
    return ",".join(str(value) for value in values)


def _join_flags(flags: list[Any]) -> str:
    return " | ".join(str(flag).replace("\r", " ").replace("\n", " ") for flag in flags)


def _score_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _identity_roll(identity: dict[str, Any] | None) -> str:
    if not identity:
        return ""
    roll_no = identity.get("roll_no")
    if roll_no:
        return str(roll_no)
    nested = identity.get("write_in_roll_read")
    if isinstance(nested, dict) and nested.get("roll_no"):
        return str(nested["roll_no"])
    return ""


def _identity_program(identity: dict[str, Any] | None) -> str:
    if not identity:
        return ""
    program = identity.get("program")
    if program:
        return str(program)
    nested = identity.get("write_in_roll_read")
    if isinstance(nested, dict) and nested.get("program"):
        return str(nested["program"])
    return ""


def _reconcile_roster(
    students: dict[str, Student] | None,
    student_results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if students is None:
        return None
    detected_rolls = {
        str(result.get("student", {}).get("roll_no") or "")
        for result in student_results
        if result.get("student", {}).get("roll_no")
    }
    missing_rolls = sorted(set(students) - detected_rolls)
    unexpected_rolls = sorted(detected_rolls - set(students))
    return {
        "roster_total": len(students),
        "detected_roster_students": len(set(students) & detected_rolls),
        "missing_count": len(missing_rolls),
        "unexpected_count": len(unexpected_rolls),
        "missing_students": [
            {
                "roll_no": students[roll_no].roll_no,
                "student_name": students[roll_no].name,
                "student_email": students[roll_no].email,
                "program": students[roll_no].program,
            }
            for roll_no in missing_rolls
        ],
        "unexpected_rolls": unexpected_rolls,
    }


def _student_report_row(
    result: dict[str, Any],
    manifest: dict,
    root: Path,
    grouping_mode: str,
) -> dict[str, Any]:
    expected_pages = list(range(1, int(manifest["num_pages"]) + 1))
    pages_found = sorted({int(page["page_index"]) for page in result.get("pages", [])})
    missing_pages = [page for page in expected_pages if page not in pages_found]
    source_indices = sorted(int(page["source_index"]) for page in result.get("source_pages", []))
    identity_kinds = [
        f"p{read.get('page_index')}:{read.get('kind')}:{read.get('confidence')}"
        for read in result.get("identity_reads", [])
    ]
    flags = list(result.get("review_flags", []))
    student = dict(result.get("student", {}))
    return {
        "item_type": "student",
        "status": result.get("status", ""),
        "roll_no": student.get("roll_no") or "",
        "program": student.get("program") or "",
        "student_name": student.get("name") or "",
        "student_email": student.get("email") or "",
        "pages_found": _join_values(pages_found),
        "expected_pages": str(len(expected_pages)),
        "missing_pages": _join_values(missing_pages),
        "source_indices": _join_values(source_indices),
        "grouping_mode": grouping_mode,
        "identity_kinds": "; ".join(identity_kinds),
        "mcq_score": _score_value(result.get("mcq_score")),
        "mcq_total": _score_value(result.get("mcq_total")),
        "numerical_score": _score_value(result.get("numerical_score")),
        "numerical_total": _score_value(result.get("numerical_total")),
        "review_flag_count": len(flags),
        "review_flags": _join_flags(flags),
        "sheet_pdf_path": _report_path(_student_artifact_path(result, "sheet_pdf_path"), root),
        "details_path": _report_path(result.get("details_path"), root),
        "source_path": "",
        "error_type": "",
    }


def _unmatched_report_row(
    result: dict[str, Any],
    manifest: dict,
    root: Path,
    grouping_mode: str,
) -> dict[str, Any]:
    identity = result.get("identity") if isinstance(result.get("identity"), dict) else None
    flags = list(result.get("review_flags", []))
    page_index = result.get("page_index")
    source_index = result.get("source_index")
    confidence = identity.get("confidence") if identity else ""
    identity_kind = result.get("identity_kind") or ""
    return {
        "item_type": "unmatched_page",
        "status": result.get("status", "needs_review"),
        "roll_no": _identity_roll(identity),
        "program": _identity_program(identity),
        "student_name": "",
        "student_email": "",
        "pages_found": str(page_index or ""),
        "expected_pages": str(manifest["num_pages"]),
        "missing_pages": "",
        "source_indices": str(source_index or ""),
        "grouping_mode": grouping_mode,
        "identity_kinds": f"p{page_index}:{identity_kind}:{confidence}",
        "mcq_score": "",
        "mcq_total": "",
        "review_flag_count": len(flags),
        "review_flags": _join_flags(flags),
        "sheet_pdf_path": "",
        "details_path": _report_path(result.get("details_path"), root),
        "source_path": str(result.get("source_path") or ""),
        "error_type": "",
    }


def _page_error_report_row(
    result: dict[str, Any],
    manifest: dict,
    root: Path,
    grouping_mode: str,
) -> dict[str, Any]:
    flags = list(result.get("review_flags", []))
    return {
        "item_type": "page_error",
        "status": result.get("status", "error"),
        "roll_no": "",
        "program": "",
        "student_name": "",
        "student_email": "",
        "pages_found": "",
        "expected_pages": str(manifest["num_pages"]),
        "missing_pages": "",
        "source_indices": str(result.get("source_index") or ""),
        "grouping_mode": grouping_mode,
        "identity_kinds": "",
        "mcq_score": "",
        "mcq_total": "",
        "review_flag_count": len(flags),
        "review_flags": _join_flags(flags),
        "sheet_pdf_path": "",
        "details_path": _report_path(result.get("details_path"), root),
        "source_path": str(result.get("source_path") or ""),
        "error_type": str(result.get("error_type") or ""),
    }


def _review_report_rows(
    student_results: list[dict[str, Any]],
    unmatched_results: list[dict[str, Any]],
    page_errors: list[dict[str, Any]],
    manifest: dict,
    root: Path,
    grouping_mode: str,
) -> list[dict[str, Any]]:
    rows = [
        _student_report_row(result, manifest, root, grouping_mode)
        for result in sorted(student_results, key=lambda item: str(item["student"].get("roll_no") or ""))
    ]
    rows.extend(_unmatched_report_row(result, manifest, root, grouping_mode) for result in unmatched_results)
    rows.extend(_page_error_report_row(result, manifest, root, grouping_mode) for result in page_errors)
    return rows


def _write_review_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_REPORT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _html_link(value: str) -> str:
    if not value:
        return ""
    escaped = html.escape(value)
    href = html.escape(Path(value).as_posix(), quote=True)
    return f'<a href="{href}">{escaped}</a>'


def _status_class(status: str) -> str:
    if status == "ready":
        return "ready"
    if status == "error":
        return "error"
    return "review"


def _render_report_table(title: str, rows: list[dict[str, Any]], columns: list[str]) -> str:
    headers = "".join(f"<th>{html.escape(column.replace('_', ' ').title())}</th>" for column in columns)
    body_rows = []
    for row in rows:
        cells = []
        for column in columns:
            value = str(row.get(column, ""))
            if column in {"sheet_pdf_path", "details_path"}:
                cell = _html_link(value)
            elif column == "status":
                status = html.escape(value)
                cell = f'<span class="badge {_status_class(value)}">{status}</span>'
            elif column == "review_flags":
                cell = f'<span class="flags">{html.escape(value)}</span>'
            else:
                cell = html.escape(value)
            cells.append(f"<td>{cell}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    if not body_rows:
        body_rows.append(f'<tr><td colspan="{len(columns)}" class="empty">None</td></tr>')
    return f"""
      <section>
        <h2>{html.escape(title)}</h2>
        <table>
          <thead><tr>{headers}</tr></thead>
          <tbody>{''.join(body_rows)}</tbody>
        </table>
      </section>
    """


def _write_review_html(
    path: Path,
    manifest: dict,
    index: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    status_counts = dict(index.get("status_counts", {}))
    student_rows = [row for row in rows if row["item_type"] == "student"]
    unmatched_rows = [row for row in rows if row["item_type"] == "unmatched_page"]
    error_rows = [row for row in rows if row["item_type"] == "page_error"]
    metric_items = [
        ("Ready", status_counts.get("ready", 0)),
        ("Needs Review", status_counts.get("needs_review", 0)),
        ("Unmatched Pages", status_counts.get("unmatched_pages", 0)),
        ("Page Errors", status_counts.get("page_errors", 0)),
        ("Grouping", index.get("grouping_mode", "")),
        ("Page Sequence", _join_values(index.get("detected_page_sequence", []))),
    ]
    metrics = "".join(
        f"<div class=\"metric\"><span>{html.escape(label)}</span><strong>{html.escape(str(value))}</strong></div>"
        for label, value in metric_items
    )
    student_columns = [
        "status",
        "roll_no",
        "program",
        "student_name",
        "pages_found",
        "source_indices",
        "mcq_score",
        "mcq_total",
        "numerical_score",
        "numerical_total",
        "review_flag_count",
        "review_flags",
        "sheet_pdf_path",
        "details_path",
    ]
    unmatched_columns = [
        "status",
        "pages_found",
        "source_indices",
        "roll_no",
        "program",
        "identity_kinds",
        "review_flags",
        "details_path",
        "source_path",
    ]
    error_columns = ["status", "source_indices", "error_type", "review_flags", "details_path", "source_path"]
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(manifest["exam_id"])} Review Report</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: Arial, Helvetica, sans-serif;
      background: #f7f8fa;
      color: #172033;
    }}
    body {{
      margin: 0;
      padding: 24px;
    }}
    main {{
      max-width: 1280px;
      margin: 0 auto;
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 24px;
      line-height: 1.2;
    }}
    h2 {{
      margin: 28px 0 12px;
      font-size: 17px;
    }}
    .subtitle {{
      margin: 0 0 18px;
      color: #5c667a;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
      margin: 18px 0 22px;
    }}
    .metric {{
      border: 1px solid #d9dde6;
      border-radius: 6px;
      background: #ffffff;
      padding: 10px 12px;
    }}
    .metric span {{
      display: block;
      color: #667085;
      font-size: 12px;
      margin-bottom: 5px;
    }}
    .metric strong {{
      display: block;
      font-size: 16px;
      overflow-wrap: anywhere;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: #ffffff;
      border: 1px solid #d9dde6;
      font-size: 13px;
    }}
    th, td {{
      border-bottom: 1px solid #edf0f5;
      padding: 8px 9px;
      text-align: left;
      vertical-align: top;
    }}
    th {{
      background: #f0f3f8;
      color: #344054;
      font-size: 12px;
      white-space: nowrap;
    }}
    a {{
      color: #1f5fbf;
      text-decoration: none;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    .badge {{
      display: inline-block;
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .ready {{
      background: #e8f5ee;
      color: #166534;
    }}
    .review {{
      background: #fff7df;
      color: #8a4b08;
    }}
    .error {{
      background: #feeceb;
      color: #b42318;
    }}
    .flags {{
      display: inline-block;
      min-width: 220px;
      max-width: 520px;
      overflow-wrap: anywhere;
    }}
    .empty {{
      color: #667085;
      text-align: center;
      padding: 18px;
    }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(manifest["exam_id"])} Review Report</h1>
    <p class="subtitle">Generated from local SmartOMR batch parsing artifacts.</p>
    <div class="metrics">{metrics}</div>
    {_render_report_table("Students", student_rows, student_columns)}
    {_render_report_table("Unmatched Pages", unmatched_rows, unmatched_columns)}
    {_render_report_table("Page Errors", error_rows, error_columns)}
  </main>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")


def _write_review_reports(
    root: Path,
    manifest: dict,
    index: dict[str, Any],
    student_results: list[dict[str, Any]],
    unmatched_results: list[dict[str, Any]],
    page_errors: list[dict[str, Any]],
) -> dict[str, str]:
    rows = _review_report_rows(
        student_results,
        unmatched_results,
        page_errors,
        manifest,
        root,
        str(index.get("grouping_mode") or ""),
    )
    csv_path = root / "review_report.csv"
    html_path = root / "review_report.html"
    _write_review_csv(csv_path, rows)
    _write_review_html(html_path, manifest, index, rows)
    return {
        "csv": _json_path(csv_path, root),
        "html": _json_path(html_path, root),
    }



def _save_aligned_page(aligned: AlignedPage, identity_dir: Path) -> None:
    meta = {
        "page_index": aligned.page_index,
        "source_index": aligned.source_index,
        "alignment_confidence": aligned.alignment_confidence,
        "page_mark_confidence": aligned.page_mark_confidence,
    }

    # Save images
    if hasattr(aligned.image, "save"):
        aligned.image.save(identity_dir / "aligned_image.png")
    else:
        Image.fromarray(aligned.image).save(identity_dir / "aligned_image.png")

    if aligned.debug_image is not None:
        if hasattr(aligned.debug_image, "save"):
            aligned.debug_image.save(identity_dir / "aligned_debug.png")
        else:
            Image.fromarray(aligned.debug_image).save(identity_dir / "aligned_debug.png")

    with open(identity_dir / "aligned.json", "w") as f:
        json.dump(meta, f)

def _load_aligned_page(identity_dir: Path) -> AlignedPage | None:
    json_path = identity_dir / "aligned.json"
    img_path = identity_dir / "aligned_image.png"
    if not json_path.exists() or not img_path.exists():
        return None

    try:
        with open(json_path) as f:
            meta = json.load(f)
        img = Image.open(img_path).copy()

        debug_img = None
        debug_path = identity_dir / "aligned_debug.png"
        if debug_path.exists():
            debug_img = Image.open(debug_path).copy()

        return AlignedPage(
            page_index=meta["page_index"],
            source_index=meta["source_index"],
            image=img,
            alignment_confidence=meta["alignment_confidence"],
            page_mark_confidence=meta["page_mark_confidence"],
            debug_image=debug_img,
        )
    except Exception:
        return None

def parse_exam_bundle(
    scans_path: str | Path,
    manifest_path: str | Path,
    *,
    students_path: str | Path | None = None,
    answer_key_path: str | Path | None = None,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    dpi: float = 200,
    course_id: str | None = None,
    written_padding_mm: float = 0.0,
    allow_partial: bool = True,
    ocr_backend: RollOcrBackend | None = None,
    min_group_confidence: str = "high",
    grouping_mode: str = "auto",
    grouping_only: bool = False,
    written_ocr_backend: WrittenOcrBackend | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    prealigned_pages: dict[int, AlignedPage] | None = None,
    prealignment_errors: dict[int, str] | None = None,
    precomputed_identities: Mapping[int, dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], Path]:
    if min_group_confidence not in CONFIDENCE_RANK:
        raise ValueError(f"unknown minimum group confidence: {min_group_confidence}")
    if grouping_mode not in GROUPING_MODES:
        raise ValueError(f"unknown grouping mode: {grouping_mode}")

    manifest = load_manifest(manifest_path)
    course_id = _safe_id(course_id) if course_id else _safe_id(manifest["exam_id"])
    root = Path(output_root) / course_id
    root.mkdir(parents=True, exist_ok=True)

    students = load_students(students_path) if students_path else None
    valid_rolls = set(students) if students else None
    program_by_roll = {roll_no: student.program for roll_no, student in (students or {}).items()}
    default_marks = float(manifest["exam"].get("marks_per_mcq", 1.0))
    answer_key = load_answer_key(answer_key_path, default_marks=default_marks) if answer_key_path and not grouping_only else None
    _validate_numerical_answer_key(manifest, answer_key)

    page_records_for_bundle: list[_PageRecord] = []
    page_errors: list[dict[str, Any]] = []

    scan_files = _scan_files(Path(scans_path))
    prealignment_errors = prealignment_errors or {}
    if prealigned_pages is not None:
        source_indices = set(prealigned_pages) | set(prealignment_errors)
        if not source_indices:
            raise ValueError("the inspected page inventory is empty")
        total_source_pages = max(source_indices)
        expected_indices = set(range(1, total_source_pages + 1))
        if source_indices != expected_indices:
            missing = sorted(expected_indices - source_indices)
            raise ValueError(f"the inspected page inventory is incomplete; missing source page(s): {missing}")
        source_pages = (
            (scan_files[0], source_index, None)
            for source_index in range(1, total_source_pages + 1)
        )
    else:
        total_source_pages = _scan_page_count(scan_files)

        def raw_source_pages():
            source_index = 0
            for scan_path in scan_files:
                for raw_page in iter_scan_pages(scan_path, dpi):
                    source_index += 1
                    yield scan_path, source_index, raw_page

        source_pages = raw_source_pages()

    for scan_path, source_counter, raw_page in source_pages:
        identity_dir = root / "_page_identity" / f"source_{source_counter:04d}"
        identity_dir.mkdir(parents=True, exist_ok=True)
        try:
            if prealigned_pages is not None:
                if source_counter in prealignment_errors:
                    raise ValueError(prealignment_errors[source_counter])
                aligned = prealigned_pages.get(source_counter)
                if aligned is None:
                    raise ValueError("inspection produced no aligned image for this source page")
            else:
                assert raw_page is not None
                aligned = align_scan_page(raw_page, manifest, dpi, source_index=source_counter)
            _save_aligned_page(aligned, identity_dir)

            cached_identity = (precomputed_identities or {}).get(source_counter)
            if cached_identity is not None:
                if (
                    cached_identity.get("source_index") != source_counter
                    or cached_identity.get("sheet_page") != aligned.page_index
                ):
                    raise ValueError("precomputed identity does not match the inspected source page")
                identity_kind = str(cached_identity["identity_kind"])
                roll_no = cached_identity.get("literal_roll_no")
                program = cached_identity.get("program")
                confidence = str(cached_identity["confidence"])
                identity_payload = cached_identity.get("identity")
                flags = list(cached_identity.get("review_flags") or [])
            else:
                identity_kind, roll_no, program, confidence, identity_payload, flags = _page_identity(
                    aligned,
                    manifest,
                    dpi,
                    identity_dir,
                    ocr_backend,
                    valid_rolls,
                )
        except Exception as exc:
            page_errors.append(_write_page_error(root, scan_path, source_counter, exc))
            if progress_callback is not None:
                progress_callback(
                    {
                        "phase": "reading_pages",
                        "processed": source_counter,
                        "total": total_source_pages,
                        "errors": len(page_errors),
                    }
                )
            continue

        record = _PageRecord(
            source_path=scan_path,
            source_index=source_counter,
            aligned_page=aligned,
            identity_kind=identity_kind,
            roll_no=roll_no,
            program=program,
            confidence=confidence,
            identity_payload=identity_payload,
            review_flags=flags,
        )
        page_records_for_bundle.append(record)
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "reading_pages",
                    "processed": source_counter,
                    "total": total_source_pages,
                    "errors": len(page_errors),
                }
            )

    order_inference = _source_order_inference(
        page_records_for_bundle,
        manifest,
        total_source_pages,
    )
    suggested_order_mode = (
        str(order_inference["mode"])
        if grouping_mode == "auto"
        else grouping_mode
    )
    applied_grouping_mode = "identity"
    grouped, unmatched = _group_records_by_safe_ownership(
        page_records_for_bundle, manifest, valid_rolls=valid_rolls, program_by_roll=program_by_roll,
    )

    current_assignments = {
        record.source_index: roll_no
        for roll_no, records in grouped.items()
        for record in records
    }
    resolution_pages = []
    for record in page_records_for_bundle:
        identity_dir = root / "_page_identity" / f"source_{record.source_index:04d}"
        resolution_pages.append(
            {
                "source_index": record.source_index,
                "sheet_page": record.aligned_page.page_index,
                "identity_kind": record.identity_kind,
                "literal_roll_no": record.roll_no,
                "program": record.program,
                "confidence": record.confidence,
                "identity": _relative_identity_payload(record.identity_payload, root),
                "page_image_path": _json_path(identity_dir / "aligned_image.png", root),
                "current_roll": current_assignments.get(record.source_index),
            }
        )
    identity_resolution = resolve_identity_proposals(
        resolution_pages,
        valid_rolls=valid_rolls,
        program_by_roll=program_by_roll,
        current_assignments=current_assignments,
    )
    identity_resolution = add_order_suggestions(
        identity_resolution, resolution_pages, current_assignments,
        mode=suggested_order_mode, expected_pages=int(manifest["num_pages"]), total_pages=total_source_pages,
    )
    identity_resolution["digest"] = resolution_digest(identity_resolution)
    identity_resolution_path = root / "identity_resolution.json"
    identity_resolution_path.write_text(json.dumps(identity_resolution, indent=2), encoding="utf-8")

    student_results: list[dict[str, Any]] = []
    grouped_students = sorted(grouped.items())
    for student_number, (roll_no, student_page_records) in enumerate(grouped_students, start=1):
        student_results.append(
            _write_student_group(
                roll_no,
                student_page_records,
                manifest,
                root,
                students,
                answer_key,
                dpi,
                written_padding_mm,
                allow_partial,
                ocr_backend,
                written_ocr_backend,
                grouping_only=grouping_only,
            )
        )
        if progress_callback is not None:
            progress_callback(
                {
                    "phase": "writing_students",
                    "processed": student_number,
                    "total": len(grouped_students),
                    "errors": len(page_errors),
                }
            )

    unmatched_results = [_write_unmatched_page(record, manifest, root, dpi, grouping_only=grouping_only) for record in unmatched]
    roster_reconciliation = _reconcile_roster(students, student_results)
    index_path = root / "parse_index.json"
    index = {
        "exam_id": manifest["exam_id"],
        "mode": "multi_student_bundle",
        "grouping_only": grouping_only,
        "requested_grouping_mode": grouping_mode,
        "grouping_mode": applied_grouping_mode,
        "grouping_order_inference": order_inference,
        "detected_page_sequence": _page_sequence(page_records_for_bundle),
        "expected_pages": manifest["num_pages"],
        "status_counts": {
            "ready": sum(1 for result in student_results if result["status"] == "ready"),
            "needs_review": sum(1 for result in student_results if result["status"] == "needs_review"),
            "unmatched_pages": len(unmatched_results),
            "page_errors": len(page_errors),
            "roster_missing": int(roster_reconciliation["missing_count"]) if roster_reconciliation else 0,
        },
        "roster_reconciliation": roster_reconciliation,
        "identity_resolution_path": _json_path(identity_resolution_path, root),
        "identity_resolution": {
            "policy_version": identity_resolution["policy_version"],
            "mode": identity_resolution["mode"],
            "digest": identity_resolution["digest"],
            "counts": identity_resolution["counts"],
        },
        "students": [
            {
                "roll_no": result["student"]["roll_no"],
                "status": result["status"],
                "student_name": result["student"]["name"],
                "sheet_pdf_path": (
                    str(Path(result["details_path"]).parent / result["sheet_pdf_path"])
                    if result.get("sheet_pdf_path")
                    else None
                ),
                "mcq_score": result["mcq_score"],
                "mcq_total": result["mcq_total"],
                "numerical_responses": result["numerical_responses"],
                "numerical_score": result["numerical_score"],
                "numerical_total": result["numerical_total"],
                "mcq_answers": _mcq_answer_summary(result),
                "pages": [
                    {
                        "page": page["page_index"],
                        "source_index": page["source_index"],
                        "canonical_image_path": page["canonical_image_path"],
                        "debug_image_path": page["debug_image_path"],
                        "alignment_overlay_path": page["alignment_overlay_path"],
                        "sampling_overlay_path": page["sampling_overlay_path"],
                    }
                    for page in result["pages"]
                ],
                "review_flags": result["review_flags"],
                "ownership_decision": result["ownership_decision"],
                "answer_review_flags": result["answer_review_flags"],
                "observations": result["observations"],
                "details_path": result["details_path"],
            }
            for result in student_results
        ],
        "unmatched_pages": unmatched_results,
        "page_errors": page_errors,
    }
    report_paths = _write_review_reports(root, manifest, index, student_results, unmatched_results, page_errors)
    index["reports"] = report_paths
    index["review_report_csv_path"] = report_paths["csv"]
    index["review_report_html_path"] = report_paths["html"]
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    return student_results, index_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smartomr-batch",
        description="Parse one PDF/folder containing pages from many students and group output by roll number.",
    )
    parser.add_argument("--exam-id", required=True, help="Exam id, e.g. CSE222_ENDSEM_2026")
    parser.add_argument("--scans", required=True, type=Path, help="Uploaded PDF/image/folder")
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="Folder containing exams/ and answer_keys/")
    parser.add_argument("--students", type=Path, default=None, help="Optional students.csv roster")
    parser.add_argument("--answer-key", type=Path, default=None, help="Optional answer key override")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--dpi", default=300.0, type=float, help="Canonical reading DPI")
    parser.add_argument("--written-padding-mm", default=0.0, type=float)
    parser.add_argument("--grouping-only", action="store_true", help="Match sheets without reading or grading answers")
    parser.add_argument(
        "--strict-complete",
        action="store_true",
        help="Fail a grouped student if any expected page is missing",
    )
    parser.add_argument(
        "--handwritten-roll-ocr",
        default="none",
        choices=["none", "local"],
        help="No-key provider for continuation-page handwritten roll crops",
    )
    parser.add_argument(
        "--tesseract-cmd",
        default=None,
        help="Optional path to tesseract.exe/tesseract when using --handwritten-roll-ocr local",
    )
    parser.add_argument(
        "--digit-model",
        default=None,
        type=Path,
        help="Optional local .npz KNN digit model for boxed handwritten roll numbers",
    )
    parser.add_argument(
        "--written-answer-ocr",
        default="none",
        choices=["none", "tesseract", "trocr"],
        help="Optional offline OCR provider for written-answer crops",
    )
    parser.add_argument(
        "--written-ocr-model",
        default=None,
        help="Model name/path for --written-answer-ocr trocr",
    )
    parser.add_argument(
        "--written-ocr-device",
        default=None,
        help="Torch device for --written-answer-ocr trocr, e.g. cpu or cuda",
    )
    parser.add_argument(
        "--written-ocr-local-files-only",
        action="store_true",
        help="Load the TrOCR model only from the local Hugging Face cache/path",
    )
    parser.add_argument(
        "--min-group-confidence",
        default="high",
        choices=["medium", "high"],
        help="Legacy option; all automatic ownership still requires exact cells with minimum confidence 0.80",
    )
    parser.add_argument(
        "--grouping-mode",
        default="auto",
        choices=sorted(GROUPING_MODES),
        help=(
            "All modes require exact independent identity for ownership. "
            "page-major and sheet-major select order-only human-review suggestions, never positional attachment."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest_path = _resolve_manifest_path(args.exam_id, args.data_dir)
        answer_key_path = _resolve_answer_key_path(args.exam_id, args.data_dir, args.answer_key)
        ocr_backend = build_roll_ocr_backend(
            args.handwritten_roll_ocr,
            digit_model_path=args.digit_model,
        )
        written_ocr_backend = build_written_ocr_backend(
            args.written_answer_ocr,
            tesseract_cmd=args.tesseract_cmd,
            model_name=args.written_ocr_model,
            device=args.written_ocr_device,
            local_files_only=args.written_ocr_local_files_only,
        )
        results, index_path = parse_exam_bundle(
            scans_path=args.scans,
            manifest_path=manifest_path,
            students_path=args.students,
            answer_key_path=answer_key_path,
            output_root=args.output_root,
            dpi=args.dpi,
            course_id=args.exam_id,
            written_padding_mm=args.written_padding_mm,
            allow_partial=not args.strict_complete,
            ocr_backend=ocr_backend,
            min_group_confidence=args.min_group_confidence,
            grouping_mode=args.grouping_mode,
            written_ocr_backend=written_ocr_backend,
            grouping_only=args.grouping_only,
        )
    except (OSError, ScanError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    ready = sum(1 for result in results if result["status"] == "ready")
    review = sum(1 for result in results if result["status"] == "needs_review")
    print(f"Wrote {index_path}")
    index_payload = json.loads(index_path.read_text(encoding="utf-8"))
    review_report_html = index_payload.get("review_report_html_path")
    review_report_csv = index_payload.get("review_report_csv_path")
    if review_report_html:
        report_path = Path(review_report_html)
        if not report_path.is_absolute():
            report_path = index_path.parent / report_path
        print(f"review_report_html={report_path}")
    if review_report_csv:
        report_path = Path(review_report_csv)
        if not report_path.is_absolute():
            report_path = index_path.parent / report_path
        print(f"review_report_csv={report_path}")
    print(f"students_ready={ready} students_needs_review={review}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
