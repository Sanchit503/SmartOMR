from __future__ import annotations

from copy import deepcopy

from omr.identity_evidence import (
    DUPLICATE_AGREED_CLAIM,
    NEAR_NEIGHBOUR_HOLD,
    NO_ANCHOR_FOR_ROLL,
    PROGRAM_SELECTOR_BLANK,
    SOURCES_CONTRADICT,
)
from omr.workflows.identity_resolution import resolve_identity_proposals


def _probabilities(roll_no: str, *, weak_last_against: str | None = None) -> list[dict[str, float]]:
    digits = roll_no.removeprefix("PHD").removeprefix("MT")
    rows = []
    for position, digit in enumerate(digits):
        row = {str(value): 0.001 for value in range(10)}
        row[digit] = 0.95
        if weak_last_against is not None and position == len(digits) - 1:
            row[digit] = 0.69
            row[weak_last_against] = 0.31
        rows.append(row)
    return rows


def _ocr_identity(
    roll_no: str,
    *,
    program: str = "BTECH",
    minimum: float = 0.95,
    selector: str | None = None,
    probabilities: list[dict[str, float]] | None = None,
) -> dict:
    cells = [
        {
            "text": digit,
            "confidence": max(row.values()),
            "raw": {
                "top_probability": max(row.values()),
                "class_probabilities": row,
            },
        }
        for digit, row in zip(roll_no.removeprefix("PHD").removeprefix("MT"), probabilities or _probabilities(roll_no))
    ]
    results = {
        program: {
            "cells": {
                "text": roll_no,
                "confidence": minimum,
                "raw": {"cells": cells},
            },
            "strip": {"text": roll_no, "confidence": minimum},
            "cell_crop_paths": [f"cell_{index}.png" for index in range(1, len(cells) + 1)],
        }
    }
    if selector is not None:
        if selector == "clear":
            signals = {
                "BTECH": {"fill": 0.80 if program == "BTECH" else 0.02, "ink": 0.80 if program == "BTECH" else 0.02},
                "MTECH": {"fill": 0.80 if program == "MTECH" else 0.02, "ink": 0.80 if program == "MTECH" else 0.02},
                "PHD": {"fill": 0.80 if program == "PHD" else 0.02, "ink": 0.80 if program == "PHD" else 0.02},
            }
        elif selector == "blank":
            signals = {name: {"fill": 0.02, "ink": 0.02} for name in ("BTECH", "MTECH", "PHD")}
        else:
            signals = {name: {"fill": 0.32, "ink": 0.30} for name in ("BTECH", "MTECH", "PHD")}
        results["_program_selector"] = {"signals": signals}
    return {
        "program": program,
        "roll_no": roll_no,
        "confidence": "high" if minimum >= 0.82 else "medium",
        "ocr_results": results,
        "cell_crop_paths": {program: [f"cell_{index}.png" for index in range(1, len(cells) + 1)]},
        "evidence_flags": [],
    }


def _page_one(
    bubble_roll: str | None,
    cell_roll: str,
    *,
    source: int = 1,
    program: str = "BTECH",
    minimum: float = 0.95,
    probabilities: list[dict[str, float]] | None = None,
) -> dict:
    return {
        "source_index": source,
        "sheet_page": 1,
        "identity_kind": "bubbled" if bubble_roll else "write_in",
        "literal_roll_no": bubble_roll or cell_roll,
        "program": program,
        "confidence": "high",
        "identity": {
            "program": program,
            "roll_no": bubble_roll,
            "confidence": "high",
            "ratios": {
                "program_selector": {
                    program: {"fill": 0.80, "ink": 0.80},
                }
            },
            "evidence_flags": [],
            "write_in_roll_read": _ocr_identity(
                cell_roll,
                program=program,
                minimum=minimum,
                probabilities=probabilities,
            ),
        },
        "page_image_path": f"page_{source}.png",
    }


def _continuation(
    roll_no: str,
    *,
    source: int = 2,
    page: int = 2,
    program: str = "BTECH",
    minimum: float = 0.95,
    selector: str = "clear",
    probabilities: list[dict[str, float]] | None = None,
) -> dict:
    return {
        "source_index": source,
        "sheet_page": page,
        "identity_kind": "handwritten",
        "literal_roll_no": roll_no,
        "program": program,
        "confidence": "high",
        "identity": _ocr_identity(
            roll_no,
            program=program,
            minimum=minimum,
            selector=selector,
            probabilities=probabilities,
        ),
        "page_image_path": f"page_{source}.png",
    }


def _codes(candidate: dict) -> set[str]:
    return {flag["code"] for flag in candidate["evidence_flags"]}


def test_path_b_is_shadow_auto_eligible_but_never_attached():
    roll = "2023478"
    result = resolve_identity_proposals(
        [_continuation(roll), _page_one(roll, roll)],
        valid_rolls={roll},
        program_by_roll={roll: "BTECH"},
        current_assignments={1: roll},
    )
    assert result["counts"]["suggested_candidates"] == 1
    candidate = result["candidates"][0]
    assert candidate["path"] == "B"
    assert candidate["shadow_auto_eligible"] is True
    assert candidate["automatic_attachment_enabled"] is False
    assert candidate["blocked"] is False


