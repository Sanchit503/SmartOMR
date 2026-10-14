from __future__ import annotations

import csv
import io
import json
import urllib.error
import urllib.request

import pymupdf
import pytest

from omr.ui import app, ta_view
from omr.ui.tests.test_app import _multipart_body
from omr.ui.tests.test_inspection import get, post, server
from omr.workflows.tests.test_ta_packets import packet_run


def configured(packet_run, tmp_path):
    parsed, manifest = packet_run
    store = app.RunStore(app.UiConfig(tmp_path / 'data'))
    store.run_dir('TA_TEST_run').mkdir()
    app._write_json(store.state_path('TA_TEST_run'), dict(run_id='TA_TEST_run', exam_id='TA_TEST', status='completed',
                      parse_dir=str(parsed), inputs={'manifest_path': str(manifest)}))
    return store, parsed


def generate(url, store):
    post(url + '/runs/TA_TEST_run/ta/generate', {'emails_q1': 'ta@example.edu', 'batch_size': '2'})
    store._worker.join(timeout=15)
    assert not store._worker.is_alive()
    state = store.read_state('TA_TEST_run')
    assert state['ta_job']['status'] == 'complete', state['ta_job']
    return state['ta_output']


def test_main_ui_assignment_generation_download_and_import(packet_run, tmp_path):
    store, parsed = configured(packet_run, tmp_path)
    with server(store) as url:
        base = url + '/runs/TA_TEST_run/ta'
        body, _ = get(base)
        assert b'Assign Question Range' in body and b'name="range_start"' in body
        assert b'Split Students' not in body and b'Students Per Packet' not in body
        assert b'Email Packets' in body and b'No generated packets available' in body
        assert b'<button disabled>Send Packets</button>' in body
        output_id = generate(url, store)
        body, _ = get(base)
        assert b'Email Packets' in body and b'Import Returned Marks' in body
        output = parsed / 'ta_packets' / output_id
        index = json.loads((output / 'index.json').read_text())
        packet = index['packets'][0]
        body, _ = get(base + '?output=' + output_id + '&packet=' + packet['id'])
        assert b'Open PDF' in body and b'Open Image' in body
        pdf, headers = get(base + '/download?output=' + output_id + '&file=Q1/students/2024001.pdf')
        assert headers['Content-Type'] == 'application/pdf'
        with pymupdf.open(stream=pdf, filetype='pdf') as doc:
            assert len(doc) == 1 and '2024001' in doc[0].get_text()
        owned = (parsed / 'verified_index.json').read_bytes()
        returned = 'roll_no,q_no,marks_awarded,comments\n' + ''.join(f'{r["roll_no"]},1,3,Checked\n' for r in packet['rows'])
        boundary = 'ta-returned'
        content = _multipart_body(boundary, [('output', None, output_id.encode()),
            ('packet', None, packet['id'].encode()), ('marks_csv', 'marks.csv', returned.encode())])
        request = urllib.request.Request(base + '/import', data=content,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}, method='POST')
        with urllib.request.urlopen(request) as response:
            assert b'Imported 2 marks' in response.read()
        assert (parsed / 'verified_index.json').read_bytes() == owned
        grades = list(csv.DictReader(io.StringIO((output / 'written_marks.csv').read_text())))
        assert sum(g['status'] == 'complete' for g in grades) == 2
        assert (output / 'final_scores.csv').is_file()
        assert not list((store.run_dir('TA_TEST_run') / '_ta_uploads').glob('*'))
    restarted = app.RunStore(app.UiConfig(tmp_path / 'data'))
    with server(restarted) as url:
        body, _ = get(url + '/runs/TA_TEST_run/ta')
        assert b'Written Marks CSV' in body and b'Q1_TA1_batch1' in body


def test_main_ui_test_email_requires_address_and_credentials_not_saved(packet_run, tmp_path, monkeypatch):
    store, _ = configured(packet_run, tmp_path)
    calls = []
    monkeypatch.setattr(app, 'send_ta_packet', lambda *args, **kwargs: calls.append(kwargs))
    with server(store) as url:
        output = generate(url, store)
        fields = dict(output=output, packet='Q1_TA1_batch1', delivery='test', confirm='on',
                      sender='prof@example.edu', host='smtp.example.edu', port='587',
                      username='prof', password='secret')
        with pytest.raises(urllib.error.HTTPError) as error:
            post(url + '/runs/TA_TEST_run/ta/send', fields)
        assert b'test recipient' in error.value.read() and not calls
        post(url + '/runs/TA_TEST_run/ta/send', {**fields, 'test_recipient': 'check@example.edu'})
    assert calls[0]['test_recipient'] == 'check@example.edu'
    assert 'secret' not in store.state_path('TA_TEST_run').read_text()


