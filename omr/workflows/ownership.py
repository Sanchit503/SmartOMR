"""Order-independent ownership decisions from immutable recognition evidence."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping

from omr.identity_evidence import (
    BLOCK, WARN, DUPLICATE_AGREED_CLAIM, LOW_CELL_CONFIDENCE,
    NO_ANCHOR_FOR_ROLL, PROGRAM_SELECTOR_AMBIGUOUS, PROGRAM_SELECTOR_BLANK,
    ROLL_NOT_IN_ROSTER, SOURCES_CONTRADICT, evidence, has_blocking_evidence,
    merge_evidence,
)
from omr.workflows.identity_resolution import near_neighbour_guard, page_evidence


POLICY_VERSION = "exact-ownership-v1-2026-10-03"
CELL_MINIMUM = 0.80
FORMAT_PATTERNS = {"BTECH": r"[0-9]{7}", "MTECH": r"MT[0-9]{5}", "PHD": r"PHD[0-9]{5}"}


def valid_cell_roll(page: Mapping[str, Any]) -> str | None:
    roll = str(page.get("cell_roll") or "")
    pattern = FORMAT_PATTERNS.get(str(page.get("cell_program") or ""))
    return roll if pattern and re.fullmatch(pattern, roll) else None


def _selector_flags(page: dict) -> list[dict]:
    state = page.get("selector_state")
    program = page.get("cell_program")
    if state == "clear":
        selected = [name for name, signal in page.get("selector_signals", {}).items()
                    if float(signal.get("fill") or 0) >= .50]
        if selected != [program]:
            return [evidence(SOURCES_CONTRADICT, BLOCK, selector_programs=selected, cell_program=program)]
        return []
    if state == "blank" and program == "BTECH":
        return [evidence(PROGRAM_SELECTOR_BLANK, WARN, program=program)]
    return [evidence(PROGRAM_SELECTOR_AMBIGUOUS if state == "ambiguous" else PROGRAM_SELECTOR_BLANK,
                     BLOCK, program=program, selector_state=state)]


def _quality_flags(page: dict, required: bool) -> list[dict]:
    quality = page.get("quality")
    if not isinstance(quality, dict):
        return [evidence("QUALITY_UNAVAILABLE", BLOCK)] if required else []
    metrics = quality.get("metrics", {})
    bad = [name for name in ("geometry_quality", "local_quality", "image_quality")
           if metrics.get(name, {}).get("status") not in {"ready", "warning"}]
    if bad or quality.get("status") not in {"ready", "aligned"}:
        return [evidence("IDENTITY_ALIGNMENT_HOLD", BLOCK, metrics=bad)]
    return []


def assess_ownership(
    pages: Iterable[Mapping[str, Any]],
    *,
    expected_pages: int,
    valid_rolls: set[str] | None = None,
    program_by_roll: Mapping[str, str] | None = None,
    require_quality: bool = True,
) -> dict:
    """Return decisions, never mutate pages or use their scanner positions.

    Path A is the only automatic identity path. B/C and order-based proposals
    remain in the separate suggestion resolver. Approval is a later operation.
    """
    if expected_pages < 1:
        raise ValueError("expected_pages must be positive")
    normalized = []
    for source in pages:
        page = dict(source) if "cell_roll" in source else page_evidence(source)
        page["quality"] = source.get("quality")
        normalized.append(page)
    programs = dict(program_by_roll or {})
    source_counts = Counter(int(page.get("source_index") or 0) for page in normalized)
    if any(source <= 0 or count != 1 for source, count in source_counts.items()):
        raise ValueError("Ownership evidence must contain unique positive source indices")
    bubble_claims = Counter(page.get("bubble_roll") for page in normalized
                            if page.get("sheet_page") == 1 and page.get("bubble_roll"))
    cell_claims = Counter((valid_cell_roll(page), page.get("sheet_page")) for page in normalized
                          if valid_cell_roll(page))
    decisions: dict[int, dict] = {}
    anchors: dict[str, dict] = {}
    for page in sorted(normalized, key=lambda item: (int(item.get("sheet_page") or 0),
                                                    int(item.get("source_index") or 0))):
        source = int(page.get("source_index") or 0)
        slot = int(page.get("sheet_page") or 0)
        roll = valid_cell_roll(page)
        flags = list(page.get("evidence_flags") or [])
        flags.extend(_quality_flags(page, require_quality))
        if source <= 0 or source_counts[source] != 1 or not 1 <= slot <= expected_pages:
            flags.append(evidence("INVALID_SOURCE_OR_SLOT", BLOCK))
        if roll is None:
            flags.append(evidence("CELL_ROLL_UNREADABLE", BLOCK))
        elif not CELL_MINIMUM <= float(page.get("cell_min_probability") or 0) <= 1.0:
            flags.append(evidence(LOW_CELL_CONFIDENCE, BLOCK,
                                  confidence=page.get("cell_min_probability"), threshold=CELL_MINIMUM))
        if roll and valid_rolls is not None and roll not in valid_rolls:
            flags.append(evidence(ROLL_NOT_IN_ROSTER, BLOCK, roll_no=roll))
        if roll and programs.get(roll) and programs[roll] != page.get("cell_program"):
            flags.append(evidence(SOURCES_CONTRADICT, BLOCK, roster_program=programs[roll]))
        if page.get("program") and page["program"] != page.get("cell_program"):
            flags.append(evidence(SOURCES_CONTRADICT, BLOCK, detected_program=page["program"]))
        flags.extend(_selector_flags(page))
        if roll and cell_claims[(roll, slot)] != 1:
            flags.append(evidence(DUPLICATE_AGREED_CLAIM, BLOCK, roll_no=roll, sheet_page=slot))
        if slot == 1:
            bubble = page.get("bubble_roll")
            if page.get("bubble_program") != page.get("cell_program"):
                flags.append(evidence(SOURCES_CONTRADICT, BLOCK, bubble_program=page.get("bubble_program")))
            if not bubble or bubble != roll:
                flags.append(evidence(SOURCES_CONTRADICT, BLOCK, bubble_roll=bubble, cell_roll=roll))
            if page.get("bubble_confidence") not in {"high", "medium"}:
                flags.append(evidence("BUBBLE_IDENTITY_UNCERTAIN", BLOCK))
            if bubble and bubble_claims[bubble] != 1:
                flags.append(evidence(DUPLICATE_AGREED_CLAIM, BLOCK, roll_no=bubble, sheet_page=1))
        else:
            anchor = anchors.get(roll or "")
            if anchor is None:
                flags.append(evidence(NO_ANCHOR_FOR_ROLL, BLOCK, roll_no=roll))
            elif anchor.get("cell_program") != page.get("cell_program"):
                flags.append(evidence(SOURCES_CONTRADICT, BLOCK, anchor_program=anchor.get("cell_program")))
        if roll:
            rows = page.get("cell_probabilities") or []
            labels = roll.removeprefix("PHD").removeprefix("MT")
            if rows and (len(rows) != len(labels) or any(
                    not math.isfinite(float(row.get(label) or 0))
                    or not CELL_MINIMUM <= float(row.get(label) or 0) <= 1.0
                    for label, row in zip(labels, rows))):
                flags.append(evidence(LOW_CELL_CONFIDENCE, BLOCK, threshold=CELL_MINIMUM))
            sources = [(f"page{slot}_cells", page.get("cell_probabilities") or [])]
            if slot > 1 and roll in anchors:
                sources.append(("page1_cells", anchors[roll].get("cell_probabilities") or []))
            _, neighbour_flags = near_neighbour_guard(
                roll, str(page.get("cell_program") or ""), sources,
                valid_rolls=valid_rolls or set(), program_by_roll=programs, sheet_page=slot,
            )
            flags.extend(neighbour_flags)
        flags = merge_evidence(flags)
        # Only the distinct BTech field may recover a blank selector. Other
        # block codes, including empty fields and conflicting identities, stay.
        if page.get("selector_state") == "blank" and page.get("cell_program") == "BTECH":
            flags = [flag for flag in flags if not (
                flag.get("code") == PROGRAM_SELECTOR_BLANK and flag.get("severity") == BLOCK
            )]
        accepted = roll is not None and not has_blocking_evidence(flags)
        decisions[source] = {
            "source_index": source, "sheet_page": slot, "roll_no": roll,
            "status": "auto_matched" if accepted else "suggested" if roll or page.get("bubble_roll") else "unmatched",
            "association_method": "exact_identity" if accepted else None,
            "evidence_flags": flags, "cell_min_probability": page.get("cell_min_probability"),
        }
        if accepted and slot == 1:
            anchors[roll] = page
    groups: dict[str, list[int]] = defaultdict(list)
    for source, decision in decisions.items():
        if decision["status"] == "auto_matched":
            groups[decision["roll_no"]].append(source)
    students = {}
    for roll, sources in sorted(groups.items()):
        sources.sort(key=lambda source: decisions[source]["sheet_page"])
        slots = {decisions[source]["sheet_page"] for source in sources}
        missing = sorted(set(range(1, expected_pages + 1)) - slots)
        students[roll] = {"roll_no": roll, "source_indices": sources, "missing_pages": missing,
                          "status": "auto_matched" if not missing else "missing_pages"}
    payload = {"policy_version": POLICY_VERSION, "cell_minimum": CELL_MINIMUM,
               "expected_pages": expected_pages, "pages": list(decisions.values()), "students": students,
               "assignments": {str(source): decision["roll_no"] for source, decision in decisions.items()
                               if decision["status"] == "auto_matched"}}
    payload["digest"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return payload


def order_suggestions(pages: list[dict], assignments: Mapping[int, str], *,
                      mode: str, expected_pages: int, total_pages: int) -> list[dict]:
    """Represent scanner adjacency as explicitly blocked proposals, never ownership."""
    if mode not in {"sheet-major", "page-major"} or expected_pages <= 1:
        return []
    if mode == "page-major" and total_pages % expected_pages:
        return []
    by_source = {int(page["source_index"]): dict(page) if "cell_roll" in page else page_evidence(page)
                 for page in pages}
    count = total_pages // expected_pages
    candidates = []
    for anchor in by_source.values():
        if anchor["sheet_page"] != 1 or not anchor.get("bubble_roll"):
            continue
        source = anchor["source_index"]
        for slot in range(2, expected_pages + 1):
            following = source + slot - 1 if mode == "sheet-major" else source + (slot - 1) * count
            continuation = by_source.get(following)
            if not continuation or continuation["sheet_page"] != slot or following in assignments:
                continue
            roll = anchor["bubble_roll"]
            key = f"{POLICY_VERSION}|order|{roll}|{source}|{following}|{slot}"
            candidates.append({
                "candidate_id": hashlib.sha256(key.encode()).hexdigest()[:20], "tier": "suggested",
                "path": "Order", "roll_no": roll, "program": anchor.get("bubble_program"),
                "sheet_page": slot, "page_one_source_index": source,
                "continuation_source_index": following, "source_indices": [source, following],
                "page_one": anchor, "continuation": continuation, "near_neighbours": [],
                "reason_code": "SCANNER_ORDER_ONLY", "blocked": True, "shadow_auto_eligible": False,
                "automatic_attachment_enabled": False, "validation_gate": "human_assignment_required",
                "evidence_flags": [evidence("SCANNER_ORDER_ONLY", BLOCK,
                    "Scanner position suggests this pair but cannot prove ownership.")],
            })
    return candidates


def add_order_suggestions(resolution: dict, pages: list[dict], assignments: Mapping[int, str], *,
                         mode: str, expected_pages: int, total_pages: int) -> dict:
    result = dict(resolution)
    candidates = list(result.get("candidates") or [])
    pairs = {(item["page_one_source_index"], item["continuation_source_index"]) for item in candidates}
    candidates.extend(item for item in order_suggestions(
        pages, assignments, mode=mode, expected_pages=expected_pages, total_pages=total_pages,
    ) if (item["page_one_source_index"], item["continuation_source_index"]) not in pairs)
    result["candidates"] = candidates
    result["counts"] = {**result.get("counts", {}), "suggested_candidates": len(candidates),
                        "blocked_suggestions": sum(bool(item.get("blocked")) for item in candidates)}
    result["ownership_policy"] = {"policy_version": POLICY_VERSION, "path_a_cell_min": CELL_MINIMUM,
                                  "scanner_order_can_attach": False, "automatic_paths": ["A"]}
    return result
