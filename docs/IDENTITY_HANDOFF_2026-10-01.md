# Identity Pipeline Handoff - 2026-10-01

## Current Baseline

The ResNet evidence work was pulled through `dab54b5`. The identity-resolution
policy described below is layered on top of that baseline.

The seven-member fine-tuned ResNet ensemble is tracked in
`data/models/roll_digit_ensemble_7/`. The application uses the ensemble
manifest automatically when it is available.

## Implemented Changes

- Handwritten roll identity uses the exact manifest-defined digit cells.
- Whole-strip OCR is diagnostic only. A disagreement is recorded as
  `STRIP_DISAGREES`; it cannot erase a valid cell roll.
- Faint-ink enhancement disagreement no longer clamps confidence to 0.58.
- A blank roll field is detected before ResNet inference and gets
  `EMPTY_FIELD`; it cannot create a plausible fake identity.
- A blank/ambiguous continuation selector reads the BTech field only. It may
  attach only through exact agreement with a verified BTech page-one anchor.
  Blank-selector MTech/PhD continuation pages remain unmatched.
- Page-one Path A anchor evidence requires exact bubble/cell agreement, cell
  minimum probability at least 0.30, and cell geometric mean at least 0.55.
- Page-two recovery requires an exact usable cell roll and a unique verified
  page-one anchor. Duplicate claims remain unmatched.
- Roster suggestions remain review-only and cannot manufacture ownership.
- Identity evidence now uses stable `info`, `warn`, and `block` codes. Decision
  logic no longer depends on matching English review strings.
- The one-digit roster-neighbour guard requires the selected ResNet digit's raw
  probability to be at least `0.85`. Pairwise support is retained only for
  diagnostics.
- Path B proposes an exact page-one bubble/page-two cell match. A different
  valid page-one cell roll, duplicate slot, occupied slot, missing page-one
  anchor, or near-neighbour failure blocks shadow eligibility.
- Path C proposes exact page-one/page-two cell agreement when bubbles are blank
  or wrong. It is always review-only.
- A blank continuation selector may be suggested only for BTech. MTech and PhD
  blank selectors remain blocked.
- `identity_resolution.json` is non-mutating shadow output. Paths B and C never
  alter parser grouping automatically.
- The Review UI shows both pages, digit crops, probabilities, roster neighbours,
  and structured evidence. Approve, assign-to-another-roster-roll, and reject
  decisions are audited in `verified_index.json`.
- Accepting a suggestion invalidates stale PDFs and email eligibility. A second
  explicit student verification is still required before release.

## Current Measured Run

Local run ID: `CSE557_MIDSEM_2026_20260930_200512_1ddb8f`

The scanned input and generated run artifacts are intentionally ignored by
Git. They remain local under `data/ui_runs/` and must not be expected after a
fresh clone.

Latest regrouping result:

- Student bundles: 139
- Ready: 23
- Needs review: 116
- Unmatched pages: 81
- Page errors: 1

Historical unmatched-page counts:

- Original baseline: 168
- Before cell-first reader: 135
- Latest run: 81

The cell/strip conflict bucket fell from 74 to 3. The remaining unmatched set
is dominated by page-two records that have a literal roll but lack a verified
page-one anchor.

## Diagnostic Commands

```powershell
cd "C:\IIIT Delhi\BTP\SmartOMR"
.venv\Scripts\python.exe -m omr.datasets.tally_unmatched_reasons `
  --run-dir data\ui_runs\<run-id> `
  --out data\benchmarks\<name>\unmatched_tally.csv

.venv\Scripts\python.exe -m omr.datasets.analyze_identity_rejections `
  --run-dir data\ui_runs\<run-id> `
  --out data\benchmarks\<name>\rejection_analysis.csv

.venv\Scripts\python.exe -m omr.datasets.shadow_identity_tiers `
  --preview data\ui_runs\<run-id>\identity_preview\index.json `
  --students data\ui_runs\<run-id>\inputs\students.csv `
  --parse-index data\ui_runs\<run-id>\parsed\<exam-id>\parse_index.json `
  --out data\benchmarks\<name>\shadow_identity_tiers.json

.venv\Scripts\python.exe -m omr.datasets.evaluate_identity_resolution `
  --labels-csv data\benchmarks\<name>\page_labels_blind.csv `
  --resolution data\ui_runs\<run-id>\parsed\<exam-id>\identity_resolution.json `
  --output-dir data\benchmarks\<name>\identity_evaluation `
  --split held_out
```

## Verified Tests

```powershell
.venv\Scripts\python.exe -m pytest omr\reader\tests\test_handwriting.py -q
.venv\Scripts\python.exe -m pytest omr\workflows\tests\test_batch.py -q `
  -k "identity_grouping or exact_cross_page or duplicate"
```

At handoff: handwriting tests were `11 passed`; focused grouping tests were
`8 passed`.

## Remaining Validation Gate

1. Independently label the blind roll-cell and page ownership CSVs. Do not use
   model predictions as labels.
2. Inspect at least 20 shadow-eligible proposals using the full pages and digit
   crops in the Review UI.
3. Run the held-out digit and identity evaluators. Record full-roll accuracy,
   wrong-prediction confidence, proposal precision, rejected-but-correct pages,
   and false shadow auto-attachments.
4. Keep Paths B/C automatic attachment disabled unless the held-out ownership
   set has adequate coverage and zero wrong mappings.
5. Tune crops or fine-tune the ResNet only after labelled errors distinguish a
   model problem from bad crop geometry or genuinely illegible writing.

Do not use scan order or nearest-roster matching to assign ownership. The
roster validates literal OCR only; it never rewrites a prediction.
