# Written-Answer OCR

Written-answer OCR is text extraction, not grading. The grading/review layer
must still decide whether extracted text is usable.

**Full OMR PDFs/photos plus their matching manifest are the normal input.**
The existing batch workflow crops answers and prepares their lines automatically.
Manually cropped line images are optional diagnostics, not a requirement.

## Current Pipeline

```text
canonical aligned page
  -> manifest written_block boxes
  -> exact answer crop per question
  -> split crop into expected ruled lines
  -> remove printed box/rule lines
  -> normalize illumination and resize line crop
  -> offline handwriting text recognizer
  -> line text + confidence + review flags
  -> question-level extracted text
```

This keeps geometry deterministic. The system does not search the page for
answers after alignment; it uses the manifest coordinates, then treats OCR as
an optional interpretation of the saved crop.

## Backends

- `trocr`: the implemented Transformer baseline for English handwritten line OCR. It uses
  `microsoft/trocr-base-handwritten` by default and runs through
  Hugging Face Transformers. It needs `python -m pip install .[htr]`.
- `tesseract`: lightweight local fallback. Useful for neat writing and printed
  text, but not reliable enough for poor handwriting.
- Future backend: PaddleOCR can be added for text detection, orientation, and
  recognition if we want a broader OCR pipeline.

## Why Line Segmentation Matters

The generated OMR already knows how many ruled lines each answer has. Splitting
the crop into expected line bands provides inputs for the single-line TrOCR
model. Each band is processed independently and joined back into the question
answer. This is not actual handwriting-line detection: writing between rules,
extra lines, and fractions can be split incorrectly. Inspect the saved line
crops alongside the complete answer crop during the pilot.

## Full-Sheet Pilot

### Inputs and checkout

Give the tester the full scanned PDF or original photos and the **original
matching manifest**, privately. A checkout does not include `data/`, scans,
student records, model weights, or diagnostic scripts from another computer.
Do not regenerate a manifest from guessed exam settings for an existing printout.

For an existing clean checkout, run from the repository root:

```powershell
git status --short
git pull --ff-only
git rev-parse HEAD
```

If there are local changes or divergent commits, inspect them before updating;
do not reset or discard them to follow this guide.

Use these locations, replacing the example exam ID with the manifest's `exam_id`:

```text
data/exams/CSE222_ENDSEM_2026.manifest.json
data/uploads/scanned_bundle.pdf
```

The upload filename is arbitrary. `--scans` also accepts an image or a folder
of supported scans. Keep a folder input limited to the intended exam bundle;
do not mix output images, other exams, or repeated photographs accidentally.
The manifest supplies answer positions and expected ruled-line counts, so no
separate line-count metadata or manual cropping is needed.

### Environment

Use an isolated Python 3.11 environment. Activate the existing project Conda
environment or virtual environment; do not copy another machine's environment.
If none exists, Windows users with Python 3.11 installed can create one:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

All commands below use `python` from that activated environment. Check it first:

```powershell
python -c "import sys; print(sys.executable); print(sys.version)"
python -m pip install --upgrade pip
```

Choose **one** PyTorch installation for the test machine. For a supported NVIDIA
GPU/driver, the following CUDA 12.8 pair was used in the reported laptop setup:

```powershell
python -m pip install torch==2.11.0 torchvision==0.26.0 --index-url https://download.pytorch.org/whl/cu128
```

For a Windows/Linux CPU-only test, use this instead and select `cpu` when parsing:

```powershell
python -m pip install torch==2.11.0 torchvision==0.26.0 --index-url https://download.pytorch.org/whl/cpu
```

