# SmartOMR

OMR-based assessment system. BTP project, IIIT Delhi. Full spec and module boundaries are in
[PROJECT_SPEC.md](PROJECT_SPEC.md) — read that first, it's the persistent design contract for this codebase.

## Status: local professor workflow ready for controlled pilots

Per the roadmap in PROJECT_SPEC.md Section 12:

- [x] **Phase 1** — OMR sheet generator + MCQ/numerical grading pipeline
- [x] **Phase 2 local batch parser** — streamed scan/PDF loading, fiducial alignment, page-index reading,
  alignment quality reports, BTech/MTech/PhD roll-number reading, roster CSV matching,
  roster reconciliation, MCQ/numerical result export, local diagnostics, and review artifacts
- [x] **Local professor UI and email release** — browser upload, student inspection,
  unmatched-page assignment, verification, preview queue, dry run, SMTP send log, and
  duplicate-send protection
- [x] **Written-answer workflow foundation** — crops, optional offline OCR, manual grading
  packet/import, and provider interface
- [ ] Production service hardening — authentication, database-backed jobs, resumable workers,
  transactional mail provider, and duplicate-sheet adjudication
- [ ] Real written-answer AI grading and re-evaluation request logging

## Professor UI

```bash
python -m omr.ui.app
```

Open `http://127.0.0.1:8765`, then upload the scanned PDF and matching manifest.
The first step is **Scan Inspection**: every source page remains in the inventory, with
original/aligned/overlay views, alignment checks, failures, progress, and retry/resume.
This step does not read roll numbers, group students, grade, or send email.
The answer key and student roster are optional uploads. CSV/XLSX rosters are normalized into the canonical
`roll_no,name,email,program` shape. The IIITD portal export is supported even when it contains a
blank row, `Roll No.`/`Student Name` headers, `Lecture` in `Class Type`, and an email column whose
header is blank.

After inspection, **Run OCR & Grouping** starts the existing evaluation workflow separately.
Only that step requires local roll-number OCR. Inspection does not require Tesseract.
Run this preflight before evaluation:

```powershell
.\.venv\Scripts\python.exe -m omr.health --check-handwriting-ocr
```

It must report `handwriting_ocr: ready`. On Windows, SmartOMR discovers the normal
`C:\Program Files\Tesseract-OCR\tesseract.exe` installation automatically; a custom location can be
set through `SMARTOMR_TESSERACT_CMD`.

After processing, resolve unmatched pages and verify every student from the Review page. Check
the `Missing Sheets` count against the roster before preparing email. Prepare and inspect the
email queue using **Sheet Verification - no marks** when returning scanned answer sheets. Use
**Evaluated Sheet + Marks** only after grading data has been checked and finalized. Run a dry run,
redirect one test email to a course-staff address, and only then perform the real send. Successful
recipients are not sent again unless the CLI is explicitly invoked with `--resend`.

## Mail final PDFs from a folder

This workflow is independent of OMR parsing. Supply the final per-student PDFs and the master
roster. Name each PDF exactly `<roll_no>.pdf`, or use `<roll_no>/sheet.pdf`. The preparer validates
that every PDF maps to one roster row, contains exactly two pages, is within the attachment size
limit, and has no duplicate roll or recipient email. It then freezes hashed copies of the PDFs so
later changes cannot silently alter an approved release.

```powershell
.\.venv\Scripts\python.exe -m omr.workflows.email prepare-folder `
  --pdf-dir "D:\final_student_pdfs" `
  --roster "D:\students.csv" `
  --marks-file "D:\tentative_marks.xlsx" `
  --body-template-file "D:\email_message.txt" `
  --output-dir "data\mail_releases\CSE557_QUIZ1_2026" `
  --exam-id "CSE557_QUIZ1_2026" `
  --sender "professor@iiitd.ac.in" `
  --sender-name "CSE557 Course Staff"
```

See [docs/EMAIL_RELEASE_WORKFLOW.md](docs/EMAIL_RELEASE_WORKFLOW.md) for the accepted marks columns,
message placeholders, validation checklist, and complete safe-send sequence.

Review `email_queue.csv`, `email_skipped.csv`, and the messages under `previews/`. A dry run opens
no SMTP connection and sends nothing:

```powershell
.\.venv\Scripts\python.exe -m omr.workflows.email send `
  --queue-csv "data\mail_releases\CSE557_QUIZ1_2026\email_queue.csv" `
  --smtp-host smtp.gmail.com --smtp-port 587 --smtp-security starttls `
  --username "professor@iiitd.ac.in" --sender "professor@iiitd.ac.in"
```

The first real operation must be a redirected test. It attaches one real student PDF but sends it
only to the specified course-staff address; it does not mark the student as sent. The password is
prompted without being written to a file or command line:

```powershell
.\.venv\Scripts\python.exe -m omr.workflows.email send `
  --queue-csv "data\mail_releases\CSE557_QUIZ1_2026\email_queue.csv" `
  --smtp-host smtp.gmail.com --smtp-port 587 --smtp-security starttls `
  --username "professor@iiitd.ac.in" --sender "professor@iiitd.ac.in" `
  --test-recipient "professor@iiitd.ac.in" --send
