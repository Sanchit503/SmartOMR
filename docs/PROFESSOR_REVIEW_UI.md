# Professor Review UI

This is a local, file-based dashboard for showing SmartOMR progress to a professor.
It does not require a database yet.

## Start

```powershell
cd "D:\SmartOMR"
.\.venv\Scripts\python.exe -m omr.ui.app
```

Open:

```text
http://127.0.0.1:8765
```

## Inputs

Create a new run from the UI and upload:

- scanned filled OMR PDF/image
- generated `manifest.json`
- answer key CSV/XLSX, optional
- master student list CSV/XLSX, optional

Answer key columns:

```text
q_no,answer,marks
```

Master list columns:

```text
roll_no,name,email,program
```

The upload also accepts institute-style headers such as:

```text
Roll No., Student Name, Class Type, Email Id
```

The IIITD portal CSV export is normalized automatically. Blank rows are skipped,
`Lecture` in `Class Type` is replaced by a safe program inference from the roll number/current
term, repeated spaces in names are collapsed, and an otherwise unnamed column is accepted as the
email column only when the row contains exactly one valid email address. The normalized copy is
stored as `inputs/students.csv`; uploaded originals remain in separate input subdirectories for audit.

Only the first sheet of an `.xlsx` file is used.

## Source-page Inspection

The upload starts **inspection only**, requiring just the scan and matching manifest. It inventories
all source pages before alignment, then processes one page at a time at 200 DPI (PDFs). Single-page
images retain their input resolution. Multi-frame images must be converted to PDF first, because
the downstream batch reader supports only single-frame image inputs.

The workspace has a searchable/filterable source list, original/aligned/overlay image modes,
zoom and page navigation, and per-page alignment evidence. Source page number and detected sheet
page number are separate: source 42 can be sheet page 2. Repeated sheet-page numbers are normal
in an exam bundle. Alignment failure does not remove a source page or stop later pages.

The original preview is a grayscale raster; the unchanged uploaded file is also available.
A pending page receives its preview when the worker reaches it. Failed rendering remains an
explicit failed page even if no preview can be produced.

No roll OCR, grouping, grading, roster lookup, or email runs in this stage. An aligned page is
not a verified identity. Quality scores are diagnostic scores, not calibrated probabilities.
The UI intentionally does not improve or certify the underlying OCR or alignment accuracy.

`Retry Failed Pages` skips completed aligned/review pages. After a server restart, interrupted
inspection can be resumed. Scan and manifest SHA-256 checks prevent reuse after input changes.
Inspection writes progress atomically. Only one processing operation runs in this local UI at a
time; wait for it before uploading another run. A batch evaluation is not resumable here: create
a new run if an interrupted batch already wrote artifacts. Existing review output is never reset
by clicking evaluation twice.

After inspection completes, `Run OCR & Grouping` explicitly starts the existing evaluation
pipeline. Alignment warnings/failures remain visible for inspection; this action does not approve
those pages or send mail. PDF decoding/alignment currently runs again in evaluation because its
identity workflow has a separate artifact contract. This extra pass is a known time/disk cost,
not a performance improvement. Inspect before committing to a long evaluation.

## Evaluation Prerequisite

Only evaluation requires local Tesseract roll-number OCR. It refuses to evaluate instead of silently
falling back when that backend is unavailable. Before evaluation, run:

```powershell
.\.venv\Scripts\python.exe -m omr.health --check-handwriting-ocr
```

On page 1, SmartOMR independently reads the bubbled roll grid and the written roll boxes. A
confident disagreement is marked `needs_review`; unreadable or low-confidence page-1 OCR is also
reviewed because the bubbled identity was not independently confirmed. Continuation-page written
roll OCR uses the same backend for grouping. The run dashboard must show
`Roll OCR: local_tesseract`.

## Output

Every run is written under:

```text
data/ui_runs/<exam_id_timestamp>/
```

Important files:

```text
inputs/                         uploaded files
inspection/index.json          all source pages, input hashes, progress, quality and errors
inspection/source_0001/         immutable per-attempt original/aligned/overlay images
inspection/source_0001/*/quality.json   full per-page alignment measurements
parsed/<exam_id>/parse_index.json
parsed/<exam_id>/email_release/email_skipped.csv
parsed/<exam_id>/students/<roll_no>/student.json
parsed/<exam_id>/students/<roll_no>/pages/page_1.png
parsed/<exam_id>/students/<roll_no>/debug/page_1_alignment_overlay.png
parsed/<exam_id>/students/<roll_no>/debug/page_1_sampling_overlay.png
reports/marks.csv
run_state.json
```

## Professor Flow

```text
Exam runs
  -> upload PDF + matching manifest (roster and answer key optional)
  -> inspect every source page and alignment failures
  -> explicitly run OCR & grouping
  -> open Student Review
  -> compare detected students with Roster and Missing Sheets counts
  -> open Review Cases
  -> assign unmatched pages to a roll or ignore confirmed duplicates/stray pages
  -> click roll number
  -> see full sheet images, overlays, answers, correct answers, marks, and flags
  -> verify, hold, or reject the grouping
  -> prepare the email queue only after missing/unreadable pages are resolved
  -> choose Sheet Verification - no marks when returning scans before grading is final
  -> inspect previews, dry-run, and real-send one test message before releasing the batch
```

A student cannot be verified while an expected page is missing. Roster students with no detected
sheet remain visible in the review page and are written to `email_skipped.csv`; they never silently
disappear from the release count.

The student UI separates parsing from verification. Parser `ready` is displayed as
`PENDING_VERIFICATION`, not `AUTO_GRADED`. Rejected and missing-page decisions stay distinct.
Verified grouping is labelled `MANUALLY_CHECKED`; this does not imply that marks were reviewed.
Without a key, marks display as `Not graded`, not `0 / 0`.

Student images and the source-page table use the same current selection as verified PDF creation,
including manual replacements. After manual assignment, the old parser PDF is not offered as the
current PDF. Verification creates the new PDF. Original response tables are withheld and the UI
shows `Regrading required` until a separate grading workflow handles the corrected selection.
This increment does not implement that regrading workflow. The dashboard's CSV is explicitly
labelled **Original Parser Marks CSV**, not a final reviewed marks export.

Raw parser statuses are preserved in artifacts:

```text
ready
needs_review
failed
```

Manual decisions are stored in:

```text
parsed/<exam_id>/verified_index.json
parsed/<exam_id>/verification_report.csv
parsed/<exam_id>/verification_report.html
```
