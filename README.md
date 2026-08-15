# SmartOMR

OMR-based assessment system. BTP project, IIIT Delhi. Full spec and module boundaries are in
[CLAUDE.md](CLAUDE.md) — read that first, it's the persistent design contract for this codebase.

## Status: Phase 1 in progress

Per the roadmap in CLAUDE.md Section 12:

- [x] **Phase 1** — OMR sheet generator + pure MCQ grading pipeline (this repo, no real scanning yet)
- [ ] Phase 2 — Real scan ingestion (canonicalization + identity resolution + verification email)
- [ ] Phase 3 — Written-answer grading (LLM-assisted)
- [ ] Phase 4 — Marks email, re-eval logging, admin panel

## Generate a sheet

Double-click **`SmartOMR.bat`**, or:

```bash
python -m omr.generator.main
```

It asks for the exam requirements one at a time — course code, exam name, how many MCQs, how many
options each, how many written questions and how many lines each — then writes the printable PDF and
its template manifest into `data/exams/` and opens the PDF.

Two other ways in, same generator underneath:

```bash
python -m omr.generator.main --config omr/generator/configs/midsem_cs301.json   # repeatable
python -m omr.generator.main --gui                                              # desktop form
```

`--output-dir` changes where files land; `--no-open` skips opening the viewer. A sheet produced
through any of the three routes is byte-identical to the same sheet produced through the others —
there's a test that pins that down.

## Layout

```
omr/
  contracts/     The shared truth between the sheet generator and the sheet reader.
    geometry.py    Page size, mm <-> px conversion (Section 4.4)
    manifest.py    Manifest schema, version, load + validate
  generator/     Module 1 — everything that MAKES a sheet (Section 4)
    main.py        Entry point: wizard / --config / --gui
    config.py      ExamConfig / WrittenQuestionConfig — the professor-facing input (Section 4.1)
    layout.py      Manifest-driven layout engine — single source of truth for every
                     fiducial/bubble/box position, in mm (Sections 4.2-4.4)
    pdf_gen.py     Renders the printable PDF from a SheetLayout (ReportLab)
    manifest.py    Builds/saves the template manifest from the same SheetLayout
    generate.py    generate_exam(config, output_dir) -> {pdf_path, manifest_path, manifest}
    gui.py         Desktop form over the same pipeline
    configs/       Worked example exam configs — copy one and edit to make your own
    tests/         Layout/manifest correctness, pagination, entry-point behaviour, plus
                     test_printed_sheet.py — rasterizes the real PDF and inspects the pixels
  grading/       Modules 4/5 — everything that READS a sheet (Sections 7-8)
    bubbles.py     Bubble measurement: fill_ratio (hard threshold) + ink_density (mean darkness)
    mcq.py         MCQ reading + grading — answered/blank/multiple, with confidence
    tests/         Grading correctness, plus test_mcq_confidence.py — imperfect real-world marks
data/exams/      Generated PDFs + manifests (gitignored)
SmartOMR.bat     Double-click launcher (wizard)
SmartOMR (GUI).bat
```

### Why `contracts/` exists

CLAUDE.md Section 2, principle 1 says the generator and the parser share **one** manifest. That only
holds if there's somewhere for the shared half to live. `omr/contracts/` is that place — page
geometry, the mm→px conversion, and the manifest schema. `omr.generator` and `omr.grading` both
import from it; **nothing in it imports from them**, which is what keeps the Phase 2 scan reader from
needing ReportLab installed to parse a sheet. There's a test (`test_contracts_never_imports_from_generator_or_grading`)
that fails if that direction is ever reversed.

Everything else — bubble radius, option pitch, block positions — is a layout *decision* the generator
makes and publishes **through the manifest**, so the reader learns it at parse time instead of sharing
a constant. `pdf_gen.py` and `manifest.py` both render from the same `SheetLayout` object, and
`grading/mcq.py` reads every coordinate it needs back out of the manifest dict. That's what lets
Phase 2's real scan parser reuse `grading/` unchanged.

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

**Every fiducial owns a quiet zone.** A contour detector finds a marker by isolating a dark square,
so 3.5mm of blank paper is reserved on every side and all content bands are derived from those
keep-outs. Previously the title sat 0.45mm below the top-left marker — close enough for a detector
to merge the two into one blob and compute a wrong corner.

**A fifth marker breaks the rotational symmetry.** Four identical corner squares look the same at
0°, 90°, 180° and 270°, so a sheet fed in upside down reads as a valid upright sheet with every
coordinate inverted. A smaller square near the top-left resolves it: whichever corner marker it
sits nearest is the true top-left, at any rotation and any scale.

Two more, for the humans:

- **Name and roll-number write-in boxes on every page.** Page 1 carries the full bubble grid; the
  digit cells sit directly above their own bubble columns. Continuation pages carry the write-in
  row only — repeating the whole grid would cost ~56mm a page and make students bubble the same
  number three times, but a continuation page with *no* identity can't be attributed to anyone if
  it gets separated, and it's a human who resolves the review queue anyway (Section 6, step 5).
