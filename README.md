# OMR-Based Assessment System

BTP project, IIIT Delhi. Full spec and module boundaries are in [CLAUDE.md](CLAUDE.md) — read that first, it's the persistent design contract for this codebase.

## Status: Phase 1 in progress

Per the roadmap in CLAUDE.md Section 12:

- [x] **Phase 1** — OMR sheet generator + pure MCQ grading pipeline (this repo, no real scanning yet)
- [ ] Phase 2 — Real scan ingestion (canonicalization + identity resolution + verification email)
- [ ] Phase 3 — Written-answer grading (LLM-assisted)
- [ ] Phase 4 — Marks email, re-eval logging, admin panel

## Layout

```
omr/
  generator/
    config.py     ExamConfig / WrittenQuestionConfig — the professor-facing input (Section 4.1)
    layout.py      Manifest-driven layout engine — single source of truth for every
                    fiducial/bubble/box position, in mm (Section 4.2-4.4)
    pdf_gen.py     Renders the printable PDF from a SheetLayout (ReportLab)
    manifest.py    Builds/saves/loads the template manifest JSON from the same SheetLayout
    generate.py    generate_exam(config, output_dir) -> {pdf_path, manifest_path, manifest}
  grading/
    bubbles.py     Generic manifest-driven fill-ratio bubble reading (mm -> px, dark-pixel ratio)
    mcq.py         MCQ response reading + grading (Section 7) — distinguishes answered/blank/multiple
tests/
  test_generator.py     Layout/manifest correctness, page-bounds, pagination, PDF page count
  test_mcq_grading.py   Draws filled bubbles onto manifest-sized page images (incl. multi-page) and verifies grading
scripts/
  gui.py                 Desktop GUI — form in, PDF out, no terminal typing (see "Run it as software" below)
  interactive_generate.py CLI wizard — prompts for requirements one at a time, for live demos
  generate_exam.py      CLI: JSON config file -> PDF + manifest under data/exams/
configs/
  *.json                 Example exam configs — copy one and edit to create your own
Run OMR Generator.bat   Double-click launcher for the GUI (Windows)
```

The generator and the grader never hardcode a shared position: `pdf_gen.py` and `manifest.py` both
render from the same `SheetLayout` object, and `grading/mcq.py` reads every coordinate it needs
back out of the manifest dict (bubble radius, option pitch, per-question x/y). That's what lets
Phase 2's real scan parser reuse `grading/bubbles.py` and `grading/mcq.py` unchanged.

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

## Run it as software (no terminal)

Double-click **`Run OMR Generator.bat`** in the project root. It opens a window — fill in exam ID,
course code, exam type, number of MCQs, options per MCQ, and add as many written questions as you
need (each with its own marks and line count), pick an output folder, and click **Generate OMR
Sheet**. The PDF opens automatically when it's done. No Python, no command line, no JSON.

(First-time setup still needs the one-time `Setup` step above — the `.bat` just launches the
already-installed environment. If you ever move the folder, `.venv` still works as long as it moves
with it, since the launcher `cd`s to its own location first.)

This is the same `generate_exam()` pipeline underneath as the CLI scripts below — the GUI is just a
form wrapped around it, so anything generated through one is identical to the other.

## Generate an exam sheet (command line)

**Live, in front of someone calling out requirements** — no JSON editing, answer the prompts as
they're given, PDF opens automatically when done:

```bash
python scripts/interactive_generate.py
```

**From a saved config** — for repeatable/scripted generation:

```bash
python scripts/generate_exam.py configs/midsem_cs301.json
python scripts/generate_exam.py configs/quiz_short.json --output-dir data/exams
```

Both write `<exam_id>.pdf` and `<exam_id>.manifest.json` into `data/exams/` (or `--output-dir`).

### Config file format (Section 4.1)

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
| `marks_per_mcq` | any number | Marks for a correct MCQ — uniform across all MCQs (see limitation below) |
| `written_questions` | list, any length | See below |
| `roster_csv` | optional string | Reserved for Phase 2 (roster upload for identity resolution) — not used yet |

Each entry in `written_questions` is independent:

