from __future__ import annotations

from contextlib import contextmanager
import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

import numpy as np
from PIL import Image
import pymupdf
import pytest

from omr.generator.config import ExamConfig, NumericalQuestionConfig
from omr.generator.generate import generate_exam
from omr.reader.quality import AlignmentQualityReport
from omr.ui import app, inspection
from omr.ui.app import RunStore, UiConfig, UploadedFile
from omr.workflows.review import assign_unmatched_page, initialize_verification_index, verify_student
from omr.workflows.tests.test_review import _write_parsed_batch


@pytest.fixture
def generated(tmp_path):
    return generate_exam(ExamConfig(
        exam_id="UI_TEST", course_code="TEST", exam_name="Inspection Test", exam_type="quiz", num_mcq=0,
        numerical_questions=[NumericalQuestionConfig(q_no=n, max_marks=1, digits=2) for n in range(1, 10)],
    ), tmp_path / "generated")


def create(store, generated, scan=None, **uploads):
    return store.create_run("", {
        "manifest": UploadedFile("UI_TEST.manifest.json", generated["manifest_path"]),
        "scan_pdf": UploadedFile("scans.pdf", scan or generated["pdf_path"]),
        **uploads,
    })


@contextmanager
def server(store):
    class Handler(app.SmartOmrUiHandler):
        def log_message(self, *args):
            pass
    Handler.store = store
    httpd = app.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def get(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read(), response.headers


def fake_alignment(image, manifest, dpi, source_index):
    return SimpleNamespace(image=image, page_index=1 + source_index % 2,
                           alignment_confidence=.9, page_mark_confidence=.8)


def fake_pipeline(monkeypatch):
    monkeypatch.setattr(inspection, "_render_page", lambda *args: np.full((12, 8), 255, dtype=np.uint8))
    monkeypatch.setattr(inspection, "align_scan_page", fake_alignment)
    monkeypatch.setattr(inspection, "assess_alignment_quality", lambda image, manifest, page, dpi:
                        AlignmentQualityReport(page, "ready", .98, {}))
    def overlay(image, manifest, page, dpi, path):
        Image.fromarray(image).save(path)
        return path
    monkeypatch.setattr(inspection, "save_alignment_overlay", overlay)


def test_300_page_inventory_and_worker_never_drop_failures(tmp_path, generated, monkeypatch):
    scan = tmp_path / "300.pdf"
    with pymupdf.open() as doc:
        for _ in range(300):
            doc.new_page(width=60, height=80)
        doc.save(scan)
    store = RunStore(UiConfig(tmp_path / "data"))
    state = create(store, generated, scan)
    run_dir = store.run_dir(state["run_id"])
    before = store.inspection_index(state["run_id"])
    assert before["total"] == 300
    assert before["counts"]["pending"] == 300
    assert before["processed"] == 0
    fake_pipeline(monkeypatch)
    def align(image, manifest, dpi, source_index):
        if source_index in {1, 150, 300}:
            raise ValueError(f"Unreadable markers on source {source_index}")
        return fake_alignment(image, manifest, dpi, source_index)
    monkeypatch.setattr(inspection, "align_scan_page", align)
    monkeypatch.setattr(app, "_require_ui_roll_ocr_backend", lambda: pytest.fail("Inspection must not run identity OCR"))
    monkeypatch.setattr(app, "parse_exam_bundle", lambda **kwargs: pytest.fail("Inspection must not group or grade"))
    store._run_inspection(state["run_id"])
    after = inspection.load_inventory(run_dir)
    assert [page["source_index"] for page in after["pages"]] == list(range(1, 301))
    assert after["processed"] == after["total"] == 300
    assert after["counts"] == {"pending": 0, "processing": 0, "aligned": 297, "needs_review": 0, "failed": 3}
    assert all((run_dir / page["original"]).is_file() for page in after["pages"])
    assert all(page["aligned"] is None for page in after["pages"] if page["status"] == "failed")
    assert store.read_state(state["run_id"])["status"] == "inspected"
    assert not (run_dir / "parsed").exists()
    retried = []
    def retry(image, manifest, dpi, source_index):
        retried.append(source_index)
        return fake_alignment(image, manifest, dpi, source_index)
    monkeypatch.setattr(inspection, "align_scan_page", retry)
    store._run_inspection(state["run_id"])
    assert retried == [1, 150, 300]
    assert inspection.load_inventory(run_dir)["counts"]["aligned"] == 300


def test_real_alignment_keeps_blank_and_upside_down_pages(tmp_path, generated):
    scan = tmp_path / "mixed.pdf"
    with pymupdf.open(generated["pdf_path"]) as source, pymupdf.open() as doc:
        doc.insert_pdf(source, from_page=1, to_page=1)
        doc[0].set_rotation(180)
        doc.new_page()
        doc.insert_pdf(source, from_page=0, to_page=0)
        doc.save(scan)
    store = RunStore(UiConfig(tmp_path / "data"))
    state = create(store, generated, scan)
    store._run_inspection(state["run_id"])
    index = store.inspection_index(state["run_id"])
    assert index["processed"] == 3
    assert [page["page_index"] for page in index["pages"]] == [2, None, 1]
    assert index["pages"][1]["status"] == "failed"
    assert index["pages"][1]["original"]
    for page in (index["pages"][0], index["pages"][2]):
        assert page["status"] in {"aligned", "needs_review"}
        assert page["overlay"]
        assert "geometry_quality" in page["quality"]["metrics"]
    assert not state["inputs"]["students_path"]
    assert not state["inputs"]["answer_key_path"]


def test_changed_inputs_cannot_resume_or_evaluate(tmp_path, generated):
    store = RunStore(UiConfig(tmp_path / "data"))
    state = create(store, generated)
    manifest = Path(state["inputs"]["manifest_path"])
    manifest.write_text(manifest.read_text() + "\n", encoding="utf-8")
    for evaluate in (False, True):
        with pytest.raises(ValueError, match="inputs have changed"):
            store.start_run(state["run_id"], evaluate=evaluate)
    store._run_inspection(state["run_id"])
    assert store.read_state(state["run_id"])["status"] == "inspection_interrupted"
    assert "inputs have changed" in store.inspection_index(state["run_id"])["error"]


def test_duplicate_upload_names_do_not_overwrite_sources(tmp_path, generated):
    roster = tmp_path / "roster.csv"
    roster.write_text("roll_no,name,email,program\n2024001,Test Student,student@example.edu,BTECH\n")
    key = tmp_path / "key.csv"
    key.write_text("q_no,answer,marks\n1,12,1\n")
    store = RunStore(UiConfig(tmp_path / "data"))
    state = create(store, generated, master_list=UploadedFile("students.csv", roster),
                   answer_key=UploadedFile("students.csv", key))
    inputs = state["inputs"]
    assert Path(inputs["original_answer_key_upload"]).read_bytes() == key.read_bytes()
    assert Path(inputs["original_master_list_upload"]).read_bytes() == roster.read_bytes()
    assert Path(inputs["answer_key_path"]).read_bytes() == key.read_bytes()


def test_missing_uploads_and_wrong_exam_are_rejected(tmp_path, generated):
    store = RunStore(UiConfig(tmp_path / "data"))
    with pytest.raises(ValueError, match="manifest.json is required"):
        store.create_run("", {})
    with pytest.raises(ValueError, match="PDF/image is required"):
        store.create_run("", {"manifest": UploadedFile("manifest.json", generated["manifest_path"])})
    with pytest.raises(ValueError, match="must match the manifest"):
        store.create_run("ANOTHER_EXAM", {"manifest": UploadedFile("manifest.json", generated["manifest_path"]),
                                          "scan_pdf": UploadedFile("scan.pdf", generated["pdf_path"])})


def test_password_protected_pdf_and_multiframe_image_are_rejected(tmp_path):
    encrypted = tmp_path / "encrypted.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(encrypted, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="test")
    with pytest.raises(ValueError, match="password-protected"):
        inspection.source_page_count(encrypted)
    image = tmp_path / "multi.tiff"
    Image.new("L", (30, 40)).save(image, save_all=True, append_images=[Image.new("L", (30, 40))])
    with pytest.raises(ValueError, match="Multi-frame"):
        inspection.source_page_count(image)
    Image.new("L", (30, 40)).save(tmp_path / "one.png")
    assert inspection.source_page_count(tmp_path / "one.png") == 1


def test_restart_recovers_inspection_and_never_replaces_reviewed_output(tmp_path, generated, monkeypatch):
    config = UiConfig(tmp_path / "data")
    store = RunStore(config)
    state = create(store, generated)
    store.write_state(state["run_id"], status="inspecting")
    recovered = RunStore(config)
    assert recovered.read_state(state["run_id"])["status"] == "inspection_interrupted"
    fake_pipeline(monkeypatch)
    recovered.start_run(state["run_id"])
    recovered._worker.join(timeout=10)
    assert not recovered._worker.is_alive()
    run_dir = store.run_dir(state["run_id"])
    (run_dir / "parsed").mkdir()
    with pytest.raises(ValueError, match="reviews are preserved"):
        recovered.start_run(state["run_id"], evaluate=True)


def test_duplicate_job_start_is_rejected(tmp_path, generated, monkeypatch):
    store = RunStore(UiConfig(tmp_path / "data"))
    state = create(store, generated)
    started, release = threading.Event(), threading.Event()
    def slow(run_id):
        started.set()
        release.wait(timeout=10)
    monkeypatch.setattr(store, "_run_inspection", slow)
    try:
        store.start_run(state["run_id"])
        assert started.wait(timeout=5)
        with pytest.raises(ValueError, match="Another operation"):
            store.start_run(state["run_id"])
    finally:
        release.set()
        store._worker.join(timeout=10)


def test_http_inventory_assets_and_error_routes(tmp_path, generated, monkeypatch):
    store = RunStore(UiConfig(tmp_path / "data"))
    state = create(store, generated)
    fake_pipeline(monkeypatch)
    store._run_inspection(state["run_id"])
    with server(store) as url:
        base = f"{url}/runs/{state['run_id']}"
        body, _ = get(base + "/pages")
        assert b"Detected sheet page" in body and b"Run OCR &amp; Grouping" in body
        payload, headers = get(base + "/inspection.json")
        index = json.loads(payload)
        assert index["total"] == 2 and index["can_evaluate"]
        assert headers["Cache-Control"] == "no-store"
        image, _ = get(base + "/pages/1/original")
        assert Image.open(io.BytesIO(image)).size == (8, 12)
        report, headers = get(base + "/pages/1/report")
        assert json.loads(report)["score"] == .98
        assert headers["Content-Type"] == "application/json"
        for resource in ("inspection.css", "inspection.js", "icons/zoom-in.svg", "icons/LICENSE"):
            assert get(f"{url}/static/{resource}")[0]
        for path, code in (("/pages/0/original", 400), ("/pages/3/original", 400), ("/pages/1/unknown", 400)):
            with pytest.raises(urllib.error.HTTPError) as error:
                get(base + path)
            assert error.value.code == code
        with pytest.raises(urllib.error.HTTPError) as error:
            get(url + "/static/../app.py")
        assert error.value.code == 404


def test_student_view_uses_current_manual_selection_and_verified_pdf(tmp_path):
    parsed_dir = _write_parsed_batch(tmp_path)
    initialize_verification_index(parsed_dir)
    # Replace a parser-selected page, not just fill an empty slot.
    assign_unmatched_page(parsed_dir, 3, "2024001", page_index=2, reviewer="test", note="Checked manually")
    store = RunStore(UiConfig(tmp_path))
    app._write_json(store.state_path("review-test"), {
        "run_id": "review-test", "exam_id": "EXAM_REVIEW", "status": "completed",
        "parse_dir": str(parsed_dir), "parse_index_path": str(parsed_dir / "parse_index.json"),
    })
    with server(store) as url:
        body, _ = get(url + "/runs/review-test/students/2024001")
        assert b"Regrading required" in body
        assert b"manual_assignment" in body
        assert b"Source 3" in body
        assert b"Source 2" not in body
        assert b"Current PDF pending verification" in body
        assert b"Open Original Parser PDF" not in body
        verify_student(parsed_dir, "2024001", reviewer="test", note="Checked both pages")
        body, _ = get(url + "/runs/review-test/students/2024001")
        assert b"Open Verified PDF" in body
        expected_pdf = parsed_dir / "verified/students/2024001/sheet.pdf"
        assert app._asset_url(expected_pdf).encode() in body
        data, _ = get(url + app._asset_url(expected_pdf))
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            assert len(doc) == 2
            pix = doc[1].get_pixmap(colorspace=pymupdf.csGRAY)
            assert abs(pix.pixel(pix.width // 2, pix.height // 2)[0] - 170) <= 2
        assert b"MANUALLY_CHECKED" in body
        assert b"Regrading required" in body


@pytest.mark.parametrize("parser,verified,expected", [
    ("ready", None, "PENDING_VERIFICATION"),
    ("ready", "pending_verification", "PENDING_VERIFICATION"),
    ("ready", "rejected", "REJECTED"),
    ("ready", "missing_pages", "MISSING_PAGES"),
    ("needs_review", "verified", "MANUALLY_CHECKED"),
])
def test_status_never_implies_grading(parser, verified, expected):
    assert app._professor_status(parser, verified) == expected


def test_marks_are_not_implied_without_grading():
    assert app._marks_label({}) == "Not graded"
    assert app._marks_label({"mcq_score": 3, "mcq_total": 10}) == "3 / 10"
    assert app._marks_label({"mcq_score": 0, "mcq_total": 0, "numerical_total": 5}) == "Grading incomplete"
    assert app._score({}) == (None, None)
    assert app._score({"mcq_score": 0, "mcq_total": 0, "numerical_total": 5}) == (None, 5)


def test_evaluation_is_explicit_and_does_not_reset_completed_run(tmp_path, generated, monkeypatch):
    store = RunStore(UiConfig(tmp_path / "data"))
    state = create(store, generated)
    with pytest.raises(ValueError, match="Complete page inspection"):
        store.start_run(state["run_id"], evaluate=True)
    fake_pipeline(monkeypatch)
    store._run_inspection(state["run_id"])
    calls = []
    monkeypatch.setattr(store, "_run_batch", lambda run_id: calls.append(run_id))
    store.start_run(state["run_id"], evaluate=True)
    store._worker.join(timeout=10)
    assert calls == [state["run_id"]]
    store.write_state(state["run_id"], status="completed", parse_index_path="already-reviewed.json")
    with pytest.raises(ValueError, match="reviews are preserved"):
        store.start_run(state["run_id"], evaluate=True)


def test_render_failure_preserves_inventory_and_later_pages(tmp_path, generated, monkeypatch):
    store = RunStore(UiConfig(tmp_path / "data"))
    state = create(store, generated)
    fake_pipeline(monkeypatch)
    def render(path, source_index, dpi):
        if source_index == 1:
            raise ValueError("Cannot render this page")
        return np.full((12, 8), 255, dtype=np.uint8)
    monkeypatch.setattr(inspection, "_render_page", render)
    store._run_inspection(state["run_id"])
    index = store.inspection_index(state["run_id"])
    assert index["total"] == index["processed"] == 2
    assert index["pages"][0]["original"] is None
    assert index["pages"][0]["status"] == "failed"
    assert index["pages"][1]["status"] == "aligned"


def test_alignment_review_flags_are_preserved(tmp_path, generated, monkeypatch):
    store = RunStore(UiConfig(tmp_path / "data"))
    state = create(store, generated)
    fake_pipeline(monkeypatch)
    monkeypatch.setattr(inspection, "assess_alignment_quality", lambda image, manifest, page, dpi:
                        AlignmentQualityReport(page, "needs_review", .4, {}, review_flags=["Local drift"]))
    store._run_inspection(state["run_id"])
    index = store.inspection_index(state["run_id"])
    assert index["counts"]["needs_review"] == 2
    assert index["counts"]["aligned"] == 0
    assert index["pages"][0]["quality"]["review_flags"] == ["Local drift"]


def test_atomic_inventory_can_be_read_while_processing(tmp_path, generated, monkeypatch):
    store = RunStore(UiConfig(tmp_path / "data"))
    state = create(store, generated)
    fake_pipeline(monkeypatch)
    observed = []
    original_progress = store.write_state
    def progress(run_id, **updates):
        index = store.inspection_index(run_id)
        observed.append(index["processed"])
        assert sum(index["counts"].values()) == index["total"]
        return original_progress(run_id, **updates)
    monkeypatch.setattr(store, "write_state", progress)
    store.start_run(state["run_id"])
    deadline = time.monotonic() + 20
    while store._worker.is_alive():
        assert time.monotonic() < deadline, "Inspection worker did not finish"
        assert store.inspection_index(state["run_id"])["total"] == 2
    store._worker.join(timeout=10)
    assert min(observed) == 0 and max(observed) == 2


def test_artifact_endpoint_does_not_expose_source_code(tmp_path):
    store = RunStore(UiConfig(tmp_path))
    with server(store) as url:
        with pytest.raises(urllib.error.HTTPError) as error:
            get(url + app._asset_url(Path(app.__file__)))
        assert error.value.code == 404
