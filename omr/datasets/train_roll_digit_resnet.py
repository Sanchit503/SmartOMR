"""Train a small ResNet-style classifier for handwritten roll digit cells."""
from __future__ import annotations

import argparse
import csv
import json
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch
from PIL import Image, ImageEnhance, ImageOps
from torch import nn
from torch.utils.data import DataLoader, Dataset


LABELS = ["blank", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]
LABEL_TO_ID = {label: index for index, label in enumerate(LABELS)}


@dataclass(frozen=True)
class TrainConfig:
    image_size: int = 64
    batch_size: int = 64
    epochs: int = 30
    learning_rate: float = 1e-3
    weight_decay: float = 1e-4
    seed: int = 557
    dense_hidden_sizes: tuple[int, ...] = ()
    dropout: float = 0.25
    optimizer: str = "adamw"

    def __post_init__(self) -> None:
        if not np.isfinite(self.learning_rate) or self.learning_rate <= 0:
            raise ValueError("learning rate must be finite and positive")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1
               for value in (self.epochs, self.batch_size)):
            raise ValueError("epochs and batch size must be positive integers")
        if self.optimizer not in {"adam", "adamw"}:
            raise ValueError("optimizer must be adam or adamw")
        if not 0 <= self.dropout < 1:
            raise ValueError("dropout must be in [0, 1)")
        if any(isinstance(size, bool) or not isinstance(size, int) or size < 1 for size in self.dense_hidden_sizes):
            raise ValueError("dense hidden sizes must be positive integers")
        if not np.isfinite(self.weight_decay) or self.weight_decay < 0:
            raise ValueError("weight decay must be finite and nonnegative")


class RollDigitDataset(Dataset):
    def __init__(self, csv_path: Path, root_dir: Path, *, image_size: int, augment: bool = False) -> None:
        self.csv_path = csv_path
        self.root_dir = root_dir
        self.image_size = image_size
        self.augment = augment
        with csv_path.open(newline="", encoding="utf-8") as handle:
            self.rows = [row for row in csv.DictReader(handle) if row.get("label") in LABEL_TO_ID]
        if not self.rows:
            raise ValueError(f"no labelled rows found in {csv_path}")

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> tuple[torch.Tensor, torch.Tensor]:
        row = self.rows[index]
        image_path = self.root_dir / str(row["image_path"])
        image = Image.open(image_path).convert("L")
        image = ImageOps.autocontrast(image)
        if self.augment:
            image = _augment_image(image)
        image = ImageOps.pad(image, (self.image_size, self.image_size), color=255)
        array = np.asarray(image, dtype=np.float32) / 255.0
        # Invert so ink is positive signal, then roughly normalize.
        array = (1.0 - array - 0.15) / 0.35
        tensor = torch.from_numpy(array).unsqueeze(0)
        label = torch.tensor(LABEL_TO_ID[str(row["label"])], dtype=torch.long)
        return tensor, label


class KaggleDigitCsvDataset(Dataset):
    """Kaggle Digit Recognizer CSV: label,pixel0,...,pixel783.

    Kaggle digits are white ink on a black 28x28 background. We invert them to
    match the OMR crop convention: black ink on white paper before the shared
    normalization step.
    """

    def __init__(
        self,
        csv_path: Path,
        *,
        image_size: int,
        augment: bool = False,
        val_fraction: float = 0.10,
        split: str = "train",
        seed: int = 557,
        limit: int | None = None,
    ) -> None:
        self.csv_path = csv_path
        self.image_size = image_size
        self.augment = augment
        with csv_path.open(newline="", encoding="utf-8") as handle:
            rows = [row for row in csv.DictReader(handle) if row.get("label") in LABEL_TO_ID]
        if limit is not None:
            rows = rows[:limit]
        if not rows:
            raise ValueError(f"no labelled Kaggle rows found in {csv_path}")
        rng = random.Random(seed)
        indices = list(range(len(rows)))
        rng.shuffle(indices)
        val_size = max(1, int(round(len(rows) * val_fraction)))
        val_indices = set(indices[:val_size])
        if split == "train":
            self.rows = [row for index, row in enumerate(rows) if index not in val_indices]
        elif split == "val":
            self.rows = [row for index, row in enumerate(rows) if index in val_indices]
        else:
            raise ValueError("split must be train or val")
        if not self.rows:
            raise ValueError(f"Kaggle {split} split is empty")

    def __len__(self) -> int:
        return len(self.rows)

    def __getitem__(self, index: int) -> tuple[torch.Tensor, torch.Tensor]:
        row = self.rows[index]
        pixels = np.array([int(row[f"pixel{i}"]) for i in range(784)], dtype=np.uint8).reshape(28, 28)
        image = Image.fromarray(255 - pixels, mode="L")
        image = ImageOps.autocontrast(image)
        if self.augment:
            image = _augment_image(image)
        image = ImageOps.pad(image, (self.image_size, self.image_size), color=255)
        array = np.asarray(image, dtype=np.float32) / 255.0
        array = (1.0 - array - 0.15) / 0.35
        tensor = torch.from_numpy(array).unsqueeze(0)
        label = torch.tensor(LABEL_TO_ID[str(row["label"])], dtype=torch.long)
        return tensor, label


