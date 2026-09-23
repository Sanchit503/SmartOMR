# SmartOMR Reliability Architecture And OCR Recommendation

Research date: 2026-09-19.
Status: recommended design and evaluation plan, not implemented or validated performance.

## 1. Decision

Use a **template-guided assessment pipeline in a modular Python application**.
Keep image recognition, student identity decisions, grading, and release as
separate operations with explicit evidence and approval records.

The immediate local roll-recognition candidate is **PP-OCRv6_medium_rec**.
Benchmark it against the current backend and a cloud reference, initially
**Azure Document Intelligence Read**, only if college data-processing approval
and credentials are available. Google Cloud Vision is a second hosted candidate.
These are test selections, not an established ranking on SmartOMR handwriting.

For written answers, compare **TrOCR-base-handwritten on detected prose lines**
with **PaddleOCR-VL-1.6 on whole answer regions**, especially mixed text/formulas.
Mathpix is an optional hosted comparison for handwritten mathematics. No written
recognizer or grader is selected for unattended release yet.

For newly printed exams, add a unique booklet identifier on every page. This
reduces dependence on handwritten roll recognition for page association. It is
an additional print mode, not a prerequisite for investigating existing sheets.

Keep the current generator, manifest contracts, objective readers, and validated
email-release components. Fix their contracts and measured failures before a
framework migration. Changing web frameworks cannot fix incorrect OCR.

## 2. Research Findings

