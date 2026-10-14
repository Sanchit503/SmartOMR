from __future__ import annotations

import csv
import hashlib
import json

import numpy as np
import torch
from PIL import Image, ImageDraw
from torch import nn

from omr.datasets.train_roll_digit_resnet import _simulate_faint_pencil
from omr.datasets.train_roll_digit_resnet import (
    LABELS,
    SmallRollDigitResNet,
    TrainConfig,
    _augment_image,
    _build_optimizer,
    _initialize_from_checkpoint,
    _validate_writer_splits,
    model_from_checkpoint,
    train,
)
import pytest


def test_faint_pencil_augmentation_lightens_ink_without_erasing_the_stroke(monkeypatch):
    image = Image.new("L", (32, 32), 255)
    ImageDraw.Draw(image).line([(8, 24), (16, 6), (24, 24)], fill=0, width=3)
    values = iter([0.50, 0.0, 0.0])
    monkeypatch.setattr("omr.datasets.train_roll_digit_resnet.random.uniform", lambda *_args: next(values))

    augmented = np.asarray(_simulate_faint_pencil(image))

    assert 0 < int(augmented[10, 14]) < 255
    assert int(augmented[10, 14]) < int(augmented[1, 1])


def test_faint_augmentation_preserves_already_faint_strokes(monkeypatch):
    image = Image.new("L", (32, 32), 255)
    ImageDraw.Draw(image).rectangle((10, 10, 20, 20), fill=250)
    values = iter([.65, 7, 7])
    monkeypatch.setattr("omr.datasets.train_roll_digit_resnet.random.uniform", lambda *_: next(values))
    augmented = np.asarray(_simulate_faint_pencil(image))
    assert augmented[15, 15] < augmented[15, 1]


def test_augmentation_stays_upright_and_does_not_flip(monkeypatch):
    image = Image.new("L", (32, 32), 255)
    ImageDraw.Draw(image).rectangle((3, 2, 9, 7), fill=0)
    monkeypatch.setattr("omr.datasets.train_roll_digit_resnet.random.uniform", lambda low, high: high)
    monkeypatch.setattr("omr.datasets.train_roll_digit_resnet.random.random", lambda: 1)
    augmented = np.asarray(_augment_image(image))
    y, x = np.where(augmented < 100)
    assert x.mean() < augmented.shape[1] / 2
    assert y.mean() < augmented.shape[0] / 2
    assert augmented.shape[0] > 32


def test_training_rejects_writer_leakage(tmp_path):
    for name in ("train.csv", "val.csv"):
        (tmp_path / name).write_text("image_path,label,writer_id\na.png,2,2023001\n")
    with pytest.raises(ValueError, match="both train and validation"):
        _validate_writer_splits(tmp_path / "train.csv", tmp_path / "val.csv")


def _checkpoint(model, path, *, legacy=False):
    payload = {"model_state": model.state_dict(), "labels": LABELS, "architecture": "SmallRollDigitResNet"}
    if not legacy:
        payload["model_config"] = model.model_config()
    torch.save(payload, path)
    return payload


def test_original_head_preserves_checkpoint_keys():
    model = SmallRollDigitResNet()
    assert isinstance(model.head, nn.Linear)
    assert model.head.in_features == 128
    assert model.head.out_features == 11
    assert {key for key in model.state_dict() if key.startswith("head.")} == {"head.weight", "head.bias"}


def test_deeper_head_has_three_dense_layers_and_two_dropouts():
    model = SmallRollDigitResNet(dense_hidden_sizes=(64, 32), dropout=.25)
    dense = [layer for layer in model.head if isinstance(layer, nn.Linear)]
    assert [(layer.in_features, layer.out_features) for layer in dense] == [(128, 64), (64, 32), (32, 11)]
    dropouts = [layer for layer in model.head if isinstance(layer, nn.Dropout)]
    assert len(dropouts) == 2
    assert all(layer.p == .25 for layer in dropouts)
    features = torch.ones(8, 128)
    model.train()
    assert not torch.equal(model.head(features), model.head(features))
    model.eval()
    assert torch.equal(model.head(features), model.head(features))
    assert model(torch.zeros(2, 1, 64, 64)).shape == (2, 11)


@pytest.mark.parametrize("hidden_sizes", [(), (64, 32)])
def test_versioned_checkpoint_restores_exact_predictions(tmp_path, hidden_sizes):
    model = SmallRollDigitResNet(dense_hidden_sizes=hidden_sizes).eval()
    path = tmp_path / "model.pt"
    _checkpoint(model, path, legacy=not hidden_sizes)
    loaded = model_from_checkpoint(torch.load(path, weights_only=True)).eval()
    image = torch.randn(2, 1, 64, 64)
    with torch.no_grad():
        assert torch.equal(model(image), loaded(image))
    assert loaded.dense_hidden_sizes == hidden_sizes


