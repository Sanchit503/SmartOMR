# GitHub Handoff

The repository includes the latest identity/grouping, student regrading,
written-answer crop, training/comparison and TA marking UI changes. Full scans,
rosters, UI run state, SMTP credentials, email logs, generated TA packets and
student-derived report previews are not published.

## Dataset

`data/roll_digit_dataset/shared_quiz_midsem_v2/` contains 4,051 labelled single-cell
PNG crops. `labels.csv`, `train.csv`, `val.csv` and `test.csv` contain only
`image_path`, `label`, `writer_id` and `crop_sha256`. Labels and crop bytes were
preserved, filenames use crop hashes, and rows are sorted by hash rather than
roll-cell order. Writer IDs are keyed pseudonyms; the lookup key is not retained.
This reduces identifying metadata, but does not establish complete anonymity or
replace the institution's requirements for sharing student-derived handwriting.

Colab splits are 3,200 training, 351 validation and 500 testing. Writers and
duplicate crops do not cross these split boundaries. `local_fit_train.csv` and
`local_fit_val.csv` retain the local models' actual earlier split of 3,230/821.
`dataset_manifest.json` records counts, classes and original/export CSV hashes.
No labels have been corrected to agree with model predictions.

## Models

These directories each include seven checkpoints, metrics and an ensemble manifest:

- `data/models/roll_digit_ensemble_7_quiz_midsem_v2/`: currently configured in the UI.
- `data/models/roll_digit_ensemble_7_diverse_heads_v1/`: completed laptop training.
- `data/models/roll_digit_ensemble_7_colab_gpu_v1/`: completed Colab training.

The previously tracked original ensemble remains available, together with its
seven Kaggle-pretrained initialization checkpoints for GPU training exports. Incomplete dense-head
experiments are not presented as completed models. Checkpoint hashes are preserved;
this publication does not retrain or switch the UI's active ensemble.

The original `ensemble_manifest.json` retains its original training provenance.
The additional `ensemble_manifest.public.json` references the shared CSV hashes
and preserves the original hashes in `publication.source_dataset_csv_sha256`.
This is a metadata reserialization of the same crop memberships, not evidence of
another training run. Source split memberships and checkpoint hashes were checked
when preparing these companion manifests.

## Start the UI

From the repository root, follow README installation steps. Roll OCR and model
comparison also require PyTorch, which is not in the base requirements:

```powershell
.venv\Scripts\python.exe -m pip install torch
```

Then run:

```powershell
.venv\Scripts\python.exe -m omr.ui.app --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765/`. Existing local exams are not cloned: upload your
authorized roster, manifest, answer key and scan PDFs to create a run.

TA Marking supports explicit question/student ranges, additional markers, packet
generation, SMTP delivery, returned marks, editable subject/message templates,
saved templates and per-packet email previews. Current written crops exclude the
MCQ section from the identity header and include more bottom whitespace using
manifest boundaries. Older local packets require explicit Regenerate Packet;
previously delivered emails and attachments remain unchanged.

## Reproduce the Comparison

This command runs every individual member and all three ensembles on the same
500 crops using only files present in a fresh clone:

```powershell
.venv\Scripts\python.exe -m omr.datasets.compare_roll_digit_ensembles `
  --config omr\datasets\configs\three_ensemble_public_comparison.json `
  --output-dir output\shared_ensemble_comparison --device cpu --cpu-threads 4
```

Use a new output directory each time. Reports include individual/ensemble
accuracies, confusion matrices, probabilities and exposure audits. The local
models already saw most of these 500 crops in training or validation. This is
diagnostic reproduction, not a fair independent generalization comparison. The
test set must not be used to tune thresholds or select a production model.

## Retraining

Use the shared `train.csv`/`val.csv` for training and checkpoint selection, leaving
`test.csv` for final evaluation. The diverse-head profile and augmentation code
are included: mild upright rotations/contrast/brightness/faint-ink changes, never
flips or upside-down digits. GPU notebook packaging instructions remain in
`COLAB_ENSEMBLE_TRAINING.md`; private source-data paths there are examples from the
original local run, not files included in GitHub. Use the shared directory instead.
