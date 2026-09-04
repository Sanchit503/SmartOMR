# SmartOMR — OMR-Based Assessment System — Project Specification

**Context:** BTP project, IIIT Delhi. Covers BTech and MTech students only (no PhD).
**Purpose of this document:** hand this to a coding assistant as persistent project context. Read the whole thing before writing any code — the module boundaries and shared schemas in Sections 3–4 are what make the rest of the pipeline work.

---

## 1. What we're building

An end-to-end pipeline for exams that mix MCQs and short (2-line) written answers, on the same OMR sheet:

1. Professor configures an exam (course, exam type, number of MCQs, number/marks of written questions, roster, answer key, marking rubric).
2. System **generates** a printable OMR sheet for that exam.
3. Sheet gets printed, students fill it by hand, professor collects them.
4. Sheets are **scanned** (flatbed/sheet-fed scanner) or **photographed** (phone camera) — the system must handle both.
5. System **identifies** which student each sheet belongs to (bubbled roll number) and **emails them the scanned copy** so they can flag if it's not theirs or something looks wrong — this happens *before* grading.
6. System **grades MCQs** automatically against the answer key, and **grades written answers** using a vision-capable LLM against a professor-supplied rubric.
7. System **auto-emails tentative marks** to each student. No professor-approval gate before sending — that's a deliberate design choice (see Section 9). Students who disagree file a **physical re-evaluation request**; the system just needs to log that a request came in.

---

## 2. Core design principles

These five decisions shape every module below. Don't deviate from them without a good reason:

1. **Generator and parser share one template manifest.** The professor's "quiz has 10 MCQs, midsem has 20 MCQs + 5 written" requirement means sheet layout changes per exam. Nothing about bubble/box positions can be hardcoded in the parser — the generator must emit a machine-readable manifest (JSON) describing exact coordinates of every bubble and answer box, and the parser reads that manifest per-exam. This is what makes "generate custom OMRs for different things" actually tractable.
2. **Identity resolution is roster-based, never pattern-derived.** Don't try to algorithmically compute a student's email from their bubbled roll number. Maintain a roster (`roll_number → name → program → email`) uploaded once per course, and look the bubbled roll number up against it. (See the flagged inconsistency in the chat — this is exactly the kind of bug a hardcoded pattern would cause silently.)
3. **Scanner and phone camera converge to one code path.** Both inputs get corrected to a canonical, deskewed image via fiducial markers *before* any bubble-reading or cropping happens. Nothing downstream of that step needs to know or care which capture method was used.
4. **Every low-confidence step queues for a human, it doesn't guess.** Ambiguous bubble reads, unmatched roll numbers, illegible handwriting — all of these get flagged into a review queue for the professor/TA rather than silently producing a number. This matters more here than in typical software because the output is a grade.
5. **Two separate email touchpoints, not one.** Verification email (right after scanning, before grading) and tentative-marks email (after grading) are different events with different purposes — don't collapse them into a single send.

---

## 3. Data model

```
Student
  roll_number      (PK, string — see Section 4.2 for format)
  name
  program           BTECH | MTECH
  email

Exam
  exam_id           (PK)
  course_code
  name
  exam_type         quiz | midsem | endsem
  config_json       (the generator input, Section 4.1)
  manifest_json     (the generator output, Section 4.1)

Question
  id                (PK)
  exam_id           (FK)
  q_no
  type              mcq | written
  max_marks
  mcq_correct_option   (nullable, only for type=mcq)
  model_answer_rubric  (nullable, only for type=written — free text from professor)
  question_text        (from the ingested question paper, used for written LLM grading context)

ScannedSheet
  id                (PK)
  exam_id           (FK)
  source            scanner | phone
  raw_image_path
  canonical_image_path      (after perspective correction)
  matched_roll_number       (nullable until resolved)
  match_confidence
  status            pending_match | matched | needs_review | verified | disputed

Response
  id                (PK)
  sheet_id          (FK)
  question_id       (FK)
  mcq_selected_option   (nullable)
  written_crop_path     (nullable, only for written questions)

Grade
  id                (PK)
  response_id       (FK)
  marks_awarded
  method            auto_mcq | llm_written | manual_override
  justification     (LLM's reasoning, for written)
  confidence         high | medium | low
  needs_human_review boolean

ReEvalRequest
  id                (PK)
  roll_number
  exam_id
  requested_at
  status            open | resolved
```

---

