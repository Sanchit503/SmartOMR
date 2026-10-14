"""Local TA packet workspace, usable while the main UI merge is unresolved."""
from __future__ import annotations

import argparse
import html
import json
import threading
import tempfile
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from omr.workflows.ta_packets import generate_ta_packets, import_ta_marks, send_ta_packet


class TaWorkspace(BaseHTTPRequestHandler):
    data_dir = Path('data')
    outputs = {}
    job = {'running': False, 'message': ''}
    lock = threading.Lock()

    def _runs(self):
        runs = {}
        for file in (self.data_dir / 'ui_runs').glob('*/run_state.json'):
            state = json.loads(file.read_text())
            if state.get('parse_dir') and Path(state['parse_dir']).is_dir():
                runs[state['run_id']] = state
        return runs

    def _send(self, body, status=200):
        content = body.encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _redirect(self, url):
        self.send_response(303)
        self.send_header('Location', url)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        if parsed.path == '/download':
            key = query.get('output', [''])[0]
            file = query.get('file', [''])[0]
            root = self.outputs.get(key)
            path = (root / file).resolve() if root else None
            if path is None or not path.is_relative_to(root.resolve()) or not path.is_file():
                self._send('File not found', 404)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Disposition', f'attachment; filename="{path.name}"')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        runs = self._runs()
        chosen = query.get('run', [next(iter(runs), '')])[0]
        state = runs.get(chosen)
        options = ''.join(f'<option value="{html.escape(key)}" {"selected" if key == chosen else ""}>{html.escape(s["exam_id"])} / {html.escape(key)}</option>' for key, s in runs.items())
        questions = []
        if state:
            manifest = json.loads(Path(state['inputs']['manifest_path']).read_text())
            questions = manifest.get('written_block', [])
            for directory in (Path(state['parse_dir']) / 'ta_packets').glob('*'):
                if (directory / 'index.json').exists():
                    self.outputs[directory.name] = directory.resolve()
        rows = ''.join(f'<tr><td>Q{q["q_no"]}</td><td>{q["max_marks"]}</td><td><input type="email" multiple name="emails_q{q["q_no"]}" placeholder="ta1@example.edu, ta2@example.edu"></td><td><select name="mode_q{q["q_no"]}"><option value="split">Split students</option><option value="double">Double marking</option></select></td></tr>' for q in questions)
        packets = ''
        selected_output = query.get('output', [''])[0]
        if not selected_output and self.outputs:
            selected_output = next(reversed(self.outputs))
        output = self.outputs.get(selected_output)
        if output:
            index = json.loads((output / 'index.json').read_text())
            if state and index['exam_id'] == state['exam_id']:
                packets = f'<h2>Packets</h2><p>{len(index["packets"])} packets · {len(index["skipped"])} held-back question answers</p><a href="/download?output={selected_output}&file=skipped.csv">Missing / unresolved report</a>'
                if (output / 'written_marks.csv').exists():
                    packets += f' · <a href="/download?output={selected_output}&file=written_marks.csv">Written marks CSV</a>'
                    packets += f' · <a href="/download?output={selected_output}&file=final_scores.csv">Final scores CSV</a>'
                packets += f'''<section><h3>Email All Pending Packets</h3><form method="post" action="/send"><input type="hidden" name="output" value="{selected_output}"><input type="hidden" name="packet" value="all"><div class="grid"><label>Sender email<input name="sender" type="email" required></label><label>SMTP host<input name="host" required></label><label>Port<input name="port" type="number" required></label><label>Security<select name="security"><option value="starttls">STARTTLS</option><option value="ssl">SSL</option></select></label><label>SMTP username<input name="username" required></label><label>App password<input name="password" type="password" required autocomplete="off"></label><label>Test recipient (optional)<input name="test_recipient" type="email"></label></div><button>Send All Pending Packets</button></form></section>'''
                for p in index['packets']:
                    file = urllib.parse.quote(str(Path(p['archive']).relative_to(output)))
                    packets += f'''<section><h3>{html.escape(p['id'])}</h3><p>{html.escape(p['ta']['email'])} · {len(p['rows'])} answers · {p['mode']} · Email: {p['mail_status']}</p>
                    <a href="/download?output={selected_output}&file={file}">Download ZIP</a>
                    <form method="post" action="/send"><input type="hidden" name="output" value="{selected_output}"><input type="hidden" name="packet" value="{p['id']}">
                    <div class="grid"><label>Sender email<input name="sender" type="email" required></label><label>SMTP host<input name="host" required></label><label>Port<input name="port" type="number" required></label><label>Security<select name="security"><option value="starttls">STARTTLS</option><option value="ssl">SSL</option></select></label><label>SMTP username<input name="username" required></label><label>App password<input name="password" type="password" required autocomplete="off"></label><label>Test recipient (optional)<input name="test_recipient" type="email"></label></div><button>Send This Packet</button></form>
                    <form method="post" action="/import"><input type="hidden" name="output" value="{selected_output}"><input type="hidden" name="packet" value="{p['id']}"><label>Returned marking CSV<input type="file" accept=".csv" required onchange="this.files[0].text().then(text => this.form.elements.csv_text.value=text)"></label><input type="hidden" name="csv_text"><button>Import Marks</button></form></section>'''
        refresh = '<meta http-equiv="refresh" content="4">' if self.job['running'] else ''
        self._send(f'''<!doctype html><html><head><title>SmartOMR TA Workspace</title>{refresh}<style>
        *{{box-sizing:border-box}}body{{margin:0;font:15px Arial;color:#20252b;background:#f4f6f7}}header{{background:white;border-bottom:1px solid #dce1e5;padding:18px 28px}}main{{max-width:1200px;margin:auto;padding:24px}}h1{{font-size:26px}}h2{{font-size:21px}}h3{{font-size:17px}}table{{width:100%;border-collapse:collapse;background:white}}td,th{{padding:12px;text-align:left;border-bottom:1px solid #dde3e6}}input,select{{width:100%;padding:10px;border:1px solid #bdc6cb;border-radius:4px}}button{{padding:11px 16px;background:#176a58;color:white;border:0;border-radius:4px;margin-top:12px;cursor:pointer}}section{{padding:18px 0;border-bottom:1px solid #cad1d6}}label{{display:block;margin-top:12px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}}a{{color:#166799}}.status{{padding:12px;background:#e5efe9;overflow-wrap:anywhere}}@media(max-width:650px){{main{{padding:12px}}table{{display:block;overflow:auto}}}}
        </style></head><body><header><strong>SmartOMR</strong> · TA Workspace</header><main><h1>Written Question Marking</h1>
        <p class="status">{html.escape(self.job['message'] or 'Ready')}</p><form method="get"><label>Exam run<select name="run">{options}</select></label><button>Select Run</button></form>
        <form method="post" action="/generate"><input type="hidden" name="run" value="{html.escape(chosen)}"><h2>Question Assignments</h2><table><thead><tr><th>Question</th><th>Max marks</th><th>TA email addresses</th><th>Marking mode</th></tr></thead><tbody>{rows or '<tr><td colspan="4">No written questions in this run</td></tr>'}</tbody></table><label>Maximum students per packet<input name="batch_size" type="number" min="1" value="100" required></label><button {'disabled' if self.job['running'] or not questions else ''}>Generate TA Packets</button></form>{packets}</main></body></html>''')

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        if length > 1024 * 1024:
            self._send('Form too large', 400)
            return
        form = {key: values[0] for key, values in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}
        try:
            if self.path == '/generate':
                state = self._runs()[form['run']]
                manifest = json.loads(Path(state['inputs']['manifest_path']).read_text())
                assignments = []
                for q in manifest['written_block']:
                    emails = [email.strip() for email in form.get(f'emails_q{q["q_no"]}', '').split(',') if email.strip()]
                    if emails:
                        assignments.append(dict(q_no=q['q_no'], mode=form.get(f'mode_q{q["q_no"]}', 'split'), tas=[dict(name=email.split('@')[0], email=email) for email in emails]))
                with self.lock:
                    if self.job['running']:
                        raise ValueError('An operation is already running')
                    self.job.update(running=True, message='Generating TA packets')
                def work():
                    try:
                        output = generate_ta_packets(state['parse_dir'], state['inputs']['manifest_path'], assignments,
                            batch_size=int(form['batch_size']), progress=lambda done, total: self.job.update(message=f'Exporting student {done}/{total}'))
                        self.outputs[output.name] = output
                        self.job['message'] = 'Packets ready'
                    except Exception as exc:
                        self.job['message'] = f'Export failed: {exc}'
                    finally:
                        self.job['running'] = False
                threading.Thread(target=work, daemon=True).start()
                self._redirect('/?run=' + urllib.parse.quote(form['run']))
            else:
                output = self.outputs[form['output']]
                with self.lock:
                    if self.job['running']:
                        raise ValueError('Wait for packet generation to finish')
                    if self.path == '/send':
                        index = json.loads((output / 'index.json').read_text())
                        packet_ids = [p['id'] for p in index['packets'] if p['mail_status'] != 'sent'] if form['packet'] == 'all' else [form['packet']]
                        for packet_id in packet_ids:
                            send_ta_packet(output, packet_id, sender=form['sender'], host=form['host'],
                                port=int(form['port']), security=form['security'], username=form['username'],
                                password=form['password'], test_recipient=form.get('test_recipient'))
                        self.job['message'] = f'{len(packet_ids)} packet emails sent'
                    elif self.path == '/import':
                        if not form.get('csv_text'):
                            raise ValueError('Select a CSV and wait for it to load before importing')
                        with tempfile.TemporaryDirectory() as directory:
                            csv_path = Path(directory) / 'returned.csv'
                            csv_path.write_text(form['csv_text'], encoding='utf-8')
                            count = import_ta_marks(output, form['packet'], csv_path)
                        self.job['message'] = f'Imported {count} marks'
                    else:
                        raise ValueError('Unknown operation')
                self._redirect('/?output=' + form['output'])
        except Exception as exc:
            self._send(f'<h1>Operation failed</h1><p>{html.escape(str(exc))}</p><a href="/">Back to TA workspace</a>', 400)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--data-dir', type=Path, default=Path('data'))
    args = parser.parse_args()
    TaWorkspace.data_dir = args.data_dir
    server = ThreadingHTTPServer(('127.0.0.1', args.port), TaWorkspace)
    print(f'TA Workspace running at http://127.0.0.1:{args.port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
