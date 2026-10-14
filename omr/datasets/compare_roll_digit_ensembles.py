"""Compare ensembles on identical crops and explicitly audit prior split exposure."""
from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path

import torch
from torch.utils.data import DataLoader

from omr.datasets.evaluate_exported_holdout import _metrics
from omr.datasets.ensemble_comparison_report import write_visual_report
from omr.datasets.train_roll_digit_resnet import LABELS, RollDigitDataset, model_from_checkpoint


def _rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def exposure_audit(test: list[dict], entry: dict, manifest: dict,
                   writer_catalog: list[dict]) -> tuple[dict, list[str]]:
    writers_by_hash = defaultdict(set)
    for row in writer_catalog:
        writers_by_hash[row["crop_sha256"]].add(row["writer_id"])
    test_writers = {row["writer_id"] for row in test}
    if any(row["writer_id"] not in writers_by_hash[row["crop_sha256"]] for row in test):
        raise ValueError("Test writer IDs do not match the crop catalogue")
    hash_sets, writer_sets = {}, {}
    for split in ("train", "val"):
        path = Path(entry[f"{split}_csv"])
        if manifest.get("dataset_csv_sha256", {}).get(f"{split}.csv") != _hash(path):
            raise ValueError(f"{entry['name']}: {split} CSV hash does not match its manifest")
        rows = _rows(path)
        hash_sets[split] = {row["crop_sha256"] for row in rows}
        unknown = hash_sets[split] - writers_by_hash.keys()
        if unknown:
            raise ValueError(f"{entry['name']}: training crops missing from writer catalogue; exposure cannot be audited")
        writer_sets[split] = set().union(*(writers_by_hash[digest] for digest in hash_sets[split]))
    if writer_sets["train"] & writer_sets["val"]:
        raise ValueError(f"{entry['name']}: training and validation writers overlap")
    states = ["training_seen" if row["writer_id"] in writer_sets["train"] else
              "validation_seen" if row["writer_id"] in writer_sets["val"] else "unused_current_fit"
              for row in test]
    audit = {
        "scope": "Verified current OMR fine-tuning splits; not a complete ancestral-model audit.",
        "exact_training_crop_overlap": sum(row["crop_sha256"] in hash_sets["train"] for row in test),
        "exact_validation_crop_overlap": sum(row["crop_sha256"] in hash_sets["val"] for row in test),
        "training_writer_overlap": len(test_writers & writer_sets["train"]),
        "validation_writer_overlap": len(test_writers & writer_sets["val"]),
        "training_seen_samples": states.count("training_seen"),
        "validation_seen_samples": states.count("validation_seen"),
        "unused_current_fit_samples": states.count("unused_current_fit"),
        "train_csv_sha256": _hash(Path(entry["train_csv"])),
        "val_csv_sha256": _hash(Path(entry["val_csv"])),
    }
    return audit, states


def _confusion(expected: torch.Tensor, probabilities: torch.Tensor, selected: list[int] | None = None):
    matrix = torch.zeros(len(LABELS), len(LABELS), dtype=torch.long)
    indexes = range(len(expected)) if selected is None else selected
    choices = probabilities.argmax(dim=1)
    for index in indexes:
        matrix[int(expected[index]), int(choices[index])] += 1
    return _metrics(matrix)


def _diagnostics(expected, probabilities, states, common_no_training, common_untouched):
    confidence, predicted = probabilities.max(dim=1)
    selective = []
    for threshold in (.80, .90, .95, .98):
        selected = confidence >= threshold
        accepted = int(selected.sum())
        errors = int(((predicted != expected) & selected).sum())
        selective.append({"threshold": threshold, "accepted": accepted,
                          "coverage": accepted / len(expected), "accepted_errors": errors,
                          "accepted_accuracy": (accepted - errors) / accepted if accepted else None})
    return {
        "metrics": _confusion(expected, probabilities),
        "by_exposure": {name: _confusion(expected, probabilities, [i for i, state in enumerate(states) if state == name])
                        for name in ("training_seen", "validation_seen", "unused_current_fit")},
        "common_training_unseen_subset": _confusion(expected, probabilities, common_no_training),
        "common_untouched_current_fit_subset": _confusion(expected, probabilities, common_untouched),
        "selective_diagnostics": selective,
    }