```

After checking that test, send to students by confirming the exact unsent count printed during
preparation (example: 150):

```powershell
.\.venv\Scripts\python.exe -m omr.workflows.email send `
  --queue-csv "data\mail_releases\CSE557_QUIZ1_2026\email_queue.csv" `
  --smtp-host smtp.gmail.com --smtp-port 587 --smtp-security starttls `
  --username "professor@iiitd.ac.in" --sender "professor@iiitd.ac.in" `
  --confirm-count 150 --delay-seconds 0.5 --send
```

Each successful delivery is logged immediately in `email_send_log.csv`. Re-running the same
command skips already-sent students and reports the new confirmation count required for the
remaining recipients. Use the SMTP host, port, security mode, and app password or relay credential
approved for the professor's college account.

## Generate a sheet

Run the terminal wizard:

```bash
python -m omr.generator.main
```

It asks for the exam requirements one at a time — university name, course code, exam name, MCQs,
non-negative whole-number questions (marks and maximum digits), and written questions (marks and
lines) — then writes the printable PDF and its template manifest into `data/exams/` and opens the PDF.

For repeatable generation, use a saved config file:

```bash
python -m omr.generator.main --config omr/generator/configs/midsem_cs301.json   # repeatable
```

`--output-dir` changes where files land; `--no-open` skips opening the viewer. A sheet produced
from prompts is byte-identical to the same sheet produced from a config file; there's a test that
pins that down.

### Preflight — how you know the sheet is actually good

Every generation rasterizes the PDF it just wrote and measures it the way the grader will, then
tells you the result:

```
Preflight (200 DPI, 1 page(s)):
  closest ink to a page edge: 11.8mm (left, page 1); safe area is 10mm
  202 bubbles, worst pre-printed fill 0.000
  3 answer boxes clean (darkest non-rule pixel 255/255 in Q21)
  marker quiet zones clear (darkest pixel 255/255 at page 1 TL)
  orientation marker present and decisive on every page
  page-index bars readable, weakest contrast 1.00 (page 1)
  tightest bubble gap 3.1mm (BT-col1-0 / BT-col1-1)
  All checks passed - this sheet is ready to print.
```

It checks that nothing is printed inside the printer-safe margin, that no bubble has ink in it
before a student writes, that every answer box contains nothing but its own writing rules, that
every registration marker (the four corners *and* the orientation marker) prints solid with clean
paper around it, that the orientation marker is decisive, that each page's index bars identify that
page and nothing else, that no two bubbles crowd each other, that the PDF's page count matches the
manifest, and that nothing runs off the page. Anything fatal prints `DO NOT PRINT` and exits
non-zero.

This exists because the layout engine can only check its own arithmetic. The failures that actually
break an OMR sheet — a glyph on top of a fiducial, a label inside a bubble — pass every coordinate
assertion and fail a printer. `--no-check` skips it, but there's rarely a reason to.

### Verify — does the grader actually read this sheet back?

Preflight proves a sheet is printable and readable. This proves the whole loop closes on *your*
exam: it fills the sheet in, grades it through the real grading path, and checks every answer came
back as the one that went in.

```bash
python -m omr.verify data/exams/CS301_MIDSEM_2026A.manifest.json
```

```
Round-trip verification of CS301_MIDSEM_2026A (1 page(s)):
  answers recovered:  20/20
  marks:              20/20 (every simulated answer was the correct one)
  signal separation:  0.958 (faintest fill 0.958 vs darkest blank 0.000)
  Generator and grader agree on this sheet.
```

**Signal separation** is the number to watch: the gap between the faintest deliberate fill and the
darkest bubble left blank. That's the headroom a real scan gets to eat into. Above ~0.5 is healthy.

You can make the simulated student sloppier to see where it breaks down, and save the filled-in
sheet to look at:

```bash
python -m omr.verify <manifest> --coverage 0.45 --darkness 110   # a careless pen: still 20/20
python -m omr.verify <manifest> --coverage 0.30 --darkness 160   # a very faint pencil: 0/20,
                                                                  # but all 20 flagged for review
python -m omr.verify <manifest> --save-filled filled.png --seed 7
```

That last case is the designed behaviour, not a bug: marks too faint to score are never silently
counted as blanks — they land in the review queue.

**What verification does not prove.** It fills and reads at the *same* manifest coordinates, so a
manifest that has drifted away from the printed sheet still round-trips; catching that is
preflight's job. And there's no perspective, lighting, or toner spread here — the thresholds are
calibrated against clean renders and need re-checking against real scans in Phase 2.

## Parse a filled sheet

Once you have a filled scan/photo PDF or image and its matching manifest, parse it with:

```bash
python -m omr.workflows.parse \
  --manifest data/exams/CSE202_Quiz_1.manifest.json \
  --scans scans/ \
  --students students.csv \
  --answer-key answer_key.csv
