# Exact Ownership And Release Approval

Implemented policy: `exact-ownership-v1-2026-10-03`.

## Recognition Is Not Ownership

Each source page retains its source index, manifest sheet-page slot, literal
bubble and cell reads, probabilities, selector evidence, and alignment report.
Scanner order, roster similarity, and whole-strip OCR cannot establish ownership.
Neither a PDF with two pages nor a model's high confidence proves a correct match.

Only Path A is automatic:

1. Page 1 has a complete boxed roll exactly matching its bubbled roll and program.
2. Its bubble read is medium/high confidence. Every boxed digit meets the 0.80
   confidence gate. When class probabilities are present, the selected digit in
   each row must also meet 0.80.
3. Every continuation independently reads that same complete roll and program,
   meets the cell gate, and occupies a unique manifest slot.
4. Duplicate page-one claims or continuation claims block automatic matching.
   No "best page" silently resolves a duplicate identity claim.
5. A supplied roster validates identity/program; it never corrects the literal
   OCR to the nearest roster entry. At a differing digit of a one-digit roster
   neighbour, selected-class probability must be at least 0.85 in both relevant
   page reads. Missing probability evidence blocks that near-neighbour decision.
6. A blank BTech continuation selector may recover only through its distinct
   complete seven-cell field and an exact accepted BTech anchor. Blank or
   ambiguous shared MTech/PhD selectors cannot use this exception.
7. Structured blocking evidence and failed/unavailable quality gates prevent
   clean-match status and release approval.

These rules cover all manifest slots, not just pages 1 and 2. For shuffled input
`A1 B2 B1 A2`, accepted identity yields `[A1,A2]` and `[B1,B2]`, never adjacent pairs.
Page 3/4 follows the same independent continuation rule. A continuation can occur
before its anchor in the source file; ownership is decided after all reads exist.

The association pass enforces identity, while the student artifact quality pass
must additionally pass before its ownership certificate becomes clean. Any
uncertain quality remains reviewable, never release-approved automatically.

## Suggested And Unmatched

Path B (bubble plus matching continuation cells) and Path C (agreeing handwritten
cells without a confirmed bubble anchor) stay human-reviewed suggestions. Their
existing shadow diagnostics are not permission to auto-attach a page.
Scanner-order proposals have path `Order`, code `SCANNER_ORDER_ONLY`, and a
blocking severity. The old scanner-order overrides now select proposal order
only; they cannot attach pages. Blank/incomplete fields stay unresolved.

Whole-strip text remains diagnostic only. Box-frame-aware blank measurement
does not modify ResNet input crops, model weights, or training preprocessing.
The system does not learn page ownership from a previous run or PDF adjacency.

## Statuses And UI

- `auto_matched`: complete clean evidence under this policy, not email eligible.
- `approved`: an explicit batch release approval produced a current PDF.
- `verified`: an individual human manually checked the current selection.
- `needs_review` / `missing_pages`: an unresolved case, not email eligible.
- `pending_verification`: legacy ready output without a new ownership certificate.
- `rejected`: a human rejection, not email eligible.

In **Exam Dashboard** or **Sheet Matching Review**, use **Reassess Saved Evidence**
to apply this policy to unreviewed records from an existing completed run.
It reuses original roll evidence and quality reports without realigning pages,
loading OCR models, or repeating inference. It creates a review snapshot and
assessment under `ownership_reviews/`, leaves raw parser output untouched, and
preserves every previous human decision, manual selection, and ignored page.
Held/rejected/manual records are never silently upgraded or moved.

This action is explicit, revision checked, and blocked while another worker is
active. Merely opening a dashboard does not reassess or release any student.

Clean matches leave the manual-review queue. **Approve Clean Matches (N)** requires
an explicit confirmation and rechecks cached ownership, selected slots, source
uniqueness, and current image artifacts. It generates PDFs for the whole batch
before writing approvals. A failure cannot partially approve the saved index.
Approval is not presented as individual manual verification.

Uncertain ownership stays in Suggested or Unmatched, with existing exact-roll
approval, rejection, manual assignment, and student-page verification actions.
All views and filters use current review statuses rather than original parser
counts. The current-selection preview uses the selected pages in manifest order.

## Grouping And Grading Stay Separate

New UI runs have two actions:

1. **Detect Rolls & Group Sheets** chains page inspection, cached literal roll
   detection, and exact grouping in one worker. Progress still reports each phase.
   An answer key is neither required nor loaded in this stage.
2. **Grade Answers** accepts an answer key after grouping. It reads the currently
   selected canonical images, without realigning, recognizing rolls, or grouping
   again. Only complete confirmed/clean ownership selections are graded; unresolved
   cases are reported as skipped. Written answers remain professor-marked.

Grading results are staged under `grading_runs/`, preserving raw parser output.
A concurrent review change prevents their application. Changing selected pages
later marks the grades stale, clears their computed scores, and blocks email
release until regrading. UI tables, marks exports, and email preparation consume
the current grading results, not the earlier grouping-only details.

`--grouping-only` and the UI's first stage skip answer reading, grading,
and written-answer extraction. Numerical-answer warnings cannot block clean
ownership in that mode. Graded runs retain a separate `answer_review_flags`
collection, which can still require review before release. Graded legacy records
without separated answer evidence are not upgraded merely by reassessing identity.

## Email Safety

Matching never sends mail. Approvals and individual verification enable only a
later explicit email preparation/send operation. Missing pages remain ineligible.
Parsed-run email queues record a hash of the review index. Changed review
decisions invalidate a prepared queue, including between sends; prepare it again.
The independently frozen folder-release workflow is unchanged.

## Validation Limits

Synthetic tests cover shuffled 2/3/4-page bundles, blank/conflicting cells,
duplicate claims, program constraints, probability thresholds, cache-only
reassessment, manual-decision preservation, batch approval rollback, and stale UI
actions. These tests establish decision-rule behaviour, not real handwriting
accuracy. Confidence values are model scores, not calibrated guarantees.

Saved-evidence assessment of an actual run is read-only and has no independent
ground truth. Keep blind manual ownership labels and held-out writers separate
before claiming a false-auto-attachment rate or relaxing acceptance thresholds.