def test_raw_legacy_state_dict_remains_supported():
    model = SmallRollDigitResNet()
    loaded = model_from_checkpoint(model.state_dict())
    assert torch.equal(model.head.weight, loaded.head.weight)


def test_deeper_head_transfer_requires_explicit_reset(tmp_path):
    source = SmallRollDigitResNet()
    path = tmp_path / "old.pt"
    _checkpoint(source, path, legacy=True)
    target = SmallRollDigitResNet(dense_hidden_sizes=(64, 32))
    with pytest.raises(ValueError, match="--reset-head"):
        _initialize_from_checkpoint(target, path, reset_head=False)
    initial_head = {key: value.clone() for key, value in target.head.state_dict().items()}
    info = _initialize_from_checkpoint(target, path, reset_head=True)
    assert info["head_reinitialized"]
    for key, value in source.state_dict().items():
        if not key.startswith("head."):
            assert torch.equal(value, target.state_dict()[key])
    assert all(torch.equal(value, target.head.state_dict()[key]) for key, value in initial_head.items())


def test_checkpoint_loader_fails_closed_on_missing_metadata_or_wrong_labels(tmp_path):
    model = SmallRollDigitResNet(dense_hidden_sizes=(64, 32))
    with pytest.raises(RuntimeError):
        model_from_checkpoint({"model_state": model.state_dict(), "labels": LABELS})
    payload = _checkpoint(model, tmp_path / "new.pt")
    payload["labels"] = list(reversed(LABELS))
    with pytest.raises(ValueError, match="label ordering"):
        model_from_checkpoint(payload)


@pytest.mark.parametrize("kwargs", [
    {"dense_hidden_sizes": (0, 32)},
    {"dense_hidden_sizes": (64, -1)},
    {"dropout": 1.0},
    {"dropout": float("nan")},
    {"optimizer": "unknown"},
    {"weight_decay": -1},
])
def test_invalid_regularization_or_head_config_is_rejected(kwargs):
    with pytest.raises(ValueError):
        TrainConfig(**kwargs)


def test_adam_l2_penalizes_weights_even_with_zero_data_gradient():
    model = nn.Linear(1, 1, bias=False)
    with torch.no_grad():
        model.weight.fill_(2)
    optimizer = _build_optimizer(model, TrainConfig(optimizer="adam", learning_rate=.01, weight_decay=.1))
    assert isinstance(optimizer, torch.optim.Adam)
    assert optimizer.param_groups[0]["weight_decay"] == .1
    model.weight.grad = torch.zeros_like(model.weight)
    optimizer.step()
    assert float(model.weight.item()) < 2
    assert isinstance(_build_optimizer(model, TrainConfig()), torch.optim.AdamW)


def test_new_head_training_checkpoint_and_mixed_ensemble_inference(tmp_path):
    from omr.reader.handwriting import LocalResnetProbabilityEnsembleRollOcr, LocalResnetRollOcr

    dataset = tmp_path / "dataset"
    dataset.mkdir()
    image = Image.new("L", (32, 32), 255)
    ImageDraw.Draw(image).text((10, 10), "2", fill=0)
    image.save(dataset / "digit.png")
    for split in ("train", "val"):
        with (dataset / f"{split}.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["image_path", "label", "writer_id"])
            writer.writeheader()
            writer.writerows([{"image_path": "digit.png", "label": "2", "writer_id": split}] * 2)
    old_path = tmp_path / "old.pt"
    _checkpoint(SmallRollDigitResNet(), old_path, legacy=True)
    new_path = train(
        dataset, model_out=tmp_path / "new.pt", initial_checkpoint=old_path, reset_head=True,
        config=TrainConfig(epochs=1, batch_size=2, dense_hidden_sizes=(64, 32), optimizer="adam"),
        device_name="cpu",
    )
    payload = torch.load(new_path, weights_only=True)
    assert payload["model_config"]["dense_hidden_sizes"] == [64, 32]
    assert payload["config"]["optimizer"] == "adam"
    assert payload["initialization"]["head_reinitialized"]
    metrics = json.loads(new_path.with_suffix(".metrics.json").read_text())
    assert len(metrics["history"]) == 1
    single = LocalResnetRollOcr(new_path)
    assert single.model.dense_hidden_sizes == (64, 32)
    assert not single.model.training
    assert single.read_digit(dataset / "digit.png").confidence is not None
    members = [{"model_path": path.name, "best_val_accuracy": .9,
                "checkpoint_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
               for path in (old_path, new_path)]
    manifest = tmp_path / "ensemble_manifest.json"
    manifest.write_text(json.dumps({"kind": "roll_digit_resnet_probability_ensemble", "members": members}))
    ensemble = LocalResnetProbabilityEnsembleRollOcr(manifest)
    assert [model.dense_hidden_sizes for model in ensemble.models] == [(), (64, 32)]
    assert all(not model.training for model in ensemble.models)
    assert ensemble.read_digit(dataset / "digit.png").raw["member_count"] == 2
