from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import time
import urllib.error

import pymupdf
import pytest

from omr.ui import app
from omr.ui.app import RunStore, UiConfig
from omr.ui.tests.test_inspection import create, generated, get, post, server
from omr.workflows import review
from omr.workflows.tests.test_review import _write_parsed_batch


def completed_run(tmp_path, generated):
    store = RunStore(UiConfig(tmp_path / "data"))
    state = create(store, generated)
    parsed_dir = _write_parsed_batch(tmp_path)
    review.initialize_verification_index(parsed_dir)
    store.write_state(state["run_id"], status="completed", parse_dir=str(parsed_dir),
                      parse_index_path=str(parsed_dir / "parse_index.json"),
                      summary={"students": 999, "ready": 999, "unmatched_pages": 999},
                      inputs={**state["inputs"], "grouping_only": True})
    return store, state["run_id"], parsed_dir


def metric(body, label):
    return int(re.search(rb"<span>" + label.encode() + rb"</span><strong>(\d+)</strong>", body)[1])


def test_current_counts_and_removed_page_pdf_across_views(tmp_path, generated):
    store, run_id, parsed_dir = completed_run(tmp_path, generated)
    with server(store) as url:
        base = f"{url}/runs/{run_id}"
        post(base + "/pages/2/assign", {"roll_no": "2024002", "page_index": 2, "note": "Checked ownership"})
        body, headers = get(base)
        assert headers["Cache-Control"] == "no-store"
        assert metric(body, "Students") == 2
        assert metric(body, "Unmatched Pages") == 1
        assert metric(body, "Pending Verification") == 0
        assert metric(body, "Needs Review") == 2
        assert b"review_sync.js" in body
        body, _ = get(base + "/students/2024001")
        assert b"Current PDF pending verification" in body
        assert b"Open Student PDF" not in body
        assert b"Original Parser Observations" in body
        post(base + "/students/2024002/verify", {})
        for route in (base, base + "/review"):
            body, _ = get(route)
            assert metric(body, "Manually Checked") == 1
        body, _ = get(url)
        assert b"Manually Checked" in body and b">999<" not in body
        payload, _ = get(base + "/inspection.json")
        inventory = json.loads(payload)
        state_payload, _ = get(base + "/review-state.json")
        assert inventory["review_summary"] == json.loads(state_payload)["summary"]
        assert inventory["pages"][1]["assignment"]["status"] == "verified"
        post(base + "/students/2024001/verify", {})
    verified, _ = review.load_or_initialize_verified_index(parsed_dir)
    first = next(student for student in verified["students"] if student["roll_no"] == "2024001")
    assert first["missing_pages"] == [2]
    assert not first["eligible_for_email"]
    with pymupdf.open(parsed_dir / first["verified_sheet_pdf_path"]) as pdf:
        assert len(pdf) == 1


def test_refresh_endpoints_are_read_only_and_revision_tracks_decisions(tmp_path, generated):
    store, run_id, parsed_dir = completed_run(tmp_path, generated)
    path = parsed_dir / "verified_index.json"
    before = (path.stat().st_mtime_ns, path.read_bytes())
    with server(store) as url:
        base = f"{url}/runs/{run_id}"
        initial, _ = get(base + "/review-state.json")
        home_initial, _ = get(url + "/review-state.json")
        for route in (base + "/inspection.json", base, base + "/review", base + "/email", url):
            get(route)
        assert (path.stat().st_mtime_ns, path.read_bytes()) == before
        post(base + "/students/2024001/verify", {})
        updated, _ = get(base + "/review-state.json")
        home_updated, _ = get(url + "/review-state.json")
        assert json.loads(initial)["revision"] != json.loads(updated)["revision"]
        assert json.loads(home_initial)["revision"] != json.loads(home_updated)["revision"]
        assert json.loads(updated)["summary"]["verified"] == 1
        post(base + "/students/2024001/hold", {})
        updated, _ = get(base + "/review-state.json")
        assert json.loads(updated)["summary"]["verified"] == 0


