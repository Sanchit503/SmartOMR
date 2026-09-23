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
    image = image.rotate(angle, fillcolor=255)
    if random.random() < 0.45:
        image = ImageEnhance.Contrast(image).enhance(random.uniform(0.85, 1.25))
    if random.random() < 0.35:
        image = ImageEnhance.Brightness(image).enhance(random.uniform(0.90, 1.10))
    return image


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
    def __init__(self, num_classes: int = len(LABELS)) -> None:
        super().__init__()
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
        self.head = nn.Linear(128, num_classes)

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
    payload = torch.load(path, map_location="cpu")
    if isinstance(payload, dict):
        for key in ("model_state", "model_state_dict", "state_dict"):
            state = payload.get(key)
            if isinstance(state, dict):
                return state
    if isinstance(payload, dict) and all(isinstance(key, str) for key in payload):
        return payload
    raise ValueError(f"could not find model weights in checkpoint: {path}")


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
) -> Path:
    _set_seed(config.seed)
    dataset_root = Path(dataset_dir)
    train_csv = dataset_root / "train.csv"
    val_csv = dataset_root / "val.csv"
    if not train_csv.exists() or not val_csv.exists():
        raise FileNotFoundError("dataset must contain train.csv and val.csv")

    device = torch.device(device_name or ("cuda" if torch.cuda.is_available() else "cpu"))
    train_ds = RollDigitDataset(train_csv, dataset_root, image_size=config.image_size, augment=True)
    val_ds = RollDigitDataset(val_csv, dataset_root, image_size=config.image_size, augment=False)
    train_loader = DataLoader(train_ds, batch_size=config.batch_size, shuffle=True, num_workers=0)
    val_loader = DataLoader(val_ds, batch_size=config.batch_size, shuffle=False, num_workers=0)

    model = SmallRollDigitResNet().to(device)
    if initial_checkpoint is not None:
        model.load_state_dict(_load_checkpoint_state(Path(initial_checkpoint)))
        print(f"loaded_pretrained={initial_checkpoint}")
    criterion = nn.CrossEntropyLoss(weight=_class_weights(train_ds, device))
    optimizer = torch.optim.AdamW(model.parameters(), lr=config.learning_rate, weight_decay=config.weight_decay)
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

    model = SmallRollDigitResNet().to(device)
    criterion = nn.CrossEntropyLoss(weight=_class_weights(train_ds, device))
    optimizer = torch.optim.AdamW(model.parameters(), lr=config.learning_rate, weight_decay=config.weight_decay)
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
    parser.add_argument("--weight-decay", type=float, default=1e-4)
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
    config = TrainConfig(
        image_size=args.image_size,
        batch_size=args.batch_size,
        epochs=args.epochs,
        learning_rate=args.lr,
        weight_decay=args.weight_decay,
        seed=args.seed,
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
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
