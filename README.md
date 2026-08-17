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
non-zero; the GUI shows the same report in an error dialog and won't open the PDF.

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
  contracts/     The shared truth between the sheet generator and the sheet reader.
    geometry.py    Page size, mm <-> px conversion (Section 4.4)
    manifest.py    Manifest schema, version, load + validate
  generator/     Module 1 — everything that MAKES a sheet (Section 4)
    main.py        Entry point: wizard / --config / --gui
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
    gui.py         Desktop form over the same pipeline
    configs/       Worked example exam configs — copy one and edit to make your own
    tests/         test_flow.py (pagination behaviour), test_page_identity.py (per-page
                     identity + page bars), test_preflight.py, test_generator.py,
                     test_main.py, plus test_printed_sheet.py — which rasterizes the
                     real PDF and inspects the pixels
  grading/       Modules 4/5 — everything that READS a sheet (Sections 7-8)
    bubbles.py     Bubble measurement: fill_ratio (hard threshold) + ink_density (mean darkness)
    mcq.py         MCQ reading + grading — answered/blank/multiple, with confidence
    tests/         Grading correctness, plus test_mcq_confidence.py — imperfect real-world marks
  verify.py      Round-trips a generated sheet: fills it in, grades it, checks it came back
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
which page it is. Attributing a page by its position in the scan queue is a guess, and principle 4
says a guess doesn't get to produce a grade.

| On every page | Read by | What it's for |
|---|---|---|
| Handwritten **Name** + 7-cell **Roll No.** strip | a human | Reattaching a page that got separated; resolving a flagged bubble read (Section 6, step 5) |
| **Page-index bars** — one per page, this page's filled solid | the machine | Confirming a batch is a complete sheet in the right order |
| Bubbled roll-number grid (**page 1 only**) | the machine | Roster lookup (Module 3) |

The strip is 7 cells because both roll-number formats are exactly 7 characters: `2024503` for BTech,
`MT25001` for MTech (Section 4.2).

The bubble grid is **not** repeated on continuation pages. It would cost ~85mm of every page, and it
would ask a student to bubble the same seven digits two or three more times — each repeat being a
fresh chance to produce a page that *contradicts* page 1, which is a review-queue item rather than
an improvement.

The page-index bars are deliberately **bars**, not squares: a marker detector rejects candidates by
squareness, so a 4.0 × 1.8mm rectangle can never be mistaken for the 3.5mm orientation marker
sitting on the same row. The reader measures ink at each bar's coordinate with the same primitive it
uses for a bubble — no new decoder — and "exactly one bar is dark" is the checksum that says the
read is trustworthy.

One more, for the humans:

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

### Multi-page exams — how the flow works

**One continuous flow, always.** A cursor walks down page 1, then page 2, and so on. Section A
(MCQs) is placed first; Section B (written answers) continues from wherever Section A ended — on the
same page if there's room, on the next page only if there isn't. A page break happens only when the
next question genuinely doesn't fit, which makes a half-empty page structurally impossible rather
than something to remember not to produce.

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

Sections still never interleave — all of Section A precedes all of Section B, exactly like a real
exam paper, and a page break never splits a single answer box.

**MCQ column count** (2, 3, or 4) is chosen once for the whole sheet, by running the real flow for
each candidate and keeping the one that needs the fewest pages. Ties go to the *fewest* columns:
more horizontal room per question means more paper between neighbouring bubbles, so a stray pen mark
is less likely to land in another question's read region. 40 MCQs fit on one page in three columns
but not in two, so three wins there.

Every page gets its own four fiducial markers plus an orientation marker (Section 4.3 requires this
on every physical sheet, since each page is deskewed independently at scan time), its own
page-index bars, its own name and roll-number strip, and a "Page X of N" label.

The manifest reflects all of it: every fiducial / MCQ / written / page-mark / write-in entry carries
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
