# Email Release Workflow

This workflow sends each verified student their evaluated response sheet and marks.

It is intentionally two-step:

```text
prepare -> review queue/previews -> send
```

Do not send directly from a raw 300-page PDF. First run SmartOMR, review flagged cases,
and verify/approve student sheets.

## What Gets Sent

For each eligible student:

- evaluated OMR PDF from `verified/students/<roll_no>/sheet.pdf`
- text marks summary attachment
- email body containing roll number and marks

Students are queued only when:

```text
status = verified
eligible_for_email = true
student email is present
verified sheet PDF exists
```

Needs-review or missing-page students are skipped by default.

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
email_release/summaries/<roll_no>_marks_summary.txt
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