```

`--scans` can be one image/PDF or a folder of them. `--students` and `--answer-key` are optional:
without them the parser still aligns pages, decodes the bubbled identity, reads MCQs and numerical
answers, and crops written answers, but it cannot roster-match names/emails or award objective marks.

The answer-key CSV uses one format for MCQs and numerical answers:

```csv
q_no,answer,marks
1,B,1
6,7,2
```

For a three-digit numerical grid, the student bubbles `007`; the key may contain `7` or `007`.
Numerical answers are currently non-negative whole numbers only. Blank grids score zero, while
incomplete, faint, or multiply marked grids go to review rather than being guessed.

To drop an MCQ or numerical question from scoring, keep its answer-key row and set
`marks` to `0`, for example `2,DROP,0`. The CSV still requires a nonempty answer;
`DROP` is a readable placeholder, while the zero marks actually trigger exclusion.
The parser gives that question zero awarded marks and excludes its weight from the
effective total. For 15 one-mark numericals with Q2 and Q15 dropped, the total is 13.
Do not delete those rows, renumber questions, or change the original scan manifest.
This is exclusion, not full credit for everyone; written-question dropping is not
implemented by this answer-key mechanism. Regenerate evaluation results after
changing a key, and review the reduced total before releasing marks.

The parser writes to `data/parsed/<exam_id>/` by default:

```text
parse_index.json                 batch summary
sheets/<scan_id>/parse.json       full debug/result JSON for one sheet
sheets/<scan_id>/pages/page_1.png canonical aligned page image
sheets/<scan_id>/debug/page_1_alignment.json         post-warp quality metrics
sheets/<scan_id>/debug/page_1_aligned_color.png      color-preserved aligned page for review
sheets/<scan_id>/debug/page_1_alignment_overlay.png  visual overlay of expected anchors
sheets/<scan_id>/debug/page_1_sampling_overlay.png   exact full/core sample regions used by the reader
sheets/<scan_id>/written/Q11.png  written-answer crop
sheets/<scan_id>/written_ocr/Q11_lines/Q11_line_1.png  optional OCR line crop
```

This is intentionally a parser, not the final grader. Written answers are cropped and saved for the
future LLM/manual-grading step; no written marks are awarded here.

Every parsed page also carries `alignment_quality_status`, `alignment_quality_score`,
`alignment_report_path`, and `alignment_overlay_path` in `parse.json`. If the page aligns but the
markers, page bars, bubble anchors, or image-quality checks look risky, the sheet is marked
`needs_review` instead of being treated as final.

## Parse a multi-student PDF

For one PDF/folder containing pages from many students, use the batch command:

```bash
python -m omr.workflows.batch \
  --exam-id CSE222_ENDSEM_2026 \
  --scans data/uploads/scanned_bundle.pdf \
  --data-dir data
```

`--exam-id` resolves `data/exams/<exam_id>.manifest.json` and, when present,
`data/answer_keys/<exam_id>_answer_key.csv`. Each PDF page is aligned independently, page identity is
read, and pages are grouped even when page 1 and page 2 are far apart in the uploaded PDF.

By default, `smartomr-batch` uses `--grouping-mode auto`. It aligns every page and uses an
identity-first rule: page 1 becomes a verified anchor only when its bubbled and handwritten roll
numbers match, and a continuation page is attached only when its handwritten roll resolves to that
same exact roll number.

```bash
python -m omr.workflows.batch \
  --exam-id CSE222_ENDSEM_2026 \
  --scans data/uploads/scanned_bundle.pdf \
  --data-dir data
```

Source order does not matter: page 2 may appear before page 1. Missing, unreadable, conflicting,
duplicate, or non-roster identities remain in the review queue instead of being guessed. Explicit
`--grouping-mode page-major` and `--grouping-mode sheet-major` overrides remain available only for
controlled scanner workflows whose physical page order has been independently verified.

Output is written under `data/parsed/<exam_id>/students/<roll_no>/`:

```text
parse_index.json                         batch summary
review_report.csv                        spreadsheet-friendly review queue
review_report.html                       local browser review report
students/<roll_no>/student.json          full result for one student
students/<roll_no>/sheet.pdf             aligned multi-page PDF for verification/email
students/<roll_no>/pages/page_1.png      aligned canonical page
students/<roll_no>/debug/...             alignment/sampling overlays
students/<roll_no>/identity/page_2_btech_roll_crop.png
students/<roll_no>/written/Q11.png       written-answer crop
students/<roll_no>/written_ocr/Q11_lines/Q11_line_1.png
unmatched_pages/source_0003/page.json    page that could not be safely attached
page_errors/source_0004.json             page that could not be validated as SmartOMR
```

Open `review_report.html` first after a bulk scan. It lists ready students, needs-review students,
unmatched pages, hard page errors, the generated `sheet.pdf`, and the exact review flags that must
be cleared before any verification email/final grading step should trust the result.

## Verify parsed results

After checking `review_report.html`, create the separate human-verification artifact:

```bash
python -m omr.workflows.review init \
  --parsed-dir data/parsed/CSE222_ENDSEM_2026
```

This writes:

```text
verified_index.json                     final human review state
verification_report.csv                 spreadsheet-friendly verification state
verification_report.html                local browser verification report
verified/students/<roll_no>/sheet.pdf   human-approved sheet for later email
```

The parser output remains unchanged. The verifier starts parser-ready students as
`pending_verification`, flagged students as `needs_review` or `missing_pages`, and only explicit
human approval changes a student to `verified`.

Useful commands:

```bash
python -m omr.workflows.review summary --parsed-dir data/parsed/CSE222_ENDSEM_2026

