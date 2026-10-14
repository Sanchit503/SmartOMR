# Three-Ensemble Comparison

The comparison runner evaluates saved ensembles on identical labelled roll-cell
crops, including every individual member as well as each combined ensemble,
without retraining or changing the UI configuration. It verifies checkpoint
hashes, crop hashes, labels and each ensemble's recorded fine-tuning split hashes.
Writer exposure is mapped through crop hashes, including anonymised Colab writer IDs.

## Recorded Diagnostic Run: 2026-10-12

| Ensemble | Correct / 500 | Errors | Digit accuracy | Training exposure | Validation exposure |
| --- | --- | --- | --- | --- | --- |
| UI configured, quiz/midsem v2 | 499 | 1 | 99.8% | 361 crops | 139 crops |
| Laptop, diverse heads v1 | 499 | 1 | 99.8% | 361 crops | 139 crops |
| Colab, diverse heads | 495 | 5 | 99.0% | 0 crops | 0 crops |

**These numbers cannot fairly rank generalisation.** There are no crops in this
comparison unused by all three current fine-tuning runs. The 139 crops not used
for training were used for validation and checkpoint selection by both local
ensembles. That subset gives 138/139, 138/139 and 136/139 respectively, but is
still not an independent shared test set. The exposure audit does not establish
that every ancestor checkpoint was free of exposure.

All three disagree with the recorded label on `Q0004__c3.png` (label 2,
prediction 9). Review the source and label blindly before interpreting this as
a confirmed model error. No labels were changed. Local ensembles have identical
argmax predictions on all 500 samples, with different confidence values.

The Colab result reproduces the user's earlier exported 495/500 test result.
The UI's configured checkpoint set was compared; stored UI runs may contain
older predictions. No production model switch was performed.

## Run Again

From the repository root in PowerShell:

```powershell
.venv\Scripts\python.exe -u -m omr.datasets.compare_roll_digit_ensembles `
  --config omr\datasets\configs\three_ensemble_comparison.json `
  --output-dir output\ensemble_comparison_repeat `
  --device cpu --cpu-threads 4
```

Use a new output directory: existing reports are not overwritten. The configuration
references the imported Colab checkpoint manifest and its exact original training
and validation CSVs under the dated comparison's `inputs` directory. Preserve those
files, not merely a renamed or reconstructed split CSV.

Each run now automatically generates:

- `Three_Ensemble_Comparison.html`: self-contained visual report. Each group has
  a seven-member table and a combined-ensemble row. Group/model selectors show
  individual confusion matrices (counts, row percentages, errors only), class
  precision/recall/F1, confidence/coverage diagnostics and failed raw crops.
- `model_comparison.csv`: one row per member and per combined ensemble (24 rows
  for three seven-member groups), including head depth, learning rate, dropout,
  seed, ensemble weight, accuracy, errors and macro/weighted metrics.
- `member_predictions.csv`: one row per member per crop (10,500 for 21 members
  on 500 crops), with all class probabilities, correctness and split exposure.
- `ensemble_comparison.csv` and `paired_predictions.csv`: retained ensemble-only
  outputs, including 1,500 predictions for three ensembles on 500 crops.
- `comparison_report.json`: nested member and ensemble metrics, confusion
  matrices, checkpoint hashes, exposure breakdowns and confidence diagnostics.

Member probabilities are retained from the same evaluation forward pass used to
combine the ensemble. Members are not mistaken for their ensemble output and no
checkpoint is retrained, switched or selected on the comparison results.

The original ensemble-only report is preserved. The complete individual-model run
is saved at
`output/ensemble_comparison_2026-10-12_members/Three_Ensemble_Comparison.html`.

## Scope and Next Fair Test

The runner uses the same evaluation preprocessing, disables augmentation/dropout,
and averages softmax probabilities weighted by manifest validation accuracy.
Classification is argmax, not a confidence threshold. It does not execute runtime
enhancement retries, blank-field ink tests, roster decoding or ownership grouping.
Do not call these results production mapping accuracy or calibrated confidence.

For a fair comparison, collect new, independently labelled writers excluded from
every model's training and selection history. Configure their test CSV and writer
catalogue, supply the original fit CSVs for each ensemble, and set
`require_common_holdout` to `true` (the default). The runner then rejects recorded
training or validation writer exposure. A complete ancestral exposure audit remains
necessary outside this current-fit check.

Use the untouched set to evaluate digit accuracy and exact full-roll accuracy.
Separately replay frozen production decisions to measure wrong accepted ownership,
review burden and rejection rates. Choose thresholds using a separate calibration
set, not the final test set. No ownership thresholds were changed by this work.
