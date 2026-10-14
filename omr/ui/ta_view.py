"""Question marking workspace within the main professor UI."""
from __future__ import annotations

import csv
import html
import json
import re
import urllib.parse
from pathlib import Path

from omr.reader.written import WRITTEN_CROP_LAYOUT_VERSION
from omr.workflows.ta_packets import (
    DEFAULT_TA_EMAIL_MESSAGE, DEFAULT_TA_EMAIL_SUBJECT, TA_EMAIL_FIELDS,
    _validate_snapshot, ta_student_catalog,
)


def packet_output(state, output_id):
    if not re.fullmatch(r"[a-f0-9]{32}", output_id):
        raise ValueError("Invalid TA export")
    root = (Path(state["parse_dir"]) / "ta_packets").resolve()
    output = (root / output_id).resolve()
    if not output.is_relative_to(root) or not (output / "index.json").is_file():
        raise FileNotFoundError("TA export not found for this exam run")
    return output


def assignments_from_form(manifest, form):
    assignments = []
    for question in manifest.get("written_block", []):
        number = int(question["q_no"])
        emails = [email.strip().lower() for email in form.get(f"emails_q{number}", "").split(",") if email.strip()]
        if emails:
            assignments.append(dict(q_no=number, mode=form.get(f"mode_q{number}", "split"),
                                    tas=[dict(name=email.split("@")[0], email=email) for email in emails]))
    if not assignments:
        raise ValueError("Assign at least one written question to a TA")
    return assignments


def _pending_email_section():
    return '''<section id="email-packets"><h2>Email Packets</h2>
      <p class="muted">No generated packets available for email.</p>
      <button disabled>Send Packets</button></section>'''