def test_downloads_are_scoped_to_run_and_export(packet_run, tmp_path):
    store, _ = configured(packet_run, tmp_path)
    with server(store) as url:
        output = generate(url, store)
        for file in ('../index.json', '../../verified_index.json'):
            with pytest.raises(urllib.error.HTTPError) as error:
                get(url + f'/runs/TA_TEST_run/ta/download?output={output}&file={file}')
            assert error.value.code == 404
    with pytest.raises(FileNotFoundError):
        ta_view.packet_output({'parse_dir': str(tmp_path / 'different')}, output)


def test_no_written_questions_is_visible_without_generating(packet_run, tmp_path):
    store, _ = configured(packet_run, tmp_path)
    state = store.read_state('TA_TEST_run')
    body = ta_view.workspace_body(state, {'written_block': []}, {})
    assert 'No written questions' in body and '<button disabled>' in body
    assert 'Email Packets' in body


def test_assignments_support_one_or_multiple_tas_per_question():
    manifest = {'written_block': [{'q_no': 11}, {'q_no': 12}, {'q_no': 13}]}
    assignments = ta_view.assignments_from_form(manifest, {
        'emails_q11': 'ONE@example.edu', 'mode_q11': 'split',
        'emails_q12': 'two@example.edu, THREE@example.edu, four@example.edu', 'mode_q12': 'double',
    })
    assert [a['q_no'] for a in assignments] == [11, 12]
    assert assignments[0]['tas'] == [{'name': 'one', 'email': 'one@example.edu'}]
    assert assignments[1]['mode'] == 'double'
    assert len(assignments[1]['tas']) == 3


@pytest.mark.parametrize('mode', ['split', 'double'])
def test_three_ta_question_export_through_main_ui(packet_run, tmp_path, mode):
    store, parsed = configured(packet_run, tmp_path)
    with server(store) as url:
        post(url + '/runs/TA_TEST_run/ta/generate', {
            'emails_q1': 'one@example.edu, two@example.edu, three@example.edu',
            'mode_q1': mode, 'batch_size': '100',
        })
        store._worker.join(timeout=15)
        assert not store._worker.is_alive()
        state = store.read_state('TA_TEST_run')
        assert state['ta_job']['status'] == 'complete'
        index = json.loads((parsed / 'ta_packets' / state['ta_output'] / 'index.json').read_text())
        packets = index['packets']
        assert len(packets) == 3
        by_ta = {p['ta']['email']: {r['roll_no'] for r in p['rows']} for p in packets}
        assert set(by_ta) == {'one@example.edu', 'two@example.edu', 'three@example.edu'}
        if mode == 'double':
            assert all(len(rolls) == 4 for rolls in by_ta.values())
            assert len(set.union(*by_ta.values())) == 4
        else:
            assert sum(len(rolls) for rolls in by_ta.values()) == 4
            assert len(set.union(*by_ta.values())) == 4
        body, _ = get(url + '/runs/TA_TEST_run/ta')
        assert b'Email Packets' in body and b'Assigned TAs' in body
        assert b'Assign Question Range' in body


def add_range(url, store, first, last, email='ta@example.edu', **extra):
    from omr.workflows.ta_packets import ta_student_catalog
    state = store.read_state('TA_TEST_run')
    _, revision = ta_student_catalog(state['parse_dir'])
    post(url + '/runs/TA_TEST_run/ta/assign-range', dict(q_no='1', range_start=str(first),
         range_end=str(last), ta_email=email, catalog_revision=revision, **extra))
    return store.read_state('TA_TEST_run')['ta_range_assignments'][-1]


def generate_range(url, store, identifier):
    post(url + '/runs/TA_TEST_run/ta/generate', {'assignment_id': identifier})
    store._worker.join(timeout=15)
    assert not store._worker.is_alive()
    state = store.read_state('TA_TEST_run')
    assert state['ta_job']['status'] == 'complete'
    return state['ta_output']


