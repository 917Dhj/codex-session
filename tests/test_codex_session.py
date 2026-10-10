import builtins
import json
import socket

import pytest

import codex_session


THREAD_ID = "00000000-0000-4000-8000-000000000001"


class Connection:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []
        self.closed = False

    def close(self):
        self.closed = True

    def settimeout(self, timeout):
        assert 0 < timeout <= 15

    def send(self, message):
        self.requests.append(json.loads(message))

    def recv(self):
        reply = next(self.replies)
        if isinstance(reply, str):
            return reply
        request_id = self.requests[-1]["id"]
        return json.dumps({"id": request_id, **reply})


def page(items, cursor=None):
    return {"result": {"data": [{"items": items}], "nextCursor": cursor}}


def final(text):
    return {"type": "agentMessage", "phase": "final_answer", "text": text}


def test_latest_final_and_read_only_protocol():
    ws = Connection([
        {"result": {}},
        {"result": {"data": [
            {"items": [{"type": "agentMessage", "phase": "commentary", "text": "busy"}]},
            {"items": [final("older"), final("answer"), {"type": "userMessage"},
                       {"type": "commandExecution"}]},
        ], "nextCursor": "unused"}},
    ])
    assert codex_session.read_reply(ws, THREAD_ID) == "answer"
    assert [r["method"] for r in ws.requests] == [
        "initialize", "initialized", "thread/turns/list",
    ]
    assert ws.requests[0]["params"]["capabilities"] == {"experimentalApi": True}
    assert ws.requests[2]["params"] == {
        "threadId": THREAD_ID, "limit": 10, "sortDirection": "desc", "itemsView": "full",
    }


def test_pagination():
    ws = Connection([{"result": {}}, page([], "next"), page([final("answer")])])
    assert codex_session.read_reply(ws, THREAD_ID) == "answer"
    assert ws.requests[-1]["params"]["cursor"] == "next"


@pytest.mark.parametrize("reply, message", [
    (page([]), "No final reply"),
    ({"error": {"code": -1, "message": "unknown thread"}}, "App Server error"),
    ("", "closed the connection"),
    ("not json", "Expecting value"),
    ("[]", "Malformed App Server response"),
    ({"result": {}}, "Malformed App Server turns page"),
    (page([{"type": "agentMessage", "phase": "final_answer", "text": None}]),
     "Malformed App Server final reply"),
])
def test_no_reply_and_bad_responses(reply, message):
    with pytest.raises(ValueError, match=message):
        codex_session.read_reply(Connection([{"result": {}}, reply]), THREAD_ID)


def test_repeated_cursor():
    ws = Connection([{"result": {}}, page([], "same"), page([], "same")])
    with pytest.raises(ValueError, match="pagination cursor"):
        codex_session.read_reply(ws, THREAD_ID)


def test_request_deadline(monkeypatch):
    ticks = iter([0, 0, 61])
    monkeypatch.setattr(codex_session.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError, match="Timed out"):
        codex_session.read_reply(Connection([]), THREAD_ID)


def test_socket_discovery(tmp_path):
    (tmp_path / "ordinary-file").touch()
    with pytest.raises(ValueError, match="Found 0"):
        codex_session.discover_socket(tmp_path)
    with socket.socket(socket.AF_UNIX) as first, socket.socket(socket.AF_UNIX) as second:
        first.bind(str(tmp_path / "first"))
        assert codex_session.discover_socket(tmp_path) == str(tmp_path / "first")
        second.bind(str(tmp_path / "second"))
        with pytest.raises(ValueError, match="Found 2"):
            codex_session.discover_socket(tmp_path)


def test_invalid_uuid_before_connect(capsys, monkeypatch):
    def unexpected(*args):
        pytest.fail("invalid UUID must not connect")
    monkeypatch.setattr(codex_session.socket, "socket", unexpected)
    with pytest.raises(SystemExit) as exc:
        codex_session.main(["read", "not-a-uuid"])
    assert exc.value.code == 2
    assert "valid UUID" in capsys.readouterr().err


