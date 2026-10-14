import csv
import json
import smtplib
from pathlib import Path

from PIL import Image
import pytest

from omr.generator.config import ExamConfig, WrittenQuestionConfig
from omr.generator.layout import build_layout
from omr.generator.manifest import build_manifest
from omr.workflows.ta_packets import (
    _smtp_credentials, _validate_snapshot, create_ta_range_assignment, generate_ta_packets,
    import_ta_marks, send_ta_packet, ta_student_catalog,
)


@pytest.fixture
def packet_run(tmp_path):
    config = ExamConfig(exam_id='TA_TEST', course_code='TEST', exam_name='Test', exam_type='quiz',
                        num_mcq=0, written_questions=[WrittenQuestionConfig(q_no=1, max_marks=5, lines=2)])
    manifest = build_manifest(build_layout(config))
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    Image.new('L', (210, 297), 255).save(tmp_path / 'page.png')
    students = [dict(roll_no=str(2024000+i), status='verified', pages=[dict(page=1, source_index=i,
                canonical_image_path=str(tmp_path / 'page.png'))]) for i in range(1, 5)]
    students.append(dict(roll_no='2024999', status='needs_review', pages=[]))
    (tmp_path / 'verified_index.json').write_text(json.dumps(dict(exam_id='TA_TEST', students=students)))
    return tmp_path, path


def assignment(mode='split'):
    return [dict(q_no=1, mode=mode, tas=[dict(email='one@example.edu'), dict(email='two@example.edu')])]


def test_split_coverage_and_unresolved_exclusion(packet_run):
    root, manifest = packet_run
    output = generate_ta_packets(root, manifest, assignment(), batch_size=1)
    index = json.loads((output / 'index.json').read_text())
    rolls = [row['roll_no'] for p in index['packets'] for row in p['rows']]
    assert len(rolls) == len(set(rolls)) == 4
    assert len(index['packets']) == 4
    assert index['skipped'][0]['roll_no'] == '2024999'
    assert (output / 'Q1/students/2024001.pdf').exists()


def test_double_mark_disagreement_and_range_validation(packet_run, tmp_path):
    root, manifest = packet_run
    output = generate_ta_packets(root, manifest, assignment('double'))
    index = json.loads((output / 'index.json').read_text())
    for number, packet in enumerate(index['packets']):
        returned = tmp_path / f'returned{number}.csv'
        with returned.open('w', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=['roll_no', 'q_no', 'marks_awarded'])
            writer.writeheader()
            writer.writerows(dict(roll_no=row['roll_no'], q_no=1, marks_awarded=3+number) for row in packet['rows'])
        import_ta_marks(output, packet['id'], returned)
    with (output / 'written_marks.csv').open() as file:
        grades = list(csv.DictReader(file))
    assert all(row['status'] == 'disagreement' and row['marks_awarded'] == '' for row in grades)
    returned.write_text('roll_no,q_no,marks_awarded\n2024001,1,6\n')
    with pytest.raises(ValueError, match='Invalid marks'):
        import_ta_marks(output, index['packets'][0]['id'], returned)


