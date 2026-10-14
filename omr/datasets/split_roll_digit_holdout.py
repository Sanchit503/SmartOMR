"""Make writer-disjoint train, validation and final-test splits without crop shuffling."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import json
from pathlib import Path
import random
import shutil


def split_rows(rows: list[dict], *, test_target: int, validation_target: int, seed: int = 557) -> dict[str, list[dict]]:
    if test_target < 1 or validation_target < 1 or test_target + validation_target >= len(rows):
        raise ValueError("Positive test/validation targets must leave training data")
    parents = {}
    def root(writer):
        parents.setdefault(writer, writer)
        if parents[writer] != writer:
            parents[writer] = root(parents[writer])
        return parents[writer]
    image_writers = {}
    for row in rows:
        writer, digest = row.get("writer_id"), row.get("crop_sha256")
        if not writer or not digest:
            raise ValueError("Every crop requires writer_id and crop_sha256")
        if digest in image_writers:
            parents[root(writer)] = root(image_writers[digest])
        image_writers[digest] = writer
        root(writer)
    groups = defaultdict(list)
    for row in rows:
        groups[root(row["writer_id"])].append(row)

    def select(available, target, selection_seed):
        order = sorted(available)
        random.Random(selection_seed).shuffle(order)
        reachable = {0: ()}
        for group in order:
            size = len(groups[group])
            for total, chosen in list(reachable.items()):
                amount = total + size
                if amount not in reachable:
                    reachable[amount] = (*chosen, group)
        candidates = [total for total in reachable if total > 0 and total < sum(len(groups[g]) for g in available)]
        if not candidates:
            raise ValueError("Too few independent writers for the requested split")
        count = min(candidates, key=lambda value: (abs(value - target), value))
        return set(reachable[count])

    test = select(set(groups), test_target, seed)
    available = set(groups) - test
    available_labels = {row["label"] for group in available for row in groups[group]}
    validation = None
    best_score = None
    # Class coverage is selected from labels, never from model predictions.
    for trial in range(32):
        candidate = select(available, validation_target, seed + 1 + trial)
        labels = {row["label"] for group in candidate for row in groups[group]}
        count = sum(len(groups[group]) for group in candidate)
        score = (abs(count - validation_target), len(available_labels - labels))
        if best_score is None or score < best_score:
            validation, best_score = candidate, score
        if score == (0, 0):
            break
    result = {"train": [], "val": [], "test": []}
    for group, items in groups.items():
        name = "test" if group in test else "val" if group in validation else "train"
        result[name].extend({**item, "split": name} for item in items)
    return result


def prepare_split(source: Path, output: Path, *, test_target: int = 500,
                  validation_target: int = 51, seed: int = 557) -> Path:
    if output.exists():
        raise FileExistsError("Choose a new output directory; existing datasets are preserved")
    with (source / "labels.csv").open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    if "split" not in fields:
        fields.append("split")
    for row in rows:
        path = (source / row["image_path"].replace("\\", "/")).resolve()
        if not path.is_relative_to(source.resolve()) or not path.is_file():
            raise ValueError("Missing crop or crop outside dataset")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row.get("crop_sha256"):
            raise ValueError("Crop hash mismatch")
    splits = split_rows(rows, test_target=test_target, validation_target=validation_target, seed=seed)
    output.mkdir(parents=True)
    for row in rows:
        relative = Path(row["image_path"].replace("\\", "/"))
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copy2(source / relative, destination)
    for name, values in {**splits, "labels": [row for values in splits.values() for row in values]}.items():
        with (output / f"{name}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(values)
    report = {
        "schema_version": 1, "seed": seed,
        "counts": {name: len(values) for name, values in splits.items()},
        "writer_counts": {name: len({row["writer_id"] for row in values}) for name, values in splits.items()},
        "class_counts": {name: dict(Counter(row["label"] for row in values)) for name, values in splits.items()},
        "targets": {"test": test_target, "val": validation_target},
        "policy": "Whole writers and duplicate-crop-linked writers stay together; validation class coverage uses labels only; test is excluded from model selection.",
        "warning": "Small validation split; checkpoint ranking may be unstable." if len(splits["val"]) < 200 else None,
        "source_labels_sha256": hashlib.sha256((source / "labels.csv").read_bytes()).hexdigest(),
    }
    (output / "split_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["counts"]))
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test-target", type=int, default=500)
    parser.add_argument("--validation-target", type=int, default=51)
    parser.add_argument("--seed", type=int, default=557)
    args = parser.parse_args()
    prepare_split(args.source, args.output, test_target=args.test_target,
                  validation_target=args.validation_target, seed=args.seed)
