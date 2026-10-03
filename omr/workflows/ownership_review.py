"""Explicit cached-evidence reassessment and approval, without rerunning OCR."""
from __future__ import annotations

from collections import Counter
import copy
import json
from pathlib import Path
import uuid

from PIL import Image

from omr.workflows.identity_resolution import resolve_identity_proposals, resolution_digest
from omr.workflows.ownership import POLICY_VERSION, assess_ownership, add_order_suggestions
from omr.workflows import review


def _local_file(root: Path, value: str | None, base: Path | None = None) -> Path | None:
    path = review._resolve_path(value, root, base)
    if path is None:
        return None
    path = path.resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Saved ownership artifacts must stay inside the parsed run")
    return path


def saved_ownership_assessment(parsed_dir: str | Path, index: dict | None = None) -> tuple[dict, dict, dict]:
    """Read-only: use original recognition, original quality reports, and roster."""
    root = Path(parsed_dir).resolve()
    parsed = review._load_json(root / "parse_index.json")
    if index is None:
        index = review._load_json(root / review.VERIFIED_INDEX_NAME)
    evidence_path = _local_file(root, parsed.get("identity_resolution_path"))
    if evidence_path is None or not evidence_path.is_file():
        raise ValueError("This run has no saved per-cell identity evidence; inspect identities first")
    original = review._load_json(evidence_path)
    expected_digest = (parsed.get("identity_resolution") or {}).get("digest")
    if expected_digest and resolution_digest(original) != expected_digest:
        raise ValueError("Original recognition evidence changed; reassessment was stopped")
    ledger = {}
    quality = {}
    for entry in [*parsed.get("students", []), *parsed.get("unmatched_pages", [])]:
        details_path = _local_file(root, entry.get("details_path"))
        details = review._load_json(details_path) if details_path and details_path.is_file() else {}
        base = details_path.parent if details_path else root
        for page in details.get("pages", entry.get("pages", [])):
            source = int(page.get("source_index") or 0)
            ledger[source] = review._normalized_student_page(page, root, base)
            report = _local_file(root, page.get("alignment_report_path"), base)
            if report and report.is_file():
                quality[source] = review._load_json(report)
    # Human selections are authoritative for display, but not model training labels.
    for student in index.get("students", []):
        for page in review.selected_student_pages(student):
            ledger[int(page.get("source_index") or 0)] = dict(page)
    pages = [{**page, "quality": quality.get(int(page["source_index"]))}
             for page in original.get("pages", [])]
    reconciliation = index.get("roster_reconciliation") or {}
    roster = [*index.get("students", []), *reconciliation.get("missing_students", [])]
    programs = {str(row["roll_no"]): str(row.get("program") or "") for row in roster}
    valid_rolls = set(programs) if reconciliation.get("roster_total") else None
    assessment = assess_ownership(pages, expected_pages=int(index["expected_pages"]),
                                  valid_rolls=valid_rolls, program_by_roll=programs)
    return assessment, ledger, {"pages": pages, "valid_rolls": valid_rolls,
                                "program_by_roll": programs, "parsed": parsed}


def _human_decided(student: dict) -> bool:
    return bool(student.get("decision_log") or student.get("manual_pages")
                or student.get("status") in {"verified", "approved", "rejected"})


