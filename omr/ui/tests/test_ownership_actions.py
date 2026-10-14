import json
import urllib.error

import pytest

from omr.ui import app, student_view
from omr.ui.tests.test_inspection import create, generated, get, post, server
from omr.ui.tests.test_review_state import metric
from omr.workflows.tests.test_ownership import fixture_run


def completed(tmp_path, generated):
    store = app.RunStore(app.UiConfig(tmp_path / "data"))
    state = create(store, generated)
    root = tmp_path / "parsed"
    root.mkdir()
    fixture_run(root, legacy=True)
    store.write_state(state["run_id"], status="completed", parse_dir=str(root),
                      parse_index_path=str(root / "parse_index.json"),
                      inputs={**state["inputs"], "grouping_only": True})
    return store, state["run_id"], root


def revision(base):
    data, _ = get(base + "/review-state.json")
    return json.loads(data)["revision"]


def test_cached_reassessment_and_approval_reflect_everywhere(tmp_path, generated, monkeypatch):
    store, run_id, root = completed(tmp_path, generated)
    monkeypatch.setattr(app, "parse_exam_bundle", lambda **kwargs: pytest.fail("No OCR or parsing rerun permitted"))
    monkeypatch.setattr(app, "_require_ui_roll_ocr_backend", lambda: pytest.fail("No model loading permitted"))
    with server(store) as url:
        base = f"{url}/runs/{run_id}"
        body, _ = get(base + "/review")
        assert b"Reassess Saved Evidence" in body
        raw = (root / "parse_index.json").read_bytes()
        post(base + "/reassess-ownership", {"confirm": "1", "revision": revision(base)})
        for route in (base, base + "/review"):
            body, _ = get(route)
            assert metric(body, "Auto Matched") == 1
            assert metric(body, "Needs Review") == 0
            assert b"Approve Clean Matches (1)" in body
        student, _ = get(base + "/students/2024001?queue=auto_matched")
        assert b"Auto matched" in student
        assert b"Regrading required" not in student
        post(base + "/approve-clean", {"confirm": "1", "confirm_count": "1", "revision": revision(base)})
        for route in (base, base + "/review"):
            body, _ = get(route)
            assert metric(body, "Approved For Release") == 1
            assert metric(body, "Auto Matched") == 0
        student, _ = get(base + "/students/2024001?queue=approved")
        assert b"Approved for release" in student
        inventory, _ = get(base + "/inspection.json")
        assert json.loads(inventory)["review_summary"]["approved"] == 1
        assert (root / "parse_index.json").read_bytes() == raw
    index = json.loads((root / "verified_index.json").read_text())
    assert index["students"][0]["eligible_for_email"]


def test_stale_bulk_actions_are_rejected_without_writes(tmp_path, generated):
    store, run_id, root = completed(tmp_path, generated)
    with server(store) as url:
        base = f"{url}/runs/{run_id}"
        before = (root / "verified_index.json").read_bytes()
        for action in ("reassess-ownership", "approve-clean"):
            with pytest.raises(urllib.error.HTTPError) as error:
                post(base + "/" + action, {"confirm": "1", "revision": "stale", "confirm_count": "1"})
            assert error.value.code == 409
        assert (root / "verified_index.json").read_bytes() == before


@pytest.mark.parametrize("status", ["auto_matched", "approved", "verified"])
def test_clean_and_approved_students_do_not_enter_manual_queue(status):
    row = {"roll_no": "2024001", "status": status}
    assert not student_view.queue_matches(row, "unchecked", "")
    assert not student_view.queue_matches(row, "needs_review", "")
    assert student_view.queue_matches(row, status, "")
