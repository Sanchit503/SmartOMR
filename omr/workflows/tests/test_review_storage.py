import json

import pytest

from omr.workflows import review


def test_atomic_review_write_retries_transient_windows_lock(tmp_path, monkeypatch):
    path = tmp_path / "verified_index.json"
    review._write_json(path, {"value": "old"})
    replace = review.os.replace
    calls = []
    def briefly_locked(source, target):
        calls.append(target)
        if len(calls) == 1:
            raise PermissionError("locked by reader")
        replace(source, target)
    monkeypatch.setattr(review.os, "replace", briefly_locked)
    monkeypatch.setattr(review.time, "sleep", lambda _: None)
    review._write_json(path, {"value": "new"})
    assert json.loads(path.read_text()) == {"value": "new"}
    assert len(calls) == 2
    assert list(tmp_path.iterdir()) == [path]


def test_failed_atomic_review_write_preserves_existing_decisions(tmp_path, monkeypatch):
    path = tmp_path / "verified_index.json"
    review._write_json(path, {"decision": "verified"})
    before = path.read_bytes()
    def locked(*args):
        raise PermissionError("still locked")
    monkeypatch.setattr(review.os, "replace", locked)
    monkeypatch.setattr(review.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError):
        review._write_json(path, {"decision": "different"})
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]
