"""Parse filled OMR scans into machine-readable artifacts.

This is the reader workflow the rest of Phase 2 builds on. It stops at the
right boundary: canonicalize pages, read identity, read MCQs, crop written
answers, and write debug/detail artifacts. It does not send email, call an
LLM, or persist database rows.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from omr.contracts import load_manifest
from omr.grading.mcq import MCQOutcome, read_mcq_responses
from omr.io.csv import load_answer_key, load_students, normalize_roll
from omr.models import AnswerKeyEntry, ParsedPage, RollRead, Student
from omr.reader.quality import (
    assess_alignment_quality,
    save_alignment_overlay,
    save_alignment_report,
    save_sampling_overlay,
)
from omr.reader.identity import read_roll_number
from omr.reader.numerical import read_numerical_responses
from omr.reader.scan import IMAGE_EXTENSIONS, ScanError, align_scan_pages, load_scan_pages
from omr.reader.written import crop_written_responses
from omr.reader.written_ocr import (
    WrittenAnswerOcr,
    WrittenOcrBackend,
    build_written_ocr_backend,
    read_written_answer_texts,
)

SCAN_EXTENSIONS = IMAGE_EXTENSIONS | {".pdf"}
DEFAULT_OUTPUT_ROOT = Path("data") / "parsed"


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "sheet"


def _scan_files(scans_path: Path) -> list[Path]:
    if scans_path.is_file():
        return [scans_path]
    if not scans_path.exists():
        raise ScanError(f"scan path does not exist: {scans_path}")
    files = [p for p in scans_path.iterdir() if p.is_file() and p.suffix.lower() in SCAN_EXTENSIONS]
    return sorted(files)


def _course_id_from_manifest(manifest: dict) -> str:
    exam_id = str(manifest.get("exam_id", "")).strip()
    if exam_id:
        return _safe_id(exam_id)
    exam = manifest.get("exam", {})
    parts = [str(exam.get("course_code", "")), str(exam.get("exam_type", ""))]
    return _safe_id("_".join(part for part in parts if part.strip()))


def _json_path(path: Path, base: Path | None) -> str:
    if base is None:
        return str(path)
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return str(path)


def _save_gray_image(image: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    gray = np.asarray(image)
    if gray.ndim == 3:
        gray = gray.mean(axis=2)
    Image.fromarray(gray.astype(np.uint8, copy=False), mode="L").save(path)


def _save_debug_image(image: object, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    array = np.asarray(image)
    if array.ndim == 3 and array.shape[2] >= 3:
        rgb = array[:, :, :3][:, :, ::-1]
        Image.fromarray(rgb.astype(np.uint8, copy=False), mode="RGB").save(path)
        return
    gray = array.mean(axis=2) if array.ndim == 3 else array
    Image.fromarray(gray.astype(np.uint8, copy=False), mode="L").convert("RGB").save(path)


def _page_artifacts(
    aligned_pages: dict[int, Any],
    manifest: dict,
    output_dir: Path,
    dpi: float,
) -> tuple[dict[int, np.ndarray], list[ParsedPage], list[Any]]:
    pages_dir = output_dir / "pages"
    debug_dir = output_dir / "debug"
    images_by_page: dict[int, np.ndarray] = {}
    pages: list[ParsedPage] = []
    quality_reports = []

    for page_no, page in sorted(aligned_pages.items()):
        image = np.asarray(page.image)
        images_by_page[page_no] = image
        image_path = pages_dir / f"page_{page_no}.png"
        _save_gray_image(image, image_path)
        debug_image = getattr(page, "debug_image", None)
        debug_image_path = None
        if debug_image is not None:
            debug_image_path = debug_dir / f"page_{page_no}_aligned_color.png"
            _save_debug_image(debug_image, debug_image_path)

        report = assess_alignment_quality(image, manifest, page_no, dpi)
        overlay_path = debug_dir / f"page_{page_no}_alignment_overlay.png"
        sampling_overlay_path = debug_dir / f"page_{page_no}_sampling_overlay.png"
        report_path = debug_dir / f"page_{page_no}_alignment.json"
        save_alignment_overlay(debug_image if debug_image is not None else image, manifest, page_no, dpi, overlay_path)
        save_sampling_overlay(debug_image if debug_image is not None else image, manifest, page_no, dpi, sampling_overlay_path)
        report = report.with_paths(
            report_path=_json_path(report_path, output_dir),
            overlay_path=_json_path(overlay_path, output_dir),
        )
        save_alignment_report(report, report_path)
        quality_reports.append(report)

        pages.append(
            ParsedPage(
                page_index=page.page_index,
                source_index=page.source_index,
                canonical_image_path=_json_path(image_path, output_dir),
                debug_image_path=_json_path(debug_image_path, output_dir) if debug_image_path is not None else None,
                alignment_confidence=page.alignment_confidence,
                page_mark_confidence=page.page_mark_confidence,
                alignment_quality_status=report.status,
                alignment_quality_score=report.score,
                alignment_report_path=report.report_path,
                alignment_overlay_path=report.overlay_path,
                sampling_overlay_path=_json_path(sampling_overlay_path, output_dir),
            )
        )

    return images_by_page, pages, quality_reports


def _missing_pages(manifest: dict, images_by_page: dict[int, np.ndarray]) -> list[int]:
    expected = set(range(1, manifest["num_pages"] + 1))
    return sorted(expected - set(images_by_page))


def _manifest_for_pages(manifest: dict, pages: set[int]) -> dict:
    partial = dict(manifest)
    partial["mcq_block"] = [entry for entry in manifest["mcq_block"] if entry.get("page", 1) in pages]
    partial["written_block"] = [entry for entry in manifest["written_block"] if entry.get("page", 1) in pages]
    partial["numerical_block"] = [entry for entry in manifest.get("numerical_block", []) if entry["page"] in pages]
    return partial


def _student_payload(student: Student | None, roll_no: str | None, program: str | None) -> dict:
    return {
        "roll_no": roll_no,
        "program": program,
        "name": student.name if student else None,
        "email": student.email if student else None,
    }


def _match_student(
    roll_no: str | None,
    program: str | None,
    students: dict[str, Student] | None,
    review_flags: list[str],
) -> Student | None:
    if not roll_no:
        review_flags.append("roll number could not be decoded confidently")
        return None
    if students is None:
        return None

    student = students.get(normalize_roll(roll_no))
    if student is None:
        review_flags.append(f"roll number {roll_no} was not found in students.csv")
        return None
    if program and student.program and student.program != program:
        review_flags.append(f"roll number {roll_no} is marked as {program}, but roster has {student.program}")
    return student


def _mcq_payload(
    readings,
    manifest: dict,
    answer_key: dict[int, AnswerKeyEntry] | None,
    review_flags: list[str],
) -> tuple[list[dict], float | None, float | None]:
    page_by_q = {entry["q_no"]: entry.get("page", 1) for entry in manifest["mcq_block"]}
    responses: list[dict] = []
    score = 0.0
    total = 0.0

    for reading in readings:
        q_no = reading.q_no
        if reading.needs_human_review:
            review_flags.append(f"Q{q_no}: {reading.review_reason}")

        correct_answer = None
        marks = None
        marks_awarded = None
        if answer_key is not None:
            key_entry = answer_key.get(q_no)
            if key_entry is None:
                review_flags.append(f"answer key is missing Q{q_no}")
                marks = 0.0
                marks_awarded = 0.0
            else:
                correct_answer = key_entry.answer
                marks = key_entry.marks
                total += key_entry.marks
                is_correct = (
                    reading.outcome == MCQOutcome.ANSWERED and reading.selected_option == correct_answer
                )
                marks_awarded = key_entry.marks if is_correct else 0.0
                score += marks_awarded

        responses.append(
            {
                "q_no": q_no,
                "page": page_by_q.get(q_no, 1),
                "outcome": reading.outcome.value,
                "selected_option": reading.selected_option,
                "confidence": reading.confidence.value,
                "needs_human_review": reading.needs_human_review,
                "review_reason": reading.review_reason,
                "correct_option": correct_answer,
                "marks": marks,
                "marks_awarded": marks_awarded,
                "fill_ratios": reading.fill_ratios,
                "ink_densities": reading.ink_densities,
            }
        )

    if answer_key is None:
        return responses, None, None
    return responses, score, total


def _numerical_payload(images_by_page, manifest, dpi, answer_key, review_flags):
    entries = {entry["q_no"]: entry for entry in manifest.get("numerical_block", [])}
    total = sum(entry["max_marks"] for entry in entries.values())
    score = 0.0
    complete = True
    responses = []
    readable_manifest = dict(manifest)
    readable_manifest["numerical_block"] = [entry for entry in entries.values() if entry["page"] in images_by_page]
    missing = [entry for entry in entries.values() if entry["page"] not in images_by_page]
    if missing:
        complete = False
        review_flags.extend(f"numerical Q{entry['q_no']} is on missing page {entry['page']}" for entry in missing)
    for reading in read_numerical_responses(images_by_page, readable_manifest, dpi):
        entry = entries[reading.q_no]
        payload = asdict(reading)
        payload.update(max_marks=entry["max_marks"], correct_value=None, marks_awarded=None)
        review_flags.extend(f"Q{reading.q_no}: {flag}" for flag in reading.review_flags)
        key = answer_key.get(reading.q_no) if answer_key is not None else None
        if key is not None:
            if not re.fullmatch(r"[0-9]+", key.answer) or len(key.answer) > 100:
                raise ValueError(f"numerical answer key Q{reading.q_no} must be a non-negative whole number")
            expected = int(key.answer)
            if expected >= 10 ** entry["positions"]:
                raise ValueError(f"numerical answer key Q{reading.q_no} exceeds its digit capacity")
            if not math.isfinite(key.marks) or key.marks != entry["max_marks"]:
                raise ValueError(f"numerical answer key Q{reading.q_no} marks must match manifest max_marks")
            payload["correct_value"] = expected
            if not reading.needs_human_review:
                awarded = entry["max_marks"] if reading.outcome == "answered" and reading.value == expected else 0.0
                payload["marks_awarded"] = awarded
                score += awarded
        elif answer_key is not None:
            review_flags.append(f"answer key is missing numerical Q{reading.q_no}")
        if payload["marks_awarded"] is None:
            complete = False
        responses.append(payload)
    if answer_key is None:
        return responses, None, None
    return responses, score if complete else None, total


def _mcq_answer_summary(result: dict) -> list[dict]:
    return [
        {
            "q_no": response["q_no"],
            "answer": response["selected_option"],
            "outcome": response["outcome"],
            "confidence": response["confidence"],
            "needs_review": response["needs_human_review"],
            "correct_option": response["correct_option"],
            "marks_awarded": response["marks_awarded"],
        }
        for response in result.get("mcq_responses", [])
    ]


def _relative_written_ocr_payload(read: WrittenAnswerOcr, output_dir: Path) -> dict:
    payload = read.to_json()
    payload["crop_path"] = _json_path(Path(str(payload["crop_path"])), output_dir)
    line_results = []
    for line in payload.get("line_results", []):
        line_payload = dict(line)
        line_payload["crop_path"] = _json_path(Path(str(line_payload["crop_path"])), output_dir)
        line_results.append(line_payload)
    payload["line_results"] = line_results
    return payload


def _written_payload(
    written_crops: list,
    output_dir: Path,
    written_ocr_reads: dict[int, WrittenAnswerOcr] | None = None,
) -> list[dict]:
    payload = []
    for crop in written_crops:
        item = asdict(crop)
        item["crop_path"] = _json_path(Path(crop.crop_path), output_dir)
        if written_ocr_reads and crop.q_no in written_ocr_reads:
            item["ocr"] = _relative_written_ocr_payload(written_ocr_reads[crop.q_no], output_dir)
        payload.append(item)
    return payload


def parse_scan(
    scan_path: str | Path,
    manifest_path: str | Path,
    output_dir: str | Path,
    students_path: str | Path | None = None,
    answer_key_path: str | Path | None = None,
    dpi: float = 200,
    written_padding_mm: float = 0.0,
    allow_partial: bool = True,
    written_ocr_backend: WrittenOcrBackend | None = None,
) -> dict:
    """Parse one filled OMR scan/PDF and write its artifacts.

    Returns the same payload written to `parse.json`.
    """
    scan_path = Path(scan_path)
    manifest = load_manifest(manifest_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    students = load_students(students_path) if students_path else None
    default_marks = float(manifest["exam"].get("marks_per_mcq", 1.0))
    answer_key = load_answer_key(answer_key_path, default_marks=default_marks) if answer_key_path else None

    raw_pages = load_scan_pages(scan_path, dpi)
    aligned_pages = align_scan_pages(raw_pages, manifest, dpi, allow_partial=allow_partial)
    images_by_page, page_records, quality_reports = _page_artifacts(aligned_pages, manifest, output_dir, dpi)
    available_pages = set(images_by_page)
    parse_manifest = _manifest_for_pages(manifest, available_pages) if allow_partial else manifest

    missing_pages = _missing_pages(manifest, images_by_page)
    review_flags = []
    for report in quality_reports:
        review_flags.extend(f"page {report.page_index}: {flag}" for flag in report.review_flags)
    if missing_pages:
        review_flags.append(f"partial scan: missing page(s): {', '.join(str(page) for page in missing_pages)}")

    if 1 in images_by_page:
        roll = read_roll_number(images_by_page[1], manifest, dpi)
        review_flags.extend(roll.review_flags)
        roll_no = normalize_roll(roll.roll_no or "") if roll.roll_no else None
    else:
        roll = RollRead(
            program=None,
            roll_no=None,
            confidence="low",
            ratios={},
            review_flags=["page 1 is missing; cannot read bubbled roll number"],
        )
        review_flags.extend(roll.review_flags)
        roll_no = None
    student = _match_student(roll_no, roll.program, students, review_flags)

    readings = read_mcq_responses(images_by_page, parse_manifest, dpi)
    mcq_responses, mcq_score, mcq_total = _mcq_payload(readings, parse_manifest, answer_key, review_flags)
    numerical_responses, numerical_score, numerical_total = _numerical_payload(
        images_by_page, manifest, dpi, answer_key, review_flags,
    )

    written_dir = output_dir / "written"
    written_crops = crop_written_responses(
        images_by_page,
        parse_manifest,
        written_dir,
        dpi,
        padding_mm=written_padding_mm,
    )
    written_ocr_reads = read_written_answer_texts(
        written_crops,
        output_dir / "written_ocr",
        written_ocr_backend,
    )
    written_payload = _written_payload(written_crops, output_dir, written_ocr_reads)

    status = "ready" if not review_flags else "needs_review"
    details_path = output_dir / "parse.json"
    payload = {
        "exam_id": manifest["exam_id"],
        "scan_path": str(scan_path),
        "status": status,
        "student": _student_payload(student, roll_no, roll.program),
        "roll_read": asdict(roll),
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


def _error_payload(scan_path: Path, output_dir: Path, error: Exception) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    details_path = output_dir / "parse.json"
    payload = {
        "scan_path": str(scan_path),
        "status": "error",
        "student": {"roll_no": None, "program": None, "name": None, "email": None},
        "roll_read": None,
        "pages": [],
        "mcq_score": None,
        "mcq_total": None,
        "numerical_responses": [],
        "numerical_score": None,
        "numerical_total": None,
        "mcq_responses": [],
        "written_responses": [],
        "review_flags": [str(error)],
        "error_type": type(error).__name__,
        "details_path": str(details_path),
    }
    details_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def parse_scans(
    manifest_path: str | Path,
    scans_path: str | Path,
    students_path: str | Path | None = None,
    answer_key_path: str | Path | None = None,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    dpi: float = 200,
    course_id: str | None = None,
    written_padding_mm: float = 0.0,
    allow_partial: bool = True,
    written_ocr_backend: WrittenOcrBackend | None = None,
) -> tuple[list[dict], Path]:
    """Parse one scan file or every scan file in a folder."""
    manifest = load_manifest(manifest_path)
    course_id = _safe_id(course_id) if course_id else _course_id_from_manifest(manifest)
    root = Path(output_root) / course_id
    sheets_dir = root / "sheets"
    scans = _scan_files(Path(scans_path))
    if not scans:
        raise ScanError(f"no scan files found in {scans_path}")

    results = []
    for scan in scans:
        sheet_dir = sheets_dir / _safe_id(scan.stem)
        try:
            results.append(
                parse_scan(
                    scan,
                    manifest_path=manifest_path,
                    output_dir=sheet_dir,
                    students_path=students_path,
                    answer_key_path=answer_key_path,
                    dpi=dpi,
                    written_padding_mm=written_padding_mm,
                    allow_partial=allow_partial,
                    written_ocr_backend=written_ocr_backend,
                )
            )
        except Exception as exc:
            results.append(_error_payload(scan, sheet_dir, exc))

    index_path = root / "parse_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index = {
        "exam_id": manifest["exam_id"],
        "status_counts": {
            "ready": sum(1 for result in results if result["status"] == "ready"),
            "needs_review": sum(1 for result in results if result["status"] == "needs_review"),
            "error": sum(1 for result in results if result["status"] == "error"),
        },
        "results": [
            {
                "scan_path": result["scan_path"],
                "status": result["status"],
                "roll_no": result["student"]["roll_no"],
                "student_name": result["student"]["name"],
                "mcq_score": result["mcq_score"],
                "mcq_total": result["mcq_total"],
                "numerical_responses": result["numerical_responses"],
                "numerical_score": result["numerical_score"],
                "numerical_total": result["numerical_total"],
                "mcq_answers": _mcq_answer_summary(result),
                "page_quality": [
                    {
                        "page": page["page_index"],
                        "status": page.get("alignment_quality_status"),
                        "score": page.get("alignment_quality_score"),
                        "report_path": page.get("alignment_report_path"),
                        "overlay_path": page.get("alignment_overlay_path"),
                        "sampling_overlay_path": page.get("sampling_overlay_path"),
                    }
                    for page in result["pages"]
                ],
                "review_flags": result["review_flags"],
                "details_path": result["details_path"],
            }
            for result in results
        ],
    }
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    return results, index_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smartomr-parse",
        description=(
            "Parse filled OMR scans into canonical pages, identity reads, MCQ readings, "
            "written-answer crops, and JSON debug artifacts."
        ),
    )
    parser.add_argument("--manifest", required=True, type=Path, help="Generated <exam_id>.manifest.json")
    parser.add_argument("--scans", required=True, type=Path, help="Scan image/PDF file or folder")
    parser.add_argument("--students", type=Path, default=None, help="Optional students.csv roster")
    parser.add_argument("--answer-key", type=Path, default=None, help="Optional MCQ answer_key.csv")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--course-id", default=None, help="Optional output folder override")
    parser.add_argument("--dpi", default=200.0, type=float, help="Canonical reading DPI")
    parser.add_argument(
        "--written-padding-mm",
        default=0.0,
        type=float,
        help="Extra margin around each written-answer crop",
    )
    parser.add_argument(
        "--strict-complete",
        action="store_true",
        help="Fail sheets that are missing any page instead of saving a partial needs-review result",
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
        "--tesseract-cmd",
        default=None,
        help="Optional path to tesseract.exe/tesseract for Tesseract OCR",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        written_ocr_backend = build_written_ocr_backend(
            args.written_answer_ocr,
            tesseract_cmd=args.tesseract_cmd,
            model_name=args.written_ocr_model,
            device=args.written_ocr_device,
            local_files_only=args.written_ocr_local_files_only,
        )
        results, index_path = parse_scans(
            manifest_path=args.manifest,
            scans_path=args.scans,
            students_path=args.students,
            answer_key_path=args.answer_key,
            output_root=args.output_root,
            dpi=args.dpi,
            course_id=args.course_id,
            written_padding_mm=args.written_padding_mm,
            allow_partial=not args.strict_complete,
            written_ocr_backend=written_ocr_backend,
        )
    except (OSError, RuntimeError, ScanError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    ready = sum(1 for result in results if result["status"] == "ready")
    review = sum(1 for result in results if result["status"] == "needs_review")
    errors = sum(1 for result in results if result["status"] == "error")
    print(f"Wrote {index_path}")
    print(f"ready={ready} needs_review={review} error={errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
