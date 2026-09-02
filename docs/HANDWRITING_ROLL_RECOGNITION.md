# Handwritten Roll-Number Recognition

SmartOMR should not treat continuation-page handwriting as normal OCR over a
whole sentence. The generated sheet already gives us boxed roll-number cells,
so the production path is isolated digit recognition with strict validation.

## Chosen Architecture

```text
canonical aligned page
  -> manifest write-in fields
  -> exact BTECH/MTECH roll strip crops
  -> exact per-cell crops
  -> OpenCV cleanup per cell
  -> local digit recognizer
  -> whole-strip OCR fallback
  -> format + roster validation
  -> high/medium/low confidence
  -> attach page only when confidence policy allows
```

OpenCV is responsible for image processing, not final handwriting reasoning:

- grayscale conversion and illumination flattening
- cell-border/rule removal
- adaptive + Otsu thresholding
- small-component cleanup
- glyph cropping, centering, resizing, and deskewing
- feature extraction for the local KNN digit model

The recognizer is local-only:

- Primary: optional local KNN digit model trained from real roll-cell crops.
- Fallback: local Tesseract OCR when installed.
- Future upgrade path: replace or ensemble the KNN backend with an offline CNN,
  TrOCR, or PaddleOCR recognizer without changing page grouping.

## Current Parameters

- Batch canonical DPI: `300`
- Digit crop upscale: `4x` below 96 px, otherwise `2x`
- Digit threshold: adaptive Gaussian, block size `31`, C `9`
- Digit feature canvas: `28x28`
- Normalized glyph size: max `20x20` inside the canvas
- KNN neighbours: max `5`, capped by training-set size
- Minimum digit foreground fraction: `0.006`
- Maximum digit foreground fraction: `0.70`

These are intentionally conservative. The reader should send doubtful pages to
review rather than attach a continuation page to the wrong student.

## Model Training Data

Use the identity crops already produced by `smartomr-batch`:

```csv
image_path,label
data/parsed/CSE222_ENDSEM_2026/students/2024587/identity/page_2_btech_roll_cell_1.png,2
data/parsed/CSE222_ENDSEM_2026/students/2024587/identity/page_2_btech_roll_cell_2.png,0
```

Train with:

```bash
python -m omr.reader.digit_model --labels data/models/roll_digit_labels.csv --model data/models/roll_digit_knn.npz
```

For professor-demo testing, a small labelled set can prove the pipeline. For
production confidence, collect several dozen samples per digit from the same
printer/scanner/camera conditions used in real exams.

## Why Not OCR Alone

Whole-strip OCR can read neat handwriting sometimes, but it is brittle for
poor writing, box lines, blur, and tilted crops. The boxed-cell design lets us
turn one hard sequence-recognition problem into several simpler single-digit
classification problems, then use roll-number format and roster matching as
strong safety checks.

## References

- TrOCR shows why transformer OCR models are strong for handwritten text:
  https://arxiv.org/abs/2109.10282
- The Hugging Face TrOCR handwritten model is designed for single-line OCR:
  https://huggingface.co/microsoft/trocr-base-handwritten
- PaddleOCR's OCR pipeline includes orientation, unwarping, detection, and
  recognition modules:
  https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/OCR.en.md
- OpenCV's handwritten digit tutorial demonstrates local KNN digit
  recognition:
  https://docs.opencv.org/5.0/tutorials_contrib/ml/py_ml/py_knn/py_knn_opencv/py_knn_opencv.html
- Tesseract is useful local OCR, but its docs still emphasize image quality
  and training for stronger results:
  https://github.com/tesseract-ocr/tesseract
