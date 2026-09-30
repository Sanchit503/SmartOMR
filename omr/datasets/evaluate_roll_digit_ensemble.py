"""Evaluate the roll-digit ensemble on a labelled holdout crop set.

This is intentionally separate from training. Use a manually verified exam set
that was not used to fit or tune the ensemble before enabling unattended roll
number grouping or email release.
"""
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from PIL import Image

from omr.reader.handwriting import LocalResnetProbabilityEnsembleRollOcr


def _safe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metrics(labels: list[str], rows: list[dict[str, object]]) -> dict[str, Any]:
    total = len(rows)
    correct = sum(row["expected"] == row["predicted"] for row in rows)
    per_label: dict[str, dict[str, float | int]] = {}
    for label in labels:
        true_positive = sum(row["expected"] == label and row["predicted"] == label for row in rows)
        false_positive = sum(row["expected"] != label and row["predicted"] == label for row in rows)
        false_negative = sum(row["expected"] == label and row["predicted"] != label for row in rows)
        precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
        recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_label[label] = {
            "support": true_positive + false_negative,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    return {
        "samples": total,
        "correct": correct,
        "accuracy": correct / total if total else 0.0,
        "per_label": per_label,
    }


def _selective_accuracy(rows: list[dict[str, object]]) -> list[dict[str, float | int]]:
    report: list[dict[str, float | int]] = []
    for min_probability in (0.60, 0.70, 0.80, 0.85, 0.90, 0.95, 0.98):
        for min_margin in (0.00, 0.05, 0.10, 0.20):
            accepted = [
                row
                for row in rows
                if float(row["top_probability"]) >= min_probability and float(row["probability_margin"]) >= min_margin
            ]
            correct = sum(row["expected"] == row["predicted"] for row in accepted)
            report.append(
                {
                    "min_probability": min_probability,
                    "min_margin": min_margin,
                    "accepted": len(accepted),
                    "coverage": len(accepted) / len(rows) if rows else 0.0,
                    "accuracy": correct / len(accepted) if accepted else 0.0,
                    "errors": len(accepted) - correct,
                }
            )
    return report


def evaluate(
    labels_csv: Path,
    output_dir: Path,
    *,
    ensemble_manifest: Path,
    allow_non_independent: bool = False,
) -> Path:
    if not labels_csv.is_file():
        raise FileNotFoundError(f"label CSV not found: {labels_csv}")
    output_dir.mkdir(parents=True, exist_ok=True)
    backend = LocalResnetProbabilityEnsembleRollOcr(ensemble_manifest)
    manifest = json.loads(ensemble_manifest.read_text(encoding="utf-8"))
    training_dataset = Path(str(manifest.get("dataset_dir") or ""))
    training_splits = {
        (training_dataset / split).resolve()
        for split in ("train.csv", "val.csv")
        if str(training_dataset)
    }
    labels_resolved = labels_csv.resolve()
    independent = labels_resolved not in training_splits
    if not independent and not allow_non_independent:
        raise ValueError(
            "the requested label CSV is an ensemble training/validation split, not an independent holdout; "
            "use a separately labelled exam set or pass --allow-non-independent only for a pipeline check"
        )
    rows: list[dict[str, object]] = []
    root = labels_csv.parent
    with labels_csv.open(newline="", encoding="utf-8-sig") as handle:
        source_rows = list(csv.DictReader(handle))
    if not source_rows or "image_path" not in source_rows[0] or "label" not in source_rows[0]:
        raise ValueError("label CSV must contain image_path,label columns")

    for row_number, source in enumerate(source_rows, start=2):
        expected = str(source.get("label") or "").strip()
        if expected not in backend.labels:
            raise ValueError(f"row {row_number}: unsupported label {expected!r}")
        image_path = root / str(source["image_path"])
        if not image_path.is_file():
            raise FileNotFoundError(f"row {row_number}: crop not found: {image_path}")
        prediction = backend.read_digit_image(Image.open(image_path).convert("L"))
        raw = prediction.raw if isinstance(prediction.raw, dict) else {}
        predicted = prediction.text or "blank"
        rows.append(
            {
                "image_path": str(source["image_path"]),
                "roll_no": str(source.get("roll_no") or ""),
                "program": str(source.get("program") or ""),
                "page": str(source.get("page") or ""),
                "cell_index": str(source.get("cell_index") or ""),
                "expected": expected,
                "predicted": predicted,
                "correct": expected == predicted,
                "top_probability": _safe_float(raw.get("top_probability")) or float(prediction.confidence or 0.0),
                "runner_up_probability": _safe_float(raw.get("runner_up_probability")) or 0.0,
                "probability_margin": _safe_float(raw.get("probability_margin")) or 0.0,
            }
        )

    labels = list(backend.labels)
    confusion = {
        expected: {predicted: 0 for predicted in labels}
        for expected in labels
    }
    for row in rows:
        confusion[str(row["expected"])][str(row["predicted"])] += 1

    rolls: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        if row["roll_no"]:
            rolls[(str(row["roll_no"]), str(row["page"]))].append(row)
    roll_rows = []
    for (roll_no, page), group in sorted(rolls.items()):
        group.sort(key=lambda item: int(str(item["cell_index"]) or "0"))
        expected = "".join("" if item["expected"] == "blank" else str(item["expected"]) for item in group)
        predicted = "".join("" if item["predicted"] == "blank" else str(item["predicted"]) for item in group)
        roll_rows.append({"roll_no": roll_no, "page": page, "expected": expected, "predicted": predicted, "correct": expected == predicted})

    prediction_path = output_dir / "digit_predictions.csv"
    with prediction_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    errors_path = output_dir / "digit_errors.csv"
    with errors_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(row for row in rows if not bool(row["correct"]))
    roll_path = output_dir / "roll_predictions.csv"
    with roll_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["roll_no", "page", "expected", "predicted", "correct"])
        writer.writeheader()
        writer.writerows(roll_rows)

    report = {
        "kind": "roll_digit_ensemble_holdout_evaluation",
        "labels_csv": str(labels_csv),
        "independent_holdout": independent,
        "ensemble": backend.provenance(),
        "digit_metrics": _metrics(labels, rows),
        "confusion_matrix": confusion,
        "selective_accuracy": _selective_accuracy(rows),
        "roll_metrics": {
            "samples": len(roll_rows),
            "correct": sum(bool(row["correct"]) for row in roll_rows),
            "exact_match_accuracy": sum(bool(row["correct"]) for row in roll_rows) / len(roll_rows) if roll_rows else None,
        },
        "artifacts": {
            "predictions": prediction_path.name,
            "errors": errors_path.name,
            "roll_predictions": roll_path.name,
        },
    }
    report_path = output_dir / "evaluation_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the seven-model roll-digit ensemble on labelled holdout crops")
    parser.add_argument("--labels-csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--ensemble-manifest",
        type=Path,
        default=Path("data/models/roll_digit_ensemble_7/ensemble_manifest.json"),
    )
    parser.add_argument(
        "--allow-non-independent",
        action="store_true",
        help="Permit evaluation on the training/validation split for a pipeline check only",
    )
    args = parser.parse_args(argv)
    report = evaluate(
        args.labels_csv,
        args.output_dir,
        ensemble_manifest=args.ensemble_manifest,
        allow_non_independent=args.allow_non_independent,
    )
    print(f"evaluation_report={report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
