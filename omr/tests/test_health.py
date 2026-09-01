from __future__ import annotations

from omr.health import main, run_checks


def test_health_check_reports_ready_for_runtime_dependencies(tmp_path):
    payload = run_checks(tmp_path)

    assert payload["service"] == "smartomr"
    assert payload["status"] == "ready", payload
    assert all(check["status"] == "ready" for check in payload["checks"])


def test_health_cli_returns_zero_when_ready(tmp_path, capsys):
    exit_code = main(["--data-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert '"status": "ready"' in captured.out
