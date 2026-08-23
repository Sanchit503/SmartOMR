from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np

from omr.contracts import load_manifest
from omr.grading.mcq import MCQOutcome, read_mcq_responses

from .csv_io import load_answer_key, load_students, normalize_roll, write_results_csv
from .cv_scan import IMAGE_EXTENSIONS, ScanError, align_scan_pages, load_scan_pages
from .models import EvaluationResult, Student
from .roll_reader import read_roll_number


SCAN_EXTENSIONS = IMAGE_EXTENSIONS | {".pdf"}


def _student_fields(student: Student | None) -> tuple[str | None, str | None]:
    if student is None:
        return None, None
    return student.name, student.email


def _status(review_flags: list[str]) -> str:
    return "ready" if not review_flags else "needs_review"


def _details_path(output_dir: Path | None, scan_path: Path) -> Path | None:
    if output_dir is None:
        return None
    details_dir = output_dir / "details"
    details_dir.mkdir(parents=True, exist_ok=True)
    return details_dir / f"{scan_path.stem}.json"


def _write_details(path: Path | None, payload: dict) -> str | None:
    if path is None:
        return None
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(path)


def evaluate_scan(
    scan_path: str | Path,
    manifest_path: str | Path,
    students_path: str | Path,
    answer_key_path: str | Path,
    output_dir: str | Path | None = None,
    dpi: float = 200,
) -> EvaluationResult:
    scan_path = Path(scan_path)
    output_path = Path(output_dir) if output_dir is not None else None
    if output_path is not None:
        output_path.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(manifest_path)
    students = load_students(students_path)
    default_marks = float(manifest["exam"].get("marks_per_mcq", 1.0))
    answer_key = load_answer_key(answer_key_path, default_marks=default_marks)

    raw_pages = load_scan_pages(scan_path, dpi)
    aligned_pages = align_scan_pages(raw_pages, manifest, dpi)
    page_images = {page_no: np.asarray(page.image) for page_no, page in aligned_pages.items()}

    roll = read_roll_number(page_images[1], manifest, dpi)
    review_flags = list(roll.review_flags)

    student = None
    roll_no = normalize_roll(roll.roll_no or "") if roll.roll_no else None
    if roll_no:
        student = students.get(roll_no)
        if student is None:
            review_flags.append(f"roll number {roll_no} was not found in students.csv")
        elif roll.program and student.program and student.program != roll.program:
            review_flags.append(
                f"roll number {roll_no} is marked as {roll.program}, but roster has {student.program}"
            )
    else:
        review_flags.append("roll number could not be decoded confidently")

    readings = read_mcq_responses(page_images, manifest, dpi)
    answers: dict[int, str | None] = {}
    score = 0.0
    total = 0.0
    grade_details: list[dict] = []
    for reading in readings:
        q_no = reading.q_no
        answers[q_no] = reading.selected_option
        key_entry = answer_key.get(q_no)
        if key_entry is None:
            review_flags.append(f"answer key is missing Q{q_no}")
            correct_answer = None
            marks = 0.0
        else:
            correct_answer = key_entry.answer
            marks = key_entry.marks
            total += marks

        is_correct = (
            key_entry is not None
            and reading.outcome == MCQOutcome.ANSWERED
            and reading.selected_option == correct_answer
        )
        marks_awarded = marks if is_correct else 0.0
        score += marks_awarded
        if reading.needs_human_review:
            review_flags.append(f"Q{q_no}: {reading.review_reason}")
        grade_details.append(
            {
                "q_no": q_no,
                "selected": reading.selected_option,
                "correct": correct_answer,
                "outcome": reading.outcome.value,
                "marks": marks,
                "marks_awarded": marks_awarded,
                "confidence": reading.confidence.value,
                "needs_human_review": reading.needs_human_review,
                "review_reason": reading.review_reason,
                "fill_ratios": reading.fill_ratios,
                "ink_densities": reading.ink_densities,
            }
        )

    student_name, student_email = _student_fields(student)
    result = EvaluationResult(
        scan_path=str(scan_path),
        status=_status(review_flags),
        roll_no=roll_no,
        program=roll.program,
        student_name=student_name,
        student_email=student_email,
        score=score,
        total=total,
        answers=answers,
        review_flags=review_flags,
    )

    details = {
        "result": asdict(result),
        "roll_read": asdict(roll),
        "pages": [
            {
                "page_index": page.page_index,
                "source_index": page.source_index,
                "alignment_confidence": page.alignment_confidence,
                "page_mark_confidence": page.page_mark_confidence,
            }
            for page in sorted(aligned_pages.values(), key=lambda p: p.page_index)
        ],
        "grades": grade_details,
    }
    details_file = _write_details(_details_path(output_path, scan_path), details)
    if details_file is None:
        return result
    return EvaluationResult(**{**asdict(result), "details_path": details_file})


def _scan_files(scans_path: Path) -> list[Path]:
    if scans_path.is_file():
        return [scans_path]
    files = [p for p in scans_path.iterdir() if p.is_file() and p.suffix.lower() in SCAN_EXTENSIONS]
    return sorted(files)


def _error_result(scan_path: Path, error: Exception, output_dir: Path | None = None) -> EvaluationResult:
    details_file = None
    if output_dir is not None:
        details_file = _write_details(
            _details_path(output_dir, scan_path),
            {
                "scan_path": str(scan_path),
                "status": "error",
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )
    return EvaluationResult(
        scan_path=str(scan_path),
        status="error",
        roll_no=None,
        program=None,
        student_name=None,
        student_email=None,
        score=0.0,
        total=0.0,
        answers={},
        review_flags=[str(error)],
        details_path=details_file,
    )


def batch_evaluate(
    course_id: str,
    manifest_path: str | Path,
    students_path: str | Path,
    answer_key_path: str | Path,
    scans_path: str | Path,
    output_root: str | Path = "prototype_eval/data",
    dpi: float = 200,
) -> tuple[list[EvaluationResult], Path]:
    course_id = course_id.strip().replace(" ", "_")
    output_dir = Path(output_root) / course_id / "results"
    output_dir.mkdir(parents=True, exist_ok=True)
    scans = _scan_files(Path(scans_path))
    if not scans:
        raise ScanError(f"no scan files found in {scans_path}")

    results: list[EvaluationResult] = []
    for scan in scans:
        try:
            results.append(
                evaluate_scan(
                    scan,
                    manifest_path=manifest_path,
                    students_path=students_path,
                    answer_key_path=answer_key_path,
                    output_dir=output_dir,
                    dpi=dpi,
                )
            )
        except Exception as exc:
            results.append(_error_result(scan, exc, output_dir=output_dir))

    summary_path = write_results_csv(results, output_dir / "results.csv")
    return results, summary_path