def test_range_assignment_snapshot_incremental_exports_and_no_regeneration(packet_run, tmp_path):
    store, parsed = configured(packet_run, tmp_path)
    owned = (parsed / 'verified_index.json').read_bytes()
    with server(store) as url:
        first = add_range(url, store, 1, 2, 'one@example.edu')
        first_output = generate_range(url, store, first['assignment_id'])
        first_path = parsed / 'ta_packets' / first_output / 'index.json'
        first_bytes = first_path.read_bytes()
        first_index = json.loads(first_bytes)
        assert len(first_index['packets']) == 1
        assert [r['roll_no'] for r in first_index['packets'][0]['rows']] == ['2024001', '2024002']
        assert [r['student_position'] for r in first_index['packets'][0]['rows']] == [1, 2]
        second = add_range(url, store, 3, 4, 'two@example.edu')
        post(url + '/runs/TA_TEST_run/ta/generate', {'pending_ranges': '1'})
        store._worker.join(timeout=15)
        second_output = store.read_state('TA_TEST_run')['ta_output']
        second_index = json.loads((parsed / 'ta_packets' / second_output / 'index.json').read_bytes())
        assert len(second_index['packets']) == 1
        assert [r['roll_no'] for r in second_index['packets'][0]['rows']] == ['2024003', '2024004']
        assert first_path.read_bytes() == first_bytes
        with pytest.raises(urllib.error.HTTPError) as error:
            post(url + '/runs/TA_TEST_run/ta/generate', {'assignment_id': first['assignment_id']})
        assert b'No pending answers' in error.value.read()
        body, _ = get(url + '/runs/TA_TEST_run/ta?output=' + second_output)
        assert b'Students 3-4' in body and b'two@example.edu' in body
        with pytest.raises(urllib.error.HTTPError):
            post(url + '/runs/TA_TEST_run/ta/remove-range', {'assignment_id': second['assignment_id']})
    assert (parsed / 'verified_index.json').read_bytes() == owned
    restarted = app.RunStore(store.config)
    assert len(restarted.read_state('TA_TEST_run')['ta_range_assignments']) == 2


def test_range_conflicts_stale_catalog_and_removal(packet_run, tmp_path):
    store, parsed = configured(packet_run, tmp_path)
    with server(store) as url:
        assignment = add_range(url, store, 1, 3)
        with pytest.raises(urllib.error.HTTPError) as error:
            add_range(url, store, 3, 4, 'other@example.edu')
        assert b'overlaps' in error.value.read()
        shared = add_range(url, store, 3, 4, 'other@example.edu', additional_marker='on')
        assert shared['additional_marker'] is True
        with pytest.raises(urllib.error.HTTPError) as error:
            add_range(url, store, 2, 2, additional_marker='on')
        assert b'already has students' in error.value.read()
        with pytest.raises(urllib.error.HTTPError) as error:
            post(url + '/runs/TA_TEST_run/ta/assign-range', dict(q_no='1', range_start='1', range_end='2',
                                                               ta_email='last@example.edu', catalog_revision='stale'))
        assert b'Student list changed' in error.value.read()
        post(url + '/runs/TA_TEST_run/ta/remove-range', {'assignment_id': assignment['assignment_id']})
        assert len(store.read_state('TA_TEST_run')['ta_range_assignments']) == 1


def test_held_answers_can_be_generated_later_without_reexporting_old_answers(packet_run, tmp_path):
    store, parsed = configured(packet_run, tmp_path)
    with server(store) as url:
        assignment = add_range(url, store, 4, 5)
        output = generate_range(url, store, assignment['assignment_id'])
        before = (parsed / 'ta_packets' / output / 'index.json').read_bytes()
        plan = store.read_state('TA_TEST_run')['ta_range_assignments'][0]
        assert plan['generated_rolls'] == ['2024004']
        verified = json.loads((parsed / 'verified_index.json').read_bytes())
        verified['students'][-1].update(status='verified', pages=[dict(page=1, source_index=5,
                                                                     canonical_image_path=str(parsed / 'page.png'))])
        (parsed / 'verified_index.json').write_text(json.dumps(verified))
        later = generate_range(url, store, assignment['assignment_id'])
        index = json.loads((parsed / 'ta_packets' / later / 'index.json').read_bytes())
        assert [r['roll_no'] for r in index['packets'][0]['rows']] == ['2024999']
        assert (parsed / 'ta_packets' / output / 'index.json').read_bytes() == before
        assert store.read_state('TA_TEST_run')['ta_range_assignments'][0]['generated_rolls'] == ['2024004', '2024999']