def _range_assignment_body(state, manifest, base, running):
    escape = lambda value: html.escape(str(value), quote=True)
    students, revision = ta_student_catalog(state.get('parse_dir') or '.')
    questions = manifest.get('written_block', [])
    disabled = 'disabled' if running or not students or not questions else ''
    plans = state.get('ta_range_assignments', [])
    default_q = int(plans[-1]['q_no']) if plans else int(questions[0]['q_no']) if questions else 0
    options = ''.join(f'<option value="{int(q["q_no"])}" data-page="{int(q.get("page", 1))}">'
                      f'Q{int(q["q_no"])} / Page {int(q.get("page", 1))}</option>' for q in questions)
    options = options.replace(f'value="{default_q}"', f'value="{default_q}" selected', 1)
    previous_end = max((p['range_end'] for p in plans if int(p['q_no']) == default_q), default=0)
    next_start = previous_end + 1 if previous_end < len(students) else 1
    rows = []
    for plan in plans:
        identifier = escape(plan['assignment_id'])
        generated = len(plan.get('generated_rolls', []))
        total = len(plan['student_rolls'])
        status = f'{generated}/{total} generated' if generated else 'Pending'
        stale = False
        packet_id = ''
        if plan.get('exports'):
            try:
                output = packet_output(state, plan['exports'][-1])
                packet_index = json.loads((output / 'index.json').read_bytes())
                packet_id = next((p['id'] for p in packet_index['packets'] if p.get('assignment_id') == plan['assignment_id']), '')
                if packet_index.get('crop_layout_version', 1) != WRITTEN_CROP_LAYOUT_VERSION:
                    stale = True
                    status = 'Crop layout updated'
                _validate_snapshot(packet_index)
            except (ValueError, FileNotFoundError):
                stale = True
                status = 'Ownership changed'
        buttons = ''
        if generated < total or stale:
            buttons += f'''<form method="post" action="{base}/generate">
              <input type="hidden" name="assignment_id" value="{identifier}">
              {'<input type="hidden" name="regenerate" value="1">' if stale else ''}
              <button {'disabled' if running else ''}>{'Regenerate Packet' if stale else 'Generate Remaining' if generated else 'Generate Packet'}</button></form>'''
        if plan.get('exports'):
            link = base + '?' + urllib.parse.urlencode(dict(output=plan['exports'][-1], packet=packet_id))
            buttons += f'<a class="button secondary" href="{escape(link)}">Open Packet</a>'
        else:
            buttons += f'''<form method="post" action="{base}/remove-range">
              <input type="hidden" name="assignment_id" value="{identifier}">
              <button class="secondary" {'disabled' if running else ''}>Remove</button></form>'''
        marker = ' / Additional marker' if plan.get('additional_marker') else ''
        rows.append(f'''<tr><td>Q{int(plan['q_no'])}</td>
          <td>{int(plan['range_start'])}-{int(plan['range_end'])}<small>{escape(plan['student_rolls'][0])} to {escape(plan['student_rolls'][-1])}</small></td>
          <td>{escape(plan['tas'][0]['email'])}{marker}</td><td>{escape(status)}</td>
          <td><div class="actions">{buttons}</div></td></tr>''')
    pending = any(len(p.get('generated_rolls', [])) < len(p['student_rolls']) for p in plans)
    data = json.dumps(students).replace('<', '\\u003c')
    return f'''<section><h2>Assign Question Range</h2>
      <div class="ta-summary"><span>{len(students)} students</span><span>Order: roll number</span></div>
      <form id="ta-range-form" method="post" action="{base}/assign-range">
        <input type="hidden" name="catalog_revision" value="{escape(revision)}">
        <fieldset {'disabled' if running else ''} class="ta-range-fields">
          <div><label for="ta-question">Question</label><select id="ta-question" name="q_no" required>{options}</select></div>
          <div><label for="ta-from">From Student</label><input id="ta-from" name="range_start" type="number" min="1" max="{len(students)}" value="{next_start}" required></div>
          <div><label for="ta-to">To Student</label><input id="ta-to" name="range_end" type="number" min="1" max="{len(students)}" value="{len(students) or ''}" required></div>
          <div><label for="ta-email">TA Email</label><input id="ta-email" name="ta_email" type="email" required autocomplete="email"></div>
        </fieldset>
        <div class="actions ta-range-actions"><label class="ta-confirm"><input type="checkbox" name="additional_marker" {'disabled' if running else ''}> Additional marker</label>
        <button id="ta-add-range" {disabled}>Add Assignment</button></div>
        <div class="ta-selection"><strong id="ta-selection-count" role="status"></strong>
          <div class="ta-scroll ta-students"><table><thead><tr><th>Student #</th><th>Roll Number</th><th>Name</th><th>Question Page</th><th>Ownership</th></tr></thead>
          <tbody id="ta-selection-rows"></tbody></table></div></div>
      </form>{'<p class="muted">No written questions in this manifest.</p>' if not questions else ''}
      {'<p class="muted">No matched students available.</p>' if not students else ''}
      <script id="ta-students" type="application/json">{data}</script><script src="/static/ta_ranges.js" defer></script></section>
      <section id="assignments"><h2>Saved Assignments</h2><div class="ta-scroll"><table><thead><tr><th>Question</th><th>Students</th><th>TA</th><th>Status</th><th>Actions</th></tr></thead>
      <tbody>{''.join(rows) or '<tr><td colspan="5" class="empty">No assignments</td></tr>'}</tbody></table></div>
      <form method="post" action="{base}/generate" class="ta-range-actions"><input type="hidden" name="pending_ranges" value="1">
      <button {'disabled' if running or not pending else ''}>Generate Pending Packets</button></form></section>'''


