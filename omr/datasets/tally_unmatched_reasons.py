"""Tally human-readable unmatched-page causes from a completed evaluation run."""
from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path


def _reason(flags: list[str], program: str | None) -> str:
    text = " ".join(flags).lower()
    if "duplicate" in text:
        return "duplicate_claim"
    if "orientation marker" in text or "could not validate" in text:
        return "inspection_or_alignment_failure"
    if "program selector" in text:
        return "program_selector_btech" if program == "BTECH" else "program_selector_postgraduate"
    if "conflicting candidates" in text or "conflicts with" in text:
        return "conflicting_identity"
    if "could not be decoded" in text or "did not produce" in text or "unreadable" in text:
        return "unreadable_roll"
    if "low confidence" in text or "whole-strip" in text:
        return "low_resnet_confidence"
    if "no page-1 sheet" in text or "missing page" in text:
        return "missing_counterpart"
    return "other"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    state = json.loads((args.run_dir / "run_state.json").read_text(encoding="utf-8"))
    index = json.loads(Path(state["parse_index_path"]).read_text(encoding="utf-8"))
    preview_path = Path(str(state.get("identity_preview_path") or ""))
    preview = json.loads(preview_path.read_text(encoding="utf-8")) if preview_path.is_file() else {"pages": []}
    preview_by_source = {int(page["source_index"]): page for page in preview.get("pages", [])}
    rows = []
    for page in index.get("unmatched_pages", []) + index.get("page_errors", []):
        flags = [str(flag) for flag in page.get("review_flags", [])]
        source_index = int(page.get("source_index", 0) or 0)
        preview_page = preview_by_source.get(source_index, {})
        program = preview_page.get("program")
        literal_roll = preview_page.get("literal_roll_no")
        rows.append({
            "source_index": source_index,
            "sheet_page": preview_page.get("sheet_page", page.get("page_index", "")),
            "program": program or "",
            "literal_roll": literal_roll or "",
            "roll_read_state": "literal" if literal_roll else "none",
            "duplicate_claim": "yes" if "duplicate" in " ".join(flags).lower() else "no",
            "reason": _reason(flags, program),
            "flags": " | ".join(flags),
        })
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["source_index", "sheet_page", "program", "literal_roll", "roll_read_state", "duplicate_claim", "reason", "flags"])
        writer.writeheader()
        writer.writerows(rows)
    summary = {
        "reasons": Counter(row["reason"] for row in rows),
        "by_page_and_roll_state": Counter((str(row["sheet_page"]), row["roll_read_state"]) for row in rows),
        "duplicates": Counter(row["duplicate_claim"] for row in rows),
    }
    print(json.dumps({key: {str(item): value for item, value in values.items()} for key, values in summary.items()}, indent=2))


if __name__ == "__main__":
    main()
