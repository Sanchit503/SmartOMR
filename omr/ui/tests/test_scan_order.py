import json
import urllib.error

import pytest

from omr.ui import app
from omr.ui.tests.test_inspection import create, fake_pipeline, generated, get, post, server
from omr.ui.tests.test_ownership_actions import completed, revision


@pytest.mark.parametrize("mode", ["identity", "sheet-major"])
def test_upload_saves_scan_order_and_inspection_shows_it(tmp_path, generated, mode):
    store = app.RunStore(app.UiConfig(tmp_path / "data"))
    state = store.create_run("", {
        "manifest": app.UploadedFile("manifest.json", generated["manifest_path"]),
        "scan_pdf": app.UploadedFile("scan.pdf", generated["pdf_path"]),
    }, {"workflow": "two_step", "grouping_mode": mode})
    assert state["inputs"]["grouping_mode"] == mode
    with server(store) as url:
        body, _ = get(url + "/new")
        assert b'value="identity" selected' in body
        assert b"Sequential scan" in body
        body, _ = get(url + f'/runs/{state["run_id"]}/pages')
        assert f'value="{mode}" selected'.encode() in body
        assert b'name="grouping_mode" form="evaluate-form"' in body


def test_invalid_upload_mode_does_not_create_run(tmp_path, generated):
    store = app.RunStore(app.UiConfig(tmp_path / "data"))
    with pytest.raises(ValueError, match="sequential"):
        store.create_run("", {}, {"grouping_mode": "unknown"})
    assert not store.list_runs()


@pytest.mark.parametrize("mode", ["identity", "sheet-major"])
def test_matching_passes_selected_mode_to_pipeline(tmp_path, generated, monkeypatch, mode):
    store = app.RunStore(app.UiConfig(tmp_path / "data"))
    state = create(store, generated)
    run_id = state["run_id"]
    store.write_state(run_id, inputs={**state["inputs"], "grouping_only": True})
    fake_pipeline(monkeypatch)
    store._run_inspection(run_id)
    monkeypatch.setattr(app, "_require_ui_roll_ocr_backend", lambda: (object(), {}))
    monkeypatch.setattr(store, "_read_identity_pages", lambda *args: {})
    calls = []
    def parse(**kwargs):
        calls.append(kwargs)
        root = kwargs["output_root"]
        root.mkdir()
        path = root / "parse_index.json"
        path.write_text(json.dumps({"exam_id": "UI_TEST", "expected_pages": 2,
            "grouping_only": True, "students": [], "unmatched_pages": [], "page_errors": [],
            "status_counts": {}}))
        return [], path
    monkeypatch.setattr(app, "parse_exam_bundle", parse)
    with server(store) as url:
        post(url + f"/runs/{run_id}/match", {"grouping_mode": mode})
        store._worker.join(timeout=10)
    assert store.read_state(run_id)["status"] == "completed"
    assert calls[0]["grouping_mode"] == mode
    assert store.read_state(run_id)["inputs"]["grouping_mode"] == mode


def test_existing_run_mode_change_reuses_evidence_and_preserves_reviews(tmp_path, generated, monkeypatch):
    from omr.workflows import review
    from omr.workflows.identity_resolution import resolution_digest
    store, run_id, root = completed(tmp_path, generated)
    path = root / "identity_resolution.json"
    original = json.loads(path.read_text())
    original["pages"][1]["identity"]["ocr_results"]["BTECH"]["cells"]["text"] = ""
    original["digest"] = resolution_digest(original)
    path.write_text(json.dumps(original))
    parsed_path = root / "parse_index.json"
    parsed = json.loads(parsed_path.read_text())
    parsed["identity_resolution"]["digest"] = original["digest"]
    parsed_path.write_text(json.dumps(parsed))
    raw = parsed_path.read_bytes()
    review.hold_student(root, "2024001", reviewer="teacher", reason="Keep manual hold")
    index, _ = review.load_or_initialize_verified_index(root)
    student = index["students"][0]
    # Preserve a human-selected page 1 while leaving page 2 unresolved.
    student["pages"] = student["pages"][:1]
    review._write_verified_index(root, index)
    before = json.loads((root / "verified_index.json").read_text())["students"][0]
    monkeypatch.setattr(app, "parse_exam_bundle", lambda **kwargs: pytest.fail("Must reuse saved evidence"))
    monkeypatch.setattr(app, "_require_ui_roll_ocr_backend", lambda: pytest.fail("Must not load ResNet"))
    with server(store) as url:
        base = f"{url}/runs/{run_id}"
        for mode, expected in (("sheet-major", True), ("identity", False)):
            post(base + "/reassess-ownership", {"confirm": "1", "revision": revision(base), "grouping_mode": mode})
            index = json.loads((root / "verified_index.json").read_text())
            resolution = json.loads((root / index["identity_resolution_path"]).read_text())
            assert any(c["path"] == "Order" for c in resolution["candidates"]) == expected
            assert index["students"][0] == before
            assert store.read_state(run_id)["inputs"]["grouping_mode"] == mode
            body, _ = get(base)
            assert f'value="{mode}" selected'.encode() in body
        before_index = (root / "verified_index.json").read_bytes()
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base + "/reassess-ownership", {"confirm": "1", "revision": revision(base), "grouping_mode": "auto"})
        assert error.value.code == 400
        assert (root / "verified_index.json").read_bytes() == before_index
    assert parsed_path.read_bytes() == raw
