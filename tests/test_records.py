import json
import os
import stat
import subprocess
import sys

import pytest

import codex_session


def test_writer_excludes_other_process_and_releases_after_error(tmp_path):
    path = tmp_path / "nested" / "exit.jsonl"
    script = "import sys; from pathlib import Path; from codex_session import writer;\nwith writer(Path(sys.argv[1])): pass"
    with pytest.raises(ValueError, match="task error"):
        with codex_session.writer(path):
            result = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True)
            assert result.returncode != 0
            assert "another writer is active" in result.stderr
            raise ValueError("task error")
    result = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_append_preserves_records_and_syncs_file_then_directory(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "exit.jsonl"
    calls = []
    fsync = os.fsync

    def sync(descriptor):
        calls.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        fsync(descriptor)

    monkeypatch.setattr(codex_session.os, "fsync", sync)
    with codex_session.writer(path):
        codex_session.append_jsonl(path, {"text": "完成"})
        prefix = path.read_bytes()
        codex_session.append_jsonl(path, {"exit_code": 7})
    assert path.read_bytes().startswith(prefix)
    assert [json.loads(line) for line in path.read_text().splitlines()] == [{"text": "完成"}, {"exit_code": 7}]
    assert calls == ["file", "directory", "file", "directory"]


def test_directory_descriptor_closed_when_sync_fails(tmp_path, monkeypatch):
    descriptor = None

    def fail(fd):
        nonlocal descriptor
        descriptor = fd
        raise OSError("sync failed")

    monkeypatch.setattr(codex_session.os, "fsync", fail)
    with pytest.raises(OSError, match="sync failed"):
        codex_session._fsync_directory(tmp_path)
    assert descriptor is not None
    with pytest.raises(OSError):
        os.fstat(descriptor)
