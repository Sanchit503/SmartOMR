"""Order-independent, non-mutating identity proposal resolver.

The resolver never changes parser grouping. It records Path B/C proposals and
the evidence required for a professor to approve or reject them later.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping

from omr.identity_evidence import (
    ANCHOR_SLOT_TAKEN,
    ANCHOR_VIA_CELLS,
    ANCHOR_VIA_PAGE2,
    BLOCK,
    DUPLICATE_AGREED_CLAIM,
    LOW_CELL_CONFIDENCE,
    NEAR_NEIGHBOUR_EVIDENCE_MISSING,
    NEAR_NEIGHBOUR_HOLD,
    NO_ANCHOR_FOR_ROLL,
    PROGRAM_SELECTOR_AMBIGUOUS,
    PROGRAM_SELECTOR_BLANK,
    ROLL_NOT_IN_ROSTER,
    SOURCES_CONTRADICT,
    WARN,
    evidence,
    merge_evidence,
)


POLICY_VERSION = "identity-resolution-v2-shadow-2026-10-01"
PATH_A_CELL_MIN = 0.30
PATH_A_GEOMETRIC_MEAN = 0.55
PATH_B_CELL_MIN = 0.80
PATH_C_CELL_MIN = 0.65
SUGGESTION_CELL_MIN = 0.30
NEAR_NEIGHBOUR_SELECTED_PROBABILITY = 0.85
# Compatibility alias for callers written against the first shadow-policy draft.
NEAR_NEIGHBOUR_SUPPORT = NEAR_NEIGHBOUR_SELECTED_PROBABILITY


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalized_roll(value: object) -> str | None:
    text = "".join(str(value or "").upper().split())
    return text or None


def _program_for_roll(roll_no: str, program_by_roll: Mapping[str, str]) -> str:
    explicit = str(program_by_roll.get(roll_no) or "").upper()
    if explicit:
        return explicit
    if roll_no.startswith("PHD"):
        return "PHD"
    if roll_no.startswith("MT"):
        return "MTECH"
    return "BTECH"


def _roll_labels(roll_no: str, program: str) -> list[str] | None:
    if program == "BTECH" and len(roll_no) == 7 and roll_no.isdigit():
        return list(roll_no)
    if program == "MTECH" and roll_no.startswith("MT") and len(roll_no) == 7:
        return list(roll_no[2:])
    if program == "PHD" and roll_no.startswith("PHD") and len(roll_no) == 8:
        return list(roll_no[3:])
    return None


def _read_payload(page: Mapping[str, Any]) -> dict[str, Any]:
    identity = page.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    nested = identity.get("write_in_roll_read")
    return nested if isinstance(nested, dict) else identity


def _selector_signals(page: Mapping[str, Any], read: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    identity = page.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    sheet_page = int(page.get("sheet_page") or page.get("page_index") or 0)
    if sheet_page == 1:
        ratios = identity.get("ratios")
        signals = ratios.get("program_selector") if isinstance(ratios, dict) else None
    else:
        results = read.get("ocr_results")
        selector = results.get("_program_selector") if isinstance(results, dict) else None
        signals = selector.get("signals") if isinstance(selector, dict) else None
    if not isinstance(signals, dict):
        return {}
    return {
        str(program).upper(): {
            "fill": _number(signal.get("fill")),
            "ink": _number(signal.get("ink")),
        }
        for program, signal in signals.items()
        if isinstance(signal, dict)
    }


def _selector_state(signals: Mapping[str, Mapping[str, float]]) -> str:
    if not signals:
        return "unavailable"
    filled = [program for program, signal in signals.items() if _number(signal.get("fill")) >= 0.50]
    if len(filled) == 1:
        return "clear"
    if len(filled) > 1:
        return "ambiguous"
    marked = [
        program
        for program, signal in signals.items()
        if _number(signal.get("fill")) >= 0.28 or _number(signal.get("ink")) >= 0.24
    ]
    return "ambiguous" if marked else "blank"


def _cell_data(read: Mapping[str, Any], fallback_program: str | None) -> dict[str, Any]:
    program = str(read.get("program") or fallback_program or "").upper()
    results = read.get("ocr_results")
    results = results if isinstance(results, dict) else {}
    payload = results.get(program) if program else None
    if not isinstance(payload, dict):
        candidates = [
            (str(key).upper(), value)
            for key, value in results.items()
            if not str(key).startswith("_") and isinstance(value, dict)
        ]
        if len(candidates) == 1:
            program, payload = candidates[0]
    payload = payload if isinstance(payload, dict) else {}
    cells = payload.get("cells")
    cells = cells if isinstance(cells, dict) else {}
    raw = cells.get("raw")
    entries = raw.get("cells") if isinstance(raw, dict) else None
    probability_rows: list[dict[str, float]] = []
    top_probabilities: list[float] = []
    if isinstance(entries, list):
        for entry in entries:
            entry_raw = entry.get("raw") if isinstance(entry, dict) else None
            if not isinstance(entry_raw, dict):
                probability_rows = []
                break
            probabilities = entry_raw.get("class_probabilities")
            if not isinstance(probabilities, dict):
                probability_rows = []
                break
            probability_rows.append({str(label): _number(value) for label, value in probabilities.items()})
            top_probabilities.append(_number(entry_raw.get("top_probability")))
    geometric_mean = (
        math.exp(sum(math.log(max(value, 1e-6)) for value in top_probabilities) / len(top_probabilities))
        if top_probabilities
        else _number(cells.get("confidence"))
    )
    crop_paths = payload.get("cell_crop_paths")
    if not isinstance(crop_paths, list):
        all_paths = read.get("cell_crop_paths")
        crop_paths = all_paths.get(program, []) if isinstance(all_paths, dict) else []
    strip = payload.get("strip")
    strip = strip if isinstance(strip, dict) else {}
    cell_roll = _normalized_roll(cells.get("text"))
    if cell_roll and len(cell_roll) == 5 and cell_roll.isascii() and cell_roll.isdigit():
        cell_roll = {"MTECH": "MT", "PHD": "PHD"}.get(program, "") + cell_roll
    return {
        "program": program or None,
        "roll_no": cell_roll,
        "minimum_probability": round(_number(cells.get("confidence")), 6),
        "geometric_mean": round(geometric_mean, 6),
        "probabilities": probability_rows,
        "cell_crop_paths": [str(path) for path in crop_paths],
        "strip_roll": _normalized_roll(strip.get("text")),
    }


def page_evidence(page: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a parser/preview page into resolver input."""
    identity = page.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    read = _read_payload(page)
    sheet_page = int(page.get("sheet_page") or page.get("page_index") or 0)
    identity_kind = str(page.get("identity_kind") or "")
    literal_roll = _normalized_roll(page.get("literal_roll_no") or page.get("roll_no"))
    program = str(page.get("program") or read.get("program") or identity.get("program") or "").upper() or None
    cells = _cell_data(read, program)
    selector_signals = _selector_signals(page, read)
    bubble_roll = literal_roll if sheet_page == 1 and identity_kind == "bubbled" else None
    bubble_program = program if bubble_roll else None
    evidence_flags = merge_evidence(
        identity.get("evidence_flags") if isinstance(identity.get("evidence_flags"), list) else [],
        read.get("evidence_flags") if isinstance(read.get("evidence_flags"), list) else [],
    )
    return {
        "source_index": int(page.get("source_index") or 0),
        "sheet_page": sheet_page,
        "identity_kind": identity_kind,
        "program": program,
        "bubble_roll": bubble_roll,
        "bubble_program": bubble_program,
        "bubble_confidence": str(page.get("confidence") or identity.get("confidence") or "low"),
        "cell_roll": cells["roll_no"],
        "cell_program": cells["program"],
        "cell_min_probability": cells["minimum_probability"],
        "cell_geometric_mean": cells["geometric_mean"],
        "cell_probabilities": cells["probabilities"],
        "cell_crop_paths": cells["cell_crop_paths"],
        "strip_roll": cells["strip_roll"],
        "selector_state": _selector_state(selector_signals),
        "selector_signals": selector_signals,
        "evidence_flags": evidence_flags,
        "page_image_path": str(page.get("page_image_path") or ""),
        "current_roll": _normalized_roll(page.get("current_roll")),
    }


