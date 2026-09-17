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
  -> see all detected students, marks, and review status
  -> click roll number
  -> see full sheet images, overlays, answers, correct answers, marks, and flags
  -> mark student manually checked or keep in review
```

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