def _augment_image(image: Image.Image) -> Image.Image:
    angle = random.uniform(-5.0, 5.0)
    image = image.rotate(angle, resample=Image.Resampling.BILINEAR, expand=True, fillcolor=255)
    if random.random() < 0.45:
        image = ImageEnhance.Contrast(image).enhance(random.uniform(0.85, 1.25))
    if random.random() < 0.35:
        image = ImageEnhance.Brightness(image).enhance(random.uniform(0.90, 1.10))
    if random.random() < 0.40:
        image = _simulate_faint_pencil(image)
    return image


def _simulate_faint_pencil(image: Image.Image) -> Image.Image:
    """Fade ink and add mild uneven illumination without erasing its shape.

    Real pencil marks can be visible to a person yet be only a small local
    contrast change after scanning. Training on this variation makes the
    ResNet less dependent on dark pen strokes. It is applied only to training
    crops, never validation data.
    """
    array = np.asarray(image.convert("L"), dtype=np.float32)
    ink_strength = random.uniform(0.65, 0.95)
    height, width = array.shape
    left = random.uniform(-7.0, 7.0)
    right = random.uniform(-7.0, 7.0)
    illumination = np.linspace(left, right, width, dtype=np.float32)[None, :]
    illumination = np.repeat(illumination, height, axis=0)
    background = 255.0 - np.abs(illumination)
    faded = background - (255.0 - array) * ink_strength
    return Image.fromarray(np.clip(faded, 0, 255).astype(np.uint8), mode="L")


