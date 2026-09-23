# Repository Hygiene

Keep maintained source, tests, and documentation separate from local exam data
and one-off experiments. A smaller root directory does not prove that OCR,
student grouping, or grading is correct.

## Maintained Structure

| Location | Responsibility |
| --- | --- |
| `omr/contracts/` | Shared manifest and geometry contracts |
| `omr/generator/` | Printable sheets, manifests, layout and print checks |
| `omr/reader/` | Scan alignment, identity and answer extraction |
| `omr/grading/` | Answer interpretation and grading contracts |
| `omr/io/` | Structured input and output helpers |
| `omr/workflows/` | Parsing, grouping, review, written grading and email workflows |
| `omr/ui/` | Local UI, scan inspection and packaged static assets |
| `omr/datasets/` | Packaged dataset preparation and training tools |
| `omr/**/tests/` | Tests beside the modules they exercise |
| `prototype_eval/` | Tested compatibility entrypoints and examples |
| `docs/` | Workflow documentation, design notes and dated handoffs |

Use packaged commands, not historical root scripts. Common entrypoints are:

```powershell
python -m omr.generator.main
python -m omr.ui.app
python -m omr.health --check-handwriting-ocr
```

`pyproject.toml` lists the installed CLI entrypoints. The README and workflow
documents describe their arguments. Do not remove a module solely because no
other Python file imports it: CLI entrypoints, compatibility imports, tests and
packaged resources also matter.

## Local Data

`data/` and `scans/` contain local artifacts, not distributable source. Existing
`eval_output/` results are also retained at their original paths and gitignored.
Do not rename or delete exam outputs casually: reports, review decisions and
email release records may depend on those paths.

Root-level model weights (`*.pth`), `smartomr_crops/`, `benchmark_crops.csv` and
`training_labels.csv` are ignored if recreated. Prefer an explicitly named
directory under `data/` for future local experiments. Review any fixtures before
adding them to Git; use synthetic or properly approved, de-identified samples.

Ignore rules are not access control and do not remove already tracked files or
Git history. Do not share local data, backups, models trained on student data,
mail credentials or release logs without the appropriate authorization.

## Cleanup On 2026-09-22

This was a root-directory cleanup, not a runtime refactor. No application source,
tests, dependencies, model configuration or processing behavior was changed.
Existing uncommitted application and UI work was preserved.

After checking references, package entrypoints and running processes, 29
untracked or ignored root entries were moved, not permanently deleted:

- Historical `patch*.py` scripts.
- Root extraction, CNN training, TrOCR experiment and benchmark scripts.
- Their two root model files, two dataset CSVs and `smartomr_crops/` directory.
- The local `.git_diff.txt` scratch file.
- Regenerable `build/` and `.pytest_cache/` directories.

The archive is local to the original workspace:

```text
data/maintenance/root_cleanup_20260922/
  checkpoint/tree/       Copies at their original relative paths
  checkpoint/manifest.json
  checkpoint/verified.json
  archive/legacy_root/   Historical scripts, models and datasets
  archive/generated/     Previous build and pytest cache
  plan.json              Original and destination paths
  move_log.json          Completed moves
  archive_complete.json
  verification.json     Post-cleanup checksum results
```

The checkpoint contains tracked and nonignored untracked working files, plus all
files within the selected archive directories and `eval_output/`. Its 10,743
files were SHA256-checked against the originals. It is **not** a complete backup
of existing `data/`, `scans/`, the virtual environment, Git history or secrets.
Those locations were outside the move set and left in place. Git staging was
not changed, and this cleanup did not commit or push anything.

Archived experiments retain their original contents, including old paths. They
are historical references, not supported commands runnable from the archive.
In particular, do not rerun old patch scripts against current source.

## Recovery And Future Changes

Consult `plan.json` to locate an archived item. Before restoring an individual
file, compare it with any current version, check its recorded SHA256 and confirm
the destination is within the intended workspace. Do not overwrite current work
or copy the entire checkpoint over the repository. Port useful experimental
logic into the appropriate maintained module with focused tests instead.

The archive and checkpoint live under ignored `data/`; a fresh clone will not
contain them. They are local recovery aids, not a backup on another device.
Keep them until their contents have been reviewed and any required long-term
backup has been arranged. Do not remove the editable-install metadata
(`smartomr.egg-info/`) or virtual environment as incidental cleanup.

Further removal of maintained code needs a separate dependency and behavior
audit. Preserve explicit verification before student-sheet release; repository
cleanup does not repair or validate the historical real-scan grouping failure.
