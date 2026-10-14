# Identity And Review Workflow Correction Checklist

Date: 2026-10-03.
Inspected source baseline: `e7ca6d1`.
Status: baseline findings. The first corrective implementation is documented in
`EXACT_OWNERSHIP_WORKFLOW.md`; remaining recognition/benchmark work stays open.

This records the problems traced in the current examination run and relevant
code paths. It is not an exhaustive codebase audit or a handwriting accuracy
benchmark. Student-specific evidence is kept in the ignored local file
`data/maintenance/identity_workflow_evidence_20261003.md`, not in this document.

## Required Ownership Rule

**Scanner position may suggest ownership; it must not establish ownership.**

A page with conflicting, blank, or insufficient identity evidence belongs in
Suggested or Unmatched until the accepted evidence policy or an audited human
decision resolves it. Creating a two-page PDF does not prove correct ownership.

Recognition, ownership, answer review, grading, and permission to release must
remain separate. Preserve literal OCR, per-digit probabilities, and source IDs.

## Confirmed Findings

### ID-01: Positional Fallback Attaches A Conflicting Continuation (P0)

- Observed: a continuation's literal handwritten roll disagreed with the
  page-one bubble roll, but both pages were placed in that student's parser PDF.
- Mechanism: `parse_exam_bundle` selects the inferred scanner-order branch;
  `_group_records_by_inferred_order` calls `_positional_record`, which assigns
  the page-one anchor's roll and appends a disagreement warning.
- Existing safeguard: this observed bundle was `needs_review` and ineligible
  for email. It was not an identity-confirmed or released pairing.
- Problem: ownership is proposed inside a named bundle before the disagreement
  is resolved, instead of being represented only as a Suggested association.
- Required change: retain the continuation as unresolved; store the proposed
  association separately, with the original OCR and conflict visible. Do not
  include it in a confirmed student selection or release PDF without approval.

### ID-02: Uncorroborated Page-One Anchor Is Only Warned About (P0)

- Observed: the page-one handwritten read also disagreed with the bubbled roll
  in the ID-01 example. The inferred-order path still used the bubble anchor.
- Mechanism: failed `_page_one_write_in_matches_bubbles` appends a warning but
  does not prevent positional grouping in `_group_records_by_inferred_order`.
- Required change: an uncorroborated anchor cannot authorize continuation
  ownership. Preserve any valid Path B/C proposal under its explicit policy;
  otherwise hold the candidate for review.

### ID-03: Grouped Confidence Is Inherited From The Anchor (P1)

- Observed: the continuation record had grouped confidence `high` while its
  own OCR confidence was `low`.
- Mechanism: `_positional_record` sets confidence from the page-one anchor.
  Original confidence is retained in the payload, but the grouped label can be
  misunderstood as certainty about the continuation's ownership.
- Required change: expose OCR confidence, anchor confidence, association method,
  and ownership decision separately. A positional suggestion must not acquire
  high identity confidence solely from a clear page-one bubble read.
- Do not present a model softmax score as a calibrated probability that the
  entire student mapping is correct.

### ID-04: Different Grouping Branches Enforce Different Gates (P0)

- Confirmed code: `auto` may select inferred-order grouping before the strict
  `_group_records_by_identity` path is used.
- The strict path calls `_path_a_near_neighbour_evidence`; the inferred-order
  path does not apply that gate in the same way.
- `resolve_identity_proposals` runs after initial grouping as shadow output.
  Its existence does not make the earlier positional assignments safe.
- Required change: one ownership policy must govern all grouping entry points.
  Audit explicit sheet-major/page-major CLI modes as well as UI `auto` mode.
  An alternate branch must not turn a blocked identity into accepted ownership.
- Keep Path C review-only and existing Path B validation gates until independent
  labelled ownership evaluation justifies a policy change.

### MODE-01: Answer Warnings Pollute Grouping-Only Review (P1)

- Observed: this run selected grouping-only, but numerical-answer warnings
  affected the student review status.
- Diagnostic count: 46 original bundles had numerical question warnings;
  13 original bundles had no other review flags. These are flag counts, not
  independently validated ownership or answer-accuracy measurements.
- Mechanism: `_run_batch` invokes the common bundle parser. `_write_student_group`
  reads answers and collects their flags even without an answer key. Its final
  status is `needs_review` whenever the combined flag list is nonempty.
- Required change: grouping-only decisions must use ownership/selection evidence,
  not numerical-answer outcomes. If answer extraction is retained, keep its
  warnings in a separate answer-review domain without blocking ownership solely
  because an answer is faint, blank, or ambiguous.