def test_resolved_unmatched_pages_leave_actionable_review_not_audit(tmp_path, generated):
    store, run_id, parsed_dir = completed_run(tmp_path, generated)
    with server(store) as url:
        base = f"{url}/runs/{run_id}"
        post(base + "/unmatched/3/assign", {"roll_no": "2024002", "page_index": 2, "note": "Checked roll"})
        body, _ = get(base + "/review")
        assert metric(body, "Unmatched Pages") == 0
        assert b"No unmatched pages" in body
    saved = json.loads((parsed_dir / "verified_index.json").read_text())
    assert saved["unmatched_pages"][0]["status"] == "assigned"
    assert saved["unmatched_pages"][0]["decision_log"]


def test_concurrent_review_decisions_do_not_overwrite_each_other(tmp_path, generated, monkeypatch):
    store, run_id, parsed_dir = completed_run(tmp_path, generated)
    write = review._write_verified_index
    def slow_write(*args):
        time.sleep(.1)
        return write(*args)
    monkeypatch.setattr(review, "_write_verified_index", slow_write)
    with server(store) as url:
        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [executor.submit(post, f"{url}/runs/{run_id}/students/{roll}/verify", {})
                    for roll in ("2024001", "2024002")]
            for job in jobs:
                job.result(timeout=10)
    saved = json.loads((parsed_dir / "verified_index.json").read_text())
    assert all(student["status"] == "verified" for student in saved["students"])


def test_removed_pages_require_regrading_even_without_manual_replacements():
    original = {"pages": [{"page": 1, "source_index": 1}, {"page": 2, "source_index": 2}],
                "mcq_score": 8, "mcq_total": 10}
    current = {"pages": [{"page": 1, "source_index": 1}], "manual_pages": []}
    assert app._marks_label(original, current) == "Regrading required"
    assert app._marks_label(original, {"pages": original["pages"]}) == "8 / 10"


def test_old_missing_roster_rows_are_projected_without_rewriting_saved_review(tmp_path, generated):
    store, run_id, parsed_dir = completed_run(tmp_path, generated)
    path = parsed_dir / "verified_index.json"
    saved = json.loads(path.read_text())
    saved["roster_reconciliation"] = {"missing_count": 2, "missing_students": [
        {"roll_no": "2024001", "student_name": "Already Recovered"},
        {"roll_no": "2024003", "student_name": "Actually Missing"},
    ]}
    app._write_json(path, saved)
    before = path.read_bytes()
    with server(store) as url:
        base = f"{url}/runs/{run_id}"
        for route in (base, base + "/review", base + "/email"):
            body, _ = get(route)
            assert metric(body, "Missing Sheets") == 1
        body, _ = get(base + "/review")
        assert b"Already Recovered" not in body
        assert b"Actually Missing" in body
        payload, _ = get(base + "/inspection.json")
        inventory = json.loads(payload)
        assert inventory["review_summary"]["roster_missing"] == 1
        assert len(inventory["review_rolls"]) == 3
    assert path.read_bytes() == before


def test_training_feedback_cannot_overwrite_review_created_student_index(tmp_path, generated, monkeypatch):
    store, run_id, parsed_dir = completed_run(tmp_path, generated)
    store.assign_source(run_id, 3, "2024003", page_index=2, note="Checked new student's roll")
    path = parsed_dir / "verified_index.json"
    before = path.read_bytes()
    def unexpected_feedback(**kwargs):
        raise AssertionError("Synthetic review detail must not enter the original feedback writer")
    monkeypatch.setattr(app, "_save_roll_correction_feedback", unexpected_feedback)
    with server(store) as url:
        base = f"{url}/runs/{run_id}/students/2024003"
        body, _ = get(base)
        assert b'disabled title="No original parser record for this student"' in body
        with pytest.raises(urllib.error.HTTPError) as error:
            post(base + "/correct-roll", {"correct_roll_no": "2024003", "program": "BTECH", "page_index": 2})
        assert error.value.code == 400
        assert b"no original parser record" in error.value.read()
    assert path.read_bytes() == before
