# SmartOMR Production Roadmap

Date: 2026-09-23. Code baseline: `851f681` on `main`.

**Status: planning only.** Recommendations, acceptance criteria, and proposed
interfaces below are not claims of implemented behavior or measured accuracy.
No new recognition model was installed, trained, or evaluated for this plan.
No student material was uploaded to external model services.

### Reading Guide

- [Current code and risks](#2-evidence-and-current-state)
- [End-to-end workflow](#4-end-to-end-workflow-and-required-inputs) and [page identity](#5-identity-strategy-existing-sheets-and-future-exams)
- [Written answers](#9-written-answers-extraction-before-automated-marking) and [model selections](#10-model-and-tool-decisions)
- [Data and fine-tuning](#11-dataset-fine-tuning-and-confidence) and [mailing](#12-mailing-and-release-reliability)
- [Application architecture](#13-application-architecture-and-persistence), [hardware](#14-hardware-performance-and-capacity), and [operations](#15-ui-security-and-operations)
- [Acceptance tests](#16-verification-and-acceptance-plan) and [small-PR delivery sequence](#17-incremental-delivery-plan)
- [College decisions](#18-decisions-needed-from-college-staff) and [immediate next action](#20-practical-next-action)

## 1. Executive Decision

Build a **template-guided assessment system with independently reviewable stages**,
not an OCR script that immediately creates student PDFs and emails them.

Keep the useful generator, manifest geometry, OpenCV readers, review operations,
and frozen email releases. Repair their contracts incrementally. A rewrite would
discard working behavior without proving that recognition or ownership improved.

The priority order is:

1. Make every source page and every processing result identifiable and reproducible.
2. Make scan inspection the actual input to downstream processing.
3. Preserve identity disagreements and prohibit unsupported ownership decisions.
4. Establish an independently labelled real-scan benchmark.
5. Compare replacement OCR under that benchmark, with rejection allowed.
6. For future exams, introduce unique booklet codes on every page, if approved.
7. Validate objective answers, review, grouping, and release end to end.
8. Improve written-answer extraction, then transcription, then assisted grading.
9. Harden persistent jobs, security, deployment, and operations before college access.
10. Add conversational assistance only after these workflows are dependable.

**There is no defensible universal "best OCR" for this project today.** The best
candidate must be selected separately for boxed rolls, prose, and formulas using
our own unseen scans. The first replacement candidate for roll strips is
PP-OCRv6 medium; handwritten prose starts with the existing TrOCR baseline;
mixed text/formulas get a PaddleOCR-VL-1.6 comparison. These are research
selections, not permission to auto-release their predictions. See Section 10.

**The first implementation change should be cache provenance and shared page
artifacts, not a new model.** A more accurate recognizer operating on the wrong
cached image, wrong crop, or unapproved alignment still gives an unsafe result.

## 2. Evidence And Current State

### 2.1 What Was Published Before This Plan

- Commit: `851f6812ce0785eaca43f2da66007a91bccec068`.
- Message: `restore numerical place labels and make sheet headings context-aware`.
- Remote `origin/main` was verified to contain this exact commit.
- Full configured suite: **375 passed in 386.96 seconds**, exit code zero.
- Staged changes were limited to the ten reviewed generator, documentation,
  and regression-test files. Whitespace checks passed.

The untracked `omr/reader/cnn_ocr.py` was deliberately left untouched and not
published. It is an unregistered experiment with an invalid helper import,
unconditional ML dependencies, and roster-constrained decoding. Its existence
is not evidence that a production CNN recognizer is available.

The test result establishes a regression baseline. It does **not** establish
real handwriting accuracy, safe grouping on the historical exam, or production
readiness. The previous incorrect grouping and subsequent manual work are
user-reported incident history; this planning exercise did not independently
reconstruct the entire incident or re-audit the historical emails.

### 2.2 What The Code Already Provides

| Area | Current capability | Important boundary |
| --- | --- | --- |
| Generation | A4 PDFs plus manifest; MCQ, unsigned numerical, written areas; BTech/MTech/PhD identity fields | Changing a generated layout does not update already printed sheets |
| Numerical layout | Vertical grids, 3.5 mm bubbles, place-value labels, 1-8 positions; four two-digit questions across | Negative signs and decimals are not supported |
| Printed labels | Per-question marks hidden; context-aware section titles; leading-zero example reflects digit width | Baseline marks remain configuration/manifest metadata |
| Input inspection | Source-page inventory, source/manifest hashes, raw/aligned previews, overlays, page failures and resume | Not yet the single artifact path used by evaluation |
| Alignment | Fiducials, orientation/page signals, several fallback paths, local registration, quality reports | A heuristic quality score is not a calibrated probability of correct sampling |
| Objective reading | Program/roll bubbles, MCQs, numerical digit grids and ambiguity flags | Real scanner/pen/cancellation accuracy still needs a benchmark |
| Roll handwriting | Tesseract and optional local digit KNN; crops and raw evidence | Boxed handwritten identifiers remain unreliable without measured gates |
| Grouping | Identity-based two-pass association; page 2 can precede page 1; duplicate/missing-page review flags | Current confidence labels and evidence handling need strengthening |
| Written answers | Answer crops, ink/overflow evidence, optional line OCR, manual marks workflow | OCR is not reliable semantic grading; registered automated grader is a mock |
| Review | Verification records, unmatched-page assignment, selected-page PDF generation | File-backed state is not a transactional multi-user audit system |
| Mailing | Roster mapping, folder-based preparation, frozen artifacts, hashes, previews, dry run, test send, send log | SMTP acceptance is not guaranteed delivery or exactly-once sending |
| UI | Local inspection/review application and progress | Standard-library HTTP server and daemon jobs are not hardened college deployment |

Current identity formats are implementation assumptions: BTech seven digits,
MTech `MT` plus five digits, and PhD `PHD` plus five digits. Confirm institutional
exceptions before freezing a production policy. Treat program and identifier as
structured fields, and preserve their original text. Do not infer PhD identity
or email formats from a public people directory.

### 2.3 Repository Map And Ownership Boundaries

| Existing location | Role to preserve | Planned work belongs here or behind it |
| --- | --- | --- |
| `omr/contracts/` | Manifest and coordinate contracts | Versioned page/evidence contracts; validation and compatibility |
| `omr/generator/` | Layout, PDF rendering, manifest, preflight | Optional unique-booklet printing; physical print validation |
| `omr/reader/scan.py`, `quality.py` | Canonicalization and quality evidence | Provenance, geometry gates, reliable reuse |
| `omr/local_registration.py` | Bounded local alignment | Per-region evidence and conservative fallback |
| `omr/reader/identity.py`, `handwriting.py`, `digit_model.py` | Identity observations | Strict independent candidates and evaluated model adapters |
| `omr/grading/bubbles.py`, `mcq.py`, `omr/reader/numerical.py` | Objective image measurements and decisions | Calibrated ambiguity/cancellation policies, deterministic scores |
| `omr/reader/written.py`, `written_ocr.py` | Written regions and recognition | Content-aware segmentation, literal transcripts, formula routing |
| `omr/grading/written.py` | Written grader contract | Validated proposals, never uncontrolled release authority |
| `omr/workflows/parse.py`, `batch.py` | Orchestration | Gradual separation of extraction, ownership and grading |
| `omr/workflows/review.py`, `written.py` | Human decisions and manual grading | Revisions, invalidation, concurrency, evidence-linked decisions |
| `omr/workflows/email.py` | Preparation and delivery | Preserve frozen releases; durable outbox and approved auth options |
| `omr/io/` | CSV/tabular ingestion | Import previews, strict identifiers, row-level errors |
| `omr/ui/` | Operator workflows | Shared services first; later authenticated web deployment |
| `omr/datasets/` | Dataset/training utilities | Versioned, permissioned datasets and reproducible evaluations |
| `prototype_eval/` | Compatibility surface | Characterize callers before any future removal |

The audit traced these critical workflows and their tests, inspected the package
inventory, and read relevant existing specifications/research. It is not a claim
that every line of every test or private generated artifact was independently
audited. Existing dated documents can conflict with current code; for example,
the September 20 handoff describes numerical place labels that have since been
restored. Use the baseline above for this plan.

### 2.4 Concrete Risks To Repair

Locations below refer to baseline `851f681`; function names are more durable
than line numbers after later edits. These are code observations, not a claim
that any one explains the entire historical incident.

| Priority | Observation | Consequence | Required regression/gate |
| --- | --- | --- | --- |
| P0 | `batch.py:_load_aligned_page` around line 1339 loads cached images without source/manifest/config/DPI fingerprints | Same exam/output path can reuse unrelated or obsolete alignment | Changing any relevant input must miss the cache; old artifacts remain untouched |
| P0 | `ui/app.py:RunStore._run_batch` around line 757 calls batch parsing on the original PDF again | Inspection results are not the authoritative evaluated artifacts | Evaluation must consume the exact selected inspection artifact revision |
| P0 | `handwriting.py:_read_write_in_roll_number` around line 520 filters candidates by roster before detecting conflicts | An in-roster candidate can conceal a conflicting out-of-roster read | Detect and preserve all raw disagreement before roster validation |
| P0 | `handwriting.py:_first_digit` around line 865 takes the first digit found | Multi-character cell output can become a seemingly valid single digit | Exact one-digit cell grammar; ambiguous/extra characters reject |
| P0 | Written result/manual-mark bounds checks in `grading/written.py` and `workflows/written.py` omit explicit finite-value checks | NaN can evade ordinary lower/upper comparisons | Reject NaN, infinity, malformed values and invalid maxima at every import/API boundary |
| P1 | Batch records retain aligned image arrays for the whole bundle | Large jobs can consume substantial RAM despite streaming PDF input | Store artifact references and bound resident-page count |
| P1 | OCR confidence combines heuristics, medians or scores from generation steps | A label such as high/medium is not exact-roll correctness | Calibrate on held-out writers; expose raw scores separately |
| P1 | `written_ocr.py:_save_written_line_crops_from_path` uses fixed bands from configured line count | Actual handwriting can be split, omitted or duplicated | Real line segmentation with whole-region fallback and coverage accounting |
| P1 | Written OCR decoding/scoring penalizes repetition and symbol-heavy text | Repeated digits, symbols or formulas may be distorted | Literal-transcription tests, truncation flags, content-specific decoding |
| P1 | `build_written_ocr_backend` aliases ensemble/best/auto to TrOCR | Operator may expect independent evidence that is not present | Report actual backend(s), weights and decoder; no misleading ensemble labels |
| P1 | `email.py` sends before appending its durable send record; duplicate filtering occurs before acquiring its send lock | Crash/timeout uncertainty; stale pre-lock selection can undermine retry safety | Durable attempts, recheck under lock/transaction, uncertain-outcome reconciliation |
| P1 | File-backed review/run state and daemon jobs | Restart/concurrent-edit durability limits | Versioned decisions, process-independent jobs, tested recovery |
| P1 | Dependencies mostly have lower bounds rather than deployment locks | Another clone can resolve a different environment | Tested lock profiles and recorded model/checkpoint/runtime versions |

Existing protections are valuable: identity-based grouping does not simply pair
adjacent pages; duplicates add review flags; inspection checks source and
manifest hashes; email preparation freezes files. Strengthen these protections
rather than treating the entire codebase as disposable.

## 3. Non-Negotiable Invariants

1. Every source PDF page receives a stable identifier and remains accounted for.
2. Original inputs are immutable; derived files never replace them.
3. Recognition observations, accepted ownership, accepted answers, grades and
   release decisions are different records with different authorities.
4. A page cannot belong to two active student bundles in the same exam attempt.
5. A student bundle cannot contain two accepted pages for the same template slot.
6. Low-quality, unreadable, conflicting or missing evidence is a review case,
   not permission to guess the nearest student or silently skip a page.
7. No scan order, visually similar handwriting, filename, roster proximity or
   LLM explanation alone proves ownership of a generic continuation page.
8. A blank answer, unreadable answer and missing page are distinct states.
9. An edited identity, transcript, key or rubric invalidates its dependent outputs.
10. A published release records exact recipient, PDF, marks, message, approvals
    and versions. It cannot silently change after approval.
11. No automated processing stage can send email. Sending is a separate explicit
    permissioned action against an approved immutable release.
12. Reprocessing and retries must not overwrite reviewed work or resend silently.

"Fail closed" means preserve the evidence, finish independent pages, explain
the issue, and block only the dependent decisions. It does not mean one bad page
must discard an entire class's progress.

## 4. End-To-End Workflow And Required Inputs

### 4.1 PDF And Manifest Come First

The first step needs only **the scanned PDF and the matching manifest**.

The roster is not required to count pages, validate a template, align a page,
locate bubbles, save handwriting crops, or read an identifier literally. The
answer key is not required to extract the student's response either.

| Stage | Required input | Output | What remains prohibited |
| --- | --- | --- | --- |
| Intake | PDF + matching manifest | Immutable upload and page inventory | Student assignment or scoring from filenames/order |
| Inspection | Page inventory + layout | Raw/aligned images, quality and page-index evidence | Treating uncertain alignment as accepted |
| Extraction | Accepted/qualified geometry | Identity candidates, bubble observations, answer crops | Resolving conflicts using the expected answer |
| Ownership | Identity evidence and/or issued booklet registry | Proposed then accepted page associations | Attaching ambiguous pages just to complete bundles |
| Roster reconciliation | Optional roster, accepted identities | Exact enrollment matches and recipient candidates | Inventing email addresses or deleting unknown students |
| Objective grading | Accepted responses + approved key/policy | Deterministic versioned marks | Inferring a key from responses |
| Written grading | Accepted crop/transcript + question/rubric | Human marks or reviewable model proposals | Treating transcription confidence as grade correctness |
| Release preparation | Approved bundles/marks + authoritative roster | Frozen previewable email package | Sending unresolved or stale artifacts |
| Delivery | Approved release + authorized sender transport | Attempt/acceptance/failure records | Reporting server acceptance as inbox delivery |

Extraction can proceed on source-page IDs before a roll number is resolved.
Fixing ownership later should reuse those observations, not rerun OCR on 300 pages.

### 4.2 Proposed Processing Graph

```text
PDF + manifest -> intake -> page inventory -> alignment/quality
                                              |
                          +-------------------+-------------------+
                          |                   |                   |
                  identity observations   objective marks    written crops
                          |                   |                   |
                    ownership review     response review     text/formula OCR
                          |                   |                   |
                     student bundles          |             transcript review
                          +-------------------+-------------------+
                                              |
                           approved key/rubric -> grading review
                                              |
                           roster -> recipient reconciliation
                                              |
                                  frozen release -> preview
                                              |
                               explicit approval -> delivery
```

Use separate dimensions of state, not a single overloaded `ready` flag:
`processing_state`, `geometry_state`, `identity_state`, `answer_state`,
`grade_state`, and `release_state`. "Processing completed" is not "all pages
accepted". Display completed/failed/review counts explicitly.

### 4.3 Example: 300 Pages

A 300-page PDF of two-page booklets may contain 150 complete students, but that
must be established from evidence. There may instead be separators, duplicates,
missing pages or scans from another exam. Never infer exactly 150 students from
page count alone.

Suppose intake records all 300 source pages and later determines 146 complete
verified booklets, three incomplete students, two duplicate pages and other
unresolved pages. The UI must show the accounting and blocked reasons, not
invent attachments to reach an expected student count. A professor may approve
a clearly scoped partial release for complete students; unresolved students stay
held. Counts must reconcile using actual source-page disposition records.

## 5. Identity Strategy: Existing Sheets And Future Exams

### 5.1 Existing Generic Sheets

Keep page-1 bubble and handwriting reads independent. Retain full candidates,
scores, model versions, program selector evidence and source crops.

| Evidence | Proposed decision |
| --- | --- |
| Page-1 bubble and written roll agree, both satisfy validated quality/acceptance gates | Eligible anchor proposal; pilot still requires human confirmation |
| Page-1 bubble and written roll disagree | Conflict; do not prefer whichever is in the roster |
| One page-1 channel missing/weak | Incomplete evidence; explicit human verification can establish the anchor |
| Continuation has accepted exact identity and matching verified anchor | Eligible association proposal for its template page |
| Continuation arrives before its anchor | Keep indexed evidence pending; resolve after all anchors are known |
| Continuation is unreadable, or two readers disagree | Unmatched review, even if only one student's page is missing |
| Two scans claim the same roll and template page | Duplicate/conflict review; preserve both scans |
| Same written number appears in different programs | Keep separate candidate identities; investigate selector/prefix conflict |
| Identity not in roster | Retain it as out-of-roster; do not coerce it into an enrolled number |

For `C2 A3 B1 C3 A2 A1 C1 B3 B2`, collect all nine observations first. Once
A1/B1/C1 establish accepted identities, attach only independently supported
continuations and sort each bundle by template page. The order is irrelevant.
If A3 is unreadable, A's bundle stays incomplete; the system must not promise
three complete correct bundles regardless of the evidence.

Agreement between two OCR outputs is useful but not proof: they can share the
same preprocessing error or the same ambiguous handwriting. Roster membership
is a consistency check, not independent visual evidence.

Do not silently translate `O` to `0`, truncate extra digits, add missing zeros,
or drop a program prefix to force a valid identifier. Any institution-approved
normalization must be explicit, lossless where possible, and retained beside
the raw observation. Near matches may be shown as reviewer suggestions only.

### 5.2 Preferred Future Printing: Unique Booklets

Print an opaque unique booklet identifier on every page of each booklet.
This does not require a roster. The first page subsequently binds that booklet
to a student after identity verification.

Proposed payload fields:

```text
payload_version, exam_id, template_revision, print_batch_id,
booklet_id, template_page_index, template_page_count, integrity_tag
```

Use a compact representation and an issued-booklet registry. Keep personal
names/emails out of the code. Validate exact payload, allowed template, page
range and issuance. A checksum detects damage, not forgery. If authenticity
is required, add a standard keyed MAC and key identifier under an approved
key-management policy; never invent custom cryptography.

Use an established QR encoder and decoder. Prefer existing ReportLab QR support
if it meets print tests, with `zxing-cpp` as the decoding candidate; its official
Python wrapper exposes barcode read/write APIs. Do not implement QR error
correction ourselves. [ZXing-C++ Python documentation](https://github.com/zxing-cpp/zxing-cpp/blob/master/wrappers/python/README.md).

Printing process:

1. Freeze one template/manifest revision and expected booklet page count.
2. Generate a print-batch PDF containing distinct booklet IDs, including spare sets.
3. Save an issuance registry and hashes for the generated artifacts.
4. Verify every generated code/page association before physical printing.
5. Print at the approved scale; physically scan representative first/last/spare sets.
6. Check duplicate or missing IDs in distribution and subsequent intake.
7. Retain readable booklet/page labels and written identity fields for review.

**Photocopying one uniquely coded blank 300 times defeats the design.** If the
college requires identical photocopies, continue the legacy OCR path or approve
a controlled unique-label process. Labels introduce placement and distribution
risks and need their own pilot.

A valid code identifies a document, not the person who used it. Conflicting
written rolls, a student using another booklet, duplicate scans and replacement
pages still require review. Do not remove human-readable roll fields initially.
Supplementary answer pages need explicit booklet binding and page-slot rules.

Reserve code dimensions, quiet zone and location in a new manifest schema;
do not guess a tiny size and squeeze existing fields. Physical decode tests
decide the final size. Existing v4/v5/v6 manifests remain readable; new issued
booklets must not reuse old identifiers or overwrite old manifest files.

## 6. Intake, Alignment And Shared Artifacts

### 6.1 Immutable Intake

Give each upload and run a generated internal ID. Treat the filename as display
metadata only. Save the original PDF and manifest bytes with SHA-256 hashes,
size, timestamp, schema version and uploader. Do not trust embedded PDF text as
ground-truth handwriting; scans can contain an old or unrelated OCR text layer.

Reject unsupported/corrupt/encrypted inputs with an actionable error. Set and
test page-count, image-pixel, upload-size, processing-time and storage limits.
Limits are deployment configuration and visible before upload; they must permit
the approved class workload. Handle PDF rotation, crop/media boxes, mixed page
sizes and image-only PDFs explicitly.

Page identity is `(upload_id, source_page_index)` plus source hash. A hash of
the rendered page may additionally suggest duplicate scans, but two separate
page occurrences remain separate inventory records until adjudicated.

### 6.2 Artifact And Cache Keys

Every stage result needs a dependency fingerprint containing:

- Source file hash and page index.
- Manifest content hash, schema and template revision.
- Input artifact revision and content hash.
- Stage code/algorithm version and relevant configuration.
- DPI, coordinate conventions, color mode and renderer version.
- Model name, immutable revision/checksum, preprocessing and decoder settings.

Not every key needs an unrelated downstream field: changing an answer key
invalidates grades, not alignment. Changing crop geometry invalidates crops
and their recognition, not the immutable source PDF.

Existing unversioned cache files must be considered untrusted, not silently
upgraded by assuming their source. Write new artifacts in unique directories
using atomic replacement for metadata. Preserve previous successful and failed
attempts. Add a cleanup policy that only removes unreferenced derivatives after
retention approval, never a blanket delete of `data/`.

### 6.3 Geometry And Quality

Continue template-based OpenCV alignment. Measure evidence before adding
learned dewarping or a full document-layout model.

- Preserve original pixels and all coordinate transforms back to the PDF page.
- Distinguish detected fiducials from inferred corners and page-boundary fallbacks.
- Record page-index alternatives, not only the winning page number.
- Test orientation at 0/90/180/270 degrees and ambiguous/torn page marks.
- Validate local bubble/roll/answer geometry, not just global corner alignment.
- Retain overlays of expected centers, sampled centers, crop boundaries and flags.
- Bound local transforms; if anchors are inadequate, reject the region instead
  of extrapolating an unconstrained warp.
- Check blur, clipping, contrast, scale, skew and missing image regions separately.
- Treat inferred-marker and page-text fallback cases as review-first until their
  own held-out evaluation supports a narrower automatic policy.

Re-render an ROI at a higher DPI only when evidence suggests inadequate raster
resolution. Upscaling cannot recreate lost scan detail. Compare 200/300 DPI on
real scans; do not make a universal switch based on one attractive preview.

Learned dewarping is a later optional experiment for visibly curved phone photos.
It may alter handwriting. Keep a raw-source comparison and never use it to
silently repair a page whose geometry cannot be verified.

### 6.4 The Inspection-To-Evaluation Contract

Move shared page-artifact contracts into a non-UI module, preserving wrappers
and CLI compatibility. The UI submits and displays the same service outputs
as the CLI. Batch extraction accepts selected artifact IDs, not an instruction
to quietly render and align the original file a second time.

A manual alignment correction creates a new revision; downstream extraction
for that page becomes stale. The UI must distinguish proposed, accepted,
superseded and failed alignments. Rejected pages can still have review crops,
but cannot contribute automatically accepted identity or answer observations.

## 7. Objective Answers And Grading

### 7.1 Read Bubbles With Image Measurements, Not An LLM

Keep the existing manifest-guided MCQ/numerical readers. Save measurements
per bubble: expected and sampled center, fill ratio, ink evidence, threshold
configuration, local geometry and a crop/overlay reference.

Evaluate these distinct states: blank, one mark, multiple marks, incomplete
numerical field, faint mark, erasure, cancellation/overwrite, and unreadable.
Cancellation interpretation is an exam policy, not something a classifier can
decide from appearance alone. A cancelled-looking mark may still require human
judgment about the student's intended final answer.

Do not globally lower thresholds to reduce the review count. Benchmark raw
measurements, the current deterministic classifier, and only then an optional
specialist bubble classifier on genuine problematic marks. Its labels should
describe visual state; grading policy consumes that state separately.

Numerical grids retain both literal `digits_text` and parsed integer. With two
positions, `07` is seven but `?7` and a blank tens column are incomplete, not
automatically `07`. Validate all positions against the exact manifest version.
Test maximum capacity, all zeros, repeated digits, and digit-place ordering.

MCQ multi-select scoring, negative marking, numerical tolerances, decimals and
signed numbers are new policies/formats, not implicit extensions of the current
single-answer and non-negative-integer implementation.

### 7.2 Keep The Key Separate From The Student Reading

Extraction cannot see the expected answer. It produces observations before
grading, so a model cannot repair a faint response toward the correct value.
Validate the complete key before expensive grading; show all invalid/missing
rows in one report rather than failing one question at a time.

Key/rubric versions are append-only. Store source file hash, question IDs,
effective weights, approved policy and approver. Regrading is deterministic and
does not require another scan/OCR run when accepted responses have not changed.
Use finite decimal marks or integer subunits consistently for aggregation and
rounding; reject non-finite values at every boundary, not just email import.

### 7.3 Dropped Questions

Current objective-key behavior supports **exclude from scoring**: retain the
question row, supply a nonempty placeholder answer and set marks to zero, e.g.
`2,DROP,0`. Do not omit the row, change printed numbering, or rewrite the original
manifest. For 15 one-mark questions with Q2 and Q15 dropped, the effective total
is 13. That is not the same policy as awarding two bonus marks.

Plan explicit policy choices: normal, excluded, full credit for all, and
approved alternate answers. Each needs a displayed effective total, professor
approval, version, examples and tests. Existing zero-mark compatibility should
map to excluded, not silently change meaning. Written-question dropping needs
its own rubric/weight support; the current objective-key convention is not a
general written grading implementation.

Dropping a question removes scoring dependence on its ambiguous mark, but never
removes identity, missing-page or privacy checks. Key changes invalidate
dependent grades and unsent release approvals. Already sent releases remain
historical records; a correction requires a separately approved revision.

## 8. Human Review And Student PDFs

### 8.1 Review Queues

Use task-specific queues: input/geometry, page identity, duplicate/missing-page
association, objective response, written transcript, written grade, and recipient
or release issue. Group related issues without hiding their original evidence.

The identity review screen should show source page number, template page,
program marks, the full written roll crop, digit cells, bubble overlay, all raw
candidates and a proposed destination's other pages. Show a rejected candidate
as rejected rather than deleting it. Do not preselect a nearest-roster guess as
if it were verified.

A reviewer can accept evidence, correct identity, assign/unassign a page, choose
a duplicate, mark a separator, request rescan, or hold. Require reasons for
overrides and exceptions. Never collapse all of these into one "approve" flag.

Use optimistic revision checks: reviewer B cannot overwrite reviewer A's newer
decision silently. Record actor, timestamp, previous/new state, reason,
evidence hashes and decision version. Reversal is another event, not deletion
of the earlier audit record.

During pilots, inspect every student's complete bundle. Later, reduce manual
checks only for evaluated categories while keeping all flagged cases and a
random audit sample. Define how a discovered false acceptance expands the audit
and pauses release before relaxing checks.

### 8.2 Bundle Integrity

Bundle completeness means exactly one accepted source occurrence per expected
template slot, correct exam/template, consistent accepted ownership, and no
unresolved duplicate or conflict. An approved exception must be explicit; a
missing answer page must not automatically earn zero marks.

Create the student sheet from approved original PDF pages, in template order,
using existing PDF libraries. Preserve legibility and avoid unnecessary image
recompression. Correct page display rotation in a derivative if needed while
retaining the exact source-page mapping. Recognition overlays are separate
review artifacts, not mandatory student attachments.

Record bundle revision, ordered source-page IDs and hashes, page count, PDF
hash, ownership approvals and verification status. Reopen and validate the
resulting PDF. Test that no neighboring student's page, roster or debug crop
can enter it. Use an optional separately generated grade summary/cover page
whose score version is explicit; do not overwrite original student writing.

An identity correction invalidates the old bundle and grades attached through
that ownership. A duplicate choice must feed the exact same selected page into
preview, extraction, grading and final PDF. This invariant needs one shared
selection function, not several independently reconstructed lists.

## 9. Written Answers: Extraction Before Automated Marking

### 9.1 Preserve What The Student Actually Wrote

The first written-answer milestone is an accurate, complete answer image with
traceable ownership, not an LLM score. Keep three separate representations:

1. Raw answer region with controlled context and a link to the full source page.
2. Analysis derivatives for ink, rule detection, segmentation and overflow.
3. Literal transcription with region/line references and uncertainty.

Never replace the raw crop with the cleaned binary image. Rule removal can erase
a minus sign, fraction bar, decimal point or writing touching the border.
Compare raw and processed variants, retaining the transform and preprocessing
recipe. The reviewer should always be able to return to the unmodified evidence.

Represent presence as blank, nonblank, uncertain or unavailable. A blank-looking
crop from a failed alignment is unavailable, not blank. Detecting handwriting
does not mean understanding it, and failing OCR does not imply the student
left the question unanswered.

Overflow is evidence to inspect. If writing crosses the answer boundary, show
the neighboring region/full page. Do not automatically steal neighboring
question content by expanding every crop. A reviewer can associate extra regions
or supplementary pages with a question, preserving the original allocation and
the reason for the override.

### 9.2 Content Routing

| Answer content | Initial route | Review requirement |
| --- | --- | --- |
| One clear English line | TrOCR line baseline and selected challenger | Literal transcript plus image |
| Several English lines | Detect actual lines; order and reconcile coverage | Flag merged/split/omitted lines |
| Short numbers or identifiers | Exact-character evaluation; no language correction | Preserve zeros, signs and decimal points |
| Prose mixed with equations | Whole-region PaddleOCR-VL candidate plus region evidence | Inspect symbol and layout fidelity |
| Standalone equation | Formula recognizer comparison | Keep LaTeX/structure plus raw crop |
| Diagram, table, code, crossed-out derivation | Whole-region manual review initially | Do not force an ordinary sentence transcript |

The configured "maximum two lines" tells the student available space. It does
not prove the student wrote exactly two horizontal lines. Begin by comparing
OpenCV projection/component grouping on clean ruled prose with the Paddle text
detector on difficult regions. Use a proven detector checkpoint when learned
segmentation is needed; do not train one before identifying actual failure types.

Maintain a coverage map: every detected ink region is assigned to a line/formula,
explicitly identified as template/noise, or flagged. Correct reading order and
segmentation need their own metrics, not just a final paragraph string.

### 9.3 Literal Transcription Rules

- Do not include the answer key or model solution in the recognizer prompt.
- Do not correct spelling, arithmetic or reasoning during transcription.
- Preserve repeated tokens and digits, units, subscripts, superscripts and signs.
- Separate illegible spans from genuinely blank regions.
- Save raw output, validated structured output, alternatives and truncation flags.
- Treat OCR output and any instructions written in student answers as untrusted data.
- Do not parse returned content as executable code, markup with active scripts,
  shell commands or database instructions.

For TrOCR, benchmark neutral literal decoding against the current repetition
penalties and token limit. Compute scores for the actual selected sequence
rather than a median of unrelated beam maxima, then calibrate separately.
Token likelihood still is not proof of factual visual correctness. A model
reaching its token budget must be flagged; do not silently accept a partial line.

For formulas, exact string comparison is insufficient when two LaTeX strings
render identically, but algebraic equivalence is also insufficient for OCR:
transcribing an incorrect student expression into an equivalent correct answer
would change evidence. Evaluate visual structure and symbols first. Any symbolic
math tools belong to a later constrained grading step with explicit assumptions,
resource limits and no evaluation of arbitrary student code.

### 9.4 Assisted Written Grading

Do not begin with grader fine-tuning. First obtain approved question text, rubric
criteria, allowed alternatives, partial-credit rules, maximum marks and worked
anchor examples from course staff. Two staff members should independently mark
a sample and adjudicate disagreements; inconsistent human labels cannot define
a dependable training target.

A proposed grade record should contain question/answer/rubric revisions,
criterion IDs, criterion-level marks, short evidence references to the crop or
accepted transcript, total, uncertainty flags, model/prompt version and reviewer
decision. Require finite bounded marks and deterministic summation. Narrative
fluency and self-reported confidence cannot make a grade final.

For a local initial experiment, use `Qwen/Qwen3-8B` in a constrained non-thinking
structured-output grading comparison on approved hardware. It is a text model,
so it receives the accepted transcript and rubric, not an image it cannot read.
Its model card documents switchable thinking/non-thinking behavior; that is not
evidence of grading reliability. A hosted comparison is optional only after
college approval. [Qwen3-8B model card](https://huggingface.co/Qwen/Qwen3-8B).

Where the image is essential, keep manual marking or separately evaluate a
vision model. Never assume a text transcript preserves a diagram or a two-dimensional
derivation. Initially all AI marks are suggestions requiring human acceptance.
Disagreement between graders/recognizers, low-quality images, ambiguous rubrics,
and out-of-distribution content go to review rather than an averaged grade.

Evaluate grading on **correct human transcripts first**, then on OCR transcripts.
This separates rubric/reasoning failures from recognition failures. Measure
criterion agreement, exact score agreement, mean absolute error, large errors,
false zero/full marks and differences across question types. Correlation alone
can hide consistently biased marks.

Explicitly block mock graders from production releases, even if a test option
labels their output final. A transcript or rubric correction invalidates the
affected grade and unsent release eligibility. Initial grading remains assistive
even after a strong benchmark; staff retain academic authority.

## 10. Model And Tool Decisions

### 10.1 Shortlist, Not A Leaderboard

Research checked primary publisher documentation/model cards on 2026-09-23.
Do not transfer vendor benchmark scores to SmartOMR. Pin the exact artifact and
license at experiment time; a model name alone is not a reproducible version.

| Task | First choice to evaluate/use | Challenger or fallback | Fine-tune now? |
| --- | --- | --- | --- |
| PDF rendering | Existing PyMuPDF path with input limits and lineage | Existing PDF assembly utilities; evaluate replacement only for a demonstrated issue | No ML |
| Geometry | Existing OpenCV fiducials + bounded local registration | Manual correction/rescan; learned dewarp only after evidence | No |
| Booklet code | Established QR encoding + ZXing-C++ decoding | Human-readable code and manual verification | No |
| MCQ/numerical bubbles | Existing image measurements and explicit ambiguity | Optional small visual-state classifier on labelled hard cases | Not initially |
| Boxed handwritten rolls | `PP-OCRv6_medium_rec` on manifest roll strips, compared with current Tesseract/KNN | Independent per-cell specialist; manual review | Only if local holdout exposes remaining domain errors |
| English handwritten prose | Existing `microsoft/trocr-base-handwritten` on actual lines | PP-OCRv6; whole-region PaddleOCR-VL comparison | Later, with local writer-disjoint lines |
| Mixed text/formulas | `PaddlePaddle/PaddleOCR-VL-1.6` on answer regions | `Qwen/Qwen3-VL-4B-Instruct` as a secondary research comparison | No initially |
| Isolated formulas | `PP-FormulaNet_plus-M` comparison | UniMERNet; staff transcription | No initially |
| Hosted recognition reference | Azure Document Intelligence Read, if approved | Google Cloud Vision handwriting, if approved | Not needed for first comparison |
| Rubric-assisted text grading | Human baseline, then `Qwen/Qwen3-8B` experiment | Institution-approved hosted reference or manual grading | No until rubrics and labels are stable |
| Ownership, arithmetic, recipients, release | Deterministic validated application logic | Explicit human decisions | Never delegate authority to an LLM |

### 10.2 Why These Candidates

**PP-OCRv6 medium:** the current official recognizer uses a CTC inference head
and publishes handwriting-English results. That makes it a sensible conventional
OCR experiment, not a guarantee on boxed digits. The publisher reports 67.8% on
its handwritten-English subset, versus 83.2% weighted overall; neither measures
our roll fields. Start with recognition-only on known strips so the model does
not need to rediscover our layout. [Architecture and benchmark](https://www.paddleocr.ai/main/en/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html),
[official training configuration](https://github.com/PaddlePaddle/PaddleOCR/blob/main/configs/rec/PP-OCRv6/PP-OCRv6_medium_rec.yml).

**TrOCR base handwritten:** already integrated, approximately 334M parameters,
and intended for single-line handwriting images, with an IAM-fine-tuned release.
Its card lists MIT licensing. Reusing it gives a low-change baseline; it does
not make equal-height slicing or confidence heuristics correct. Whole answer
boxes are not automatically valid inputs for a line recognizer.
[Model card and intended use](https://huggingface.co/microsoft/trocr-base-handwritten).

**PaddleOCR-VL-1.6:** the publisher provides a compact document model with text,
formula and other document capabilities under an Apache-2.0-labelled model card.
It is a reasonable whole-answer candidate when line OCR loses structure.
Published document-parsing scores are not handwritten exam accuracy. Initially
use it in shadow mode, not as an identity or grading authority.
[Publisher model card](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6).

**Qwen3-VL-4B-Instruct:** an Apache-2.0-labelled vision-language checkpoint to
consider if the first mixed-content candidate leaves substantial errors. Do not
integrate several VLMs at once or assume a 4B model fits a 6 GB card at full
precision. Hardware and latency are experiment outcomes.
[Publisher model card](https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct).

**PP-FormulaNet_plus-M and UniMERNet:** the official formula module supplies
these specialized recognizers and structured mathematical output paths.
Evaluate isolated handwritten equations, including fraction bars and subscripts;
document/table benchmarks are insufficient. Keep human transcription for unsupported
notation. [Formula module and model list](https://www.paddleocr.ai/main/en/version3.x/module_usage/formula_recognition.html).

**Azure Read / Google Vision:** approved hosted references can help determine
whether a local model or the crop itself is failing. Azure returns words,
polygons and confidence; its handwriting-style confidence is not a full-roll
correctness probability. Google documents handwriting through
`DOCUMENT_TEXT_DETECTION`. Neither service bypasses ownership checks, privacy
approval or our benchmark. [Azure Read](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/prebuilt/read?view=doc-intel-4.0.0),
[Google handwriting OCR](https://docs.cloud.google.com/vision/docs/handwriting).

The published OmniHandwritingOCR research offers additional motivation to test
handwriting and formula subtypes separately. It is not a head-to-head SmartOMR
evaluation and must not be used to assign its older checkpoint results to
newer model versions. [Research paper](https://arxiv.org/html/2608.18586v1).

Tesseract remains useful as a baseline and for printed text. Do not spend the
first development cycle fine-tuning it or stacking more preprocessing attempts
without measuring their effect. The historical failure is enough reason not to
presume the current local handwriting path is adequate.

### 10.3 Recognition Experiment Contract

Every adapter consumes a crop plus an explicit task/format, and returns literal
text, alternatives, character/sequence scores where available, quality flags,
timing, model revision and error details. It cannot directly assign ownership,
modify the roster, grade the answer or trigger a release.

Store results from each candidate separately. A decision policy consumes them;
do not have each backend quietly choose whichever candidate exists in the roster.
Model disagreement is a first-class result. Distinguish two differently processed
calls to one model from two genuinely different recognizers.

Benchmark the same frozen raw crops across variants:

1. Current baseline with actual current configuration.
2. Raw/light-normalized whole roll strip with PP-OCRv6 medium.
3. Exact per-cell recognition, preserving blanks and rejects.
4. Optional hosted reference on an approved subset.
5. Evidence policy combining outputs without discarding disagreements.

Report model-only results and end-to-end source-PDF results separately. A perfect
crop benchmark cannot validate alignment or student grouping.

### 10.4 Model Supply And Runtime

Record publisher URL, checkpoint revision, hashes, weight/license notices,
processor/tokenizer revision, runtime dependencies, device, precision and
generation settings. Download approved artifacts outside exam processing and
run production offline where practical. Never download a surprise checkpoint
when a professor starts a timed run.

Review any custom model code before enabling it; prefer standard reviewed
loaders and safe weight formats. Quantization is a separately evaluated model
variant, not a free speed improvement assumed to preserve accuracy.

The current package pins Transformers below version 5 but otherwise allows broad
dependency ranges. Keep a tested CPU application lock, a PyTorch OCR lock, and
a Paddle environment/container profile where compatibility demands separation.
Do not upgrade every dependency as a side effect of introducing one model.

## 11. Dataset, Fine-Tuning And Confidence

### 11.1 Ground Truth Before Training

With institutional authorization, use the old failed exam as an incident replay
dataset. Preserve original scans and independently reconstruct actual page
ownership; a manually named PDF is useful evidence, not automatically infallible
ground truth. New volunteer pilot booklets should supplement it with diverse
writers, programs, pens, scanners and print variations.

Separate labels for:

- Source-page/template-page identity, orientation and quality.
- Visible handwritten roll transcription, including ambiguous/illegible positions.
- Bubbled roll and program exactly as marked.
- Actual student ownership established by authorized evidence.
- Per-bubble visual state and per-question accepted response.
- Written region boundaries, actual lines, overflow and reading order.
- Literal prose and formula transcriptions.
- Adjudicated rubric criteria and marks, separate from transcripts.

If the student wrote the wrong roll, the correct OCR label is the visible wrong
roll; actual ownership is a different field. Do not train a recognizer to
"correct" images toward enrollment truth. Mark human uncertainty rather than
inventing labels for illegible ink.

Two annotators should independently check all identity labels used for acceptance
evaluation, then adjudicate disagreements. For written data, double-check a
substantial sample and all disputed formulas/critical symbols. Annotation tools
should hide model suggestions initially to reduce anchoring bias.

### 11.2 Dataset Size And Splitting

Initial collection targets, not sufficiency guarantees:

| Dataset | Initial target | Purpose |
| --- | --- | --- |
| Physical pipeline pilot | 20-30 complete booklets with 2/3/4-page layouts | Expose geometry, ordering, duplicate and review failures |
| Roll benchmark | All authorized legacy pages plus at least 100 distinct new writers where feasible | Exact-roll and rejection measurement |
| Specialist digit training | Roughly 10,000-30,000 labelled cells with writer diversity and hard negatives | Determine whether a small specialist helps |
| Prose adaptation | Roughly 2,000-5,000 independently checked real lines initially | Domain adaptation experiment, not a production certificate |
| Formula pilot | Several hundred varied handwritten regions, split by writer/question | Identify supported notation and dominant errors |
| Grading pilot | Several hundred double-marked answers across multiple questions | Test rubric consistency and model assistance |

If fewer examples are available, run a smaller exploratory study and explicitly
limit the claim. Do not claim a large training set by counting many augmentations
of the same student's writing as independent examples.

Split by writer first, for example 70/15/15 training/validation/test where group
counts allow. Keep all cells, pages, rescans and augmentations from a writer in
one partition. Reserve additional new-exam/template/scanner holdouts. Test-set
results may be examined only after configuration/threshold selection; if used
for tuning, that set becomes development data and a new sealed test is needed.

Store split manifests, hashes, annotation versions, permissions and sampling
rules. Keep personal identity mapping separately access-controlled; public Git
contains only synthetic fixtures and permitted code, never identifiable scans.

### 11.3 Specialist Roll Model: Conditional First Fine-Tune

If whole-strip OCR still fails on correctly cropped boxed digits, a specialist
per-cell classifier is the first sensible local training project. Start with a
standard torchvision ResNet-18 baseline, adapted to the input/label contract,
before considering a smaller architecture for speed. The official library
already provides the architecture. [Torchvision ResNet-18](https://docs.pytorch.org/vision/stable/models/generated/torchvision.models.resnet18.html).

Predict digits 0-9 plus blank and reject/ambiguous visual states. A reject class
alone is not sufficient for unknown inputs; add calibrated thresholds and
quality gates. Preserve per-cell alternatives, and evaluate full-roll exactness
and wrong acceptance, not only digit accuracy.

Suggested experimental setup, to tune on validation rather than present as an
optimal recipe: cross-entropy with measured class balancing, AdamW, head-only
warm-up then selective unfreezing, learning-rate trials around 1e-4 to 1e-3 for
the head and smaller backbone rates. Choose batch size by measured memory;
use early stopping on the safety/coverage objective and several fixed seeds.

Use realistic translation, mild scale/skew, blur, contrast, compression and
background variation estimated from actual scans. Do not horizontally flip
digits, apply arbitrary rotations, or crop away distinguishing strokes. Include
border-touching digits, empty cells, overwritten digits and multiple symbols.
Synthetic/public digit data can bootstrap only after license review; it cannot
replace local printed-box validation.

If segmentation itself fails because writing crosses cells, compare a standard
CTC sequence recognizer rather than forcing each character through a cell model.
Never multiply uncalibrated cell scores and call the result reliable identity.

### 11.4 Prose Fine-Tuning

Fine-tune TrOCR only after segmentation, decoder behavior and literal-transcript
evaluation are correct. Compare the unchanged checkpoint, limited-layer tuning
and a larger update only if data supports it. Use the same preprocessing at
training and inference, teacher-forced transcription loss with ignored padding,
validation CER and number/symbol error analysis, and early stopping.

Starting learning-rate trials for pretrained layers can be in the 1e-5 to 5e-5
range, with gradient accumulation and memory-saving options if verified. These
are experiment bounds, not a validated recipe. Do not promise full-model
fine-tuning fits the 6 GB laptop. Training needs gradients and optimizer state
that model-loading reports do not measure.

Keep misspellings and incorrect reasoning in labels. Include numbers, units and
short answers; do not adapt exclusively to long fluent sentences. Do not train
using the recognizer's own unchecked predictions. Store a model card describing
supported language/content, failure modes, test split and license lineage.

### 11.5 What Not To Fine-Tune Initially

- No general-purpose VLM or grading LLM before a useful held-out dataset exists.
- No layout detector when the manifest already locates the region.
- No bubble CNN before deterministic measurements and cancellation policies are benchmarked.
- No model trained to map a noisy roll to the nearest roster entry.
- No model trained on current test students and advertised as general handwriting accuracy.

Large-model LoRA/QLoRA may become worthwhile only if a specific recurring failure
remains after crop/prompt/policy fixes, sufficient authorized labels exist, and
the tuned version wins on unseen writers/questions without worsening severe
errors. The promotion decision must include latency, memory and maintenance cost.

### 11.6 Confidence And Acceptance

Keep raw model scores separate from calibrated acceptance scores and policy
states. Fit calibration on validation data only, using a simple established
method such as temperature scaling or isotonic calibration where appropriate.
Evaluate calibration and risk-versus-coverage curves on untouched test data.

Acceptance can depend on image quality, exact syntax, per-character uncertainty,
independent evidence and conflict flags. Do not hard-code "0.90 means safe" for
every backend. Calibrate a policy for the exact model/preprocessing/template
combination, then invalidate calibration when that combination changes.

The optimization order is: prevent observed wrong automatic ownership, then
improve coverage, then reduce review time and latency. Report all three. A model
that rejects everything has zero false acceptance but no useful automation.

## 12. Mailing And Release Reliability

### 12.1 Preserve The Working Release Boundary

Keep both audited-parser output and manually prepared folder-based release
workflows. Manual preparation remains a valuable recovery path, but a filename
such as `2024503.pdf`, `MT12345.pdf` or `PhD12345.pdf` is a declared identity,
not proof that every attached page belongs to that student.

Before preparing a release, reconcile PDF identifiers, marks and authoritative
roster entries. Show duplicate/missing identifiers, duplicate recipients,
unrecognized program prefixes, invalid emails, missing PDFs/marks and unexpected
files together. Do not infer an email from a roll or silently ignore excluded
students. Spreadsheet import must preserve leading zeros and expose header,
encoding, formula/cached-value and text-versus-number issues.

A release snapshot freezes recipient, attachment, message subject/body, score
and total, tentative/final label, key/rubric/bundle versions, approver and sender.
Recheck these hashes immediately before sending. A preview and a redirected
test email are separate from authorization to send the whole class.

Require explicit selection/count for partial releases and separate approval
for resends or corrections. Deduplication must cover the logical exam/student/
release revision across jobs, not just whichever CSV log happens to be in one
directory. A correction can legitimately be another release; make it visible.

### 12.2 Sender Authentication

Keep existing SMTP STARTTLS/SSL where institutionally supported. Do not ask for
a professor's ordinary password in chat or assume an app password is available.
The college must approve the sender identity and transport before exam day.

For Google Workspace, plan a Gmail API OAuth connector as an alternative. Use
the narrow `gmail.send` scope when only sending is required, with institutional
app approval and securely stored tokens. The API supports MIME-based sending;
it does not bypass account policy. Reading Sent Mail for reconciliation requires
additional permission and must not be silently bundled into send-only access.
[Gmail sending guide](https://developers.google.com/workspace/gmail/api/guides/sending),
[OAuth scope definitions](https://developers.google.com/workspace/gmail/api/auth/scopes).

A ten-minute login window is not a reliable deployment arrangement. Prepare and
approve artifacts beforehand, and arrange an approved durable authorization
method with IT. Verify quotas, attachment limits and sender permissions against
the actual account before each rollout rather than hard-coding an assumed limit.

### 12.3 Delivery State And Crash Recovery

Use a durable outbox with states such as prepared, approved, attempting,
accepted_by_provider, failed_retryable, failed_permanent and outcome_unknown.
Write an attempt record before contacting the provider. Store provider response,
message ID, recipient, attachment hash, attempt number and timestamps.

Transport acceptance followed by a process crash is not automatically safe to
retry. SMTP cannot participate in our database transaction. A timeout after
submission may mean the provider accepted the message. Mark the result unknown
and reconcile through authorized provider records or a human before retrying.
A stable Message-ID helps investigation but does not guarantee recipient-side
deduplication. Do not promise exactly-once external delivery.

Use short per-send database transactions/locks, recheck current outbox state
under that lock, and never hold a whole-class transaction open during network
calls. Rate-limit and back off known transient errors; stop on authentication
failure and expose clear progress. Distinguish accepted, bounced where known,
and delivered where independently reported. Never label server acceptance as
proof the student read the message.

Release tests must cover changed files/recipients, cross-student attachment,
stale scores, duplicate workers, process interruption, ambiguous provider timeout,
partial batches and explicit corrected resends. Use a controlled test transport
and consenting test recipients, not live student addresses during development.

## 13. Application Architecture And Persistence

### 13.1 Target Stack

Use one repository and a modular Python application with a few independently
managed processes. Do not introduce separately owned microservices for every
reader. A web process, CPU workers, an optional GPU worker and a delivery worker
can share domain contracts without sharing mutable global state.

| Component | Recommended direction | When |
| --- | --- | --- |
| Domain logic | Existing Python modules and Pydantic contracts | Preserve and strengthen immediately |
| Web/API | FastAPI with server-rendered templates and restrained existing JavaScript | After extraction/review services have stable contracts |
| Database | PostgreSQL; SQLAlchemy and Alembic for typed persistence/migrations | Before concurrent college deployment |
| Long-running jobs | Celery with Redis broker; authoritative job/outbox state in PostgreSQL | Before unattended durable deployment |
| Artifacts | Private filesystem with immutable IDs/hashes and an artifact-store interface | Start here; add object storage only when multiple hosts require it |
| Recognition | Isolated, pinned CPU/PyTorch/Paddle runtime profiles | With each approved experiment/deployment |
| Delivery | Dedicated permissioned worker and frozen outbox | Harden before the next bulk automatic release |
| Authentication | College-approved SSO through a maintained OIDC/SAML integration | Before any shared/network access |
| Deployment | Linux host, reproducible containers, reverse proxy/TLS, managed backups | College rollout, not an immediate local-development rewrite |

FastAPI's own guidance distinguishes heavy computation from small in-process
background work and suggests a separate task system such as Celery. This plan
uses that separation for restart recovery and resource isolation, not because a
web framework will improve OCR. [FastAPI background-task guidance](https://fastapi.tiangolo.com/tutorial/background-tasks/).

Celery is not itself a correctness guarantee. Its documentation explains
idempotency, acknowledgements and redelivery tradeoffs. Workers must verify
dependencies and use idempotent stage commits; a database job record and an
outbox dispatcher should recover work even if broker publication fails.
[Celery task semantics](https://docs.celeryq.dev/en/stable/userguide/tasks.html).

Do not add Kubernetes, Kafka, a vector database, a custom distributed scheduler,
or multiple frontend frameworks for this workload. Start with one deployment
host if measured capacity permits. Windows remains supported for the current
CLI/local development; the college job runtime can be Linux without forcing a
premature migration of every contributor's laptop.

### 13.2 Data Entities And Constraints

Proposed logical entities, not a migration implemented by this document:

| Entity | Key data/invariant |
| --- | --- |
| Exam/template revision | Exam ID, schema, layout hash, question definitions, generator version; immutable once used |
| Print batch/booklet | Issued opaque IDs, template/page slots, status and payload version |
| Upload | Original path/object ID, hash, bytes, owner, exam context and page count |
| Source page | Upload ID + source index, page geometry, content hash and disposition |
| Stage attempt | Stage/config/model versions, dependency hashes, timestamps, result/error and artifact IDs |
| Artifact | Content hash, MIME/type, byte size, storage key, lineage, retention class |
| Identity observation | Raw program/roll/code candidates, crop IDs, scores and flags; never overwritten by resolution |
| Page assignment | Source-page ID, student/booklet, template slot, decision revision and evidence |
| Student/exam participation | Program + exact roll identity, roster revision, participation status |
| Response observation | Question ID, source region, literal data, bubble measurements or OCR variants |
| Accepted response | Observation/correction reference, actor, revision and dependency links |
| Key/rubric revision | Question policies, effective weights, approval and original import hash |
| Grade | Accepted response version + grading policy version + criterion marks and state |
| Review event | Actor, action, reason, before/after revision, evidence IDs and timestamp |
| Bundle | Ordered approved pages, PDF artifact/hash, ownership revision and completeness |
| Release | Frozen recipient/content/bundle/grade revisions, approval and release scope |
| Delivery attempt | Outbox item, transport identity, attempt state, provider ID and uncertainty |

Enforce uniqueness of active page assignment and accepted template slot in the
database, not only UI checks. Store OCR text as data, never path fragments or
SQL. Keep full roll strings and program dimensions; use internal opaque IDs for
storage so special characters cannot create path traversal.

Use the database's established constraint mechanisms and existing persistence/
migration tooling rather than ad hoc schema edits. These are implementation
choices, not infrastructure currently installed in SmartOMR.
[PostgreSQL constraints](https://www.postgresql.org/docs/current/ddl-constraints.html),
[SQLAlchemy overview](https://docs.sqlalchemy.org/en/20/intro.html),
[Alembic migrations](https://alembic.sqlalchemy.org/en/latest/).

Use foreign keys and revision checks to prevent orphan grades or releases.
An audit table with access controls and immutable application semantics is the
first step; do not call ordinary editable JSON cryptographically tamper-proof.
Document administrator powers and backup/audit retention separately.

### 13.3 Dependency Invalidation

| Changed item | Recompute/reapprove | Keep reusable |
| --- | --- | --- |
| Source PDF bytes | New upload/page inventory and all dependent artifacts | Unrelated exams |
| Manifest geometry | New alignment/crops/readings/decisions as affected | Original source files |
| Selected alignment | That page's crops/readings and affected decisions | Other pages' accepted revisions |
| OCR model or decoder | New observations and re-evaluation of dependent decisions | Raw/aligned images with matching provenance |
| Page ownership/duplicate selection | Affected bundles, student aggregates, grade bindings and release eligibility | Source-indexed unchanged observations |
| Accepted response/transcript | That question's grade and relevant totals/releases | Image extraction and other answers |
| Answer key/drop policy/rubric | Affected grades, totals and release approvals | Accepted student responses |
| Roster email | Recipient validation and unsent release approval | Page geometry and grading |
| Message body or sender | New release preview/approval | Verified underlying bundle/grades |

Do not mutate already sent historical records. A superseding correction refers
to the previous release and requires explicit authorization.

### 13.4 Job Execution

Use page/stage-sized tasks with bounded work, not one inseparable 300-page task.
Persist queued/running/failed/completed state, attempt number, heartbeat,
dependency fingerprint, worker version, cancellation request and error category.

Task messages contain IDs, not entire image arrays or credentials. A worker
checks whether an equivalent successful result already exists, acquires the
appropriate lease/claim, computes, writes artifacts atomically, then commits
result metadata. Recovery must distinguish an expired attempt from an active
slow worker. A repeated task cannot create a second accepted decision.

Separate retriable infrastructure failures from permanent input problems and
human-review issues. Back off transient failures with bounded retries; repeated
OOM or corrupt-PDF failures must not loop forever. Cancellation stops at a safe
boundary and preserves completed pages. Do not automatically restart delivery
tasks whose external outcome is unknown.

Start with a small bounded CPU pool and one GPU inference worker per GPU. Avoid
multiple processes each loading the same large model. Route models requiring
different environments through explicit versioned adapters, not shared mutable
interpreter state. Load models once per worker and batch compatible crops after
measuring memory and latency.

## 14. Hardware, Performance And Capacity

### 14.1 Hardware Reality

The user's verified other laptop is an **RTX 4050 Laptop GPU with 6 GB VRAM and
16 GB RAM**, not an assumed 4060. The earlier report showed TrOCR base loading
on CUDA, around 1.285 GB peak allocated GPU memory for that load measurement.
It did not measure real recognition, sustained inference, training or accuracy.

| Environment | Sensible role | Do not assume |
| --- | --- | --- |
| Current non-GPU laptop | Generation, inspection, deterministic readers, UI, test suite, controlled CPU benchmarks | Fast large-model handwriting processing |
| RTX 4050 6 GB / 16 GB RAM | Small-model experiments; TrOCR inference trials with small batches; digit-model work | Full-model HTR/VLM fine-tuning or concurrent large models will fit |
| College 12 GB GPU scenario | More inference headroom, measured batch experiments | Adequate for every selected model/precision |
| College 24 GB GPU / 32-64 GB RAM scenario | Broader HTR/VLM experiments and larger pilot workloads | Necessary purchase or guaranteed runtime before benchmarking |
| Larger college hardware | Only when validated workload or training requires it | Bigger hardware will repair evidence/identity errors |

Budget model weights, activations, decoder/cache state, batch size, beam count,
runtime overhead and other processes. Parameter count alone is insufficient.
Do not load TrOCR, Paddle and a VLM simultaneously on the laptop simply because
each loads separately. Prefer sequential experiments with recorded peak memory.

### 14.2 Measure Before Promising A Runtime

Record per-stage wall time: intake/hash, PDF rendering, fiducial search, local
registration, crop writing, bubble reading, each OCR backend, ownership
resolution, bundle assembly, grading and release preparation. Include cold model
load separately from warmed inference. Sample median, p95, slowest pages and
failure/retry time, not only the best page.

Report pages/second and crops/second together with DPI, CPU threads, model,
precision, batch size, input distribution, hardware, RAM and VRAM. Record end-to-end
time including IO. Use realistic scans rather than duplicated clean pages alone.

An A4 page at 300 DPI is roughly 8.7 million pixels: about 8.7 MB as uint8 gray
or 26 MB as RGB before temporary copies. Retaining 300 RGB pages alone is roughly
7.8 GB. This arithmetic explains why bounded page storage matters on a 16 GB
machine; it is not a measurement of the current process's peak RAM.

Use artifact paths and an explicitly bounded image cache. Reuse accepted
alignment/crops, reuse opened PDF handles where safe, avoid redundant OCR
variants, and tune CPU/OpenCV/Tesseract thread counts to avoid oversubscription.
Profile first; do not sacrifice disagreement checks merely for a fast label.

### 14.3 Capacity Test And Operator Feedback

Run representative 10/50/150/300/600-page benchmarks as the pipeline matures.
Separately test 300 students with the maximum approved booklet size. "300 pages"
is not equivalent to "300 students". Keep disk headroom for source, derivatives,
attempt history, crops, student bundles and release snapshots.

The UI should show active stage, pages processed/failed/held, recent heartbeat,
model/device, last error and a conservative ETA after enough observations.
An ETA range should reflect retries and warm-up, not a fabricated precise finish
time. A stalled heartbeat must be distinguishable from slow but progressing work.

Choose operational service targets after the first measured baseline: acceptable
time to first reviewable page, full-exam processing window, review minutes per
student, maximum concurrent exams and storage retention. Publish the measured
capacity envelope, not an unsupported "300 pages will finish in X minutes".

## 15. UI, Security And Operations

### 15.1 Professor And Operator Workflow

Keep the brand SmartOMR and the first screen an actual work queue. Navigation:
Exams, Runs, Page Inspection, Identity Review, Answer Review, Grading and Release.
Use dense filterable tables, stable page previews and explicit counts. Do not
replace unresolved issues with a large green success card.

Intake requests PDF and manifest first. Roster/key/rubric can be attached later
with revision-aware import previews. Report invalid rows and required decisions
before expensive downstream work. No raw Python exception should be the only
explanation shown to staff; include the affected question/page/row and recovery.

Provide keyboard-efficient review, next unresolved item, side-by-side evidence,
zoom/rotation, accessible contrast/focus, resumable sessions and clear revision
conflicts. Never allow a collapsed/hidden warning to remain release-eligible.
Retain a CLI for repeatable operations against the same services, not a separate
implementation with different ownership rules.

Future conversational assistance may answer "Which pages still need review?"
or prepare a draft report by calling permissioned read APIs. It cannot override
ownership, silently correct marks, invent recipient addresses, access secrets,
or send a release without the same explicit approvals. Retrieved student text
remains data, not instructions. This is a late-stage interface, not core logic.

### 15.2 Security Boundaries

The current server must remain local-only until deployment hardening is complete.
Python's documentation explicitly warns that `http.server` is not recommended
for production. [Python HTTP server documentation](https://docs.python.org/3/library/http.server.html).

Before shared use:

- Authenticate users and enforce per-exam authorization on every API and artifact.
- Separate operator, reviewer, grader, release approver and administrator powers;
  support a second approver for sensitive overrides and bulk release.
- Protect sessions, state-changing requests, uploads and downloads; test CSRF,
  XSS, path traversal and cross-exam object access.
- Escape roster names/OCR text in HTML; neutralize spreadsheet-formula injection
  in exported CSV without silently altering source identity data.
- Validate content and size, not merely filename extensions. Reject ZIP traversal,
  archive bombs, symlinks and unexpected nested content if archive intake exists.
- Run PDF/image/model parsing with resource limits and least privilege; do not
  evaluate embedded actions or trust extracted executable content.
- Keep database/broker private, use TLS where traffic leaves the host, protect
  storage/backups, and minimize personal data in logs.
- Store sender tokens and application secrets in an approved secret store,
  not source control, downloadable run files, command history or model prompts.
- Restrict OCR worker network egress; local recognition should not unexpectedly
  transmit scans or fetch remote code/models.
- Test dependency/security updates in staging; keep an SBOM and license inventory.

College policy must decide retention, permitted cloud processing, access logging,
data locality, incident response and any training use of student work. This plan
does not make a legal-compliance claim. PyMuPDF has AGPL/commercial licensing;
have the institution review the chosen distribution/deployment obligations rather
than assuming every dependency is permissive. [PyMuPDF licensing](https://pymupdf.io/licensing).

### 15.3 Operational Readiness

Create staging with synthetic or specifically authorized data and a disabled
real-mail transport. Back up database, original uploads, referenced artifacts,
model manifests and configuration. Regularly restore into an isolated environment
and verify bundle/release hashes; a backup that has never been restored is not
demonstrated recovery.

Agree recovery-point and recovery-time targets with IT. Monitor disk capacity,
job age, heartbeat failures, per-stage errors, review backlog, OCR rejection
changes, GPU/RAM utilization, outbox uncertainty and provider failures. Alerts
should contain opaque run IDs rather than full student details.

Prepare runbooks for wrong ownership, damaged scans, missing pages, stale grades,
model failure, worker restart, disk exhaustion, database restore, credentials
revocation and incorrect email. On suspected wrong attachment, stop pending
releases, preserve evidence, notify authorized staff and follow college incident
procedures. Do not erase logs or quietly resend a different file.

## 16. Verification And Acceptance Plan

### 16.1 Test Layers

| Layer | Coverage |
| --- | --- |
| Unit/contracts | Coordinates, schemas, exact identifier syntax, finite values, score arithmetic, policy states |
| Generated-sheet readback | All question types, widths, page counts, old manifests, overlays and sampled bubble positions |
| Integration | Intake through crops/identity/grades/bundles with controlled model adapters |
| Real-scan benchmark | Independent page ownership, literal handwriting and marks on unseen writers/scanners |
| Browser workflow | Intake, progress, review changes, concurrent edits, release preview and errors |
| Recovery/fault injection | Process kills, stale cache, broker/DB outages, disk full, OOM, failed page and uncertain delivery |
| Security | Artifact isolation, malformed inputs, authorization, hostile OCR/HTML/CSV content |
| Physical pilot | Printed, filled, scanned booklets including real user mistakes and photocopy variations |
| Operational load | Representative long PDFs, concurrent jobs, backups and restore |

Keep tests beside the modules they cover. Add CI from a clean clone, not just a
developer's existing virtual environment. Run a fast deterministic suite on
every PR; full suite and synthetic PDF roundtrips before merge. Optional ML
jobs need pinned environments and offline approved weights; GPU benchmarks can
run separately on the controlled hardware. Never require live email credentials
or private student files for ordinary CI.

### 16.2 Required Regression Cases

Intake/geometry:

- Same exam ID and filenames but different PDF/manifest bytes cannot reuse cache.
- Changed DPI/config/renderer/model revision invalidates only dependent artifacts.
- One corrupt page stays in inventory while later pages continue.
- Rotation, cropping, missing markers, inferred markers, faint page marks, wrong
  template and mixed exam pages are all explicit outcomes.
- Coordinate round trips and overlays cover local roll/numerical/written regions.

Identity/grouping:

- Bubbled and handwritten mismatch on page 1; either channel blank/ambiguous.
- In-roster versus out-of-roster OCR disagreement stays a conflict.
- A cell returning two digits, a letter, a blank or punctuation cannot become a
  confidently accepted single digit by truncation.
- All permutations of a small two/three-student fixture produce the same accepted
  associations; page 2 before page 1 is ordinary pending work.
- Two-, three- and four-page booklets, duplicates, missing anchors, missing
  continuations, repeated wrong rolls, one-digit-different rolls and program clashes.
- No source page assigned twice; no missing page filled using nearest roster/order.
- Duplicate selection is identical in preview, regrading and final PDF.

Objective/written:

- Blank/single/multiple/faint/erased/cancelled bubbles; zeros/repeated numerical
  digits, incomplete columns and horizontal legacy versus vertical new grids.
- Missing key rows fail preflight; excluded Q2/Q15 do not crash or renumber.
- Effective totals, alternate-key policy and corrected-grade invalidation.
- True blank versus lone zero/decimal/minus sign, edge writing, overflow,
  merged lines, formulas, crossed-out work and token-budget truncation.
- Non-finite marks and wrong question/rubric revisions are rejected.
- Student-written instructions cannot alter grader rules or tool permissions.

Review/release:

- Two reviewers updating one item cannot silently overwrite each other.
- Corrected ownership/response/key invalidates appropriate unsent release.
- Unauthorized artifact access and cross-exam assignments are denied.
- Recipient/PDF/body edits after preparation block sending.
- Duplicate workers and cross-release retries do not silently resend.
- SMTP/provider acceptance followed by lost acknowledgement/log commit creates
  an uncertain state requiring reconciliation.
- Interrupted recovery cannot switch the chosen student attachment.

### 16.3 Metrics And Gates

Separate non-negotiable invariants from empirical performance targets.

| Area | Measure | Initial release rule |
| --- | --- | --- |
| Source accounting | Every page assigned, unresolved, duplicate, failed or explicitly excluded | 100% accounted for; no silent disappearance |
| Ownership | Wrong automatically accepted assignments; exact-roll accuracy; coverage | Zero observed wrong accepted assignments on agreed pilot; all pilot bundles manually checked |
| Identity review | Reject rate, cause breakdown, review time | Reduce effort only without increasing false acceptance |
| Objective extraction | Per-question exact answer, false mark/blank rates and ambiguity | Pilot accepted answers independently checked; severe discrepancies block promotion |
| Written OCR | CER/WER, exact numbers/symbols, omissions/hallucinations, segmentation coverage | Reviewable drafts only until content-specific targets are agreed and met |
| Grading | Criterion and score agreement, large errors, false zero/full marks | Human approval initially; no unattended AI grades |
| Bundle/release | Correct ordered source-page mapping and exact recipient/attachment pairing | 100% checked in pilot; any mismatch stops release |
| Reliability | Resume equivalence, loss/duplication under fault injection | No overwritten approvals or unsupported retry side effects |
| Operations | Time, memory, failure rate, recovery and review effort | Measured workload fits agreed deployment envelope |

Report confidence intervals and absolute counts. With zero errors in N genuinely
independent cases, a rough one-sided 95% upper bound is 3/N: 300 cases supports
only about 1%, 3,000 about 0.1%, and 30,000 about 0.01%. Repeated scans/cells from
the same writers are correlated, so do not count them as independent evidence.
Use writer/exam-clustered uncertainty estimates where appropriate.

Those figures explain why a small clean demo cannot establish near-perfect
privacy-sensitive ownership. The institution must choose acceptable risk and
human verification policy. Unique booklet IDs improve the available evidence;
they do not remove the need to validate identity binding and code conflicts.

### 16.4 Pilot Progression

1. Synthetic regressions and preserved incident fixtures, no real mail.
2. Controlled physical 20-30-booklet pilot, full independent verification.
3. Approximately 50 students, mixed programs, agreed scanner workflow, full review.
4. 150-student/two-page-class replay or approved shadow run, no autonomous release.
5. 300-student load and recovery exercise with actual planned page counts.
6. Limited supervised live exam, explicit staff sign-off and rollback procedure.
7. Expand only the categories whose measured evidence supports it; keep all
   failures and new layouts in shadow/review until evaluated.

Synthetic perturbations complement real scans. They do not prove handwriting
generalization. Shuffled copies of one good scan test ordering, not 300-writer
recognition accuracy.

## 17. Incremental Delivery Plan

Do not combine the following into one large commit. Each item needs a scoped
diff, tests, example artifacts where relevant, review and a recorded result.
Durations depend on data access and failure findings; gates, not deadlines,
determine readiness.

| Step | Deliverable | Main touch points | Exit gate |
| --- | --- | --- | --- |
| 1 | Characterize and fix provenance-aware alignment caching | `workflows/batch.py`, shared contracts, inspection tests | Input/config swaps cannot reuse stale alignment; old results preserved |
| 2 | Strict identity evidence and finite-mark validation | `reader/handwriting.py`, written grading/import validation | Conflict suppression/truncation/NaN regressions pass |
| 3 | Shared page artifacts consumed by batch and UI | `ui/inspection.py`, `ui/app.py`, `workflows/parse.py`, `batch.py` | Evaluated artifact hash equals inspected selected revision; restart parity |
| 4 | Incident replay dataset and benchmark runner | `datasets/`, test fixtures, evaluation reports | Independently checked labels, writer splits and baseline report |
| 5 | One replacement roll adapter in shadow mode | `reader/handwriting.py` adapter boundary, optional runtime profile | PP-OCRv6 compared on exact same inputs; acceptance/coverage report |
| 6 | Versioned identity decisions and bundle selection | `workflows/batch.py`, `review.py`, UI | Shuffle/duplicate/missing-page invariants and corrected-page invalidation |
| 7 | Optional unique-booklet print mode | `generator/`, manifest contracts, code reader | Physical decode/duplicate/conflict tests and professor approval |
| 8 | Objective response policy and regrading | `grading/`, `reader/numerical.py`, key import/workflows | Real-scan answer benchmark; dropped-question and revision tests |
| 9 | Durable release/outbox hardening and sender integration | `workflows/email.py`, release services | Controlled transport fault tests, recipient/PDF preview and approvals |
| 10 | Written regions, actual lines and transcript review | `reader/written.py`, `written_ocr.py`, UI | Crop completeness, overflow, literal text/formula benchmark |
| 11 | Conditional specialist/prose training | `datasets/`, model adapters and registry | Unseen-writer gains without worse severe errors; reproducible model card |
| 12 | Rubric-assisted grading pilot | `grading/written.py`, written workflow, UI | Staff-labelled benchmark, bounded structured marks, human approval |
| 13 | Persistent authenticated college deployment | services, PostgreSQL/migrations, Celery, FastAPI, ops | Authorization/load/restart/restore/security gates |
| 14 | Supervised rollout and monitoring | Runbooks, staff training, release controls | Signed pilot report and tested incident recovery |
| 15 | Optional conversational interface | Read-only/permissioned APIs | Cannot bypass any existing verification or release gate |

Steps 4 and the design approval for 7 can progress alongside early code repairs.
Persistence/security design can start early, but shared network access must wait
for step 13. Step 9 hardening must precede another automatic bulk send. Written
and ML work must not delay the correctness fixes in steps 1-3.

### 17.1 First Three PRs In Detail

**PR 1: make alignment-cache reuse evidence-based.** Add characterization
fixtures for same path/different PDF, changed manifest, DPI and configuration;
introduce a versioned provenance record; treat old cache entries as misses;
write new outputs atomically without deleting old runs. Keep existing CLI
arguments and numerical layouts unchanged. Rollback: select the earlier code
with a fresh run directory, never pretend new artifacts belong to old code.

**PR 2: preserve identity conflicts and reject invalid observations.** Move
conflict detection before roster filtering, preserve original candidates, require
exact one-digit cell output and remove unsafe automatic coercion. Add negative
cases for leading zeros/programs and non-finite marks. Recognition thresholds
and models stay unchanged so effects are attributable. Rollback must not restore
unsafe auto-release eligibility; new ambiguous cases remain held.

**PR 3: evaluate the inspected artifact.** Extract neutral page-artifact contracts
and route both CLI/UI evaluation through selected revisions. Replace batch-wide
resident arrays with artifact references where needed for this boundary. Add
tests proving manual/selected alignment is the image read by downstream readers,
and that unchanged completed pages are reused after interruption. No OCR-model
change in this PR.

Review PR 2's two validation concerns separately if its diff becomes too large;
the table expresses delivery order, not a requirement to bundle unrelated fixes.

### 17.2 Working With Your Partner

One person owns page provenance/contracts and pipeline integration; the other
owns labelled benchmark preparation and model experiments behind the agreed
adapter. Review each other's ownership and release changes. Do not both edit
the grouping resolver independently or tune thresholds against different data.

Use short feature branches, a written experiment ID, small PRs and clean-clone
verification. Preserve local experiments outside production imports until they
have dependencies, tests, dataset lineage and a clear owner. Never use `git add .`
without checking that student data, credentials and accidental artifacts are
excluded. A clean clone must reproduce application behavior with documented
optional model setup, not depend on private files from one laptop.

### 17.3 Definition Of Done For Each Change

- Before/after behavior and the failure being addressed are explicit.
- Tests reproduce the failure and cover the intended boundary.
- Current public CLI/API behavior remains compatible or has a documented migration.
- Preview/report evidence is inspected, not just generated.
- No privacy-sensitive data or secrets enter Git or external services.
- Dependency/model/config revisions and invalidation behavior are documented.
- Full relevant regressions pass; known remaining limits are stated honestly.
- A rollback/recovery path preserves reviewed decisions and original scans.
- Documentation distinguishes implemented behavior from proposals and experiments.

## 18. Decisions Needed From College Staff

These do not block the early cache/evidence fixes, but they block the named
downstream capabilities:

| Decision | Blocks |
| --- | --- |
| Can each printed booklet have a unique code, or must sheets be identical photocopies? | Preferred future grouping strategy |
| Exact BTech/MTech/PhD identifier formats and exception policy | Production identity validation |
| Scanner model/settings, duplex/separator behavior and expected volume | Physical acceptance and capacity envelope |
| Who may adjudicate identity and who approves a release? | Review roles and multi-user deployment |
| Official dropped-question, cancellation, multi-mark and rounding policies | Deterministic academic grading |
| Required written content: prose, equations, diagrams, languages and extra sheets | Written benchmark and routing scope |
| Permission to use historical/volunteer answers for evaluation/training | Dataset collection and fine-tuning |
| Whether any cloud OCR/grading provider is permitted, with retention/locality terms | Hosted comparisons |
| Available production CPU/RAM/GPU/storage and IT ownership | Deployment sizing and maintenance |
| Approved sender account, SMTP/OAuth/admin arrangement and release procedure | Production mailing |
| Data retention, backup/recovery and incident policy | Operational sign-off |

Do not invent these answers to make a demonstration appear complete.

## 19. Explicitly Deferred Work

- Chatbot-first operation and autonomous agents controlling grades or mail.
- Another sweeping repository restructure or UI rewrite before reliability fixes.
- Training a large vision/language model from scratch.
- Integrating every fashionable OCR service before comparing two serious candidates.
- Automatic ownership from nearest-roster search, page order or handwriting similarity.
- Automatic written marking without rubrics, checked transcripts/images and staff review.
- Negative/decimal numerical formats without new generator/manifest/reader contracts.
- Uncontrolled self-training on accepted predictions or private student data.
- A claim of perfect accuracy based on synthetic tests or a successful model load.

## 20. Practical Next Action

After this plan is approved, implement **PR 1 only**, inspect its results, then
continue through the gates. In parallel, prepare the permissioned ground-truth
pilot and ask whether unique booklet printing is acceptable.

The first visible improvement should be simple and verifiable: upload PDF plus
manifest, see every source page and its exact inspected result, resume without
mixing old data, and know that downstream reading uses that same artifact.
Then make the identity decision safer and measure a better recognizer. That is
the foundation needed before faster processing, written grading or a richer UI.

## 21. Relationship To Existing Documentation

- [Project specification](../PROJECT_SPEC.md): supported product/layout contracts.
- [README](../README.md): installation and current entry points.
- [Reliability architecture](RELIABILITY_ARCHITECTURE.md): earlier research;
  this roadmap expands delivery gates and current code observations.
- [Answer extraction research](ANSWER_EXTRACTION_AND_GRADING_RESEARCH.md):
  earlier written-answer investigation, to read alongside current model versions.
- [Email release workflow](EMAIL_RELEASE_WORKFLOW.md): currently implemented commands.
- [OMR dimensions](OMR_DIMENSIONS.md): current physical geometry; do not derive
  new coordinates from prose in an old handoff.
- [Detailed handoff](AI_HANDOFF.md): historical context, not a current-state guarantee.

The primary-source links next to each model and infrastructure recommendation
explain the external capability claims. The architectural decisions, sequence,
threshold proposals and experiment recipes are engineering recommendations for
SmartOMR, not performance guarantees made by those sources.
