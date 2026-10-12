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
        "ding.deliver_message = lambda *args: {'outcome': 'started', 'error': None}\n"
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
    assert len(records) == 20
    assert {record["message"] for record in records} == {
        f"{name}:{index}\n" + "界" * 16000 for name in ["first", "second"] for index in range(5)
    }
    intents = {record["client_message_id"] for record in records if record["event"] == "send_intent"}
    results = [record for record in records if record["event"] == "send_result"]
    assert len(intents) == len(results) == 10
    assert {record["client_message_id"] for record in results} == intents
    assert all(record["outcome"] == "started" for record in results)
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600


@pytest.fixture(autouse=True)
def isolated_run_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def test_concurrent_run_registration_is_private_and_preserves_all_invocations(tmp_path):
    script = (
        "import ding, os, sys\n"
        "from pathlib import Path\n"
        "os.umask(0)\n"
        "for index in range(5):\n"
        "    with ding.registered_run('00000000-0000-4000-8000-000000000001', Path(sys.argv[1])): pass\n"
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda index: subprocess.run(
            [sys.executable, "-c", script, str(tmp_path / f"{index}.jsonl")],
            capture_output=True, text=True, timeout=15), range(3)))
    assert all(result.returncode == 0 for result in results), [result.stderr for result in results]
    registry = ding.run_registry()
    events = [json.loads(line) for line in registry.read_text().splitlines()]
    assert len(events) == len({event["run_id"] for event in events}) == 15
    assert all(event["event"] == "run_start" and event["pid"] > 0 and event["started_ns"] > 0
               and os.path.isabs(event["record"]) and os.path.isabs(event["cwd"]) for event in events)
    assert stat.S_IMODE(registry.stat().st_mode) == 0o600
    assert stat.S_IMODE(registry.parent.stat().st_mode) == 0o700
    tokens = list((registry.parent / "run-locks").glob("*.lock"))
    assert len(tokens) == 15
    assert all(stat.S_IMODE(token.stat().st_mode) == 0o600 for token in tokens)
    latest, = ding.latest_runs().values()
    assert latest == events[-1]
    assert ding.run_state(latest) == "lost"