def test_email_attachment_and_no_duplicate_send(packet_run, monkeypatch):
    root, manifest = packet_run
    output = generate_ta_packets(root, manifest, assignment())
    packet = json.loads((output / 'index.json').read_text())['packets'][0]
    messages = []
    class FakeSmtp:
        def __init__(self, *args, **kwargs): pass
        def ehlo(self): pass
        def starttls(self, **kwargs): pass
        def login(self, *args): pass
        def send_message(self, message): messages.append(message); return {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr('omr.workflows.ta_packets.smtplib.SMTP', FakeSmtp)
    kwargs = dict(sender='prof@example.edu', host='smtp.example.edu', port=587, username='prof', password='test')
    send_ta_packet(output, packet['id'], **kwargs)
    assert messages[0]['To'] == 'one@example.edu'
    assert next(messages[0].iter_attachments()).get_filename().endswith('.zip')
    with pytest.raises(ValueError, match='already been sent'):
        send_ta_packet(output, packet['id'], **kwargs)


def test_duplicate_source_is_excluded(packet_run):
    root, manifest = packet_run
    path = root / 'verified_index.json'
    index = json.loads(path.read_text())
    index['students'][1]['pages'][0]['source_index'] = 1
    path.write_text(json.dumps(index))
    output = generate_ta_packets(root, manifest, assignment())
    exported = json.loads((output / 'index.json').read_text())
    assert len([s for s in exported['skipped'] if s['reason'] == 'Source ownership is missing or disputed']) == 2


def test_import_rejects_changed_ownership_but_allows_grade_updates(packet_run):
    root, manifest = packet_run
    output = generate_ta_packets(root, manifest, assignment())
    packet = json.loads((output / 'index.json').read_text())['packets'][0]
    returned = root / 'returned.csv'
    returned.write_text('roll_no,q_no,marks_awarded\n2024001,1,3\n')
    path = root / 'verified_index.json'
    verified = json.loads(path.read_text())
    verified['students'][0]['mcq_score'] = 2
    path.write_text(json.dumps(verified))
    assert import_ta_marks(output, packet['id'], returned) == 1
    saved = (output / 'index.json').read_bytes()
    verified['students'][0]['pages'][0]['source_index'] = 99
    path.write_text(json.dumps(verified))
    with pytest.raises(ValueError, match='mappings changed'):
        import_ta_marks(output, packet['id'], returned)
    assert (output / 'index.json').read_bytes() == saved


def range_assignment(root, manifest_path, first, last, email='one@example.edu', existing=(), **extra):
    _, revision = ta_student_catalog(root)
    return create_ta_range_assignment(root, json.loads(manifest_path.read_text()),
        dict(q_no='1', range_start=str(first), range_end=str(last), ta_email=email,
             catalog_revision=revision, **extra), existing)


def test_exact_1_to_78_then_79_to_82_is_not_scanner_order(packet_run):
    root, manifest = packet_run
    path = root / 'verified_index.json'
    index = json.loads(path.read_bytes())
    index['students'] = [dict(roll_no=str(2024000 + i), status='verified', pages=[dict(page=1,
        source_index=200 - i, canonical_image_path=str(root / 'page.png'))]) for i in range(83, 0, -1)]
    path.write_text(json.dumps(index))
    first = range_assignment(root, manifest, 1, 78)
    second = range_assignment(root, manifest, 79, 82, 'two@example.edu', existing=[first])
    output = generate_ta_packets(root, manifest, [first, second], batch_size=78)
    result = json.loads((output / 'index.json').read_bytes())
    assert len(result['packets']) == 2
    assert [len(p['rows']) for p in result['packets']] == [78, 4]
    assert [r['roll_no'] for r in result['packets'][0]['rows']] == [str(2024000 + i) for i in range(1, 79)]
    assert [r['student_position'] for r in result['packets'][1]['rows']] == [79, 80, 81, 82]
    assert len({r['roll_no'] for p in result['packets'] for r in p['rows']}) == 82
    assert not result['skipped']
    assert result['ownership_scope_rolls'][-1] == '2024082'


def test_range_packet_remains_valid_after_unrelated_approval_but_blocks_competing_claim(packet_run):
    root, manifest = packet_run
    assignment = range_assignment(root, manifest, 1, 2)
    output = generate_ta_packets(root, manifest, [assignment])
    result = json.loads((output / 'index.json').read_bytes())
    path = root / 'verified_index.json'
    index = json.loads(path.read_bytes())
    index['students'][-1]['status'] = 'verified'
    path.write_text(json.dumps(index))
    _validate_snapshot(result)
    index['students'][-1]['pages'] = [dict(page=1, source_index=1, canonical_image_path=str(root / 'page.png'))]
    path.write_text(json.dumps(index))
    with pytest.raises(ValueError, match='mappings changed'):
        _validate_snapshot(result)
    index['students'][-1]['pages'] = []
    index['students'][0]['pages'][0]['source_index'] = 77
    path.write_text(json.dumps(index))
    with pytest.raises(ValueError, match='mappings changed'):
        _validate_snapshot(result)


@pytest.mark.parametrize('first,last', [(0, 2), (3, 2), (1, 6)])
def test_range_bounds_are_validated(packet_run, first, last):
    root, manifest = packet_run
    with pytest.raises(ValueError, match='student range'):
        range_assignment(root, manifest, first, last)


def test_range_choice_is_frozen_even_after_new_student_changes_positions(packet_run):
    root, manifest = packet_run
    assignment = range_assignment(root, manifest, 2, 3)
    path = root / 'verified_index.json'
    index = json.loads(path.read_bytes())
    index['students'].append(dict(roll_no='2023000', status='verified', pages=[]))
    path.write_text(json.dumps(index))
    output = generate_ta_packets(root, manifest, [assignment])
    result = json.loads((output / 'index.json').read_bytes())
    assert [r['roll_no'] for r in result['packets'][0]['rows']] == ['2024002', '2024003']
    assert [r['student_position'] for r in result['packets'][0]['rows']] == [2, 3]


def test_gmail_app_password_validation_preserves_non_google_passwords():
    with pytest.raises(ValueError, match='not your normal email password'):
        _smtp_credentials('smtp.gmail.com', 587, 'prof@example.edu', 'ordinary', 'starttls')
    assert _smtp_credentials('SMTP.GMAIL.COM', 587, ' prof@example.edu ', 'abcd efgh ijkl mnop', 'starttls') == (
        'SMTP.GMAIL.COM', 'prof@example.edu', 'abcdefghijklmnop')
    assert _smtp_credentials('smtp.example.edu', 587, 'prof', ' a b ', 'starttls')[2] == ' a b '
    with pytest.raises(ValueError, match='port 587 with STARTTLS'):
        _smtp_credentials('smtp.gmail.com', 587, 'prof@example.edu', 'abcdefghijklmnop', 'ssl')


def test_normal_gmail_password_is_rejected_before_any_network_call(packet_run, monkeypatch):
    root, manifest = packet_run
    output = generate_ta_packets(root, manifest, assignment())
    index_file = output / 'index.json'
    original = index_file.read_bytes()
    packet = json.loads(original)['packets'][0]
    def forbidden(*args, **kwargs):
        pytest.fail('Network connection was attempted for an invalid Gmail app password')
    monkeypatch.setattr('omr.workflows.ta_packets.smtplib.SMTP', forbidden)
    with pytest.raises(ValueError, match='16-character Google app password'):
        send_ta_packet(output, packet['id'], sender='prof@example.edu', host='smtp.gmail.com', port=587,
                       username='prof@example.edu', password='normalpassword')
    assert index_file.read_bytes() == original


@pytest.mark.parametrize('failure', ['auth', 'disconnect', 'tls', 'quit'])
def test_smtp_failures_are_sanitized_and_accepted_delivery_is_not_retried(packet_run, monkeypatch, failure):
    import ssl
    root, manifest = packet_run
    output = generate_ta_packets(root, manifest, assignment())
    index_file = output / 'index.json'
    original = index_file.read_bytes()
    packet = json.loads(original)['packets'][0]
    calls = []
    class FakeSmtp:
        def __init__(self, *args, **kwargs): pass
        def ehlo(self): pass
        def starttls(self, **kwargs):
            if failure == 'tls': raise ssl.SSLError('super-secret')
        def login(self, username, password):
            calls.append((username, password))
            if failure == 'auth': raise smtplib.SMTPAuthenticationError(535, b'super-secret')
            if failure == 'disconnect': raise smtplib.SMTPServerDisconnected('super-secret')
        def send_message(self, message): return {}
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *args):
            if failure == 'quit': raise smtplib.SMTPServerDisconnected('super-secret')
    monkeypatch.setattr('omr.workflows.ta_packets.smtplib.SMTP', FakeSmtp)
    kwargs = dict(sender='prof@example.edu', host='smtp.gmail.com', port=587,
                  username='prof@example.edu', password='abcd efgh ijkl mnop')
    if failure == 'quit':
        send_ta_packet(output, packet['id'], **kwargs)
        assert json.loads(index_file.read_bytes())['packets'][0]['mail_status'] == 'sent'
        with pytest.raises(ValueError, match='already been sent'):
            send_ta_packet(output, packet['id'], **kwargs)
    else:
        with pytest.raises(ValueError) as error:
            send_ta_packet(output, packet['id'], **kwargs)
        assert 'super-secret' not in str(error.value)
        if failure == 'disconnect':
            assert 'authenticating your account' in str(error.value)
        assert index_file.read_bytes() == original
    if calls:
        assert calls[0][1] == 'abcdefghijklmnop'


