# SmartOMR: Detailed Engineering Handoff

**Housekeeping update (2026-09-22):** The root experiment inventory below is
historical. Those scripts and their associated artifacts were archived locally;
application modules and existing exam outputs were retained. Read
[Repository Hygiene](REPOSITORY_HYGIENE.md) for locations and recovery details.
Continue to inspect the current source and Git status rather than assuming this
snapshot describes today's working tree.

Snapshot date: **2026-09-20**. Original workspace: **D:\SmartOMR**.

This document transfers project context to the next engineer or AI. It describes
the local workspace, not just the last GitHub commit. It must be read alongside
the source and tests. It is not a production-readiness certificate.

### Navigation

- [Immediate professor request and accepted layout](#2-immediate-task-and-latest-user-preferences)
- [Environment](#3-repository-and-environment-snapshot) and [uncommitted work](#4-critical-local-work-is-not-all-committed)
- [Module map](#6-module-map-and-reading-order) and [generator contract](#7-generator-contract-in-detail)
- [Whole-class processing](#8-what-processing-a-whole-class-pdf-means) and [grouping](#10-current-identity-and-grouping-logic)
- [Known risks](#11-known-local-risks-to-audit-before-real-automation)
- [Objective grading](#12-objective-reading-grading-and-dropped-questions) and [written answers](#13-written-answers-real-current-state)
- [Human verification](#14-artifacts-and-human-verification) and [mailing](#15-mailing-working-path-and-safety-boundary)
- [Tests](#18-tests-and-verification-evidence) and [commands](#19-safe-operating-commands)
- [Future architecture](#20-architecture-and-ocr-research-proposals-only) and [next steps](#21-recommended-sequence-after-onboarding)

## 1. Read This First

SmartOMR is intended for actual college exams at IIIT Delhi, not only a demo.
Its purpose is to generate machine-readable answer sheets, process whole-class
scans, identify students and pages, read and grade objective responses, preserve
written answers for review/grading, and release each student's own sheet and
marks by email.

There was a serious real-world failure: a 300+ page run produced unreliable roll
recognition and incorrect student-page combinations. The user and their partner
had to do the grouping and grading manually. They subsequently used the mailing
workflow with manually prepared PDFs and corrected marks. The user reported
mailing was completed for 149 students. This handoff has not re-audited those
deliveries or certified the contents of the historical PDFs.

The correct response is incremental engineering with inspectable evidence, not
a promise that another OCR model will make everything work. A wrong attachment
sent to a student is a privacy incident as well as an academic correctness issue.
An unresolved page is preferable to a confidently wrong student assignment.

### Evidence Categories

- **Current code:** behavior inspected in this local workspace. Some of it is
  uncommitted and differs from GitHub.
- **Automated checks:** unit/integration tests and generated-sheet readback.
  These do not establish real handwriting accuracy or zero wrong groupings.
- **User-reported history:** incidents, other-machine setup, and completed mail.
  Do not present these as independently reproduced measurements.
- **Proposal:** architecture/OCR research, not necessarily implemented or tested.

When documentation and executable code disagree, explain the discrepancy and
inspect the code/test contract. Do not silently choose whichever is convenient.

## 2. Immediate Task And Latest User Preferences

The professor's latest message was:

> Tarandeep and Sanchit: I have 15 questions in the midterm. Prepare an OMR sheet
> for that.

Only the total count and exam category are known. A clarification email was
requested asking for:

- Counts of MCQ, numerical, and written questions.
- MCQ option count and marks.
- Maximum digits for each numerical question and whether negative/decimal
  answers are required. The present implementation supports neither.
- Space/line count and marks for each written question.
- Exact question numbering and order.
- Course code, exam title, and other final sheet metadata.

No reply specifying those details is available in this handoff. Do not generate
the final midterm by inventing a distribution such as 15 numerical questions.
The user wants the next AI to understand the project first, then await direction.

### Latest Accepted Generator Direction

1. Numerical answer choices must be arranged vertically.
2. Bubbles were considered too large; new sheets use 3.5 mm nominal diameter.
3. Fit four two-digit questions across; do not squeeze five across.
4. Remove numerical Ones/Tens/Hundreds headings.
5. Remove numerical digit handwriting boxes; numerical answers are bubbled.
6. Reduce digit-column gaps slightly after removing headings, but maintain
   separation between questions.
7. Retain readable, useful identity fields and machine registration marks.
8. Do not print per-question marks on MCQ, numerical, or written labels. Keep
   marks in the configuration and manifest for grading and total calculation.

The local implementation follows that direction. Do not confuse the numerical
write-in boxes that were removed with roll-number write-in boxes, which remain
essential on every page.

### Latest Preview Versus Old IDE Tab

Current preview, if the original ignored data folder is available:

```text
data/layout_previews/vertical_no_labels_20260919/NUMERICAL_8_TWO_DIGIT.pdf
data/layout_previews/vertical_no_labels_20260919/NUMERICAL_8_TWO_DIGIT.manifest.json
data/layout_previews/vertical_no_labels_20260919/NUMERICAL_ALL_WIDTHS.pdf
```

The all-widths preview covers one through eight digit positions and is two pages.
Associated raster previews were generated locally. The old IDE tab points at
`vertical_35mm_20260919/NUMERICAL_8_TWO_DIGIT.pdf`, which retains the earlier
place-value labels and spacing. It is not the newest output.

## 3. Repository And Environment Snapshot

| Item | Observed state |
| --- | --- |
| Workspace | `D:\SmartOMR` |
| Shell | Windows PowerShell |
| Repository | `https://github.com/Sanchit503/SmartOMR.git` |
| Local HEAD | `2281e5f` |
| HEAD subject | `add safe folder-based email release workflow` |
| Package | `smartomr` version `0.3.0` |
| Declared Python support | `>=3.11` |
| Current workspace interpreter | `.venv\Scripts\python.exe`, Python `3.14.2` |
| Application storage | Primarily local files, JSON, CSV, images, PDFs |
| UI server | Python standard-library HTTP server, not FastAPI/Django |

No fetch or pull was performed for this handoff. Therefore HEAD is a local
observation, not a claim that it equals the current remote tip.

Recent commits preceding HEAD include:

```text
bbac906 harden large-batch processing and dropped-question handling
c6e98ac harden page-one roll OCR cross-checks
38bb00c harden batch review and roster email workflow
ba966ae EMAIL AND OTHER THINGS
```

### Dependencies

Core dependencies in `pyproject.toml` are NumPy, headless OpenCV, Pillow,
Pydantic, PyMuPDF, pypdf, pytesseract, and ReportLab. The `dev` extra adds pytest.
The `htr` extra adds torch, Transformers `>=4.40,<5`, sentencepiece, protobuf,
and tiktoken. The `ml` extra adds scikit-learn and joblib.

These are broad dependency ranges, not a complete reproducible lockfile.
`pytesseract` is a Python wrapper; installing it alone does not install the
Tesseract executable. Use the project health check for the actual machine.
Do not assume optional GPU/HTR packages are present just because the core tests
pass. Do not overwrite this environment with another Python version casually.

### Another Laptop: User-Reported, Not This Environment

The Victus machine was ultimately reported as an RTX 4050 Laptop GPU with 6 GB
VRAM and 16 GB system RAM, not the initially guessed RTX 4060. Its setup report
listed Conda Python 3.11.16, PyTorch 2.11.0+cu128, torchvision 0.26.0+cu128, and
Transformers 4.57.6. CUDA availability and a tensor operation reportedly passed.

`microsoft/trocr-base-handwritten` reportedly loaded on that GPU with roughly
334 million parameters and about 1.285 GB peak allocated GPU memory. That was
**model loading only**: no handwriting inference/accuracy benchmark was produced
because pilot input was missing. A successful model load proves neither answer
quality nor full-exam throughput. The first measurement script attempt also
failed before loading, then the measurement harness was corrected.

The initial written-answer pilot target was short English answers, sometimes
with numbers or formulas. College hardware may be stronger, but no final
deployment specification has been confirmed.

## 4. Critical: Local Work Is Not All Committed

The following is the pre-handoff dirty-work snapshot. The two new handoff
documents themselves are additional untracked files created by this task.

### Tracked Files Already Modified

```text
PROJECT_SPEC.md
README.md
omr/contracts/geometry.py
omr/contracts/manifest.py
omr/contracts/tests/test_contracts.py
omr/datasets/bubble_crops.py
omr/generator/flow.py
omr/generator/manifest.py
omr/generator/metrics.py
omr/generator/pdf_gen.py
omr/generator/tests/test_generator.py
omr/generator/tests/test_printed_sheet.py
omr/reader/handwriting.py
omr/reader/identity.py
omr/reader/numerical.py
omr/reader/tests/test_handwriting.py
omr/reader/tests/test_numerical.py
omr/ui/app.py
omr/verify.py
omr/workflows/batch.py
omr/workflows/tests/test_batch.py
```

These changes do not constitute one homogeneous, validated feature. Generator,
geometry, manifest, and numerical tests include the recent layout work. Other
local handwriting, grouping/cache, and UI changes predate that work. Preserve
them and inspect their diffs; do not assume the generator work validated them.

### Untracked Files And Directories Already Present

```text
.git_diff.txt
benchmark_crops.csv
digit_cnn.pth
docs/OMR_DIMENSIONS.md
docs/RELIABILITY_ARCHITECTURE.md
eval_output/
extract_crops.py
extract_crops2.py
extract_crops3.py
extract_crops4.py
extract_data.py
extract_data2.py
finetune_cnn.py
omr/generator/tests/test_numerical_layout.py
omr/reader/cnn_ocr.py
omr/reader/tests/fixtures/
patch2.py
patch3.py
patch_batch.py
patch_batch_similarity.py
patch_handwriting.py
patch_tests.py
pytorch_ocr.py
run_benchmark.ps1
run_benchmark.py
run_benchmark2.ps1
smartomr_cnn.pth
smartomr_crops/
test_trocr.py
train_cnn.py
train_cnn2.py
train_cnn3.py
training_labels.csv
```

Do not run these root-level patch/training scripts during onboarding. Inspect
them as experimental artifacts first. Crops, CSVs, outputs, and model files may
be private, large, or dependent on undocumented local paths.

`omr/reader/tests/fixtures/numerical_v5/` is different: it contains a synthetic
legacy horizontal PDF/manifest plus README for regression testing. Preserve
that fixture; generating it again with the current generator would defeat its
purpose. It is not a real student submission.

### Transfer Rules

`data/`, scans, virtual environments, caches, logs, and environment-secret files
are ignored by Git. Some root experimental files above are not ignored.

- A clone gets committed source, not this dirty source snapshot.
- A handoff document explains missing work but does not transfer it.
- Transfer intended source through a deliberately reviewed commit or private
  source patch/archive. Include needed new tests and synthetic fixtures.
- Transfer necessary real data separately and privately with authorization.
- Never use `git add .` for this workspace without inspecting every path.
- Do not commit student lists, filled sheets, credentials, or email releases.
- Do not reset/clean/stash/discard the user's work to obtain a clean checkout.

No commit or push is requested by the current handoff task.

## 5. Implemented Capabilities Versus Limits

| Area | Current capability | Important limit |
| --- | --- | --- |
| Generation | A4 PDF plus matching geometric manifest | Latest geometry is local/uncommitted |
| MCQs | Single-choice bubble reading and key-based grading | Not a general multi-select question engine |
| Numerical | 1-8 digit non-negative integer grids; blank/uncertain states | No signed values, decimals, or algebraic parsing |
| Written | Manifest-based answer crops, optional OCR, review/mark import | No validated automatic academic grader |
| Alignment | Corner/orientation/page-mark processing and quality artifacts | Not immune to curl, clipping, blur, or wrong templates |
| Identity | Page-1 bubbles plus optional handwritten cross-check | Handwriting accuracy is not established |
| Grouping | Exact identity grouping, incomplete/duplicate review | Does not guarantee correct ownership on real scans |
| Review | Separate verification state and reviewed PDFs | Requires meaningful human evidence, not bulk approval |
| Email | Frozen releases, previews, SMTP, test redirect, logs | Requires correct approved PDFs and authorized credentials |
| UI | Local run upload, progress, review, workflow controls | Not hardened multi-user college hosting |

The architecture/research documents discuss more ambitious capabilities. They
must not be described as working code merely because they appear in a spec.

## 6. Module Map And Reading Order

Read all files within these areas, not just the named entry points. Tests are
part of the contract and often reveal safety behavior absent from the README.

### 6.1 Contracts And Records

| File | Responsibility |
| --- | --- |
| `omr/models.py` | File-workflow dataclasses; not a database ORM |
| `omr/contracts/geometry.py` | Units, canonical sizes, bubble/digit-grid coordinate helpers |
| `omr/contracts/manifest.py` | Manifest loading/validation and supported schema versions |
| `omr/local_registration.py` | Bounded local registration used by readers |
| `omr/io/csv.py` | Roster, answer-key, rubric parsing and roll normalization |
| `omr/io/tabular.py` | CSV/XLSX table input helpers |

Contracts are a shared lower layer. Package-structure tests prevent the manifest
contract from depending on generator/grading implementation. Do not introduce
a circular generator-reader dependency while sharing geometry.

Useful records include `Student`, `AnswerKeyEntry`, `WrittenQuestionMeta`,
`RollRead`, `HandwrittenRollRead`, `AlignedPage`, `ParsedPage`, and `WrittenCrop`.
Read their actual definitions before changing JSON consumers.

`AlignedPage` contains page/source indices, image, alignment confidence,
page-mark confidence, and optional debug image. It does not contain a quality
report field. Quality report/status/overlay paths appear later in parsed-page
artifacts. Do not invent a missing quality-field cache bug from the record name.

### 6.2 Generation

| File | Responsibility |
| --- | --- |
| `generator/config.py` | Pydantic exam/question configuration |
| `generator/metrics.py` | Physical dimensions and layout constants |
| `generator/flow.py` | Packing, MCQ columns, section flow, pagination |
| `generator/layout.py` | Sheet layout, identity fields, fiducials/page markers |
| `generator/pdf_gen.py` | ReportLab drawing, strokes, typography, printed wording |
| `generator/manifest.py` | Serialize the same computed layout for the reader |
| `generator/generate.py` | Produce the PDF/manifest pair |
| `generator/preflight.py` | Rendered-sheet checks |
| `generator/main.py` | Interactive terminal wizard and config-file CLI |
| `generator/configs/*.json` | Eight example inputs, including invalid/edge examples |

The PDF and manifest must come from the same computed layout. Never make a
renderer-only geometry change that leaves the manifest describing old positions.

### 6.3 Reading And Grading

| File | Responsibility |
| --- | --- |
| `reader/scan.py` | Load/rasterize pages; align, orient, identify template page |
| `reader/quality.py` | Alignment-quality measurements, anchor checks, visual overlays |
| `reader/identity.py` | Program and roll-digit bubbles, grid calibration |
| `reader/handwriting.py` | Roll write-in crops, OCR adapters, evidence/candidate selection |
| `reader/digit_model.py` | Local OpenCV KNN boxed-digit model utilities |
| `reader/cnn_ocr.py` | Untracked experimental CNN adapter, not production-integrated |
| `reader/numerical.py` | Manifest-driven numerical digit reading and outcomes |
| `reader/written.py` | Written-answer crops, template/background/ink processing |
| `reader/written_ocr.py` | Tesseract/TrOCR/ensemble written-text adapters and line preparation |
| `grading/bubbles.py` | Bubble ink measurements and low-level sampling |
| `grading/mcq.py` | MCQ calibration, classification/confidence, grading |
| `grading/written.py` | Written-grader protocol, result validation, mock provider |

The roll OCR path and written-answer OCR path are distinct. A loaded TrOCR
written-answer model does not replace the continuation-page roll OCR factory.

### 6.4 Workflows And UI

| File | Responsibility |
| --- | --- |
| `workflows/parse.py` | One-student parsing, artifacts, validation, response results |
| `workflows/batch.py` | Whole-class scans, identity/grouping, per-student outputs, reports |
| `workflows/review.py` | Separate human verification state, decisions, page assignment |
| `workflows/written.py` | Written grading packets, manual mark import, totals, mock grading |
| `workflows/email.py` | Verified-result and folder-based email preparation/sending |
| `workflows/evaluate.py` | Older MCQ evaluation/demo workflow |
| `ui/app.py` | Local HTTP application, run store, worker threads, pages/actions |
| `health.py` | Dependency/environment readiness checks |
| `verify.py` | Generated-manifest bubble-fill/readback verification |

`prototype_eval/` contains compatibility/demo wrappers and sample inputs. New
production logic belongs under `omr/`, not in a second implementation there.

`omr/datasets/` contains bubble-crop extraction, cancelled-bubble dataset tools,
training, and failed-page debugging helpers. Their existence does not mean a
validated cancelled-mark classifier is active in the production reader.

## 7. Generator Contract In Detail

### Question Types And Numbering

`ExamConfig` includes exam ID, university, course code, title, exam type
(`quiz`, `midsem`, `endsem`), MCQ count/options/marks, numerical questions,
written questions, and an optional roster path.

- At least one question is required.
- MCQ count may be zero. Options are globally 2-6; marks per MCQ are global.
- Numerical questions have `q_no`, positive finite `max_marks`, and strict
  integer `digits` from 1 through 8.
- Written questions have `q_no`, `max_marks`, and positive line count.
- MCQs occupy question numbers 1 through `num_mcq`.
- Numerical numbers must follow MCQs and be unique.
- Written numbers must be unique and follow all numerical questions when any
  are present.
- Physical section order is MCQ, then numerical, then written. Arbitrary
  interleaving requested by a professor is not currently a supported input.

Ask for the actual question distribution instead of assuming section defaults
match the professor's paper. Do not broaden scope to negatives/decimals without
designing the printed format, manifest, reader, grading, and review together.

### Geometry And Units

Full detailed measurements are in [OMR_DIMENSIONS.md](OMR_DIMENSIONS.md), checked
against the latest local generator. That file includes fonts, strokes, label
positions, identity fields, section transitions, and digit-width tables.

All manifest geometry is millimetres. Origin is top-left, x rightward and y
downward. Bubble/fiducial coordinates are centers; box coordinates are top-left
edges. PDF fonts and line widths are points. ReportLab uses a different drawing
origin internally, which the renderer converts.

| Element | Current dimensions |
| --- | --- |
| Page | A4 portrait, 210 x 297 mm |
| Main content | x=12..198 mm, width 186 mm |
| Design safe margin | 10 mm |
| Last question-layout coordinate | y=274.5 mm |
| Page-1 identity frame | x=12, y=42.5, width=186, height=76.75 mm |
| Page-1 question band | y=124.25..274.5 mm |
| Continuation identity frame | x=12, y=31, width=186, height=19 mm |
| Continuation question band | y=55..274.5 mm |
| Common bubble radius/diameter | 1.75 / 3.5 mm |
| Published bubble sampling radius | 1.26 mm |
| Bubble outline stroke | 1 pt; outside ink diameter is about 3.853 mm |
| Four corner squares | 7 x 7 mm |
| Corner centers | (15.5,15.5), (194.5,15.5), (15.5,281.5), (194.5,281.5) |
| Orientation square | 3.5 x 3.5 mm, center (29.5,15.5) |
| Page-index bars | 4 x 1.8 mm, 6 mm pitch, y=15.5 mm |
| Written box width | 186 mm |
| Written line spacing | 7 mm |

Nominal bubble path dimensions differ from outside ink dimensions. Sampling
radius is deliberately inside the printed outline. Actual-size printing matters;
printer fitting/scaling can change the physical dimensions.

### Latest Vertical Numerical Layout

Each digit position is a vertical column of 0-9 bubbles. Positions run left to
right from most significant to least significant. For a two-digit answer 07,
the student fills row 0 in the left column and row 7 in the right column.
There are no numerical handwriting cells or place-value headings.

| Property | Value |
| --- | --- |
| Digit-row pitch | 5.2 mm |
| Digit-column pitch | 9 mm, reduced from 10 mm on 2026-09-21 |
| First row center below question allocation top | 8 mm, reduced from 14 mm |
| Minimum question allocation width | 42 mm |
| Allocation width | `max(42, 9 * digits + 9)` mm |
| Question allocation height | 58.55 mm |
| Inter-question horizontal gap | 6 mm |
| Gap between question rows | 5 mm |
| Section heading/instruction allocation | 12 mm |
| Two-digit grid position | Centered within its allocation |

Equal-width capacities across a row are: four questions for 1-3 digits, three
for 4-5 digits, and two for 6-8 digits. For two-digit questions the four allocation
left edges are x=12,60,108,156 mm; first bubble-column centers are x=28.5,76.5,124.5,172.5.

Eight two-digit numerical questions fit on page 1 of a numerical-only exam;
twelve fit on a full continuation page. Other sections consume some of that
space. This is a readable four-across layout, not maximum packing density.
Questions are kept intact across page breaks.

### Identity Layout

Page 1 has BTECH, MTECH, and PHD program selectors. BTech uses seven numeric
positions. MTech and PhD use separate program selectors but share a five-position
numeric grid and write-in field; the program supplies the prefix.

Roll-number handwriting cells remain on all pages. The page-1 roll digit grid
has 10 mm column pitch and 6 mm row pitch. Write-in cells are 7 x 7 mm; page-1
cell pitch is 10 mm and continuation cell pitch is 12 mm.

Typical normalized identities are seven BTech digits, `MT` plus five digits,
and `PHD` plus five digits. Case-insensitive PhD filename normalization does not
mean every possible historical PhD roll format is supported. Confirm real
institutional exceptions instead of guessing. Some IO/OCR code recognizes other
prefixes such as `SP`; that is not evidence of a corresponding generated selector.

The current sheet has **no unique per-booklet identity barcode**. The small top
bars encode only the page index. Printed "Page 1 of 2" text is for people; the
bars are for machine reading. Neither identifies which student owns a page.

### Manifest Compatibility

New local generator output is manifest schema v6. The reader supports v4, v5,
and v6. Legacy v5 horizontal numerical grids must continue using their original
manifests. Numerical sampling, verification, and dataset cropping share geometry
helpers that account for grid orientation.

Never regenerate an old exam manifest from approximate question counts after
the physical sheets were printed with a different layout. Preserve the exact
original PDF/manifest pair. The abbreviated schema example in `PROJECT_SPEC.md`
is explanatory, not a complete valid replacement manifest.

## 8. What Processing A Whole-Class PDF Means

For an exam with two pages per student, 300 PDF pages might represent 150
complete students, but duplicates, blanks, missing pages, and unrelated pages
mean division by two is not proof of that count.

The conceptual sequence should be:

1. Validate the manifest and input readability. Preserve the original scan and
   provenance. Source hashing/versioned immutable jobs are an improvement target,
   not a claim that the current batch cache already implements them.
2. Enumerate source pages with stable source indices.
3. Rasterize, orient, align, and determine the template page index.
4. Assess geometric quality and save inspectable canonical images/overlays.
5. Crop and read identity evidence without discarding contradictory evidence.
6. Resolve page ownership, leaving uncertain/duplicate/missing cases for review.
7. Read objective responses and crop written answers under the correct manifest.
8. Produce reports and candidate student PDFs, then verify ownership/completeness.
9. Grade using the approved key/rubric and resolve uncertain answers.
10. Prepare an immutable email release from approved identities, marks, and PDFs.
11. Preview/test, approve recipients/content/count, then explicitly send.

The current functions combine some of these stages. Do not present this list
as an already implemented durable distributed job architecture.

### Roster: Useful, But Not The First Requirement

The roster is not needed to generate blank sheets, load the PDF, align pages,
or crop fields. The batch API accepts no roster. A roster helps check known
rolls, reconcile missing students, and supply authoritative names/email addresses.
It must not force unreadable ink into the nearest valid student's identity.
Never derive an email address from a roll number or a public profile pattern.

The CSV loader handles common exported roster headings and normalization.
The tabular layer supports CSV and XLSX, reading the first XLSX worksheet via
ZIP/XML. A filename containing `.xls` followed by `.csv` can still be ordinary
CSV. Native binary `.xls` is not the same format and is not covered by this
claim. Duplicate or conflicting records need explicit handling, not silent edits.

## 9. Alignment And Quality: What Is And Is Not Guaranteed

`reader/scan.py` locates registration features and maps the page into canonical
A4 coordinates. Orientation and page-index markers help determine which
manifest page to sample. Quality evaluation and overlays help inspect whether
printed anchors and expected geometry agree after alignment.

A homography can address planar rotation, scale, skew, and perspective. It
cannot perfectly undo arbitrary paper curl, local folds, missing corners,
motion blur, or heavy occlusion. Bounded local registration helps with limited
residual error but is not a universal correction.

Inspect the canonical image, alignment overlay, sampling overlay, and identity
crops before blaming only OCR. A strong OCR model cannot recover the correct
roll if the wrong boxes were cropped. Conversely, good alignment does not make
illegible handwriting unambiguous.

Do not call the system "alignment-proof" or remove quality/review gates to get
more automatic results. Any threshold change needs both error and coverage
measurements on representative real scans.

## 10. Current Identity And Grouping Logic

The intended ownership rule requested by the user is:

- Page 1: read bubbled roll and handwritten roll independently; disagreement
  or uncertain evidence requires review.
- Continuation pages: read their own handwritten roll and match it exactly to a
  sufficiently supported page-1 identity.
- Arrival order must not determine ownership for randomly ordered scans.

### Actual Batch Path

In the dirty local `workflows/batch.py`, `auto` maps directly to `identity`.
It no longer automatically selects positional grouping from scanner order.
Some older inference/similarity helpers remain in the file; follow call sites
rather than assuming every helper is active.

`_page_identity` reads page-1 bubbles. When a roll OCR backend is configured,
it also calls the write-in reader, preserves its payload, and flags unreadable,
low-confidence, or conflicting write-in results. If bubbles yield no roll but
write-in does, a write-in identity can be returned with review evidence.
Continuation pages call the continuation handwritten-roll reader.

`_group_records_by_identity` makes two passes over the available records:

1. It considers all page-1 records first. A sufficiently confident roll may form
   a candidate student group. A roll enters the automatic continuation-anchor
   set only when the identity kind is bubbled and normalized write-in equals
   the bubble roll with at least medium write-in confidence.
2. It considers continuation records. A sufficiently confident handwritten roll
   is attached only if that exact roll has a supported page-1 anchor. Otherwise
   it remains unmatched, with review evidence.

Therefore `C2 A3 B1 C3 A2 A1 C1 B3 B2` can be grouped independently of input order
**if the required identity/page evidence is correct**. This is not a guarantee
that OCR will produce that evidence. A page-2 record arriving before page 1 does
not itself cause failure, because anchors are resolved in a separate pass.

Explicit `page-major` and `sheet-major` overrides still exist for independently
verified ordering. They are not a remedy for random ordering or failed OCR.
Do not restore automatic positional or visual handwriting-similarity matching
as a shortcut to producing complete PDFs.

### Defaults Matter

- CLI/API minimum grouping confidence is normally `high`.
- The dirty local UI currently passes `medium`, unlike the prior `high` value.
- CLI handwritten-roll OCR defaults to `none`; `--handwritten-roll-ocr local`
  is needed to request the local cross-check path.
- Without a write-in backend, page-1 bubbles alone do not create the confirmed
  anchors required by the current identity grouping path for continuation pages.
- Partial outputs are allowed by default; `--strict-complete` changes behavior.

A student directory or PDF being present does not mean the student is verified,
all expected pages are present, or the contents are safe to email.

### Duplicate And Missing Pages

`_write_student_group` tracks expected pages, duplicates, and review flags.
`_best_page` ranks duplicate candidates by confidence/alignment/page-mark scores.
Selecting a candidate does not establish that two conflicting pages belong to
the same person. Human verification must resolve conflicts and missing sheets.
Read the actual resulting JSON and reports rather than assuming a two-page
manifest guarantees a two-page output.

## 11. Known Local Risks To Audit Before Real Automation

These are static observations from the current source, not fixes performed by
the handoff task and not a complete security/code review.

### 11.1 Alignment Cache Has No Input Identity

Local batch additions `_save_aligned_page` and `_load_aligned_page` cache files
under `_page_identity/source_0001/` and similarly numbered directories:

```text
aligned.json
aligned_image.png
aligned_debug.png   (when available)
```

Metadata contains page/source indices and alignment/page-mark confidence. It
does not contain source-PDF hash, manifest hash, DPI, preprocessing version, or
code version. Reusing the same output root with changed input can therefore
reuse a stale aligned image for a different page or geometry.

Until correct invalidation is implemented and tested, use a fresh output root
for a changed input or processing configuration. Preserve old runs as evidence;
do not solve this by indiscriminately deleting `data/`.

The batch loop also retains aligned-page records/images until grouping. Although
PDF pages are iterated, this is not a fully bounded-memory page-at-a-time system.
Measure RAM as well as time before claiming 300+ page scalability.

### 11.2 OCR Confidence And Candidate Filtering

Inspect these paths in `reader/handwriting.py`:

- `_read_roll_from_cells` uses `_first_digit`, which extracts the first digit
  found in text. That is weaker than requiring exactly one unambiguous digit.
- Cell confidence aggregation is not calibrated full-roll correctness.
- `fast_cell_first` may avoid whole-strip OCR after a usable cell result,
  including a roster check when a roster is supplied. Do not describe this as
  two independent recognizers agreeing on every roll.
- Candidate filtering to roster hits occurs before some conflict decisions.
  An out-of-roster disagreement can therefore disappear from the selected
  candidate set even though raw OCR payloads are retained.
- Whole-strip-only high confidence is downgraded and review evidence is added,
  but that does not eliminate all incorrect recognized rolls.
- Character normalization includes lookalike substitutions. A normalized match
  is not equivalent to literal, error-free reading of the original ink.

Local interface changes add `valid_rolls` in places, but the ensemble does not
forward it consistently to children, and the new cell-reader argument is not a
fully integrated sequence-decoder path. Audit callers before extending it.

### 11.3 Experimental CNN Is Not A Validated Replacement

`omr/reader/cnn_ocr.py` is untracked and is not registered in the normal roll OCR
factory. It imports `_expected_digit_count` from `omr.grading.bubbles`, where that
symbol does not exist; the helper is in `reader/handwriting.py`. This is an
observed import inconsistency, not a claim that a full CNN runtime test was run.

It also contains roster-constrained candidate selection. A highest-probability
roster candidate is not sufficient evidence to override contradictory ink.
The existence of `.pth` weights and crop CSVs does not establish writer-disjoint
training/test separation, calibrated rejection, or safe production integration.
Do not run or commit the experimental scripts/weights blindly.

### 11.4 Grade Validation Needs Further Hardening

`grading/written.py` validates ranges and matching question/max marks, but plain
comparisons do not explicitly reject all non-finite floating-point values.
For example, NaN can evade less-than/greater-than range checks. Add explicit
finite-number validation and regression tests before trusting future model
outputs. Do not infer that numeric validation is equally strong in every module
because the numerical answer-key path checks finiteness.

The manual written-mark import in
`workflows/written.py::_apply_manual_mark` also uses ordinary float/range checks
without an explicit finite-number test. Include that path in the same audit;
human-entered spreadsheets are not immune to malformed numeric values. The
folder-mailer's `_positive_mark` does explicitly reject non-finite marks.

### 11.5 Passing Tests Are Not A Field Benchmark

Many identity/HTR workflow tests use fake providers or generated filled sheets.
They verify mechanics and rejection behavior under specified inputs, not the
accuracy of Tesseract, KNN, CNN, or TrOCR on the professor's real handwriting.
No new real-batch ownership benchmark was performed by this handoff task.

## 12. Objective Reading, Grading, And Dropped Questions

### MCQ

The reader measures ink relative to printed bubble geometry, performs local
calibration, and emits answers/confidence/review evidence. Single selections,
blank answers, and ambiguous/multiple marks are distinct outcomes. A multi-mark
row is not silently treated as a valid multi-select question.

### Numerical

The manifest determines position count and orientation. Each position is read
from its 0-9 choices. Outcomes distinguish answered, blank, incomplete, multiple,
and ambiguous states. Leading zeros matter to the filled representation even
when the numeric value is unchanged: `digits_text="007"`, numeric value `7`.

Non-negative whole numbers only are supported. Empty positions within an
otherwise marked answer should not be guessed. All positions blank is different
from a partial answer and from a double-marked digit.

### Answer-Key Validation

`workflows/parse.py::_validate_numerical_answer_key` is shared by batch processing.
Without an answer key, extraction can proceed without grading. When a key is
provided, every numerical question in the manifest must be represented.

- A normal question's marks must match the manifest's maximum marks.
- Marks must be finite.
- A numerical answer must be digits fitting the configured positions.
- A dropped question is represented explicitly with marks `0`.
- Dropped-question answer validation is skipped.
- Dropped questions contribute zero awarded marks and zero denominator marks.

This implements exclusion from scoring, not automatically giving full credit to
everyone. Confirm the professor's drop policy. Deleting a key row is not the
same as dropping a question and triggers the missing-question error.

Historical failures included Q2 marks mismatch and a missing Q15 key entry.
Do not assume those question numbers or the old policy apply to the new midterm.
Validate a new key before starting a large expensive run.

## 13. Written Answers: Real Current State

The user expects to submit full OMR pages with the matching manifest. That is a
valid starting point: the system can locate and crop written-answer boxes. They
should not be forced to manually create a line-crop directory merely to use the
existing page-to-answer extraction path.

`reader/written.py` preserves per-question crops and performs template/ink
processing. Other paths support OCR-oriented crops and overflow/review evidence.
These mechanisms do not establish that every out-of-box handwritten stroke is
automatically assigned to the correct question.

`reader/written_ocr.py` provides optional local OCR adapters, including Tesseract
and TrOCR. The workflow stores recognized text, confidence and review metadata.
Keep the original answer image available to reviewers.

Important limitations:

- Configured ruled-line count is not necessarily the actual number of handwritten
  text lines. Fixed bands can split or merge real writing.
- Rule/box removal and morphology can erase minus signs, fraction bars, and
  other mathematical marks.
- Cropping too tightly can lose ink near edges or overflow.
- A prose handwriting model is not a guaranteed formula/diagram recognizer.
- Provider confidence is not calibrated correctness of an entire answer.
- Model loading, fake-provider tests, and OCR text extraction are not grading.

### Written Marking Workflow

`workflows/written.py` exports a grading packet with answer crops, metadata,
optional rubric/model-answer text, HTML review, and a manual marks template.
It can import reviewed marks, write grade artifacts, and combine objective and
written totals into `final_scores.csv`.

`grading/written.py` defines a provider-agnostic request/result protocol. The
only implemented grading provider is **mock**. Its own documentation says it
does not inspect handwriting; it emits explicit mock flags and defaults to human
review. Do not present `auto-grade` with this provider as real academic grading.
Future model integration must validate results, preserve explanations/evidence,
support abstention, and remain separate from final release approval.

Related references:
[WRITTEN_ANSWER_OCR.md](WRITTEN_ANSWER_OCR.md) and
[ANSWER_EXTRACTION_AND_GRADING_RESEARCH.md](ANSWER_EXTRACTION_AND_GRADING_RESEARCH.md).

## 14. Artifacts And Human Verification

A batch output root is normally an exam-specific subdirectory beneath the
chosen output root. Important artifacts include:

```text
parse_index.json
review_report.csv / review_report.html
_page_identity/source_XXXX/...
students/<normalized_roll>/student.json
students/<normalized_roll>/sheet.pdf
canonical images, identity crops, answer crops, quality/overlay artifacts
unmatched-page and page-error artifacts
```

Use paths published in the current JSON rather than assuming every workflow
stores relative paths in precisely the same way.

`parse_index.json` records candidate students, `ready`/`needs_review`, missing
roster entries, unmatched pages, page errors, and report links. A parser `ready`
label is not equivalent to a human having verified ownership and release.

`workflows/review.py` maintains `verified_index.json` separately from raw parser
outputs. Commands include `init`, `summary`, `verify`, `reject`, `hold`,
`assign-page`, and `ignore-page`. Decisions include reviewer/note information.
Verification can produce a reviewed sheet PDF from selected pages. Missing
required pages prevent ordinary verification.

Keep raw parser results and the human decision trail separate. Correct page
ownership through the review workflow, not by editing an OCR result so that it
looks as if the recognizer was correct. Never bulk-approve uncertain identities
merely to make an email queue nonempty.

## 15. Mailing: Working Path And Safety Boundary

Mailing is useful independently of automatic OMR parsing. This is how manually
corrected PDFs/marks could be released after the earlier grouping failure.

### Two Preparation Paths

1. `prepare`: release from the parsed/verified workflow and its result artifacts.
2. `prepare-folder`: release from an already checked folder of `<roll>.pdf`, an
   authoritative roster, and optional marks in CSV/XLSX.

Expected manual filenames are BTech numeric roll, MTech `MT` plus roll, and PhD
`PhD`/`PHD` plus roll. Normalization is shared, but unknown/duplicate identities
must be resolved. Filenames do not prove that the pages inside match the roll.

`<pdf_dir>/<roll>/sheet.pdf` is also accepted. Discovery is recursive, so stray
or backup PDFs under the input folder can cause duplicate/unknown-roll errors.
Keep the release output directory outside the PDF input directory; the code
rejects nesting that could re-import frozen attachments. Preparation also
rejects shared recipient addresses across roster rolls and a nonempty existing
release directory.

The folder path checks roll/roster mapping, recipient email validity, supplied
marks, PDF readability, expected page count, and attachment size. Default
expected pages is 2, but is configurable for another exam. Default attachment
limit is 20 MB. Inspect all preparation rejections before approving a release.

Recipient selection in `prepare-folder` is driven by the matched PDFs, not by
mailing every roster row. Supplied marks must cover every PDF recipient. A marks
spreadsheet's arbitrary `status` column is not an automatic `ready` filter in
`_load_marks`; that loader reads rolls and marks. If only a reviewed subset is
authorized, construct and check that subset explicitly before preparation.
Conversely, the old request to ignore historical statuses for manually corrected
marks is not a permanent rule for future releases.

Supported body placeholders include `display_name`, `name`, `roll_no`,
`exam_id`, `marks_obtained`, and `max_marks`. The user's earlier exam used
tentative marks wording and later removed a discrepancy sentence. That history
is not an instruction to use the same text for every future exam.

### Frozen Release And Send Behavior

Preparation produces the recipient queue, message bodies/previews, copied
attachments, integrity metadata, and summaries. Read the queue and `.eml`
previews before a real send. Hash validation protects against changed release
files, but cannot detect an already-wrong student attachment.

`send` without `--send` is a dry run and does not open an SMTP connection; it
can still write a dry-run log. There is no separate `--dry-run` flag.

A real send requires explicit `--send`. A non-test real send also requires
`--confirm-count` matching the currently selected unsent count. The
`--test-recipient` option redirects exactly one queue item to a staff address,
recording `TEST_SENT`, not marking that student as sent.

The sender validates the release/attachments before SMTP, uses a send lock,
and normally skips entries already logged as `SENT`. `--resend` deliberately
bypasses that duplicate protection. Moving to a fresh release/log is not a
global cross-release deduplication strategy.

### Authentication And Delivery Caveats

- SMTP is implemented with STARTTLS or SSL. The inspected path is not Gmail
  browser-login/OAuth integration.
- A normal login/password is not necessarily sufficient. The institution must
  permit the chosen authenticated SMTP method. Consult current IT/provider rules
  before making account-policy claims.
- Secrets should be entered locally through the hidden prompt or configured
  environment secret, not pasted into chat or committed.
- The default password environment name is `SMARTOMR_SMTP_PASSWORD`.
- `SENT` means the SMTP server accepted the message, not inbox delivery or reading.
- A disconnect/crash between SMTP acceptance and durable logging can leave an
  uncertain outcome. Investigate before blindly resending; logs are not proof
  of exactly-once external delivery.

The user previously authorized a historical exam release. That is not permanent
authorization for the next AI to send, resend, or test new messages.

See [EMAIL_RELEASE_WORKFLOW.md](EMAIL_RELEASE_WORKFLOW.md) and the actual CLI
help for all flags. Do not recommend `--include-unverified` or
`--include-needs-review` as a shortcut around academic/privacy checks.

## 16. UI And Deployment Reality

The browser banner is **SmartOMR**, as requested. Descriptive module/help text
may still say professor review; that is not the requested visible brand.

`ui/app.py` uses `ThreadingHTTPServer`, a file-backed run store, and background
daemon threads. Default local binding is `127.0.0.1:8765`. It accepts manifests,
scans, roster/key inputs and exposes run progress/review artifacts. Runtime paths
include `data/ui_runs/<run_id>/inputs`, `run_state.json`, parsed results, and
report/marks files.

This is not yet an authenticated, durable multi-user deployment with a database
job queue. A server restart can interrupt daemon-thread work. Do not expose the
development UI on the college network and call it production-ready without an
explicit deployment/security design.

The UI currently requests local roll OCR and uses a `medium` grouping threshold.
The local factory can use configured Tesseract/KNN paths. Check actual backend
availability and selected provider; UI labels or installed Python wrappers alone
do not prove a working handwritten-digit recognizer.

## 17. Performance Expectations

No defensible wall-clock estimate for the professor's 300+ page scan follows
from unit-test runtime or the other laptop's model-load time. Cost depends on
DPI, source quality, rasterization, alignment, number of OCR variants/cells,
available CPU/RAM, model/device choice, and output writing.

Current batch progress callbacks distinguish reading pages and writing student
outputs. Read progress and logs before concluding a long run is stuck. Profile
representative pages and measure peak memory before optimizing.

The API's default batch DPI is 200, whereas the CLI default is 300. Be explicit
when comparing runs. Higher DPI is more expensive and not automatically a cure
for bad handwriting. Cached results must be input/configuration-aware before
using cache hits as valid performance evidence.

## 18. Tests And Verification Evidence

### This Handoff's Full-Suite Run

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Result on this local snapshot: **332 passed in 433.96 seconds (7 minutes
13 seconds)**. The command completed successfully. This covers the configured
suite, including the current local numerical layout and legacy compatibility
test, but not the root experimental scripts or a new real-student OCR benchmark.

### Earlier Layout Verification Context

The latest no-label/10 mm numerical layout was previously checked with 222
focused tests, rendered-sheet preflight, and numerical fill/readback at 200 and
300 DPI for eight two-digit questions and one-through-eight-digit samples.
An earlier full-suite run during the preceding layout iteration had 331 passing
tests in 448.88 seconds; a compatibility regression was subsequently added.
Treat earlier counts as historical checks of those working states, not an
independent real-student benchmark or an invariant project test count.

### Test Inventory

Read the files, not merely their passing counts:

```text
omr/contracts/tests/test_contracts.py
omr/generator/tests/test_flow.py
omr/generator/tests/test_generator.py
omr/generator/tests/test_main.py
omr/generator/tests/test_numerical_layout.py
omr/generator/tests/test_page_identity.py
omr/generator/tests/test_preflight.py
omr/generator/tests/test_printed_sheet.py
omr/grading/tests/test_mcq_confidence.py
omr/grading/tests/test_mcq_grading.py
omr/grading/tests/test_written_grading.py
omr/reader/tests/test_handwriting.py
omr/reader/tests/test_identity.py
omr/reader/tests/test_numerical.py
omr/reader/tests/test_quality.py
omr/reader/tests/test_scan.py
omr/reader/tests/test_written_ocr.py
omr/tests/test_health.py
omr/tests/test_local_registration.py
omr/tests/test_package_structure.py
omr/tests/test_verify.py
omr/ui/tests/test_app.py
omr/workflows/tests/test_batch.py
omr/workflows/tests/test_email.py
omr/workflows/tests/test_parse.py
omr/workflows/tests/test_review.py
omr/workflows/tests/test_written.py
prototype_eval/tests/test_pipeline.py
```

The configured pytest roots are `omr` and `prototype_eval`. Root experimental
scripts such as `test_trocr.py` are not automatically validated just because the
configured full suite passes. Email tests use controlled/mocked transports; they
are not permission to send real email.

### Required Real-World Acceptance Work

For future OCR/grouping improvements, establish an independently checked dataset
with source page, template page, true roll, program, visible writing, and ownership.
Keep writer/exam splits separate from training examples and report:

- Full-roll exact accuracy, not only digit accuracy.
- Wrong accepted ownership assignments separately from rejected cases.
- Automatic acceptance coverage and manual-review rate.
- Duplicate, missing, random-order, and before-anchor page behavior.
- Program-prefix confusion and one-digit-different enrolled rolls.
- Blank/erased/cancelled/partial digit behavior.
- Timing and peak memory under a representative large batch.

A release gate can require zero observed wrong accepted assignments on the
agreed validation set, while explicitly acknowledging finite-sample uncertainty.
Do not claim this proves future zero error. Do not optimize coverage by hiding
conflicts or choosing the nearest roster entry.

## 19. Safe Operating Commands

These are reference commands, not authorization to process private data or mail.
Use PowerShell, an explicit interpreter, and fresh output paths for experiments.
Do not paste the `PS D:\SmartOMR>` prompt or accidental text such as `copilotcd`.

### Inspect And Test

```powershell
cd D:\SmartOMR
git status --short
git log -5 --oneline
git remote -v
git diff --stat
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m omr.health --check-handwriting-ocr
.\.venv\Scripts\python.exe -m pytest -q
```

Health "ready" indicates checked dependencies, not correct ownership/recognition.
Do not run `git pull` over this dirty workspace before assessing incoming changes.

### Generate

```powershell
.\.venv\Scripts\python.exe -m omr.generator.main
```

With no arguments, the generator provides the terminal input workflow. For a
reproducible preview using an existing example rather than final professor input:

```powershell
.\.venv\Scripts\python.exe -m omr.generator.main --config omr/generator/configs/mixed_numerical.json --output-dir data/layout_previews/handoff_example_new_run --no-open
```

Use a new directory or ensure an existing PDF is not locked in a viewer.
Windows `PermissionError` on saving often means that PDF is open. Do not delete
old exam artifacts or demand administrator privileges as the first fix.

`omr.verify` accepts a manifest path for generated-sheet readback. It is not a
scanner field test. Inspect rendered pages and physically print/scan a small
pilot before printing hundreds of the final midterm sheets.

### Local UI

```powershell
.\.venv\Scripts\python.exe -m omr.ui.app --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765`. If already in use, choose a different port such as
8766 and use the matching URL. Do not start another run merely for onboarding.

### Batch Reference

The CLI expects the exact matching manifest under
`data/exams/<EXAM_ID>.manifest.json`. Replace the illustrative ID/paths below
with actual approved inputs and a fresh output root:

```powershell
.\.venv\Scripts\python.exe -m omr.workflows.batch `
  --exam-id YOUR_EXAM_ID `
  --scans "data/uploads/your_scan.pdf" `
  --data-dir data `
  --output-root "data/parsed/your_fresh_run" `
  --handwritten-roll-ocr local `
  --grouping-mode identity `
  --min-group-confidence high
```

This is a diagnostic processing command, not a promise of release-ready results.
An optional `--students` supplies the roster. An optional `--answer-key` supplies
the key; the CLI can also discover an exam key under its data directory, so
inspect resolved inputs if you intend extraction without grading. Do not lower
confidence or change to positional grouping merely to reduce unmatched pages.

Other useful switches include `--dpi`, `--strict-complete`, `--tesseract-cmd`,
`--digit-model`, and separate written OCR switches. Enabling TrOCR may require
optional dependencies and model files; do not trigger downloads accidentally.
Inspect help before using them:

```powershell
.\.venv\Scripts\python.exe -m omr.workflows.batch --help
.\.venv\Scripts\python.exe -m omr.workflows.review --help
.\.venv\Scripts\python.exe -m omr.workflows.written --help
.\.venv\Scripts\python.exe -m omr.workflows.email prepare-folder --help
.\.venv\Scripts\python.exe -m omr.workflows.email send --help
```

No real-send command with historical credentials/recipients is included here.
Construct one only after the new release is verified and currently authorized.

## 20. Architecture And OCR Research: Proposals Only

[RELIABILITY_ARCHITECTURE.md](RELIABILITY_ARCHITECTURE.md), dated 2026-09-19,
proposes an evidence-first redesign and discusses candidate OCR approaches.
[ANSWER_EXTRACTION_AND_GRADING_RESEARCH.md](ANSWER_EXTRACTION_AND_GRADING_RESEARCH.md)
contains earlier written-answer research. These are not a list of installed,
benchmarked production components.

The broad architecture direction is a modular Python application with explicit
stages and durable job/artifact provenance. FastAPI, PostgreSQL, a task queue
such as Celery/Redis, private artifact storage, and dedicated workers are
proposals for deployment, not the present implementation. Do not begin by
replacing all modules or distributing them into many services.

For future printed booklets, unique machine-readable booklet IDs on every page
could make ownership independent of handwriting. This requires a distinct ID
per booklet and a validated binding to the student. The same copied QR code
on all students' sheets would not solve the problem. Existing printed sheets
cannot acquire that evidence retroactively.

Research discusses newer PaddleOCR-family recognizers, approved cloud OCR as a
possible benchmark, TrOCR, and document/vision models for written material.
There is no established "best OCR" for this project's real sheets yet.
Recheck model existence/version, official installation instructions, licensing,
resource requirements, and privacy policy before adoption. Model names in a
dated research document are not current compatibility guarantees, particularly
with this project's Transformers `<5` constraint and differing Python versions.

Choose based on a small, held-out, independently transcribed benchmark of the
actual boxed rolls and answer crops. Never upload student material to a cloud
provider solely because a model might be stronger.

## 21. Recommended Sequence After Onboarding

Do not start all of this at once. The user specifically wants small changes.

1. Confirm the current workspace state and understand all source/tests/docs.
2. Obtain the professor's 15-question format. Generate and visually inspect a
   fresh PDF/manifest pair using the accepted vertical layout.
3. Print a few sheets at actual size, fill realistic examples, and scan them.
   Preserve the exact input pair and raw scans for repeatable checks.
4. Before another automated real batch, address provenance/cache invalidation
   and demonstrate trustworthy alignment/cropping on representative pages.
5. Establish independently checked roll/page ownership ground truth. Evaluate
   the current OCR and each proposed alternative without forcing roster matches.
6. Implement one evidence/recognition improvement with rejection tests and
   measure wrong accepted assignments plus coverage, not just a pass count.
7. Keep human verification and release separate while automation is uncertain.
8. Only after identity/crops are dependable, expand written transcription and
   structured grading with validated providers and human review.
9. Harden deployment, durable jobs, access control, audit, backups, and release
   operations before institution-wide unattended use.

This is a suggested ordering, not permission to perform a rewrite now. The next
AI should first report its understanding and wait for the user's next task.

## 22. Documentation Map And Known Staleness

| Document | How to use it |
| --- | --- |
| `README.md` | Commands, workflow overview; compare examples to current CLI |
| `PROJECT_SPEC.md` | Broad specification/history, not proof of every implemented feature |
| `docs/OMR_DIMENSIONS.md` | Latest local generator measurement reference |
| `docs/HANDWRITING_ROLL_RECOGNITION.md` | Local roll OCR usage and evidence concepts |
| `docs/PROFESSOR_REVIEW_UI.md` | Local UI workflow; inspect code for changed defaults |
| `docs/EMAIL_RELEASE_WORKFLOW.md` | Release preparation, test, send and operational caveats |
| `docs/WRITTEN_ANSWER_OCR.md` | Current optional written OCR workflow |
| `docs/CANCELLED_BUBBLE_DATASET.md` | Experimental dataset/training work |
| `docs/ANSWER_EXTRACTION_AND_GRADING_RESEARCH.md` | Dated research/proposals, not production integration |
| `docs/RELIABILITY_ARCHITECTURE.md` | Dated reliability architecture proposal |
| `docs/NEXT_AI_PROMPT.md` | Copy-paste onboarding instruction |
| This file | Cross-cutting local-state handoff, risks, immediate context |

The original `CLAUDE.md` was renamed to `PROJECT_SPEC.md` earlier at the user's
request. Terminal generation is preferred; obsolete launcher `.bat` files were
removed earlier. Do not recreate tool-branded instruction files or launchers
without a reason tied to a current task.

## 23. Onboarding Checklist And Guardrails

Use `git ls-files` and `rg --files omr prototype_eval docs` to inventory current
source. Read configuration and tests along with implementation. Read untracked
source separately and label it experimental when appropriate. Do not claim
"each and every file understood" after scanning only entry points.

Before editing, explain the scope. Prefer existing module boundaries. Add tests
that expose the actual failure, not only tests that repeat the chosen algorithm.
For layout changes, inspect rendered PDFs at multiple representative question
counts/digit widths and preserve old-format compatibility fixtures.

Do not:

- Assert real-sheet accuracy from synthetic filled bubbles or fake OCR tests.
- Infer student ownership from file order in a randomly ordered PDF.
- Force ambiguous writing to a nearby roster entry.
- Treat a candidate PDF, parser `ready`, and human-verified release as identical.
- Alter old printed-sheet manifests to match new geometry.
- Grade real written answers with the mock provider.
- Upload student scans/rosters to public repositories or external OCR casually.
- Read passwords into chat, expose secrets in commands/logs, or reuse old send
  authorization for a new release.
- Run unknown root patch scripts or train on test examples without an audit.
- Promise a timeline/perfect OCR/production readiness without measurement.
- Delete generated evidence, overwrite dirty source, or commit everything blindly.

For final reports, distinguish what was inspected, changed, executed, and left
unverified. If something fails or is unknown, say so plainly. The user needs
dependable, incremental work more than confident reassurance.

## 24. Scope Of This Handoff Task

This task creates documentation only: `docs/AI_HANDOFF.md` and
`docs/NEXT_AI_PROMPT.md`. Existing application changes remain untouched.
The full automated suite completed with 332 passing tests. No real student OCR
benchmark, new model installation, cloud upload, email send, application
deployment, commit, or push was performed as part of this task.

For a next AI with workspace access, paste `NEXT_AI_PROMPT.md` and let it read
this document and the source. For a chat-only AI, supply both Markdown files
and the current relevant source privately. A GitHub link alone cannot transfer
the local uncommitted work described above.