def test_near_neighbour_guard_holds_one_digit_collision():
    selected = "2023478"
    neighbour = "2023473"
    result = resolve_identity_proposals(
        [
            _page_one(selected, selected),
            _continuation(selected, probabilities=_probabilities(selected, weak_last_against="3")),
        ],
        valid_rolls={selected, neighbour},
        program_by_roll={selected: "BTECH", neighbour: "BTECH"},
        current_assignments={1: selected},
    )
    candidate = result["candidates"][0]
    assert candidate["blocked"] is True
    assert candidate["shadow_auto_eligible"] is False
    assert NEAR_NEIGHBOUR_HOLD in _codes(candidate)
    assert candidate["near_neighbours"][0]["differing_position"] == 7


def test_near_neighbour_guard_uses_raw_selected_probability_not_pairwise_ratio():
    selected = "2023478"
    neighbour = "2023473"
    probabilities = _probabilities(selected)
    probabilities[-1]["8"] = 0.84
    probabilities[-1]["3"] = 0.01
    result = resolve_identity_proposals(
        [_page_one(selected, selected), _continuation(selected, probabilities=probabilities)],
        valid_rolls={selected, neighbour},
        program_by_roll={selected: "BTECH", neighbour: "BTECH"},
        current_assignments={1: selected},
    )
    candidate = result["candidates"][0]
    neighbour_evidence = candidate["near_neighbours"][0]
    assert neighbour_evidence["minimum_pairwise_support"] > 0.98
    assert neighbour_evidence["minimum_selected_probability"] == 0.84
    assert NEAR_NEIGHBOUR_HOLD in _codes(candidate)


def test_path_b_requires_an_existing_page_one_anchor_for_shadow_eligibility():
    roll = "2023478"
    result = resolve_identity_proposals(
        [_page_one(roll, roll), _continuation(roll)],
        valid_rolls={roll},
        program_by_roll={roll: "BTECH"},
    )
    candidate = result["candidates"][0]
    assert candidate["blocked"] is True
    assert candidate["shadow_auto_eligible"] is False
    assert NO_ANCHOR_FOR_ROLL in _codes(candidate)


def test_path_b_blocks_different_valid_page_one_cell_roll():
    bubble_roll = "2023478"
    cell_roll = "2023473"
    result = resolve_identity_proposals(
        [_page_one(bubble_roll, cell_roll), _continuation(bubble_roll)],
        valid_rolls={bubble_roll, cell_roll},
        program_by_roll={bubble_roll: "BTECH", cell_roll: "BTECH"},
        current_assignments={1: bubble_roll},
    )
    candidate = result["candidates"][0]
    assert candidate["blocked"] is True
    assert SOURCES_CONTRADICT in _codes(candidate)


def test_path_c_remains_suggested_even_with_strong_agreement():
    roll = "2023478"
    result = resolve_identity_proposals(
        [_continuation(roll), _page_one(None, roll)],
        valid_rolls={roll},
        program_by_roll={roll: "BTECH"},
    )
    candidate = result["candidates"][0]
    assert candidate["path"] == "C"
    assert candidate["blocked"] is False
    assert candidate["shadow_auto_eligible"] is False
    assert candidate["automatic_attachment_enabled"] is False


def test_blank_selector_is_reviewable_only_for_btech():
    btech = "2023478"
    result = resolve_identity_proposals(
        [_page_one(btech, btech), _continuation(btech, selector="blank")],
        valid_rolls={btech},
        program_by_roll={btech: "BTECH"},
        current_assignments={1: btech},
    )
    candidate = result["candidates"][0]
    assert PROGRAM_SELECTOR_BLANK in _codes(candidate)
    assert candidate["blocked"] is False

    mtech = "MT25081"
    result = resolve_identity_proposals(
        [
            _page_one(mtech, mtech, program="MTECH"),
            _continuation(mtech, program="MTECH", selector="blank"),
        ],
        valid_rolls={mtech},
        program_by_roll={mtech: "MTECH"},
        current_assignments={1: mtech},
    )
    assert result["candidates"][0]["blocked"] is True


def test_duplicate_continuation_claims_block_every_candidate_order_independently():
    roll = "2023478"
    pages = [_page_one(roll, roll), _continuation(roll, source=2), _continuation(roll, source=7)]
    first = resolve_identity_proposals(
        pages,
        valid_rolls={roll},
        program_by_roll={roll: "BTECH"},
        current_assignments={1: roll},
    )
    second = resolve_identity_proposals(
        list(reversed(deepcopy(pages))),
        valid_rolls={roll},
        program_by_roll={roll: "BTECH"},
        current_assignments={1: roll},
    )
    assert len(first["candidates"]) == 2
    assert all(DUPLICATE_AGREED_CLAIM in _codes(candidate) for candidate in first["candidates"])
    assert [candidate["candidate_id"] for candidate in first["candidates"]] == [
        candidate["candidate_id"] for candidate in second["candidates"]
    ]
