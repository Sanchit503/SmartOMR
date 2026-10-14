# Google Colab: Writer-Held-Out Ensemble Training

Prepared on 2026-10-12. Original datasets, production models and active local
training remain unchanged. Two separate Colab export options are provided:

| Option | Training | Validation | Final Test |
| --- | --- | --- | --- |
| Requested counts | 3,500 | 51 | 500 |
| Recommended validation size | 3,200 | 351 | 500 |

The second option treats 3,551 as the training/development pool, with 351 reserved
internally for model selection. The first matches the requested actual gradient-
training count, but 51 validation crops from only a few writers is too small for
stable calibration or strong checkpoint-comparison claims. Choose the option
with care: the 51-crop validation set has no examples of digits 7 or 8, while
the recommended 351-crop validation set covers all digits and blank. Choose
before seeing any test results. Do not use final-test performance to pick a
split, tune thresholds, change hyperparameters or select ensemble members.

All 4,051 usable crops remain labelled from the visual review. Writers stay on
one side only, across both exams; duplicate-crop-linked writers stay together.
Validation label coverage is selected without looking at model predictions.
The test split spans 20 writer groups. It is used only after all seven members
finish. These assistant-transcribed labels still need independent human audit.

## Why The Starting Weights Differ

Colab starts from the seven saved **Kaggle-only** checkpoints, not the already
OMR-fine-tuned models. Otherwise previous OMR training could already have seen
newly designated test students. These weights were pretrained on the original
42k Kaggle digits; that pretraining is not repeated. All seven convolutional
backbones remain trainable; changed dense heads initialize anew. The same
versioned one/two/three-dense-layer configuration is used for OMR training.

## Run On Colab

1. Open the generated `smartomr_colab_training.ipynb` in Google Colab.
2. Select Runtime > Change runtime type > GPU, then connect.
3. Run the notebook cell. Upload the matching `smartomr_colab_training.zip` when prompted.
4. Keep the notebook/data private; upload only if permitted by your project.
5. Download the result ZIP after training and final-test evaluation finish.

See the official [Colab FAQ](https://research.google.com/colaboratory/intl/en-GB/faq.html)
for notebook upload, sharing and runtime details. Runtime storage is temporary;
download outputs before the session is discarded. GPU execution is not verified
on this local CPU machine. The notebook refuses to silently fall back to CPU.

The bundle contains code, digit crops, pseudonymous writer groups and pretrained
weights. No names, emails, roster, full scans or literal roll columns are included.
This reduces identifiers but does not make handwriting data fully anonymous.

Outputs contain the seven completed checkpoints, ensemble manifest, effective
member settings, training logs, `final_test/test_report.json` and
`final_test/test_predictions.csv`. Test metrics include digit accuracy, per-class
precision/recall/F1, confusion matrix and raw probability vectors. No test labels
are used for fitting, checkpoint selection or validation weighting.

## Compare Individual Models After Training

An older generated notebook reports only the ensemble. Upload the updated local
`omr/datasets/evaluate_exported_holdout.py` into the same still-connected Colab
session. Run that script with the exported dataset and the completed manifest,
with the input package on `PYTHONPATH`. Use a separate output directory such as
`/content/individual_model_test`; do not rerun the training notebook cell.

The current evaluator writes `model_comparison.csv` with seven individual rows
plus the ensemble, including sample/correct/error counts, accuracy, macro and
weighted precision/recall/F1. `test_report.json` contains each member's per-digit
metrics and confusion matrix. `per_model_predictions.csv` shows the expected
digit, predicted digit, correctness and raw probabilities for every model/crop.
All members use identical crops and preprocessing, with augmentation disabled
and dropout in evaluation mode. Results are diagnostic: select or remove members
using validation, not these final-test scores. If you tune using the test set,
it is no longer an untouched final test.

The test evaluates raw per-cell inference, not production PDF ownership/grouping.
More real, independently labelled unseen-writer scans are still needed before
automatic release. Do not overwrite the current local ensemble on download.
An interrupted cloud run preserves partial files while the runtime exists, but
does not automatically resume; incomplete ensembles must not be deployed.

## Rebuild The Requested Split

Use a fresh output folder/version; existing artifacts are protected.

```powershell
.venv\Scripts\python.exe -m omr.datasets.split_roll_digit_holdout `
  --source data\roll_digit_dataset\quiz_midsem_visual_v2 `
  --output data\roll_digit_dataset\quiz_midsem_colab_3500_51_500_v2 `
  --test-target 500 --validation-target 51

.venv\Scripts\python.exe -m omr.datasets.export_kaggle_training `
  --dataset-dir data\roll_digit_dataset\quiz_midsem_colab_3500_51_500_v2 `
  --initial-ensemble data\models\roll_digit_ensemble_7\ensemble_manifest.json `
  --platform colab --checkpoint-stage kaggle `
  --output output\colab_training\requested_v2\smartomr_colab_training.zip
```

For the recommended option, set `--validation-target 351` and use a separate
dataset and export folder. Whole-writer grouping may make requested counts
approximate on other datasets; never split a writer to force an exact number.
