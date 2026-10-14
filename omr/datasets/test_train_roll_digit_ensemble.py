import hashlib
import json
from pathlib import Path

import pytest
import torch

from omr.datasets import train_roll_digit_ensemble as ensemble


def _inputs(tmp_path):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    for name in ("train.csv", "val.csv"):
        (dataset / name).write_text("image_path,label,writer_id\n")
    source = tmp_path / "source"
    source.mkdir()
    members = []
    for seed in (557, 558):
        path = source / f"{seed}.pt"
        torch.save({"labels": ensemble.LABELS, "model_state": {}, "seed": seed}, path)
        members.append({"seed": seed, "model_path": path.name,
                        "checkpoint_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = source / "ensemble_manifest.json"
    manifest.write_text(json.dumps({"kind": "roll_digit_resnet_probability_ensemble", "members": members}))
    return dataset, manifest, tmp_path / "new"


def test_resume_uses_corresponding_members_and_skips_kaggle(tmp_path, monkeypatch):
    dataset, source, output = _inputs(tmp_path)
    calls = []

    def train(_dataset, *, model_out, config, initial_checkpoint, **_kwargs):
        calls.append((config.seed, initial_checkpoint.name))
        torch.save({"labels": ensemble.LABELS}, model_out)
        model_out.with_suffix(".metrics.json").write_text(json.dumps({"best_val_accuracy": .9}))

    monkeypatch.setattr(ensemble, "train", train)
    monkeypatch.setattr(ensemble, "pretrain_kaggle", lambda *_a, **_k: pytest.fail("Kaggle must not run"))
    result = ensemble.train_ensemble(dataset, None, output, seeds=(558, 557), initial_ensemble=source)
    assert calls == [(558, "558.pt"), (557, "557.pt")]
    saved = json.loads(result.read_text())
    assert saved["member_count"] == 2
    assert saved["kaggle_epochs"] == 0
    assert all((output / member["model_path"]).is_file() for member in saved["members"])
    assert "no flips" in saved["augmentation"]
    with pytest.raises(FileExistsError):
        ensemble.train_ensemble(dataset, None, output, seeds=(557, 558), initial_ensemble=source)


def test_resume_rejects_changed_checkpoint(tmp_path):
    dataset, source, output = _inputs(tmp_path)
    (source.parent / "557.pt").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="hash mismatch"):
        ensemble.train_ensemble(dataset, None, output, seeds=(557, 558), initial_ensemble=source)
    assert not output.exists()


def test_resume_rejects_wrong_label_order(tmp_path):
    dataset, source, output = _inputs(tmp_path)
    path = source.parent / "557.pt"
    torch.save({"labels": list(reversed(ensemble.LABELS)), "model_state": {}}, path)
    manifest = json.loads(source.read_text())
    manifest["members"][0]["checkpoint_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    source.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="label ordering"):
        ensemble.train_ensemble(dataset, None, output, seeds=(557, 558), initial_ensemble=source)


def test_resume_cannot_overwrite_source_ensemble(tmp_path):
    dataset, source, _ = _inputs(tmp_path)
    with pytest.raises(ValueError, match="separate"):
        ensemble.train_ensemble(dataset, None, source.parent, seeds=(557, 558), initial_ensemble=source)


def test_dense_head_and_regularization_options_reach_every_member(tmp_path, monkeypatch):
    dataset, source, output = _inputs(tmp_path)
    configs = []

    def train(_dataset, *, model_out, config, reset_head, **_kwargs):
        configs.append((config, reset_head))
        torch.save({"labels": ensemble.LABELS}, model_out)
        model_out.with_suffix(".metrics.json").write_text(json.dumps({"best_val_accuracy": .9}))

    monkeypatch.setattr(ensemble, "train", train)
    manifest = ensemble.train_ensemble(
        dataset, None, output, seeds=(557, 558), initial_ensemble=source,
        dense_hidden_sizes=(64, 32), dropout=.25, optimizer="adam", weight_decay=.0001, reset_head=True,
    )
    assert len(configs) == 2
    assert all(config.dense_hidden_sizes == (64, 32) and config.dropout == .25
               and config.optimizer == "adam" and config.weight_decay == .0001 and reset
               for config, reset in configs)
    saved = json.loads(manifest.read_text())
    assert saved["training_config"]["regularization"] == "l2_penalty"
    assert saved["training_config"]["head_reinitialized"]


def _profiles(tmp_path, members):
    path = tmp_path / "members.json"
    path.write_text(json.dumps({"schema_version": 1, "members": members}))
    return path


def test_diverse_members_are_seed_mapped_and_recorded(tmp_path, monkeypatch):
    dataset, source, output = _inputs(tmp_path)
    profiles = _profiles(tmp_path, [
        {"seed": 557, "dense_hidden_sizes": [], "optimizer": "adamw", "learning_rate": .0001},
        {"seed": 558, "dense_hidden_sizes": [64], "dropout": .15, "optimizer": "adam",
         "learning_rate": .0003, "weight_decay": .0002, "epochs": 24, "reset_head": True},
    ])
    configs = []
    def train(_dataset, *, model_out, config, reset_head, initial_checkpoint, **_kwargs):
        configs.append((config, reset_head, initial_checkpoint.name))
        torch.save({"labels": ensemble.LABELS}, model_out)
        model_out.with_suffix(".metrics.json").write_text(json.dumps({"best_val_accuracy": .9}))
    monkeypatch.setattr(ensemble, "train", train)
    monkeypatch.setattr(ensemble, "pretrain_kaggle", lambda *_a, **_k: pytest.fail("No Kaggle repeat"))
    result = ensemble.train_ensemble(dataset, None, output, seeds=(558, 557),
                                     initial_ensemble=source, member_config=profiles, epochs=12)
    first, reset, checkpoint = configs[0]
    assert (first.seed, first.dense_hidden_sizes, first.learning_rate, first.epochs) == (558, (64,), .0003, 24)
    assert reset and checkpoint == "558.pt" and first.optimizer == "adam" and first.dropout == .15
    second, reset, checkpoint = configs[1]
    assert second.seed == 557 and second.dense_hidden_sizes == () and second.epochs == 12
    assert not reset and checkpoint == "557.pt"
    saved = json.loads(result.read_text())
    assert saved["member_config_sha256"] == hashlib.sha256(profiles.read_bytes()).hexdigest()
    assert saved["members"][0]["training_config"]["regularization"] == "l2_penalty"
    assert saved["members"][1]["training_config"]["regularization"] == "decoupled_weight_decay"
    plan = json.loads((output / "training_plan.json").read_text())
    assert plan["members"][0]["dense_hidden_sizes"] == [64]
    assert plan["dataset_csv_sha256"] == saved["dataset_csv_sha256"]


@pytest.mark.parametrize("rows,match", [
    ([{"seed": 557}], "each requested seed"),
    ([{"seed": 557}, {"seed": 557}], "each requested seed"),
    ([{"seed": 557}, {"seed": 999}], "each requested seed"),
    ([{"seed": 557}, {"seed": 558, "flip": True}], "Unknown"),
    ([{"seed": 557}, {"seed": 558, "reset_head": "true"}], "boolean"),
    ([{"seed": 557}, {"seed": 558, "learning_rate": "0.001"}], "numbers"),
    ([{"seed": 557}, {"seed": 558, "learning_rate": 0}], "learning rate"),
    ([{"seed": 557}, {"seed": 558, "learning_rate": float("nan")}], "learning rate"),
    ([{"seed": 557}, {"seed": 558, "dense_hidden_sizes": "64"}], "list"),
    ([{"seed": 557}, {"seed": 558, "dense_hidden_sizes": [0]}], "positive integers"),
    ([{"seed": 557}, {"seed": 558, "optimizer": "unknown"}], "optimizer"),
    ([{"seed": 557}, {"seed": 558, "epochs": 0}], "positive integers"),
])
def test_invalid_member_settings_fail_before_training(tmp_path, rows, match):
    dataset, source, output = _inputs(tmp_path)
    with pytest.raises(ValueError, match=match):
        ensemble.train_ensemble(dataset, None, output, seeds=(557, 558),
                                initial_ensemble=source, member_config=_profiles(tmp_path, rows))
    assert not output.exists()


def test_changed_head_requires_explicit_reset_before_any_member_trains(tmp_path, monkeypatch):
    dataset, source, output = _inputs(tmp_path)
    profiles = _profiles(tmp_path, [{"seed": 557}, {"seed": 558, "dense_hidden_sizes": [64]}])
    monkeypatch.setattr(ensemble, "train", lambda *_a, **_k: pytest.fail("Must validate all heads first"))
    with pytest.raises(ValueError, match="reset_head"):
        ensemble.train_ensemble(dataset, None, output, seeds=(557, 558), initial_ensemble=source, member_config=profiles)
    assert not output.exists()


def test_versioned_seven_member_plan_has_mixed_heads_and_regularization():
    path = Path(ensemble.__file__).parent / "configs" / "roll_digit_ensemble_diverse_v1.json"
    configs = ensemble._member_configs(ensemble.DEFAULT_SEEDS, ensemble.TrainConfig(), False, path)
    assert [len(configs[seed][0].dense_hidden_sizes) + 1 for seed in ensemble.DEFAULT_SEEDS] == [1, 2, 3, 3, 2, 3, 1]
    assert len({config.learning_rate for config, _reset in configs.values()}) >= 4
    assert {config.optimizer for config, _reset in configs.values()} == {"adam", "adamw"}
    assert all(reset == bool(config.dense_hidden_sizes) for config, reset in configs.values())