class ResidualBlock(nn.Module):
    def __init__(self, channels: int) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv2d(channels, channels, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(channels, channels, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(channels),
        )
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.relu(x + self.net(x))


class SmallRollDigitResNet(nn.Module):
    def __init__(
        self,
        num_classes: int = len(LABELS),
        *,
        dense_hidden_sizes: tuple[int, ...] = (),
        dropout: float = 0.25,
    ) -> None:
        super().__init__()
        TrainConfig(dense_hidden_sizes=tuple(dense_hidden_sizes), dropout=dropout)
        self.dense_hidden_sizes = tuple(dense_hidden_sizes)
        self.dropout = dropout if dense_hidden_sizes else 0.0
        self.num_classes = num_classes
        self.stem = nn.Sequential(
            nn.Conv2d(1, 32, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(32),
            nn.ReLU(inplace=True),
        )
        self.stage1 = nn.Sequential(ResidualBlock(32), nn.MaxPool2d(2))
        self.to64 = nn.Sequential(
            nn.Conv2d(32, 64, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
        )
        self.stage2 = nn.Sequential(ResidualBlock(64), nn.MaxPool2d(2))
        self.to128 = nn.Sequential(
            nn.Conv2d(64, 128, kernel_size=3, padding=1, bias=False),
            nn.BatchNorm2d(128),
            nn.ReLU(inplace=True),
        )
        self.stage3 = nn.Sequential(ResidualBlock(128), nn.AdaptiveAvgPool2d((1, 1)))
        if self.dense_hidden_sizes:
            layers: list[nn.Module] = []
            in_features = 128
            for size in self.dense_hidden_sizes:
                layers.extend([nn.Linear(in_features, size), nn.ReLU(), nn.Dropout(dropout)])
                in_features = size
            layers.append(nn.Linear(in_features, num_classes))
            self.head = nn.Sequential(*layers)
        else:
            # Preserve the original head.weight/head.bias checkpoint contract.
            self.head = nn.Linear(128, num_classes)

    def model_config(self) -> dict[str, Any]:
        return {
            "num_classes": self.num_classes,
            "dense_hidden_sizes": list(self.dense_hidden_sizes),
            "dropout": self.dropout,
        }

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.stem(x)
        x = self.stage1(x)
        x = self.to64(x)
        x = self.stage2(x)
        x = self.to128(x)
        x = self.stage3(x)
        return self.head(torch.flatten(x, 1))


def _set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def _class_weights(dataset: RollDigitDataset, device: torch.device) -> torch.Tensor:
    counts = np.zeros(len(LABELS), dtype=np.float32)
    for row in dataset.rows:
        counts[LABEL_TO_ID[str(row["label"])]] += 1
    counts[counts == 0] = 1.0
    weights = counts.sum() / (len(LABELS) * counts)
    weights = np.minimum(weights, 8.0)
    return torch.tensor(weights, dtype=torch.float32, device=device)


def _load_checkpoint_state(path: Path) -> dict[str, torch.Tensor]:
    return _checkpoint_state(torch.load(path, map_location="cpu", weights_only=True))


def _checkpoint_state(payload: object) -> dict[str, torch.Tensor]:
    if isinstance(payload, dict):
        for key in ("model_state", "model_state_dict", "state_dict"):
            state = payload.get(key)
            if isinstance(state, dict):
                return state
    if isinstance(payload, dict) and all(isinstance(key, str) and isinstance(value, torch.Tensor) for key, value in payload.items()):
        return payload
    raise ValueError("could not find model weights in checkpoint")


def model_from_checkpoint(payload: object) -> SmallRollDigitResNet:
    """Load legacy or versioned classifier heads without guessing their shape."""
    spec: dict[str, Any] = {}
    if isinstance(payload, dict):
        if payload.get("architecture", "SmallRollDigitResNet") != "SmallRollDigitResNet":
            raise ValueError("unsupported roll-digit architecture")
        if "labels" in payload and list(payload["labels"]) != LABELS:
            raise ValueError("checkpoint label ordering does not match runtime labels")
        if "model_config" in payload:
            spec = payload["model_config"]
            if not isinstance(spec, dict) or "dense_hidden_sizes" not in spec or "dropout" not in spec:
                raise ValueError("invalid checkpoint model_config")
            if spec.get("num_classes", len(LABELS)) != len(LABELS):
                raise ValueError("checkpoint class count does not match runtime labels")
    model = SmallRollDigitResNet(
        dense_hidden_sizes=tuple(spec.get("dense_hidden_sizes", ())),
        dropout=spec.get("dropout", 0.0),
    )
    model.load_state_dict(_checkpoint_state(payload), strict=True)
    return model


def _initialize_from_checkpoint(model: SmallRollDigitResNet, path: Path, *, reset_head: bool) -> dict[str, Any]:
    source = model_from_checkpoint(torch.load(path, map_location="cpu", weights_only=True))
    if not reset_head:
        if source.dense_hidden_sizes != model.dense_hidden_sizes:
            raise ValueError("Classifier shape changed; use --reset-head to transfer only the convolutional backbone")
        model.load_state_dict(source.state_dict(), strict=True)
    else:
        state = {key: value for key, value in source.state_dict().items() if not key.startswith("head.")}
        missing, unexpected = model.load_state_dict(state, strict=False)
        expected = {key for key in model.state_dict() if key.startswith("head.")}
        if set(missing) != expected or unexpected:
            raise ValueError("checkpoint backbone is incompatible with the requested model")
    return {"head_reinitialized": reset_head, "initial_model_config": source.model_config()}


def _build_optimizer(model: nn.Module, config: TrainConfig) -> torch.optim.Optimizer:
    # Adam uses an L2 gradient penalty; AdamW applies decoupled weight decay.
    optimizer_class = torch.optim.Adam if config.optimizer == "adam" else torch.optim.AdamW
    return optimizer_class(model.parameters(), lr=config.learning_rate, weight_decay=config.weight_decay)


def _run_epoch(
    model: nn.Module,
    loader: DataLoader,
    *,
    device: torch.device,
    criterion: nn.Module,
    optimizer: torch.optim.Optimizer | None = None,
) -> dict[str, Any]:
    training = optimizer is not None
    model.train(training)
    total_loss = 0.0
    total = 0
    correct = 0
    per_class_total = np.zeros(len(LABELS), dtype=np.int64)
    per_class_correct = np.zeros(len(LABELS), dtype=np.int64)
    with torch.set_grad_enabled(training):
        for images, labels in loader:
            images = images.to(device)
            labels = labels.to(device)
            logits = model(images)
            loss = criterion(logits, labels)
            if optimizer is not None:
                optimizer.zero_grad(set_to_none=True)
                loss.backward()
                optimizer.step()
            preds = logits.argmax(dim=1)
            total_loss += float(loss.item()) * int(labels.numel())
            total += int(labels.numel())
            correct += int((preds == labels).sum().item())
            for label, pred in zip(labels.cpu().numpy(), preds.cpu().numpy(), strict=False):
                per_class_total[int(label)] += 1
                if int(label) == int(pred):
                    per_class_correct[int(label)] += 1
    return {
        "loss": total_loss / max(1, total),
        "accuracy": correct / max(1, total),
        "per_class_accuracy": {
            label: (
                float(per_class_correct[index] / per_class_total[index])
                if per_class_total[index]
                else None
            )
            for index, label in enumerate(LABELS)
        },
    }


def train(
    dataset_dir: str | Path,
    *,
    model_out: str | Path | None = None,
    config: TrainConfig = TrainConfig(),
    device_name: str | None = None,
    initial_checkpoint: str | Path | None = None,
    reset_head: bool = False,
) -> Path:
    _set_seed(config.seed)
    dataset_root = Path(dataset_dir)
    train_csv = dataset_root / "train.csv"
    val_csv = dataset_root / "val.csv"
    if not train_csv.exists() or not val_csv.exists():
        raise FileNotFoundError("dataset must contain train.csv and val.csv")
    _validate_writer_splits(train_csv, val_csv)

    device = torch.device(device_name or ("cuda" if torch.cuda.is_available() else "cpu"))
    train_ds = RollDigitDataset(train_csv, dataset_root, image_size=config.image_size, augment=True)
    val_ds = RollDigitDataset(val_csv, dataset_root, image_size=config.image_size, augment=False)
    train_loader = DataLoader(train_ds, batch_size=config.batch_size, shuffle=True, num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=config.batch_size, shuffle=False, num_workers=0)

    model = SmallRollDigitResNet(dense_hidden_sizes=config.dense_hidden_sizes, dropout=config.dropout).to(device)
    initialization: dict[str, Any] = {"head_reinitialized": False}
    if initial_checkpoint is not None:
        initialization = _initialize_from_checkpoint(model, Path(initial_checkpoint), reset_head=reset_head)
        print(f"loaded_pretrained={initial_checkpoint}")
        if reset_head:
            print("classifier_head_reinitialized=True; convolutional backbone retained")
    elif reset_head:
        raise ValueError("--reset-head requires an initial checkpoint")
    criterion = nn.CrossEntropyLoss(weight=_class_weights(train_ds, device))
    optimizer = _build_optimizer(model, config)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(1, config.epochs))

    output_path = Path(model_out) if model_out is not None else dataset_root / "roll_digit_resnet.pt"
    history: list[dict[str, Any]] = []
    best_val = -1.0
    best_payload: dict[str, Any] | None = None
    for epoch in range(1, config.epochs + 1):
        train_metrics = _run_epoch(model, train_loader, device=device, criterion=criterion, optimizer=optimizer)
        val_metrics = _run_epoch(model, val_loader, device=device, criterion=criterion)
        scheduler.step()
        record = {"epoch": epoch, "train": train_metrics, "val": val_metrics}
        history.append(record)
        print(
            f"epoch={epoch:03d} "
            f"train_acc={train_metrics['accuracy']:.3f} val_acc={val_metrics['accuracy']:.3f} "
            f"train_loss={train_metrics['loss']:.4f} val_loss={val_metrics['loss']:.4f}"
        )
        if val_metrics["accuracy"] > best_val:
            best_val = float(val_metrics["accuracy"])
            best_payload = {
                "model_state": model.state_dict(),
                "labels": LABELS,
                "config": asdict(config),
                "best_epoch": epoch,
                "best_val_accuracy": best_val,
                "val_metrics": val_metrics,
                "architecture": "SmallRollDigitResNet",
                "model_config": model.model_config(),
                "initialization": initialization,
                "augmentation": "upright_v2: +/-5 degrees, contrast 0.85-1.25, brightness 0.90-1.10, ink 0.65-0.95; no flips",
            }
            output_path.parent.mkdir(parents=True, exist_ok=True)
            torch.save(best_payload, output_path)

    metrics_path = output_path.with_suffix(".metrics.json")
    metrics_path.write_text(
        json.dumps(
            {
                "model_path": str(output_path),
                "dataset_dir": str(dataset_root),
                "initial_checkpoint": str(initial_checkpoint) if initial_checkpoint else None,
                "device": str(device),
                "best_val_accuracy": best_val,
                "history": history,
                "labels": LABELS,
                "model_config": model.model_config(),
                "config": asdict(config),
                "initialization": initialization,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if best_payload is None:
        raise RuntimeError("training did not produce a model")
    print(f"saved_model={output_path}")
    print(f"metrics={metrics_path}")
    return output_path


def _validate_writer_splits(train_csv: Path, val_csv: Path) -> None:
    writers = []
    for path in (train_csv, val_csv):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        writers.append({row.get("writer_id") or row.get("roll_no") for row in rows
                        if row.get("writer_id") or row.get("roll_no")})
    overlap = writers[0] & writers[1]
    if overlap:
        raise ValueError(f"Students occur in both train and validation: {sorted(overlap)[:10]}")


def pretrain_kaggle(
    kaggle_train_csv: str | Path,
    *,
    model_out: str | Path,
    config: TrainConfig = TrainConfig(),
    device_name: str | None = None,
    val_fraction: float = 0.10,
    limit: int | None = None,
) -> Path:
    _set_seed(config.seed)
    csv_path = Path(kaggle_train_csv)
    if not csv_path.exists():
        raise FileNotFoundError(f"Kaggle train.csv not found: {csv_path}")

    device = torch.device(device_name or ("cuda" if torch.cuda.is_available() else "cpu"))
    train_ds = KaggleDigitCsvDataset(
        csv_path,
        image_size=config.image_size,
        augment=True,
        val_fraction=val_fraction,
        split="train",
        seed=config.seed,
        limit=limit,
    )
    val_ds = KaggleDigitCsvDataset(
        csv_path,
        image_size=config.image_size,
        augment=False,
        val_fraction=val_fraction,
        split="val",
        seed=config.seed,
        limit=limit,
    )
    train_loader = DataLoader(train_ds, batch_size=config.batch_size, shuffle=True, num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=config.batch_size, shuffle=False, num_workers=0)

    model = SmallRollDigitResNet(dense_hidden_sizes=config.dense_hidden_sizes, dropout=config.dropout).to(device)
    criterion = nn.CrossEntropyLoss(weight=_class_weights(train_ds, device))
    optimizer = _build_optimizer(model, config)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=max(1, config.epochs))

    output_path = Path(model_out)
    history: list[dict[str, Any]] = []
    best_val = -1.0
    best_payload: dict[str, Any] | None = None
    for epoch in range(1, config.epochs + 1):
        train_metrics = _run_epoch(model, train_loader, device=device, criterion=criterion, optimizer=optimizer)
        val_metrics = _run_epoch(model, val_loader, device=device, criterion=criterion)
        scheduler.step()
        record = {"epoch": epoch, "train": train_metrics, "val": val_metrics}
        history.append(record)
        print(
            f"epoch={epoch:03d} "
            f"train_acc={train_metrics['accuracy']:.3f} val_acc={val_metrics['accuracy']:.3f} "
            f"train_loss={train_metrics['loss']:.4f} val_loss={val_metrics['loss']:.4f}"
        )
        if val_metrics["accuracy"] > best_val:
            best_val = float(val_metrics["accuracy"])
            best_payload = {
                "model_state": model.state_dict(),
                "labels": LABELS,
                "config": asdict(config),
                "best_epoch": epoch,
                "best_val_accuracy": best_val,
                "val_metrics": val_metrics,
                "architecture": "SmallRollDigitResNet",
                "model_config": model.model_config(),
                "pretrain_source": "kaggle_digit_recognizer",
            }
            output_path.parent.mkdir(parents=True, exist_ok=True)
            torch.save(best_payload, output_path)

    metrics_path = output_path.with_suffix(".metrics.json")
    metrics_path.write_text(
        json.dumps(
            {
                "model_path": str(output_path),
                "kaggle_train_csv": str(csv_path),
                "device": str(device),
                "best_val_accuracy": best_val,
                "history": history,
                "labels": LABELS,
                "model_config": model.model_config(),
                "config": asdict(config),
                "val_fraction": val_fraction,
                "limit": limit,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if best_payload is None:
        raise RuntimeError("Kaggle pretraining did not produce a model")
    print(f"saved_model={output_path}")
    print(f"metrics={metrics_path}")
    return output_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train a ResNet-style roll digit classifier")
    parser.add_argument("--dataset-dir", type=Path, help="OMR crop dataset dir containing train.csv and val.csv")
    parser.add_argument("--kaggle-train-csv", type=Path, help="Kaggle Digit Recognizer train.csv for pretraining")
    parser.add_argument("--model-out", type=Path, default=None)
    parser.add_argument("--pretrained", type=Path, default=None, help="Checkpoint to load before OMR fine-tuning")
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--weight-decay", type=float, default=1e-4, help="L2 coefficient for Adam; decoupled decay for AdamW")
    parser.add_argument("--optimizer", choices=("adam", "adamw"), default="adamw")
    parser.add_argument("--dense-hidden-sizes", type=int, nargs="+", default=(), help="For example: 64 32 adds two hidden dense layers")
    parser.add_argument("--dropout", type=float, default=0.25, help="Dropout after each hidden dense layer")
    parser.add_argument("--reset-head", action="store_true", help="Keep checkpoint backbone but initialize a new classifier head")
    parser.add_argument("--image-size", type=int, default=64)
    parser.add_argument("--seed", type=int, default=557)
    parser.add_argument("--val-fraction", type=float, default=0.10, help="Validation fraction for Kaggle CSV pretraining")
    parser.add_argument("--limit", type=int, default=None, help="Optional row limit for quick Kaggle smoke tests")
    parser.add_argument("--device", default=None, help="cpu, cuda, etc. Defaults to cuda if available else cpu.")
    args = parser.parse_args(argv)
    if args.kaggle_train_csv is None and args.dataset_dir is None:
        parser.error("provide either --dataset-dir for OMR training or --kaggle-train-csv for Kaggle pretraining")
    if args.kaggle_train_csv is not None and args.dataset_dir is not None:
        parser.error("use either --kaggle-train-csv or --dataset-dir in one command, not both")
    if args.kaggle_train_csv is not None and args.pretrained is not None:
        parser.error("--pretrained is for OMR fine-tuning with --dataset-dir, not Kaggle pretraining")
    if args.reset_head and args.pretrained is None:
        parser.error("--reset-head requires --pretrained")
    config = TrainConfig(
        image_size=args.image_size,
        batch_size=args.batch_size,
        epochs=args.epochs,
        learning_rate=args.lr,
        weight_decay=args.weight_decay,
        seed=args.seed,
        dense_hidden_sizes=tuple(args.dense_hidden_sizes),
        dropout=args.dropout,
        optimizer=args.optimizer,
    )
    if args.kaggle_train_csv is not None:
        if args.model_out is None:
            parser.error("--model-out is required for Kaggle pretraining")
        pretrain_kaggle(
            args.kaggle_train_csv,
            model_out=args.model_out,
            config=config,
            device_name=args.device,
            val_fraction=args.val_fraction,
            limit=args.limit,
        )
    else:
        train(
            args.dataset_dir,
            model_out=args.model_out,
            config=config,
            device_name=args.device,
            initial_checkpoint=args.pretrained,
            reset_head=args.reset_head,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
