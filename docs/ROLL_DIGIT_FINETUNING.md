# Quiz + Midsem Roll-Digit Fine-Tuning

Prepared on 2026-10-11. Original model files and identity rules are preserved.
The completed single-head fine-tuning ensemble is now selected locally for testing.

## Reviewed Dataset

Directory: `data/roll_digit_dataset/quiz_midsem_visual_v2`

- 4,077 extracted cell crops from quiz and the original 324-page midsem run.
- 4,051 visually labelled examples: 3,230 train / 821 validation.
- 157 actual blank boxes use the existing `blank` class.
- 26 cells excluded: 18 ambiguous/overwritten/partially erased digits and 8 S/P letter cells.
- Midsem source 261 could not be extracted because alignment failed.
- No uncertain cell is labelled from the model prediction or assigned student roll.

`labels.csv`, `train.csv` and `val.csv` contain image paths, literal digit labels,
writer identifiers, exam/page/cell provenance, reviewer and crop SHA-256.
`visual_annotations.json` holds the reviewed transcriptions: `_` means blank;
`?` means exclude. `fields.json` links crops back to their original files.
`pending_review.csv` and `pending_review.png` show the excluded cells.
`blind_contact_sheets/` contains the raw field crops without model predictions.

Quiz writers retain their original train/validation membership across both exams.
New writer identifiers are assigned reproducibly, with approximately 20% in validation.
Written-roll aliases found in quiz are kept in the same split as their original
writer; they do not change digit labels or production ownership. Completely blank
fields with unknown owners have source-level groups. Exact image duplicates cannot
appear on both sides of the split.

Labels were visually transcribed by the assistant, not independently verified by
a second human reviewer. Inspect the contact sheets before making production
accuracy claims. Validation is for model selection, not an independent production
test; use separately labelled unseen writers before replacing the current ensemble.

## Fine-Tune All Seven Existing Members

Run in PowerShell:

```powershell
cd "C:\IIIT Delhi\BTP\SmartOMR"
.venv\Scripts\python.exe -u -m omr.datasets.train_roll_digit_ensemble `
  --dataset-dir data\roll_digit_dataset\quiz_midsem_visual_v2 `
  --initial-ensemble data\models\roll_digit_ensemble_7\ensemble_manifest.json `
  --output-dir data\models\roll_digit_ensemble_7_quiz_midsem_v2 `
  --epochs 12 `
  --batch-size 64 `
  --lr 0.0001 `
  --device cpu `
  --cpu-threads 4
```

Each member starts from its own existing Kaggle-pretrained, OMR-fine-tuned
checkpoint, preserving seeds 557 through 563. Kaggle pretraining is not repeated.
Members train sequentially. The command verifies source checkpoint hashes and label
ordering, rejects writer overlap, and refuses to overwrite an existing ensemble.
The best validation-accuracy checkpoint is saved for each member. Check its loss,
class metrics and full-roll errors too; high digit accuracy alone does not establish
safe student ownership.

Outputs are seven new checkpoints, per-member metrics and a new
`ensemble_manifest.json`. The existing production ensemble is preserved and is not
automatically replaced. Choose a different output directory for another experiment.
The seven-member baseline run was started by the assistant and completed on
2026-10-11. Its checkpoints are preserved as the single-dense-layer baseline.

## Three-Dense-Layer Experiment

The supported architecture can now use two hidden dense layers after the same
convolutional backbone and global average pooling:

`128 -> Dense(64) -> ReLU -> Dropout(0.25) -> Dense(32) -> ReLU -> Dropout(0.25) -> Dense(11)`.

This is three dense layers total, with the same ten digits plus blank outputs.
Use `--optimizer adam --weight-decay 0.0001` for Adam's L2 gradient penalty.
The legacy default remains AdamW with decoupled weight decay; the two are not
described as equivalent. Dropout is active only during training. Upright
augmentation and writer-level validation splitting are unchanged.