python -m omr.workflows.review verify \
  --parsed-dir data/parsed/CSE222_ENDSEM_2026 \
  --roll-no 2024503 \
  --reviewer "TA" \
  --note "Checked roll number, pages, and review flags"

python -m omr.workflows.review reject \
  --parsed-dir data/parsed/CSE222_ENDSEM_2026 \
  --roll-no 2024544 \
  --reviewer "TA" \
  --reason "Wrong page grouping"

python -m omr.workflows.review assign-page \
  --parsed-dir data/parsed/CSE222_ENDSEM_2026 \
  --source-index 17 \
  --roll-no 2024503 \
  --page 2 \
  --reviewer "TA" \
  --note "Manual match from review report"
```

Later email/grading automation must use `verified_index.json`, not raw `parse_index.json`.

## Grade written answers manually

For production safety, written grading starts with a manual marks packet. Only `verified` students
are exported by default.

Add a rubric CSV when question text or marking guidance is available. The command auto-detects
`data/rubrics/<exam_id>_written_rubric.csv`, or you can pass `--rubric` explicitly:

```csv
q_no,question_text,rubric,model_answer,max_marks
11,Define OMR,Award one point for the definition and one for the use,A scanner-readable form,2
12,Explain alignment,Award partial credit for marker detection and perspective correction,Fiducials correct page warp,2
```

```bash
python -m omr.workflows.written export \
  --parsed-dir data/parsed/CSE222_ENDSEM_2026 \
  --rubric data/rubrics/CSE222_ENDSEM_2026_written_rubric.csv
```

This writes under `data/parsed/<exam_id>/written_grading/`:

```text
written_packet.json              structured list of exported written answers
written_answer_index.csv         crop links, question text/rubric, OCR text/confidence
manual_marks_template.csv        marks-entry template for professor/TA
written_review.html              local browser page with crop/question/rubric previews
```

Fill only these columns in `manual_marks_template.csv`:

```text
marks_awarded
needs_human_review
grader_comment
```

Then import the filled CSV:

```bash
python -m omr.workflows.written import-marks \
  --parsed-dir data/parsed/CSE222_ENDSEM_2026 \
  --marks-csv data/parsed/CSE222_ENDSEM_2026/written_grading/manual_marks_template.csv \
  --grader "TA"
```

The importer validates every mark against that question's `max_marks` and writes:

```text
written_grades.json              structured written grades
written_grades_report.csv        one row per written answer
final_scores.csv                 MCQ + numerical + written totals per verified student
```

The written-grading workflow also has a provider interface for future LLM/vision graders. Right
now the only shipped provider is `mock`, which is for plumbing tests only. By default it marks every
answer as `needs_review`, so it cannot accidentally become final college marks:

```bash
python -m omr.workflows.written auto-grade \
  --parsed-dir data/parsed/CSE222_ENDSEM_2026 \
  --provider mock
```

This writes the same `written_grades.json`, `written_grades_report.csv`, `final_scores.csv`, and
`written_review.html` artifacts as manual grading. A real provider should be added behind
`omr.grading.written.WrittenGrader` and must emit the same structured grade records: transcript,
marks, max marks, justification, confidence, provider/method, and review flags.

If you need to inspect crops before verification is complete, use `--include-unverified` on export,
but those rows are for debugging only and should not become final marks.

Continuation-page handwritten roll reading is local-first. With no OCR provider, the system saves
the roll crop and sends the page to review. With `--handwritten-roll-ocr local`, it uses offline
tools only: a trained local digit model when `--digit-model` is supplied, and local Tesseract as
fallback evidence when installed. No API key is needed:

```bash
python -m omr.workflows.batch \
  --exam-id CSE222_ENDSEM_2026 \
  --scans data/uploads/scanned_bundle.pdf \
  --data-dir data \
  --handwritten-roll-ocr local \
  --digit-model data/models/roll_digit_knn.npz
```

The local handwritten reader crops both the full roll strip and every digit cell using manifest
geometry, cleans the cell borders/noise with OpenCV, prefers cell-by-cell agreement, and rejects
conflicting or non-roster roll numbers instead of attaching a page to the wrong student.

To train the optional digit model, label saved identity cell crops in a CSV:

```csv
image_path,label
data/parsed/CSE222_ENDSEM_2026/students/2024587/identity/page_2_btech_roll_cell_1.png,2
data/parsed/CSE222_ENDSEM_2026/students/2024587/identity/page_2_btech_roll_cell_2.png,0
```

Then train:

```bash
python -m omr.reader.digit_model \
  --labels data/models/roll_digit_labels.csv \
  --model data/models/roll_digit_knn.npz
