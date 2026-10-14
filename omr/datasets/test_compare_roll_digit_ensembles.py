import csv
import hashlib
import json

import pytest
import torch
from PIL import Image

from omr.datasets.compare_roll_digit_ensembles import compare, exposure_audit
from omr.datasets.train_roll_digit_resnet import LABELS, SmallRollDigitResNet


def write_csv(path, rows):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["image_path", "label", "writer_id", "crop_sha256"])
        writer.writeheader()
        writer.writerows(rows)


def setup(tmp_path):
    values = []
    for i, writer in enumerate(("a", "b", "a", "c", "d", "e")):
        image = tmp_path / f"{i}.png"
        Image.new("L", (32, 32), i * 30).save(image)
        values.append({"image_path": image.name, "label": "2", "writer_id": writer,
                       "crop_sha256": hashlib.sha256(image.read_bytes()).hexdigest()})
    write_csv(tmp_path / "test.csv", values[:2])
    write_csv(tmp_path / "catalog.csv", values)
    entries = []
    for index, chosen in enumerate(("2", "3")):
        model = SmallRollDigitResNet()
        with torch.no_grad():
            model.head.weight.zero_()
            model.head.bias.zero_()
            model.head.bias[LABELS.index(chosen)] = 10
        checkpoint = tmp_path / f"model{index}.pt"
        torch.save({"labels": LABELS, "model_state": model.state_dict()}, checkpoint)
        train, val = tmp_path / f"train{index}.csv", tmp_path / f"val{index}.csv"
        write_csv(train, [values[2 + index * 2]])
        write_csv(val, [values[3 + index * 2]])
        manifest = tmp_path / f"manifest{index}.json"
        manifest.write_text(json.dumps({"kind": "roll_digit_resnet_probability_ensemble", "image_size": 64,
            "member_count": 1, "members": [{"model_path": checkpoint.name, "best_val_accuracy": .9,
                                            "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest()}],
            "dataset_csv_sha256": {"train.csv": hashlib.sha256(train.read_bytes()).hexdigest(),
                                   "val.csv": hashlib.sha256(val.read_bytes()).hexdigest()}}))
        entries.append({"name": f"ensemble{index}", "manifest": str(manifest), "train_csv": str(train), "val_csv": str(val)})
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"labels_csv": str(tmp_path / "test.csv"),
                                 "writer_catalog_csv": str(tmp_path / "catalog.csv"),
                                 "require_common_holdout": False, "ensembles": entries}))
    return config


def test_diagnostic_compares_same_crops_and_reports_writer_exposure(tmp_path):
    config = setup(tmp_path)
    report = compare(config, tmp_path / "report", cpu_threads=2)
    assert report["comparison_type"] == "diagnostic_with_exposure"
    assert [r["metrics"]["accuracy"] for r in report["results"]] == [1.0, 0.0]
    assert report["results"][0]["exposure"]["exact_training_crop_overlap"] == 0
    assert report["results"][0]["exposure"]["training_seen_samples"] == 1
    assert report["results"][1]["exposure"]["unused_current_fit_samples"] == 2
    assert report["common_training_unseen_samples"] == report["common_untouched_current_fit_samples"] == 1
    assert report["results"][0]["selective_diagnostics"][0]["accepted"] == 2
    with (tmp_path / "report/paired_predictions.csv").open() as handle:
        assert len(list(csv.DictReader(handle))) == 4
    assert [r["members"][0]["metrics"]["accuracy"] for r in report["results"]] == [1.0, 0.0]
    with (tmp_path / "report/model_comparison.csv").open() as handle:
        comparisons = list(csv.DictReader(handle))
    assert len(comparisons) == 4
    assert [r["kind"] for r in comparisons] == ["member", "ensemble", "member", "ensemble"]
    with (tmp_path / "report/member_predictions.csv").open() as handle:
        assert len(list(csv.DictReader(handle))) == 4
    assert (tmp_path / "report/Three_Ensemble_Comparison.html").is_file()
    with pytest.raises(FileExistsError):
        compare(config, tmp_path / "report")


