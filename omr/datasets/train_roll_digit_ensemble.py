"""Train a reproducible ensemble of OMR-fine-tuned roll-digit ResNets."""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

import torch

from omr.datasets.train_roll_digit_resnet import LABELS, TrainConfig, pretrain_kaggle, train


DEFAULT_SEEDS = (557, 558, 559, 560, 561, 562, 563)


def _member_configs(seeds: tuple[int, ...], defaults: TrainConfig, reset_head: bool,
                    path: Path | None) -> dict[int, tuple[TrainConfig, bool]]:
    if path is None:
        return {seed: (replace(defaults, seed=seed), reset_head) for seed in seeds}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1 or not isinstance(payload.get("members"), list)):
        raise ValueError("Member configuration requires schema_version 1 and a members list")
    allowed = {"seed", "dense_hidden_sizes", "dropout", "optimizer", "learning_rate",
               "weight_decay", "reset_head", "epochs", "batch_size"}
    configs = {}
    for row in payload["members"]:
        if not isinstance(row, dict) or set(row) - allowed:
            raise ValueError("Unknown member configuration field")
        seed = row.get("seed")
        if type(seed) is not int or seed not in seeds or seed in configs:
            raise ValueError("Member configuration must contain each requested seed exactly once")
        values = {key: value for key, value in row.items() if key not in {"seed", "reset_head"}}
        if any(type(value) not in {int, float} for key, value in values.items()
               if key in {"learning_rate", "weight_decay", "dropout"}):
            raise ValueError("learning_rate, weight_decay and dropout must be numbers")
        if "dense_hidden_sizes" in values:
            if not isinstance(values["dense_hidden_sizes"], list):
                raise ValueError("dense_hidden_sizes must be a list")
            values["dense_hidden_sizes"] = tuple(values["dense_hidden_sizes"])
        reset = row.get("reset_head", reset_head)
        if not isinstance(reset, bool):
            raise ValueError("reset_head must be boolean")
        configs[seed] = (replace(defaults, seed=seed, **values), reset)
    if set(configs) != set(seeds):
        raise ValueError("Member configuration must contain each requested seed exactly once")
    return configs