```

Check that it loads:

```bash
smartomr-doctor --data-dir data --digit-model data/models/roll_digit_knn.npz
```

Keep at least a few dozen examples per digit before trusting the model for unattended grouping.
Until then, roster validation and review flags are the guardrails.
The detailed architecture and parameter notes are in
[docs/HANDWRITING_ROLL_RECOGNITION.md](docs/HANDWRITING_ROLL_RECOGNITION.md).

Written-answer OCR is also optional. It does not grade answers; it extracts line-level text,
confidence, and review flags into `student.json` / `parse.json`. The implemented Transformer
backend is TrOCR. Full scans and their original matching manifest are sufficient: answer
cropping and line preparation are automatic; manually cropped samples are not required.

For another machine or a first real handwriting test, follow the
[full-sheet pilot guide](docs/WRITTEN_ANSWER_OCR.md#full-sheet-pilot). It covers CPU/GPU setup,
first-run model downloads, separate output directories, and where to inspect the OCR text.
The [research decision document](docs/ANSWER_EXTRACTION_AND_GRADING_RESEARCH.md) describes
proposed improvements, not additional implemented capabilities.

Example with optional handwritten-roll recognition already configured:

```bash
python -m pip install .[htr]
python -m omr.workflows.batch \
  --exam-id CSE222_ENDSEM_2026 \
  --scans data/uploads/scanned_bundle.pdf \
  --data-dir data \
  --handwritten-roll-ocr local \
  --digit-model data/models/roll_digit_knn.npz \
  --written-answer-ocr trocr
```

Details are in [docs/WRITTEN_ANSWER_OCR.md](docs/WRITTEN_ANSWER_OCR.md).

## Printing

The sheet is laid out for **ordinary A4 printers** and never relies on borderless printing.

- **Print at 100% scale.** Not "fit to page", not "shrink oversized pages". Scaling moves every
  coordinate away from where the manifest says it is.
- **Plain white A4**, laser or good inkjet. Avoid coloured or recycled stock with visible flecks.
- **Single-sided.** Each page is deskewed independently and needs its own four corner markers.
- Check one printed copy before running 200: the four corner squares and the small fifth square
  near the top-left must all be present and solid black, none of them clipped.

### The safe area

Nothing important is placed within **10mm** of any page edge (`PRINTER_SAFE_MARGIN_MM`), and the
fiducials get a further 2mm on top of that, so their outer edges sit **12mm** in. Preflight measures
the real figure on the rendered page and prints it — currently **11.8mm** of clearance to the
nearest ink on any edge.

Compare that number against your printer's stated unprintable border:

| Printer type | Typical unprintable border | Clears 11.8mm? |
|---|---|---|
| Office laser / MFP (HP, Brother, Canon, Xerox) | 4–6mm all round | yes, with room to spare |
| Inkjet, sides and top | 3–5mm | yes |
| Inkjet, **bottom** (roller clearance) | 5mm typical, up to ~14mm on some Epson/HP models | mostly, but check yours |

A university printing exam sheets in bulk is using a laser or an MFP, which this clears comfortably.
The one case to watch is a consumer inkjet with a deep bottom margin. If your printer needs more,
raise `PRINTER_SAFE_MARGIN_MM` in `omr/generator/metrics.py` — everything else (fiducial inset,
content margin, page bottom, header position) derives from it, and preflight will tell you whether
the result still fits. Expect to trade page count for margin.

**Why a clipped fiducial is the worst case, not just a cosmetic one:** the surviving part of a
clipped square is still roughly square, so detection succeeds and returns a centroid that is a few
millimetres off. The homography built from it then skews every coordinate on the sheet — quietly,
on every copy. That's why markers get the extra 2mm, and why preflight refuses rather than warns.

## Layout

```
omr/
  models.py       Small file-workflow dataclasses shared by reader/io/workflows
  contracts/     The shared truth between the sheet generator and the sheet reader.
    geometry.py    Page size, mm <-> px conversion (Section 4.4)
    manifest.py    Manifest schema, version, load + validate
  generator/     Module 1 — everything that MAKES a sheet (Section 4)
    main.py        Entry point: terminal wizard / --config
    config.py      ExamConfig / WrittenQuestionConfig — the professor-facing input (Section 4.1)
    metrics.py     Every millimetre of the sheet, and the geometry derived from it.
                     Edit this to change how the sheet looks; nothing else hardcodes a size.
    flow.py        Which question lands on which page, and where. One continuous
                     placement algorithm for every exam (Sections 4.2-4.4)
    layout.py      Assembles a SheetLayout: the flow, plus markers and identity fields
    pdf_gen.py     Renders the printable PDF from a SheetLayout (ReportLab)
    manifest.py    Builds/saves the template manifest from the same SheetLayout
    generate.py    generate_exam(config, output_dir) -> {pdf_path, manifest_path, manifest}
    preflight.py   Rasterizes the generated PDF and verifies it is machine-readable
    configs/       Worked example exam configs — copy one and edit to make your own
    tests/         test_flow.py (pagination behaviour), test_page_identity.py (per-page
                     identity + page bars), test_preflight.py, test_generator.py,
                     test_main.py, plus test_printed_sheet.py — which rasterizes the
                     real PDF and inspects the pixels
  reader/        Modules 2/3 — scan/PDF loading, fiducial alignment,
                   alignment quality reports, page-index reading, and roll-number/program decoding
  grading/       Modules 4/5 — everything that READS/grades a sheet (Sections 7-8)
    bubbles.py     Bubble measurement: fill_ratio (hard threshold) + ink_density (mean darkness)
    mcq.py         MCQ reading + grading — answered/blank/multiple, with confidence
    written.py     Written-answer grading contracts and safe mock provider
    tests/         Grading correctness, plus test_mcq_confidence.py — imperfect real-world marks
  io/            Roster, answer-key, and result CSV helpers
  workflows/     File-based scan evaluation workflow that composes reader + grading + CSV I/O
    parse.py       Official parser: canonical pages, alignment diagnostics, identity, MCQs,
                     written crops, JSON artifacts
    batch.py       Multi-student PDF parser: page grouping, per-student sheets, review reports
    review.py      Human verification layer: verified_index, audit decisions, verified sheets
    evaluate.py    Older MCQ summary/evaluation workflow retained for compatibility
  verify.py      Round-trips a generated sheet: fills it in, grades it, checks it came back
