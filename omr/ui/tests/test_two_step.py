import json
import threading
import urllib.error
import urllib.request

import pytest

from omr.ui import app
from omr.ui.tests.test_app import _multipart_body
from omr.ui.tests.test_inspection import fake_pipeline, generated, get, server
from omr.ui.tests.test_student_workspace import completed_run


def test_new_run_has_one_grouping_action_and_no_grading_requirements(tmp_path):
    store = app.RunStore(app.UiConfig(tmp_path))
    with server(store) as url:
        body, _headers = get(url + "/new")
    assert b"1. Detect Rolls &amp; Group Sheets" in body
    assert b'name="workflow" value="two_step"' in body
    assert b"Start Inspection" not in body and b'name="answer_key"' not in body
    assert b"grouping-only" not in body


def test_upload_chains_inspection_and_grouping_without_second_request(tmp_path, generated, monkeypatch):
    store = app.RunStore(app.UiConfig(tmp_path / "data"))
    fake_pipeline(monkeypatch)
    finished = threading.Event()
    def match(run_id):
        inventory = store.inspection_index(run_id)
        assert inventory["status"] == "completed"
        assert store.read_state(run_id)["inputs"]["grouping_only"] is True
        store.write_state(run_id, status="completed", stage="Grouped")
        finished.set()
    monkeypatch.setattr(store, "_run_batch", match)
    boundary = "two-step-test"
    body = _multipart_body(boundary, [("manifest", "manifest.json", generated["manifest_path"].read_bytes()),
        ("scan_pdf", "scan.pdf", generated["pdf_path"].read_bytes()), ("workflow", None, b"two_step")])
    with server(store) as url:
        request = urllib.request.Request(url + "/runs", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            assert "matching=1" in response.url
        assert finished.wait(timeout=10)
        store._worker.join(timeout=10)
        state = store.list_runs()[0]
        assert state["status"] == "completed" and state["inputs"]["answer_key_path"] is None


def test_completed_two_step_run_offers_grading_on_all_views(tmp_path, generated):
    store, run_id, parsed = completed_run(tmp_path, generated)
    state = store.read_state(run_id)
    store.write_state(run_id, inputs={**state["inputs"], "workflow": "two_step"})
    with server(store) as url:
        for path in (f"/runs/{run_id}", f"/runs/{run_id}/review", f"/runs/{run_id}/pages"):
            body, _headers = get(url + path)
            assert b"2. Grade Answers" in body
        inventory, _headers = get(url + f"/runs/{run_id}/inspection.json")
        inventory = json.loads(inventory)
        assert inventory["can_grade"] and not inventory["can_identity_preview"] and not inventory["can_re_evaluate"]
        form, _headers = get(url + f"/runs/{run_id}/grade")
        assert b"renderAnswerKey({" in form and b"Dropped" in form


def test_stale_grading_form_cannot_start_job(tmp_path, generated):
    store, run_id, parsed = completed_run(tmp_path, generated)
    boundary = "stale-grade"
    body = _multipart_body(boundary, [("revision", None, b"old-revision")])
    before = (parsed / "verified_index.json").read_bytes()
    with server(store) as url:
        request = urllib.request.Request(url + f"/runs/{run_id}/grade", data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=10)
        assert error.value.code == 409
    assert (parsed / "verified_index.json").read_bytes() == before and store._worker is None