def test_help_and_missing_dependency(capsys, monkeypatch):
    original_import = builtins.__import__

    def without_websocket(name, *args, **kwargs):
        if name == "websocket":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_websocket)
    with pytest.raises(SystemExit) as exc:
        codex_session.main(["--help"])
    assert exc.value.code == 0
    capsys.readouterr()
    assert codex_session.main(["read", THREAD_ID, "--socket", "/nonexistent"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "reinstall codex-session" in captured.err


@pytest.mark.parametrize("reply, status", [(page([final("answer")]), 0), (page([]), 1)])
def test_cli_explicit_socket_output_and_cleanup(tmp_path, monkeypatch, capsys, reply, status):
    websocket = pytest.importorskip("websocket")

    ws = Connection([{"result": {}}, reply])
    sockets = []

    def connect(url, **kwargs):
        sockets.append(kwargs["socket"])
        assert url == "ws://localhost/"
        assert kwargs["suppress_origin"] is True
        return ws

    def unexpected_discovery(*args):
        pytest.fail("explicit socket must bypass discovery")

    monkeypatch.setattr(websocket, "create_connection", connect)
    monkeypatch.setattr(codex_session, "discover_socket", unexpected_discovery)
    path = str(tmp_path / "server")
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(path)
        server.listen(1)
        assert codex_session.main(["read", THREAD_ID, "--socket", path]) == status
    captured = capsys.readouterr()
    assert captured.out == ("answer\n" if status == 0 else "")
    assert bool(captured.err) == bool(status)
    assert ws.closed
    assert sockets[0].fileno() == -1


def test_status_protocol():
    status = {"type": "active", "activeFlags": ["waitingOnUserInput"]}
    ws = Connection([{"result": {}}, {"result": {"thread": {"status": status}}}])
    assert codex_session.read_status(ws, THREAD_ID) == status
    assert [r["method"] for r in ws.requests] == ["initialize", "initialized", "thread/read"]
    assert ws.requests[-1]["params"] == {"threadId": THREAD_ID, "includeTurns": False}


@pytest.mark.parametrize("status", [None, "active", {}, {"type": None}])
def test_invalid_status(status):
    ws = Connection([{"result": {}}, {"result": {"thread": {"status": status}}}])
    with pytest.raises(ValueError, match="thread status"):
        codex_session.read_status(ws, THREAD_ID)


def test_status_cli(capsys, monkeypatch):
    def query(thread_id, path, *, status_only):
        assert (thread_id, path, status_only) == (THREAD_ID, "/test.sock", True)
        return {"type": "active", "activeFlags": []}

    monkeypatch.setattr(codex_session, "query", query)
    assert codex_session.main(["status", THREAD_ID, "--socket", "/test.sock"]) == 0
    assert json.loads(capsys.readouterr().out) == {"type": "active", "activeFlags": []}


def test_send_direct_queue_argv(monkeypatch):
    calls = []

    def queue(argv, *, timeout):
        calls.append(argv)
        assert timeout == 60
        return codex_session.subprocess.CompletedProcess(argv, 9)

    monkeypatch.setattr(codex_session.subprocess, "run", queue)
    monkeypatch.setattr(codex_session, "query", lambda *a, **kw: pytest.fail("send must not query status"))
    message = 'text "quoted"; $(not-shell)\nnext'
    assert codex_session.main(["send", THREAD_ID, "--socket", "/test.sock", "--message", message]) == 9
    assert calls == [["codex", "queue", "--remote", "unix:///test.sock", "--thread", THREAD_ID,
                      "--message", message]]


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("status", ["active", "idle", "notLoaded", "systemError", "future-state", None])
def test_run_state_gate_and_durable_order(tmp_path, monkeypatch, status):
    path = tmp_path / "record.jsonl"
    queues = []
    child_calls = []
    command = ["program", "--message", "--socket", "--", "$(literal)"]

    def child(argv):
        child_calls.append(argv)
        return 7, None

    def query(thread_id, socket_path, *, status_only):
        assert (thread_id, socket_path, status_only) == (THREAD_ID, "/test.sock", True)
        assert records(path)[0]["exit_code"] == 7
        if status is None:
            raise ValueError("Malformed App Server thread status.")
        return {"type": status}

    def queue(thread_id, message, socket_path):
        assert len(records(path)) == 1
        queues.append((thread_id, message, socket_path))
        return 0

    monkeypatch.setattr(codex_session, "task_exit_code", child)
    monkeypatch.setattr(codex_session, "query", query)
    monkeypatch.setattr(codex_session, "queue_message", queue)
    assert codex_session.main([
        "run", THREAD_ID, "--socket", "/test.sock", "--message", "message", "--record", str(path),
        "--", *command,
    ]) == 7
    assert child_calls == [command]
    events = records(path)
    assert [e["event"] for e in events] == ["task_exit", "notification"]
    assert events[0]["run_id"] == events[1]["run_id"]
    if status == "active":
        assert queues == []
        assert events[1]["outcome"] == "skipped_active"
    else:
        assert queues == [(THREAD_ID, "message", "/test.sock")]
        assert events[1]["outcome"] == "queued"
    assert bool(events[1]["status_error"]) == (status is None)


@pytest.mark.parametrize("resolved", [False, True])
def test_run_socket_or_query_failure_still_sends(tmp_path, monkeypatch, resolved):
    path = tmp_path / "record.jsonl"
    queued = []
    monkeypatch.setattr(codex_session, "task_exit_code", lambda command: (0, None))

    def discover(directory):
        assert records(path)[0]["exit_code"] == 0
        if not resolved:
            raise ValueError("Found 0 sockets")
        return "/resolved.sock"

    def failed_query(*args, **kwargs):
        raise OSError("unavailable")

    def queue(thread_id, message, socket_path):
        queued.append(socket_path)
        return 0

    monkeypatch.setattr(codex_session, "discover_socket", discover)
    monkeypatch.setattr(codex_session, "query", failed_query)
    monkeypatch.setattr(codex_session, "queue_message", queue)
    assert codex_session.main(["run", THREAD_ID, "--message", "m", "--record", str(path), "--", "task"]) == 0
    assert queued == (["/resolved.sock"] if resolved else [None])
    assert records(path)[1]["status_error"]


@pytest.mark.parametrize("queue_result", [8, FileNotFoundError("codex missing")])
def test_real_child_output_and_queue_failure(tmp_path, monkeypatch, capfd, queue_result):
    path = tmp_path / "record.jsonl"
    counter = tmp_path / "calls.txt"
    queue_calls = []
    monkeypatch.setattr(codex_session, "query", lambda *args, **kwargs: {"type": "idle"})

    def queue(*args):
        queue_calls.append(args)
        if isinstance(queue_result, Exception):
            raise queue_result
        return queue_result

    monkeypatch.setattr(codex_session, "queue_message", queue)
    code = "import pathlib,sys; pathlib.Path(sys.argv[1]).open('a').write('run\\n'); print('out'); print('err', file=sys.stderr); sys.exit(6)"
    assert codex_session.main([
        "run", THREAD_ID, "--socket", "/test.sock", "--message", "m", "--record", str(path),
        "--", codex_session.sys.executable, "-c", code, str(counter),
    ]) == 6
    assert counter.read_text() == "run\n"
    assert len(queue_calls) == 1
    captured = capfd.readouterr()
    assert captured.out == "out\n"
    assert "err\n" in captured.err
    assert records(path)[0]["exit_code"] == 6
    assert records(path)[1]["outcome"] == "queue_failed"


def test_signal_and_missing_task_exit_codes(tmp_path):
    import signal

    assert codex_session.task_exit_code([str(tmp_path / "absent")])[0] == 127
    code = "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"
    assert codex_session.task_exit_code([codex_session.sys.executable, "-c", code]) == (128 + signal.SIGTERM, None)


def test_record_unwritable_prevents_task(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(codex_session, "task_exit_code", lambda command: pytest.fail("must not start task"))
    assert codex_session.main(["run", THREAD_ID, "--message", "m", "--record", str(tmp_path), "--", "task"]) == 1
    assert "directory" in capsys.readouterr().err


def test_exit_record_failure_prevents_network(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(codex_session, "task_exit_code", lambda command: (0, None))
    monkeypatch.setattr(codex_session, "query", lambda *a, **kw: pytest.fail("must persist exit first"))

    def fail_write(*args):
        raise OSError("disk full")

    monkeypatch.setattr(codex_session, "append_jsonl", fail_write)
    assert codex_session.main([
        "run", THREAD_ID, "--message", "m", "--record", str(tmp_path / "exit.jsonl"), "--", "task",
    ]) == 1
    assert "disk full" in capsys.readouterr().err


def test_run_requires_command(tmp_path):
    with pytest.raises(SystemExit) as exc:
        codex_session.main(["run", THREAD_ID, "--message", "m", "--record", str(tmp_path / "exit.jsonl"), "--"])
    assert exc.value.code == 2


def test_keyboard_interrupt_reaps_child(monkeypatch):
    import signal

    events = []

    class Child:
        def wait(self, timeout=None):
            events.append(("wait", timeout))
            if len(events) == 1:
                raise KeyboardInterrupt
            return -signal.SIGINT

        def send_signal(self, value):
            events.append(("signal", value))

    monkeypatch.setattr(codex_session.subprocess, "Popen", lambda command: Child())
    assert codex_session.task_exit_code(["task"]) == (130, None)
    assert events == [("wait", None), ("signal", signal.SIGINT), ("wait", 5)]


def test_notification_interrupt_preserves_task_exit(tmp_path, monkeypatch):
    path = tmp_path / "record.jsonl"
    monkeypatch.setattr(codex_session, "task_exit_code", lambda command: (7, None))
    monkeypatch.setattr(codex_session, "query", lambda *args, **kwargs: {"type": "idle"})

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(codex_session, "queue_message", interrupt)
    assert codex_session.main([
        "run", THREAD_ID, "--socket", "/test.sock", "--message", "m", "--record", str(path), "--", "task",
    ]) == 7
    assert records(path)[1]["queue_error"] == "KeyboardInterrupt"


def test_notification_record_failure_preserves_task_exit(tmp_path, monkeypatch, capsys):
    path = tmp_path / "record.jsonl"
    append_jsonl = codex_session.append_jsonl
    monkeypatch.setattr(codex_session, "task_exit_code", lambda command: (7, None))
    monkeypatch.setattr(codex_session, "query", lambda *args, **kwargs: {"type": "active"})

    def fail_notification(record, event):
        if event["event"] == "notification":
            raise OSError("disk full")
        append_jsonl(record, event)

    monkeypatch.setattr(codex_session, "append_jsonl", fail_notification)
    assert codex_session.main([
        "run", THREAD_ID, "--socket", "/test.sock", "--message", "m", "--record", str(path),
        "--", "task", "secret-argument",
    ]) == 7
    assert len(records(path)) == 1
    assert records(path)[0]["exit_code"] == 7
    assert "secret-argument" not in path.read_text()
    captured = capsys.readouterr()
    assert "notification record failed" in captured.err
    assert "disk full" in captured.err