prototype_eval/  Backward-compatible professor-demo CLI and sample CSVs; implementation is in omr/
data/exams/      Generated PDFs + manifests (gitignored)
```

See [Repository Hygiene](docs/REPOSITORY_HYGIENE.md) for maintained entrypoints,
local-data boundaries, and the recoverable archive of old root experiments.
Existing `eval_output/` files remain in place locally and are now gitignored.

### Why `contracts/` exists

PROJECT_SPEC.md Section 2, principle 1 says the generator and the parser share **one** manifest. That only
holds if there's somewhere for the shared half to live. `omr/contracts/` is that place — page
geometry, the mm→px conversion, and the manifest schema. `omr.generator` and `omr.grading` both
import from it; **nothing in it imports from them**, which is what keeps the Phase 2 scan reader from
needing ReportLab installed to parse a sheet. There's a test (`test_contracts_never_imports_from_generator_or_grading`)
that fails if that direction is ever reversed.

Everything else — bubble radius, option pitch, block positions — is a layout *decision* the generator
makes and publishes **through the manifest**, so the reader learns it at parse time instead of sharing
a constant. `pdf_gen.py` and `manifest.py` both render from the same `SheetLayout` object, and
`grading/mcq.py` and `reader/identity.py` read every coordinate they need back out of the manifest
dict. That's what lets the scan parser reuse `grading/` unchanged after canonicalization.

## Sheet design: why it looks the way it does

Four decisions on the printed sheet exist purely to make it readable by machine. They're easy to
undo by accident, so each one has a test that fails if you do.

**Nothing is ever printed inside a bubble.** Option letters go in a header row above each MCQ
column; roll-number digits go in a label column to the left of each grid. Anything pre-printed
inside a bubble is ink the reader can't distinguish from a student's mark. Measured on the real
generated PDF at 200 DPI, an untouched bubble reads **0.000** against a 0.50 fill threshold. The
earlier design — letters straddling the outline, digits centred inside — read 0.204 (MCQ) and 0.261
(roll), giving away half the usable signal range before a student picked up a pen.

**The reader measures a smaller disc than gets printed.** `bubble_sample_radius_mm` is inset to 72%
of `bubble_radius_mm` so the bubble's own outline stroke is never counted as a fill.

**Every marker owns a quiet zone.** A contour detector finds a marker by isolating a dark square,
so 3.5mm of blank paper is reserved on every side and all content bands are derived from those
keep-outs. Previously the title sat 0.45mm below the top-left marker — close enough for a detector
to merge the two into one blob and compute a wrong corner. The orientation marker gets one too.

**A fifth marker breaks the rotational symmetry.** Four identical corner squares look the same at
0°, 90°, 180° and 270°, so a sheet fed in upside down reads as a valid upright sheet with every
coordinate inverted. A smaller square near the top-left resolves it: whichever corner marker it
sits nearest is the true top-left, at any rotation and any scale.

### Identity on every page

A multi-page sheet is scanned as a loose batch, so every page has to say who it belongs to and
which page it is. The default parser groups by roll identity. Separate page-major and sheet-major
scanner modes are available only when the operator knows the scan order; in those modes, the
page-index bars must confirm the exact selected sequence before continuation pages are attached by
position.

| On every page | Read by | What it's for |
|---|---|---|
| **BTECH** choice + 7 boxes; **MTECH/PHD** choices + shared 5 boxes | a human | Reattaching a page that got separated; resolving a flagged bubble read (Section 6, step 5) |
| **Page-index bars** — one per page, this page's filled solid | the machine | Confirming a batch is a complete sheet in the right order |
| Bubbled roll-number grid (**page 1 only**) | the machine | Roster lookup (Module 3) |

The BTech write-in row is 7 boxes (`2024503`). MTech and PhD share a 5-box row because the selected
program supplies the prefix: `MT25001` and `PHD20301` are written/bubbled as `25001` and `20301`.
The program selector is mandatory for that shared grid; an unselected five-digit grid cannot safely
be classified as MTech or PhD.

The full 0-9 bubble grid is **not** repeated on continuation pages. It would cost ~85mm of every page, and it
would ask a student to bubble the same seven digits two or three more times — each repeat being a
fresh chance to produce a page that *contradicts* page 1, which is a review-queue item rather than
an improvement. Continuation pages repeat only the compact BTECH/MTECH/PHD choices and their write-in
boxes.

The page-index bars are deliberately **bars**, not squares: a marker detector rejects candidates by
squareness, so a 4.0 × 1.8mm rectangle can never be mistaken for the 3.5mm orientation marker
sitting on the same row. The reader measures ink at each bar's coordinate with the same primitive it
uses for a bubble — no new decoder — and "exactly one bar is dark" is the checksum that says the
read is trustworthy.

One more, for the humans:

- **Print at 100% scale**, not "fit to page". The fiducials let the reader recover a uniform scale,
  but there's no reason to make it work harder.

## Grading: what a reading tells you

`read_mcq_responses()` returns more than a letter, because PROJECT_SPEC.md principle 4 says a
low-confidence step queues for a human rather than guessing:

| Field | What it's for |
|---|---|
| `outcome` | `answered` / `blank` / `multiple` — blank and multiple stay distinct (Section 7, step 3) |
| `fill_ratios` | Per option: fraction of pixels below the dark threshold |
| `ink_densities` | Per option: mean darkness. Catches what the hard threshold misses |
| `confidence` | `high` / `medium` / `low` |
| `needs_human_review` | Travels through to the `MCQGrade`, so Module 6 won't auto-send the sheet |
| `review_reason` | Plain-English explanation for the review queue |

`ink_density` exists for one specific failure: a light pencil fill covers the whole bubble but never
gets dark enough to cross the fill threshold, so on `fill_ratio` alone it scores 0.00 — identical to
a student who skipped the question, and the mark is silently lost. Mean darkness still sees it, so
the reading comes back as a flagged blank rather than a confident one. Erasure residue, a
too-close-to-call margin, and multiple fills are flagged the same way.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install .[dev]
smartomr-doctor --data-dir data
```

