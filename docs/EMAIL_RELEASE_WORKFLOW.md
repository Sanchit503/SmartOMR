# Email Release Workflow

This workflow sends each verified student either their response sheet for verification or their
evaluated response sheet with marks. The local UI defaults to the safer sheet-verification mode.

It is intentionally two-step:

```text
prepare -> review queue/previews -> send
```

Do not send directly from a raw 300-page PDF. First run SmartOMR, review flagged cases,
and verify/approve student sheets.

## Release Modes

### Sheet verification (recommended for returning scans)

Each message contains only the verified multi-page student PDF. It does not contain computed marks,
correct answers, or a marks-summary attachment. This mode is appropriate after pages have been
grouped and manually verified, even when grading is not final.

In the UI, leave **Release Type** set to **Sheet Verification - no marks**. From the CLI, add
`--sheet-only`:

```powershell
.\.venv\Scripts\python.exe -m omr.workflows.email prepare `
  --parsed-dir "data\ui_runs\<run_id>\parsed\<exam_id>" `
  --sender "professor@iiitd.ac.in" `
  --sender-name "Course Staff" `
  --sheet-only
```

### Evaluated sheet and marks

For each eligible student:

- evaluated OMR PDF from `verified/students/<roll_no>/sheet.pdf`
- text marks summary attachment
- email body containing roll number and marks

Use this mode only after objective results, written marks, and any manually assigned pages have
been rechecked. It is the CLI default when `--sheet-only` is omitted.

Students are queued only when:

```text
status = verified
eligible_for_email = true
student email is present
verified sheet PDF exists
```

Needs-review or missing-page students are skipped by default. When a roster was uploaded, students
for whom no sheet was detected are also included in `email_skipped.csv` with `missing_sheet` status.

## Prepare Queue

```powershell
cd "C:\IIIT Delhi\BTP\SmartOMR"

.\.venv\Scripts\python.exe -m omr.workflows.email prepare `
  --parsed-dir "data\ui_runs\<run_id>\parsed\<exam_id>" `
  --sender "professor@iiitd.ac.in" `
  --sender-name "Course Staff"
```

This writes:

```text
email_release/email_queue.csv
email_release/email_skipped.csv
email_release/previews/<roll_no>.eml
email_release/summaries/<roll_no>_marks_summary.txt  # evaluated mode only
email_release/bodies/<roll_no>_email_body.txt
```

Review before sending:

```text
email_queue.csv       who will receive email
email_skipped.csv     who will not receive email and why
previews/*.eml        exact message previews
```

## Dry Run

Dry run writes a log but sends nothing:

```powershell
.\.venv\Scripts\python.exe -m omr.workflows.email send `
  --queue-csv "data\ui_runs\<run_id>\parsed\<exam_id>\email_release\email_queue.csv" `
  --smtp-host smtp.gmail.com `
  --smtp-port 587 `
  --username "professor@gmail.com" `
  --sender "professor@gmail.com" `
  --sender-name "Course Staff"
```

Output:

```text
email_release/email_send_log.csv
```

Rows will have:

```text
status = DRY_RUN
```

## Send For Real

Set the SMTP password in an environment variable:

```powershell
$env:SMARTOMR_SMTP_PASSWORD = "app-password-or-smtp-password"
```

Then send one test email first:

```powershell
.\.venv\Scripts\python.exe -m omr.workflows.email send `
  --queue-csv "data\ui_runs\<run_id>\parsed\<exam_id>\email_release\email_queue.csv" `
  --smtp-host smtp.gmail.com `
  --smtp-port 587 `
  --username "professor@gmail.com" `
  --sender "professor@gmail.com" `
  --sender-name "Course Staff" `
  --limit 1 `
  --send
```

If the test email is correct, send the rest:

```powershell
.\.venv\Scripts\python.exe -m omr.workflows.email send `
  --queue-csv "data\ui_runs\<run_id>\parsed\<exam_id>\email_release\email_queue.csv" `
  --smtp-host smtp.gmail.com `
  --smtp-port 587 `
  --username "professor@gmail.com" `
  --sender "professor@gmail.com" `
  --sender-name "Course Staff" `
  --send
```

Real sends pause for `0.25` seconds between messages by default. A successful `SENT` record prevents
that roll/email pair from being sent again if the command or UI button is accidentally run twice.
Use `--resend` only for a deliberate repeat after checking `email_send_log.csv`.

Preparing a queue never sends mail. Before a real batch send, inspect `email_queue.csv`,
`email_skipped.csv`, and several `.eml` previews; run a dry run; then perform one real test send to
an approved recipient or roll number. Do not use `--resend` for the remaining batch after that test:
the successful test recipient will be skipped automatically while unsent rows continue.

## Gmail Note

Normal Gmail password login usually does not work for SMTP. Use one of:

```text
Gmail App Password
Google Workspace SMTP relay
another SMTP account provided by the institute
```

If the professor only logs into Gmail in a browser for 10-15 minutes, that alone
does not give Python permission to send 300 emails. We need SMTP credentials or
an approved mail API/OAuth setup.

