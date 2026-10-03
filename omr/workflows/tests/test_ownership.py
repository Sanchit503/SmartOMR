import json

from PIL import Image
import pymupdf
import pytest

from omr.workflows.ownership import POLICY_VERSION, assess_ownership, order_suggestions
from omr.workflows.ownership_review import reassess_saved_ownership, approve_clean_matches
from omr.workflows.identity_resolution import resolution_digest, page_evidence
from omr.workflows import review
from omr.workflows.tests.test_identity_resolution import _page_one, _ocr_identity


def page(source, slot, roll="2024001", *, minimum=.95, selector="clear", program="BTECH"):
    if slot == 1:
        result = _page_one(roll, roll, source=source, minimum=minimum, program=program)
    else:
        result = {"source_index": source, "sheet_page": slot, "identity_kind": "handwritten",
                  "literal_roll_no": roll, "program": program, "confidence": "high",
                  "identity": _ocr_identity(roll, minimum=minimum, selector=selector, program=program)}
    result["quality"] = {"status": "ready", "metrics": {
        name: {"status": "ready"} for name in ("geometry_quality", "local_quality", "image_quality")}}
    return result


@pytest.mark.parametrize("slots", [2, 3, 4])
def test_shuffled_multi_page_ownership_is_order_independent(slots):
    pages = [page(source, slot, roll) for source, (roll, slot) in enumerate(
        [(roll, slot) for roll in ("2024001", "2024002", "2024003") for slot in range(1, slots + 1)], 1)]
    shuffled = list(reversed(pages[::2])) + list(reversed(pages[1::2]))
    result = assess_ownership(shuffled, expected_pages=slots, valid_rolls={"2024001", "2024002", "2024003"})
    assert result["assignments"] == assess_ownership(pages, expected_pages=slots)["assignments"]
    for roll, student in result["students"].items():
        assert student["status"] == "auto_matched"
        assert len(student["source_indices"]) == slots
        assert all(pages[source - 1]["literal_roll_no"] == roll for source in student["source_indices"])


def test_alternating_page_codes_do_not_establish_ownership():
    result = assess_ownership([page(1, 1), page(2, 2, "2024002"), page(3, 1, "2024002"), page(4, 2)], expected_pages=2)
    assert result["students"]["2024001"]["source_indices"] == [1, 4]
    assert result["students"]["2024002"]["source_indices"] == [3, 2]


@pytest.mark.parametrize("failure", ["blank", "strip", "low", "conflict", "duplicate", "quality"])
def test_ambiguous_continuation_is_never_automatic(failure):
    pages = [page(1, 1), page(2, 2)]
    if failure in {"blank", "strip"}:
        pages[1]["identity"]["ocr_results"]["BTECH"]["cells"]["text"] = ""
    if failure == "low":
        pages[1]["identity"]["ocr_results"]["BTECH"]["cells"]["confidence"] = .79
    if failure == "conflict":
        pages[1] = page(2, 2, "2025001")
    if failure == "duplicate":
        pages.append(page(3, 2))
    if failure == "quality":
        pages[1]["quality"]["status"] = "needs_review"
    result = assess_ownership(pages, expected_pages=2)
    assert "2" not in result["assignments"]
    assert result["students"]["2024001"]["missing_pages"] == [2]


@pytest.mark.parametrize("probability,accepted", [(.84, False), (.85, True)])
def test_near_neighbour_guard_applies_to_differing_cell(probability, accepted):
    pages = [page(1, 1), page(2, 2)]
    read = pages[1]["identity"]["ocr_results"]["BTECH"]["cells"]
    read["raw"]["cells"][-1]["raw"]["class_probabilities"]["1"] = probability
    result = assess_ownership(pages, expected_pages=2, valid_rolls={"2024001", "2024002"})
    assert ("2" in result["assignments"]) == accepted


