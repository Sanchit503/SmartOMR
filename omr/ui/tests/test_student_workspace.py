from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

import pymupdf
import pytest

from omr.ui import app, student_view
from omr.ui.tests.test_inspection import create, generated, get, server
from omr.workflows import review
from omr.workflows.tests.test_review import _write_page, _write_parsed_batch


def completed_run(tmp_path, generated):
    store = app.RunStore(app.UiConfig(tmp_path))
    state = create(store, generated)
    parsed = _write_parsed_batch(tmp_path)
    review.initialize_verification_index(parsed)
    store.write_state(state["run_id"], status="completed", parse_dir=str(parsed),
                      parse_index_path=str(parsed / "parse_index.json"),
                      inputs={**state["inputs"], "grouping_only": True})
    return store, state["run_id"], parsed


def view_data(body):
    return json.loads(re.search(rb'<script id="student-view-data" type="application/json">(.*?)</script>', body, re.S)[1])


def decision(url, fields):
    request = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                     headers={"Accept": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def test_workspace_and_preview_follow_current_selection_without_reprocessing(tmp_path, generated, monkeypatch):
    store, run_id, parsed = completed_run(tmp_path, generated)
    store.assign_source(run_id, 2, "2024002", page_index=2, note="Checked written roll")
    monkeypatch.setattr(app, "parse_exam_bundle", lambda **kwargs: pytest.fail("Viewer must not run recognition"))
    monkeypatch.setattr(app, "_require_ui_roll_ocr_backend", lambda: pytest.fail("Viewer must not load OCR"))
    saved = (parsed / "verified_index.json").read_bytes()
    with server(store) as url:
        base = f"{url}/runs/{run_id}/students"
        body, _ = get(base + "/2024001")
        assert b"student.css" in body and b"student.js" in body
        assert b"Current Selection Preview" in body and b"1 of 2 pages selected" in body
        assert b"Current PDF pending verification" in body
        assert b"Answers And Marks" not in body and b"Regrading required" not in body
        assert [page["source_index"] for page in view_data(body)["pages"]] == [1, None]
        pdf, headers = get(base + "/2024001/preview.pdf")
        assert headers["Content-Type"] == "application/pdf" and headers["Cache-Control"] == "no-store"
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            assert len(doc) == 1
        body, _ = get(base + "/2024002")
        assert [page["source_index"] for page in view_data(body)["pages"]] == [4, 2]
        for page in view_data(body)["pages"]:
            image, _ = get(url + page["aligned"])
            assert image.startswith(b"\x89PNG")
        pdf, _ = get(base + "/2024002/preview.pdf")
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            assert len(doc) == 2
            assert doc[0].get_pixmap().samples != doc[1].get_pixmap().samples
    assert (parsed / "verified_index.json").read_bytes() == saved
    assert not (parsed / "verified").exists()


@pytest.mark.parametrize("expected", [3, 4])
def test_workspace_handles_all_manifest_slots_and_manual_replacement_order(tmp_path, generated, expected):
    store, run_id, parsed = completed_run(tmp_path, generated)
    path = parsed / "verified_index.json"
    saved = json.loads(path.read_text())
    saved["expected_pages"] = expected
    first = saved["students"][0]
    for number in range(3, expected + 1):
        image = parsed / f"extra_{number}.png"
        _write_page(image, 100 + number)
        first["manual_pages"].append({"page": number, "source_index": number + 10,
                                      "canonical_image_path": str(image), "origin": "manual_assignment"})
    first["missing_pages"] = []
    app._write_json(path, saved)
    with server(store) as url:
        base = f"{url}/runs/{run_id}/students/2024001"
        body, _ = get(base)
        assert [page["page"] for page in view_data(body)["pages"]] == list(range(1, expected + 1))
        assert f"{expected} of {expected} pages selected".encode() in body
        pdf, _ = get(base + "/preview.pdf")
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            assert len(doc) == expected
        response = decision(base + "/verify", {"workspace": "1", "advance": "1", "queue": "unchecked"})
        assert "/students/2024002?" in response["redirect_url"]
    checked = json.loads(path.read_text())["students"][0]
    with pymupdf.open(parsed / checked["verified_sheet_pdf_path"]) as doc:
        assert len(doc) == expected


def test_missing_sheet_requires_explicit_acknowledgement_in_workspace(tmp_path, generated):
    store, run_id, parsed = completed_run(tmp_path, generated)
    with server(store) as url:
        base = f"{url}/runs/{run_id}/students/2024002"
        before = (parsed / "verified_index.json").read_bytes()
        with pytest.raises(urllib.error.HTTPError) as error:
            decision(base + "/verify", {"workspace": "1", "advance": "1"})
        assert b"missing page(s) 2" in error.value.read()
        assert (parsed / "verified_index.json").read_bytes() == before
        decision(base + "/verify", {"workspace": "1", "allow_missing": "1"})
    checked = json.loads((parsed / "verified_index.json").read_text())["students"][1]
    assert checked["status"] == "verified" and not checked["eligible_for_email"]


def test_stale_workspace_cannot_verify_new_selection_or_lose_note(tmp_path, generated):
    store, run_id, parsed = completed_run(tmp_path, generated)
    with server(store) as url:
        base = f"{url}/runs/{run_id}"
        payload, _ = get(base + "/review-state.json")
        revision = json.loads(payload)["revision"]
        store.assign_source(run_id, 2, "2024002", page_index=2, note="Check current owner")
        before = (parsed / "verified_index.json").read_bytes()
        with pytest.raises(urllib.error.HTTPError) as error:
            decision(base + "/students/2024001/verify", {"workspace": "1", "revision": revision, "note": "old view"})
        assert error.value.code == 409
        assert "Review state changed" in json.loads(error.value.read())["error"]
        assert (parsed / "verified_index.json").read_bytes() == before


def test_verify_and_hold_next_use_filtered_queue_and_stop_at_end(tmp_path, generated):
    store, run_id, parsed = completed_run(tmp_path, generated)
    with server(store) as url:
        base = f"{url}/runs/{run_id}/students"
        result = decision(base + "/2024001/verify", {"workspace": "1", "advance": "1", "queue": "unchecked", "note": "Checked both"})
        assert "/2024002?" in result["redirect_url"] and "queue=unchecked" in result["redirect_url"]
        body, _ = get(url + result["redirect_url"])
        assert b"Saved: sheet manually checked." in body
        result = decision(base + "/2024002/hold", {"workspace": "1", "advance": "1", "queue": "unchecked", "note": "Find missing second page"})
        assert "finished=1" in result["redirect_url"]
        body, _ = get(url + result["redirect_url"])
        assert b"End of this queue." in body
        assert b"Find missing second page" not in body  # Note is in audit, not silently submitted again.
        result = decision(base + "/2024001/hold", {"advance": "1", "queue": "all", "q": "Student 1"})
        assert "/2024001?" in result["redirect_url"] and "finished=1" in result["redirect_url"]
    index = json.loads((parsed / "verified_index.json").read_text())
    assert index["students"][1]["decision_log"][-1]["note"] == "Find missing second page"


@pytest.mark.parametrize("corruption", ["duplicate_slot", "duplicate_source", "foreign_owner", "invalid_slot"])
def test_verification_blocks_ambiguous_selection(tmp_path, generated, corruption):
    store, run_id, parsed = completed_run(tmp_path, generated)
    path = parsed / "verified_index.json"
    saved = json.loads(path.read_text())
    first = saved["students"][0]
    if corruption == "duplicate_slot":
        first["pages"].append(dict(first["pages"][0]))
    elif corruption == "duplicate_source":
        first["pages"][1]["source_index"] = 1
    elif corruption == "foreign_owner":
        saved["students"][1]["pages"][0]["source_index"] = 1
    else:
        first["pages"][1]["page"] = 9
    app._write_json(path, saved)
    before = path.read_bytes()
    with server(store) as url:
        base = f"{url}/runs/{run_id}/students/2024001"
        body, _ = get(base)
        assert view_data(body)["blocked"]
        with pytest.raises(urllib.error.HTTPError):
            decision(base + "/verify", {"workspace": "1", "allow_missing": "1"})
    assert path.read_bytes() == before


def test_queue_filters_and_safe_markup():
    index = {"students": [
        {"roll_no": "2024001", "student_name": "Alice", "status": "verified", "missing_pages": [2]},
        {"roll_no": "2024002", "student_name": "Bob", "status": "pending_verification"},
        {"roll_no": "MT25001", "student_name": "Carol", "status": "missing_pages", "missing_pages": [1]},
    ]}
    assert student_view.next_student(index, "2024001", "unchecked", "") == "2024002"
    assert student_view.next_student(index, "2024001", "all", "CAROL") == "MT25001"
    assert student_view.queue_matches(index["students"][0], "missing_pages", "alice")
    assert not student_view.queue_matches(index["students"][0], "unchecked", "")
    assert student_view.queue_parameters({"queue": "unknown"}) == ("all", "")
    body = student_view.workspace_body(
        state={"run_id": "test", "exam_id": "<exam>", "inputs": {}},
        student=index["students"][0], index=index, summary=app._review_summary(index),
        page_views=[{"page": 1, "aligned": '</script><script>alert(1)</script>', "source_index": 1}],
        flags=["<flag>"], conflicts=[], revision="revision", kind="all", search="<search>",
        pdf_link="", operations="", answers="", original="",
    )
    assert "<exam>" not in body and "<flag>" not in body and "<search>" not in body
    assert "</script><script>alert" not in body


def test_empty_preview_does_not_create_verified_artifacts():
    with pytest.raises(ValueError, match="no selected pages"):
        student_view.preview_pdf([])


def test_assigned_page_shows_its_own_saved_identity_not_previous_students_read(tmp_path, generated):
    store, run_id, parsed = completed_run(tmp_path, generated)
    store.assign_source(run_id, 3, "2024001", page_index=2, note="Replace incorrect page", replace_existing=True)
    evidence_path = store.run_dir(run_id) / "identity_preview" / "source_0003" / "evidence.json"
    app._write_json(evidence_path, {"page": {
        "source_index": 3, "sheet_page": 2, "literal_roll_no": "2024001", "confidence": "medium",
        "identity_kind": "handwritten", "identity": {"roll_no": "2024001"},
        "review_flags": ["Faint digit needs manual inspection"],
    }})
    before = (parsed / "verified_index.json").read_bytes()
    with server(store) as url:
        body, _ = get(f"{url}/runs/{run_id}/students/2024001")
        second = view_data(body)["pages"][1]
        assert second["source_index"] == 3
        assert second["evidence"]["written_roll"] == "2024001"
        assert second["evidence"]["flags"] == ["Faint digit needs manual inspection"]
    assert (parsed / "verified_index.json").read_bytes() == before


def test_uploaded_page_does_not_show_wrong_original_batch_source(tmp_path, generated):
    store, run_id, parsed = completed_run(tmp_path, generated)
    details_path = parsed / "students" / "2024001" / "student.json"
    details = app._read_json(details_path)
    details["source_pages"] = [{"source_index": 1, "page_index": 1, "source_path": str(tmp_path / "replacement.pdf")}]
    app._write_json(details_path, details)
    original = store.run_dir(run_id) / "inspection" / "original.png"
    _write_page(original, 200)
    app._write_json(store.run_dir(run_id) / "inspection" / "index.json", {
        "pages": [{"source_index": 1, "original": "inspection/original.png"}],
    })
    with server(store) as url:
        body, _ = get(f"{url}/runs/{run_id}/students/2024001")
        first = view_data(body)["pages"][0]
        assert first["original"] is None
        assert first["source_index"] is None and first["origin"] == "uploaded"
        assert first["aligned"]


def test_grading_views_remain_separate_and_dashboard_links_to_workspace(tmp_path, generated):
    store, run_id, parsed = completed_run(tmp_path, generated)
    with server(store) as url:
        base = f"{url}/runs/{run_id}"
        for route in (base, base + "/review"):
            body, _ = get(route)
            assert b"Review Student Sheets" in body and b"queue=unchecked" in body
        body, _ = get(base + "/students/2024001")
        assert b"Answers And Marks" not in body and b"Edit Marks" not in body
        state = store.read_state(run_id)
        store.write_state(run_id, inputs={**state["inputs"], "grouping_only": False})
        body, _ = get(base + "/students/2024001")
        assert b"Answers And Marks" in body and b"Edit Marks" in body
