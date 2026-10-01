"""Build a non-mutating evidence-tier report from an identity preview."""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _read(page: dict[str, Any]) -> dict[str, Any]:
    identity = page.get("identity") if isinstance(page.get("identity"), dict) else {}
    value = identity.get("write_in_roll_read")
    return value if isinstance(value, dict) else identity


def _cell_metrics(read: dict[str, Any]) -> tuple[str | None, float, float]:
    program = str(read.get("program") or "").upper()
    results = read.get("ocr_results") if isinstance(read.get("ocr_results"), dict) else {}
    payload = results.get(program) if program else None
    cells = payload.get("cells") if isinstance(payload, dict) else None
    if not isinstance(cells, dict):
        return None, 0.0, 0.0
    roll = str(cells.get("text") or "") or None
    try:
        minimum = float(cells.get("confidence") or 0.0)
    except (TypeError, ValueError):
        minimum = 0.0
    entries = cells.get("raw", {}).get("cells", []) if isinstance(cells.get("raw"), dict) else []
    values = []
    for entry in entries:
        try:
            values.append(float(entry["raw"]["top_probability"]))
        except (KeyError, TypeError, ValueError):
            pass
    geometric_mean = math.exp(sum(math.log(max(value, 1e-6)) for value in values) / len(values)) if values else minimum
    return roll, minimum, geometric_mean


def _blank_selector(read: dict[str, Any]) -> bool:
    flags = read.get("review_flags") if isinstance(read.get("review_flags"), list) else []
    return any("continuation program selector is blank" in str(flag).lower() for flag in flags)


def build(preview_path: Path, output_path: Path) -> dict[str, int]:
    preview = json.loads(preview_path.read_text(encoding="utf-8"))
    pages = [page for page in preview.get("pages", []) if isinstance(page, dict)]
    anchors: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for page in pages:
        if page.get("sheet_page") != 1:
            continue
        identity = page.get("identity") if isinstance(page.get("identity"), dict) else {}
        bubble_roll = str(page.get("literal_roll_no") or "")
        read = _read(page)
        cell_roll, minimum, geometric_mean = _cell_metrics(read)
        if bubble_roll and cell_roll == bubble_roll and minimum >= 0.30 and geometric_mean >= 0.55:
            anchors[bubble_roll].append(page)

    unique_anchors = {roll: claims[0] for roll, claims in anchors.items() if len(claims) == 1}
    rows: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    claimed_slots: Counter[str] = Counter()
    for page in pages:
        source = int(page.get("source_index") or 0)
        page_number = int(page.get("sheet_page") or 0)
        read = _read(page)
        program = str(read.get("program") or page.get("program") or "").upper()
        cell_roll, minimum, geometric_mean = _cell_metrics(read)
        tier = "unmatched"
        reason = "NO_VALID_CELL_ROLL"
        anchor_source = None
        if page_number == 1:
            claims = anchors.get(str(page.get("literal_roll_no") or ""), [])
            if len(claims) == 1 and claims[0] is page:
                tier, reason = "auto_attached", "PATH_A_BUBBLE_PLUS_CELLS"
            elif cell_roll:
                tier, reason = "suggested", "PAGE1_CONTRADICTORY_OR_SINGLE_SOURCE"
        elif cell_roll and minimum >= 0.30:
            anchor = unique_anchors.get(cell_roll)
            btech_blank = _blank_selector(read) and program == "BTECH"
            program_ok = anchor is not None and (
                str(anchor.get("program") or "").upper() == program or btech_blank
            )
            if program_ok:
                claimed_slots[cell_roll] += 1
                tier, reason, anchor_source = "auto_attached", "PAGE2_EXACT_VERIFIED_ANCHOR", anchor.get("source_index")
            else:
                tier, reason = "suggested", "PAGE2_LITERAL_WITHOUT_VERIFIED_ANCHOR"
        counts[tier] += 1
        rows.append({"source_index": source, "sheet_page": page_number, "tier": tier, "reason": reason, "literal_cell_roll": cell_roll or "", "program": program, "cell_min_probability": round(minimum, 6), "cell_geometric_mean": round(geometric_mean, 6), "anchor_source_index": anchor_source or ""})

    for row in rows:
        if row["tier"] == "auto_attached" and row["sheet_page"] > 1 and claimed_slots[row["literal_cell_roll"]] > 1:
            counts["auto_attached"] -= 1
            counts["suggested"] += 1
            row["tier"] = "suggested"
            row["reason"] = "DUPLICATE_PAGE_SLOT"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps({"counts": dict(counts), "pages": rows}, indent=2), encoding="utf-8")
    return dict(counts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.preview, args.out), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
