"""Structured identity evidence shared by readers, grouping, and review UI."""
from __future__ import annotations

from typing import Any, Iterable


INFO = "info"
WARN = "warn"
BLOCK = "block"
SEVERITIES = {INFO, WARN, BLOCK}

STRIP_DISAGREES = "STRIP_DISAGREES"
FAINT_BUBBLE = "FAINT_BUBBLE"
LOW_CELL_CONFIDENCE = "LOW_CELL_CONFIDENCE"
EMPTY_FIELD = "EMPTY_FIELD"
PROGRAM_SELECTOR_BLANK = "PROGRAM_SELECTOR_BLANK"
PROGRAM_SELECTOR_AMBIGUOUS = "PROGRAM_SELECTOR_AMBIGUOUS"
PROGRAM_INFERRED_FROM_GRID = "PROGRAM_INFERRED_FROM_GRID"
BUBBLE_ROLL_UNREADABLE = "BUBBLE_ROLL_UNREADABLE"
CELL_ROLL_UNREADABLE = "CELL_ROLL_UNREADABLE"
SOURCES_CONTRADICT = "SOURCES_CONTRADICT"
DUPLICATE_AGREED_CLAIM = "DUPLICATE_AGREED_CLAIM"
NEAR_NEIGHBOUR_HOLD = "NEAR_NEIGHBOUR_HOLD"
NEAR_NEIGHBOUR_EVIDENCE_MISSING = "NEAR_NEIGHBOUR_EVIDENCE_MISSING"
NO_ANCHOR_FOR_ROLL = "NO_ANCHOR_FOR_ROLL"
ANCHOR_SLOT_TAKEN = "ANCHOR_SLOT_TAKEN"
ROLL_NOT_IN_ROSTER = "ROLL_NOT_IN_ROSTER"
PAGE1_MISSING = "PAGE1_MISSING"
ANCHOR_VIA_PAGE2 = "ANCHOR_VIA_PAGE2"
ANCHOR_VIA_CELLS = "ANCHOR_VIA_CELLS"


FRIENDLY_MESSAGES = {
    STRIP_DISAGREES: "Whole-strip OCR disagrees with the individual digit cells.",
    FAINT_BUBBLE: "The bubbled identity was recovered from faint marks.",
    LOW_CELL_CONFIDENCE: "At least one handwritten digit has low model confidence.",
    EMPTY_FIELD: "The handwritten roll-number field appears empty.",
    PROGRAM_SELECTOR_BLANK: "The program selector is blank.",
    PROGRAM_SELECTOR_AMBIGUOUS: "The program selector has competing or faint marks.",
    PROGRAM_INFERRED_FROM_GRID: "The program was inferred from a completed unique grid.",
    BUBBLE_ROLL_UNREADABLE: "The bubbled roll number is incomplete or ambiguous.",
    CELL_ROLL_UNREADABLE: "The handwritten digit cells do not form a complete roll number.",
    SOURCES_CONTRADICT: "Independent identity sources support different roll numbers.",
    DUPLICATE_AGREED_CLAIM: "More than one page claims the same student page slot.",
    NEAR_NEIGHBOUR_HOLD: "A one-digit roster neighbour remains plausible.",
    NEAR_NEIGHBOUR_EVIDENCE_MISSING: "Digit probabilities are unavailable for a nearby roster roll.",
    NO_ANCHOR_FOR_ROLL: "No unique page-one anchor supports this roll number.",
    ANCHOR_SLOT_TAKEN: "The proposed student page slot is already occupied.",
    ROLL_NOT_IN_ROSTER: "The proposed roll number is not present in the roster.",
    PAGE1_MISSING: "The proposal has no page-one identity anchor.",
    ANCHOR_VIA_PAGE2: "Page-one bubbles and continuation-page cells agree.",
    ANCHOR_VIA_CELLS: "Page-one and continuation-page digit cells agree.",
}


def evidence(
    code: str,
    severity: str,
    message: str | None = None,
    **metadata: object,
) -> dict[str, object]:
    if severity not in SEVERITIES:
        raise ValueError(f"unknown identity evidence severity: {severity}")
    item: dict[str, object] = {
        "code": code,
        "severity": severity,
        "message": message or FRIENDLY_MESSAGES.get(code, code.replace("_", " ").title()),
    }
    if metadata:
        item["metadata"] = metadata
    return item


def evidence_codes(items: Iterable[dict[str, Any]] | None) -> set[str]:
    return {
        str(item.get("code") or "")
        for item in (items or [])
        if isinstance(item, dict) and item.get("code")
    }


def has_blocking_evidence(items: Iterable[dict[str, Any]] | None) -> bool:
    return any(
        isinstance(item, dict) and str(item.get("severity") or "") == BLOCK
        for item in (items or [])
    )


def merge_evidence(*groups: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for group in groups:
        for item in group or []:
            if not isinstance(item, dict):
                continue
            key = (
                str(item.get("code") or ""),
                str(item.get("severity") or ""),
                str(item.get("message") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(item))
    return merged


__all__ = [
    "ANCHOR_SLOT_TAKEN",
    "ANCHOR_VIA_CELLS",
    "ANCHOR_VIA_PAGE2",
    "BLOCK",
    "BUBBLE_ROLL_UNREADABLE",
    "CELL_ROLL_UNREADABLE",
    "DUPLICATE_AGREED_CLAIM",
    "EMPTY_FIELD",
    "FAINT_BUBBLE",
    "INFO",
    "LOW_CELL_CONFIDENCE",
    "NEAR_NEIGHBOUR_EVIDENCE_MISSING",
    "NEAR_NEIGHBOUR_HOLD",
    "NO_ANCHOR_FOR_ROLL",
    "PAGE1_MISSING",
    "PROGRAM_INFERRED_FROM_GRID",
    "PROGRAM_SELECTOR_AMBIGUOUS",
    "PROGRAM_SELECTOR_BLANK",
    "ROLL_NOT_IN_ROSTER",
    "SOURCES_CONTRADICT",
    "STRIP_DISAGREES",
    "WARN",
    "evidence",
    "evidence_codes",
    "has_blocking_evidence",
    "merge_evidence",
]