def _training_metadata(config: TrainConfig, reset_head: bool) -> dict:
    return {
        **asdict(config),
        "dropout": config.dropout if config.dense_hidden_sizes else 0.0,
        "regularization": "l2_penalty" if config.optimizer == "adam" else "decoupled_weight_decay",
        "head_reinitialized": reset_head,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def train_ensemble(
    dataset_dir: Path,
    kaggle_train_csv: Path | None,
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
    initial_ensemble: Path | None = None,
    cpu_threads: int = 4,
    dense_hidden_sizes: tuple[int, ...] = (),
    dropout: float = 0.25,
    optimizer: str = "adamw",
    reset_head: bool = False,
    member_config: Path | None = None,
) -> Path:
    defaults = TrainConfig(image_size=64, batch_size=batch_size, epochs=epochs,
                           learning_rate=learning_rate, weight_decay=weight_decay,
                           dense_hidden_sizes=dense_hidden_sizes, dropout=dropout, optimizer=optimizer)
    configs = _member_configs(seeds, defaults, reset_head, member_config)
    if any(reset for _config, reset in configs.values()) and initial_ensemble is None:
        raise ValueError("--reset-head requires --initial-ensemble")
    if (kaggle_train_csv is None) == (initial_ensemble is None):
        raise ValueError("Provide either Kaggle pretraining data or an existing ensemble to fine-tune")
    if kaggle_train_csv is not None and not kaggle_train_csv.is_file():
        raise FileNotFoundError(f"Kaggle training CSV not found: {kaggle_train_csv}")
    if not (dataset_dir / "train.csv").is_file() or not (dataset_dir / "val.csv").is_file():
        raise FileNotFoundError(f"OMR dataset must contain train.csv and val.csv: {dataset_dir}")
    if len(seeds) < 2 or len(set(seeds)) != len(seeds):
        raise ValueError("provide at least two unique seeds")
    if cpu_threads < 1:
        raise ValueError("cpu_threads must be positive")

    source_members = {}
    if initial_ensemble is not None:
        source = json.loads(initial_ensemble.read_text(encoding="utf-8"))
        if source.get("kind") != "roll_digit_resnet_probability_ensemble":
            raise ValueError("Initial ensemble manifest has the wrong kind")
        source_members = {int(member["seed"]): member for member in source["members"]}
        if len(source_members) != len(source["members"]) or any(seed not in source_members for seed in seeds):
            raise ValueError("Initial ensemble must contain one checkpoint for each requested seed")
        for seed in seeds:
            member = source_members[seed]
            path = Path(member["model_path"])
            if not path.is_file():
                path = initial_ensemble.parent / path
            if not path.is_file():
                raise FileNotFoundError(f"Initial checkpoint not found: {path}")
            if not member.get("checkpoint_sha256") or _sha256(path) != member["checkpoint_sha256"]:
                raise ValueError(f"Initial checkpoint hash mismatch: {path}")
            checkpoint = torch.load(path, map_location="cpu", weights_only=True)
            if list(checkpoint.get("labels", [])) != LABELS:
                raise ValueError(f"Initial checkpoint label ordering mismatch: {path}")
            config, reset = configs[seed]
            source_hidden = tuple(checkpoint.get("model_config", {}).get("dense_hidden_sizes", ()))
            if not reset and config.dense_hidden_sizes != source_hidden:
                raise ValueError(f"Seed {seed} changes dense head shape; set reset_head to true")
            if path.parent.resolve() == output_dir.resolve():
                raise ValueError("Fine-tuning output must be separate from the initial ensemble")
            member["resolved_checkpoint"] = path
    if (output_dir / "ensemble_manifest.json").exists() or list(output_dir.glob("omr_finetuned_member_*.pt")):
        raise FileExistsError("Output directory already contains an ensemble; choose a new directory")
    if device == "cpu":
        torch.set_num_threads(cpu_threads)

    output_dir.mkdir(parents=True, exist_ok=True)
    plan = {
        "schema_version": 1,
        "member_config_path": str(member_config) if member_config else None,
        "member_config_sha256": _sha256(member_config) if member_config else None,
        "initial_ensemble": str(initial_ensemble) if initial_ensemble else None,
        "initial_ensemble_sha256": _sha256(initial_ensemble) if initial_ensemble else None,
        "dataset_csv_sha256": {name: _sha256(dataset_dir / name) for name in ("train.csv", "val.csv")},
        "members": [_training_metadata(*configs[seed]) for seed in seeds],
    }
    (output_dir / "training_plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    models: list[dict[str, object]] = []
    for index, seed in enumerate(seeds, start=1):
        config, member_reset = configs[seed]
        kaggle_model = output_dir / f"kaggle_pretrained_member_{index:02d}_seed_{seed}.pt"
        model_path = output_dir / f"omr_finetuned_member_{index:02d}_seed_{seed}.pt"
        initial_checkpoint = source_members[seed]["resolved_checkpoint"] if initial_ensemble else kaggle_model
        if initial_ensemble is None:
            print(f"ensemble_member={index}/{len(seeds)} seed={seed} stage=kaggle_pretrain", flush=True)
            pretrain_kaggle(
                kaggle_train_csv,
                model_out=kaggle_model,
                config=replace(config, epochs=kaggle_epochs),
                device_name=device,
                limit=kaggle_limit,
            )
        print(f"ensemble_member={index}/{len(seeds)} seed={seed} stage=omr_finetune", flush=True)
        print("member_training_config=" + json.dumps(_training_metadata(config, member_reset)), flush=True)
        train(
            dataset_dir,
            model_out=model_path,
            config=config,
            device_name=device,
            initial_checkpoint=initial_checkpoint,
            reset_head=member_reset,
        )
        metrics = json.loads(model_path.with_suffix(".metrics.json").read_text(encoding="utf-8"))
        models.append(
            {
                "member": index,
                "seed": seed,
                "initial_checkpoint": str(initial_checkpoint),
                "initial_checkpoint_sha256": _sha256(initial_checkpoint),
                "model_path": model_path.name,
                "metrics_path": model_path.with_suffix(".metrics.json").name,
                "checkpoint_sha256": _sha256(model_path),
                "best_val_accuracy": metrics["best_val_accuracy"],
                "model_config": metrics.get("model_config"),
                "training_config": _training_metadata(config, member_reset),
            }
        )

    manifest_path = output_dir / "ensemble_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "kind": "roll_digit_resnet_probability_ensemble",
                "dataset_dir": str(dataset_dir),
                "kaggle_train_csv": str(kaggle_train_csv) if kaggle_train_csv else None,
                "kaggle_epochs": kaggle_epochs if kaggle_train_csv else 0,
                "kaggle_limit": kaggle_limit,
                "image_size": 64,
                "cell_padding_mm": 1.5,
                "member_count": len(models),
                "members": models,
                "aggregation": "validation-weighted probability average",
                "initial_ensemble": str(initial_ensemble) if initial_ensemble else None,
                "initial_ensemble_sha256": _sha256(initial_ensemble) if initial_ensemble else None,
                "dataset_csv_sha256": {name: _sha256(dataset_dir / name) for name in ("train.csv", "val.csv")},
                "augmentation": "upright_v2: rotations within 5 degrees, mild contrast/brightness, faint ink; no flips",
                "decision_policy": "literal ResNet prediction; roster validation never rewrites OCR",
                "member_config_path": plan["member_config_path"],
                "member_config_sha256": plan["member_config_sha256"],
                "training_config": {
                    "scope": "defaults_overridden_per_member" if member_config else "shared",
                    "dense_hidden_sizes": list(dense_hidden_sizes),
                    "dropout": dropout if dense_hidden_sizes else 0.0,
                    "optimizer": optimizer,
                    "weight_decay": weight_decay,
                    "regularization": "l2_penalty" if optimizer == "adam" else "decoupled_weight_decay",
                    "head_reinitialized": reset_head,
                },
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
    initial = parser.add_mutually_exclusive_group(required=True)
    initial.add_argument("--kaggle-train-csv", type=Path)
    initial.add_argument("--initial-ensemble", type=Path, help="Fine-tune each member from its existing OMR checkpoint")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seeds", default=",".join(map(str, DEFAULT_SEEDS)))
    parser.add_argument("--epochs", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--weight-decay", type=float, default=1e-4)
    parser.add_argument("--optimizer", choices=("adam", "adamw"), default="adamw")
    parser.add_argument("--dense-hidden-sizes", type=int, nargs="+", default=(), help="64 32 gives three total dense layers")
    parser.add_argument("--dropout", type=float, default=0.25)
    parser.add_argument("--reset-head", action="store_true", help="Transfer each backbone, initializing a new head")
    parser.add_argument("--member-config", type=Path, help="Versioned JSON settings for each seed; overrides shared defaults")
    parser.add_argument("--kaggle-epochs", type=int, default=4)
    parser.add_argument("--kaggle-limit", type=int, default=None, help="Optional Kaggle row limit; omitted uses all rows")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--cpu-threads", type=int, default=4)
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
        initial_ensemble=args.initial_ensemble,
        cpu_threads=args.cpu_threads,
        dense_hidden_sizes=tuple(args.dense_hidden_sizes),
        dropout=args.dropout,
        optimizer=args.optimizer,
        reset_head=args.reset_head,
        member_config=args.member_config,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