def reassess_saved_ownership(parsed_dir: str | Path, *, reviewer: str, grouping_only: bool) -> tuple[dict, Path]:
    """Only this explicit action changes unreviewed selections; human decisions stay."""
    root = Path(parsed_dir).resolve()
    current, _ = review.load_or_initialize_verified_index(root)
    index = copy.deepcopy(current)
    assessment, ledger, saved = saved_ownership_assessment(root, index)
    protected_rolls = {row["roll_no"] for row in index["students"] if _human_decided(row)}
    protected_sources = {int(page["source_index"]) for row in index["students"]
                         if row["roll_no"] in protected_rolls for page in review.selected_student_pages(row)
                         if page.get("source_index") is not None}
    protected_sources.update(int(row["source_index"]) for row in index.get("unmatched_pages", [])
                             if row.get("decision_log") or row.get("status") == "ignored")
    available = {roll: group for roll, group in assessment["students"].items()
                 if roll not in protected_rolls and not (set(group["source_indices"]) & protected_sources)}
    by_roll = {row["roll_no"]: row for row in index["students"]}
    for roll in available:
        if roll not in by_roll:
            group = available[roll]
            page = next(row for row in saved["pages"] if row["source_index"] == group["source_indices"][0])
            row = review._ensure_review_student(index, roll, page["cell_program"], reviewer, origin="cached_ownership")
            row["decision_log"] = []
            by_roll[roll] = row
    for roll, student in by_roll.items():
        if roll in protected_rolls:
            continue
        group = available.get(roll, {})
        sources = list(group.get("source_indices", []))
        if any(source not in ledger or not ledger[source].get("canonical_image_path")
               or not _local_file(root, ledger[source]["canonical_image_path"]).is_file() for source in sources):
            raise ValueError(f"Canonical pages are missing for {roll}; no selections were changed")
        student.setdefault("observations", list(student.get("review_flags", [])))
        student["pages"] = [dict(ledger[source]) for source in sources]
        student.update(manual_pages=[], sheet_pdf_path=None, verified_sheet_pdf_path=None, eligible_for_email=False)
        student["ownership_decision"] = {"policy_version": POLICY_VERSION, "status": group.get("status", "needs_review"),
                                         "source_indices": sources, "evidence_digest": assessment["digest"]}
        # Legacy answer strings are retained as observations, not ownership decisions.
        # Graded legacy runs without separated answer evidence cannot be bulk-approved.
        answer_flags = student.get("answer_review_flags")
        original_student = next((row for row in saved["parsed"].get("students", []) if row["roll_no"] == roll), {})
        if (answer_flags is None or "answer_review_flags" not in original_student) and not grouping_only:
            answer_flags = ["Legacy grading evidence must be reviewed before approval."]
        student["review_flags"] = [] if grouping_only else list(answer_flags or [])
        if group.get("status") == "auto_matched" and not student["review_flags"]:
            student["status"] = "auto_matched"
        else:
            student["status"] = "needs_review"
            student["review_flags"].append("Missing or uncertain page ownership; inspect Suggested and Unmatched pages.")
    decisions = {int(row["source_index"]): row for row in assessment["pages"]}
    unmatched = {int(row["source_index"]): row for row in index.get("unmatched_pages", [])}
    for source, decision in decisions.items():
        if source in protected_sources:
            continue
        row = unmatched.setdefault(source, {"source_index": source, "decision_log": [], "details_path": None})
        owner = next((roll for roll, group in available.items() if source in group["source_indices"]), None)
        row.update(status="assigned" if owner else "needs_review", assigned_to_roll_no=owner,
                   assigned_page=decision["sheet_page"] if owner else None, page_index=decision["sheet_page"],
                   pages=[dict(ledger[source])] if source in ledger else [],
                   review_flags=[flag["message"] for flag in decision["evidence_flags"] if flag["severity"] == "block"])
    index["unmatched_pages"] = [unmatched[source] for source in sorted(unmatched)]
    assignments = {int(page["source_index"]): row["roll_no"] for row in index["students"]
                   for page in review.selected_student_pages(row) if page.get("source_index")}
    resolution = resolve_identity_proposals(saved["pages"], valid_rolls=saved["valid_rolls"],
                                            program_by_roll=saved["program_by_roll"], current_assignments=assignments)
    parsed = saved["parsed"]
    resolution = add_order_suggestions(resolution, saved["pages"], assignments,
                                      mode=(parsed.get("grouping_order_inference") or {}).get("mode", "identity"),
                                      expected_pages=int(index["expected_pages"]),
                                      total_pages=len(saved["pages"]) + len(parsed.get("page_errors", [])))
    resolution["digest"] = resolution_digest(resolution)
    target = root / "ownership_reviews" / uuid.uuid4().hex
    target.mkdir(parents=True)
    review._write_json(target / "previous_verified_index.json", current)
    review._write_json(target / "assessment.json", assessment)
    review._write_json(target / "identity_resolution.json", resolution)
    index.update(grouping_only=grouping_only, ownership_policy_version=POLICY_VERSION,
                 identity_resolution_path=review._json_path(target / "identity_resolution.json", root),
                 identity_resolution_digest=resolution["digest"])
    index.setdefault("ownership_audit", []).append(review._decision(
        "reassess_saved_ownership", reviewer, "Reused saved evidence; preserved every human decision.",
        assessment_digest=assessment["digest"], protected_rolls=sorted(protected_rolls),
        backup_path=review._json_path(target / "previous_verified_index.json", root)))
    return index, review._write_verified_index(root, index)


def approve_clean_matches(parsed_dir: str | Path, *, reviewer: str, confirm_count: int) -> tuple[dict, Path]:
    """Explicit release approval; never approve suggestions and never send mail."""
    root = Path(parsed_dir).resolve()
    index, _ = review.load_or_initialize_verified_index(root)
    candidates = [row for row in index["students"] if row.get("status") == "auto_matched"]
    if not candidates or len(candidates) != confirm_count:
        raise ValueError("Clean-match count changed; refresh and confirm the current count")
    assessment, _ledger, _saved = saved_ownership_assessment(root, index)
    claims = Counter(int(page["source_index"]) for row in index["students"]
                     for page in review.selected_student_pages(row) if page.get("source_index"))
    for row in candidates:
        group = assessment["students"].get(row["roll_no"], {})
        certificate = row.get("ownership_decision") or {}
        selected = review.selected_student_pages(row)
        sources = [page.get("source_index") for page in selected]
        if (group.get("status") != "auto_matched" or sources != group.get("source_indices")
                or sources != certificate.get("source_indices") or certificate.get("policy_version") != POLICY_VERSION
                or certificate.get("status") != "auto_matched" or row.get("manual_pages")
                or row.get("review_flags") or any(claims[source] != 1 for source in sources)
                or [page.get("page") for page in selected] != list(range(1, int(index["expected_pages"]) + 1))):
            raise ValueError(f"Ownership or selection changed for {row['roll_no']}; reassess before approval")
    target = root / "approved" / uuid.uuid4().hex
    target.mkdir(parents=True)
    # Build the entire batch before committing any approval to the index.
    for row in candidates:
        images = []
        try:
            for page in review.selected_student_pages(row):
                path = _local_file(root, page.get("canonical_image_path"))
                if path is None:
                    raise ValueError("Missing canonical image")
                with Image.open(path) as image:
                    images.append(image.convert("RGB"))
            pdf = target / f"{row['roll_no']}.pdf"
            images[0].save(pdf, save_all=True, append_images=images[1:], resolution=300.)
            row.update(status="approved", verified_sheet_pdf_path=review._json_path(pdf, root))
            row.setdefault("decision_log", []).append(review._decision(
                "approve_clean_match", reviewer, "Explicit batch release approval, not individual manual verification.",
                source_indices=row["source_indices"], assessment_digest=assessment["digest"]))
        finally:
            for image in images:
                image.close()
    return index, review._write_verified_index(root, index)
