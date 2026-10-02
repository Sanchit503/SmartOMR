from __future__ import annotations

import pytest

from omr.ui import identity_cache


@pytest.mark.parametrize("changed", ["scan", "manifest", "roster", "model", "dpi"])
def test_identity_context_invalidates_changed_reading_inputs(tmp_path, changed):
    inputs = {}
    for name, key in [("scan", "scan_path"), ("manifest", "manifest_path"), ("roster", "students_path")]:
        path = tmp_path / name
        path.write_bytes(b"original")
        inputs[key] = str(path)
    backend = {"provider": "resnet", "provenance": {"checkpoint_sha256": "original"}}
    original = identity_cache.identity_context(inputs, backend, 200.0)
    dpi = 200.0
    if changed in {"scan", "manifest", "roster"}:
        (tmp_path / changed).write_bytes(b"changed")
    elif changed == "model":
        backend["provenance"]["checkpoint_sha256"] = "changed"
    else:
        dpi = 300.0
    assert identity_cache.identity_context(inputs, backend, dpi) != original


def test_page_cache_preserves_evidence_and_rejects_stale_or_missing_assets(tmp_path):
    crop = tmp_path / "identity_preview" / "source_0001" / "digit.png"
    crop.parent.mkdir(parents=True)
    crop.write_bytes(b"crop")
    payload = {"write_in_roll_read": {"cell_crop_paths": {"BTECH": [str(crop)]},
                                    "ocr_results": {"BTECH": {"cell_crop_paths": [str(crop)]}}}}
    row = {"source_index": 1, "sheet_page": 2, "identity_kind": "handwritten", "confidence": "high",
           "literal_roll_no": "2024001", "review_flags": [],
           "identity": identity_cache.relative_evidence(payload, tmp_path)}
    identity_cache.save_page_evidence(tmp_path, row, "image", "context")
    assert identity_cache.load_page_evidence(tmp_path, 1, 2, "image", "context") == row
    assert identity_cache.absolute_evidence(row["identity"], tmp_path) == payload
    for source, page, image, context in [(1, 1, "image", "context"), (1, 2, "other", "context"),
                                          (1, 2, "image", "other")]:
        assert identity_cache.load_page_evidence(tmp_path, source, page, image, context) is None
    crop.unlink()
    assert identity_cache.load_page_evidence(tmp_path, 1, 2, "image", "context") is None


def test_page_cache_rejects_corruption_and_external_asset_paths(tmp_path):
    path = tmp_path / "identity_preview" / "source_0001" / "evidence.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    assert identity_cache.load_page_evidence(tmp_path, 1, 1, "image", "context") is None
    with pytest.raises(ValueError, match="outside its run"):
        identity_cache.absolute_evidence({"crop_paths": {"BTECH": "../../outside.png"}}, tmp_path)


@pytest.mark.parametrize("contents", ["[]", "null", '{"context":"context","aligned_sha256":"image","page":null}'])
def test_page_cache_rejects_malformed_json_shapes(tmp_path, contents):
    path = tmp_path / "identity_preview" / "source_0001" / "evidence.json"
    path.parent.mkdir(parents=True)
    path.write_text(contents, encoding="utf-8")
    assert identity_cache.load_page_evidence(tmp_path, 1, 1, "image", "context") is None
