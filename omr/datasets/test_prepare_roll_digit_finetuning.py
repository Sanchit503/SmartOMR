import csv
import hashlib
import json

import pytest
from PIL import Image

from omr.datasets.prepare_roll_digit_finetuning import finalize


def _dataset(tmp_path):
    fields = []
    annotations = {}
    for index, (writer, split, transcription) in enumerate([
        ("00000", "train", "22222"), ("11111", "val", "11111"),
        (None, None, "22222"), (None, None, "_____"),
    ]):
        paths, hashes = [], []
        for cell in range(5):
            path = tmp_path / f"f{index}_c{cell}.png"
            Image.new("L", (12, 12), index * 30 + cell).save(path)
            paths.append(path.name)
            hashes.append(hashlib.sha256(path.read_bytes()).hexdigest())
        field_id = f"F{index}"
        fields.append({"field_id": field_id, "exam": "quiz" if index < 2 else "midsem",
                       "source_index": index, "sheet_page": 1, "program": "BTECH",
                       "prior_writer": writer, "prior_split": split, "cell_paths": paths,
                       "cell_sha256": hashes, "source_image_path": "", "source_details_path": ""})
        annotations[field_id] = {"transcription": transcription, "reviewer": "human"}
    (tmp_path / "fields.json").write_text(json.dumps(fields))
    path = tmp_path / "annotations.json"
    path.write_text(json.dumps(annotations))
    return fields, annotations, path


def test_visible_labels_override_owner_and_splits_are_retained_across_exams(tmp_path):
    _, _, path = _dataset(tmp_path)
    summary = finalize(tmp_path, path)
    with (tmp_path / "labels.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["label"] for row in rows if row["field_id"] == "F0"} == {"2"}
    assert {row["split"] for row in rows if row["field_id"] == "F2"} == {"train"}
    assert summary["split_only_literal_aliases"] == {"22222": "00000"}
    assert set(summary["train_writers"]).isdisjoint(summary["val_writers"])
    assert summary["class_counts"]["blank"] == 5


def test_uncertain_cells_are_excluded_and_reported(tmp_path):
    _, annotations, path = _dataset(tmp_path)
    annotations["F0"]["transcription"] = "22?22"
    annotations["F0"]["note"] = "Crossed-out digit"
    path.write_text(json.dumps(annotations))
    summary = finalize(tmp_path, path)
    assert summary["crops"] == 19
    assert summary["pending_review_items"] == 1
    assert "Crossed-out digit" in (tmp_path / "pending_review.csv").read_text()


def test_crop_integrity_is_verified_before_training(tmp_path):
    _, _, path = _dataset(tmp_path)
    Image.new("L", (12, 12), 255).save(tmp_path / "f0_c0.png")
    with pytest.raises(ValueError, match="Crop changed"):
        finalize(tmp_path, path)


def test_duplicate_images_are_not_kept_in_both_splits(tmp_path):
    fields, annotations, path = _dataset(tmp_path)
    fields[1]["cell_paths"][0] = fields[0]["cell_paths"][0]
    fields[1]["cell_sha256"][0] = fields[0]["cell_sha256"][0]
    annotations["F1"]["transcription"] = "21111"
    (tmp_path / "fields.json").write_text(json.dumps(fields))
    path.write_text(json.dumps(annotations))
    summary = finalize(tmp_path, path)
    assert summary["duplicate_crops_removed"] == 1
    train = list(csv.DictReader((tmp_path / "train.csv").open()))
    val = list(csv.DictReader((tmp_path / "val.csv").open()))
    assert {row["crop_sha256"] for row in train}.isdisjoint(row["crop_sha256"] for row in val)


def test_bad_length_and_unknown_fields_fail_closed(tmp_path):
    _, annotations, path = _dataset(tmp_path)
    annotations["F0"]["transcription"] = "123"
    path.write_text(json.dumps(annotations))
    with pytest.raises(ValueError, match="Invalid transcription"):
        finalize(tmp_path, path)
    annotations["UNKNOWN"] = {"transcription": "12345", "reviewer": "human"}
    path.write_text(json.dumps(annotations))
    with pytest.raises(ValueError, match="unknown field"):
        finalize(tmp_path, path)
