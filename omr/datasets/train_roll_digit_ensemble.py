"""Train a reproducible ensemble of OMR-fine-tuned roll-digit ResNets."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from omr.datasets.train_roll_digit_resnet import TrainConfig, train


DEFAULT_SEEDS = (557, 558, 559, 560, 561, 562, 563)


def train_ensemble(
    dataset_dir: Path,
    pretrained: Path,
    output_dir: Path,
    *,
    seeds: tuple[int, ...] = DEFAULT_SEEDS,
    epochs: int = 20,
    batch_size: int = 64,
    learning_rate: float = 3e-4,
    weight_decay: float = 1e-4,
    device: str = "cpu",
) -> Path:
    if not pretrained.is_file():
        raise FileNotFoundError(f"Kaggle-pretrained checkpoint not found: {pretrained}")
    if not (dataset_dir / "train.csv").is_file() or not (dataset_dir / "val.csv").is_file():
        raise FileNotFoundError(f"OMR dataset must contain train.csv and val.csv: {dataset_dir}")
    if len(seeds) < 2 or len(set(seeds)) != len(seeds):
        raise ValueError("provide at least two unique seeds")

    output_dir.mkdir(parents=True, exist_ok=True)
    models: list[dict[str, object]] = []
    for index, seed in enumerate(seeds, start=1):
        model_path = output_dir / f"roll_digit_resnet_member_{index:02d}_seed_{seed}.pt"
        print(f"ensemble_member={index}/{len(seeds)} seed={seed}", flush=True)
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
            initial_checkpoint=pretrained,
        )
        metrics = json.loads(model_path.with_suffix(".metrics.json").read_text(encoding="utf-8"))
        models.append(
            {
                "member": index,
                "seed": seed,
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
                "pretrained_checkpoint": str(pretrained),
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
    parser.add_argument("--pretrained", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seeds", default=",".join(map(str, DEFAULT_SEEDS)))
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--weight-decay", type=float, default=1e-4)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)
    seeds = tuple(int(value.strip()) for value in args.seeds.split(",") if value.strip())
    train_ensemble(
        args.dataset_dir,
        args.pretrained,
        args.output_dir,
        seeds=seeds,
        epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.lr,
        weight_decay=args.weight_decay,
        device=args.device,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
