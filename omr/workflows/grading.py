"""Grade current page selections without recognizing identities or regrouping."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
from typing import Callable
import uuid

import numpy as np
from PIL import Image

from omr.contracts import load_manifest
from omr.grading.mcq import read_mcq_responses
from omr.io.csv import load_answer_key
from omr.reader.written import crop_written_responses
from omr.workflows.parse import (
    _manifest_for_pages, _mcq_payload, _numerical_payload,
    _safe_id, _validate_numerical_answer_key, _written_payload,
)
from omr.workflows.review import (
    VERIFIED_INDEX_NAME, _now, _resolve_path, _write_json, _write_verified_index,
    load_or_initialize_verified_index, selected_student_pages, student_selection_key,
)


def validate_grading_key(manifest: dict, answer_key: dict | None) -> None:
    questions = {int(entry["q_no"]) for entry in manifest.get("mcq_block", [])}
    questions.update(int(entry["q_no"]) for entry in manifest.get("numerical_block", []))
    missing = questions - set(answer_key or {})
    if missing:
        raise ValueError("Answer key is missing " + ", ".join(f"Q{q}" for q in sorted(missing)))
    _validate_numerical_answer_key(manifest, answer_key)
    for entry in manifest.get("mcq_block", []):
        key = answer_key[int(entry["q_no"])]
        if not math.isfinite(key.marks) or key.marks < 0:
            raise ValueError(f"Q{entry['q_no']} marks must be non-negative and finite")
        if key.marks and key.answer not in entry["options"]:
            raise ValueError(f"Q{entry['q_no']} answer must be one of its printed options")


def grade_selected_sheets(
    parsed_dir: str | Path,
    manifest_path: str | Path,
    answer_key_path: str | Path | None,
    *,
    dpi: float = 200,
    reviewer: str = "professor-ui",
    progress: Callable[[dict], None] | None = None,
) -> dict:
    root = Path(parsed_dir).resolve()
    manifest = load_manifest(manifest_path)
    key = load_answer_key(answer_key_path, default_marks=float(manifest["exam"].get("marks_per_mcq", 1))) if answer_key_path else None
    validate_grading_key(manifest, key)
    saved, index_path = load_or_initialize_verified_index(root)
    if saved.get("exam_id") != manifest["exam_id"] or int(saved["expected_pages"]) != int(manifest["num_pages"]):
        raise ValueError("Manifest does not match the grouped run")
    snapshot = hashlib.sha256(index_path.read_bytes()).hexdigest()
    index = deepcopy(saved)
    output = root / "grading_runs" / uuid.uuid4().hex
    output.mkdir(parents=True)
    summary = {"graded": 0, "needs_answer_review": 0, "skipped": []}
    claims: dict[int, list[str]] = {}
    for student in index["students"]:
        for page in selected_student_pages(student):
            if page.get("source_index") is not None:
                claims.setdefault(int(page["source_index"]), []).append(student["roll_no"])
    for number, student in enumerate(index["students"], start=1):
        pages = selected_student_pages(student)
        slots = [int(page["page"]) for page in pages]
        sources = [int(page["source_index"]) for page in pages if page.get("source_index") is not None]
        if (student.get("status") not in {"auto_matched", "approved", "verified"}
                or slots != list(range(1, int(manifest["num_pages"]) + 1))
                or any(len(claims[source]) != 1 for source in sources)):
            summary["skipped"].append({"roll_no": student["roll_no"], "reason": "Resolve page ownership before grading"})
            continue
        images = {}
        for page in pages:
            path = _resolve_path(page.get("canonical_image_path"), root)
            if path is None or not path.resolve().is_relative_to(root) or not path.is_file():
                raise ValueError(f"Selected image missing or outside run: {student['roll_no']} page {page['page']}")
            with Image.open(path) as image:
                images[int(page["page"])] = np.asarray(image.convert("L"))
        folder = output / _safe_id(student["roll_no"])
        folder.mkdir()
        flags: list[str] = []
        mcq, mcq_score, mcq_total = _mcq_payload(read_mcq_responses(images, manifest, dpi), manifest, key, flags)
        numerical, numerical_score, numerical_total = _numerical_payload(images, manifest, dpi, key, flags)
        crops = crop_written_responses(images, _manifest_for_pages(manifest, set(images)), folder / "written", dpi)
        if manifest.get("written_block"):
            flags.append("Written answers require professor marking; objective marks exclude written answers")
        original = _resolve_path(student.get("details_path"), root)
        details = json.loads(original.read_text(encoding="utf-8")) if original and original.is_file() else {}
        details.update({
            "student": {"roll_no": student["roll_no"], "program": student.get("program"),
                        "name": student.get("student_name"), "email": student.get("student_email")},
            "grouping_only": False, "mcq_responses": mcq, "numerical_responses": numerical,
            "mcq_score": mcq_score, "mcq_total": mcq_total,
            "numerical_score": numerical_score, "numerical_total": numerical_total,
            "written_responses": _written_payload(crops, folder), "answer_review_flags": flags,
            "review_flags": flags, "details_path": str(folder / "student.json"),
            "pages": [{**page, "page_index": page["page"],
                       "canonical_image_path": str(_resolve_path(page["canonical_image_path"], root))}
                      for page in pages],
        })
        _write_json(folder / "student.json", details)
        student.update({
            "grading_selection_key": student_selection_key(student),
            "grading_details_path": str(folder / "student.json"), "grading_status": "graded",
            "grouping_only": False, "answer_review_flags": flags,
            "mcq_score": mcq_score, "mcq_total": mcq_total,
            "numerical_score": numerical_score, "numerical_total": numerical_total,
        })
        if flags:
            student["review_flags"] = list(dict.fromkeys(student.get("review_flags", []) + flags))
            student["status"] = "needs_review"
            student["eligible_for_email"] = False
            summary["needs_answer_review"] += 1
        student.setdefault("decision_log", []).append({"action": "grade_selected_answers", "reviewer": reviewer,
                                                       "created_at": _now(), "output": str(folder)})
        summary["graded"] += 1
        if progress:
            progress({"processed": number, "total": len(index["students"]), "graded": summary["graded"]})
    if not summary["graded"]:
        raise ValueError("No complete, confirmed selections to grade. Resolve sheet ownership first.")
    # Files are staged first. A changed human selection cannot be overwritten.
    if hashlib.sha256((root / VERIFIED_INDEX_NAME).read_bytes()).hexdigest() != snapshot:
        raise ValueError("Review changed during grading; results were not applied. Try again.")
    index.update(grouping_only=False, grading_summary=summary, grading_output=str(output))
    _write_json(output / "summary.json", summary)
    _write_verified_index(root, index)
    return summary
