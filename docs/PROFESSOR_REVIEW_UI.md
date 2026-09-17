# Professor Review UI

This is a local, file-based dashboard for showing SmartOMR progress to a professor.
It does not require a database yet.

## Start

```powershell
cd "C:\IIIT Delhi\BTP\SmartOMR"
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
stored as `inputs/students.csv`; the uploaded source remains beside it for audit.

Only the first sheet of an `.xlsx` file is used.

The UI automatically attempts local Tesseract roll-number OCR. On page 1 it
compares the bubbled roll grid against the written roll boxes in the manifest.
If both are readable and disagree, that student is marked `needs_review`.
Continuation-page written roll OCR uses the same backend for grouping.

## Output

Every run is written under:

```text
data/ui_runs/<exam_id_timestamp>/
```

Important files:

```text
inputs/                         uploaded files
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
  -> open one exam run
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

Statuses come from the parser:

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
