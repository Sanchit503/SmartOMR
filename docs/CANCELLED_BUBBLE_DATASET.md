# Cancelled Bubble Dataset

This workspace is for training a small classifier that separates:

- `empty`
- `filled`
- `cancelled`

The extractor aligns each scan using the normal SmartOMR marker pipeline, then
uses the matching manifest to crop every bubble with context around the printed
circle. The default crop is `6r x 6r`, where `r` is the printed bubble radius.

## Folder Layout

Generated datasets live under:

```text
data/bubble_cancellation_dataset/<EXAM_ID>/
  raw_crops/
  metadata.csv
  features.csv
  labels.csv
  manifest_used.json
```

You only edit `labels.csv`. Fill the `label` column with:

```text
empty
filled
cancelled
```

Keep uncertain examples blank until review.

## Extract Crops

Example:

```powershell
python -m omr.datasets.bubble_crops --exam-id CSE557_QUIZ1_2026 --scans data\my_scans --data-dir data
```

If the scan is only one page from a multi-page OMR:

```powershell
python -m omr.datasets.bubble_crops --exam-id CSE557_QUIZ1_2026 --scans data\my_scans --data-dir data --allow-partial
```

## Train

Install ML dependencies:

```powershell
pip install scikit-learn joblib
```

Then train:

```powershell
python -m omr.datasets.train_cancelled_bubbles --dataset-dir data\bubble_cancellation_dataset\CSE557_QUIZ1_2026
```

The train/test split is sheet-wise using `sheet_id`, not random bubble-wise.
This avoids fake-high accuracy caused by the same sheet appearing in both
training and testing.
