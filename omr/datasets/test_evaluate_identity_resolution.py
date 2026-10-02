from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from omr.datasets.evaluate_identity_resolution import evaluate
from omr.workflows.identity_resolution import resolution_digest


def _write_labels(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["source_index", "split", "true_sheet_page", "true_owner_roll", "true_program"],
        )
        writer.writeheader()
        writer.writerows(rows)


def _candidate(candidate_id: str, roll_no: str, page_one: int, continuation: int, *, eligible: bool) -> dict:
    return {
        "candidate_id": candidate_id,
        "path": "B",
        "roll_no": roll_no,
        "page_one_source_index": page_one,
        "continuation_source_index": continuation,
        "sheet_page": 2,
        "page_one": {"cell_min_probability": 0.42},
        "continuation": {"cell_min_probability": 0.91},
        "blocked": not eligible,
        "shadow_auto_eligible": eligible,
    }


def _write_resolution(path: Path, candidates: list[dict]) -> None:
    payload = {
        "schema_version": 1,
        "pages": [
            {"source_index": 1, "sheet_page": 1, "current_roll": "2024001"},
            {"source_index": 2, "sheet_page": 2, "current_roll": None},
            {"source_index": 3, "sheet_page": 1, "current_roll": "2024002"},
            {"source_index": 4, "sheet_page": 2, "current_roll": None},
        ],
        "candidates": candidates,
    }
    payload["digest"] = resolution_digest(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_identity_evaluation_measures_false_auto_and_rejected_correct_pages(tmp_path: Path):
    labels = tmp_path / "page_labels_blind.csv"
    _write_labels(
        labels,
        [
            {"source_index": 1, "split": "held_out", "true_sheet_page": 1, "true_owner_roll": "2024001", "true_program": "BTECH"},
            {"source_index": 2, "split": "held_out", "true_sheet_page": 2, "true_owner_roll": "2024001", "true_program": "BTECH"},
            {"source_index": 3, "split": "held_out", "true_sheet_page": 1, "true_owner_roll": "2024003", "true_program": "BTECH"},
            {"source_index": 4, "split": "held_out", "true_sheet_page": 2, "true_owner_roll": "2024003", "true_program": "BTECH"},
        ],
    )
    resolution = tmp_path / "identity_resolution.json"
    _write_resolution(
        resolution,
        [
            _candidate("correct", "2024001", 1, 2, eligible=True),
            _candidate("wrong", "2024002", 3, 4, eligible=True),
        ],
    )

    report_path = evaluate(labels, resolution, tmp_path / "report", split="held_out")
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["current_grouping"] == {
        "attached_pages": 2,
        "correct_pages": 1,
        "wrong_pages": 1,
        "accuracy": 0.5,
    }
    assert report["suggestions"]["correct_candidates"] == 1
    assert report["suggestions"]["rejected_but_correct_pages"] == 1
    assert report["shadow_auto"]["eligible_candidates"] == 2
    assert report["shadow_auto"]["false_attachments"] == 1
    assert report["shadow_auto"]["false_attachment_rate"] == 0.5
    assert report["shadow_auto"]["gate_passed"] is False
    assert (tmp_path / "report" / "candidate_evaluation.csv").is_file()
    assert (tmp_path / "report" / "page_evaluation.csv").is_file()


def test_identity_evaluation_refuses_unlabelled_ground_truth(tmp_path: Path):
    labels = tmp_path / "page_labels_blind.csv"
    _write_labels(
        labels,
        [
            {"source_index": 1, "split": "held_out", "true_sheet_page": "", "true_owner_roll": "", "true_program": ""},
        ],
    )
    resolution = tmp_path / "identity_resolution.json"
    _write_resolution(resolution, [])

    with pytest.raises(ValueError, match="must be labelled integers"):
        evaluate(labels, resolution, tmp_path / "report", split="held_out")