def test_stale_packet_offers_explicit_regeneration_without_changing_original(packet_run, tmp_path):
    store, parsed = configured(packet_run, tmp_path)
    with server(store) as url:
        assignment = add_range(url, store, 1, 2)
        output = generate_range(url, store, assignment['assignment_id'])
        old_file = parsed / 'ta_packets' / output / 'index.json'
        original = old_file.read_bytes()
        verified = json.loads((parsed / 'verified_index.json').read_bytes())
        verified['students'][0]['pages'][0]['source_index'] = 77
        (parsed / 'verified_index.json').write_text(json.dumps(verified))
        body, _ = get(url + '/runs/TA_TEST_run/ta')
        assert b'Regenerate Packet' in body and b'Ownership changed' in body
        post(url + '/runs/TA_TEST_run/ta/generate', {'assignment_id': assignment['assignment_id'], 'regenerate': '1'})
        store._worker.join(timeout=15)
        state = store.read_state('TA_TEST_run')
        assert state['ta_job']['status'] == 'complete'
        assert state['ta_output'] != output
        assert old_file.read_bytes() == original
        with pytest.raises(urllib.error.HTTPError) as error:
            post(url + '/runs/TA_TEST_run/ta/generate', {'assignment_id': assignment['assignment_id'], 'regenerate': '1'})
        assert b'Packet is current' in error.value.read()


