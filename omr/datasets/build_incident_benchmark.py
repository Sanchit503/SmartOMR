"""Create a manually-labelled incident benchmark from one completed UI run."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np
from PIL import Image

from omr.reader.numerical import read_numerical_responses


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _cells(page: dict) -> list[dict]:
    identity = page.get("identity") or {}
    reads = [identity, identity.get("write_in_roll_read")]
    rows: list[dict] = []
    for read in reads:
        if not isinstance(read, dict):
            continue
        for program, result in (read.get("ocr_results") or {}).items():
            if str(program).startswith("_") or not isinstance(result, dict):
                continue
            cells = ((result.get("cells") or {}).get("raw") or {}).get("cells") or []
            paths = result.get("cell_crop_paths") or []
            for index, cell in enumerate(cells):
                raw = cell.get("raw") or {}
                rows.append({
                    "program": program,
                    "cell_index": index + 1,
                    "crop_path": paths[index] if index < len(paths) else "",
                    "predicted_digit": cell.get("text", ""),
                    "confidence": cell.get("confidence", ""),
                    "margin": raw.get("probability_margin", ""),
                    "enhancement": raw.get("enhancement", ""),
                })
    return rows


def _bubble_rows(source_index: int, details_path: Path) -> list[dict]:
    if not details_path.is_file():
        return []
    details = _read(details_path)
    rows: list[dict] = []
    for response in details.get("mcq_responses", []):
        for option, ratio in (response.get("fill_ratios") or {}).items():
            rows.append({
                "source_index": source_index, "kind": "mcq", "question": response.get("q_no"),
                "column": "", "option": option, "fill_ratio": ratio,
                "ink_density": (response.get("ink_densities") or {}).get(option, ""),
                "enhanced_fill_ratio": (response.get("enhanced_fill_ratios") or {}).get(option, ""),
                "reader_outcome": response.get("outcome", ""), "reader_selected": response.get("selected_option", ""),
                "hand_label": "", "review_note": "",
            })
    for response in details.get("numerical_responses", []):
        for column in response.get("columns", []):
            for option, ratio in (column.get("fill_ratios") or {}).items():
                rows.append({
                    "source_index": source_index, "kind": "numerical", "question": response.get("q_no"),
                    "column": column.get("column", ""), "option": option, "fill_ratio": ratio,
                    "ink_density": (column.get("ink_densities") or {}).get(option, ""),
                    "enhanced_fill_ratio": (column.get("enhanced_fill_ratios") or {}).get(option, ""),
                    "reader_outcome": column.get("outcome", ""), "reader_selected": column.get("selected_digit", ""),
                    "hand_label": "", "review_note": "",
                })
    return rows


def _measured_numerical_bubbles(run_dir: Path, manifest: dict, inspection_by_source: dict[int, dict], source_index: int) -> list[dict]:
    record = inspection_by_source.get(source_index, {})
    aligned = record.get("aligned")
    page_index = record.get("page_index")
    if not aligned or not page_index:
        return []
    path = run_dir / str(aligned)
    if not path.is_file():
        return []
    entries = [entry for entry in manifest.get("numerical_block", []) if entry.get("page") == page_index]
    if not entries:
        return []
    local_manifest = dict(manifest)
    local_manifest["numerical_block"] = entries
    image = np.asarray(Image.open(path).convert("L"))
    readings = read_numerical_responses({page_index: image}, local_manifest, 200.0)
    rows: list[dict] = []
    for reading in readings:
        for column in reading.columns:
            for option, ratio in column["fill_ratios"].items():
                rows.append({
                    "source_index": source_index, "kind": "numerical", "question": reading.q_no,
                    "column": column["column"], "option": option, "fill_ratio": ratio,
                    "ink_density": column["ink_densities"].get(option, ""),
                    "enhanced_fill_ratio": column["enhanced_fill_ratios"].get(option, ""),
                    "reader_outcome": column["outcome"], "reader_selected": column["selected_digit"] or "",
                    "hand_label": "", "review_note": "",
                })
    return rows


def build(run_dir: Path, output_dir: Path, limit: int = 50) -> dict[str, int]:
    state = _read(run_dir / "run_state.json")
    preview = _read(Path(state["identity_preview_path"]))
    parsed = _read(Path(state["parse_index_path"]))
    manifest = _read(Path(state["inputs"]["manifest_path"]))
    inspection = _read(run_dir / "inspection" / "index.json")
    inspection_by_source = {int(page["source_index"]): page for page in inspection.get("pages", [])}
    unmatched = {int(page["source_index"]): page for page in parsed.get("unmatched_pages", [])}
    preview_by_source = {int(page["source_index"]): page for page in preview.get("pages", [])}
    priority = sorted(unmatched) + [index for index, page in preview_by_source.items() if page.get("status") != "detected" and index not in unmatched]
    selected = list(dict.fromkeys(priority))[:limit]
    output_dir.mkdir(parents=True, exist_ok=True)
    page_rows, cell_rows, bubble_rows = [], [], []
    for source_index in selected:
        page = preview_by_source.get(source_index, {})
        unmatched_page = unmatched.get(source_index, {})
        flags = page.get("review_flags") or unmatched_page.get("review_flags") or []
        page_rows.append({
            "source_index": source_index, "sheet_page": page.get("sheet_page", ""),
            "predicted_program": page.get("program", ""), "predicted_roll": page.get("literal_roll_no", ""),
            "confidence": page.get("confidence", ""), "preview_status": page.get("status", ""),
            "review_flags": " | ".join(map(str, flags)), "split": "train" if len(page_rows) < max(0, len(selected) - 10) else "held_out", "true_sheet_page": "", "true_program": "",
            "true_bubbled_roll": "", "true_handwritten_roll": "", "true_owner_roll": "", "review_note": "",
        })
        for cell in _cells(page):
            cell_rows.append({"source_index": source_index, **cell, "true_digit": "", "review_note": ""})
        details_path = Path(str(unmatched_page.get("details_path") or ""))
        bubble_rows.extend(_bubble_rows(source_index, details_path))
        bubble_rows.extend(_measured_numerical_bubbles(run_dir, manifest, inspection_by_source, source_index))

    def write(name: str, rows: list[dict]) -> None:
        fields = list(rows[0]) if rows else []
        with (output_dir / name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    write("page_labels.csv", page_rows)
    write("roll_cell_labels.csv", cell_rows)
    write("bubble_labels.csv", bubble_rows)
    bubble_row_labels: list[dict] = []
    bubble_sources = set(selected[:15])
    for source_index, kind, question, column in sorted({
        (row["source_index"], row["kind"], row["question"], row["column"])
        for row in bubble_rows if row["source_index"] in bubble_sources
    }):
        rows = [row for row in bubble_rows if (row["source_index"], row["kind"], row["question"], row["column"]) == (source_index, kind, question, column)]
        selected_row = next((row for row in rows if row["reader_selected"] == row["option"]), rows[0])
        bubble_row_labels.append({
            "source_index": source_index, "kind": kind, "question": question, "column": column,
            "reader_outcome": selected_row["reader_outcome"], "reader_selected": selected_row["reader_selected"],
            "options": " | ".join(f"{row['option']}: fill={float(row['fill_ratio']):.3f}, ink={float(row['ink_density']):.3f}" for row in rows),
            "hand_label": "", "review_note": "",
        })
    write("bubble_row_labels.csv", bubble_row_labels)

    # Blind sheets prevent the reviewer from being anchored by the model's answer.
    write("page_labels_blind.csv", [{key: row[key] for key in ("source_index", "sheet_page", "split", "true_sheet_page", "true_program", "true_bubbled_roll", "true_handwritten_roll", "true_owner_roll", "review_note")} for row in page_rows])
    write("roll_cell_labels_blind.csv", [{key: row[key] for key in ("source_index", "program", "cell_index", "crop_path", "true_digit", "review_note")} for row in cell_rows])
    write("bubble_row_labels_blind.csv", [{key: row[key] for key in ("source_index", "kind", "question", "column", "hand_label", "review_note")} for row in bubble_row_labels])
    (output_dir / "README.md").write_text(
        "Label from the raw aligned image. Use blind CSVs while labelling; blank hand_label means unlabelled, not blank.\n"
        "There are 40 train and 10 held_out page labels. Label only bubble_row_labels.csv (15 pages), not every bubble_labels.csv row.\n",
        encoding="utf-8",
    )
    return {"pages": len(page_rows), "roll_cells": len(cell_rows), "bubbles": len(bubble_rows), "bubble_rows": len(bubble_row_labels)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    print(build(args.run_dir, args.output_dir, args.limit))


if __name__ == "__main__":
    main()
