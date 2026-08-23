# Prototype OMR Evaluator

This is the professor-demo CLI for the current SmartOMR sheet format.

The implementation now lives in the main package:

- `omr.reader` — scan/PDF loading, fiducial alignment, page-index bars, roll-number reading
- `omr.io` — roster, answer-key, and result CSV helpers
- `omr.workflows` — end-to-end scan evaluation workflow

Keep new production code in `omr/`. This folder remains for sample files and
the backward-compatible `python -m prototype_eval` command.

It takes:

- `answer_key.csv` from the professor
- `students.csv` roster
- scanned student OMR images/PDFs
- the generated `<exam_id>.manifest.json`

and writes:

- `results.csv` summary for automailer/SQL import
- `details/*.json` with roll-read, page alignment, and per-question fill ratios

## Folder Shape

```text
prototype_eval/
  data/                         # local runtime data, gitignored
    <exam_id from manifest>/
      scans/
      results/
  samples/                      # committed examples
    CS301_2026_MIDSEM/
      answer_key.csv
      students.csv
      scans/
  *.py                          # thin compatibility wrappers over omr/
```

The output folder is read from the manifest's `exam_id`, so the scan results
stay tied to the exact OMR layout that was generated.

## CSV Formats

`students.csv`

```csv
roll_no,name,email,program
2026001,Aryan,aryan@example.com,BTECH
MT24001,Riya,riya@example.com,MTECH
```

`answer_key.csv`

```csv
q_no,answer,marks
1,A,1
2,C,1
```

## Run

```powershell
cd "C:\IIIT Delhi\BTP\SmartOMR"
.\.venv\Scripts\python.exe -m prototype_eval `
  --manifest data\exams\CS301_MIDSEM_2026A.manifest.json `
  --students prototype_eval\samples\CS301_2026_MIDSEM\students.csv `
  --answer-key prototype_eval\samples\CS301_2026_MIDSEM\answer_key.csv `
  --scans prototype_eval\samples\CS301_2026_MIDSEM\scans
```

This writes to:

```text
prototype_eval/data/<exam_id from manifest>/results/results.csv
```

## Reliability Rules

The reader accepts mild tilt/rotation by detecting the four black corner
markers and warping the page back to A4.

It rejects unsafe scans when:

- fewer than four corner markers are visible
- the page is cropped/half-page
- the orientation marker cannot be recovered
- page-index bars are ambiguous
- the roll number or program selector is unclear

Rejected/unclear sheets go to `needs_review` or `error` instead of producing a
quietly wrong grade.