def _prediction_rows(test, expected, probabilities, states, ensemble, model=None):
    confidence, predicted = probabilities.max(dim=1)
    rows = []
    for index, row in enumerate(test):
        record = {"ensemble": ensemble, "image_path": row["image_path"],
                  "crop_sha256": row["crop_sha256"], "expected": row["label"],
                  "predicted": LABELS[int(predicted[index])],
                  "correct": int(predicted[index]) == int(expected[index]),
                  "confidence": float(confidence[index]), "exposure": states[index],
                  "probabilities": json.dumps(dict(zip(LABELS, probabilities[index].tolist())))}
        if model is not None:
            record["model"] = model
        rows.append(record)
    return rows


def compare(config_path: Path, output: Path, *, device: str = "cpu", cpu_threads: int = 4) -> dict:
    if (output / "comparison_report.json").exists():
        raise FileExistsError("Comparison already exists; use a new output directory")
    if cpu_threads < 1:
        raise ValueError("CPU thread count must be positive")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    entries = config["ensembles"]
    if len(entries) < 2 or len({entry["name"] for entry in entries}) != len(entries):
        raise ValueError("At least two uniquely named ensembles are required")
    labels_path = Path(config["labels_csv"])
    test = _rows(labels_path)
    if not test or any(row.get("label") not in LABELS or not row.get("writer_id") for row in test):
        raise ValueError("Test CSV must contain supported labels and writer IDs")
    for row in test:
        image = (labels_path.parent / row["image_path"].replace("\\", "/")).resolve()
        if not image.is_relative_to(labels_path.parent.resolve()) or _hash(image) != row["crop_sha256"]:
            raise ValueError("Test crop is outside the dataset or its hash changed")
    catalog = _rows(Path(config["writer_catalog_csv"]))
    manifests, audits, states = [], [], []
    for entry in entries:
        manifest = json.loads(Path(entry["manifest"]).read_text())
        if manifest.get("kind") != "roll_digit_resnet_probability_ensemble" or manifest.get("image_size") != 64:
            raise ValueError("Unsupported ensemble or input geometry")
        if manifest.get("member_count") != len(manifest["members"]):
            raise ValueError("Incomplete ensemble manifest")
        audit, state = exposure_audit(test, entry, manifest, catalog)
        manifests.append(manifest)
        audits.append(audit)
        states.append(state)
    common_no_training = [index for index in range(len(test)) if all(state[index] != "training_seen" for state in states)]
    common_untouched = [index for index in range(len(test)) if all(state[index] == "unused_current_fit" for state in states)]
    independent = len(common_untouched) == len(test)
    if config.get("require_common_holdout", True) and not independent:
        raise ValueError("This is not a common holdout: training or validation exposure exists. Diagnostic mode must be explicit.")
    if device == "cpu":
        torch.set_num_threads(cpu_threads)
    target = torch.device(device)
    loader = DataLoader(RollDigitDataset(labels_path, labels_path.parent, image_size=64, augment=False), batch_size=64)
    batches = [(images.to(target), labels) for images, labels in loader]
    expected = torch.cat([labels for _images, labels in batches])
    results, paired, member_predictions = [], [], []
    for entry, manifest, audit, state in zip(entries, manifests, audits, states):
        print(f"Evaluating {entry['name']} ({len(test)} identical crops)", flush=True)
        models, weights, checkpoint_hashes = [], [], []
        for member in manifest["members"]:
            path = Path(member["model_path"])
            if not path.is_file():
                path = Path(entry["manifest"]).parent / path
            digest = _hash(path)
            if digest != member["checkpoint_sha256"]:
                raise ValueError("Checkpoint hash mismatch")
            payload = torch.load(path, map_location="cpu", weights_only=True)
            if list(payload.get("labels", [])) != LABELS:
                raise ValueError("Checkpoint label ordering mismatch")
            models.append(model_from_checkpoint(payload).to(target).eval())
            weights.append(max(0.0, float(member.get("best_val_accuracy") or 0.0)))
            checkpoint_hashes.append(digest)
        if not models:
            raise ValueError("No ensemble members")
        weight = torch.tensor(weights if any(weights) else [1.0] * len(models), device=target)
        weight /= weight.sum()
        probabilities = []
        individual_batches = []
        with torch.inference_mode():
            for images, _labels in batches:
                raw = torch.stack([torch.softmax(model(images), dim=1) for model in models])
                probabilities.append((raw * weight[:, None, None]).sum(dim=0).cpu())
                individual_batches.append(raw.cpu())
        probabilities = torch.cat(probabilities)
        individual = torch.cat(individual_batches, dim=1)
        members = []
        for index, (member, model) in enumerate(zip(manifest["members"], models)):
            model_id = f"member_{index + 1:02d}_seed_{member.get('seed', 'unknown')}"
            training = member.get("training_config", {})
            member_result = {
                "model": model_id, "member": index + 1, "seed": member.get("seed"),
                "checkpoint_sha256": checkpoint_hashes[index],
                "dense_layers": len(model.dense_hidden_sizes) + 1,
                "dense_hidden_sizes": list(model.dense_hidden_sizes), "dropout": model.dropout,
                "learning_rate": training.get("learning_rate"), "optimizer": training.get("optimizer"),
                "ensemble_weight": float(weight[index]),
                **_diagnostics(expected, individual[index], state, common_no_training, common_untouched),
            }
            members.append(member_result)
            member_predictions.extend(_prediction_rows(test, expected, individual[index], state, entry["name"], model_id))
            print(f"  {model_id}: accuracy={member_result['metrics']['accuracy']:.4f}, "
                  f"errors={member_result['metrics']['errors']}", flush=True)
        result = {"name": entry["name"], "manifest": entry["manifest"],
                  "manifest_sha256": _hash(Path(entry["manifest"])),
                  "checkpoint_sha256": checkpoint_hashes, "member_count": len(models),
                  "dense_layers": [len(model.dense_hidden_sizes) + 1 for model in models],
                  "exposure": audit, "members": members,
                  **_diagnostics(expected, probabilities, state, common_no_training, common_untouched)}
        results.append(result)
        paired.extend(_prediction_rows(test, expected, probabilities, state, entry["name"]))
        metrics = result["metrics"]
        print(f"{entry['name']}: accuracy={metrics['accuracy']:.4f}, errors={metrics['errors']}", flush=True)
        del models
    report = {"kind": "same_crop_ensemble_comparison", "samples": len(test),
              "labels_csv_sha256": _hash(labels_path), "config_sha256": _hash(config_path),
              "comparison_type": "diagnostic_with_exposure" if not independent else "current_fit_holdout",
              "common_training_unseen_samples": len(common_no_training),
              "common_untouched_current_fit_samples": len(common_untouched),
              "scope": "Raw per-cell probabilities, identical preprocessing, no augmentation, no threshold rejection, no runtime enhancement or page ownership checks.",
              "warning": "Local models used some or all comparison crops for training/validation; raw accuracy cannot rank generalisation." if not independent else None,
              "results": results}
    output.mkdir(parents=True, exist_ok=True)
    (output / "comparison_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    columns = ["ensemble", "samples", "correct", "errors", "accuracy", "macro_precision", "macro_recall", "macro_f1",
               "weighted_precision", "weighted_recall", "weighted_f1", "training_seen", "validation_seen", "unused_current_fit",
               "common_training_unseen_accuracy"]
    with (output / "ensemble_comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for result in results:
            m, a = result["metrics"], result["exposure"]
            writer.writerow({"ensemble": result["name"], "samples": m["test_samples"],
                             **{key: m[key] for key in ("correct", "errors", "accuracy", "macro_precision", "macro_recall", "macro_f1", "weighted_precision", "weighted_recall", "weighted_f1")},
                             "training_seen": a["training_seen_samples"], "validation_seen": a["validation_seen_samples"],
                             "unused_current_fit": a["unused_current_fit_samples"],
                             "common_training_unseen_accuracy": result["common_training_unseen_subset"]["accuracy"]})
    for filename, rows in (("paired_predictions.csv", paired), ("member_predictions.csv", member_predictions)):
        with (output / filename).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    model_columns = ["ensemble", "model", "kind", "seed", "dense_layers", "learning_rate", "optimizer", "dropout",
                     "ensemble_weight", *columns[1:]]
    with (output / "model_comparison.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=model_columns)
        writer.writeheader()
        for result in results:
            for model in [*result["members"], {"model": "ensemble", **result}]:
                m, a = model["metrics"], result["exposure"]
                writer.writerow({"ensemble": result["name"], "model": model["model"],
                                 "kind": "ensemble" if model["model"] == "ensemble" else "member",
                                 **{key: model.get(key) for key in ("seed", "learning_rate", "optimizer", "dropout", "ensemble_weight")},
                                 "dense_layers": model["dense_layers"], "samples": m["test_samples"],
                                 **{key: m[key] for key in columns[2:11]},
                                 "training_seen": a["training_seen_samples"], "validation_seen": a["validation_seen_samples"],
                                 "unused_current_fit": a["unused_current_fit_samples"],
                                 "common_training_unseen_accuracy": model["common_training_unseen_subset"]["accuracy"]})
    write_visual_report(output / "Three_Ensemble_Comparison.html", report, paired, member_predictions, labels_path.parent)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--cpu-threads", type=int, default=4)
    args = parser.parse_args()
    compare(args.config, args.output_dir, device=args.device, cpu_threads=args.cpu_threads)