def test_email_renderer_personalizes_fields_without_second_interpolation():
    from omr.workflows.ta_packets import render_ta_email
    packet = dict(id='packet1', q_no=11, mode='range', range_start=1, range_end=78,
                  rows=[{}, {}], ta=dict(name='TA {name}', email='ta@example.edu'))
    rendered = render_ta_email(dict(exam_id='EXAM'), packet, sender='prof@example.edu',
        subject_template='{exam_id} Q{question_number} - {student_range}',
        message_template='Hello {ta_name}\n{answer_count} answers for {ta_email}. '
                         'Packet {packet_id}; sender {sender_email}; literal {{braces}}.')
    assert rendered['subject'] == 'EXAM Q11 - students 1-78'
    assert rendered['message'] == ('Hello TA {name}\n2 answers for ta@example.edu. '
                                   'Packet packet1; sender prof@example.edu; literal {braces}.')


@pytest.mark.parametrize('subject,message', [
    ('', 'hello'), ('subject', '   '), ('x' * 513, 'hello'), ('subject', 'x' * 65537),
    ('subject\nBcc: evil@example.edu', 'hello'), ('subject', '{password}'),
    ('{ta_name.__class__}', 'hello'), ('{ta_name[0]}', 'hello'),
    ('subject', '{ta_name!r}'), ('subject', '{answer_count:04d}'), ('subject', '{oops'),
], ids=['empty-subject', 'empty-message', 'long-subject', 'long-message', 'header-injection',
        'unknown-field', 'attribute', 'indexing', 'conversion', 'format-spec', 'unmatched-brace'])