def test_run_lock_distinguishes_exact_invocation_and_releases_on_death(tmp_path):
    script = (
        "import ding, sys\n"
        "from pathlib import Path\n"
        "with ding.registered_run('00000000-0000-4000-8000-000000000001', Path(sys.argv[1])):\n"
        "    print('ready', flush=True)\n"
        "    sys.stdin.read()\n"
    )
    child = subprocess.Popen([sys.executable, "-c", script, str(tmp_path / "missing.jsonl")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline() == "ready\n"
        run, = ding.latest_runs().values()
        run["pid"] = os.getpid()
        assert ding.run_state(run) == "running"
        child.kill()
        child.wait(timeout=5)
        assert ding.run_state(run) == "lost"
        assert (ding.run_registry().parent / "run-locks" / f"{run['run_id']}.lock").exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()


@pytest.mark.parametrize("outcome,expected", [
    ("started", "exited(7)"), ("queued_behind_active", "exited(7)"),
    ("skipped_active", "exited(7)"), ("queue_failed", "notify_failed"),
    ("failed", "notify_failed"), ("queued", "notify_failed"), ("unknown", "notify_failed"),
])
def test_run_state_matches_receipts_by_invocation(tmp_path, outcome, expected):
    thread_id = "00000000-0000-4000-8000-000000000001"
    record = tmp_path / "exit.jsonl"
    with ding.registered_run(thread_id, record) as run_id:
        run = ding.latest_runs()[thread_id]
        ding.append_jsonl(record, {"event": "notification", "run_id": "older", "outcome": "started"})
        assert ding.run_state(run) == "running"
        ding.append_jsonl(record, {"event": "task_exit", "run_id": run_id,
                                  "thread_id": thread_id, "exit_code": 7})
        assert ding.run_state(run) == "exited(7)"
        ding.append_jsonl(record, {"event": "notification", "run_id": run_id,
                                  "thread_id": thread_id, "outcome": outcome})
        assert ding.run_state(run) == expected
    assert ding.run_state(run) == expected


def test_dead_run_without_notification_is_failed_and_missing_token_is_dead(tmp_path):
    thread_id = "00000000-0000-4000-8000-000000000001"
    record = tmp_path / "exit.jsonl"
    with ding.registered_run(thread_id, record) as run_id:
        run = ding.latest_runs()[thread_id]
        ding.append_jsonl(record, {"event": "task_exit", "run_id": run_id,
                                  "thread_id": thread_id, "exit_code": 0})
        assert ding.run_state(run) == "exited(0)"
    assert ding.run_state(run) == "notify_failed"
    (ding.run_registry().parent / "run-locks" / f"{run_id}.lock").unlink()
    assert ding.run_state(run) == "notify_failed"
    record.unlink()
    assert ding.run_state(run) == "lost"
    assert not (ding.run_registry().parent / "run-locks" / f"{run_id}.lock").exists()


@pytest.mark.parametrize("evidence", ['{"event":', '{}\n', '[]\n', 'bad\n'])
def test_run_corrupt_evidence_is_unknown_except_incomplete_tail(tmp_path, capsys, evidence):
    thread_id = "00000000-0000-4000-8000-000000000001"
    record = tmp_path / "exit.jsonl"
    with ding.registered_run(thread_id, record):
        run = ding.latest_runs()[thread_id]
    record.write_text(evidence)
    expected = "lost" if evidence == '{"event":' else "unknown"
    assert ding.run_state(run) == expected
    assert capsys.readouterr().err


def test_completed_receipts_survive_partial_tail_and_unreadable_record_warns(tmp_path, capsys):
    thread_id = "00000000-0000-4000-8000-000000000001"
    record = tmp_path / "exit.jsonl"
    with ding.registered_run(thread_id, record) as run_id:
        run = ding.latest_runs()[thread_id]
        ding.append_jsonl(record, {"event": "task_exit", "run_id": run_id,
                                  "thread_id": thread_id, "exit_code": 0})
        ding.append_jsonl(record, {"event": "notification", "run_id": run_id,
                                  "thread_id": thread_id, "outcome": "started"})
    with record.open("a") as handle:
        handle.write('{"event":')
    assert ding.run_state(run) == "exited(0)"
    assert "incomplete run record tail" in capsys.readouterr().err
    record.unlink()
    record.mkdir()
    assert ding.run_state(run) == "unknown"
    assert "run evidence unavailable" in capsys.readouterr().err


def test_registry_skips_invalid_rows_and_uses_append_order_even_if_clock_rolls_back(tmp_path, capsys):
    thread_id = "00000000-0000-4000-8000-000000000001"
    with ding.registered_run(thread_id, tmp_path / "old.jsonl"):
        old = ding.latest_runs()[thread_id]
    with ding.registered_run(thread_id, tmp_path / "new.jsonl"):
        new = ding.latest_runs()[thread_id]
    new["started_ns"] = 1
    registry = ding.run_registry()
    registry.write_text("bad\n" + json.dumps(old) + "\n" + json.dumps(new) + '\n{"event":')
    assert ding.latest_runs() == {thread_id: new}
    assert capsys.readouterr().err.count("invalid run registry row") == 2
    registry.unlink()
    registry.mkdir()
    assert ding.latest_runs() == {}
    assert "run registry unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("tail", [b'{"event":', b'{"event":"\xe7'])
def test_registration_preserves_broken_tail_and_appends_readable_new_run(tmp_path, capsys, tail):
    thread_id = "00000000-0000-4000-8000-000000000001"
    with ding.registered_run(thread_id, tmp_path / "old.jsonl"):
        old = ding.latest_runs()[thread_id]
    registry = ding.run_registry()
    prefix = registry.read_bytes() + tail
    registry.write_bytes(prefix)
    with ding.registered_run(thread_id, tmp_path / "new.jsonl") as run_id:
        new = ding.latest_runs()[thread_id]
        assert new["run_id"] == run_id
        assert new["record"] == str(tmp_path / "new.jsonl")
    assert registry.read_bytes().startswith(prefix + b"\n")
    assert json.loads(registry.read_bytes().splitlines()[0]) == old
    assert "invalid run registry row 2" in capsys.readouterr().err


def test_registry_preserves_complete_rows_before_incomplete_utf8_tail(tmp_path, capsys):
    thread_id = "00000000-0000-4000-8000-000000000001"
    with ding.registered_run(thread_id, tmp_path / "record.jsonl"):
        run = ding.latest_runs()[thread_id]
    with ding.run_registry().open("ab") as handle:
        handle.write(b'{"event":"\xe7')
    assert ding.latest_runs() == {thread_id: run}
    assert "invalid run registry row 2" in capsys.readouterr().err


@pytest.mark.parametrize("tail,expected", [(b'{"event":"\xe7', "exited(0)"),
                                            (b'{"event":"\xe7\n', "unknown")])
def test_record_preserves_receipts_before_incomplete_utf8_but_rejects_corrupt_complete_row(tmp_path, capsys, tail, expected):
    thread_id = "00000000-0000-4000-8000-000000000001"
    record = tmp_path / "record.jsonl"
    with ding.registered_run(thread_id, record) as run_id:
        run = ding.latest_runs()[thread_id]
        ding.append_jsonl(record, {"event": "task_exit", "run_id": run_id,
                                  "thread_id": thread_id, "exit_code": 0})
        ding.append_jsonl(record, {"event": "notification", "run_id": run_id,
                                  "thread_id": thread_id, "outcome": "started"})
    with record.open("ab") as handle:
        handle.write(tail)
    assert ding.run_state(run) == expected
    assert capsys.readouterr().err


@pytest.mark.parametrize("outcome", [[], {}, None, 1])
def test_run_malformed_notification_outcome_is_unknown_with_warning(tmp_path, capsys, outcome):
    thread_id = "00000000-0000-4000-8000-000000000001"
    record = tmp_path / "record.jsonl"
    with ding.registered_run(thread_id, record) as run_id:
        run = ding.latest_runs()[thread_id]
        ding.append_jsonl(record, {"event": "task_exit", "run_id": run_id,
                                  "thread_id": thread_id, "exit_code": 0})
        ding.append_jsonl(record, {"event": "notification", "run_id": run_id,
                                  "thread_id": thread_id, "outcome": outcome})
    assert ding.run_state(run) == "unknown"
    assert "invalid notification outcome" in capsys.readouterr().err