## Run tests

```bash
pytest
```

Tests live beside the code they cover (`omr/generator/tests/`, `omr/grading/tests/`,
`omr/contracts/tests/`), so each module folder is self-contained.

## Config file format (Section 4.1)

```json
{
  "exam_id": "CS301_MIDSEM_2026A",
  "university_name": "IIIT Delhi",
  "course_code": "CS301",
  "exam_name": "Mid-Semester Examination",
  "exam_type": "midsem",
  "num_mcq": 20,
  "mcq_options": 4,
  "marks_per_mcq": 1,
  "numerical_questions": [
    { "q_no": 21, "max_marks": 2, "digits": 3 }
  ],
  "written_questions": [
    { "q_no": 22, "max_marks": 5, "lines": 2 },
    { "q_no": 23, "max_marks": 5, "lines": 2 },
    { "q_no": 24, "max_marks": 10, "lines": 4 }
  ]
}
```

| Field | Customizable range | What it changes |
|---|---|---|
| `exam_id` | any string | Output filename, printed in the header |
| `university_name` | any string | Printed centered at the top of the sheet |
| `course_code` | any string | Printed in the header |
| `exam_name` | any string | Printed as the sheet title |
| `exam_type` | `quiz` \| `midsem` \| `endsem` | Metadata only right now — doesn't change layout |
| `num_mcq` | any non-negative integer | Number of MCQ rows; layout auto-picks 2/3/4 columns |
| `mcq_options` | 2–6 | Number of bubbles per MCQ (A–B up to A–F); widens each MCQ row |
| `marks_per_mcq` | any number | Marks for a correct MCQ — uniform across all MCQs (see limitations below) |
| `numerical_questions` | list, any length | Non-negative whole-number grids; see below |
| `written_questions` | list, any length | See below |
| `roster_csv` | optional string | Reserved for Phase 2 (roster upload for identity resolution) — not used yet |

Each entry in `numerical_questions` is independent:

| Field | What it does |
|---|---|
| `q_no` | Printed beside the grid; must follow all MCQ question numbers |
| `max_marks` | Stored in the manifest for grading an exact match; not printed beside the question |
| `digits` | 1–8 answer positions. Each position is one vertical column of 0–9 bubbles |

Columns run left to right from the most significant digit to the least significant,
with place-value headings (`Tens`, `Ones` for two digits) but no duplicate handwriting
boxes. Students fill every column: answer `7` uses `07` in a two-digit grid and `007`
in a three-digit grid. The printed example names a digit width present on that page;
one-digit-only pages omit leading-zero guidance.
Four two-digit numerical questions are packed side by side.

Each entry in `written_questions` is independent:

| Field | What it does |
|---|---|
| `q_no` | Printed on the answer box; must follow all MCQ and numerical question numbers |
| `max_marks` | Stored in the manifest for grading; not printed beside the question |
| `lines` | How many ruled lines the answer box gets — the box height scales automatically, so a 2-line short-answer box and a 6-line derivation box come out very different sizes on the same sheet |

There is no fixed count for either section. The layout engine packs MCQs first, vertical numerical
grids second, then written-answer boxes, each sized to its own `lines` value.
Single-type exams print the question-type heading without a section letter.
Mixed exams print consecutive Section A/B/C headings, including on continuation pages.

New numerical grids have labelled digit columns spaced 9 mm apart, with
0-9 labels down the left and no redundant handwriting boxes. One-, two-, and
three-digit questions fit four across; wider questions receive more space. A
question is never split across pages. All newly printed bubbles are 3.5 mm in
diameter, with a 1.26 mm sampling radius published in the manifest.