| Field | What it does |
|---|---|
| `q_no` | Printed on the answer box; must be greater than `num_mcq` (numbering continues after the MCQs) |
| `max_marks` | Printed next to the question number |
| `lines` | How many ruled lines the answer box gets — the box height scales automatically (`6mm` header + `lines × 6mm` + `4mm` padding), so a 2-line short-answer box and a 6-line derivation box come out very different sizes on the same sheet |

There's no fixed number of written questions — add as many objects to the list as you want. The
layout engine packs the MCQ block first (auto-picking however many columns are needed), then
stacks the written-answer boxes underneath in order, each sized to its own `lines` value.

### Multi-page exams

Everything is tried on a single A4 page first (the common case for a normal quiz/midsem). If the
MCQs and written questions don't fit together, the sheet automatically spills onto as many pages
as it needs — MCQs fill page(s) using the same column-packing logic, then the written section
always starts fresh on a new page and bin-packs its answer boxes (which can each be a different
height) across however many pages it needs. Sections never interleave mid-page: Section A (MCQs)
finishes, then Section B (Written) starts, exactly like a real multi-page exam paper.

Every page gets its own 4 fiducial markers (Section 4.3 requires this on every physical sheet, since
each page is deskewed independently at scan time) and repeats the header with a "Page X of N" label,
so pages can be manually reassembled if they ever get separated. Only page 1 carries the roll-number
identity block — see `configs/multi_page_20q.json` (10 MCQs + 10 written questions, 3 lines each) for
a worked example: it comes out to 3 pages (MCQs on page 1, written spread across pages 2–3).

The manifest reflects this: every fiducial/MCQ/written entry carries a `"page"` field, and there's a
top-level `"num_pages"`. Grading (`grading/mcq.py`) takes one canonical image *per page*
(`{page_no: image}`) rather than a single image, precisely so a multi-page sheet can never get graded
against the wrong page's image by accident.

There's still a genuine limit: a single written question whose box is taller than one whole page
(e.g. asking for 60 ruled lines) can't be placed anywhere, and the generator refuses to guess — see
`configs/example_impossible_question.json`:

```
Layout error: Written question Q1 needs 60 lines, which is too tall to fit on its own page.
Reduce its line count or split it into multiple questions.
```

That's the one case where you should still expect a hard stop (Section 2, principle 4 — flag rather
than guess); everything else now paginates instead of failing.

### Known Phase 1 limitations (worth flagging to your professor)

- **Uniform MCQ marks** — `marks_per_mcq` applies to every MCQ; per-question weighting isn't wired up.
- **`exam_type` is metadata only** — it's stored but doesn't currently change the layout (e.g. no
  "quiz = compact" vs "endsem = spacious" behavior). Easy to add if you want it.
- **No question-paper/rubric ingestion yet** — that's Module 5 (Phase 3); Phase 1 only lays out
  bubbles and answer boxes, not question text.

### Example configs

- `configs/midsem_cs301.json` — the Section 4.1 example from the spec (20 MCQs + 3 written, 1 page)
- `configs/quiz_short.json` — a bare 10-MCQ quiz, no written section
- `configs/endsem_heavy_written.json` — 8 MCQs + 4 written questions with varying line counts (2/2/3/2), 1 page
- `configs/mcq_options_6.json` — 15 MCQs with 6 options each (A–F) instead of the usual 4
- `configs/multi_page_20q.json` — 10 MCQs + 10 written questions -> auto-paginates to 3 pages
- `configs/example_impossible_question.json` — a single question too tall for any page, to show the remaining hard-stop case above

Copy any of these and edit the numbers to build your own — that's the whole workflow.

## Notes on Phase 1 scope

- No database yet. `Exam`/`Question`/etc. (CLAUDE.md Section 3) will be wired up starting Phase 2,
  once there's something that actually needs persisting (rosters, scanned sheets, review queue state).
- No real scanning/photo ingestion — MCQ grading is tested by drawing filled bubbles directly onto
  blank canonical-size page images at manifest coordinates, per the roadmap's own Phase 1 test
  strategy (CLAUDE.md Section 12). Perspective correction from a real scan/photo is Phase 2 (Module 2).