def _blocking(items: Iterable[Mapping[str, Any]]) -> bool:
    return any(str(item.get("severity") or "") == BLOCK for item in items)


def _near_neighbours(
    roll_no: str,
    program: str,
    valid_rolls: set[str],
    program_by_roll: Mapping[str, str],
) -> list[tuple[str, int]]:
    labels = _roll_labels(roll_no, program)
    if labels is None:
        return []
    neighbours: list[tuple[str, int]] = []
    for other in valid_rolls:
        if other == roll_no or _program_for_roll(other, program_by_roll) != program:
            continue
        other_labels = _roll_labels(other, program)
        if other_labels is None or len(other_labels) != len(labels):
            continue
        differences = [index for index, pair in enumerate(zip(labels, other_labels)) if pair[0] != pair[1]]
        if len(differences) == 1:
            neighbours.append((other, differences[0]))
    return sorted(neighbours)


def _near_neighbour_guard(
    roll_no: str,
    program: str,
    probability_sources: list[tuple[str, list[dict[str, float]]]],
    *,
    valid_rolls: set[str],
    program_by_roll: Mapping[str, str],
    sheet_page: int,
    occupied_slots: Mapping[tuple[str, int], set[int]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    labels = _roll_labels(roll_no, program)
    rows: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []
    if labels is None:
        return rows, [evidence(NEAR_NEIGHBOUR_EVIDENCE_MISSING, BLOCK, roll_no=roll_no)]
    for neighbour, position in _near_neighbours(roll_no, program, valid_rolls, program_by_roll):
        neighbour_labels = _roll_labels(neighbour, program)
        assert neighbour_labels is not None
        source_support: list[dict[str, Any]] = []
        missing = False
        for source_name, probabilities in probability_sources:
            if position >= len(probabilities):
                missing = True
                continue
            row = probabilities[position]
            selected_probability = _number(row.get(labels[position]))
            neighbour_probability = _number(row.get(neighbour_labels[position]))
            denominator = selected_probability + neighbour_probability
            if denominator <= 0:
                missing = True
                continue
            source_support.append(
                {
                    "source": source_name,
                    "selected_probability": round(selected_probability, 6),
                    "neighbour_probability": round(neighbour_probability, 6),
                    "pairwise_support": round(selected_probability / denominator, 6),
                }
            )
        minimum_support = min((row["pairwise_support"] for row in source_support), default=0.0)
        minimum_selected_probability = min(
            (row["selected_probability"] for row in source_support),
            default=0.0,
        )
        open_slot = not occupied_slots.get((neighbour, sheet_page))
        rows.append(
            {
                "roll_no": neighbour,
                "differing_position": position + 1,
                "selected_digit": labels[position],
                "neighbour_digit": neighbour_labels[position],
                "open_slot": open_slot,
                "minimum_selected_probability": round(minimum_selected_probability, 6),
                "minimum_pairwise_support": round(minimum_support, 6),
                "sources": source_support,
            }
        )
        if missing:
            flags.append(
                evidence(
                    NEAR_NEIGHBOUR_EVIDENCE_MISSING,
                    BLOCK,
                    roll_no=roll_no,
                    neighbour_roll=neighbour,
                    differing_position=position + 1,
                )
            )
        elif minimum_selected_probability < NEAR_NEIGHBOUR_SELECTED_PROBABILITY:
            flags.append(
                evidence(
                    NEAR_NEIGHBOUR_HOLD,
                    BLOCK,
                    roll_no=roll_no,
                    neighbour_roll=neighbour,
                    differing_position=position + 1,
                    selected_probability=round(minimum_selected_probability, 6),
                    pairwise_support=round(minimum_support, 6),
                    threshold=NEAR_NEIGHBOUR_SELECTED_PROBABILITY,
                    open_slot=open_slot,
                )
            )
    return rows, flags


def near_neighbour_guard(
    roll_no: str,
    program: str,
    probability_sources: list[tuple[str, list[dict[str, float]]]],
    *,
    valid_rolls: set[str],
    program_by_roll: Mapping[str, str] | None = None,
    sheet_page: int,
    occupied_slots: Mapping[tuple[str, int], set[int]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply the production one-digit roster collision guard.

    The public wrapper keeps automatic Path A and shadow Path B/C on the same
    probability threshold and evidence schema.
    """
    return _near_neighbour_guard(
        roll_no,
        program,
        probability_sources,
        valid_rolls=valid_rolls,
        program_by_roll=program_by_roll or {},
        sheet_page=sheet_page,
        occupied_slots=occupied_slots or {},
    )


def _candidate_id(path: str, roll_no: str, page_one: int, continuation: int, sheet_page: int) -> str:
    value = f"{POLICY_VERSION}|{path}|{roll_no}|{page_one}|{continuation}|{sheet_page}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _candidate(
    *,
    path: str,
    roll_no: str,
    program: str,
    page_one: dict[str, Any],
    continuation: dict[str, Any],
    valid_rolls: set[str],
    program_by_roll: Mapping[str, str],
    occupied_slots: Mapping[tuple[str, int], set[int]],
    page_one_claim_count: int,
    continuation_claim_count: int,
) -> dict[str, Any]:
    flags: list[dict[str, Any]] = []
    sheet_page = int(continuation["sheet_page"])
    if roll_no not in valid_rolls:
        flags.append(evidence(ROLL_NOT_IN_ROSTER, BLOCK, roll_no=roll_no))
    if page_one_claim_count != 1 or continuation_claim_count != 1:
        flags.append(
            evidence(
                DUPLICATE_AGREED_CLAIM,
                BLOCK,
                roll_no=roll_no,
                page_one_claims=page_one_claim_count,
                continuation_claims=continuation_claim_count,
                sheet_page=sheet_page,
            )
        )
    occupied = occupied_slots.get((roll_no, sheet_page), set())
    if occupied and continuation["source_index"] not in occupied:
        flags.append(
            evidence(
                ANCHOR_SLOT_TAKEN,
                BLOCK,
                roll_no=roll_no,
                sheet_page=sheet_page,
                occupied_source_indices=sorted(occupied),
            )
        )

    probability_sources: list[tuple[str, list[dict[str, float]]]] = []
    if path == "B":
        flags.append(evidence(ANCHOR_VIA_PAGE2, WARN, roll_no=roll_no))
        probability_sources.append(("page2_cells", continuation["cell_probabilities"]))
        if page_one.get("current_roll") != roll_no:
            flags.append(
                evidence(
                    NO_ANCHOR_FOR_ROLL,
                    BLOCK,
                    roll_no=roll_no,
                    page_one_current_roll=page_one.get("current_roll"),
                )
            )
        page_one_cell = page_one.get("cell_roll")
        if page_one_cell and page_one_cell in valid_rolls and page_one_cell != roll_no:
            flags.append(
                evidence(
                    SOURCES_CONTRADICT,
                    BLOCK,
                    bubble_roll=roll_no,
                    page1_cell_roll=page_one_cell,
                )
            )
        if float(continuation["cell_min_probability"]) < PATH_B_CELL_MIN:
            flags.append(
                evidence(
                    LOW_CELL_CONFIDENCE,
                    WARN,
                    source="page2_cells",
                    confidence=continuation["cell_min_probability"],
                    threshold=PATH_B_CELL_MIN,
                )
            )
    else:
        flags.append(evidence(ANCHOR_VIA_CELLS, WARN, roll_no=roll_no))
        probability_sources.extend(
            [
                ("page1_cells", page_one["cell_probabilities"]),
                ("page2_cells", continuation["cell_probabilities"]),
            ]
        )
        if min(float(page_one["cell_min_probability"]), float(continuation["cell_min_probability"])) < PATH_C_CELL_MIN:
            flags.append(
                evidence(
                    LOW_CELL_CONFIDENCE,
                    WARN,
                    source="page1_and_page2_cells",
                    page1_confidence=page_one["cell_min_probability"],
                    page2_confidence=continuation["cell_min_probability"],
                    threshold=PATH_C_CELL_MIN,
                )
            )
        bubble_roll = page_one.get("bubble_roll")
        if bubble_roll and bubble_roll != roll_no:
            flags.append(
                evidence(
                    SOURCES_CONTRADICT,
                    BLOCK,
                    bubble_roll=bubble_roll,
                    agreed_cell_roll=roll_no,
                )
            )

    selector_state = continuation.get("selector_state")
    if selector_state == "blank":
        severity = WARN if program == "BTECH" else BLOCK
        flags.append(evidence(PROGRAM_SELECTOR_BLANK, severity, program=program, source_index=continuation["source_index"]))
    elif selector_state == "ambiguous":
        flags.append(evidence(PROGRAM_SELECTOR_AMBIGUOUS, BLOCK, program=program, source_index=continuation["source_index"]))
    elif continuation.get("cell_program") and continuation.get("cell_program") != program:
        flags.append(
            evidence(
                SOURCES_CONTRADICT,
                BLOCK,
                anchor_program=program,
                continuation_program=continuation.get("cell_program"),
            )
        )

    neighbours, neighbour_flags = _near_neighbour_guard(
        roll_no,
        program,
        probability_sources,
        valid_rolls=valid_rolls,
        program_by_roll=program_by_roll,
        sheet_page=sheet_page,
        occupied_slots=occupied_slots,
    )
    flags = merge_evidence(flags, neighbour_flags)
    threshold_eligible = (
        path == "B"
        and float(continuation["cell_min_probability"]) >= PATH_B_CELL_MIN
        and not _blocking(flags)
    )
    return {
        "candidate_id": _candidate_id(path, roll_no, page_one["source_index"], continuation["source_index"], sheet_page),
        "tier": "suggested",
        "path": path,
        "reason_code": ANCHOR_VIA_PAGE2 if path == "B" else ANCHOR_VIA_CELLS,
        "roll_no": roll_no,
        "program": program,
        "sheet_page": sheet_page,
        "page_one_source_index": page_one["source_index"],
        "continuation_source_index": continuation["source_index"],
        "source_indices": [page_one["source_index"], continuation["source_index"]],
        "page_one": page_one,
        "continuation": continuation,
        "near_neighbours": neighbours,
        "evidence_flags": flags,
        "blocked": _blocking(flags),
        "shadow_auto_eligible": threshold_eligible,
        "automatic_attachment_enabled": False,
        "validation_gate": "disabled_pending_blind_labels",
    }


def resolve_identity_proposals(
    pages: Iterable[Mapping[str, Any]],
    *,
    valid_rolls: set[str] | None,
    program_by_roll: Mapping[str, str] | None = None,
    current_assignments: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    """Return Path B/C suggestions without modifying any assignment."""
    normalized = [dict(page) if "cell_roll" in page else page_evidence(page) for page in pages]
    roster = set(valid_rolls or set())
    programs = dict(program_by_roll or {})
    assignments = {int(source): str(roll) for source, roll in (current_assignments or {}).items()}
    for page in normalized:
        page["current_roll"] = assignments.get(page["source_index"], page.get("current_roll"))

    occupied_slots: dict[tuple[str, int], set[int]] = defaultdict(set)
    for page in normalized:
        current_roll = page.get("current_roll")
        if current_roll:
            occupied_slots[(str(current_roll), int(page["sheet_page"]))].add(int(page["source_index"]))

    page_ones = [page for page in normalized if page["sheet_page"] == 1]
    continuations = [page for page in normalized if page["sheet_page"] > 1]
    bubble_claims = Counter(page["bubble_roll"] for page in page_ones if page.get("bubble_roll"))
    cell_page_one_claims = Counter(page["cell_roll"] for page in page_ones if page.get("cell_roll"))
    continuation_claims = Counter(
        (page["cell_roll"], page["sheet_page"])
        for page in continuations
        if page.get("cell_roll")
    )

    proposed: dict[tuple[str, int, int], dict[str, Any]] = {}
    for page_one in page_ones:
        bubble_roll = page_one.get("bubble_roll")
        if bubble_roll:
            program = _program_for_roll(str(bubble_roll), programs)
            for continuation in continuations:
                if continuation.get("cell_roll") != bubble_roll:
                    continue
                if continuation.get("current_roll") == bubble_roll:
                    continue
                if float(continuation.get("cell_min_probability") or 0.0) < SUGGESTION_CELL_MIN:
                    continue
                candidate = _candidate(
                    path="B",
                    roll_no=str(bubble_roll),
                    program=program,
                    page_one=page_one,
                    continuation=continuation,
                    valid_rolls=roster,
                    program_by_roll=programs,
                    occupied_slots=occupied_slots,
                    page_one_claim_count=bubble_claims[str(bubble_roll)],
                    continuation_claim_count=continuation_claims[(str(bubble_roll), continuation["sheet_page"])],
                )
                proposed[(str(bubble_roll), page_one["source_index"], continuation["source_index"])] = candidate

        cell_roll = page_one.get("cell_roll")
        if not cell_roll or float(page_one.get("cell_min_probability") or 0.0) < SUGGESTION_CELL_MIN:
            continue
        if page_one.get("bubble_roll") == cell_roll:
            continue
        program = _program_for_roll(str(cell_roll), programs)
        for continuation in continuations:
            if continuation.get("cell_roll") != cell_roll:
                continue
            if continuation.get("current_roll") == cell_roll:
                continue
            if float(continuation.get("cell_min_probability") or 0.0) < SUGGESTION_CELL_MIN:
                continue
            key = (str(cell_roll), page_one["source_index"], continuation["source_index"])
            if key in proposed:
                continue
            proposed[key] = _candidate(
                path="C",
                roll_no=str(cell_roll),
                program=program,
                page_one=page_one,
                continuation=continuation,
                valid_rolls=roster,
                program_by_roll=programs,
                occupied_slots=occupied_slots,
                page_one_claim_count=cell_page_one_claims[str(cell_roll)],
                continuation_claim_count=continuation_claims[(str(cell_roll), continuation["sheet_page"])],
            )

    candidates = sorted(
        proposed.values(),
        key=lambda item: (
            item["blocked"],
            not item["shadow_auto_eligible"],
            item["roll_no"],
            item["sheet_page"],
            item["continuation_source_index"],
        ),
    )
    suggested_sources = {source for candidate in candidates for source in candidate["source_indices"]}
    current_sources = set(assignments)
    counts = {
        "pages": len(normalized),
        "current_attached_pages": len(current_sources),
        "suggested_candidates": len(candidates),
        "shadow_auto_eligible": sum(bool(candidate["shadow_auto_eligible"]) for candidate in candidates),
        "blocked_suggestions": sum(bool(candidate["blocked"]) for candidate in candidates),
        "unmatched_without_suggestion": sum(
            page["source_index"] not in current_sources and page["source_index"] not in suggested_sources
            for page in normalized
        ),
    }
    return {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "mode": "shadow_only",
        "automatic_paths_enabled": ["A"],
        "automatic_paths_disabled": ["B", "C"],
        "thresholds": {
            "path_a_cell_min": PATH_A_CELL_MIN,
            "path_a_geometric_mean": PATH_A_GEOMETRIC_MEAN,
            "path_b_cell_min": PATH_B_CELL_MIN,
            "path_c_cell_min": PATH_C_CELL_MIN,
            "suggestion_cell_min": SUGGESTION_CELL_MIN,
            "near_neighbour_selected_probability": NEAR_NEIGHBOUR_SELECTED_PROBABILITY,
        },
        "counts": counts,
        "pages": normalized,
        "candidates": candidates,
    }


def resolution_digest(payload: Mapping[str, Any]) -> str:
    without_digest = {key: value for key, value in payload.items() if key != "digest"}
    canonical = json.dumps(without_digest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "NEAR_NEIGHBOUR_SUPPORT",
    "NEAR_NEIGHBOUR_SELECTED_PROBABILITY",
    "PATH_B_CELL_MIN",
    "PATH_C_CELL_MIN",
    "POLICY_VERSION",
    "near_neighbour_guard",
    "page_evidence",
    "resolve_identity_proposals",
    "resolution_digest",
]
