"""Batch parse one uploaded PDF/folder that may contain many students.

Unlike `parse.py`, this workflow does not assume one input file is one
student. Every scanned page is aligned independently, identified, then
grouped into `students/<roll_no>/` before scoring/cropping.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from omr.contracts import load_manifest
from omr.grading.mcq import read_mcq_responses
from omr.io.csv import load_answer_key, load_students, normalize_roll
from omr.models import AlignedPage, Student
from omr.reader.handwriting import (
    RollOcrBackend,
    build_roll_ocr_backend,
    read_continuation_roll_number,
    read_write_in_roll_number,
    save_roll_number_crop_sets,
)
from omr.reader.identity import read_roll_number
from omr.reader.scan import ScanError, align_scan_page, load_scan_pages
from omr.reader.written import crop_written_responses
from omr.workflows.parse import (
    DEFAULT_OUTPUT_ROOT,
    _json_path,
    _manifest_for_pages,
    _match_student,
    _mcq_answer_summary,
    _mcq_payload,
    _missing_pages,
    _page_artifacts,
    _safe_id,
    _scan_files,
    _student_payload,
    _written_payload,
)
from omr.reader.written_ocr import WrittenOcrBackend, build_written_ocr_backend, read_written_answer_texts


CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


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
            payload["write_in_roll_read"] = asdict(write_in)
            write_roll_no = normalize_roll(write_in.roll_no or "") if write_in.roll_no else None
            if roll_no and write_roll_no and roll_no != write_roll_no and write_in.confidence != "low":
                flags.append(f"page-1 write-in roll {write_roll_no} conflicts with bubbled roll {roll_no}")
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


def _strong_enough(confidence: str, minimum: str) -> bool:
    return CONFIDENCE_RANK.get(confidence, 0) >= CONFIDENCE_RANK[minimum]


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
) -> dict[str, Any]:
    output_dir = root / "unmatched_pages" / f"source_{record.source_index:04d}"
    aligned = {record.aligned_page.page_index: record.aligned_page}
    _images_by_page, page_records, quality_reports = _page_artifacts(aligned, manifest, output_dir, dpi)
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
    payload = {
        "source_path": str(record.source_path),
        "source_index": record.source_index,
        "status": "needs_review",
        "page_index": record.aligned_page.page_index,
        "identity_kind": record.identity_kind,
        "identity": _relative_identity_payload(identity_payload, output_dir),
        "pages": [asdict(page) for page in page_records],
        "review_flags": review_flags,
    }
    details_path = output_dir / "page.json"
    details_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    payload["details_path"] = str(details_path)
    return payload


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
            roll_read["write_in_roll_read"] = asdict(write_in)
            write_roll_no = normalize_roll(write_in.roll_no or "") if write_in.roll_no else None
            if write_roll_no and write_roll_no != roll_no and write_in.confidence != "low":
                review_flags.append(f"page-1 write-in roll {write_roll_no} conflicts with grouped roll {roll_no}")

    student = _match_student(roll_no, program, students, review_flags)
    available_pages = set(images_by_page)
    parse_manifest = _manifest_for_pages(manifest, available_pages) if allow_partial else manifest

    readings = read_mcq_responses(images_by_page, parse_manifest, dpi)
    mcq_responses, mcq_score, mcq_total = _mcq_payload(readings, parse_manifest, answer_key, review_flags)

    written_crops = crop_written_responses(
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
            record.identity_kind in {"handwritten", "write_in"}
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
                if record.identity_kind in {"handwritten", "write_in"}:
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
    status = "ready" if not review_flags else "needs_review"
    details_path = output_dir / "student.json"
    payload = {
        "exam_id": manifest["exam_id"],
        "status": status,
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
        "pages": [asdict(page) for page in page_records],
        "mcq_score": mcq_score,
        "mcq_total": mcq_total,
        "mcq_responses": mcq_responses,
        "written_responses": written_payload,
        "review_flags": review_flags,
        "details_path": str(details_path),
    }
    details_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


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
    written_ocr_backend: WrittenOcrBackend | None = None,
) -> tuple[list[dict[str, Any]], Path]:
    manifest = load_manifest(manifest_path)
    course_id = _safe_id(course_id) if course_id else _safe_id(manifest["exam_id"])
    root = Path(output_root) / course_id
    root.mkdir(parents=True, exist_ok=True)

    students = load_students(students_path) if students_path else None
    valid_rolls = set(students) if students else None
    default_marks = float(manifest["exam"].get("marks_per_mcq", 1.0))
    answer_key = load_answer_key(answer_key_path, default_marks=default_marks) if answer_key_path else None

    grouped: dict[str, list[_PageRecord]] = {}
    unmatched: list[_PageRecord] = []
    page_errors: list[dict[str, Any]] = []

    source_counter = 0
    for scan_path in _scan_files(Path(scans_path)):
        raw_pages = load_scan_pages(scan_path, dpi)
        for raw_page in raw_pages:
            source_counter += 1
            identity_dir = root / "_page_identity" / f"source_{source_counter:04d}"
            try:
                aligned = align_scan_page(raw_page, manifest, dpi, source_index=source_counter)
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
            if roll_no and _strong_enough(confidence, min_group_confidence):
                grouped.setdefault(roll_no, []).append(record)
            else:
                unmatched.append(record)

    student_results: list[dict[str, Any]] = []
    for roll_no, records in sorted(grouped.items()):
        student_results.append(
            _write_student_group(
                roll_no,
                records,
                manifest,
                root,
                students,
                answer_key,
                dpi,
                written_padding_mm,
                allow_partial,
                ocr_backend,
                written_ocr_backend,
            )
        )

    unmatched_results = [_write_unmatched_page(record, manifest, root, dpi) for record in unmatched]
    index_path = root / "parse_index.json"
    index = {
        "exam_id": manifest["exam_id"],
        "mode": "multi_student_bundle",
        "status_counts": {
            "ready": sum(1 for result in student_results if result["status"] == "ready"),
            "needs_review": sum(1 for result in student_results if result["status"] == "needs_review"),
            "unmatched_pages": len(unmatched_results),
            "page_errors": len(page_errors),
        },
        "students": [
            {
                "roll_no": result["student"]["roll_no"],
                "status": result["status"],
                "student_name": result["student"]["name"],
                "mcq_score": result["mcq_score"],
                "mcq_total": result["mcq_total"],
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
                "details_path": result["details_path"],
            }
            for result in student_results
        ],
        "unmatched_pages": unmatched_results,
        "page_errors": page_errors,
    }
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
        help="Minimum identity confidence required before a page is attached to a student",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest_path = _resolve_manifest_path(args.exam_id, args.data_dir)
        answer_key_path = _resolve_answer_key_path(args.exam_id, args.data_dir, args.answer_key)
        ocr_backend = build_roll_ocr_backend(
            args.handwritten_roll_ocr,
            tesseract_cmd=args.tesseract_cmd,
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
            written_ocr_backend=written_ocr_backend,
        )
    except (OSError, ScanError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    ready = sum(1 for result in results if result["status"] == "ready")
    review = sum(1 for result in results if result["status"] == "needs_review")
    print(f"Wrote {index_path}")
    print(f"students_ready={ready} students_needs_review={review}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
