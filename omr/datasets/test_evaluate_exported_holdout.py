import csv
import hashlib
import json

import pytest
import torch
from PIL import Image

from omr.datasets.evaluate_exported_holdout import evaluate
from omr.datasets.train_roll_digit_resnet import LABELS, SmallRollDigitResNet


def setup(tmp_path):
    torch.set_num_threads(2)
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    for index, split in enumerate(("train", "val", "test")):
        image = dataset / f"{split}.png"
        Image.new("L", (32, 32), index * 50).save(image)
        with (dataset / f"{split}.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["image_path", "label", "writer_id", "crop_sha256"])
            writer.writeheader()
            writer.writerow({"image_path": image.name, "label": "2", "writer_id": split,
                             "crop_sha256": hashlib.sha256(image.read_bytes()).hexdigest()})
    model = SmallRollDigitResNet()
    with torch.no_grad():
        model.head.weight.zero_()
        model.head.bias.zero_()
        model.head.bias[LABELS.index("2")] = 10
    checkpoint = tmp_path / "model.pt"
    torch.save({"labels": LABELS, "model_state": model.state_dict()}, checkpoint)
    manifest = tmp_path / "ensemble_manifest.json"
    manifest.write_text(json.dumps({
        "members": [{"model_path": checkpoint.name, "best_val_accuracy": 1.0,
                     "seed": 557,
                     "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest()}],
        "dataset_csv_sha256": {f"{split}.csv": hashlib.sha256((dataset / f"{split}.csv").read_bytes()).hexdigest()
                              for split in ("train", "val")},
    }))
    return dataset, manifest


def test_final_test_report_has_accuracy_precision_recall_and_predictions(tmp_path):
    dataset, manifest = setup(tmp_path)
    report = evaluate(dataset, manifest, tmp_path / "report")
    assert report["test_samples"] == 1 and report["accuracy"] == 1.0
    assert report["per_class"]["2"]["precision"] == report["per_class"]["2"]["recall"] == 1.0
    assert not report["test_used_for_current_run_training_or_checkpoint_selection"]
    assert (tmp_path / "report/test_predictions.csv").is_file()
    assert report["member_reports"][0]["accuracy"] == 1.0
    assert report["member_reports"][0]["dense_layers"] == 1
    with (tmp_path / "report/model_comparison.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert [row["model"] for row in rows] == ["member_01_seed_557", "ensemble"]
    assert rows[0]["correct"] == "1" and rows[0]["errors"] == "0"


def test_members_with_different_heads_are_scored_separately(tmp_path):
    dataset, manifest_path = setup(tmp_path)
    model = SmallRollDigitResNet(dense_hidden_sizes=(64,))
    with torch.no_grad():
        model.head[-1].weight.zero_()
        model.head[-1].bias.zero_()
        model.head[-1].bias[LABELS.index("3")] = 10
    checkpoint = tmp_path / "wrong.pt"
    torch.save({"labels": LABELS, "model_state": model.state_dict(), "model_config": model.model_config()}, checkpoint)
    manifest = json.loads(manifest_path.read_text())
    manifest["members"].append({"seed": 558, "model_path": checkpoint.name, "best_val_accuracy": 1.0,
                                "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest()})
    manifest_path.write_text(json.dumps(manifest))
    report = evaluate(dataset, manifest_path, tmp_path / "report")
    assert [row["accuracy"] for row in report["member_reports"]] == [1.0, 0.0]
    assert [row["dense_layers"] for row in report["member_reports"]] == [1, 2]
    assert report["member_reports"][1]["errors"] == 1
    assert report["member_reports"][0]["weighted_f1"] == 1.0
    with (tmp_path / "report/per_model_predictions.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2 and rows[1]["predicted"] == "3" and rows[1]["correct"] == "False"


def test_evaluation_rejects_leaked_test_writer(tmp_path):
    dataset, manifest = setup(tmp_path)
    path = dataset / "test.csv"
    path.write_text(path.read_text().replace(",test,", ",train,"))
    with pytest.raises(ValueError, match="Students"):
        evaluate(dataset, manifest, tmp_path / "report")


def test_evaluation_rejects_changed_training_split(tmp_path):
    dataset, manifest = setup(tmp_path)
    with (dataset / "train.csv").open("a") as handle:
        handle.write("\n")
    with pytest.raises(ValueError, match="training manifest"):
        evaluate(dataset, manifest, tmp_path / "report")
