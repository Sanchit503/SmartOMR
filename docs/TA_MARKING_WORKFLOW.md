# Question-wise TA marking

Start the main professor UI from the repository root:

```powershell
.venv\Scripts\python.exe -m omr.ui.app --port 8765
```

Open `http://127.0.0.1:8765`, open an exam, and select **TA Marking** (or
**TA Marking & Email** on Sheet Matching Review).

## Student Range Assignments

Use **Assign Question Range**:

1. Choose a written question, such as Q11.
2. Enter **From Student** and **To Student**, inclusive, such as 1 and 78.
3. Enter one **TA Email**, then choose **Add Assignment**.
4. Inspect the saved range and choose **Generate Packet**.
5. Add 79 through the next endpoint for the next TA. The previous range is retained.

Student numbers are positions in the displayed roll-sorted matched-student list,
not scanner source pages or numerical roll suffixes. The preview shows the actual
rolls, question-page availability and approval status. The assignment saves those
exact rolls and their displayed positions; later students cannot silently change
an existing range. A stale student-list submission is rejected.

Overlapping ranges are rejected unless **Additional marker** is explicitly selected
for a different TA. Duplicate assignment of a student/question to the same TA is
always rejected. Unrelated questions may use different assignments freely.

Generate one saved range or **Generate Pending Packets**. Already generated answers
are not re-exported when another range is added. Held-back answers stay pending and
can be generated after approval using **Generate Remaining**. A pending assignment
can be removed; generated exports and their delivery logs are retained.

Each range produces its own recipient packet, with a combined answers PDF,
individual roll-named PDFs, and a marking CSV carrying the student positions and
source indices. Multiple ranges for the same question are supported. Student
ownership rules are unchanged: pending identities, missing pages and disputed
source claims cannot be mailed as approved answers.

Legacy split/double exports remain readable and their API remains supported.
The new UI does not show split modes or a students-per-packet setting.

The workspace uses the same saved student mappings as the exam. Exports run in
the background with progress and remain available after restarting the UI. It
does not rerun roll recognition or change page ownership. The standalone
`omr.ui.ta_workspace` utility is optional, not required.

## Export

Packets are saved under `<parsed_dir>/ta_packets/<export_id>/`:

- `Q11/students/<roll>.pdf`: original page header, full-width question strip,
  question heading and ownership caption. All crop bounds derive from the manifest.
- `Q11/Q11_all_students.pdf`: combined question PDF.
- `Q11/<packet_id>.zip`: assigned student PDFs, combined `answers.pdf`,
  and `marking.csv` for one TA and one batch.
- `skipped.csv`: missing pages, unresolved ownership, disputed source claims,
  or missing image files, recorded for each assigned question.
- `index.json`: assignments, source indices, crop paths, ownership/manifest
  fingerprints, mail status, and imported grading records.

TA crop layout version 3 separates the identity header from the question area.
Header height derives from the manifest's roll-cell/bubble extents and the first
question boundary, with a bubble-radius safety margin. There is no fixed 60 mm
crop. Legacy question headings reserve one ruled-line pitch above the answer
box when explicit `heading_top_mm` is unavailable. Answer strips remain full
width and retain neighbouring whitespace without including the next question's
heading. Source scans and saved student ownership are not edited.
The lower crop edge includes three-quarters of the available gap below the
answer box rather than half, reserving the remaining quarter before the next
region's heading. Extra space scales with the manifest instead of a fixed mm
padding. The last answer remains bounded by the page's printer-safe margin.

Exports record `crop_layout_version`. A saved range from an older layout shows
**Crop layout updated / Regenerate Packet**. Regeneration creates a new export;
old PDFs, send logs and returned marks remain intact. Previously mailed attachments
cannot be changed by regenerating. No automatic email is sent on regeneration.

Resolved statuses are `verified`, `approved`, and `auto_matched`. Manual page
replacements override original parser pages. Source pages claimed by multiple
students are excluded. Each student appears once per question in split mode;
each TA receives the same student set in double mode. Question crops are
generated once per student page and reused across recipients and batches.

## Email

The Email Packets section remains visible before export, with sending disabled
until packets exist. Students awaiting ownership approval remain held back.
Open a packet to inspect its individual student PDFs and strip images. Select
one packet or All Pending Packets in Email Packets. Enter the sender email, SMTP host, port, SSL/STARTTLS setting, username,
and app password. Credentials are used for that request and are not persisted.
For Gmail SMTP, use `smtp.gmail.com`, port 587 with STARTTLS (or 465 with
SSL/TLS), and a 16-character Google app password generated for the SMTP username.
Normal Google login passwords are not supported by this flow. Spaced Google app
passwords are normalised; other providers' passwords are left unchanged.
The browser and server validate the Gmail app-password format before attempting
a connection. This checks format, not whether Google issued or accepts the password.
Authentication, TLS and disconnection errors return to the workspace with
sanitised guidance. Non-secret fields are retained only in that response; the
password is cleared and never echoed or saved. College administrators may disable
app passwords; that requires an institution-approved authentication alternative.
Delivery defaults to Test Recipient and requires an address. Explicitly select
Assigned TAs and confirm delivery to send real messages. Test delivery does not
mark the real TA delivery as sent. Sent packets are skipped on later bulk sends.

Subject and Message are editable in Email Packets. Save Template stores only this
wording for the exam run, without sending anything or saving SMTP credentials.
An unsaved draft is used when Send Packets is clicked; saving is optional.
Reset Template restores the original wording in the editor, and Save Template
can persist that reset. Delivered emails and previous send logs are unchanged.

Insert field supports `{exam_id}`, `{ta_name}`, `{ta_email}`, `{question_number}`,
`{student_range}`, `{answer_count}`, `{packet_id}` and `{sender_email}`. Plain text
without fields is supported. Use `{{` and `}}` for literal braces. Only these
fields are allowed; attribute access, format conversions and header newlines
are rejected. Subjects are limited to 512 characters, messages to 65,536.

Preview Email uses the same server renderer as delivery, showing the recipient,
subject, message and attachment filename. All Pending Packets offers a per-packet
preview; each message receives its own TA/question/range values. Preview requests
exclude SMTP credentials and never send mail. Bulk sends validate every rendered
message before starting delivery. Successful sends retain the exact subject and
message in the packet's mail log. A stale ownership snapshot can still block
sending even when the wording preview is valid.

Range packets validate the included students' ownership and all competing source
claims, plus the manifest fingerprint. An unrelated student's approval does not
invalidate the packet. Changed included mappings or competing claims block sending
and expose **Regenerate Packet**, preserving the old export. Legacy exports retain
the original whole-index fingerprint check.

Mail servers impose attachment limits; choose smaller ranges if
the provider rejects a ZIP. Failed sends leave the packet pending for retry.

## Marks

TAs fill `marks_awarded` and `comments` in the supplied `marking.csv`. Upload
each returned CSV against its original packet. Unknown students/questions,
duplicate rows, non-finite marks, and marks outside the question's range are
rejected. Partial imports retain earlier imported marks.

`written_marks.csv` shows complete, awaiting_marks, or disagreement. Double
marking requires all assigned TAs to return equal marks; disagreements remain
unresolved. `final_scores.csv` combines agreed written marks with saved objective
marks and marks completion only after every manifest written question is marked.
Written Marks CSV and Combined Scores CSV are downloadable from TA Marking for
the selected export. This change does not introduce cross-export mark aggregation.
They do not silently overwrite the objective-only parser CSV or professor-entered
mark overrides. Double-marking disagreements are listed in the workspace.