New sheets use manifest schema v6. The reader still accepts original v4/v5
manifests, including horizontal numerical grids and the older 4 mm bubbles.
Always use the manifest generated with the actual printed PDF; never replace an
old sheet's manifest with a regenerated one. Print a small sample at 100% scale
and check real filled scans before using the new size for a class batch.

### Multi-page exams — how the flow works

**One continuous flow, always.** A cursor walks down page 1, then page 2, and so on. MCQs are placed
first, numerical grids second, and written answers last. Each section continues in the space left on
the current page and breaks only when its next row or answer box does not fit.

So a 10-MCQ + 10-two-liner quiz comes out as:

```
page 1   Section A: Q1-Q10 (two columns)   then Section B: Q11, Q12, Q13
page 2   Section B (continued): Q14-Q20
```

This used to be **three** pages. There were two layout code paths — a "fits on one page" one and a
paginating one — and the paginating one always started the written section on a fresh page. The MCQs
took page 1 and left 101mm of it blank, Section B started on page 2, and Q19–Q20 spilled onto page 3.
Every coordinate assertion passed; what was wrong was *which page* things went on. `flow.py` is now
the only placement algorithm, and `test_flow.py` asserts the two properties that catch that class of
bug: a page break happens only when the next question doesn't fit, and no page is ever empty.

Sections never interleave: all MCQs precede all numerical grids, which precede all written answers.
A page break never splits a numerical grid or a written answer box.

**MCQ column count** (2, 3, or 4) is chosen once for the whole sheet, by running the real flow for
each candidate and keeping the one that needs the fewest pages. Ties go to the *fewest* columns:
more horizontal room per question means more paper between neighbouring bubbles, so a stray pen mark
is less likely to land in another question's read region. 40 MCQs fit on one page in three columns
but not in two, so three wins there.

Every page gets its own four fiducial markers plus an orientation marker (Section 4.3 requires this
on every physical sheet, since each page is deskewed independently at scan time), its own
page-index bars, its own compact BTECH/MTECH/PHD write-in blocks, and a "Page X of N" label.

The manifest reflects all of it: every fiducial / MCQ / numerical / written / page-mark / write-in entry carries
a `"page"` field, and there's a top-level `"num_pages"`. Grading (`grading/mcq.py`) takes one
canonical image *per page* (`{page_no: image}`) rather than a single image, precisely so a multi-page
sheet can never get graded against the wrong page's image by accident.

A single written question whose box is taller than one whole page can't be placed anywhere, and the
generator refuses to guess — see `omr/generator/configs/example_impossible_question.json`:

```
Layout error: written question Q1 asks for 60 lines, but at most 30 fit on an A4 page.
Reduce its line count, or split it into two questions.
```

That's the one case where you should expect a hard stop (Section 2, principle 4 — flag rather than
guess); everything else paginates instead of failing.

### Example configs

All under `omr/generator/configs/`:

- `mcq_then_written_flow.json` — 10 MCQs + 10 two-line written questions -> 2 pages, with Section B
  continuing on page 1 directly under the MCQs. The canonical shape for this generator
- `midsem_cs301.json` — the Section 4.1 example from the spec (20 MCQs + 3 written, 1 page)
- `quiz_short.json` — a bare 10-MCQ quiz, no written section
- `endsem_heavy_written.json` — 8 MCQs + 4 written questions with varying line counts, 1 page
- `mcq_options_6.json` — 15 MCQs with 6 options each (A–F) instead of the usual 4
- `multi_page_20q.json` — 10 MCQs + 10 written questions at 3 lines each -> 2 pages
- `mixed_numerical.json` — MCQ + vertical numerical grids + written answers
- `example_impossible_question.json` — a single question too tall for any page, to show the hard-stop case

## Known Phase 1 limitations

Worth flagging to your professor:

- **Uniform MCQ marks** — `marks_per_mcq` applies to every MCQ; per-question weighting isn't wired up.
- **`exam_type` is metadata only** — stored, but doesn't currently change the layout.
- **No answer key in generated exam configs yet** — `grade_mcq_responses()` takes one, and the
  scan-evaluation workflow can load `answer_key.csv`, but `ExamConfig` still has nowhere to store it.
- **No full question-paper ingestion yet** — written question/rubric metadata can be supplied by CSV
  for grading packets, but `ExamConfig` still stores only layout-level written question settings.
- **PhD roster data is still required for names and email** — the sheet/parser support current
  `PHD` + 5-digit IDs, but public directory entries are not used as an authoritative exam roster.
  Confirm any older four-digit PhD enrollment format before printing for those students.
- **No database** — `Exam`/`Question`/etc. (PROJECT_SPEC.md Section 3) are not wired up yet.
- **Scan evaluation is a local batch workflow, not yet a hosted web service** — `omr.reader`
  writes per-page alignment quality reports, color debug pages, overlays, and review flags, but
  thresholds and the optional digit model still need calibration against a larger real
  printed-sheet dataset.
- **The professor UI is local and single-machine** — it has no authentication, database-backed
  job queue, or hosted multi-user deployment yet. Keep it bound to `127.0.0.1` for controlled pilots.