@pytest.mark.parametrize("program,roll,accepted", [("BTECH", "2024001", True), ("MTECH", "MT24001", False), ("PHD", "PHD24001", False)])
def test_blank_selector_recovery_is_only_for_btech(program, roll, accepted):
    result = assess_ownership([page(1, 1, roll, program=program), page(2, 2, roll, selector="blank", program=program)], expected_pages=2)
    assert ("2" in result["assignments"]) == accepted


def test_order_only_suggestion_is_blocked():
    pages = [page(1, 1), page(2, 2)]
    pages[1]["identity"]["ocr_results"]["BTECH"]["cells"]["text"] = ""
    result = order_suggestions(pages, {1: "2024001"}, mode="sheet-major", expected_pages=2, total_pages=2)
    assert result[0]["path"] == "Order"
    assert result[0]["blocked"]
    assert not result[0]["automatic_attachment_enabled"]


@pytest.mark.parametrize("program,roll", [("MTECH", "MT24001"), ("PHD", "PHD24001")])
def test_numeric_cells_receive_correct_program_prefix(program, roll):
    data = page(2, 2, roll, program=program)
    data["identity"]["ocr_results"][program]["cells"]["text"] = "24001"
    assert page_evidence(data)["cell_roll"] == roll


def fixture_run(root, *, slots=2, legacy=False):
    pages = [page(index, index) for index in range(1, slots + 1)]
    artifacts = []
    for item in pages:
        source = item["source_index"]
        Image.new("RGB", (100, 140), (255 - source * 15, 255, 255)).save(root / f"{source}.png")
        (root / f"{source}.json").write_text(json.dumps(item["quality"]))
        artifacts.append({"page_index": source, "source_index": source,
                          "canonical_image_path": f"{source}.png", "alignment_report_path": f"{source}.json"})
    certificate = {"policy_version": POLICY_VERSION, "status": "auto_matched", "source_indices": list(range(1, slots + 1))}
    details = {"student": {"roll_no": "2024001", "program": "BTECH", "email": "nobody@example.invalid"}, "pages": artifacts}
    (root / "student.json").write_text(json.dumps(details))
    resolution = {"pages": pages, "candidates": []}
    resolution["digest"] = resolution_digest(resolution)
    (root / "identity_resolution.json").write_text(json.dumps(resolution))
    student = {"roll_no": "2024001", "status": "ready", "details_path": "student.json", "pages": artifacts,
               "review_flags": ["numerical Q2 faint"] if legacy else [], "answer_review_flags": []}
    if not legacy:
        student["ownership_decision"] = certificate
    parsed = {"exam_id": "OWNERSHIP_TEST", "expected_pages": slots, "grouping_only": True, "students": [student],
              "unmatched_pages": [], "page_errors": [], "identity_resolution_path": "identity_resolution.json",
              "identity_resolution": {"digest": resolution["digest"]}, "grouping_order_inference": {"mode": "sheet-major"}}
    (root / "parse_index.json").write_text(json.dumps(parsed))
    return review.initialize_verification_index(root)[0]


@pytest.mark.parametrize("slots", [2, 3, 4])
def test_clean_release_requires_explicit_approval_and_builds_all_pages(tmp_path, slots):
    initial = fixture_run(tmp_path, slots=slots)
    assert initial["students"][0]["status"] == "auto_matched"
    assert not initial["students"][0]["eligible_for_email"]
    with pytest.raises(ValueError, match="count"):
        approve_clean_matches(tmp_path, reviewer="test", confirm_count=2)
    result, _ = approve_clean_matches(tmp_path, reviewer="test", confirm_count=1)
    student = result["students"][0]
    assert student["status"] == "approved"
    assert student["eligible_for_email"]
    with pymupdf.open(tmp_path / student["verified_sheet_pdf_path"]) as pdf:
        assert len(pdf) == slots


