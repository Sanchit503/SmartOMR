# Identity Pipeline Handoff - 2026-10-01

## Current Commit

`9b9dd01 harden handwritten identity evidence and diagnostics`

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
  --out data\benchmarks\<name>\shadow_identity_tiers.json
```

## Verified Tests

```powershell
.venv\Scripts\python.exe -m pytest omr\reader\tests\test_handwriting.py -q
.venv\Scripts\python.exe -m pytest omr\workflows\tests\test_batch.py -q `
  -k "identity_grouping or exact_cross_page or duplicate"
```

At handoff: handwriting tests were `11 passed`; focused grouping tests were
`8 passed`.

## Do Next

1. Add a near-neighbour guard: compare one-digit roster alternatives using
   per-cell probabilities and require posterior `P(R) >= 0.85` before an
   automatic attachment.
2. Implement Path B as Suggested first: page-one bubbles plus high-confidence
   page-two cells. If page-one cells support another valid roll, block it.
3. Implement Path C as Suggested: page-one and page-two cells agree while
   bubble evidence is blank or contradictory.
4. Add a Suggested review UI with candidates, page images, cell crops, and
   differing-digit probabilities.
5. Replace text-dependent decision rules with structured `info`, `warn`, and
   `block` evidence codes.
6. Label the blind benchmark crops before enabling Paths B/C for automatic
   attachment.

Do not use scan order or nearest-roster matching to assign ownership. The
roster validates literal OCR only; it never rewrites a prediction.
