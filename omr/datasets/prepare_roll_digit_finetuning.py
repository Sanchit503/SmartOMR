"""Prepare and finalize visually labelled cells from multiple OMR exams."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import random
import shutil

from PIL import Image, ImageDraw, ImageOps

from omr.workflows.identity_resolution import page_evidence


LABELS = set("0123456789") | {"blank"}


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _csv_rows(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_rows(path: Path, rows: list[dict], fields: list[str]):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _sha256(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve(path_value: str, base: Path):
    path = Path(path_value)
    if path.is_absolute() or path.is_file():
        return path
    return base / path


def prepare(quiz_dataset: Path, run_dirs: list[Path], output: Path, *, extra_fields: list[str] | None = None):
    overrides = defaultdict(set)
    for value in extra_fields or []:
        source, program = value.split(":", 1)
        if program not in ("BTECH", "MTECH", "PHD"):
            raise ValueError(f"Unknown program in extra field: {value}")
        overrides[int(source)].add(program)
    output.mkdir(parents=True, exist_ok=True)
    fields = []
    problems = []
    groups = defaultdict(list)
    for row in _csv_rows(quiz_dataset / "labels.csv"):
        groups[(row["roll_no"], row["page"])].append(row)
    for index, ((roll, page), rows) in enumerate(sorted(groups.items()), 1):
        rows.sort(key=lambda row: int(row["cell_index"]))
        fields.append({"field_id": f"Q{index:04d}", "exam": "quiz1", "source_index": index,
                       "sheet_page": int(page), "program": rows[0]["program"], "prior_writer": roll,
                       "prior_split": rows[0]["split"], "cell_paths": [str(quiz_dataset / row["image_path"]) for row in rows],
                       "source_details_path": rows[0]["source_details_path"],
                       "source_image_path": rows[0]["source_image_path"]})
    for exam_number, run in enumerate(run_dirs, 1):
        preview_path = run / "identity_preview/index.json"
        preview = _read_json(preview_path)
        for original in preview["pages"]:
            page = page_evidence(original)
            source = page["source_index"]
            slot = page["sheet_page"]
            if slot not in (1, 2):
                problems.append({"exam": run.name, "source": source, "reason": "No inspected/aligned template page"})
                continue
            folder = run / "identity_preview" / f"source_{source:04d}"
            banks = {"BTECH": sorted(folder.glob(f"page_{slot}_btech_roll_cell_*.png")),
                     "MTECH": sorted(folder.glob(f"page_{slot}_mtech_phd_roll_cell_*.png"))}
            program = page.get("cell_program") or page.get("program")
            selected = [program] if program in ("BTECH", "MTECH", "PHD") else ["BTECH", "MTECH"]
            selected = sorted(set(selected) | overrides[source])
            for bank in selected:
                paths = banks["MTECH" if bank == "PHD" else bank]
                expected = 7 if bank == "BTECH" else 5
                if len(paths) != expected:
                    problems.append({"exam": run.name, "source": source, "reason": f"Missing cached {bank} cells"})
                    continue
                fields.append({"field_id": f"M{exam_number}_{source:04d}_{bank[0]}", "exam": run.name,
                               "source_index": source, "sheet_page": slot, "program": bank,
                               "prior_writer": None, "prior_split": None,
                               "cell_paths": list(map(str, paths)), "source_image_path": "",
                               "source_details_path": str(folder / "evidence.json")})
    raw_dir = output / "raw_cells"
    raw_dir.mkdir(exist_ok=True)
    for field in fields:
        field["original_cell_paths"] = field["cell_paths"]
        copied = []
        hashes = []
        for position, value in enumerate(field["original_cell_paths"], 1):
            source = _resolve(value, Path.cwd())
            destination = raw_dir / f"{field['field_id']}__c{position}.png"
            if source.resolve() != destination.resolve():
                shutil.copy2(source, destination)
            copied.append(destination.relative_to(output).as_posix())
            hashes.append(_sha256(destination))
        field["cell_paths"] = copied
        field["cell_sha256"] = hashes
    (output / "fields.json").write_text(json.dumps(fields, indent=2), encoding="utf-8")
    (output / "extraction_problems.json").write_text(json.dumps(problems, indent=2), encoding="utf-8")
    audit = output / "blind_contact_sheets"
    audit.mkdir(exist_ok=True)
    for start in range(0, len(fields), 16):
        batch = fields[start:start + 16]
        canvas = Image.new("RGB", (1320, 1160), "white")
        draw = ImageDraw.Draw(canvas)
        for index, field in enumerate(batch):
            x, y = (index % 2) * 660, (index // 2) * 145
            draw.text((x + 8, y + 4), f"{field['field_id']} | {len(field['cell_paths'])} cells | page {field['sheet_page']}", fill="black")
            for position, path in enumerate(field["cell_paths"]):
                with Image.open(output / path) as image:
                    crop = ImageOps.contain(image.convert("RGB"), (84, 108))
                canvas.paste(crop, (x + 8 + position * 91, y + 25))
        canvas.save(audit / f"sheet_{start // 16 + 1:02d}.png")
    summary = {"fields": len(fields), "crops": sum(len(field["cell_paths"]) for field in fields),
               "exams": dict(Counter(field["exam"] for field in fields)), "extraction_problems": problems}
    (output / "preparation.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def finalize(output: Path, annotations_path: Path, *, seed: int = 557, val_fraction: float = .2):
    if not 0 < val_fraction < 1:
        raise ValueError("val_fraction must be between zero and one")
    fields = _read_json(output / "fields.json")
    annotations = _read_json(annotations_path)
    known_ids = {field["field_id"] for field in fields}
    if set(annotations) - known_ids:
        raise ValueError("Annotations contain unknown field IDs")
    prior_splits = {}
    for field in fields:
        if field["prior_writer"]:
            writer = field["prior_writer"].upper()
            if writer in prior_splits and prior_splits[writer] != field["prior_split"]:
                raise ValueError(f"Prior student occurs in both splits: {writer}")
            prior_splits[writer] = field["prior_split"]
    # A miswritten literal is an alias for splitting only, never a digit label.
    aliases = {}
    for field in fields:
        annotation = annotations.get(field["field_id"], {})
        text = annotation.get("transcription", "").replace(" ", "")
        if field["prior_writer"] and text and set(text) <= set("0123456789"):
            literal = {"MTECH": "MT", "PHD": "PHD"}.get(field["program"], "") + text
            split = field["prior_split"]
            if literal in prior_splits and prior_splits[literal] != split:
                raise ValueError(f"Literal/writer split conflict requires review: {literal}")
            prior_splits[literal] = split
            if literal != field["prior_writer"]:
                aliases[literal] = field["prior_writer"]
    pending = []
    rows = []
    for field in fields:
        annotation = annotations.get(field["field_id"])
        if not annotation:
            pending.append({"field_id": field["field_id"], "cell_index": "", "reason": "Not visually reviewed"})
            continue
        text = annotation["transcription"].replace(" ", "")
        if len(text) != len(field["cell_paths"]) or set(text) - set("0123456789_?"):
            raise ValueError(f"Invalid transcription for {field['field_id']}: {text!r}")
        if not annotation.get("reviewer"):
            raise ValueError(f"Reviewer missing for {field['field_id']}")
        writer = str(annotation.get("writer_id") or field["prior_writer"] or "").upper()
        if not writer and "?" not in text and "_" not in text:
            writer = {"MTECH": "MT", "PHD": "PHD"}.get(field["program"], "") + text
        if not writer and set(text) <= {"_"}:
            writer = f"BLANK:{field['exam']}:{field['source_index']}"
        for position, (label, image_path, expected_hash) in enumerate(zip(text, field["cell_paths"], field["cell_sha256"]), 1):
            reason = (annotation.get("note") or "Unreadable digit") if label == "?" else "Writer identity requires review" if not writer else None
            if reason:
                pending.append({"field_id": field["field_id"], "cell_index": position, "reason": reason,
                                "image_path": image_path, "transcription": text})
                continue
            if _sha256(output / image_path) != expected_hash:
                raise ValueError(f"Crop changed after extraction: {image_path}")
            rows.append({"image_path": image_path, "label": "blank" if label == "_" else label,
                         "roll_no": writer, "writer_id": writer, "exam": field["exam"],
                         "source_index": field["source_index"], "page": field["sheet_page"],
                         "program": field["program"], "cell_index": position, "field_id": field["field_id"],
                         "reviewer": annotation["reviewer"], "label_source": "visual_transcription",
                         "crop_sha256": expected_hash, "source_image_path": field["source_image_path"],
                         "source_details_path": field["source_details_path"], "note": annotation.get("note", "")})
    # Old training writers must never become validation writers for resumed models.
    new_writers = sorted({row["writer_id"] for row in rows} - set(prior_splits))
    random.Random(seed).shuffle(new_writers)
    new_val = set(new_writers[:max(1, round(len(new_writers) * val_fraction))]) if new_writers else set()
    splits = dict(prior_splits)
    splits.update({writer: "val" if writer in new_val else "train" for writer in new_writers})
    seen = {}
    duplicate_count = 0
    unique_rows = []
    for row in rows:
        row["split"] = splits[row["writer_id"]]
        key = row["crop_sha256"]
        previous = seen.get(key)
        if previous:
            duplicate_count += 1
            if previous["label"] != row["label"]:
                raise ValueError(f"Identical crops have conflicting labels: {row['image_path']}")
            if previous["split"] != row["split"]:
                pending.append({"field_id": row["field_id"], "cell_index": row["cell_index"],
                                "reason": "Exact duplicate already in other split", "image_path": row["image_path"]})
            continue
        seen[key] = row
        unique_rows.append(row)
    rows = unique_rows
    if not rows or not any(row["split"] == "train" for row in rows) or not any(row["split"] == "val" for row in rows):
        raise ValueError("Reviewed dataset must have both training and validation examples")
    csv_fields = list(rows[0])
    _write_rows(output / "labels.csv", rows, csv_fields)
    for split in ("train", "val"):
        _write_rows(output / f"{split}.csv", [row for row in rows if row["split"] == split], csv_fields)
    _write_rows(output / "pending_review.csv", pending,
                ["field_id", "cell_index", "reason", "image_path", "transcription"])
    summary = {"schema_version": 1, "crops": len(rows), "fields_reviewed": len(annotations),
               "fields_extracted": len(fields), "pending_review_items": len(pending),
               "duplicate_crops_removed": duplicate_count,
               "split_only_literal_aliases": aliases,
               "students": len({row["writer_id"] for row in rows if not row["writer_id"].startswith("BLANK:")}),
               "train_crops": sum(row["split"] == "train" for row in rows),
               "val_crops": sum(row["split"] == "val" for row in rows),
               "class_counts": dict(Counter(row["label"] for row in rows)),
               "exam_counts": dict(Counter(row["exam"] for row in rows)),
               "train_writers": sorted({row["writer_id"] for row in rows if row["split"] == "train"}),
               "val_writers": sorted({row["writer_id"] for row in rows if row["split"] == "val"}),
               "annotations_sha256": _sha256(annotations_path), "seed": seed,
               "padding_mm": 1.5, "image_size": 64, "preprocessing": "autocontrast_pad64_invert_v1",
               "label_policy": "Visible digit transcriptions; '?' cells excluded. Original writer splits retained."}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiz-dataset", type=Path)
    parser.add_argument("--run-dir", action="append", type=Path, default=[])
    parser.add_argument("--extra-field", action="append", default=[], help="Visually checked additional source:PROGRAM bank, e.g. 26:MTECH")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--seed", type=int, default=557)
    args = parser.parse_args(argv)
    if args.annotations:
        summary = finalize(args.output_dir, args.annotations, seed=args.seed)
    else:
        if not args.quiz_dataset or not args.run_dir:
            parser.error("Extraction requires --quiz-dataset and at least one --run-dir")
        summary = prepare(args.quiz_dataset, args.run_dir, args.output_dir, extra_fields=args.extra_field)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