These pairs are listed in the [official PyTorch installation combinations](https://pytorch.org/get-started/previous-versions/).
Other hardware/platforms need their appropriate official build; do not assume
that a friend's machine has the same GPU as the reported RTX 4050 laptop.

Install the existing project and the same Transformers version used for that
loading check. This is a pilot baseline, not a claim that all other versions fail:

```powershell
python -m pip install -e ".[dev,htr]" "transformers==4.57.6"
python -m pip check
python -c "from transformers import TrOCRProcessor, VisionEncoderDecoderModel; print('TrOCR imports passed')"
python -m pytest -q omr/reader/tests/test_written_ocr.py
```

Those OCR tests use a fake backend. They do not download weights or measure
handwriting accuracy. For a CUDA run, also verify an actual GPU calculation:

```powershell
python -c "import torch; print(torch.__version__, torch.version.cuda); assert torch.cuda.is_available(), 'CUDA unavailable'; print(torch.cuda.get_device_name(0)); x = torch.ones(4, device='cuda'); print((x * 2).tolist()); torch.cuda.synchronize()"
```

Do not silently switch to CPU after a failed GPU check. Diagnose the installation
or explicitly select CPU and report that choice.

### Run the complete bundle

Run this PowerShell example from the repository root. Change the first three
values for the actual inputs and device. The new output root preserves older runs.

```powershell
$examId = "CSE222_ENDSEM_2026"
$scans = "data/uploads/scanned_bundle.pdf"
$device = "cuda"
$runRoot = Join-Path "data/pilots" ([guid]::NewGuid().ToString("N"))

python -m omr.workflows.batch `
  --exam-id $examId `
  --scans $scans `
  --data-dir data `
  --output-root $runRoot `
  --written-answer-ocr trocr `
  --written-ocr-model microsoft/trocr-base-handwritten `
  --written-ocr-device $device

if ($LASTEXITCODE -ne 0) { throw "Batch failed; inspect the error and retain partial outputs." }
Join-Path $runRoot $examId
```

On this friend's first run, allow the model to download into the local Hugging
Face cache. The other laptop's cache is not part of GitHub. After a successful
download, add `--written-ocr-local-files-only` to the batch command to require
cached weights. The recognizer runs locally; scans are not sent to an OCR API.

The batch workflow loads the model once, then processes the grouped students'
answer crops line by line. It does not award written-answer marks or send email.
An MCQ answer key and roster are optional for extraction; without a key, MCQs
are read but not scored. Existing files in `data/answer_keys/` may be loaded
automatically, so check that any key belongs to this exam.

Grouping defaults to `auto` with a `high` minimum identity confidence. Do not
lower the threshold or force scanner order simply to make every page attach.
Handwritten roll OCR is separate from written-answer OCR; this command does
not enable it or install Tesseract's executable. If grouping needs that backend,
configure it separately using the [batch instructions](../README.md#parse-a-multi-student-pdf).

### Inspect and return results

The run prints its index and review-report paths. Under
`<runRoot>/<examId>/`, inspect:

```text
parse_index.json
review_report.html
review_report.csv
students/<roll_no>/sheet.pdf
students/<roll_no>/student.json
students/<roll_no>/written/Q11.png
students/<roll_no>/written_ocr/Q11_lines/Q11_line_1.png
```

Question numbers and student folders come from the actual exam/results; `Q11`
is only an example. `student.json` stores text at
`written_responses[].ocr.text`, with `confidence`, `review_flags`, and
`line_results` in the same `ocr` object. OCR paths are relative to that student's
directory. For example, display one student's extracted answers in PowerShell:

```powershell
$studentPath = Join-Path (Join-Path $runRoot $examId) "students/2024587/student.json"
$student = Get-Content -LiteralPath $studentPath -Raw | ConvertFrom-Json
$student.written_responses | Select-Object q_no, @{Name="Text"; Expression={$_.ocr.text}}, @{Name="Confidence"; Expression={$_.ocr.confidence}}
```

Replace the sample roll number with an actual generated folder. The HTML report
primarily covers page grouping, alignment, and review links; it is not an OCR
accuracy dashboard. A student's `ready` status does **not** certify its OCR:
inspect the nested OCR flags and compare every pilot answer with the image.
Unmatched pages and page errors are in the index/report; missing OCR output for
an unassigned page is not evidence that the student's answer was blank.

Return the Git commit, OS/hardware, Python/package versions, exact command,
device, elapsed time, page/student counts, unmatched/error counts, and run path.
For representative questions, include the original answer crop, prepared line
crops, extracted text, and a human-checked literal reading. Note omissions,
duplicates, blank-answer hallucinations, and number/formula errors. Separate
first-run download/loading time from any warmed-up timing; the batch CLI does
not itself produce per-answer latency or GPU-memory benchmarks.

Keep artifacts private and out of Git. Review all pilot transcriptions; a
confidence score is not an accuracy percentage. Quantitative CER/WER requires
independently checked transcripts and an explicit scoring policy. Start with a
small full-sheet bundle before measuring a class-sized exam.

## Current Parameters

- Written line target height: `72 px`
- Minimum line ink fraction: `0.0025`
- Rule removal: horizontal morphology kernel `max(24, width / 5)`
- Vertical border removal: vertical morphology kernel `max(10, height / 2)`
- TrOCR default model: `microsoft/trocr-base-handwritten`
- TrOCR generation: `num_beams=4`, `max_new_tokens=96`

## Production Caution

Very poor handwriting cannot be made perfectly accurate by threshold changes
alone. Accuracy needs three things:

- clean aligned crops
- a recognizer trained or fine-tuned on handwriting similar to our students
- confidence/roster/rubric validation before accepting the result

The current confidence calculation is not calibrated to real transcription
correctness. Rule removal can also erase mathematical strokes, and expected-line
splitting can omit or duplicate writing. Keep these outputs provisional and
review them against the original images, even when the reported confidence is high.

The [extraction and grading research](ANSWER_EXTRACTION_AND_GRADING_RESEARCH.md)
records known limitations, proposed fixes, and candidate comparisons. Those
proposals are not additional implemented backends or measured accuracy results.

## References

- TrOCR paper:
  https://arxiv.org/abs/2109.10282
- Hugging Face TrOCR model docs:
  https://huggingface.co/docs/transformers/en/model_doc/trocr
- TrOCR handwritten model:
  https://huggingface.co/microsoft/trocr-base-handwritten
- PaddleOCR pipeline docs:
  https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/OCR.en.md
- Tesseract OCR:
  https://github.com/tesseract-ocr/tesseract
