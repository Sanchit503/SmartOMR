# Copy-Paste Prompt For The Next AI

**Housekeeping update (2026-09-22):** Also read
`docs/REPOSITORY_HYGIENE.md`. Old root experiments are now in a local ignored
archive; they are not supported commands. The older handoff's root inventory is
historical, and existing application work and exam outputs were preserved.

You are taking over SmartOMR, a real college assessment project. Read and
understand the existing system before proposing or making changes. We have
already experienced incorrect student-page grouping during a real 300+ page
exam run. The users had to group and grade sheets manually. Do not promise
accuracy, hide ambiguity, or treat passing synthetic tests as production proof.

## Start By Reading The Entire Codebase

The original workspace is `D:\SmartOMR`, using Windows PowerShell. The repository
is `https://github.com/Sanchit503/SmartOMR.git`. Do not assume GitHub contains all
the current work: there are important uncommitted and untracked changes.

If you have workspace access, begin with these read-only commands:

```powershell
Get-Location
git status --short
git log -5 --oneline
git remote -v
Get-Content -Raw -Encoding UTF8 docs/AI_HANDOFF.md
Get-Content -Raw -Encoding UTF8 README.md
Get-Content -Raw -Encoding UTF8 PROJECT_SPEC.md
Get-Content -Raw -Encoding UTF8 pyproject.toml
Get-Content -Raw -Encoding UTF8 .gitignore
git ls-files
rg --files omr prototype_eval docs
git diff --stat
git diff --name-only
```

Then read every application source file, configuration example, test, and
documentation file. Follow the dependency/read order in `docs/AI_HANDOFF.md`.
Read `git diff` in manageable, file-specific chunks. Inspect untracked source
files too, but do not run experimental training, benchmark, or patch scripts
merely because they exist. Do not upload student scans or dump student records
into your answer. Inspect real data locally only when needed for an authorized
task. Do not claim to have read files you could not access.

If you are a chat-only AI, first read the supplied `AI_HANDOFF.md`. It is a
handoff, not a substitute for source access. Ask for the relevant current source
files or a private, reviewed source snapshot before making code-specific claims.
If working from a fresh clone, identify what dirty source and untracked fixtures
are missing rather than assuming the handoff's local state exists in that clone.

## Preserve The Current Work

- Do not reset, clean, overwrite, stash, pull over, or discard local changes.
- Do not run `git add .`: untracked files include datasets, crops, model weights,
  and experimental scripts which may contain private student material.
- Do not regenerate manifests for already printed exams using newer geometry.
- Do not commit or push until I ask. Show the intended commit message first.
- Do not send any email, read credentials, download models, or upload student
  material to a cloud OCR service without a current, explicit need and approval.
- Do not rewrite the architecture during onboarding. Work in small, verified
  increments and preserve existing working behavior.

## The Immediate Context

Our professor has asked: "Tarandeep and Sanchit: I have 15 questions in the
midterm. Prepare an OMR sheet for that."

We have not yet received the breakdown of MCQ/numerical/written questions,
numerical digit counts, written answer space, marks, exact numbering, or final
exam metadata. Do not assume all 15 questions are numerical. Wait for the
professor's format before generating the final sheet.

The latest local generator now uses:

- Vertical numerical grids: 0 through 9 downward in each digit column.
- Four two-digit numerical questions across, with breathing room.
- 3.5 mm nominal bubbles for new sheets.
- No numerical handwriting boxes and no Ones/Tens/Hundreds headings.
- 9 mm numerical digit-column pitch; place values remain left-to-right.
- New manifest v6, with existing v4/v5 reading compatibility retained.

The latest preview is
`data/layout_previews/vertical_no_labels_20260919/NUMERICAL_8_TWO_DIGIT.pdf`.
The older `vertical_35mm_20260919` preview still has the previous labels/spacing.
Do not use the older file merely because it is open in the IDE.

The long-term objective is reliable scan alignment, roll identification,
student-page grouping, objective grading, written-answer extraction and human
review, then controlled per-student email release. Several pieces exist, but
unattended, accurate end-to-end processing is not established. The handoff
describes the exact implementation, known risks, experiments, and test limits.

## Your First Response After Reading

Report, with source references where appropriate:

1. Which workspace/commit you inspected, and which local changes are present.
2. The actual generation-to-email workflow and module boundaries.
3. What is implemented versus experimental versus only proposed.
4. The latest numerical layout and pending professor requirements.
5. The critical OCR/grouping, cache, review, and privacy limitations.
6. Tests actually run and what they do not prove.
7. Any missing source/data or uncertainties that prevent a reliable conclusion.

Then wait for my next instruction. Do not silently pick a large new feature or
start a rewrite. If I ask you to implement something later, inspect the relevant
code again, make one scoped change, add meaningful regression tests, verify the
rendered output when layout changes, and clearly report remaining limitations.