- **Print at 100% scale**, not "fit to page". The fiducials let the reader recover a uniform scale,
  but there's no reason to make it work harder.

## Grading: what a reading tells you

`read_mcq_responses()` returns more than a letter, because CLAUDE.md principle 4 says a
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
pip install -r requirements.txt
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
  ]
}
```

| Field | Customizable range | What it changes |
|---|---|---|
| `exam_id` | any string | Output filename, printed in the header |
| `course_code` | any string | Printed in the header |
| `exam_name` | any string | Printed as the sheet title |
| `exam_type` | `quiz` \| `midsem` \| `endsem` | Metadata only right now — doesn't change layout |
| `num_mcq` | any non-negative integer | Number of MCQ rows; layout auto-picks 2/3/4 columns to fit them above the written section |
| `mcq_options` | 2–6 | Number of bubbles per MCQ (A–B up to A–F); widens each MCQ row |
| `marks_per_mcq` | any number | Marks for a correct MCQ — uniform across all MCQs (see limitations below) |
| `written_questions` | list, any length | See below |
| `roster_csv` | optional string | Reserved for Phase 2 (roster upload for identity resolution) — not used yet |

Each entry in `written_questions` is independent:

| Field | What it does |
|---|---|
| `q_no` | Printed on the answer box; must be greater than `num_mcq` (numbering continues after the MCQs) |
| `max_marks` | Printed next to the question number |
| `lines` | How many ruled lines the answer box gets — the box height scales automatically, so a 2-line short-answer box and a 6-line derivation box come out very different sizes on the same sheet |

There's no fixed number of written questions. The layout engine packs the MCQ block first
(auto-picking however many columns are needed), then stacks the written-answer boxes underneath in
order, each sized to its own `lines` value.

### Multi-page exams

Everything is tried on a single A4 page first (the common case for a normal quiz/midsem). If the
MCQs and written questions don't fit together, the sheet automatically spills onto as many pages as
it needs — MCQs fill page(s) using the same column-packing logic, then the written section always
starts fresh on a new page and bin-packs its answer boxes across however many pages it needs.
Sections never interleave mid-page: Section A (MCQs) finishes, then Section B (Written) starts,
exactly like a real multi-page exam paper.

Every page gets its own 4 fiducial markers (Section 4.3 requires this on every physical sheet, since
each page is deskewed independently at scan time) and repeats the header with a "Page X of N" label.
Only page 1 carries the roll-number identity block — see `omr/generator/configs/multi_page_20q.json`
(10 MCQs + 10 written questions, 3 lines each) for a worked example: it comes out to 3 pages.

The manifest reflects this: every fiducial/MCQ/written entry carries a `"page"` field, and there's a
top-level `"num_pages"`. Grading (`grading/mcq.py`) takes one canonical image *per page*
(`{page_no: image}`) rather than a single image, precisely so a multi-page sheet can never get graded
against the wrong page's image by accident.

A single written question whose box is taller than one whole page (e.g. 60 ruled lines) can't be
placed anywhere, and the generator refuses to guess — see
`omr/generator/configs/example_impossible_question.json`:

```
Layout error: Written question Q1 needs 60 lines, which is too tall to fit on its own page.
Reduce its line count or split it into multiple questions.
```

That's the one case where you should expect a hard stop (Section 2, principle 4 — flag rather than
guess); everything else paginates instead of failing.

### Example configs

All under `omr/generator/configs/`:

- `midsem_cs301.json` — the Section 4.1 example from the spec (20 MCQs + 3 written, 1 page)
- `quiz_short.json` — a bare 10-MCQ quiz, no written section
- `endsem_heavy_written.json` — 8 MCQs + 4 written questions with varying line counts, 1 page
- `mcq_options_6.json` — 15 MCQs with 6 options each (A–F) instead of the usual 4
- `multi_page_20q.json` — 10 MCQs + 10 written questions -> auto-paginates to 3 pages
- `example_impossible_question.json` — a single question too tall for any page, to show the hard-stop case

## Known Phase 1 limitations

Worth flagging to your professor:

- **Uniform MCQ marks** — `marks_per_mcq` applies to every MCQ; per-question weighting isn't wired up.
- **`exam_type` is metadata only** — stored, but doesn't currently change the layout.
- **No answer key yet** — `grade_mcq_responses()` takes one, but `ExamConfig` has nowhere to put it.
- **No question-paper/rubric ingestion** — that's Module 5 (Phase 3); Phase 1 lays out bubbles and
  answer boxes, not question text.
- **No database** — `Exam`/`Question`/etc. (CLAUDE.md Section 3) get wired up starting Phase 2, once
  there's something that actually needs persisting.
- **No real scanning** — grading is tested against the *rendered PDF* rasterized back to pixels,
  with simulated pen marks drawn on it. That covers print fidelity and imperfect marks, but not
  perspective, lighting, or paper texture. Perspective correction from a real scan/photo is Phase 2
  (Module 2), and the fill/ink thresholds should be re-calibrated against real scans then.
- **No roll-number reading yet** — the bubble grid is printed and its coordinates are in the
  manifest, but `grading/` only reads MCQs so far. Identity resolution is Module 3 (Phase 2).