## 4. Module 1 — OMR Sheet Generator

### 4.1 Input / output contract

**Input** (what the professor provides — matches "just give it numbers"):

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
  "written_questions": [
    { "q_no": 21, "max_marks": 5, "lines": 2 },
    { "q_no": 22, "max_marks": 5, "lines": 2 },
    { "q_no": 23, "max_marks": 10, "lines": 4 }
  ],
  "roster_csv": "roster_cs301_2026.csv"
}
```

**Output:** a printable PDF, **plus** a template manifest JSON (see 4.4) — always generate both together, never just the PDF.

### 4.2 The roll-number block — handling BTech vs MTech

BTech roll numbers are 7 numeric digits (e.g., `2024503`). MTech roll numbers are a fixed 2-letter prefix + 5 digits (e.g., `MT25001`). Don't try to build one alphanumeric bubble grid that handles both — that's unnecessarily hard to print and read. Instead:

- One **program-selector bubble pair**: `BTECH` / `MTECH`.
- A **7-column all-numeric digit grid** (0–9 per column) for BTech.
- A **5-column all-numeric digit grid** (0–9 per column) for MTech (the `MT` prefix is implied by the program bubble, not bubbled).

Print both grids on every sheet; the parser only reads the grid matching whichever program bubble is filled. This keeps every bubble a simple 0–9 digit bubble — no letter-bubbling anywhere, which keeps both generation and OMR reading standard and reliable.

Parsing reconstructs the full roll number (`2024503` or `MT25001`) and looks it up in the roster — it does **not** attempt to derive an email from it directly (Section 2, principle 2).

### 4.3 Fiducial markers

Print four solid black square markers, one in each corner, on every sheet, regardless of content. These are what let Module 2 correct for both a flatbed scan (near-perfect already) and a phone photo (skewed, rotated, unevenly lit) using the same code path. Don't skip these even though scanner input might not strictly need them — consistency here is what avoids two parallel parsing pipelines.

### 4.4 Template manifest (generator output, parser input)

```json
{
  "exam_id": "CS301_MIDSEM_2026A",
  "page": { "width_mm": 210, "height_mm": 297 },
  "fiducials": [
    { "corner": "TL", "x_mm": 10, "y_mm": 10 },
    { "corner": "TR", "x_mm": 200, "y_mm": 10 },
    { "corner": "BL", "x_mm": 10, "y_mm": 287 },
    { "corner": "BR", "x_mm": 200, "y_mm": 287 }
  ],
  "roll_number_block": {
    "program_selector": { "BTECH": { "x_mm": 20, "y_mm": 25 }, "MTECH": { "x_mm": 35, "y_mm": 25 } },
    "btech_digits":  { "columns": 7, "x_mm": 20, "y_mm": 32, "col_pitch_mm": 8, "row_pitch_mm": 6 },
    "mtech_digits":  { "columns": 5, "x_mm": 20, "y_mm": 70, "col_pitch_mm": 8, "row_pitch_mm": 6 }
  },
  "mcq_block": [
    { "q_no": 1, "x_mm": 20, "y_mm": 110, "options": ["A", "B", "C", "D"] }
  ],
  "written_block": [
    { "q_no": 21, "x_mm": 20, "y_mm": 220, "width_mm": 170, "height_mm": 18, "max_marks": 5 }
  ]
}
```

Use **millimeters tied to a known page size**, not pixels — pixel coordinates only make sense after you've fixed a scan DPI, and you want this manifest to be resolution-independent. Convert to pixels at parse time based on the canonical image's known output size.

**Libraries:** ReportLab or fpdf2 for the PDF — both give you exact coordinate placement, which you need since the manifest must match the print output pixel-for-pixel (well, mm-for-mm).

---

## 5. Module 2 — Scan ingestion & canonicalization

Accepts either:
- A batch scan (multi-page PDF or folder of images from a scanner) — split into one image per sheet.
- Individual phone photos (single or bulk upload).

Pipeline (same for both, this is the convergence point from principle 3):

1. Detect the four fiducial markers (contour/corner detection).
2. Compute a perspective transform from detected marker positions to the canonical page rectangle (`cv2.getPerspectiveTransform` + `warpPerspective` if using OpenCV).
3. Deskew and normalize to a fixed canonical resolution.
4. Run a post-alignment quality gate on the canonical image: re-check marker darkness/position, page-index bar contrast, printed bubble anchor residuals, blur, and page size. Save an alignment JSON report plus an overlay image so a TA can inspect failures quickly.
5. From here on, every downstream module works only with canonical images and manifest mm→px coordinates. It never needs to know if the source was a scanner or a phone.

If fiducials can't be reliably detected (torn corner, extreme blur), or if the page warps but fails the quality gate, flag the sheet `needs_review` rather than guessing a transform.

For sheet-fed scanner bundles, default to automatic grouping. The system first reads the page-index
bars and infers the clean scanner order:
- `page-major`: all page 1s first, then all page 2s, etc. (`A1 B1 C1 A2 B2 C2`).
- `sheet-major`: each student's full sheet stays together (`A1 A2 B1 B2 C1 C2`).
- `write-in-similarity`: irregular order, but continuation-page boxed roll handwriting visually
  matches a confident page-1 boxed roll.
- `identity`: fallback mode; every page is attached only when its own roll identity is read confidently.

Automatic positional grouping still depends on page-1 identity. The kth continuation page is
attached only to the kth confident page-1 roll in a fully verified sequence. Similarity grouping
uses a one-to-one best-score assignment between fixed roll boxes and keeps close matches in review.
If order, similarity, and identity checks all fail, those pages stay in review.

The current file-based workflow also writes `review_report.csv` and `review_report.html` beside
`parse_index.json`. These are the local version of the future review queue: they show ready
students, needs-review students, unmatched pages, page errors, generated `sheet.pdf` links, and the
exact flags that must be cleared before emailing or final grading.

The human verification step writes a separate `verified_index.json`; it does not rewrite the raw
parser output. A reviewer can verify a student, reject a grouping, keep a student on hold, assign an
unmatched continuation page to a roll number, or ignore a stray page. Only rows marked `verified`
with a generated verified sheet become eligible for later email/grading automation.

---

## 6. Module 3 — Identity resolution & verification email

1. At the manifest's roll-number-block coordinates, read the program-selector bubble, then read the corresponding digit grid (fill-ratio thresholding per bubble — a filled bubble has much higher dark-pixel ratio than an empty one).
2. Reconstruct the roll number string.
3. Look it up against the uploaded roster.
4. **Match found, high confidence:** mark `matched`, proceed.
5. **No match / ambiguous digits (e.g., two bubbles filled in one column, or none):** mark `needs_review`, surface the sheet image plus your best-guess candidates to the professor/TA for manual resolution. Never auto-assign a low-confidence guess.
6. **Immediately on match** (this is independent of grading — do this step even before MCQ/written grading runs): email the canonical scanned image to the matched student's roster email. Short, clear message: this is the sheet matched to you for [exam name]; reply / contact [TA] within [window] if this isn't yours or something looks off.

---

## 7. Module 4 — MCQ auto-grading

Straightforward once Module 5/6 give you canonical images and manifest coordinates:

1. For each MCQ, read fill ratio at each option's bubble coordinates.
2. Exactly one bubble above threshold → that's the answer, compare to `mcq_correct_option`.
3. Zero bubbles or multiple bubbles above threshold → treat as invalid/blank (0 marks), but log it distinctly from "answered wrong" so the professor can spot scanning issues vs. genuine blanks.

---

## 8. Module 5 — Written-answer grading (LLM-assisted)

1. Crop each written answer region using the manifest's `written_block` coordinates from the canonical image.
2. Export a manual written-grading packet for verified students: crop links, question text,
   rubric/model answer metadata, OCR text/confidence when available, `manual_marks_template.csv`,
   and a browser review page. Import professor/TA marks only after validating
   `0 <= marks_awarded <= max_marks`.
3. Store written grades separately from raw parser output in structured JSON/CSV, then produce a
   combined `final_scores.csv` with MCQ + written totals.
4. Written grading now goes through `omr.grading.written.WrittenGrader`. The as-built provider is
   a deterministic `mock` provider for workflow testing; it defaults to `needs_human_review` and
   must not be used as real marks.
5. For AI-assisted grading, add a real provider behind that interface and call a vision-capable
   LLM with: the question text (from the ingested question paper), the max marks, the professor's
   rubric/model answer, and the cropped image.
6. Require **structured JSON output** — don't parse free text.

**Example prompt (adapt to whichever vision-capable LLM provider you use; most accept an image + text prompt in broadly the same shape):**

```
SYSTEM:
You are grading a short handwritten exam answer (max {lines} lines) for a college
course. You will be given the question, the maximum marks, a marking guideline, and
an image of the student's handwritten response. Transcribe the handwriting as
accurately as you can; mark genuinely illegible parts as [illegible]. Grade strictly
against the marking guideline only — do not award marks for correct-but-unrequested
content. Return ONLY valid JSON matching this schema, no other text:

{
  "transcribed_answer": string,
  "marks_awarded": number,
  "max_marks": number,
  "justification": string,   // 1-2 sentences
  "confidence": "high" | "medium" | "low",
  "needs_human_review": boolean   // true if confidence is "low" or handwriting is
                                   // substantially illegible
}

USER:
Question: {question_text}
Max marks: {max_marks}
Marking guideline: {rubric_text}
[attached: cropped handwritten-answer image]
```

7. Anything returned with `needs_human_review: true` (or a parse failure) goes into the same review queue as Module 3/4's flagged items — don't silently accept it.
8. Every provider must emit the same grade-record shape as the manual marks importer: transcript,
   marks, max marks, justification, confidence, provider/method, review flags, and final status.
   That keeps `written_grades.json`, `written_grades_report.csv`, `final_scores.csv`, and
   `written_review.html` stable across manual, mock, and future LLM grading.

---

## 9. Module 6 — Tentative marks email & re-evaluation logging

- Once both MCQ and written grades exist for a sheet (and nothing on it is `needs_human_review`), auto-send an email: per-question breakdown, MCQ score, written scores with brief justification text, and total — clearly labeled **"Tentative marks — AI-assisted grading for written answers."**
- Include re-evaluation instructions (the physical process itself is outside the system, per your professor's design). What the system *should* do: include a link/button in the email that logs a `ReEvalRequest` row (roll number, exam, timestamp) so the professor has a queue of who to expect, rather than relying on memory or word-of-mouth. This is a small addition but it's the one piece of digital record-keeping for what's otherwise a manual process — worth building even though it's minor, since it was called out as the main point of the auto-send design.
- Sheets with any `needs_human_review` grade should **not** auto-send — they wait in the review queue until a human resolves the flagged item, then send.

---

## 10. Admin / professor panel

Minimum surface for the professor/TA:
- Create/configure an exam (Section 4.1 config, upload question paper, upload rubric per written question, upload roster CSV).
- Trigger sheet generation → download PDF for printing.
- Upload a scan batch, watch it process.
- **Review queue**: every `needs_review` sheet (identity) and every `needs_human_review` grade (written), shown with the source image, for one-click resolution.
- **Re-eval queue**: list of open `ReEvalRequest`s.
- Export final marks (CSV) once satisfied.

---

## 11. Suggested tech stack

Architecture above is language-agnostic, but for a BTP with this CV + ML surface area:

- **Backend:** Python (FastAPI or Django) — best ecosystem overlap with the image-processing and LLM-client work.
- **Image processing:** OpenCV + NumPy.
- **PDF generation:** ReportLab or fpdf2. **PDF/scan splitting:** PyMuPDF (`fitz`) or `pdf2image`.
- **Database:** PostgreSQL (SQLite is fine to prototype with).
- **LLM grading:** Anthropic/OpenAI/Google Python SDK behind the provider-agnostic interface from Section 8.
- **Email:** use a transactional email API (e.g., SES, SendGrid) or the institute's SMTP relay if one exists, rather than a personal Gmail account — you'll be sending to many `@iiitd.ac.in` addresses in a short window, and personal-account bulk sending gets rate-limited or spam-flagged fast. Rate-limit your own sends regardless.
- **Admin panel frontend:** a simple React/Next.js app, or even a server-rendered admin UI if you want to move faster — this is the lowest-risk part of the system to build with the plainest tools available.

### 11.1 As-built code layout

The repo is organized by pipeline module, so code is findable by the section number above:

```
omr/
  models.py     Plain dataclasses shared by the current file-based reader/io/workflow
                code. These are not the future database models from Section 3.
  contracts/   The ONLY thing the generator and the reader share: page geometry,
               mm<->px conversion, and the manifest schema (Sections 4.4, principle 1).
               Nothing here imports from generator/ or grading/ — that direction is
               enforced by a test, so the Phase 2 scan reader never has to depend on
               the PDF-generation stack.
  generator/   Module 1 (Section 4). Self-contained, and split by responsibility:
                 config.py     what a professor supplies (Section 4.1)
                 metrics.py    every millimetre of the sheet + derived keep-out
                               geometry. The file to edit to change how it looks.
                 flow.py       which question lands on which page, and where
                 layout.py     assembles a SheetLayout from the flow + markers +
                               identity fields
                 pdf_gen.py    what gets printed        manifest.py  what's published
                 preflight.py  rasterizes the result and proves it is readable
                 main.py       entry point (terminal wizard / --config)
               Run it with `python -m omr.generator.main`.
  grading/     Modules 4/5 (Sections 7-8). Bubble reading + MCQ grading; written-answer
               grading contracts and safe mock provider.
  reader/      Modules 2/3 prototype (Sections 5-6): scan/PDF loading,
               fiducial alignment to canonical A4, alignment quality reports,
               page-index reading, and roll-number/program decoding.
  io/          Roster CSV, answer-key CSV, and result CSV helpers.
  workflows/  File-based workflows that compose contracts + reader + grading + I/O,
               currently parse.py for one-sheet parsing, batch.py for
               multi-student PDFs, review.py for human verification artifacts,
               written.py for written marks packets/import, and evaluate.py
               for the older MCQ summary professor-demo flow.