def test_gmail_send_error_returns_workspace_without_echoing_or_saving_password(packet_run, tmp_path, monkeypatch):
    store, parsed = configured(packet_run, tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid Gmail password reached the network')
    monkeypatch.setattr('omr.workflows.ta_packets.smtplib.SMTP', forbidden)
    with server(store) as url:
        output = generate(url, store)
        post_data = dict(output=output, packet='Q1_TA1_batch1', delivery='test', test_recipient='check@example.edu',
                         confirm='on', sender='prof@example.edu', host='smtp.gmail.com', port='587',
                         username='prof@example.edu', password='ordinary-secret')
        with pytest.raises(urllib.error.HTTPError) as error:
            post(url + '/runs/TA_TEST_run/ta/send', post_data)
        html = error.value.read()
        assert b'Assign Question Range' in html and b'role="alert"' in html
        assert b'16-character Google app password' in html
        assert b'value="smtp.gmail.com"' in html and b'value="prof@example.edu"' in html
        assert b'ordinary-secret' not in html
        assert b'name="password" type="password" autocomplete="off" required' in html
    assert 'ordinary-secret' not in store.state_path('TA_TEST_run').read_text()
    assert 'ordinary-secret' not in (parsed / 'ta_packets' / output / 'index.json').read_text()


def test_crop_layout_regeneration_preserves_original_export(packet_run, tmp_path):
    from omr.reader.written import WRITTEN_CROP_LAYOUT_VERSION
    store, parsed = configured(packet_run, tmp_path)
    with server(store) as url:
        assignment = add_range(url, store, 1, 2)
        output = generate_range(url, store, assignment['assignment_id'])
        old = parsed / 'ta_packets' / output / 'index.json'
        index = json.loads(old.read_bytes())
        index.pop('crop_layout_version')
        old.write_text(json.dumps(index))
        original = old.read_bytes()
        body,_ = get(url + '/runs/TA_TEST_run/ta')
        assert b'Crop layout updated' in body and b'Regenerate Packet' in body
        post(url + '/runs/TA_TEST_run/ta/generate', {'assignment_id':assignment['assignment_id'],'regenerate':'1'})
        store._worker.join(timeout=15)
        state = store.read_state('TA_TEST_run')
        assert state['ta_job']['status']=='complete'
        assert state['ta_output']!=output
        assert old.read_bytes()==original
        latest=json.loads((parsed / 'ta_packets' / state['ta_output'] / 'index.json').read_bytes())
        assert latest['crop_layout_version']==WRITTEN_CROP_LAYOUT_VERSION


def preview_email(base, fields):
    import urllib.parse
    request = urllib.request.Request(base + '/preview-email',
        data=urllib.parse.urlencode(fields).encode(),
        headers={'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'})
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def test_email_template_save_preview_and_restart_without_mail(packet_run, tmp_path, monkeypatch):
    store, parsed = configured(packet_run, tmp_path)
    monkeypatch.setattr(app, 'send_ta_packet', lambda *args, **kwargs: pytest.fail('Preview/save sent an email'))
    with server(store) as url:
        output = generate(url, store)
        base = url + '/runs/TA_TEST_run/ta'
        path = parsed / 'ta_packets' / output / 'index.json'
        before = path.read_bytes()
        fields = dict(output=output, packet='Q1_TA1_batch1', subject_template='{exam_id} - Q{question_number}',
                      message_template='Hello {ta_name},\nPlease return {answer_count} marks by Friday.')
        post(base + '/save-template', {**fields, 'password': 'must-not-be-stored'})
        saved = store.read_state('TA_TEST_run')['ta_mail_template']
        assert saved == {key: fields[key] for key in ('subject_template', 'message_template')}
        assert 'must-not-be-stored' not in store.state_path('TA_TEST_run').read_text()
        body, _ = get(base)
        assert b'Save Template' in body and b'Preview Email' in body and b'/static/ta_email_editor.js' in body
        assert b'Please return {answer_count} marks by Friday.' in body
        preview = preview_email(base, dict(output=output, packet=fields['packet'], delivery='test',
                                           test_recipient='test@example.edu'))
        assert preview['subject'] == 'TA_TEST - Q1'
        assert preview['message'] == 'Hello ta,\nPlease return 2 marks by Friday.'
        assert preview['recipient'] == 'test@example.edu'
        assert preview_email(base, dict(output=output, packet=fields['packet'], delivery='real'))['recipient'] == 'ta@example.edu'
        assert path.read_bytes() == before
    assert app.RunStore(store.config).read_state('TA_TEST_run')['ta_mail_template'] == saved


def test_invalid_email_template_preserves_safe_draft_and_blocks_send(packet_run, tmp_path, monkeypatch):
    store, _ = configured(packet_run, tmp_path)
    calls = []
    monkeypatch.setattr(app, 'send_ta_packet', lambda *args, **kwargs: calls.append(kwargs))
    with server(store) as url:
        output = generate(url, store)
        base = url + '/runs/TA_TEST_run/ta'
        fields = dict(output=output, packet='all', subject_template='Q{question_number}',
                      message_template='</textarea><script>alert(1)</script> {password}',
                      confirm='on', delivery='test', test_recipient='test@example.edu', password='hidden-secret')
        for action in ('save-template', 'send'):
            with pytest.raises(urllib.error.HTTPError) as error:
                post(base + '/' + action, fields)
            body = error.value.read()
            assert b'Unsupported email field' in body
            assert b'&lt;/textarea&gt;&lt;script&gt;alert(1)&lt;/script&gt;' in body
            assert b'hidden-secret' not in body
        with pytest.raises(urllib.error.HTTPError) as error:
            preview_email(base, dict(output=output, packet='Q1_TA1_batch1', preview_packet='Q1_TA1_batch2'))
        assert b'Choose a packet included' in error.value.read()
    assert not calls and 'ta_mail_template' not in store.read_state('TA_TEST_run')


def test_saved_template_is_used_for_bulk_send_and_all_messages_are_preflighted(packet_run, tmp_path, monkeypatch):
    store, parsed = configured(packet_run, tmp_path)
    calls = []
    monkeypatch.setattr(app, 'send_ta_packet', lambda *args, **kwargs: calls.append(kwargs))
    with server(store) as url:
        output = generate(url, store)
        base = url + '/runs/TA_TEST_run/ta'
        fields = dict(output=output, packet='all', subject_template='{ta_name} Q{question_number}', message_template='Due Friday')
        post(base + '/save-template', fields)
        send_fields = dict(output=output, packet='all', confirm='on', delivery='real', port='587')
        index_path = parsed / 'ta_packets' / output / 'index.json'
        index = json.loads(index_path.read_bytes())
        index['packets'][1]['ta']['name'] = 'TA\nInjected'
        index_path.write_text(json.dumps(index))
        with pytest.raises(urllib.error.HTTPError):
            post(base + '/send', send_fields)
        assert not calls
        index['packets'][1]['ta']['name'] = 'Second TA'
        index_path.write_text(json.dumps(index))
        post(base + '/send', send_fields)
    assert len(calls) == 2
    assert all(c['subject_template'] == fields['subject_template'] and c['message_template'] == 'Due Friday' for c in calls)