def test_common_holdout_mode_does_not_hide_writer_leakage(tmp_path):
    config = setup(tmp_path)
    data = json.loads(config.read_text())
    data["require_common_holdout"] = True
    config.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="not a common holdout"):
        compare(config, tmp_path / "report")
    assert not (tmp_path / "report").exists()


def test_split_hash_mismatch_fails_closed(tmp_path):
    config = setup(tmp_path)
    path = tmp_path / "train0.csv"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="hash"):
        compare(config, tmp_path / "report")


def test_exposure_audit_handles_private_colab_writer_codes(tmp_path):
    config = setup(tmp_path)
    data = json.loads(config.read_text())
    entry = data["ensembles"][0]
    train = tmp_path / "train0.csv"
    rows = list(csv.DictReader(train.open()))
    rows[0]["writer_id"] = "anonymous_123"
    write_csv(train, rows)
    manifest = json.loads((tmp_path / "manifest0.json").read_text())
    manifest["dataset_csv_sha256"]["train.csv"] = hashlib.sha256(train.read_bytes()).hexdigest()
    test = list(csv.DictReader((tmp_path / "test.csv").open()))
    catalogue = list(csv.DictReader((tmp_path / "catalog.csv").open()))
    audit, states = exposure_audit(test, entry, manifest, catalogue)
    assert states == ["training_seen", "unused_current_fit"]
    assert audit["training_writer_overlap"] == 1


def test_changed_crop_hash_is_rejected(tmp_path):
    config = setup(tmp_path)
    (tmp_path / "0.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash changed"):
        compare(config, tmp_path / "report")


def test_members_keep_independent_metrics_and_weighted_ensemble_predictions(tmp_path):
    config = setup(tmp_path)
    manifest_path = tmp_path / "manifest0.json"
    manifest = json.loads(manifest_path.read_text())
    other = json.loads((tmp_path / "manifest1.json").read_text())["members"][0]
    manifest["members"][0].update(seed=10, best_val_accuracy=.7)
    manifest["members"].append({**other, "seed": 11, "best_val_accuracy": .3})
    manifest["member_count"] = 2
    manifest_path.write_text(json.dumps(manifest))
    output = tmp_path / "report"
    report = compare(config, output, cpu_threads=2)
    first = report["results"][0]
    assert first["metrics"]["accuracy"] == 1.0
    assert [m["metrics"]["accuracy"] for m in first["members"]] == [1.0, 0.0]
    assert [m["metrics"]["errors"] for m in first["members"]] == [0, 2]
    assert [m["ensemble_weight"] for m in first["members"]] == pytest.approx([.7, .3])
    assert first["members"][1]["by_exposure"]["training_seen"]["errors"] == 1
    assert first["members"][1]["common_training_unseen_subset"]["errors"] == 1
    assert first["members"][1]["selective_diagnostics"][0]["accepted_errors"] == 2
    with (output / "member_predictions.csv").open() as handle:
        individual = list(csv.DictReader(handle))
    with (output / "paired_predictions.csv").open() as handle:
        ensemble = list(csv.DictReader(handle))
    assert len(individual) == 6
    assert len(ensemble) == 4
    for row in ensemble[:2]:
        member_rows = [r for r in individual if r["ensemble"] == row["ensemble"] and r["image_path"] == row["image_path"]]
        probabilities = json.loads(row["probabilities"])
        for label in LABELS:
            expected = sum(w * json.loads(r["probabilities"])[label] for w, r in zip([.7, .3], member_rows))
            assert probabilities[label] == pytest.approx(expected, abs=1e-7)
    with (output / "model_comparison.csv").open() as handle:
        assert len(list(csv.DictReader(handle))) == 5


def test_visual_report_escapes_names_and_embeds_only_failed_crops(tmp_path):
    config = setup(tmp_path)
    data = json.loads(config.read_text())
    data["ensembles"][0]["name"] = '</script><script>alert("bad")</script>'
    config.write_text(json.dumps(data))
    compare(config, tmp_path / "report", cpu_threads=2)
    html = (tmp_path / "report/Three_Ensemble_Comparison.html").read_text()
    assert data["ensembles"][0]["name"] not in html
    assert "\\u003c/script>" in html
    assert html.count("data:image/png;base64,") == 2
