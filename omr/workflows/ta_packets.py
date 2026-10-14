"""Question-wise TA packets built only from resolved source ownership."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import smtplib
import ssl
from string import Formatter
import uuid
import zipfile
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from omr.contracts import load_manifest
from omr.reader.written import WRITTEN_CROP_LAYOUT_VERSION, crop_written_responses


DEFAULT_TA_EMAIL_SUBJECT = '{exam_id}: Q{question_number} / {student_range}'
DEFAULT_TA_EMAIL_MESSAGE = (
    'Hello {ta_name},\n\n'
    'Attached are {answer_count} approved answers for Q{question_number} ({student_range}) and marking.csv. '
    'The CSV identifies the exact students included. Please enter marks and comments in the CSV '
    'and return it to the professor.\n\nThank you.'
)
TA_EMAIL_FIELDS = {
    'exam_id': 'Exam ID', 'ta_name': 'TA Name', 'ta_email': 'TA Email',
    'question_number': 'Question Number', 'student_range': 'Student Range',
    'answer_count': 'Answer Count', 'packet_id': 'Packet ID', 'sender_email': 'Sender Email',
}


def validate_ta_email_templates(subject, message):
    for text, limit, label in ((subject, 512, 'Subject'), (message, 65536, 'Message')):
        if not isinstance(text, str) or not text.strip() or len(text) > limit:
            raise ValueError(f'{label} must be nonempty and at most {limit} characters')
        if label == 'Subject' and ('\r' in text or '\n' in text):
            raise ValueError('Email subject must be a single line')
        try:
            pieces = list(Formatter().parse(text))
        except ValueError:
            raise ValueError('Email template has unmatched braces; use {{ and }} for literal braces') from None
        for _literal, field, spec, conversion in pieces:
            if field is not None and (field not in TA_EMAIL_FIELDS or spec or conversion):
                raise ValueError('Unsupported email field; use the fields offered in the template editor')


def render_ta_email(index, packet, *, sender='', subject_template=None, message_template=None):
    subject = DEFAULT_TA_EMAIL_SUBJECT if subject_template is None else subject_template
    message = DEFAULT_TA_EMAIL_MESSAGE if message_template is None else message_template
    validate_ta_email_templates(subject, message)
    context = dict(exam_id=index['exam_id'], ta_name=packet['ta'].get('name') or 'TA',
        ta_email=packet['ta']['email'], question_number=packet['q_no'],
        student_range=f"students {packet['range_start']}-{packet['range_end']}" if packet.get('mode') == 'range' else packet['id'],
        answer_count=len(packet['rows']), packet_id=packet['id'], sender_email=sender)
    rendered = dict(subject=subject.format_map(context), message=message.format_map(context))
    validate_ta_email_templates_rendered(rendered)
    return rendered


def validate_ta_email_templates_rendered(rendered):
    if not rendered['subject'].strip() or not rendered['message'].strip():
        raise ValueError('Rendered subject and message must not be empty')
    if '\n' in rendered['subject'] or '\r' in rendered['subject']:
        raise ValueError('Rendered email subject must be a single line')
    if len(rendered['subject']) > 512 or len(rendered['message']) > 65536:
        raise ValueError('Rendered email exceeds the subject or message size limit')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temp.replace(path)


def _safe(value):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', str(value)):
        raise ValueError(f'Invalid identifier: {value}')
    return str(value)


def _email(value):
    if not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+', value):
        raise ValueError(f'Invalid email: {value}')
    return value


def ta_student_catalog(parsed_dir):
    path = Path(parsed_dir) / 'verified_index.json'
    if not path.is_file():
        return [], ''
    students = sorted(json.loads(path.read_bytes())['students'], key=lambda s: s['roll_no'])
    rolls = [s['roll_no'] for s in students]
    if len(rolls) != len(set(rolls)):
        raise ValueError('Duplicate students in the ownership index')
    revision = hashlib.sha256(json.dumps(rolls).encode()).hexdigest()
    return [dict(position=i, roll_no=s['roll_no'], name=s.get('student_name', ''),
                 status=s.get('status', ''), pages=sorted(_pages(s)))
            for i, s in enumerate(students, 1)], revision


def create_ta_range_assignment(parsed_dir, manifest, form, existing):
    students, revision = ta_student_catalog(parsed_dir)
    if not revision or form.get('catalog_revision') != revision:
        raise ValueError('Student list changed; refresh before assigning a range')
    try:
        first, last, q_no = int(form['range_start']), int(form['range_end']), int(form['q_no'])
    except (KeyError, ValueError):
        raise ValueError('Choose a question and enter whole-number student positions') from None
    if not 1 <= first <= last <= len(students):
        raise ValueError(f'Choose a student range between 1 and {len(students)}')
    if q_no not in {int(q['q_no']) for q in manifest.get('written_block', [])}:
        raise ValueError('Choose a written question from this manifest')
    email = _email(form.get('ta_email', '').strip().lower())
    selected = [s['roll_no'] for s in students[first - 1:last]]
    extra = form.get('additional_marker') == 'on'
    for item in existing:
        if int(item['q_no']) != q_no or not set(item['student_rolls']) & set(selected):
            continue
        if item['tas'][0]['email'].lower() == email:
            raise ValueError('This TA already has students in the selected range for this question')
        if not extra:
            raise ValueError('Range overlaps another TA; select Additional marker to intentionally share answers')
    return dict(assignment_id=uuid.uuid4().hex, q_no=q_no, mode='range',
                tas=[dict(name=email.split('@')[0], email=email)], student_rolls=selected,
                student_positions={s['roll_no']: s['position'] for s in students[first - 1:last]},
                range_start=first, range_end=last, catalog_revision=revision,
                additional_marker=extra, created_at=_now(), generated_rolls=[], exports=[])


def _pages(student):
    selected = {int(p.get('page', p.get('page_index'))): p for p in student.get('pages', [])}
    selected.update({int(p.get('page', p.get('page_index'))): p for p in student.get('manual_pages', [])})
    return selected


def _image_path(value, root, student):
    path = Path(value or '')
    for candidate in (path, root / path, root / 'students' / student['roll_no'] / path):
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError('Selected source image is missing')


def _ownership_digest(verified, root):
    # Grading notes and scores can change without invalidating TA ownership.
    students = []
    for student in sorted(verified.get('students', []), key=lambda s: s['roll_no']):
        pages = []
        for number, page in sorted(_pages(student).items()):
            try:
                image = _image_path(page.get('canonical_image_path'), root, student)
                stat = image.stat()
                image_state = (str(image), stat.st_size, stat.st_mtime_ns)
            except ValueError:
                image_state = (page.get('canonical_image_path'), None, None)
            pages.append((number, page.get('source_index'), image_state))
        students.append(dict(roll_no=student['roll_no'], program=student.get('program'), pages=pages,
            resolved=student.get('status') in {'verified', 'approved', 'auto_matched'}))
    return hashlib.sha256(json.dumps(dict(exam_id=verified.get('exam_id'), students=students), sort_keys=True).encode()).hexdigest()


def _range_ownership_digest(verified, root, rolls):
    selected = [s for s in verified['students'] if s['roll_no'] in set(rolls)]
    sources = {p.get('source_index') for s in selected for p in _pages(s).values()}
    claims = {str(source): sorted({s['roll_no'] for s in verified['students']
                                  if any(p.get('source_index') == source for p in _pages(s).values())})
              for source in sources}
    snapshot = _ownership_digest(dict(verified, students=selected), root)
    return hashlib.sha256(json.dumps(dict(snapshot=snapshot, claims=claims), sort_keys=True).encode()).hexdigest()


def _pdf(path, rows, dpi):
    # Each PDF page has the strip's own aspect ratio and an identity caption.
    canvas = Canvas(str(path))
    for row in rows:
        with Image.open(row['crop']) as image:
            width, height = image.size
            row_dpi = row.get('dpi', dpi)
            width_pt, height_pt = width * 72 / row_dpi, height * 72 / row_dpi
            header_height = 0
            if row.get('header'):
                with Image.open(row['header']) as header:
                    header_height = header.height * 72 / row_dpi
                    canvas.setPageSize((width_pt, height_pt + header_height + canvas._fontsize * 3))
                    canvas.drawImage(ImageReader(header), 0, height_pt, width=width_pt, height=header_height)
            caption_height = canvas._fontsize * 3
            canvas.setPageSize((width_pt, height_pt + header_height + caption_height))
            canvas.drawString(10, height_pt + header_height + caption_height / 2,
                              f"{row['roll_no']} | Q{row['q_no']} | Max marks {row['max_marks']} | Source {row['source_index']}")
            canvas.drawImage(ImageReader(image), 0, 0, width=width_pt, height=height_pt)
            canvas.showPage()
    canvas.save()


def generate_ta_packets(parsed_dir, manifest_path, assignments, *, batch_size=100, progress=None):
    root = Path(parsed_dir).resolve()
    manifest = load_manifest(manifest_path)
    verified_path = root / 'verified_index.json'
    verified_bytes = verified_path.read_bytes()
    verified = json.loads(verified_bytes)
    if verified.get('exam_id') != manifest['exam_id']:
        raise ValueError('The manifest does not belong to this run')
    questions = {int(q['q_no']): q for q in manifest['written_block']}
    if not assignments or batch_size < 1:
        raise ValueError('Assign at least one written question and use a positive batch size')
    students = sorted(verified['students'], key=lambda s: s['roll_no'])
    known_rolls = {s['roll_no'] for s in students}
    normalized = {}
    range_ids = set()
    for assignment in assignments:
        q_no = int(assignment['q_no'])
        mode = assignment.get('mode', 'split')
        if q_no not in questions or (q_no in normalized and
            (mode != 'range' or any(a['mode'] != 'range' for a in normalized[q_no]))):
            raise ValueError(f'Unknown or duplicate written question Q{q_no}')
        tas = assignment['tas']
        if not tas or len({ta['email'].lower() for ta in tas}) != len(tas):
            raise ValueError(f'Q{q_no} needs distinct TA email addresses')
        for ta in tas:
            _email(ta['email'])
        if mode not in {'split', 'double', 'range'} or (mode == 'double' and len(tas) < 2):
            raise ValueError('Double marking requires at least two TAs')
        if mode == 'range':
            identifier = assignment.get('assignment_id', '')
            rolls = assignment.get('student_rolls', [])
            if (not re.fullmatch(r'[a-f0-9]{32}', identifier) or identifier in range_ids
                or len(tas) != 1 or not rolls or len(set(rolls)) != len(rolls) or not set(rolls) <= known_rolls):
                raise ValueError('Invalid range assignment or unknown student')
            positions = assignment.get('student_positions', {})
            if (not set(rolls) <= positions.keys() or
                any(type(positions[r]) is not int or positions[r] < 1 for r in rolls) or
                len({positions[r] for r in rolls}) != len(rolls)):
                raise ValueError('Invalid student positions in range assignment')
            range_ids.add(identifier)
            for other in normalized.get(q_no, []):
                if set(other['student_rolls']) & set(rolls):
                    if other['tas'][0]['email'].lower() == tas[0]['email'].lower():
                        raise ValueError('Duplicate student assignment to the same TA')
                    if not assignment.get('additional_marker'):
                        raise ValueError('Overlapping ranges require an additional marker')
        normalized.setdefault(q_no, []).append(dict(assignment, mode=mode))
    claims = {}
    for student in students:
        for page in _pages(student).values():
            source = page.get('source_index')
            if source is not None:
                claims.setdefault(source, set()).add(student['roll_no'])
    output = root / 'ta_packets' / uuid.uuid4().hex
    output.mkdir(parents=True)
    index = dict(exam_id=manifest['exam_id'], created_at=_now(),
                 crop_layout_version=WRITTEN_CROP_LAYOUT_VERSION,
                 ownership_sha256=_ownership_digest(verified, root), ownership_digest_version=2,
                 manifest_sha256=hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest(),
                 assignments=[a for items in normalized.values() for a in items], packets=[], skipped=[], grades={})
    index['manifest_path'] = str(Path(manifest_path).resolve())
    index['written_questions'] = {str(q): question['max_marks'] for q, question in questions.items()}
    index['objective_kinds'] = [kind for kind in ('mcq', 'numerical') if manifest.get(f'{kind}_block')]
    index['students'] = [dict(roll_no=s['roll_no'], student_name=s.get('student_name', ''),
                             mcq_score=s.get('mcq_score'), numerical_score=s.get('numerical_score'))
                         for s in students]
    rows_by_question = {q: [] for q in normalized}
    crop_cache = {}
    for number, student in enumerate(students, 1):
        roll = _safe(student['roll_no'])
        pages = _pages(student)
        for q_no in normalized:
            selections = normalized[q_no]
            if selections[0]['mode'] == 'range' and not any(roll in a['student_rolls'] for a in selections):
                continue
            question = questions[q_no]
            page = pages.get(int(question.get('page', 1)))
            reason = None
            if student.get('status') not in {'verified', 'approved', 'auto_matched'}:
                reason = 'Ownership awaiting approval'
            elif not page:
                reason = 'Question page missing'
            elif page.get('source_index') is None or len(claims.get(page['source_index'], set())) != 1:
                reason = 'Source ownership is missing or disputed'
            if reason:
                index['skipped'].append(dict(roll_no=roll, q_no=q_no, reason=reason))
                continue
            try:
                path = _image_path(page.get('canonical_image_path'), root, student)
                with Image.open(path) as source:
                    image = source.convert('L')
                dpi = image.width * 25.4 / manifest['page']['width_mm']
                folder = output / f'Q{q_no}' / 'students'
                folder.mkdir(parents=True, exist_ok=True)
                crop_manifest = dict(manifest)
                # Preserve all neighbouring geometry when cropping one question.
                cache_key = (roll, int(question.get('page', 1)))
                if cache_key not in crop_cache:
                    crop_cache[cache_key] = crop_written_responses({int(question.get('page', 1)): image},
                        dict(crop_manifest, written_block=[q for q in manifest['written_block']
                             if int(q.get('page', 1)) == int(question.get('page', 1))]),
                        output / 'crops' / roll / f'page_{question.get("page", 1)}', dpi)
                crops = crop_cache[cache_key]
                crop = next(c for c in crops if c.q_no == q_no)
                row = dict(roll_no=roll, q_no=q_no, max_marks=question['max_marks'],
                           source_index=page['source_index'], crop=crop.crop_path, header=crop.page_header_path, dpi=dpi)
                _pdf(folder / f'{roll}.pdf', [row], dpi)
                rows_by_question[q_no].append(row)
            except (ValueError, OSError) as exc:
                index['skipped'].append(dict(roll_no=roll, q_no=q_no, reason=str(exc)))
        if progress:
            progress(number, len(students))
    for q_no, rows in rows_by_question.items():
        if not rows:
            continue
        folder = output / f'Q{q_no}'
        _pdf(folder / f'Q{q_no}_all_students.pdf', rows, rows[0]['dpi'])
        for assignment in normalized[q_no]:
            for ta_number, ta in enumerate(assignment['tas']):
                if assignment['mode'] == 'range':
                    selected = set(assignment['student_rolls'])
                    assigned = [dict(row, student_position=assignment['student_positions'][row['roll_no']])
                                for row in rows if row['roll_no'] in selected]
                else:
                    assigned = rows[ta_number::len(assignment['tas'])] if assignment['mode'] == 'split' else rows
                for start in range(0, len(assigned), batch_size):
                    batch = assigned[start:start + batch_size]
                    tag = f"A{assignment['assignment_id']}" if assignment['mode'] == 'range' else f'TA{ta_number+1}'
                    packet_id = f'Q{q_no}_{tag}_batch{start//batch_size+1}'
                    packet_dir = folder / packet_id
                    packet_dir.mkdir()
                    _pdf(packet_dir / 'answers.pdf', batch, batch[0]['dpi'])
                    with (packet_dir / 'marking.csv').open('w', newline='', encoding='utf-8') as handle:
                        fields = ['roll_no', 'q_no', 'max_marks', 'marks_awarded', 'comments']
                        if assignment['mode'] == 'range':
                            fields += ['student_position', 'source_index']
                        writer = csv.DictWriter(handle, fieldnames=fields)
                        writer.writeheader()
                        writer.writerows({k: row[k] for k in fields if k in row} for row in batch)
                    archive = folder / f'{packet_id}.zip'
                    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zipped:
                        for file in packet_dir.iterdir():
                            zipped.write(file, file.name)
                        for row in batch:
                            zipped.write(folder / 'students' / f"{row['roll_no']}.pdf", f"students/{row['roll_no']}.pdf")
                    packet = dict(id=packet_id, q_no=q_no, ta=ta, mode=assignment['mode'],
                                  archive=str(archive), rows=batch, mail_status='pending')
                    if assignment['mode'] == 'range':
                        packet.update(assignment_id=assignment['assignment_id'], range_start=assignment['range_start'],
                                      range_end=assignment['range_end'])
                    index['packets'].append(packet)
    with (output / 'skipped.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['roll_no', 'q_no', 'reason'])
        writer.writeheader()
        writer.writerows(index['skipped'])
    if _ownership_digest(json.loads(verified_path.read_bytes()), root) != index['ownership_sha256']:
        raise ValueError('Ownership changed during export; regenerate the packet')
    if all(a.get('mode') == 'range' for a in assignments):
        scope = sorted({r['roll_no'] for p in index['packets'] for r in p['rows']})
        index['ownership_scope_rolls'] = scope
        index['ownership_digest_version'] = 3
        index['ownership_sha256'] = _range_ownership_digest(verified, root, scope)
        index['students'] = [s for s in index['students'] if s['roll_no'] in scope]
    index['ownership_path'] = str(verified_path)
    _json(output / 'index.json', index)
    return output


def _validate_snapshot(index):
    path = Path(index['ownership_path'])
    if index.get('ownership_digest_version') == 3:
        current = _range_ownership_digest(json.loads(path.read_bytes()), path.parent, index['ownership_scope_rolls'])
    elif index.get('ownership_digest_version') == 2:
        current = _ownership_digest(json.loads(path.read_bytes()), path.parent)
    else:
        current = hashlib.sha256(path.read_bytes()).hexdigest()
    if current != index['ownership_sha256']:
        raise ValueError('Student mappings changed after export; regenerate TA packets')
    if index.get('manifest_path') and hashlib.sha256(Path(index['manifest_path']).read_bytes()).hexdigest() != index['manifest_sha256']:
        raise ValueError('Manifest changed after export; regenerate TA packets')


def _packet(index, packet_id):
    packet = next((p for p in index['packets'] if p['id'] == packet_id), None)
    if packet is None:
        raise ValueError('Packet not found in this TA export')
    return packet


def _smtp_credentials(host, port, username, password, security):
    host, username = host.strip(), username.strip()
    if not host or not username or not password:
        raise ValueError('SMTP host, username and password are required')
    if security not in {'ssl', 'starttls'}:
        raise ValueError('Choose SSL/TLS or STARTTLS')
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError('SMTP port must be between 1 and 65535')
    if host.lower().rstrip('.') == 'smtp.gmail.com':
        if (port, security) not in {(587, 'starttls'), (465, 'ssl')}:
            raise ValueError('For Gmail use port 587 with STARTTLS, or port 465 with SSL/TLS')
        # Google displays app passwords in spaced groups; never trim other providers' passwords.
        password = ''.join(password.split())
        if not re.fullmatch(r'[A-Za-z0-9]{16}', password):
            raise ValueError('Gmail requires a 16-character Google app password, not your normal email password. '
                             'Create one at myaccount.google.com/apppasswords; your college administrator may restrict it.')
    return host, username, password


def send_ta_packet(output, packet_id, *, sender, host, port, username, password, security='starttls', test_recipient=None,
                   subject_template=None, message_template=None):
    output = Path(output)
    index = json.loads((output / 'index.json').read_text())
    _validate_snapshot(index)
    packet = _packet(index, packet_id)
    if packet.get('mail_status') == 'sent' and not test_recipient:
        raise ValueError('This packet has already been sent')
    rendered = render_ta_email(index, packet, sender=sender, subject_template=subject_template, message_template=message_template)
    host, username, password = _smtp_credentials(host, port, username, password, security)
    message = EmailMessage()
    message['From'] = _email(sender)
    message['To'] = _email(test_recipient or packet['ta']['email'])
    message['Subject'] = rendered['subject']
    message.set_content(rendered['message'])
    archive = Path(packet['archive'])
    message.add_attachment(archive.read_bytes(), maintype='application', subtype='zip', filename=archive.name)
    context = ssl.create_default_context()
    stage = 'connecting to the mail server'
    accepted = False
    smtp = None
    try:
        if security == 'ssl':
            smtp = smtplib.SMTP_SSL(host, port, timeout=60, context=context)
        else:
            smtp = smtplib.SMTP(host, port, timeout=60)
        with smtp:
            stage = 'negotiating TLS'
            smtp.ehlo()
            if security == 'starttls':
                smtp.starttls(context=context)
                smtp.ehlo()
            stage = 'authenticating your account'
            smtp.login(username, password)
            stage = 'sending the packet'
            refused = smtp.send_message(message)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
            accepted = True
            stage = 'closing the connection'
    except smtplib.SMTPAuthenticationError as exc:
        raise ValueError(f'SMTP authentication was rejected (code {exc.smtp_code}). '
                         'For Gmail, use an app password generated for the SMTP Username account, '
                         'not its normal login password. If app passwords are unavailable, contact college IT.') from None
    except smtplib.SMTPServerDisconnected:
        if not accepted:
            raise ValueError(f'Mail server disconnected while {stage}. '
                             'Check the port/security pairing and account authentication policy. '
                             'Delivery is not confirmed; check the recipient inbox before retrying.') from None
    except ssl.SSLError:
        raise ValueError('SMTP TLS verification failed. Check the mail-server address and port/security pairing; '
                         'certificate verification must remain enabled.') from None
    except (smtplib.SMTPException, OSError) as exc:
        if not accepted:
            raise ValueError(f'Mail operation failed while {stage} ({type(exc).__name__}). '
                             'Check server permissions, recipient and attachment limits. '
                             'Delivery is not confirmed; check the inbox before retrying.') from None
    finally:
        if smtp is not None and hasattr(smtp, 'close'):
            try:
                smtp.close()
            except (OSError, smtplib.SMTPException):
                pass
    packet['mail_status'] = 'sent' if not test_recipient else packet.get('mail_status', 'pending')
    packet.setdefault('mail_log', []).append(dict(sent_at=_now(), recipient=message['To'], test=bool(test_recipient),
                                                subject=rendered['subject'], message=rendered['message']))
    _json(output / 'index.json', index)


def import_ta_marks(output, packet_id, csv_path):
    output = Path(output)
    index = json.loads((output / 'index.json').read_text())
    _validate_snapshot(index)
    current_students = {s['roll_no']: s for s in json.loads(Path(index['ownership_path']).read_bytes())['students']}
    for student in index['students']:
        current = current_students[student['roll_no']]
        student.update(student_name=current.get('student_name', ''), mcq_score=current.get('mcq_score'),
                       numerical_score=current.get('numerical_score'))
    packet = _packet(index, packet_id)
    expected = {(r['roll_no'], int(r['q_no'])): r for r in packet['rows']}
    marks = {}
    seen = set()
    with Path(csv_path).open(newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        if not {'roll_no', 'q_no', 'marks_awarded'}.issubset(reader.fieldnames or []):
            raise ValueError('Marking CSV requires roll_no, q_no and marks_awarded')
        for row in reader:
            key = (row['roll_no'], int(row['q_no']))
            if key not in expected or key in seen:
                raise ValueError('Unknown or duplicate student/question in returned marks')
            seen.add(key)
            if not row.get('marks_awarded', '').strip():
                continue
            score = float(row['marks_awarded'])
            if not math.isfinite(score) or not 0 <= score <= expected[key]['max_marks']:
                raise ValueError(f'Invalid marks for {key}')
            marks[key] = dict(marks_awarded=score, comments=row.get('comments', ''))
    existing = {(g['roll_no'], g['q_no']): g for g in index.setdefault('grades', {}).get(packet_id, [])}
    existing.update({(r, q): dict(roll_no=r, q_no=q, **value) for (r, q), value in marks.items()})
    index['grades'][packet_id] = list(existing.values())
    packet.setdefault('import_log', []).append(dict(imported_at=_now(), count=len(marks),
        marks=[dict(roll_no=r, q_no=q, **value) for (r, q), value in marks.items()]))
    combined = {}
    required = {}
    for item in index['packets']:
        for row in item['rows']:
            key = (row['roll_no'], row['q_no'])
            required.setdefault(key, set()).add(item['ta']['email'])
        for grade in index['grades'].get(item['id'], []):
            combined.setdefault((grade['roll_no'], grade['q_no']), {})[item['ta']['email']] = grade['marks_awarded']
    resolved = {}
    with (output / 'written_marks.csv').open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=['roll_no', 'q_no', 'marks_awarded', 'status', 'ta_marks'])
        writer.writeheader()
        for (roll, question), tas in sorted(required.items()):
            values = combined.get((roll, question), {})
            complete = set(values) == tas
            agreed = complete and len(set(values.values())) == 1
            if agreed:
                resolved[(roll, question)] = next(iter(values.values()))
            writer.writerow(dict(roll_no=roll, q_no=question,
                marks_awarded=next(iter(values.values())) if agreed else '',
                status='complete' if agreed else 'disagreement' if complete else 'awaiting_marks',
                ta_marks=json.dumps(values)))
    with (output / 'final_scores.csv').open('w', newline='', encoding='utf-8') as handle:
        columns = ['roll_no', 'name', 'written_score', 'written_total', 'total_score', 'grading_complete']
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        questions = {int(q) for q in index['written_questions']}
        for student in index['students']:
            roll = student['roll_no']
            complete = all((roll, q) in resolved for q in questions)
            written_score = sum(resolved.get((roll, q), 0) for q in questions)
            objective_complete = all(student[f'{kind}_score'] is not None for kind in index['objective_kinds'])
            writer.writerow(dict(roll_no=roll, name=student['student_name'], written_score=written_score,
                written_total=sum(index['written_questions'].values()),
                total_score=written_score + float(student['mcq_score'] or 0) + float(student['numerical_score'] or 0)
                if complete and objective_complete else '', grading_complete=complete and objective_complete))
    _json(output / 'index.json', index)
    return len(marks)
