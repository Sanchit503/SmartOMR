"""Prepare a balanced cancelled-bubble training dataset from crop datasets."""
from __future__ import annotations

import argparse
import csv
import random
import shutil
from pathlib import Path

from PIL import Image, ImageDraw


LABELS = ("empty", "filled", "cancelled")
AUTO_CANCEL_KINDS = {"mcq", "numerical"}


def _auto_label(row: dict[str, str]) -> tuple[str, str]:
    bubble_kind = row.get("bubble_kind", "")
    total = float(row["total_ink_ratio"])
    inner = float(row["inner_ink_ratio"])
    outer = float(row["outside_circle_ink_ratio"])
    diagonal_45 = float(row["diagonal_45_score"])
    diagonal_135 = float(row["diagonal_135_score"])
    diagonal_max = max(diagonal_45, diagonal_135)
    diagonal_min = min(diagonal_45, diagonal_135)
    distance = float(row["ink_distance_from_center_max"])
    bbox_width = float(row["bounding_box_width"])
    bbox_height = float(row["bounding_box_height"])
    bbox_major = max(bbox_width, bbox_height)

    if bubble_kind == "program":
        return "needs_review", "program selector crops are excluded from starter auto-labels"

    if inner <= 0.12 and outer <= 0.035 and distance <= 1.50 and total <= 0.16:
        return "empty", "low inner ink, low outside ink"
    if inner >= 0.42 and outer <= 0.055 and distance <= 1.55:
        return "filled", "high inner ink, low outside ink"

    if bubble_kind not in AUTO_CANCEL_KINDS:
        return "needs_review", "cancelled auto-label is only enabled for answer bubbles"

    has_cross = (
        outer >= 0.11
        and distance >= 1.75
        and diagonal_max >= 0.46
        and diagonal_min >= 0.20
        and bbox_width >= 2.15
        and bbox_height >= 2.15
    )
    has_strong_slash = (
        outer >= 0.15
        and distance >= 1.95
        and diagonal_max >= 0.55
        and bbox_major >= 2.45
    )
    if has_cross or has_strong_slash:
        return "cancelled", "clear outside stroke/cross evidence"

    return "needs_review", "not confidently empty, filled, or cancelled"


def _read_feature_rows(dataset_dirs: list[Path]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for dataset_dir in dataset_dirs:
        with (dataset_dir / "features.csv").open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                row["source_dataset"] = dataset_dir.name
                row["source_dataset_dir"] = str(dataset_dir)
                label, reason = _auto_label(row)
                row["auto_label"] = label
                row["auto_label_reason"] = reason
                rows.append(row)
    return rows


def _copy_crop(row: dict[str, str], output_dir: Path, bucket: str) -> str:
    src = Path(row["source_dataset_dir"]) / row["crop_path"]
    dst = output_dir / bucket / row["auto_label"] / f"{row['source_dataset']}__{src.name}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return dst.relative_to(output_dir).as_posix()


def _write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _clean_output_dirs(output_dir: Path) -> None:
    for child in ("train_balanced", "reserve", "needs_review"):
        shutil.rmtree(output_dir / child, ignore_errors=True)


def _write_contact_sheet(rows: list[dict[str, str]], output_path: Path, limit: int = 120) -> None:
    if not rows:
        return
    thumbs: list[tuple[Image.Image, str]] = []
    for row in rows[:limit]:
        crop_path = Path(row["source_dataset_dir"]) / row["crop_path"]
        image = Image.open(crop_path).convert("RGB")
        image.thumbnail((90, 90))
        label = row["auto_label"]
        question = str(row.get("q_no") or "")
        value = str(row.get("value") or "")
        thumbs.append((image, f"{label}\n{row['source_dataset']} {question} {value}".strip()))

    cell_w, cell_h = 140, 125
    cols = 10
    rows_count = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell_w, rows_count * cell_h), "white")
    draw = ImageDraw.Draw(sheet)
    for index, (image, label) in enumerate(thumbs):
        x = (index % cols) * cell_w
        y = (index // cols) * cell_h
        sheet.paste(image, (x + (cell_w - image.width) // 2, y + 5))
        draw.text((x + 4, y + 96), label[:42], fill="black")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path)


def _balanced_rows(rows: list[dict[str, str]], seed: int, target_per_class: int | None) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    rng = random.Random(seed)
    by_label = {label: [row for row in rows if row["auto_label"] == label] for label in LABELS}
    target = target_per_class or min(len(items) for items in by_label.values())
    balanced: list[dict[str, str]] = []
    reserve: list[dict[str, str]] = []
    for label in LABELS:
        items = list(by_label[label])
        rng.shuffle(items)
        balanced.extend(items[:target])
        reserve.extend(items[target:])
    rng.shuffle(balanced)
    return balanced, reserve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a balanced cancelled-bubble training dataset")
    parser.add_argument("--dataset-dir", required=True, action="append", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--target-per-class", type=int)
    args = parser.parse_args(argv)

    _clean_output_dirs(args.output_dir)
    rows = _read_feature_rows(args.dataset_dir)
    labelled = [row for row in rows if row["auto_label"] in LABELS]
    needs_review = [row for row in rows if row["auto_label"] == "needs_review"]
    balanced, reserve = _balanced_rows(labelled, args.seed, args.target_per_class)

    for bucket_rows, bucket in ((balanced, "train_balanced"), (reserve, "reserve"), (needs_review, "needs_review")):
        for row in bucket_rows:
            row["balanced_crop_path"] = _copy_crop(row, args.output_dir, bucket)

    fields = list(rows[0].keys()) if rows else []
    if "balanced_crop_path" not in fields:
        fields.append("balanced_crop_path")
    _write_csv(args.output_dir / "auto_labels_all.csv", rows, fields)
    _write_csv(args.output_dir / "train_balanced.csv", balanced, fields)
    _write_csv(args.output_dir / "reserve.csv", reserve, fields)
    _write_csv(args.output_dir / "needs_review.csv", needs_review, fields)
    _write_contact_sheet(balanced, args.output_dir / "train_balanced_contact_sheet.png")
    _write_contact_sheet(
        [row for row in balanced if row["auto_label"] == "cancelled"],
        args.output_dir / "cancelled_contact_sheet.png",
    )
    _write_contact_sheet(needs_review, args.output_dir / "needs_review_contact_sheet.png")

    summary = {
        label: sum(1 for row in rows if row["auto_label"] == label)
        for label in (*LABELS, "needs_review")
    }
    summary_lines = [
        "Cancelled bubble training-prep dataset",
        "",
        f"Total crops read: {len(rows)}",
        f"Auto-label counts: {summary}",
        f"Balanced training rows: {len(balanced)}",
        f"Reserve rows: {len(reserve)}",
        f"Needs-review rows: {len(needs_review)}",
        "",
        "Important: auto labels are conservative starter labels, not ground truth.",
        "Review needs_review.csv and visually check a sample from every class before training.",
    ]
    (args.output_dir / "README.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    print("\n".join(summary_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