def test_invalid_email_templates_rejected(subject, message):
    from omr.workflows.ta_packets import validate_ta_email_templates
    with pytest.raises(ValueError):
        validate_ta_email_templates(subject, message)


def test_metadata_cannot_inject_email_subject_headers():
    from omr.workflows.ta_packets import render_ta_email
    packet = dict(id='packet1', q_no=11, rows=[], ta=dict(name='TA', email='ta@example.edu'))
    with pytest.raises(ValueError, match='single line'):
        render_ta_email(dict(exam_id='EXAM\r\nBcc: evil@example.edu'), packet)


def test_custom_email_is_sent_and_audited_without_credentials(packet_run, monkeypatch):
    root, manifest = packet_run
    output = generate_ta_packets(root, manifest, assignment())
    packet = json.loads((output / 'index.json').read_bytes())['packets'][0]
    captured = []
    class FakeSmtp:
        def __init__(self, *args, **kwargs): pass
        def ehlo(self): pass
        def starttls(self, **kwargs): pass
        def login(self, *args): pass
        def send_message(self, message): captured.append(message); return {}
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr('omr.workflows.ta_packets.smtplib.SMTP', FakeSmtp)
    send_ta_packet(output, packet['id'], sender='prof@example.edu', host='smtp.example.edu', port=587,
        username='prof', password='credential-not-in-message', test_recipient='test@example.edu',
        subject_template='Please mark Q{question_number}', message_template='Hello {ta_name},\nDue Friday.')
    assert captured[0]['Subject'] == 'Please mark Q1'
    assert captured[0].get_body().get_content().strip() == 'Hello TA,\nDue Friday.'
    assert next(captured[0].iter_attachments()).get_filename() == Path(packet['archive']).name
    saved = (output / 'index.json').read_text()
    log = json.loads(saved)['packets'][0]['mail_log'][-1]
    assert log['subject'] == 'Please mark Q1' and log['message'] == 'Hello TA,\nDue Friday.'
    assert 'credential-not-in-message' not in saved
