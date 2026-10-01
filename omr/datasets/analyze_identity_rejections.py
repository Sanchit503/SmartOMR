"""Summarize why the current batch kept pages unmatched.

This is diagnostic only. It reads stored page artifacts and never changes OCR,
roster data, grouping, or grades.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _text(value: object) -> str:
    return str(value or "").strip()


def _read_payload(identity: dict[str, Any]) -> dict[str, Any]:
    write_in = identity.get("write_in_roll_read")
    return write_in if isinstance(write_in, dict) else identity


def _ocr_values(
    payload: dict[str, Any],
) -> tuple[list[str], list[str], list[float], list[float], list[str]]:
    results = payload.get("ocr_results")
    if not isinstance(results, dict):
        return [], [], [], [], []
    cell_reads: list[str] = []
    strip_reads: list[str] = []
    cell_confidences: list[float] = []
    strip_confidences: list[float] = []
    disagreements: list[str] = []
    for program, result in results.items():
        if str(program).startswith("_") or not isinstance(result, dict):
            continue
        cells = result.get("cells")
        strip = result.get("strip")
        if isinstance(cells, dict):
            text = _text(cells.get("text"))
            if text:
                cell_reads.append(f"{program}:{text}")
            try:
                cell_confidences.append(float(cells.get("confidence")))
            except (TypeError, ValueError):
                pass
        if isinstance(strip, dict):
            text = _text(strip.get("text"))
            if text:
                strip_reads.append(f"{program}:{text}")
            try:
                strip_confidences.append(float(strip.get("confidence")))
            except (TypeError, ValueError):
                pass
        if isinstance(cells, dict) and isinstance(strip, dict):
            cell_text = _text(cells.get("text"))
            strip_text = _text(strip.get("text"))
            if cell_text and strip_text and cell_text != strip_text:
                disagreements.append(str(program))
    return cell_reads, strip_reads, cell_confidences, strip_confidences, disagreements


def _selector_state(flags: list[str]) -> str:
    joined = " | ".join(flags).lower()
    if "continuation program selector is blank" in joined:
        return "blank"
    if "continuation program selector is ambiguous" in joined:
        return "ambiguous"
    if "program selector is blank" in joined:
        return "blank"
    if "program selector is faint/ambiguous" in joined:
        return "ambiguous"
    return "clear_or_unrecorded"


def _reason(flags: list[str], page_index: object, program: str, roll_no: str) -> str:
    joined = " | ".join(flags).lower()
    if "duplicate page-" in joined:
        return "duplicate_claim"
    if "missing page" in joined or "missing counterpart" in joined:
        return "missing_counterpart"
    if "no page-1 sheet has the same roll" in joined:
        return "no_verified_page1_anchor"
    if "does not match the verified page-1 program" in joined:
        return "program_mismatch"
    if "produced conflicting candidates" in joined:
        return "cell_strip_or_field_conflict"
    if "could not be decoded confidently" in joined or "did not produce a valid roll" in joined:
        return "no_literal_roll"
    if "low confidence" in joined or "requires manual confirmation" in joined:
        return "low_identity_confidence"
    if _selector_state(flags) == "blank":
        return "blank_selector_btech" if program == "BTECH" else "blank_selector_postgraduate"
    if not roll_no:
        return "no_literal_roll"
    return f"other_page_{page_index or 'unknown'}"


def analyze(run_dir: Path, out_path: Path) -> Counter[str]:
    root = run_dir / "parsed"
    parse_dirs = list(root.glob("*/unmatched_pages"))
    if len(parse_dirs) != 1:
        raise FileNotFoundError(f"expected one parsed unmatched_pages directory beneath {root}")

    rows: list[dict[str, str]] = []
    counts: Counter[str] = Counter()
    for page_path in sorted(parse_dirs[0].glob("source_*/page.json")):
        payload = json.loads(page_path.read_text(encoding="utf-8"))
        identity = payload.get("identity") if isinstance(payload.get("identity"), dict) else {}
        read = _read_payload(identity)
        flags = [str(flag) for flag in payload.get("review_flags", [])]
        flags.extend(str(flag) for flag in read.get("review_flags", []) if str(flag) not in flags)
        ocr_results = read.get("ocr_results") if isinstance(read.get("ocr_results"), dict) else {}
        suggestion = ocr_results.get("_roster_suggestion") if isinstance(ocr_results.get("_roster_suggestion"), dict) else {}
        cell_reads, strip_reads, cell_confidences, strip_confidences, disagreements = _ocr_values(read)
        roll_no = _text(identity.get("roll_no") or read.get("roll_no"))
        program = _text(identity.get("program") or read.get("program")).upper()
        page_index = payload.get("page_index", "")
        reason = _reason(flags, page_index, program, roll_no)
        counts[reason] += 1
        rows.append(
            {
                "source_index": _text(payload.get("source_index")),
                "sheet_page": _text(page_index),
                "program": program,
                "literal_roll": roll_no,
                "identity_confidence": _text(identity.get("confidence") or read.get("confidence")),
                "selector_state": _selector_state(flags),
                "rejection_reason": reason,
                "cell_reads": " | ".join(cell_reads),
                "cell_min_confidence": f"{min(cell_confidences):.3f}" if cell_confidences else "",
                "strip_reads": " | ".join(strip_reads),
                "strip_max_confidence": f"{max(strip_confidences):.3f}" if strip_confidences else "",
                "same_field_disagreements": " | ".join(disagreements),
                "roster_suggestion": _text(suggestion.get("roll_no")),
                "roster_margin": _text(suggestion.get("margin")),
                "flags": " | ".join(flags),
            }
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        writer.writeheader()
        writer.writerows(rows)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(analyze(args.run_dir, args.out), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