| Option | Verified capability | Recommended role and limitation |
| --- | --- | --- |
| Tesseract | Its maintainers describe it as designed for printed text, with poor handwriting suitability. [FAQ](https://tesseract-ocr.github.io/tessdoc/FAQ.html) | Retain as an incident baseline and optional printed-text reader, not the default handwriting dependency. |
| PP-OCRv6 medium | Conventional recognizer; published evaluation includes handwritten English. The documented inference head uses CTC. [Official documentation](https://www.paddleocr.ai/main/en/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html) | First local candidate for full roll strips; compare raw and border-aware crops. Boxed identifiers remain a distinct evaluation task. |
| Azure Document Intelligence Read | Extracts handwriting and returns word polygons/confidences. [Read documentation](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/prebuilt/read?view=doc-intel-4.0.0) | Hosted reference for rolls and prose. Handwriting-style confidence is not transcription confidence; neither automatically establishes full-roll accuracy. |
| Google Cloud Vision | `DOCUMENT_TEXT_DETECTION` supports handwriting with structured results. [Handwriting documentation](https://docs.cloud.google.com/vision/docs/handwriting) | Alternative hosted reference. No claim that it beats Azure or local models on these sheets. |
| TrOCR handwritten | Microsoft's released models include IAM-trained handwriting recognition. The base model has about 334M parameters. [Microsoft repository](https://github.com/microsoft/unilm/tree/master/trocr), [single-line intended use](https://huggingface.co/microsoft/trocr-base-handwritten) | Reuse the existing adapter as a prose-line baseline; do not feed arbitrary multiline boxes into a line recognizer or treat IAM results as identifier accuracy. |
| PaddleOCR-VL-1.6 | Compact 0.9B document model supporting text and formula tasks. [Publisher model card](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6) | Whole-answer/mixed-content candidate. Its document benchmark score is not handwritten exam accuracy, and it is not an identity authority. |
| Mathpix | API supports handwritten text and equations with structured mathematical output. [API documentation](https://docs.mathpix.com/) | Optional formula reference, with approved cloud use. Recognition is separate from judging whether the student's mathematics is correct. |
| Mistral OCR 4.1 | Current documented service offers paragraph boxes, structural labels, and confidence output. [Model documentation](https://docs.mistral.ai/models/ocr-4-1) | Broader document comparison if the first candidates are insufficient; not necessary to integrate every service initially. |

An important qualification: PP-OCRv6's own table reports 67.8% recognition
accuracy on its handwritten-English subset versus 83.2% weighted overall. Those
numbers describe the publisher's dataset, not our numeric fields. They support
testing the model, not promising near-perfect handwriting recognition.
[Published breakdown](https://www.paddleocr.ai/main/en/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html).

Independent handwriting research also finds that model rankings change with
content and formula complexity, and generative readers can invent plausible
corrections. That paper tests an older PaddleOCR-VL checkpoint, not version 1.6;
its numerical results must not be attributed to the newer model.
[OmniHandwritingOCR](https://arxiv.org/html/2608.18586v1).

Therefore a model can be a strong document parser and still be an unsuitable
automatic student-identity reader. Likewise, two recognizers agreeing does not
prove correctness: their errors can be correlated.

## 3. Processing Architecture

```text
Exam manifest + roster + separately versioned answer key/rubric
                         |
Uploaded PDF -> immutable source pages, hashes and page count
                         |
              alignment + local geometry/quality checks
                         |
             manifest-defined regions + preserved evidence
                         |
         +---------------+----------------+----------------+
         |               |                |                |
   page/booklet ID   bubble identity   handwritten ID   answers
         |               |                |                |
         +-------- identity evidence -----+        MCQ/numerical OMR
                         |                         written extraction
                exact identity resolver                   |
                         |                       response candidates
              proposed page associations                  |
                         +--------------+------------------+
                                        |
                    independent identity/extraction review
                                        |
                  verified student bundle + accepted responses
                                        |
              deterministic objective grading / rubric-based proposals
                                        |
                    approved PDF, score and recipient snapshot
                                        |
                         controlled email release
```

Answers can be extracted before ownership is resolved, indexed by source-page
ID. They cannot become a named student's accepted responses until ownership is
verified. An identity correction then reuses saved crops and extraction instead
of rerunning the whole PDF.

The manifest is the layout authority. A model need not rediscover where the
roll boxes, bubbles, or answer regions are. The roster is the recipient authority;
email addresses must never be inferred from roll patterns or OCR.

## 4. Identity And Grouping Rules

### Existing sheets

- Read bubbled and handwritten identity on page 1 as separate evidence.
- Preserve every raw candidate, including out-of-roster and conflicting reads.
- Validate exact program and full roll, crop quality, and calibrated acceptance.
  Roster membership alone is not proof and must not erase disagreement.
- Automatically accept a page-1 identity only with the required corroboration;
  a reviewer can explicitly resolve unreadable or conflicting evidence.
- Attach a continuation page only to the same accepted identity using its own
  accepted evidence or an audited human assignment. Never use source order,
  nearest roster number, or visual handwriting similarity as automatic proof.
- Collect evidence for the whole batch before resolving relationships. A page 2
  arriving before its page 1 is simply pending, not exceptional.
- Missing pages, duplicate page indices, reused source pages, conflicting program
  selectors, and multiple possible anchors remain unresolved.
- Suggestions may help a reviewer, but cannot silently change an identity or
  become a release PDF. Do not maximize the number of attached pages.

The saved failed crop inspected during research contains legible boxed digits.
That shows at least some failed examples are recoverable; it is not a measured
accuracy estimate. Test the entire crop path, not just the recognizer in isolation.

### Future booklets

Print a validated machine-readable payload with exam/template version, opaque
booklet ID, and page number. A registry validates issued codes; use established
encoding/decoding libraries, not custom barcode logic.

Generate one print-batch PDF containing distinct booklets. Every page of a
booklet shares its booklet ID but has a different page index. Different booklets
must have different IDs: photocopying one coded master for all students defeats
the scheme. If identical photocopies are mandatory, remain on the legacy OCR
path or arrange unique labels under a controlled distribution process.

Printing can happen without a roster. Bind an opaque booklet to an accepted
student identity on page 1. Pre-personalized printing is another mode when a
roster exists. Unknown, damaged, duplicated, or conflicting codes require review.
Keep readable identity fields and flag conflicting written identity; a barcode
identifies a booklet, not the person who physically used it.

## 5. Extraction And Grading

**Roll recognition:** retain raw color/gray crops and limited, reproducible
preprocessing variants. Compare whole-strip recognition with a separate
digit-cell recognizer where useful. Do not treat a language model's prose-like
completion as evidence. Never silently take just the first digit of a multi-digit
cell result. Preserve leading zeros and exact program prefixes.

A specialist digit model is an optional later improvement. Train it on diverse,
correctly labelled boxed writing; blank, damaged, and multiply written cells need
an explicit reject path. Training on the same students used for final evaluation
would not establish generalization.

**MCQ and bubbled numerical answers:** keep template-guided image measurements,
not OCR or LLM reading. Validate bubble-center alignment and local print/ink
contrast; retain measurements and overlays. Distinguish blank, single mark,
multiple marks, erasure/cancellation, and uncertain detection. Compare detected
answers and deterministic grades separately. Dropped questions require an
explicit approved policy, not deleting answer-key rows or changing the manifest.

**Written answers:** preserve the complete answer region with context and
overflow evidence. Detect actual lines for line OCR; the configured number of
printed answer lines is not the number of lines the student actually wrote.
Maintain a whole-region path for formulas and mixed layout. Do not remove
minus signs, fraction bars, decimals, or edge writing while cleaning rules.

Store literal transcripts and uncertainty with the image. An OCR model must not
repair wrong reasoning or spelling into a correct answer. A versioned accepted
transcript/formula representation feeds a separate rubric-based grading step.
Retain original images for visual grading/review where text loses structure.
Instructions written inside student answers are data, not grader instructions.

Written grades begin as proposals. Require approved question text/rubrics,
criterion evidence, finite in-range marks, deterministic summation, and human
adjudication where needed. Correcting a transcript or rubric invalidates affected
grades and release eligibility, not unrelated extraction.

Current code still divides answer regions into fixed bands in
`omr/reader/written_ocr.py:_save_written_line_crops_from_path`, and generation
confidence is not calibrated full-transcript correctness. Those need explicit
tests and correction before trusting a model replacement. See also
[the earlier written-answer investigation](ANSWER_EXTRACTION_AND_GRADING_RESEARCH.md).

## 6. Application And Deployment

Recommended college deployment: **FastAPI with a server-rendered review UI,
PostgreSQL, Celery/Redis workers, and private filesystem artifact storage**.
Use one repository and shared domain modules, not separately owned microservices.
Object storage can replace local storage when multiple hosts actually require it.

The current UI uses `ThreadingHTTPServer` and starts daemon threads for batch
runs. Preserve the useful screens while migrating persistence and jobs in stages.
The web request should submit work and display durable progress, not own the
lifetime of OCR. FastAPI itself recommends a separate tool such as Celery for
heavy background computation. [FastAPI guidance](https://fastapi.tiangolo.com/tutorial/background-tasks/).

PostgreSQL owns job state, evidence, identities, review decisions, and releases.
Redis transports work, not the only copy of a result. Use bounded per-page or
per-answer tasks, timeouts, heartbeats, bounded retries, and atomic artifacts.
A recovery process finds unfinished database jobs after an interruption.

Jobs must tolerate repeated execution. Celery documents redelivery and the need
for idempotent tasks; a queue alone does not provide exactly-once processing.
[Celery tasks](https://docs.celeryq.dev/en/latest/userguide/tasks.html).

Use CPU workers for PDF/geometry work and a bounded GPU worker for inference.
Keep models loaded between jobs and batch compatible crops. Share artifact IDs,
not large image arrays in queue messages. Display processed/total pages, current
stage, failed items, elapsed time, and measured throughput; flag stalled jobs.

Run production workers on Linux. Celery does not officially support native
Windows; Windows development can use WSL2/Linux containers, with GPU integration
verified separately. [Celery platform support](https://docs.celeryq.dev/en/stable/getting-started/introduction.html).

Model environments should be isolated and pinned. The repo currently specifies
`transformers<5`, whereas the documented PP-OCRv6 Transformers route requires
`>=5.8.0`; evaluate it in a separate environment or use its supported Paddle
runtime. Do not upgrade the working application environment blindly.
[PP-OCRv6 runtime instructions](https://www.paddleocr.ai/main/en/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html).

The confirmed laptop is RTX 4050, 6 GB VRAM, 16 GB RAM. Its reported TrOCR-base
load succeeded, but that was not a throughput/accuracy measurement. Start with
small batches and one model at a time. Do not promise that a whole VLM pipeline
fits because its weights alone fit. Measure memory and latency on the actual
hardware before choosing the college server or quantization settings.

## 7. Evidence, State And Release

Use stable IDs for exam version, scan, source page, crop, recognition attempt,
identity decision, student bundle, response, grade, review event, and release.
Every derived artifact records source hash, manifest hash, geometry/DPI,
preprocessing version, model revision/configuration, and code version.

Separate states: `identity_status`, `group_status`, `extraction_status`,
`grade_status`, and `release_status`. One generic `ready` flag cannot describe
all these facts. A reviewer action records actor, reason, previous value, new
value, and evidence. Concurrent review must reject stale edits.

The review UI should show side-by-side page identities and the original scan;
for answers, show the crop, detected selection/transcript, and grading rule.
Resolve only affected fields and reuse unaffected results. Teachers should not
need to maintain CSV files for ordinary corrections.

Create an immutable approved release snapshot of student ID, roster recipient,
page bundle, PDF hash, marks, and message. Release requires resolved ownership,
required pages or an explicit missing-page exception, and approved grading.
Missing-page exceptions must never approve ambiguous ownership. Input changes
invalidate the relevant approval and freeze sending until resolved.

Use an outbox and duplicate-send checks, but do not claim exactly-once SMTP
delivery: a connection can fail after server acceptance. Ambiguous transport
outcomes need reconciliation, not blind resend. Keep test recipients isolated.

Require staff authentication, course-scoped permissions, private artifact access,
upload limits, retention policy, backups/restoration tests, and secret storage.
Do not upload student images to public demos or cloud OCR without institutional
approval. Google documents service-specific data handling, but that is not a
substitute for college approval or checking the chosen provider's current terms.
[Google Vision data usage](https://docs.cloud.google.com/vision/docs/data-usage).

## 8. Evaluation And Implementation Order

1. **Freeze evidence.** Use the recovered 300-page correspondence from the
   corrected PDFs as provisional labels. Visually validate a subset and resolve
   disagreements. Label what is actually written separately from intended student
   ownership; a student can write an incorrect roll. Keep private data out of Git.
2. **Repair the identity contract.** Preserve conflicts, reject unsupported
   associations, invalidate stale caches, and keep suggestions out of approved
   PDFs. Verify this with targeted software tests before changing models.
3. **Run real OCR comparisons.** Current baseline versus PP-OCRv6 and an approved
   hosted reference. Use identical crops; measure raw and post-validation results.
   Report full-roll exact match, wrong accepted identity, review fraction,
   complete correct bundles, and wall-clock time. Count failures/timeouts too.
4. **Evaluate generalization.** Split by writer/student, keeping pages and crop
   variants together. Use development/calibration data for tuning and a locked
   test set only for evaluation. A previously trained-on exam is regression data,
   not an independent test. Add a later exam and broader program representation.
5. **Validate objective answers.** Independently label selected bubbles and
   apply the approved dropped-question policy. Test clean, faint, erased,
   cancelled, blank, double-marked, shifted, and skewed examples.
6. **Finish operational reliability.** Durable job resume, dependency-aware
   invalidation, shuffled and duplicate pages, output provenance, ergonomic
   review, authenticated deployment, and approved release. Benchmark the full
   PDF with bounded memory; do not extrapolate from model-load time.
7. **Pilot written extraction, then grading.** Measure prose CER/WER, critical
   number/sign/negation errors, formula structure, blank hallucinations, line
   omissions, and review effort. Compare grading on human transcripts first,
   then OCR transcripts, to identify which component introduces errors.

A release gate is zero observed wrong automatically accepted associations on
the locked evaluation set, alongside useful automation coverage and acceptable
review effort. Rejecting everything is not operational success. Zero observed
errors in a small or correlated dataset is not a guarantee of zero deployment
errors; collect additional independent exams and monitor accepted outputs.

This research added a recommendation document only. It did not install models,
upload student data, change application behavior, grade answers, or send mail.
