# Train The Diverse Ensemble On Kaggle

The export command packages the current local training code, the seven-member
configuration, the 4,051 reviewed OMR crops, and the original seven checkpoints.
It does not include full scans, names, emails or a roster. CSV writer groups are
pseudonymous, with the same train/validation split; this is not a guarantee that
handwriting data is anonymous. Obtain project approval before uploading it.

```powershell
cd "C:\IIIT Delhi\BTP\SmartOMR"
.venv\Scripts\python.exe -m omr.datasets.export_kaggle_training `
  --dataset-dir data\roll_digit_dataset\quiz_midsem_visual_v2 `
  --initial-ensemble data\models\roll_digit_ensemble_7\ensemble_manifest.json `
  --output output\kaggle_training\smartomr_kaggle_training.zip
```

The exporter also creates `smartomr_kaggle_training.ipynb` beside the ZIP.
Existing exports are never overwritten; use a new folder after changing code.

1. Create a **private** Kaggle dataset from the ZIP. Do not publish the student data.
2. Import the generated notebook and keep it **private** as well.
3. Attach your private dataset to the notebook.
4. In Settings, select a GPU accelerator. Run the notebook cell.
5. Download `/kaggle/working/smartomr_trained_ensemble.zip` after all seven members finish.

Kaggle documents [notebook GPU settings](https://www.kaggle.com/docs/notebooks)
and [private datasets](https://www.kaggle.com/docs/datasets). Dataset and notebook
privacy settings are separate; check both. No external credentials or network
downloads are required by this training code.

This continues from existing MNIST/Kaggle-pretrained, OMR-fine-tuned backbones;
it does not repeat the initial 42k digit training. New dense heads are initialized
only for members whose shape changes. Heads have one, two or three dense layers,
with per-member learning rates, Adam/AdamW, dropout and decay specified in the JSON.
All layers remain trainable. Rotation stays within +/-5 degrees; no flips or
upside-down rotations. Blank is included as an eleventh class.

Models train sequentially on one GPU. GPU and CPU runs need not be bit-identical.
Best-validation-accuracy weights, per-class metrics, logs, hashes and effective
training settings are saved. A failed/interrupted run preserves partial artifacts
but is not automatically resumed; never deploy an incomplete ensemble.

This export leaves local training and the UI model selection unchanged. Do not
overwrite local models with downloaded output until comparing wrong ownership
and exact-roll accuracy on independently labelled, unseen writers. Reused
validation/student scans are not an independent production benchmark.