def test_cached_reassessment_preserves_human_decisions_and_raw_parser(tmp_path):
    fixture_run(tmp_path, legacy=True)
    review.hold_student(tmp_path, "2024001", reviewer="teacher", reason="Keep this on hold")
    before = review._load_json(tmp_path / review.VERIFIED_INDEX_NAME)["students"][0]
    raw = (tmp_path / "parse_index.json").read_bytes()
    result, _ = reassess_saved_ownership(tmp_path, reviewer="test", grouping_only=True)
    assert result["students"][0] == before
    assert (tmp_path / "parse_index.json").read_bytes() == raw


def test_cached_reassessment_removes_answer_only_legacy_flags(tmp_path):
    fixture_run(tmp_path, legacy=True)
    result, _ = reassess_saved_ownership(tmp_path, reviewer="test", grouping_only=True)
    assert result["students"][0]["status"] == "auto_matched"
    assert not result["students"][0]["review_flags"]
    assert not result["students"][0]["eligible_for_email"]


def test_changed_quality_cannot_be_bulk_approved(tmp_path):
    fixture_run(tmp_path)
    report = json.loads((tmp_path / "2.json").read_text())
    report["status"] = "needs_review"
    (tmp_path / "2.json").write_text(json.dumps(report))
    with pytest.raises(ValueError, match="Ownership"):
        approve_clean_matches(tmp_path, reviewer="test", confirm_count=1)
    assert review._load_json(tmp_path / review.VERIFIED_INDEX_NAME)["students"][0]["status"] == "auto_matched"


def test_incomplete_legacy_continuation_returns_to_unmatched(tmp_path):
    fixture_run(tmp_path, legacy=True)
    path = tmp_path / "identity_resolution.json"
    data = json.loads(path.read_text())
    data["pages"][1]["identity"]["ocr_results"]["BTECH"]["cells"]["text"] = ""
    data["digest"] = resolution_digest(data)
    path.write_text(json.dumps(data))
    parsed_path = tmp_path / "parse_index.json"
    parsed = json.loads(parsed_path.read_text())
    parsed["identity_resolution"]["digest"] = data["digest"]
    parsed_path.write_text(json.dumps(parsed))
    result, _ = reassess_saved_ownership(tmp_path, reviewer="test", grouping_only=True)
    assert result["students"][0]["missing_pages"] == [2]
    assert not result["students"][0]["eligible_for_email"]
    assert next(row for row in result["unmatched_pages"] if row["source_index"] == 2)["status"] == "needs_review"


def test_approval_failure_leaves_index_unmodified(tmp_path):
    fixture_run(tmp_path)
    (tmp_path / "2.png").write_bytes(b"not an image")
    before = (tmp_path / review.VERIFIED_INDEX_NAME).read_bytes()
    with pytest.raises(OSError):
        approve_clean_matches(tmp_path, reviewer="test", confirm_count=1)
    assert (tmp_path / review.VERIFIED_INDEX_NAME).read_bytes() == before


def test_email_queue_is_invalidated_after_review_changes(tmp_path):
    from omr.workflows.email import prepare_email_release, send_email_release
    fixture_run(tmp_path)
    approve_clean_matches(tmp_path, reviewer="test", confirm_count=1)
    _metadata, queue = prepare_email_release(tmp_path, sender="teacher@example.invalid", sheet_only=True)
    review.hold_student(tmp_path, "2024001", reviewer="teacher", reason="Ownership needs another check")
    with pytest.raises(ValueError, match="Review decisions changed"):
        send_email_release(queue, sender="teacher@example.invalid", smtp_host="example.invalid",
                           username="teacher@example.invalid", password="", dry_run=True)


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), 1.01, -.1])
def test_invalid_model_confidence_fails_closed(confidence):
    pages = [page(1, 1), page(2, 2)]
    pages[1]["identity"]["ocr_results"]["BTECH"]["cells"]["confidence"] = confidence
    result = assess_ownership(pages, expected_pages=2)
    assert "2" not in result["assignments"]
