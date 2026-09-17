"""Train a first-pass cancelled-bubble classifier from extracted features."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

LABELS = ("empty", "filled", "cancelled")
META_COLUMNS = {
    "crop_id", "exam_id", "sheet_id", "source_path", "page", "bubble_kind",
    "q_no", "row", "value", "x_mm", "y_mm", "center_x_px", "center_y_px",
    "bubble_radius_px", "crop_half_size_px", "crop_path", "label",
}


def _load_rows(features_path: Path, labels_path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with labels_path.open(newline="", encoding="utf-8") as handle:
        labels = {row["crop_id"]: row.get("label", "").strip() for row in csv.DictReader(handle)}
    rows = []
    with features_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        feature_columns = [name for name in (reader.fieldnames or []) if name not in META_COLUMNS]
        for row in reader:
            label = labels.get(row["crop_id"], "").strip()
            if label in LABELS:
                row["label"] = label
                rows.append(row)
    return rows, feature_columns


def _sheet_split(rows: list[dict[str, str]], test_fraction: float) -> tuple[list[int], list[int]]:
    sheets = sorted({row["sheet_id"] for row in rows})
    if len(sheets) < 2:
        raise ValueError("need labelled crops from at least two sheets for sheet-wise validation")
    test_count = max(1, round(len(sheets) * test_fraction))
    test_sheets = set(sheets[-test_count:])
    train_idx, test_idx = [], []
    for index, row in enumerate(rows):
        (test_idx if row["sheet_id"] in test_sheets else train_idx).append(index)
    return train_idx, test_idx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train cancelled-bubble classifier from labels.csv + features.csv")
    parser.add_argument("--dataset-dir", required=True, type=Path)
    parser.add_argument("--model-out", type=Path)
    parser.add_argument("--test-fraction", type=float, default=0.2)
    args = parser.parse_args(argv)

    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import classification_report
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        import joblib
    except ImportError:
        print("error: install ML dependencies first: pip install scikit-learn joblib")
        return 1

    rows, feature_columns = _load_rows(args.dataset_dir / "features.csv", args.dataset_dir / "labels.csv")
    if not rows:
        print("error: no labelled rows found; fill labels.csv with empty/filled/cancelled first")
        return 1

    train_idx, test_idx = _sheet_split(rows, args.test_fraction)
    x = [[float(row[column]) for column in feature_columns] for row in rows]
    y = [row["label"] for row in rows]
    x_train = [x[index] for index in train_idx]
    y_train = [y[index] for index in train_idx]
    x_test = [x[index] for index in test_idx]
    y_test = [y[index] for index in test_idx]

    baseline = make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000, class_weight="balanced"))
    baseline.fit(x_train, y_train)
    print("Logistic regression baseline:")
    print(classification_report(y_test, baseline.predict(x_test), zero_division=0))

    model = RandomForestClassifier(
        n_estimators=300,
        max_depth=8,
        min_samples_leaf=3,
        class_weight="balanced",
        random_state=42,
    )
    model.fit(x_train, y_train)
    print("Random forest:")
    print(classification_report(y_test, model.predict(x_test), zero_division=0))

    model_out = args.model_out or (args.dataset_dir / "cancelled_bubble_random_forest.joblib")
    model_out.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "feature_columns": feature_columns, "labels": LABELS}, model_out)
    (model_out.with_suffix(".json")).write_text(
        json.dumps({"feature_columns": feature_columns, "labels": LABELS}, indent=2),
        encoding="utf-8",
    )
    print(f"Saved model: {model_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

