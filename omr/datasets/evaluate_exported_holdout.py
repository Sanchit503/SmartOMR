"""Report final-test digit metrics without selecting checkpoints on the test set."""
from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
from pathlib import Path

import torch
from torch.utils.data import DataLoader

from omr.datasets.train_roll_digit_resnet import LABELS, RollDigitDataset, model_from_checkpoint, _validate_writer_splits


def _metrics(matrix: torch.Tensor) -> dict:
    per_class = {}
    samples = int(matrix.sum())
    correct = int(matrix.diag().sum())
    for index, label in enumerate(LABELS):
        tp = int(matrix[index, index])
        support, assigned = int(matrix[index].sum()), int(matrix[:, index].sum())
        precision = tp / assigned if assigned else 0.0
        recall = tp / support if support else 0.0
        per_class[label] = {"support": support, "precision": precision, "recall": recall,
                            "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0}
    averages = {}
    for metric in ("precision", "recall", "f1"):
        averages[f"macro_{metric}"] = sum(item[metric] for item in per_class.values()) / len(LABELS)
        averages[f"weighted_{metric}"] = sum(item[metric] * item["support"] for item in per_class.values()) / samples if samples else 0.0
    return {"test_samples": samples, "correct": correct, "errors": samples - correct,
            "accuracy": correct / samples if samples else None, "per_class": per_class,
            "confusion_matrix": matrix.tolist(), "labels": LABELS, **averages}


def evaluate(dataset: Path, manifest_path: Path, output: Path, device: str = "cpu") -> dict:
    splits = {}
    for name in ("train", "val", "test"):
        with (dataset / f"{name}.csv").open(newline="", encoding="utf-8") as handle:
            splits[name] = list(csv.DictReader(handle))
    for first, second in itertools.combinations(splits, 2):
        _validate_writer_splits(dataset / f"{first}.csv", dataset / f"{second}.csv")
        if {row["crop_sha256"] for row in splits[first]} & {row["crop_sha256"] for row in splits[second]}:
            raise ValueError("Duplicate crops cross split boundaries")
    manifest = json.loads(manifest_path.read_text())
    for split in ("train", "val"):
        digest = hashlib.sha256((dataset / f"{split}.csv").read_bytes()).hexdigest()
        if manifest.get("dataset_csv_sha256", {}).get(f"{split}.csv") != digest:
            raise ValueError("Evaluation dataset does not match the training manifest")
    if not splits["test"] or any(row["label"] not in LABELS for row in splits["test"]):
        raise ValueError("Test CSV must contain supported, labelled examples")
    for row in splits["test"]:
        image = (dataset / row["image_path"]).resolve()
        if not image.is_relative_to(dataset.resolve()):
            raise ValueError("Test crop is outside the dataset")
        if hashlib.sha256(image.read_bytes()).hexdigest() != row["crop_sha256"]:
            raise ValueError("Test crop hash mismatch")
    models, weights = [], []
    target = torch.device(device)
    for member in manifest["members"]:
        path = manifest_path.parent / member["model_path"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != member["checkpoint_sha256"]:
            raise ValueError("Checkpoint hash mismatch")
        models.append(model_from_checkpoint(torch.load(path, map_location="cpu", weights_only=True)).to(target).eval())
        weights.append(max(0.0, float(member.get("best_val_accuracy") or 0.0)))
    if not models:
        raise ValueError("No ensemble members")
    if not any(weights):
        weights = [1.0] * len(models)
    weights = torch.tensor(weights, device=target)
    weights /= weights.sum()
    loader = DataLoader(RollDigitDataset(dataset / "test.csv", dataset, image_size=64, augment=False), batch_size=64)
    matrix = torch.zeros(len(LABELS), len(LABELS), dtype=torch.long)
    member_matrices = [torch.zeros_like(matrix) for _ in models]
    member_ids = [f"member_{index:02d}_seed_{member.get('seed', 'unknown')}"
                  for index, member in enumerate(manifest["members"], start=1)]
    predictions = []
    member_predictions = []
    offset = 0
    with torch.inference_mode():
        for images, labels in loader:
            images = images.to(target)
            probs = torch.stack([torch.softmax(model(images), dim=1) for model in models])
            mean = (probs * weights[:, None, None]).sum(dim=0).cpu()
            individual = probs.cpu()
            for index, expected in enumerate(labels.tolist()):
                predicted = int(mean[index].argmax())
                matrix[expected, predicted] += 1
                predictions.append({"image_path": splits["test"][offset]["image_path"],
                                    "expected": LABELS[expected], "predicted": LABELS[predicted],
                                    "confidence": float(mean[index, predicted]),
                                    "probabilities": json.dumps(dict(zip(LABELS, mean[index].tolist())))})
                for member_index, member_probs in enumerate(individual):
                    choice = int(member_probs[index].argmax())
                    member_matrices[member_index][expected, choice] += 1
                    member_predictions.append({"model": member_ids[member_index],
                                               "image_path": splits["test"][offset]["image_path"],
                                               "expected": LABELS[expected], "predicted": LABELS[choice],
                                               "correct": expected == choice,
                                               "confidence": float(member_probs[index, choice]),
                                               "probabilities": json.dumps(dict(zip(LABELS, member_probs[index].tolist())))})
                offset += 1
    reports = [{"model": identifier, "seed": member.get("seed"),
                "model_path": member["model_path"], "checkpoint_sha256": member["checkpoint_sha256"],
                "dense_layers": len(model.dense_hidden_sizes) + 1, **_metrics(values)}
               for identifier, member, model, values in zip(member_ids, manifest["members"], models, member_matrices)]
    report = {**_metrics(matrix), "member_reports": reports,
              "test_used_for_current_run_training_or_checkpoint_selection": False,
              "scope": "Raw per-cell ensemble probabilities; not a production page-grouping or ownership benchmark."}
    output.mkdir(parents=True, exist_ok=True)
    columns = ["model", "seed", "dense_layers", "test_samples", "correct", "errors", "accuracy",
               "macro_precision", "macro_recall", "macro_f1", "weighted_precision", "weighted_recall", "weighted_f1"]
    with (output / "model_comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows([*reports, {"model": "ensemble", **_metrics(matrix)}])
    (output / "test_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if predictions:
        with (output / "test_predictions.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(predictions[0]))
            writer.writeheader()
            writer.writerows(predictions)
    if member_predictions:
        with (output / "per_model_predictions.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(member_predictions[0]))
            writer.writeheader()
            writer.writerows(member_predictions)
    print(json.dumps({"test_samples": offset, "accuracy": report["accuracy"]}), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--ensemble-manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    evaluate(args.dataset_dir, args.ensemble_manifest, args.output_dir, args.device)