prototype_eval/
  Backward-compatible demo CLI, samples, and import shims. Real implementation
  lives under omr/ now; do not add new production code here.
```

Bubble radius, option pitch, and block positions are **layout decisions the generator owns**
and publishes through the manifest — they are deliberately NOT in `contracts/`, so the reader
learns them at parse time rather than sharing a constant that could drift.

**Sheet flow (settled, don't undo):** questions flow continuously — Section A (MCQs) first,
then Section B (written) starting in whatever space is left on the same page, breaking to a
new page only when the next question does not fit. There is exactly one placement algorithm
(`flow.py`) for every exam, because two of them disagreed and produced half-empty pages.

**Identity per page (settled):** every page carries compact BTECH and MTECH identity blocks:
BTECH has 7 write-in boxes; MTECH has 5 because the `MT` prefix is implied by the selector. It also
has a row of page-index bars with its own index printed solid. The *bubbled* roll-number
grid is on page 1 only — repeating it costs ~85mm a page and invites a continuation page that
contradicts page 1. The manifest schema enforces both (every page must have a roll-number
field, exactly one filled page bar at its own index, and at least one question).

---

## 12. Build roadmap — 4 phases

Sized so each phase is independently testable before moving on.

**Phase 1 — Generator + pure MCQ pipeline (no real scanning yet)**
Build the generator (4.1–4.4) and MCQ grading (Module 7). Test by programmatically drawing "filled" bubbles onto a generated sheet in code — you don't need real students or a scanner to validate this phase. Get the manifest-driven approach solid here; everything else depends on it.

**Phase 2 — Real scan ingestion**
Module 2 (canonicalization) and Module 3 (identity resolution + verification email). Test with a handful of real printed-and-filled sheets, both scanned and phone-photographed, to validate the fiducial/perspective-correction path handles both.

**Phase 3 — Written-answer grading**
Module 5: cropping + LLM integration. Test against a small batch of real handwritten answers before trusting it on a full class.

**Phase 4 — Marks email, re-eval logging, admin panel polish**
Module 6 + Module 10 + hardening the edge cases in Section 13.

---

## 13. Edge cases to explicitly handle (don't discover these in production)

- Multiple bubbles filled in one roll-number column, or one MCQ.
- No bubbles filled (blank vs. scanning miss — log distinctly).
- Fiducial marker obscured (torn/folded corner, thumb over it in a phone photo).
- Roll number bubbled but doesn't exist in the roster (typo by student, or roster out of date).
- Two different students matched to the same roll number read (shouldn't happen with roster lookup, but validate uniqueness).
- Handwriting the LLM marks fully illegible.
- A student's sheet has smudged/erased-and-rebubbled answers.
- Duplicate scan of the same sheet uploaded twice.

---

## 14. Open items to confirm with your professor before Phase 2

- The real roll-number → email rule (Section 2 flagged an inconsistency in the two examples given — resolve this before generating the actual roster CSV).
- Which LLM provider/API access you'll have for Module 5 (affects Section 11's grading client).
- Whether multiple exam "sets" (shuffled question order, for anti-cheating) are needed — not in this MVP, but worth asking now since it changes the manifest and generator if needed later.
- Deployment target: professor's own machine, a college server, or cloud — affects nothing in Sections 4–9, only Section 10's deployment.