```powershell
.venv\Scripts\python.exe -u -m omr.datasets.train_roll_digit_ensemble `
  --dataset-dir data\roll_digit_dataset\quiz_midsem_visual_v2 `
  --initial-ensemble data\models\roll_digit_ensemble_7\ensemble_manifest.json `
  --output-dir data\models\roll_digit_ensemble_7_dense3_adam_l2_v1 `
  --dense-hidden-sizes 64 32 `
  --dropout 0.25 `
  --optimizer adam `
  --weight-decay 0.0001 `
  --reset-head `
  --epochs 12 --batch-size 64 --lr 0.0001 --device cpu --cpu-threads 4
```

`--reset-head` explicitly transfers each existing convolutional backbone but
initializes a new classifier: an old 128-to-11 head cannot be loaded into the
new 128-to-64-to-32-to-11 head. A shape change without this flag fails closed.
This is not an exact resume: no optimizer or scheduler state is restored.
Kaggle pretraining is not repeated, and all backbone parameters remain trainable.
Choose a new output directory for each run; existing ensembles cannot be overwritten.

Checkpoint `model_config` records hidden sizes, dropout and class count. Both
single-model and ensemble inference reconstruct the saved head before strictly
loading its weights, verify label order, and run in evaluation mode. Legacy
single-head checkpoints remain supported without architecture metadata.
Ensemble manifests also record optimizer, regularization and head-reset settings.

More layers, dropout and L2 do not guarantee better accuracy or eliminate
overfitting. Compare this experiment with the preserved baseline on identical
independently labelled, previously unseen writers; measure exact full-roll
accuracy, calibration and wrong accepted ownership before promoting it. Reused
validation crops are for model selection only. No production model path,
mapping threshold, or whole-strip OCR policy is changed by this experiment.

## Diverse Seven-Member Experiment

The uniform three-dense-layer experiment was stopped on 2026-10-11 after member
1 completed and member 2 reached epoch 8/12. Its checkpoints and logs remain in
`data/models/roll_digit_ensemble_7_dense3_adam_l2_v1`; this is not a completed
seven-member ensemble and is not used for evaluation.

The new versioned configuration is
`omr/datasets/configs/roll_digit_ensemble_diverse_v1.json`:

| Member | Dense Head (128 Features, 11 Labels) | Initial LR | Optimizer | Dropout | Decay | Epochs |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 128 -> 11 | 0.0001 | AdamW | 0 | 0.0001 | 12 |
| 2 | 128 -> 64 -> 11 | 0.0003 | Adam | 0.15 | 0.0001 | 24 |
| 3 | 128 -> 64 -> 32 -> 11 | 0.0005 | Adam | 0.20 | 0.0001 | 24 |
| 4 | 128 -> 96 -> 48 -> 11 | 0.0003 | AdamW | 0.25 | 0.0002 | 24 |
| 5 | 128 -> 48 -> 11 | 0.0002 | AdamW | 0.30 | 0.0003 | 24 |
| 6 | 128 -> 96 -> 32 -> 11 | 0.0003 | Adam | 0.15 | 0.00005 | 24 |
| 7 | 128 -> 11 | 0.00005 | Adam | 0 | 0.0002 | 12 |

All retain their corresponding original Kaggle/OMR convolutional backbone, with
all layers trainable. Members 1 and 7 also retain their compatible original
head; members 2-6 explicitly initialize a new head. Longer training for new
heads allows time to learn after initialization. Learning rates follow the
existing cosine schedule. These are experimental settings, not tuned optima.
Adam uses an L2 penalty; AdamW uses decoupled decay, not the same penalty.

```powershell
.venv\Scripts\python.exe -u -m omr.datasets.train_roll_digit_ensemble `
  --dataset-dir data\roll_digit_dataset\quiz_midsem_visual_v2 `
  --initial-ensemble data\models\roll_digit_ensemble_7\ensemble_manifest.json `
  --output-dir data\models\roll_digit_ensemble_7_diverse_heads_v1 `
  --member-config omr\datasets\configs\roll_digit_ensemble_diverse_v1.json `
  --batch-size 64 --device cpu --cpu-threads 4
```

