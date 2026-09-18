# Mailing Final Student PDFs

Use this workflow after the two-page PDF for each student has been checked. It does not depend on
OMR grouping or OCR. It matches filenames to the master roster, imports tentative marks, creates
reviewable email previews, and sends with safeguards against wrong attachments and duplicate mail.

## 1. Prepare The Inputs

Create one folder containing only final student PDFs:

```text
final_student_pdfs/
  2024432.pdf       BTech
  MT25007.pdf       MTech
  PHD25111.pdf      PhD
```

Filenames are case-insensitive, but the roll must match the roster after spaces are removed and
letters are uppercased. Each PDF must have exactly two pages by default.

The master roster must be a CSV with roll, name, email, and program columns. Common headings such
as `Roll No`, `Student Name`, `Email ID`, and `Program` are accepted.

The tentative-marks file may be CSV or XLSX. It needs one roll column, one marks column, and either
a maximum-marks column or the `--max-marks` command option. For example:

```csv
roll_no,marks_obtained,max_marks
2024432,17,20
MT25007,14.5,20
PHD25111,19,20
```

Create a UTF-8 text file named `email_message.txt`. Supported placeholders are
`{display_name}`, `{name}`, `{roll_no}`, `{exam_id}`, `{marks_obtained}`, and `{max_marks}`.

```text
Dear {display_name},

Please find attached your evaluated response sheet for {exam_id}.

Tentative marks: {marks_obtained} / {max_marks}

Please contact the course staff by the announced deadline if you find a discrepancy.

Regards,
CSE557 Course Staff
```

## 2. Install And Prepare

From PowerShell in the cloned repository:

```powershell
cd D:\SmartOMR
git pull
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Prepare a frozen release. Replace the example paths and sender address:

```powershell
.\.venv\Scripts\python.exe -m omr.workflows.email prepare-folder `
  --pdf-dir "D:\Mailing\final_student_pdfs" `
  --roster "D:\Mailing\students.csv" `
  --marks-file "D:\Mailing\tentative_marks.xlsx" `
  --body-template-file "D:\Mailing\email_message.txt" `
  --output-dir "D:\Mailing\release" `
  --exam-id "CSE557_QUIZ1_2026" `
  --sender "professor@iiitd.ac.in" `
  --sender-name "CSE557 Course Staff" `
  --expected-pages 2
```

If the marks file has no total column, add `--max-marks 20`. Preparation sends nothing. It stops
on an unknown/duplicate roll, invalid email, missing recipient mark, malformed PDF, wrong page
count, oversized attachment, or invalid message placeholder.

## 3. Review Before Sending

Open and verify:

```text
release/email_queue.csv       exact recipients, marks, attachments, and subjects
release/email_skipped.csv     roster students with no supplied PDF
release/previews/*.eml        complete email previews with attachments
release/attachments/*.pdf     frozen PDFs that will actually be sent
release/bodies/*.txt          rendered message for each student
release/email_release.json    release counts and integrity metadata
```

Check the queued count, several BTech/MTech/PhD rows, marks, recipient addresses, and attachments.
Do not edit files inside `release` after preparation; integrity checks intentionally reject edits.

Run a dry run. This opens no SMTP connection and sends nothing:

```powershell
.\.venv\Scripts\python.exe -m omr.workflows.email send `
  --queue-csv "D:\Mailing\release\email_queue.csv" `
  --smtp-host smtp.gmail.com --smtp-port 587 --smtp-security starttls `
  --username "professor@iiitd.ac.in" --sender "professor@iiitd.ac.in"
```

## 4. Send One Redirected Test

The first real send must go to a staff-controlled address. It uses one complete student email but
does not contact that student and does not mark the student as sent:

```powershell
.\.venv\Scripts\python.exe -m omr.workflows.email send `
  --queue-csv "D:\Mailing\release\email_queue.csv" `
  --smtp-host smtp.gmail.com --smtp-port 587 --smtp-security starttls `
  --username "professor@iiitd.ac.in" --sender "professor@iiitd.ac.in" `
  --sender-name "CSE557 Course Staff" `
  --test-recipient "course-staff@iiitd.ac.in" `
  --send
```

The password is requested in a hidden prompt. Never put it in the command, spreadsheet, repository,
or chat. Inspect the received test email, marks, wording, and both PDF pages.

## 5. Send The Confirmed Batch

Use the exact unsent recipient count reported by the tool. Example for 150 recipients:

```powershell
.\.venv\Scripts\python.exe -m omr.workflows.email send `
  --queue-csv "D:\Mailing\release\email_queue.csv" `
  --smtp-host smtp.gmail.com --smtp-port 587 --smtp-security starttls `
  --username "professor@iiitd.ac.in" --sender "professor@iiitd.ac.in" `
  --sender-name "CSE557 Course Staff" `
  --confirm-count 150 --delay-seconds 0.5 `
  --send
```

Every successful delivery is immediately recorded in `release/email_send_log.csv`. If the command
is interrupted, run it again with the new unsent count; already successful students are skipped.
Do not use `--resend` unless a deliberate duplicate delivery has been approved.

## SMTP Requirement

A browser login or normal Google password is usually insufficient. The professor account needs an
approved SMTP method: a Google app password, an institute SMTP relay, or another credential issued
by IT. Confirm this before the mailing window. The release can be prepared and reviewed without any
mail credential.