def workspace_body(state, manifest, query, *, mail_defaults=None, mail_error=None):
    escape = lambda value: html.escape(str(value), quote=True)
    mail_defaults = mail_defaults or {}
    value = lambda name: escape(mail_defaults.get(name, ''))
    base = f'/runs/{urllib.parse.quote(state["run_id"])}/ta'
    job = state.get("ta_job", {})
    running = job.get("status") == "running"
    disabled = "disabled" if running else ""
    body = f'''<style>
      .ta-workspace section {{padding:20px 0;border-bottom:1px solid var(--border)}}
      .ta-workspace h2 {{margin:12px 0}}
      .ta-workspace table {{display:table;width:100%;border-collapse:collapse}}
      .ta-workspace td {{overflow-wrap:anywhere}}
      .ta-workspace td small {{display:block;color:var(--muted);margin-top:4px}}
      .ta-workspace .ta-scroll {{overflow:auto;max-width:100%}}
      .ta-workspace .ta-scroll table {{min-width:620px}}
      .ta-workspace button:disabled {{opacity:.5;cursor:not-allowed}}
      .ta-workspace .ta-range-fields {{display:grid;grid-template-columns:1fr 140px 140px 2fr;gap:16px;border:0;padding:0;margin:0;min-width:0}}
      .ta-workspace .ta-range-fields input,.ta-workspace .ta-range-fields select {{width:100%;min-width:0}}
      .ta-workspace .ta-range-actions {{margin-top:16px}}
      .ta-workspace .ta-summary {{display:flex;gap:20px;color:var(--muted);margin-bottom:16px}}
      .ta-workspace .ta-selection {{margin-top:20px}}
      .ta-workspace .ta-students {{max-height:320px;margin-top:8px}}
      .ta-workspace .ta-students thead {{position:sticky;top:0;background:var(--panel)}}
      .ta-workspace .ta-confirm {{display:flex;align-items:center;gap:8px}}
      .ta-workspace input[type="checkbox"] {{width:16px;height:16px;margin:0}}
      .ta-workspace .ta-export-picker {{max-width:100%;flex:1;min-width:180px}}
      .ta-workspace .ta-mail-editor {{display:grid;gap:14px;margin:20px 0}}
      .ta-workspace .ta-mail-editor input,.ta-workspace .ta-mail-editor textarea {{width:100%;max-width:100%;box-sizing:border-box}}
      .ta-workspace .ta-mail-editor textarea {{min-height:180px;resize:vertical;line-height:1.5}}
      .ta-workspace .ta-email-preview {{margin:16px 0}}
      .ta-workspace .ta-email-preview pre {{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;line-height:1.6}}
      .ta-workspace .ta-email-preview dl {{display:grid;grid-template-columns:90px minmax(0,1fr);gap:8px}}
      .ta-workspace .ta-email-preview dt {{font-weight:700}}
      .ta-workspace .ta-email-preview dd {{margin:0;overflow-wrap:anywhere}}
      @media(max-width:700px) {{.ta-workspace .ta-range-fields {{grid-template-columns:1fr 1fr}} .ta-workspace .ta-range-fields>div:first-child,.ta-workspace .ta-range-fields>div:last-child {{grid-column:1/-1}}}}
      </style><div class="ta-workspace"><div class="actions"><a href="/runs/{escape(state["run_id"])}">Back to Exam</a></div>
      <h1>TA Marking</h1><h2>{escape(state.get("exam_id", ""))}</h2>
      <div role="status" class="band">{escape(job.get("message") or "Ready")}</div>
      {'<div role="alert" class="band">' + escape(mail_error) + '</div>' if mail_error else ''}
      {_range_assignment_body(state, manifest, base, running)}'''
    root = Path(state.get("parse_dir") or ".") / "ta_packets"
    exports = sorted([p for p in root.glob("*/index.json") if re.fullmatch(r"[a-f0-9]{32}", p.parent.name)],
                     key=lambda p: p.stat().st_mtime, reverse=True)
    if not exports:
        return body + _pending_email_section() + '</div>'
    selected = query.get("output", [exports[0].parent.name])[0]
    output = packet_output(state, selected)
    index = json.loads((output / "index.json").read_text(encoding="utf-8"))
    download = lambda file: f'{base}/download?{urllib.parse.urlencode(dict(output=selected, file=file))}'
    options = []
    for path in exports:
        info = json.loads(path.read_text(encoding="utf-8"))
        options.append(f'<option value="{path.parent.name}" {"selected" if path.parent.name == selected else ""}>{escape(info["created_at"])} / {len(info["packets"])} packets</option>')
    body += f'''<section><h2>Exports</h2><form method="get" action="{base}" class="actions">
      <select class="ta-export-picker" name="output" aria-label="TA export">{"".join(options)}</select><button class="secondary">Open Export</button></form>
      <p>{len(index["packets"])} packets / {len(index["skipped"])} held-back answers</p>
      <div class="actions"><a class="button secondary" href="{download("skipped.csv")}">Held-Back Report</a>'''
    for filename, label in (("written_marks.csv", "Written Marks CSV"), ("final_scores.csv", "Combined Scores CSV")):
        if (output / filename).is_file():
            body += f'<a class="button secondary" href="{download(filename)}">{label}</a>'
    for q in sorted({int(p["q_no"]) for p in index["packets"]}):
        body += f'<a class="button secondary" href="{download(f"Q{q}/Q{q}_all_students.pdf")}">Q{q} All Students PDF</a>'
    body += '</div><div class="ta-scroll"><table><thead><tr><th>Packet</th><th>TA</th><th>Answers</th><th>Delivery</th><th>Files</th></tr></thead><tbody>'
    for packet in index["packets"]:
        archive = Path(packet["archive"]).relative_to(output).as_posix()
        link = base + "?" + urllib.parse.urlencode(dict(output=selected, packet=packet["id"]))
        label = f"Q{packet['q_no']} / Students {packet['range_start']}-{packet['range_end']}" if packet.get('mode') == 'range' else packet['id']
        body += f'''<tr><td><a href="{link}">{escape(label)}</a></td><td>{escape(packet["ta"]["email"])}</td>
          <td>{len(packet["rows"])}</td><td>{escape(packet["mail_status"])}</td>
          <td><a href="{download(archive.removesuffix(".zip") + "/answers.pdf")}">Answers PDF</a> / <a href="{download(archive)}">ZIP</a> / <a href="{download(archive.removesuffix(".zip") + "/marking.csv")}">Marking CSV</a></td></tr>'''
    body += '</tbody></table></div></section>'
    def packet_label(p):
        return f"Q{p['q_no']} / Students {p['range_start']}-{p['range_end']} / {p['ta']['email']}" if p.get('mode') == 'range' else f"{p['id']} / {p['ta']['email']}"
    selected_id = query.get('packet', [''])[0] or (index['packets'][0]['id'] if index['packets'] else '')
    packet_options = ''.join(f'<option value="{escape(p["id"])}" {"selected" if p["id"] == selected_id else ""}>{escape(packet_label(p))}</option>' for p in index["packets"])
    if not packet_options:
        return body + _pending_email_section() + '</div>'
    stored_template = state.get('ta_mail_template', {})
    subject_template = mail_defaults.get('subject_template', stored_template.get('subject_template', DEFAULT_TA_EMAIL_SUBJECT))
    message_template = mail_defaults.get('message_template', stored_template.get('message_template', DEFAULT_TA_EMAIL_MESSAGE))
    field_options = ''.join(f'<option value="{{{name}}}">{escape(label)}</option>' for name, label in TA_EMAIL_FIELDS.items())
    editor_data = json.dumps(dict(packets=[dict(id=p['id'], label=packet_label(p), status=p['mail_status']) for p in index['packets']],
                                 defaults=dict(subject=DEFAULT_TA_EMAIL_SUBJECT, message=DEFAULT_TA_EMAIL_MESSAGE))).replace('<', '\\u003c')
    body += f'''<section id="email-packets"><h2>Email Packets</h2><form method="post" action="{base}/send">
      <input type="hidden" name="output" value="{selected}"><div class="grid">
      <div><label>Packet</label><select name="packet"><option value="all">All Pending Packets</option>{packet_options}</select></div>
      <div><label>Delivery</label><select name="delivery"><option value="test">Test Recipient</option><option value="real" {'selected' if mail_defaults.get('delivery') == 'real' else ''}>Assigned TAs</option></select></div>
      <div><label>Test Recipient</label><input name="test_recipient" type="email" value="{value('test_recipient')}"></div>
      <div><label>Sender Email</label><input name="sender" type="email" value="{value('sender')}" required></div>
      </div><div class="ta-mail-editor">
      <div><label for="ta-email-subject">Subject</label><input id="ta-email-subject" name="subject_template" value="{escape(subject_template)}" maxlength="512" required></div>
      <div><label for="ta-email-message">Message</label><textarea id="ta-email-message" name="message_template" rows="9" maxlength="65536" required>{escape(message_template)}</textarea></div>
      <div class="actions"><select id="ta-insert-email-field" aria-label="Insert email field"><option value="">Insert field</option>{field_options}</select>
      <button type="button" class="secondary" id="ta-reset-email-template">Reset Template</button>
      <button type="submit" class="secondary" formaction="{base}/save-template" formnovalidate {disabled}>Save Template</button></div>
      </div><details class="ta-email-preview" open><summary>Preview Email</summary>
      <div><label for="ta-email-preview-packet">Preview Packet</label><select id="ta-email-preview-packet"></select></div>
      <p id="ta-email-preview-status" role="status"></p><dl>
      <dt>To</dt><dd id="ta-email-preview-to"></dd><dt>Subject</dt><dd id="ta-email-preview-subject"></dd>
      <dt>Attachment</dt><dd id="ta-email-preview-attachment"></dd></dl><pre id="ta-email-preview-message"></pre></details>
      <script id="ta-email-editor-data" type="application/json">{editor_data}</script><script src="/static/ta_email_editor.js" defer></script>
      <details class="ta-mail-settings"><summary>Mail Server</summary><div class="grid">
      <div><label>SMTP Host</label><input name="host" value="{value('host')}" required></div>
      <div><label>SMTP Port</label><input name="port" type="number" min="1" max="65535" value="{value('port')}" required></div>
      <div><label>Security</label><select name="security"><option value="starttls">STARTTLS</option><option value="ssl" {'selected' if mail_defaults.get('security') == 'ssl' else ''}>SSL/TLS</option></select></div>
      <div><label>SMTP Username</label><input name="username" value="{value('username')}" required></div>
      <div><label>App Password</label><input name="password" type="password" autocomplete="off" required aria-describedby="ta-app-password-link">
      <a id="ta-app-password-link" href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener noreferrer" hidden>Create Google App Password</a></div>
      </div></details><p><label class="ta-confirm"><input type="checkbox" name="confirm" required> Confirm Email Delivery</label></p>
      <button id="ta-send-packets" {disabled}>Send Packets</button></form></section>
      <section><h2>Import Returned Marks</h2><form method="post" enctype="multipart/form-data" action="{base}/import">
      <input type="hidden" name="output" value="{selected}"><div class="grid">
      <div><label>Packet</label><select name="packet">{packet_options}</select></div>
      <div><label>Returned Marking CSV</label><input type="file" name="marks_csv" accept=".csv" required></div>
      </div><button {disabled}>Import Marks</button></form></section>'''
    if (output / "written_marks.csv").is_file():
        with (output / "written_marks.csv").open(newline="", encoding="utf-8") as handle:
            marks = list(csv.DictReader(handle))
        conflicts = [m for m in marks if m["status"] == "disagreement"]
        body += f'<section><h2>Marking Results</h2><p>{sum(m["status"] == "complete" for m in marks)} agreed / {len(conflicts)} disagreements / {sum(m["status"] == "awaiting_marks" for m in marks)} awaiting marks</p>'
        if conflicts:
            body += '<table><thead><tr><th>Roll</th><th>Question</th><th>TA Marks</th></tr></thead><tbody>'
            body += ''.join(f'<tr><td>{escape(m["roll_no"])}</td><td>Q{escape(m["q_no"])}</td><td>{escape(m["ta_marks"])}</td></tr>' for m in conflicts)
            body += '</tbody></table>'
        body += '</section>'
    selected_packet = next((p for p in index["packets"] if p["id"] == query.get("packet", [""])[0]), None)
    if selected_packet:
        body += f'<section><h2>{escape(packet_label(selected_packet))}</h2><div class="ta-scroll"><table><thead><tr><th>Student #</th><th>Roll Number</th><th>Source Page</th><th>Question PDF</th><th>Strip Image</th></tr></thead><tbody>'
        for row in selected_packet["rows"]:
            crop = Path(row["crop"]).relative_to(output).as_posix()
            pdf = f'Q{row["q_no"]}/students/{row["roll_no"]}.pdf'
            body += f'<tr><td>{escape(row.get("student_position", "-"))}</td><td>{escape(row["roll_no"])}</td><td>{escape(row["source_index"])}</td><td><a href="{download(pdf)}">Open PDF</a></td><td><a href="{download(crop)}">Open Image</a></td></tr>'
        body += '</tbody></table></div></section>'
    return body + '</div>'