Per-seed settings override shared CLI defaults. Exactly one entry per requested
seed is required; unknown keys, invalid settings, and incompatible head shapes
without an explicit reset fail before training. `training_plan.json` records
effective settings and source/data hashes at startup. The completed ensemble
manifest records the actual training configuration separately for every member.
Incomplete runs are not promoted or automatically resumed.

All members use the same writer-disjoint train/validation split and upright-only
augmentation. Different heads, seeds and hyperparameters do not guarantee
independent errors. Compare member error overlap, exact full-roll accuracy and
wrong ownership against the single-head baseline on untouched writers before
promoting this ensemble. It does not automatically replace the UI's selected
completed ensemble or change grouping thresholds.

## Testing The Completed Ensemble

The local default roll reader now uses
`data/models/roll_digit_ensemble_7_quiz_midsem_v2/ensemble_manifest.json`, the
completed seven-member fine-tuning run. The original ensemble is preserved;
the three-dense-layer experiment is not selected. Explicit model paths and
`SMARTOMR_ROLL_ENSEMBLE_MANIFEST` still override the default.

Restart the UI. Open Exams, the exam run, then Inspect Source Pages (or Source
Pages on the review screen). At the top, use Re-run Grouping. This action also
works for the two-step workflow. It archives the previous parsed results and
manual reviews under the run's `history/parsed_<timestamp>` directory; previous
manual assignments are not automatically applied to the new results. Existing
inspected alignment images are reused. OCR caches are invalidated by changed
model provenance, so a separate preview is not required to refresh the model.
For two-step runs, grading remains separate: review the new grouping, then use
2. Grade Answers to generate current marks again.

These scans contributed training crops. Improved results on them are a useful
regression check, not independent evidence of accuracy on new writers.

## Augmentation

Training only: rotation within +/-5 degrees with expanded canvas; contrast
0.85-1.25; brightness 0.90-1.10; ink strength 0.65-0.95 and mild illumination.
The faint-ink transform preserves local stroke contrast rather than clipping
already-faint marks to white. No horizontal/vertical flips, upside-down rotations,
or digit substitutions. Validation uses no augmentation.

Training and inference retain the existing autocontrast, 64-pixel padding,
inversion and normalization pipeline. The cached cell crops retain printed frames;
this change does not silently introduce frame removal or different inference crops.
`augmentation_preview.png` shows raw examples followed by five upright variants.

## Rebuild After Reviewing Excluded Cells

Update only transcriptions that are genuinely readable in the raw crop, with a
reviewer recorded. Keep letters/illegible cells excluded. Then regenerate the CSVs:

```powershell
.venv\Scripts\python.exe -m omr.datasets.prepare_roll_digit_finetuning `
  --output-dir data\roll_digit_dataset\quiz_midsem_visual_v2 `
  --annotations data\roll_digit_dataset\quiz_midsem_visual_v2\visual_annotations.json
```

For reproducible extraction of this dataset:

```powershell
.venv\Scripts\python.exe -m omr.datasets.prepare_roll_digit_finetuning `
  --quiz-dataset data\roll_digit_dataset\CSE557_QUIZ1_2026_20260918_183850_e22193 `
  --run-dir data\ui_runs\CSE557_MIDSEM_2026_20260930_200512_1ddb8f `
  --output-dir data\roll_digit_dataset\quiz_midsem_visual_v2 `
  --extra-field 26:MTECH `
  --extra-field 276:MTECH
```

The additional fields were selected by visual audit of the alternate bank, not by
rewriting OCR or assuming adjacent pages belong to the same student. Source numbers
above refer to the original 324-page midsem run, not later filtered PDFs.
