import ast
import csv
import hashlib
import io
import json
from pathlib import Path
import zipfile

import pytest
from PIL import Image

from omr.datasets.export_kaggle_training import export_bundle
from omr.datasets.kaggle_train_roll_ensemble import find_package
from omr.datasets.train_roll_digit_ensemble import DEFAULT_SEEDS


def inputs(tmp_path):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    for split, student, color in (("train", "2023001", 100), ("val", "2023002", 200)):
        image = dataset / f"{student}.png"
        Image.new("L", (32, 32), color).save(image)
        with (dataset / f"{split}.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["image_path", "label", "writer_id", "roll_no", "email"])
            writer.writeheader()
            writer.writerow({"image_path": image.name, "label": "2", "writer_id": student,
                             "roll_no": student, "email": "private@example.edu"})
    models = tmp_path / "models"
    models.mkdir()
    members = []
    for seed in DEFAULT_SEEDS:
        checkpoint = models / f"{seed}.pt"
        checkpoint.write_bytes(str(seed).encode())
        members.append({"seed": seed, "model_path": str(checkpoint), "best_val_accuracy": .9,
                        "checkpoint_sha256": hashlib.sha256(checkpoint.read_bytes()).hexdigest()})
    manifest = models / "ensemble_manifest.json"
    manifest.write_text(json.dumps({"kind": "roll_digit_resnet_probability_ensemble", "members": members}))
    return dataset, manifest


def test_export_is_portable_and_drops_direct_student_identifiers(tmp_path):
    dataset, manifest = inputs(tmp_path)
    output = tmp_path / "smartomr_kaggle_training.zip"
    export_bundle(dataset, manifest, output)
    with zipfile.ZipFile(output) as archive:
        metadata = json.loads(archive.read("package_manifest.json"))
        assert metadata["counts"] == {"train": 1, "val": 1}
        groups = []
        for split in ("train", "val"):
            text = archive.read(f"dataset/{split}.csv").decode()
            assert "202300" not in text and "private@example.edu" not in text and str(tmp_path) not in text
            rows = list(csv.DictReader(io.StringIO(text)))
            assert set(rows[0]) == {"image_path", "label", "writer_id", "crop_sha256"}
            assert f"dataset/{rows[0]['image_path']}" in archive.namelist()
            groups.append(rows[0]["writer_id"])
        assert groups[0] != groups[1]
        source = json.loads(archive.read("models/ensemble_manifest.json"))
        assert source["member_count"] == 7
        assert all(f"models/{row['model_path']}" in archive.namelist() for row in source["members"])
        assert not any(".pdf" in name or "roster" in name for name in archive.namelist())
        ast.parse(archive.read("kaggle_train_roll_ensemble.py").decode())
    notebook = json.loads(output.with_suffix(".ipynb").read_text())
    assert notebook["nbformat"] == 4
    ast.parse("".join(notebook["cells"][1]["source"]))
    with pytest.raises(FileExistsError):
        export_bundle(dataset, manifest, output)


def test_bundle_finder_supports_zip_and_extracted_kaggle_inputs(tmp_path):
    dataset, manifest = inputs(tmp_path)
    input_root = tmp_path / "input"
    input_root.mkdir()
    export_bundle(dataset, manifest, input_root / "smartomr_kaggle_training.zip")
    root = find_package(input_root, tmp_path / "work")
    assert (root / "dataset/train.csv").is_file()
    assert find_package(tmp_path / "work", tmp_path / "unused") == root


def test_export_rejects_writer_leakage(tmp_path):
    dataset, manifest = inputs(tmp_path)
    text = (dataset / "val.csv").read_text().replace("2023002", "2023001")
    (dataset / "val.csv").write_text(text)
    with pytest.raises(ValueError, match="Students occur"):
        export_bundle(dataset, manifest, tmp_path / "bad.zip")


def test_export_rejects_changed_checkpoint(tmp_path):
    dataset, manifest = inputs(tmp_path)
    (manifest.parent / "557.pt").write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash mismatch"):
        export_bundle(dataset, manifest, tmp_path / "bad.zip")


def test_archive_traversal_is_rejected(tmp_path):
    input_root = tmp_path / "input"
    input_root.mkdir()
    with zipfile.ZipFile(input_root / "smartomr_kaggle_training.zip", "w") as archive:
        archive.writestr("../escape", "unsafe")
    with pytest.raises(ValueError, match="Unsafe archive"):
        find_package(input_root, tmp_path / "work")
    assert not (tmp_path / "escape").exists()


def test_colab_export_uses_kaggle_only_weights_and_includes_final_test(tmp_path):
    import torch
    from omr.datasets.train_roll_digit_resnet import LABELS, SmallRollDigitResNet

    dataset, manifest = inputs(tmp_path)
    image = dataset / "test.png"
    Image.new("L", (32, 32), 250).save(image)
    (dataset / "test.csv").write_text("image_path,label,writer_id\ntest.png,2,new_writer\n")
    kaggle = manifest.parent / "mnist.pt"
    torch.save({"labels": LABELS, "model_state": SmallRollDigitResNet().state_dict(),
                "best_val_accuracy": .99}, kaggle)
    source = json.loads(manifest.read_text())
    for member in source["members"]:
        member["kaggle_pretrained_path"] = str(kaggle)
    manifest.write_text(json.dumps(source))
    output = tmp_path / "smartomr_colab_training.zip"
    export_bundle(dataset, manifest, output, platform="colab", checkpoint_stage="kaggle")
    with zipfile.ZipFile(output) as archive:
        metadata = json.loads(archive.read("package_manifest.json"))
        assert metadata["checkpoint_stage"] == "kaggle" and metadata["counts"]["test"] == 1
        assert "omr/datasets/evaluate_exported_holdout.py" in archive.namelist()
        assert archive.read("models/member_seed_557.pt") == kaggle.read_bytes()
        ast.parse(archive.read("colab_train_roll_ensemble.py").decode())
    notebook = json.loads(output.with_suffix(".ipynb").read_text())
    assert "Runtime > Change runtime type" in "".join(notebook["cells"][0]["source"])


def test_colab_rejects_omr_pretrained_weights_for_new_holdout(tmp_path):
    dataset, manifest = inputs(tmp_path)
    with pytest.raises(ValueError, match="Kaggle-only"):
        export_bundle(dataset, manifest, tmp_path / "bad.zip", platform="colab")
