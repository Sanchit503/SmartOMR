"""Train a reproducible ensemble of OMR-fine-tuned roll-digit ResNets."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from omr.datasets.train_roll_digit_resnet import TrainConfig, pretrain_kaggle, train


DEFAULT_SEEDS = (557, 558, 559, 560, 561, 562, 563)


def train_ensemble(
    dataset_dir: Path,
    kaggle_train_csv: Path,
    output_dir: Path,
    *,
    seeds: tuple[int, ...] = DEFAULT_SEEDS,
    epochs: int = 20,
    batch_size: int = 64,
    learning_rate: float = 3e-4,
    weight_decay: float = 1e-4,
    kaggle_epochs: int = 4,
    kaggle_limit: int | None = None,
    device: str = "cpu",
) -> Path:
    if not kaggle_train_csv.is_file():
        raise FileNotFoundError(f"Kaggle training CSV not found: {kaggle_train_csv}")
    if not (dataset_dir / "train.csv").is_file() or not (dataset_dir / "val.csv").is_file():
        raise FileNotFoundError(f"OMR dataset must contain train.csv and val.csv: {dataset_dir}")
    if len(seeds) < 2 or len(set(seeds)) != len(seeds):
        raise ValueError("provide at least two unique seeds")

    output_dir.mkdir(parents=True, exist_ok=True)
    models: list[dict[str, object]] = []
    for index, seed in enumerate(seeds, start=1):
        kaggle_model = output_dir / f"kaggle_pretrained_member_{index:02d}_seed_{seed}.pt"
        model_path = output_dir / f"omr_finetuned_member_{index:02d}_seed_{seed}.pt"
        print(f"ensemble_member={index}/{len(seeds)} seed={seed} stage=kaggle_pretrain", flush=True)
        pretrain_kaggle(
            kaggle_train_csv,
            model_out=kaggle_model,
            config=TrainConfig(
                image_size=64,
                batch_size=batch_size,
                epochs=kaggle_epochs,
                learning_rate=learning_rate,
                weight_decay=weight_decay,
                seed=seed,
            ),
            device_name=device,
            limit=kaggle_limit,
        )
        print(f"ensemble_member={index}/{len(seeds)} seed={seed} stage=omr_finetune", flush=True)
        train(
            dataset_dir,
            model_out=model_path,
            config=TrainConfig(
                image_size=64,
                batch_size=batch_size,
                epochs=epochs,
                learning_rate=learning_rate,
                weight_decay=weight_decay,
                seed=seed,
            ),
            device_name=device,
            initial_checkpoint=kaggle_model,
        )
        metrics = json.loads(model_path.with_suffix(".metrics.json").read_text(encoding="utf-8"))
        models.append(
            {
                "member": index,
                "seed": seed,
                "kaggle_pretrained_path": str(kaggle_model),
                "model_path": str(model_path),
                "metrics_path": str(model_path.with_suffix(".metrics.json")),
                "best_val_accuracy": metrics["best_val_accuracy"],
            }
        )

    manifest_path = output_dir / "ensemble_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "kind": "roll_digit_resnet_probability_ensemble",
                "dataset_dir": str(dataset_dir),
                "kaggle_train_csv": str(kaggle_train_csv),
                "kaggle_epochs": kaggle_epochs,
                "kaggle_limit": kaggle_limit,
                "member_count": len(models),
                "members": models,
                "aggregation": "validation-weighted probability average",
                "decision_policy": "use roster-constrained decoding; send close candidates to review",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"ensemble_manifest={manifest_path}")
    return manifest_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train a seven-member OMR roll digit ResNet ensemble")
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--kaggle-train-csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seeds", default=",".join(map(str, DEFAULT_SEEDS)))
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--weight-decay", type=float, default=1e-4)
    parser.add_argument("--kaggle-epochs", type=int, default=4)
    parser.add_argument("--kaggle-limit", type=int, default=None, help="Optional Kaggle row limit; omitted uses all rows")
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)
    seeds = tuple(int(value.strip()) for value in args.seeds.split(",") if value.strip())
    train_ensemble(
        args.dataset_dir,
        args.kaggle_train_csv,
        args.output_dir,
        seeds=seeds,
        epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.lr,
        weight_decay=args.weight_decay,
        kaggle_epochs=args.kaggle_epochs,
        kaggle_limit=args.kaggle_limit,
        device=args.device,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