### STATE-01: Severity And Review Domains Are Flattened (P1)

- Confirmed code: structured `info`, `warn`, and `block` identity evidence exists,
  but student readiness still depends on a combined list of English flags.
- Problem: an informational observation, an answer uncertainty, and an ownership
  conflict can all become the same student-level `Needs review` label.
- Required change: use structured reason codes, domain, severity, and an explicit
  decision state. Define which conditions block automatic ownership, which only
  inform a reviewer, and which block answer acceptance or release.
- Informational flags may remain visible without manufacturing a new task.
  Warnings must not simply be discarded: their effect needs a documented policy.

### UX-01: Every Clean Bundle Requires An Individual Approval Click (P1)

- Observed: a complete bundle with matching page-one bubbles, page-one cells,
  page-two cells, and no review flags still showed `Awaiting verification`.
- Mechanism: `_initial_status` maps parser `ready` to `pending_verification`;
  the UI initializes verification with automatic verification disabled.
- This is a release gate, not an OCR failure. It currently adds a per-student
  approval step even for clean matches.
- Required change: distinguish automatically matched ownership from human
  verification. Offer explicit batch approval of eligible clean candidates,
  with an exact count and recipient/attachment preflight.
- Do not label a system decision `Manually checked`. Do not bulk-approve every
  existing `ready` record solely because its string flag list is empty; first
  evaluate it under the unified ownership rules.

### UX-02: Proposed PDF And Confirmed Ownership Are Easy To Confuse (P1)

- Observed: a conflicting association was already visible as a two-page student
  bundle. This made the user reasonably ask why it had been matched.
- Existing UI separates current preview from the verified PDF, but the underlying
  parser grouping can still place unresolved ownership inside that preview.
- Required change: show Suggested candidates as proposed source-page pairs,
  separate from accepted selections. Display the conflicting literal roll and
  association method prominently. PDF existence is not an ownership status.
- No unresolved candidate may enter release preparation. Changing ownership must
  invalidate old approved artifacts and require an explicit new release decision.

### MODEL-01: Identity Disagreements Need Independent Labels (P1)

- Observed: the investigated student's bubble and handwritten reads disagreed;
  both handwritten reads had low confidence. This is an unresolved identity
  problem, not proof of the true handwritten characters.
- Required investigation: label the original field and individual digit crops
  independently. Separate crop/alignment problems, classification mistakes,
  blank fields, and genuinely ambiguous handwriting.
- Do not rewrite OCR to the nearest roster entry or train on guessed ownership.
  Do not promise that switching models alone will fix the association policy.
- ResNet recognizes digit crops; it does not learn which scanned pages belong
  together. Page count comes from the manifest, not from a two-page model rule.

### MODEL-02: Visibly Blank Field Produces A Spurious Roll (P1)

- Independently inspected the original continuation page and saved roll strip
  for a second case. The program selector and all visible roll boxes were blank.
- Saved cell result was incomplete, containing one missing digit. Only one cell
  was classified blank, and the saved `empty_field` flag was false.
- The whole-strip recognizer nevertheless emitted a complete, out-of-roster
  numeric string. It became a low-confidence literal candidate.
- `_read_roll_from_cells` marks a field empty using a blank-cell count or a
  low-ink heuristic. That guard failed on this visually blank scan. The exact
  crop/ink failure needs measurement; printed borders or scan noise are possible
  contributors, not a proven root cause from the current inspection alone.
- Required change: validate blank detection against actual empty printed boxes
  and faint genuine digits, independent of the classifier's predicted class.
  An empty field must not yield a usable roll or a roster-based identity guess.
- In the observed case, positional grouping still attached the page and inherited
  high grouped confidence. It remained ineligible for mail, but ownership was
  not established. Blank identity belongs in Unmatched; adjacency may appear
  only as an explicitly unconfirmed Suggested candidate.

### ID-05: Invalid Cells Can Fall Back To A Whole-Strip Candidate (P1)

- Confirmed code: `read_write_in_roll_number` adds a strip candidate when
  `cell_normalized` is absent but `strip_normalized` exists, unless the field is
  identified as empty. This happened in MODEL-02.
- This differs from the cell-first design's statement that strip reading is
  diagnostic only. Warning about missing cell confirmation does not make the
  strip candidate usable identity evidence.
- Required change: keep a strip-only string as diagnostic/suggested evidence,
  not an accepted literal cell identity. Incomplete cells and blocked structured
  evidence must survive through every grouping branch.
