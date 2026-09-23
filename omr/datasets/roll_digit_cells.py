"""Build a labelled handwritten roll-digit cell dataset from parsed SmartOMR runs.

This is meant for roll-named PDF runs: the parsed student roll number is trusted,
so every roll-number write-in cell can be labelled automatically from that roll.
The output is a simple image-folder + CSV dataset suitable for CNN/ResNet
fine-tuning.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from omr.reader.handwriting import _cell_crop_boxes_px


DEFAULT_OUTPUT_ROOT = Path("data") / "roll_digit_dataset"
DEFAULT_DPI = 200.0


def _safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "item"


def _resolve_path(path_value: str | Path | None, base: Path) -> Path:
    if path_value is None or str(path_value) == "":
        return base
    path = Path(path_value)
    if path.is_absolute() or path.exists():
        return path
    candidate = base / path
    if candidate.exists():
        return candidate
    return path


def _roll_digits(roll_no: str) -> str:
    return "".join(char for char in str(roll_no).upper() if char.isdigit())


def _program_matches(field: dict[str, Any], program: str) -> bool:
    program = program.upper()
    programs = [str(item).upper() for item in field.get("programs", [])]
    if programs:
        return program in programs
    return str(field.get("program") or "").upper() == program


def _roll_field(manifest: dict[str, Any], page_index: int, program: str) -> dict[str, Any] | None:
    return next(
        (
            field
            for field in manifest.get("write_in_fields", [])
            if field.get("page", 1) == page_index
            and field.get("name") == "roll_number"
            and _program_matches(field, program)
        ),
        None,
    )


def _split_rolls(rolls: list[str], val_fraction: float, seed: int) -> tuple[set[str], set[str]]:
    shuffled = sorted(set(rolls))
    random.Random(seed).shuffle(shuffled)
    val_count = max(1, round(len(shuffled) * val_fraction)) if len(shuffled) > 1 else 0
    val = set(shuffled[:val_count])
    train = set(shuffled[val_count:])
    if not train and val:
        moved = sorted(val)[0]
        val.remove(moved)
        train.add(moved)
    return train, val


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_roll_digit_dataset(
    parsed_dir: str | Path,
    *,
    manifest_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    dpi: float = DEFAULT_DPI,
    padding_mm: float = 1.5,
    val_fraction: float = 0.2,
    seed: int = 557,
) -> Path:
    parsed_root = Path(parsed_dir)
    parse_index_path = parsed_root / "parse_index.json"
    if not parse_index_path.exists():
        raise FileNotFoundError(f"parse_index.json not found: {parse_index_path}")
    parse_index = json.loads(parse_index_path.read_text(encoding="utf-8"))

    if manifest_path is None:
        # In UI runs the manifest lives two folders above parsed/<exam_id>/.
        run_dir = parsed_root.parent.parent
        candidates = sorted((run_dir / "inputs").glob("*.manifest.json"))
        if not candidates:
            candidates = sorted((run_dir / "inputs").glob("*manifest*.json"))
        if not candidates:
            raise FileNotFoundError("manifest path was not supplied and no manifest was found in run inputs")
        manifest_file = candidates[0]
    else:
        manifest_file = Path(manifest_path)
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))

    dataset_dir = Path(output_dir) if output_dir is not None else DEFAULT_OUTPUT_ROOT / _safe_id(str(parse_index.get("exam_id") or parsed_root.name))
    images_dir = dataset_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for student in parse_index.get("students", []):
        roll_no = str(student.get("roll_no") or "")
        details_path = _resolve_path(student.get("details_path"), parsed_root)
        try:
            details = json.loads(details_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append({"roll_no": roll_no, "error": f"details_read_failed: {type(exc).__name__}: {exc}"})
            continue
        student_payload = details.get("student", {})
        program = str(student_payload.get("program") or student.get("program") or "").upper()
        digits = _roll_digits(roll_no)
        if not roll_no or not digits or not program:
            errors.append({"roll_no": roll_no, "error": "missing roll digits or program"})
            continue

        details_dir = details_path.parent
        for page in details.get("pages", []):
            page_index = int(page.get("page_index") or 0)
            field = _roll_field(manifest, page_index, program)
            if field is None:
                continue
            image_path = _resolve_path(page.get("canonical_image_path"), details_dir)
            try:
                image = Image.open(image_path).convert("L")
            except Exception as exc:
                errors.append({"roll_no": roll_no, "error": f"page_image_failed: {type(exc).__name__}: {exc}"})
                continue
            gray = np.asarray(image)
            boxes = _cell_crop_boxes_px(field, gray.shape, dpi, padding_mm)
            for cell_index, box in enumerate(boxes):
                label = digits[cell_index] if cell_index < len(digits) else "blank"
                x0, y0, x1, y1 = box
                crop = image.crop((x0, y0, x1, y1))
                rel_path = Path("images") / f"{_safe_id(roll_no)}__p{page_index}__c{cell_index + 1}__{label}.png"
                crop.save(dataset_dir / rel_path)
                rows.append(
                    {
                        "image_path": rel_path.as_posix(),
                        "label": label,
                        "roll_no": roll_no,
                        "program": program,
                        "page": page_index,
                        "cell_index": cell_index + 1,
                        "cell_count": len(boxes),
                        "source_details_path": str(details_path),
                        "source_image_path": str(image_path),
                    }
                )

    train_rolls, val_rolls = _split_rolls([str(row["roll_no"]) for row in rows], val_fraction, seed)
    for row in rows:
        row["split"] = "val" if row["roll_no"] in val_rolls else "train"

    fields = [
        "image_path",
        "label",
        "split",
        "roll_no",
        "program",
        "page",
        "cell_index",
        "cell_count",
        "source_details_path",
        "source_image_path",
    ]
    _write_csv(dataset_dir / "labels.csv", rows, fields)
    _write_csv(dataset_dir / "train.csv", [row for row in rows if row["split"] == "train"], fields)
    _write_csv(dataset_dir / "val.csv", [row for row in rows if row["split"] == "val"], fields)
    _write_csv(dataset_dir / "errors.csv", errors, ["roll_no", "error"])

    summary = {
        "exam_id": parse_index.get("exam_id"),
        "parsed_dir": str(parsed_root),
        "manifest_path": str(manifest_file),
        "dataset_dir": str(dataset_dir),
        "students": len({row["roll_no"] for row in rows}),
        "crops": len(rows),
        "train_crops": sum(1 for row in rows if row["split"] == "train"),
        "val_crops": sum(1 for row in rows if row["split"] == "val"),
        "train_rolls": len(train_rolls),
        "val_rolls": len(val_rolls),
        "class_counts": dict(sorted(Counter(str(row["label"]) for row in rows).items())),
        "program_counts": dict(sorted(Counter(str(row["program"]) for row in rows).items())),
        "errors": len(errors),
        "padding_mm": padding_mm,
        "dpi": dpi,
    }
    (dataset_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (dataset_dir / "README.txt").write_text(
        "Roll digit cell dataset for CNN/ResNet fine-tuning.\n"
        "Labels are derived from trusted roll-named PDF outputs.\n"
        "Use train.csv and val.csv for writer-disjoint training/validation splits.\n",
        encoding="utf-8",
    )
    return dataset_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract labelled roll-number digit cell crops")
    parser.add_argument("--parsed-dir", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--dpi", type=float, default=DEFAULT_DPI)
    parser.add_argument("--padding-mm", type=float, default=1.5)
    parser.add_argument("--val-fraction", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=557)
    args = parser.parse_args(argv)
    dataset_dir = build_roll_digit_dataset(
        args.parsed_dir,
        manifest_path=args.manifest,
        output_dir=args.output_dir,
        dpi=args.dpi,
        padding_mm=args.padding_mm,
        val_fraction=args.val_fraction,
        seed=args.seed,
    )
    summary = json.loads((dataset_dir / "summary.json").read_text(encoding="utf-8"))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
