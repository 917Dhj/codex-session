import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

import ding


def test_writer_excludes_other_process_and_releases_after_error(tmp_path):
    path = tmp_path / "nested" / "exit.jsonl"
    script = "import sys; from pathlib import Path; from ding import writer;\nwith writer(Path(sys.argv[1])): pass"
    with pytest.raises(ValueError, match="task error"):
        with ding.writer(path):
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

    monkeypatch.setattr(ding.os, "fsync", sync)
    with ding.writer(path):
        ding.append_jsonl(path, {"text": "完成"})
        prefix = path.read_bytes()
        ding.append_jsonl(path, {"exit_code": 7})
    assert path.read_bytes().startswith(prefix)
    assert [json.loads(line) for line in path.read_text().splitlines()] == [{"text": "完成"}, {"exit_code": 7}]
    assert calls == ["file", "directory", "file", "directory"]


def test_directory_descriptor_closed_when_sync_fails(tmp_path, monkeypatch):
    descriptor = None

    def fail(fd):
        nonlocal descriptor
        descriptor = fd
        raise OSError("sync failed")

    monkeypatch.setattr(ding.os, "fsync", fail)
    with pytest.raises(OSError, match="sync failed"):
        ding._fsync_directory(tmp_path)
    assert descriptor is not None
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_concurrent_sends_append_complete_private_records(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    script = (
        "import ding, os, sys\n"
        "os.umask(0)\n"
        "ding.queue_message = lambda *args: 0\n"
        "for index in range(5):\n"
        "    message = sys.argv[1] + ':' + str(index) + '\\n' + '界' * 16000\n"
        "    assert ding.main(['send', '00000000-0000-4000-8000-000000000001', "
        "'--socket', '/fake.sock', '--message', message]) == 0\n"
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda name: subprocess.run(
            [sys.executable, "-c", script, name], capture_output=True, text=True, timeout=15),
            ["first", "second"]))
    assert all(result.returncode == 0 for result in results), [result.stderr for result in results]
    journal = tmp_path / "state/ding/sent.jsonl"
    records = [json.loads(line) for line in journal.read_text().splitlines()]
    assert len(records) == 10
    assert {record["message"] for record in records} == {
        f"{name}:{index}\n" + "界" * 16000 for name in ["first", "second"] for index in range(5)
    }
    assert all(record["outcome"] == "queued" for record in records)
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