- If a continuation has no identity and pages may be shuffled, no scanner-order
  rule can recover its owner with certainty. Require human assignment or a
  validated per-booklet identifier; do not invent handwriting or ownership.

### VALID-01: Documentation And Some Tests Permit Different Policies (P1)

- `RELIABILITY_ARCHITECTURE.md` and `IDENTITY_HANDOFF_2026-10-01.md` say that
  scan order is not automatic ownership proof. The positional branch differs.
- The inspected test `test_auto_order_inference_recovers_sheet_major_bundle_with_one_failed_page`
  expects unreadable continuations to be bundled with warnings.
- Required change: align architecture, runtime branches, and test assertions.
  A passing test that expects the old behavior does not validate the new safety
  requirement. Check ownership precision, not just attached-page counts.

### PERF-01: Page-Level Inspection Is Sequential (P2)

- Confirmed code: `inspection.inspect_pages` processes one page per loop.
  Some native image operations may use internal threads; multiple pages are
  not inspected concurrently by this loop.
- At the earlier hardware check, the laptop had 4 physical cores, 8 logical
  processors, about 5.8 GiB usable RAM, and about 0.3 GiB free RAM.
- Proposed improvement: separately benchmark a bounded, memory-aware process
  pool, initially two workers only when memory permits. One coordinator owns
  index/progress writes; workers must preserve source IDs and isolation.
- Do not change DPI, detection thresholds, or identity rules to claim a speedup.
  Verify output equivalence and resumability. Do not restart the current run
  merely to collect a timing experiment.

## Out-Of-Order Failure Scenario

```text
Actual booklets: A1 A2 and B1 B2
Uploaded order: A1 B2 B1 A2
Page codes:     1  2  1  2
```

The page-code pattern is indistinguishable from normal consecutive booklets.
An order-only association would suggest A1+B2 and B1+A2. Readable conflicting
identities can flag the problem, but faint or absent continuation identity
cannot make adjacency reliable. A detected scanner pattern is not proof that
students' pages were not swapped.

This is a code-level risk scenario, not a claim that those particular shuffled
booklets were observed in the live examination or mailed incorrectly.

## Correction Order

1. Make unresolved/conflicting positional associations Suggested-only. Apply
   one identity gate to auto, explicit-order, and recovery paths.
2. Separate literal OCR, inferred associations, confidence, and ownership state.
3. Separate ownership review from answer review and grading in grouping-only runs.
4. Add the acceptance tests below and independently label the investigated fields.
5. Add explicit batch approval for candidates that pass the unified ownership
   policy. Keep sending and recipient/attachment preflight separate.
6. Evaluate bounded inspection parallelism after correctness changes are stable.

## Required Regression And Validation Checks

- Two-page swapped continuations with unchanged `1,2,1,2` page-code pattern.
- Arbitrary input order and continuation pages arriving before page one.
- Three- and four-page manifests with independently shuffled continuation slots.
- Page-one bubble/cell conflict, out-of-roster OCR, and valid-roster conflicts.
- Low-confidence conflicting continuation and completely blank continuation.
- Actual empty printed roll boxes must produce no usable identity. Strip-only
  recognition must not bypass incomplete cells or blank-field rejection.
- Near-neighbour hold in every automatic grouping entry point.
- BTech blank-selector recovery remains gated; no analogous implicit MTech/PhD
  program inference from their shared field.
- Duplicate agreed claims, duplicate slots, reused source pages, and missing pages.
- Suggested candidates stay outside confirmed selections and release queues.
- Grouping-only answer warnings do not become ownership problems.
- Batch approval excludes unresolved, blocked, incomplete, or stale candidates.
- Manual reassignment invalidates prior release eligibility and approved PDFs.
- No fresh OCR, forced verification-index initialization, or lost user decisions
  during a review-only policy migration.
- Inspect independently labelled full-page ownership results. Report wrong
  automatic attachments, rejected-but-correct pages, coverage, and calibrated
  confidence; do not use fewer unmatched pages as the sole success metric.

## Safeguards To Preserve

- The observed conflicting bundle is blocked from mailing unless a human verifies
  it. Verification itself sends no email.
- Immutable source numbering, raw identity evidence, checkpoint/resume artifacts,
  duplicate-selection checks, stale-revision protection, and decision audit logs.
- Roster email addresses are the recipient authority, never guessed from OCR.
- Current manual decisions and current run artifacts must not be rewritten while
  documenting these findings. Any later migration needs backups and an audit.

No runtime code, approval policy, OCR weights, or current run was modified as
part of creating this checklist.
