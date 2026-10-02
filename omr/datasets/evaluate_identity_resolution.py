"""Evaluate identity grouping and shadow proposals against blind page labels."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from omr.workflows.identity_resolution import resolution_digest


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"identity-resolution report not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _normalized_roll(value: object) -> str:
    return "".join(str(value or "").upper().split())


def _load_labels(path: Path, split: str | None) -> dict[int, dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(f"blind page-label CSV not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        source_rows = list(csv.DictReader(handle))
    labels: dict[int, dict[str, Any]] = {}
    for row_number, row in enumerate(source_rows, start=2):
        if split and str(row.get("split") or "").strip() != split:
            continue
        try:
            source_index = int(str(row.get("source_index") or ""))
            sheet_page = int(str(row.get("true_sheet_page") or ""))
        except ValueError as exc:
            raise ValueError(f"row {row_number}: source_index and true_sheet_page must be labelled integers") from exc
        owner = _normalized_roll(row.get("true_owner_roll"))
        if not owner:
            raise ValueError(f"row {row_number}: true_owner_roll is not labelled")
        if source_index in labels:
            raise ValueError(f"row {row_number}: duplicate source_index {source_index}")
        labels[source_index] = {
            "source_index": source_index,
            "true_sheet_page": sheet_page,
            "true_owner_roll": owner,
            "true_program": str(row.get("true_program") or "").strip().upper(),
            "split": str(row.get("split") or ""),
        }
    if not labels:
        suffix = f" for split {split!r}" if split else ""
        raise ValueError(f"no fully labelled page rows found{suffix}")
    return labels


def _candidate_page_claims(candidate: dict[str, Any]) -> list[tuple[int, int]]:
    return [
        (int(candidate.get("page_one_source_index") or 0), 1),
        (
            int(candidate.get("continuation_source_index") or 0),
            int(candidate.get("sheet_page") or 0),
        ),
    ]


def evaluate(
    labels_csv: Path,
    resolution_path: Path,
    output_dir: Path,
    *,
    split: str | None = None,
) -> Path:
    labels = _load_labels(labels_csv, split)
    resolution = _read_json(resolution_path)
    expected_digest = str(resolution.get("digest") or "")
    actual_digest = resolution_digest(resolution)
    if expected_digest and expected_digest != actual_digest:
        raise ValueError("identity-resolution report digest does not match its contents")

    resolution_pages = {
        int(page.get("source_index") or 0): page
        for page in resolution.get("pages", [])
        if isinstance(page, dict) and page.get("source_index") is not None
    }
    candidates = [candidate for candidate in resolution.get("candidates", []) if isinstance(candidate, dict)]
    candidate_claims: dict[int, list[tuple[str, int, str]]] = defaultdict(list)
    candidate_rows: list[dict[str, Any]] = []
    path_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for candidate in candidates:
        roll_no = _normalized_roll(candidate.get("roll_no"))
        path = str(candidate.get("path") or "")
        claims = _candidate_page_claims(candidate)
        labelled_claims = []
        for source_index, sheet_page in claims:
            if source_index in labels:
                label = labels[source_index]
                correct = (
                    label["true_owner_roll"] == roll_no
                    and label["true_sheet_page"] == sheet_page
                )
                labelled_claims.append(correct)
                candidate_claims[source_index].append((roll_no, sheet_page, str(candidate.get("candidate_id") or "")))
        fully_labelled = len(labelled_claims) == len(claims)
        correct = fully_labelled and all(labelled_claims)
        shadow_eligible = bool(candidate.get("shadow_auto_eligible"))
        path_counts[path]["candidates"] += 1
        path_counts[path]["fully_labelled"] += int(fully_labelled)
        path_counts[path]["correct"] += int(correct)
        path_counts[path]["shadow_auto_eligible"] += int(shadow_eligible)
        path_counts[path]["false_shadow_auto"] += int(shadow_eligible and not correct)
        minimum_confidence = min(
            float((candidate.get("page_one") or {}).get("cell_min_probability") or 0.0),
            float((candidate.get("continuation") or {}).get("cell_min_probability") or 0.0),
        )
        candidate_rows.append(
            {
                "candidate_id": candidate.get("candidate_id"),
                "path": path,
                "proposed_roll": roll_no,
                "page_one_source_index": claims[0][0],
                "continuation_source_index": claims[1][0],
                "continuation_sheet_page": claims[1][1],
                "minimum_cell_confidence": round(minimum_confidence, 6),
                "blocked": bool(candidate.get("blocked")),
                "shadow_auto_eligible": shadow_eligible,
                "fully_labelled": fully_labelled,
                "correct": correct,
                "false_shadow_auto": shadow_eligible and not correct,
            }
        )

    page_rows: list[dict[str, Any]] = []
    for source_index, label in sorted(labels.items()):
        page = resolution_pages.get(source_index, {})
        current_roll = _normalized_roll(page.get("current_roll"))
        detected_sheet_page = int(page.get("sheet_page") or 0)
        current_correct = (
            current_roll == label["true_owner_roll"]
            and detected_sheet_page == label["true_sheet_page"]
        )
        claims = candidate_claims.get(source_index, [])
        correct_suggestions = [
            candidate_id
            for proposed_roll, proposed_page, candidate_id in claims
            if proposed_roll == label["true_owner_roll"] and proposed_page == label["true_sheet_page"]
        ]
        page_rows.append(
            {
                "source_index": source_index,
                "true_owner_roll": label["true_owner_roll"],
                "true_sheet_page": label["true_sheet_page"],
                "current_roll": current_roll,
                "detected_sheet_page": detected_sheet_page,
                "current_attached": bool(current_roll),
                "current_correct": current_correct,
                "suggestion_count": len(claims),
                "correct_suggestion_count": len(correct_suggestions),
                "rejected_but_correctly_suggested": not current_correct and bool(correct_suggestions),
                "correct_candidate_ids": " | ".join(correct_suggestions),
            }
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = output_dir / "candidate_evaluation.csv"
    with candidate_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(candidate_rows[0]) if candidate_rows else ["candidate_id"])
        writer.writeheader()
        writer.writerows(candidate_rows)
    page_path = output_dir / "page_evaluation.csv"
    with page_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(page_rows[0]))
        writer.writeheader()
        writer.writerows(page_rows)

    fully_labelled_candidates = [row for row in candidate_rows if row["fully_labelled"]]
    shadow_auto = [row for row in fully_labelled_candidates if row["shadow_auto_eligible"]]
    false_shadow_auto = [row for row in shadow_auto if not row["correct"]]
    current_attached = [row for row in page_rows if row["current_attached"]]
    current_correct = [row for row in current_attached if row["current_correct"]]
    rejected_but_correct = [row for row in page_rows if row["rejected_but_correctly_suggested"]]
    report = {
        "kind": "identity_resolution_blind_evaluation",
        "labels_csv": str(labels_csv),
        "resolution_path": str(resolution_path),
        "split": split or "all",
        "label_sha256": hashlib.sha256(labels_csv.read_bytes()).hexdigest(),
        "resolution_digest": actual_digest,
        "labelled_pages": len(page_rows),
        "current_grouping": {
            "attached_pages": len(current_attached),
            "correct_pages": len(current_correct),
            "wrong_pages": len(current_attached) - len(current_correct),
            "accuracy": _ratio(len(current_correct), len(current_attached)),
        },
        "suggestions": {
            "fully_labelled_candidates": len(fully_labelled_candidates),
            "correct_candidates": sum(bool(row["correct"]) for row in fully_labelled_candidates),
            "precision": _ratio(
                sum(bool(row["correct"]) for row in fully_labelled_candidates),
                len(fully_labelled_candidates),
            ),
            "rejected_but_correct_pages": len(rejected_but_correct),
        },
        "shadow_auto": {
            "eligible_candidates": len(shadow_auto),
            "false_attachments": len(false_shadow_auto),
            "false_attachment_rate": _ratio(len(false_shadow_auto), len(shadow_auto)),
            "gate_passed": bool(shadow_auto) and not false_shadow_auto,
        },
        "by_path": {
            path: {
                **dict(counts),
                "precision": _ratio(counts["correct"], counts["fully_labelled"]),
                "false_shadow_auto_rate": _ratio(
                    counts["false_shadow_auto"],
                    counts["shadow_auto_eligible"],
                ),
            }
            for path, counts in sorted(path_counts.items())
        },
        "artifacts": {
            "candidate_evaluation": candidate_path.name,
            "page_evaluation": page_path.name,
        },
    }
    report_path = output_dir / "identity_evaluation_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels-csv", type=Path, required=True)
    parser.add_argument("--resolution", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--split", help="Evaluate one labelled split, for example held_out")
    args = parser.parse_args(argv)
    report = evaluate(
        args.labels_csv,
        args.resolution,
        args.output_dir,
        split=args.split,
    )
    print(f"identity_evaluation_report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
