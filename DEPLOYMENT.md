# SmartOMR Deployment Guide

This project is now packaged as an installable Python command-line service.
The deployable unit is the `smartomr` package plus three console commands:

- `smartomr-generate` - generate printable OMR PDFs and manifests
- `smartomr-parse` - parse scanned sheets and write results/debug artifacts
- `smartomr-health` - verify runtime dependencies and writable storage

## Runtime Model

SmartOMR is currently a stateless batch processor. It reads inputs from disk and writes JSON/images
back to disk. For deployment, mount one persistent data directory containing:

```text
data/
  exams/         generated PDFs and manifest JSON files
  my_scans/      uploaded scan images or PDFs
  answer_keys/   answer-key CSV files
  parsed/        parser output
```

The scoring path uses grayscale canonical pages for stable computer vision. The debug path also
saves a color-preserved aligned page for human inspection:

```text
sheets/<scan_id>/pages/page_1.png                 machine-readable grayscale page
sheets/<scan_id>/debug/page_1_aligned_color.png   human-readable aligned color page
sheets/<scan_id>/debug/page_1_alignment_overlay.png
sheets/<scan_id>/debug/page_1_sampling_overlay.png
sheets/<scan_id>/debug/page_1_alignment.json
```

## Local Production Install

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\smartomr-health.exe --data-dir data
```

Generate a sheet without opening a desktop PDF viewer:

```powershell
.\.venv\Scripts\smartomr-generate.exe --config omr\generator\configs\multi_page_20q.json --output-dir data\exams --no-open
```

Parse a scan folder:

```powershell
.\.venv\Scripts\smartomr-parse.exe --manifest data\exams\CSE222_ENDSEM_2026.manifest.json --scans data\my_scans --answer-key data\answer_keys\CSE222_ENDSEM_2026_answer_key.csv --output-root data\parsed
```

## Docker

Build the image:

```powershell
docker build -t smartomr:latest .
```

Run the health check:

```powershell
docker run --rm -v "${PWD}\data:/app/data" smartomr:latest smartomr-health --data-dir /app/data
```

Parse a scan folder:

```powershell
docker run --rm -v "${PWD}\data:/app/data" smartomr:latest smartomr-parse --manifest /app/data/exams/CSE222_ENDSEM_2026.manifest.json --scans /app/data/my_scans --answer-key /app/data/answer_keys/CSE222_ENDSEM_2026_answer_key.csv --output-root /app/data/parsed
```

With Docker Compose:

```powershell
docker compose up --build
```

## Exit Codes

- `smartomr-health` returns `0` only when dependencies import and the data directory is writable.
- `smartomr-generate` returns non-zero if config validation, layout, writing, or preflight fails.
- `smartomr-parse` returns non-zero if any uploaded scan is an `error`.
- Sheets with `needs_review` are still written to disk. They are not fatal because review is an
  expected production state.

## Deployment Notes

- Keep generated manifests with the exact exam PDF used for printing.
- Do not use compressed chat-app photos for final production evaluation.
- Store uploads and parser outputs outside the container image by mounting `data/`.
- Use `debug/page_1_aligned_color.png` for human inspection and `pages/page_1.png` for machine logic.
- Treat `needs_review` as a queue item, not a crash.
