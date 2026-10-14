"""Export a minimal, private GPU-training bundle without roster or source scans."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import hashlib
import hmac
import io
import itertools
import json
from pathlib import Path
import secrets
import zipfile

from omr.datasets.train_roll_digit_ensemble import DEFAULT_SEEDS, _member_configs, _sha256
from omr.datasets.train_roll_digit_resnet import LABELS, TrainConfig, _validate_writer_splits


def export_bundle(dataset: Path, ensemble: Path, output: Path, *, platform: str = "kaggle",
                  checkpoint_stage: str = "omr") -> Path:
    if platform not in {"kaggle", "colab"} or checkpoint_stage not in {"omr", "kaggle"}:
        raise ValueError("Unsupported platform or checkpoint stage")
    if output.exists() or output.with_suffix(".ipynb").exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    split_names = ["train", "val"] + (["test"] if (dataset / "test.csv").is_file() else [])
    if platform == "colab" and (checkpoint_stage != "kaggle" or "test" not in split_names):
        raise ValueError("Colab final-test export requires test.csv and Kaggle-only checkpoints")
    for first, second in itertools.combinations(split_names, 2):
        _validate_writer_splits(dataset / f"{first}.csv", dataset / f"{second}.csv")
    package_dir = Path(__file__).resolve().parents[1]
    profile = package_dir / "datasets/configs/roll_digit_ensemble_diverse_v1.json"
    _member_configs(DEFAULT_SEEDS, TrainConfig(), False, profile)
    original = json.loads(ensemble.read_text(encoding="utf-8"))
    if original.get("kind") != "roll_digit_resnet_probability_ensemble":
        raise ValueError("Unsupported initial ensemble")
    members = original["members"]
    if len(members) != 7 or {member["seed"] for member in members} != set(DEFAULT_SEEDS):
        raise ValueError("Expected exactly the original seven seeds")
    entries: dict[str, Path | bytes] = {}
    portable_members = []
    for member in members:
        checkpoint = Path(member["kaggle_pretrained_path"] if checkpoint_stage == "kaggle" else member["model_path"])
        if not checkpoint.is_file():
            checkpoint = ensemble.parent / checkpoint
        digest = _sha256(checkpoint)
        accuracy = member["best_val_accuracy"]
        if checkpoint_stage == "omr" and digest != member.get("checkpoint_sha256"):
            raise ValueError(f"Checkpoint hash mismatch: {checkpoint}")
        if checkpoint_stage == "kaggle":
            import torch
            from omr.datasets.train_roll_digit_resnet import model_from_checkpoint
            payload = torch.load(checkpoint, map_location="cpu", weights_only=True)
            if list(payload.get("labels", [])) != LABELS:
                raise ValueError("Kaggle checkpoint label ordering mismatch")
            model_from_checkpoint(payload)
            accuracy = payload["best_val_accuracy"]
        name = f"member_seed_{member['seed']}.pt"
        entries[f"models/{name}"] = checkpoint
        portable_members.append({"seed": member["seed"], "model_path": name,
                                 "checkpoint_sha256": digest,
                                 "best_val_accuracy": accuracy})
    entries["models/ensemble_manifest.json"] = json.dumps({
        "kind": original["kind"], "member_count": 7, "members": portable_members,
        "image_size": 64, "cell_padding_mm": 1.5,
    }, indent=2).encode()
    key = secrets.token_bytes(32)
    counts = {}
    class_counts = {}
    hashes_by_split = {}
    for split in split_names:
        with (dataset / f"{split}.csv").open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        sanitized = []
        for row in rows:
            relative = Path(row["image_path"].replace("\\", "/"))
            image = (dataset / relative).resolve()
            if not image.is_relative_to(dataset.resolve()) or not image.is_file():
                raise ValueError("Crop is missing or outside the dataset")
            if row["label"] not in LABELS or not row.get("writer_id"):
                raise ValueError("Each crop needs a supported label and writer group")
            digest = _sha256(image)
            if row.get("crop_sha256") and digest != row["crop_sha256"]:
                raise ValueError(f"Crop hash mismatch: {relative}")
            # Neutral paths remove original filenames as well as direct identifiers.
            name = f"cells/{digest}.png"
            entries[f"dataset/{name}"] = image
            group = hmac.new(key, row["writer_id"].encode(), hashlib.sha256).hexdigest()
            sanitized.append({"image_path": name, "label": row["label"],
                              "writer_id": group, "crop_sha256": digest})
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=["image_path", "label", "writer_id", "crop_sha256"])
        writer.writeheader()
        writer.writerows(sanitized)
        entries[f"dataset/{split}.csv"] = buffer.getvalue().encode()
        counts[split] = len(sanitized)
        class_counts[split] = dict(Counter(row["label"] for row in sanitized))
        hashes_by_split[split] = {row["crop_sha256"] for row in sanitized}
    for first, second in itertools.combinations(split_names, 2):
        if hashes_by_split[first] & hashes_by_split[second]:
            raise ValueError("Duplicate crops cross split boundaries")
    for name in ("__init__.py", "datasets/__init__.py", "datasets/train_roll_digit_resnet.py",
                 "datasets/train_roll_digit_ensemble.py", "datasets/configs/roll_digit_ensemble_diverse_v1.json"):
        entries[f"omr/{name}"] = package_dir / name
    runner = package_dir / f"datasets/{platform}_train_roll_ensemble.py"
    entries[f"{platform}_train_roll_ensemble.py"] = runner
    if platform == "colab":
        entries["omr/datasets/evaluate_exported_holdout.py"] = package_dir / "datasets/evaluate_exported_holdout.py"
        if (dataset / "split_report.json").is_file():
            entries["split_report.json"] = dataset / "split_report.json"
    metadata = {
        "kind": "smartomr_private_training_bundle", "schema_version": 1,
        "counts": counts, "member_count": 7,
        "class_counts": class_counts,
        "platform": platform, "checkpoint_stage": checkpoint_stage,
        "privacy": "Only labelled digit crops, pseudonymous writer groups, code and checkpoints; no roster or full scans. Keep private.",
        "source_csv_sha256": {split: _sha256(dataset / f"{split}.csv") for split in split_names},
        "code_sha256": {name: _sha256(path) for name, path in entries.items()
                        if isinstance(path, Path) and name.endswith((".py", ".json"))},
    }
    entries["package_manifest.json"] = json.dumps(metadata, indent=2).encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            if isinstance(value, Path):
                archive.write(value, name)
            else:
                archive.writestr(name, value)
    notebook = {
        "nbformat": 4, "nbformat_minor": 5,
        "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}},
        "cells": [
            {"id": "instructions", "cell_type": "markdown", "metadata": {}, "source": [
                "# SmartOMR Diverse Seven-Member Ensemble\n",
                ("Upload smartomr_kaggle_training.zip as a PRIVATE dataset and attach it. Keep this notebook PRIVATE too. Enable GPU under Settings > Accelerator.\n" if platform == "kaggle"
                 else "Keep this notebook PRIVATE. Select Runtime > Change runtime type > GPU. Run the code cell and upload smartomr_colab_training.zip when prompted.\n"),
                f"Bundle contains {sum(counts.values()):,} OMR crops. Reuses MNIST/Kaggle-pretrained weights. No flips or upside-down rotations.\n",
                f"Counts: {counts}. Only train.csv fits weights; val.csv selects checkpoints; test.csv is evaluated after all seven finish.\n" if platform == "colab"
                else "The same students supplied training data; validate on unseen writers before deployment.\n",
                "For Colab, initial weights are Kaggle-only, not already OMR-fine-tuned. Test data must never be used to tune thresholds.\n" if platform == "colab" else "",
            ]},
            {"id": "train", "cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [],
             "source": runner.read_text(encoding="utf-8").splitlines(keepends=True)},
        ],
    }
    output.with_suffix(".ipynb").write_text(json.dumps(notebook, indent=2), encoding="utf-8")
    print(f"bundle={output}; counts={counts}; members=7")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--initial-ensemble", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platform", choices=("kaggle", "colab"), default="kaggle")
    parser.add_argument("--checkpoint-stage", choices=("omr", "kaggle"), default="omr")
    args = parser.parse_args()
    export_bundle(args.dataset_dir, args.initial_ensemble, args.output,
                  platform=args.platform, checkpoint_stage=args.checkpoint_stage)


if __name__ == "__main__":
    main()
