import builtins
import json
import io
import stat
import socket
from pathlib import Path
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import ding


THREAD_ID = "00000000-0000-4000-8000-000000000001"
OTHER_ID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def isolated_state_and_no_real_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    run = subprocess.run
    popen = subprocess.Popen

    def guarded_run(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and Path(argv[0]).name == "codex" and any(
                action in argv for action in ("queue", "exec", "resume")):
            pytest.fail("tests must not queue messages to real sessions")
        return run(argv, *args, **kwargs)

    def guarded_popen(argv, *args, **kwargs):
        if isinstance(argv, (list, tuple)) and Path(argv[0]).name == "codex" and any(
                action in argv for action in ("queue", "exec", "resume")):
            pytest.fail("tests must not message real sessions through codex")
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    connect = socket.socket.connect

    def guarded_connect(sock, address):
        if sock.family == socket.AF_UNIX and not Path(address).resolve().is_relative_to(tmp_path):
            pytest.fail("tests must not connect to real App Server sockets")
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


def thread(thread_id=THREAD_ID, name="Build", cwd="/repo", status="idle"):
    return {"id": thread_id, "name": name, "cwd": cwd, "status": {"type": status},
            "preview": "first user message is not a final reply"}


@contextmanager
def server_calls(call):
    yield call


class Connection:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []
        self.closed = False
        self.cont_frame = SimpleNamespace(skip_utf8_validation=False)

    def close(self):
        self.closed = True

    def settimeout(self, timeout):
        assert 0 < timeout <= 60

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
            {"items": [final("older"), final("answer"), {"type": "userMessage"},
                       {"type": "commandExecution"}]},
        ], "nextCursor": "unused"}},
    ])
    assert ding.read_reply(ding.initialize(ws), THREAD_ID) == "answer"
    assert [r["method"] for r in ws.requests] == [
        "initialize", "initialized", "thread/turns/list",
    ]
    assert ws.requests[0]["params"]["capabilities"] == {"experimentalApi": True}
    assert ws.requests[2]["params"] == {
        "threadId": THREAD_ID, "limit": 1, "sortDirection": "desc", "itemsView": "full",
    }


def test_pagination():
    ws = Connection([{"result": {}}, page([], "next"),
                     page([{"type": "agentMessage", "phase": "commentary", "text": "busy"}], "older"),
                     page([final("answer")])])
    assert ding.read_reply(ding.initialize(ws), THREAD_ID) == "answer"
    assert ws.requests[-1]["params"]["cursor"] == "older"
    assert all(request["params"]["limit"] == 1 for request in ws.requests[2:])


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
        ding.read_reply(ding.initialize(Connection([{"result": {}}, reply])), THREAD_ID)


def test_repeated_cursor():
    ws = Connection([{"result": {}}, page([], "same"), page([], "same")])
    with pytest.raises(ValueError, match="pagination cursor"):
        ding.read_reply(ding.initialize(ws), THREAD_ID)


def test_request_deadline(monkeypatch):
    ticks = iter([0, 0, 61])
    monkeypatch.setattr(ding.time, "monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError, match="Timed out"):
        ding.read_reply(ding.initialize(Connection([])), THREAD_ID)


def test_rpc_deadline_restarts_for_each_call(monkeypatch):
    elapsed = [0]
    monkeypatch.setattr(ding.time, "monotonic", lambda: elapsed[0])
    call = ding.initialize(Connection([{"result": {}}, {"result": {}}, {"result": {}}]))
    elapsed[0] = 59
    assert call("thread/read", {"threadId": THREAD_ID}) == {}
    elapsed[0] = 118
    assert call("thread/read", {"threadId": OTHER_ID}) == {}


def test_socket_discovery(tmp_path):
    (tmp_path / "ordinary-file").touch()
    with pytest.raises(ValueError, match="Found 0"):
        ding.discover_socket(tmp_path)
    with socket.socket(socket.AF_UNIX) as first, socket.socket(socket.AF_UNIX) as second:
        first.bind(str(tmp_path / "first"))
        assert ding.discover_socket(tmp_path) == str(tmp_path / "first")
        second.bind(str(tmp_path / "second"))
        with pytest.raises(ValueError, match="Found 2"):
            ding.discover_socket(tmp_path)


def test_invalid_issue_before_connect(capsys, monkeypatch):
    def unexpected(*args):
        pytest.fail("invalid issue must not connect")
    monkeypatch.setattr(ding.socket, "socket", unexpected)
    with pytest.raises(SystemExit) as exc:
        ding.main(["read", "--issue", "0"])
    assert exc.value.code == 2
    assert "positive issue number" in capsys.readouterr().err


def test_help_and_missing_dependency(capsys, monkeypatch):
    original_import = builtins.__import__

    def without_websocket(name, *args, **kwargs):
        if name == "websocket":
            raise ImportError("not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_websocket)
    with pytest.raises(SystemExit) as exc:
        ding.main(["--help"])
    assert exc.value.code == 0
    capsys.readouterr()
    assert ding.main(["read", THREAD_ID, "--socket", "/nonexistent"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "reinstall dkitchen" in captured.err


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
    monkeypatch.setattr(ding, "discover_socket", unexpected_discovery)
    path = str(tmp_path / "server")
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(path)
        server.listen(1)
        assert ding.main(["read", THREAD_ID, "--socket", path]) == status
    captured = capsys.readouterr()
    assert captured.out == ("answer\n" if status == 0 else "")
    assert bool(captured.err) == bool(status)
    assert ws.closed
    assert sockets[0].fileno() == -1


def test_status_protocol():
    status = {"type": "active", "activeFlags": ["waitingOnUserInput"]}
    ws = Connection([{"result": {}}, {"result": {"thread": {"status": status}}}])
    assert ding.read_status(ding.initialize(ws), THREAD_ID) == status
    assert [r["method"] for r in ws.requests] == ["initialize", "initialized", "thread/read"]
    assert ws.requests[-1]["params"] == {"threadId": THREAD_ID, "includeTurns": False}


@pytest.mark.parametrize("status", [None, "active", {}, {"type": None}])
def test_invalid_status(status):
    ws = Connection([{"result": {}}, {"result": {"thread": {"status": status}}}])
    with pytest.raises(ValueError, match="thread status"):
        ding.read_status(ding.initialize(ws), THREAD_ID)


def test_status_cli(capsys, monkeypatch):
    def query(thread_id, path, *, status_only):
        assert (thread_id, path, status_only) == (THREAD_ID, "/test.sock", True)
        return {"type": "active", "activeFlags": []}

    monkeypatch.setattr(ding, "query", query)
    assert ding.main(["status", THREAD_ID, "--socket", "/test.sock"]) == 0
    assert json.loads(capsys.readouterr().out) == {"type": "active", "activeFlags": []}


def test_send_persists_intent_before_delivery(monkeypatch, tmp_path):
    calls = []
    message = 'text "quoted"; $(not-shell)\nnext'

    def deliver(thread_id, text, path, client_id):
        intent, = records(tmp_path / "state/ding/sent.jsonl")
        assert intent["event"] == "send_intent"
        assert intent["outcome"] == "pending"
        assert intent["client_message_id"] == client_id
        assert intent["message"] == message
        calls.append((thread_id, text, path))
        return {"outcome": "started", "error": None}

    monkeypatch.setattr(ding, "deliver_message", deliver)
    assert ding.main(["send", THREAD_ID, "--socket", "/test.sock", "--message", message]) == 0
    assert calls == [(THREAD_ID, message, "/test.sock")]


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.parametrize("status", ["active", "idle", "notLoaded", "systemError", "future-state", None])
def test_run_state_gate_and_durable_order(tmp_path, monkeypatch, status):
    path = tmp_path / "record.jsonl"
    child_calls = []
    command = ["program", "--message", "--socket", "--", "$(literal)"]
    calls = []
    client_id = [None]

    def child(argv):
        child_calls.append(argv)
        return 7, None

    def call(method, params):
        calls.append(method)
        intent, = records(path)
        assert intent["exit_code"] == 7
        assert intent["message"] == "message"
        client_id[0] = intent["client_message_id"]
        if method == "thread/read":
            if status is None:
                raise ValueError("Malformed App Server thread status.")
            return {"thread": thread(status="idle" if "thread/resume" in calls else status)}
        if method == "thread/resume":
            return {"thread": thread()}
        if method == "thread/queue/add":
            return {"queuedSubmission": {"id": "q", "clientUserMessageId": client_id[0],
                                         "input": params["input"]}}
        if method == "thread/turns/list":
            return {"data": []}
        if method == "thread/queue/start":
            return {"turn": started_turn(client_id[0])}
        pytest.fail(method)

    monkeypatch.setattr(ding, "task_exit_code", child)
    monkeypatch.setattr(ding, "app_server", lambda path, **kwargs: server_calls(call))
    assert ding.main(["run", THREAD_ID, "--socket", "/test.sock", "--message", "message",
                      "--record", str(path), "--", *command]) == 7
    assert child_calls == [command]
    events = records(path)
    assert [e["event"] for e in events] == ["task_exit", "notification"]
    assert events[0]["run_id"] == events[1]["run_id"]
    assert events[0]["client_message_id"] == events[1]["client_message_id"]
    expected = "skipped_active" if status == "active" else (
        "started" if status in {"idle", "notLoaded"} else "failed")
    assert events[1]["outcome"] == expected
    assert calls.count("thread/queue/add") == (1 if expected == "started" else 0)


@pytest.mark.parametrize("resolved", [False, True])
def test_run_socket_or_query_failure_prevents_delivery(tmp_path, monkeypatch, resolved):
    path = tmp_path / "record.jsonl"
    calls = []
    monkeypatch.setattr(ding, "task_exit_code", lambda command: (0, None))

    def discover(directory):
        assert records(path)[0]["exit_code"] == 0
        if not resolved:
            raise ValueError("Found 0 sockets")
        return "/resolved.sock"

    def call(method, params):
        calls.append(method)
        raise OSError("unavailable")

    monkeypatch.setattr(ding, "discover_socket", discover)
    monkeypatch.setattr(ding, "app_server", lambda path, **kwargs: server_calls(call))
    assert ding.main(["run", THREAD_ID, "--message", "m", "--record", str(path), "--", "task"]) == 0
    assert calls == (["thread/read"] if resolved else [])
    assert records(path)[1]["outcome"] == "failed"
    assert records(path)[1]["error"]


def test_real_child_output_and_notification_failure(tmp_path, monkeypatch, capfd):
    path = tmp_path / "record.jsonl"
    counter = tmp_path / "calls.txt"
    calls = []

    def deliver(*args, **kwargs):
        calls.append(args)
        return {"outcome": "failed", "error": "notification unavailable"}

    monkeypatch.setattr(ding, "deliver_message", deliver)
    code = "import pathlib,sys; pathlib.Path(sys.argv[1]).open('a').write('run\\n'); print('out'); print('err', file=sys.stderr); sys.exit(6)"
    assert ding.main(["run", THREAD_ID, "--socket", "/test.sock", "--message", "m", "--record", str(path),
                      "--", ding.sys.executable, "-c", code, str(counter)]) == 6
    assert counter.read_text() == "run\n"
    assert len(calls) == 1
    captured = capfd.readouterr()
    assert captured.out == "out\n"
    assert "err\n" in captured.err
    assert records(path)[0]["exit_code"] == 6
    assert records(path)[1]["outcome"] == "failed"


def test_signal_and_missing_task_exit_codes(tmp_path):
    import signal

    assert ding.task_exit_code([str(tmp_path / "absent")])[0] == 127
    code = "import os,signal; os.kill(os.getpid(), signal.SIGTERM)"
    assert ding.task_exit_code([ding.sys.executable, "-c", code]) == (128 + signal.SIGTERM, None)


def test_record_unwritable_prevents_task(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ding, "task_exit_code", lambda command: pytest.fail("must not start task"))
    assert ding.main(["run", THREAD_ID, "--message", "m", "--record", str(tmp_path), "--", "task"]) == 1
    assert "directory" in capsys.readouterr().err


def test_exit_record_failure_prevents_network(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ding, "task_exit_code", lambda command: (0, None))
    monkeypatch.setattr(ding, "query", lambda *a, **kw: pytest.fail("must persist exit first"))

    def fail_write(*args):
        raise OSError("disk full")

    monkeypatch.setattr(ding, "append_jsonl", fail_write)
    assert ding.main([
        "run", THREAD_ID, "--message", "m", "--record", str(tmp_path / "exit.jsonl"), "--", "task",
    ]) == 1
    assert "disk full" in capsys.readouterr().err


def test_run_requires_command(tmp_path):
    with pytest.raises(SystemExit) as exc:
        ding.main(["run", THREAD_ID, "--message", "m", "--record", str(tmp_path / "exit.jsonl"), "--"])
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

    monkeypatch.setattr(ding.subprocess, "Popen", lambda command: Child())
    assert ding.task_exit_code(["task"]) == (130, None)
    assert events == [("wait", None), ("signal", signal.SIGINT), ("wait", 5)]


def test_notification_interrupt_preserves_task_exit(tmp_path, monkeypatch):
    path = tmp_path / "record.jsonl"
    monkeypatch.setattr(ding, "task_exit_code", lambda command: (7, None))

    def call(method, params):
        if method == "thread/read":
            return {"thread": thread()}
        if method == "thread/turns/list":
            return {"data": []}
        assert method == "thread/queue/add"
        raise KeyboardInterrupt

    monkeypatch.setattr(ding, "app_server", lambda path, **kwargs: server_calls(call))
    assert ding.main(["run", THREAD_ID, "--socket", "/test.sock", "--message", "m",
                      "--record", str(path), "--", "task"]) == 7
    assert records(path)[1]["error"] == "KeyboardInterrupt"
    assert records(path)[1]["outcome"] == "unknown"


def test_notification_record_failure_preserves_task_exit(tmp_path, monkeypatch, capsys):
    path = tmp_path / "record.jsonl"
    append_jsonl = ding.append_jsonl
    monkeypatch.setattr(ding, "task_exit_code", lambda command: (7, None))
    monkeypatch.setattr(ding, "deliver_message", lambda *args, **kwargs: {"outcome": "skipped_active", "error": None})

    def fail_notification(record, event):
        if event["event"] == "notification":
            raise OSError("disk full")
        append_jsonl(record, event)

    monkeypatch.setattr(ding, "append_jsonl", fail_notification)
    assert ding.main([
        "run", THREAD_ID, "--socket", "/test.sock", "--message", "m", "--record", str(path),
        "--", "task", "secret-argument",
    ]) == 7
    assert len(records(path)) == 1
    assert records(path)[0]["exit_code"] == 7
    assert "secret-argument" not in path.read_text()
    captured = capsys.readouterr()
    assert "notification record failed" in captured.err
    assert "disk full" in captured.err


def test_thread_list_pagination_and_metadata():
    ws = Connection([
        {"result": {}},
        {"result": {"data": [thread(name=None)], "nextCursor": "next"}},
        {"result": {"data": [thread(name="duplicate"), thread(OTHER_ID, "Other")]}},
    ])
    threads = ding.list_threads(ding.initialize(ws))
    assert [(value["id"], value["name"]) for value in threads] == [
        (THREAD_ID, THREAD_ID), (OTHER_ID, "Other"),
    ]
    assert [request["method"] for request in ws.requests] == [
        "initialize", "initialized", "thread/list", "thread/list",
    ]
    assert ws.requests[-1]["params"]["cursor"] == "next"
    assert ws.requests[-1]["params"]["archived"] is False
    assert ws.requests[-1]["params"]["useStateDbOnly"] is True
    assert "subAgent" in ws.requests[-1]["params"]["sourceKinds"]


@pytest.mark.parametrize("data", [None, [None], [thread(name=42)],
                                  [thread(cwd="relative")], [thread(status=None)],
                                  [thread(thread_id="not-a-uuid")]])
def test_invalid_thread_list(data):
    with pytest.raises(ValueError, match="Malformed App Server"):
        ding.list_threads(lambda *args: {"data": data})


def test_thread_list_rejects_repeated_cursor():
    with pytest.raises(ValueError, match="pagination cursor"):
        ding.list_threads(lambda *args: {"data": [], "nextCursor": "same"})


def test_uuid_and_unambiguous_issue_targets_are_local(monkeypatch):
    monkeypatch.setattr(ding, "app_server", lambda *a: pytest.fail("must not connect"))
    monkeypatch.setattr(ding, "socket_path", lambda *a: pytest.fail("must not discover socket"))
    monkeypatch.setattr(ding, "github_repository", lambda: "example/project")
    issues = []

    def issue_session(number, path=None, repo=None):
        issues.append((number, path, repo))
        return OTHER_ID

    monkeypatch.setattr(ding, "issue_session", issue_session)
    assert ding.resolve_target(THREAD_ID.upper(), None, None) == THREAD_ID
    assert ding.resolve_target("#42", None, "/test.sock") == OTHER_ID
    assert ding.resolve_target(None, 7, "/other.sock") == OTHER_ID
    assert issues == [(42, "/test.sock", "example/project"), (7, "/other.sock", "example/project")]
    with pytest.raises(ValueError, match="positive issue number"):
        ding.resolve_target("#zero", None, None)


@pytest.mark.parametrize("target", [
    "example/project#42",
    "https://github.com/example/project/issues/42",
    "https://github.com/example/project/issues/42#issuecomment-123",
    "https://github.com/example/project/pull/42",
    "https://github.com/example/project/pull/42#issuecomment-123",
    "https://github.com/example/project/issues/42/",
    "https://github.com/example/project/issues/42?query=1",
    "https://github.com/example/project/issues/42?notification_referrer_id=NT_1#issuecomment-123",
])
def test_explicit_issue_target_uses_its_repository(monkeypatch, tmp_path, target):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_REPO", "wrong/repository")
    monkeypatch.setattr(ding, "github_repository", lambda: pytest.fail("must not discover repository"))
    monkeypatch.setattr(ding, "app_server", lambda *a: pytest.fail("must not connect"))
    calls = []

    def gh(arguments):
        calls.append(arguments)
        if "--paginate" in arguments:
            return [[{"body": f"Codex owner: {THREAD_ID}"}]]
        return {"body": f"Codex owner: {OTHER_ID}"}

    monkeypatch.setattr(ding, "gh_json", gh)
    assert ding.resolve_target(target, None, "/test.sock") == THREAD_ID
    assert calls == [
        ["api", "repos/example/project/issues/42"],
        ["api", "--paginate", "--slurp", "repos/example/project/issues/42/comments?per_page=100"],
    ]


@pytest.mark.parametrize("target", [
    "#0", "#-1", "#abc", "example/project#0", "example/project#-1",
    "example/project#abc", "example/project#", "example/project#42#extra",
    "example/project/extra#42", "example//project#42", "/project#42", "example/#42",
    "example/..#42", "./project#42",
    "éxample/project#42", "example/pro%2Fject#42",
    "http://github.com/example/project/issues/42",
    "https://example.com/example/project/issues/42",
    "https://github.com.evil/example/project/issues/42",
    "https://user@github.com/example/project/issues/42",
    "https://github.com:443/example/project/issues/42",
    "https://github.com/example/project/issues/0",
    "https://github.com/example/project/issues/-1",
    "https://github.com/example/project/issues/abc",
    "https://github.com/example/project/issues/42/extra",
    "https://github.com/example/../issues/42",
    "https://github.com/example/project/discussions/42",
])
def test_invalid_issue_targets_fail_before_lookup(monkeypatch, target):
    for function in ("github_repository", "gh_json", "gh_records", "app_server"):
        monkeypatch.setattr(ding, function, lambda *a, **kw: pytest.fail("invalid target must not look up"))
    with pytest.raises(ValueError, match="Issue (target|URL)"):
        ding.resolve_target(target, None, "/test.sock")


@pytest.mark.parametrize("target, names, expected", [
    ("Build", ["Build", "Other"], THREAD_ID),
    ("Build #42", ["Build #42", "Other"], THREAD_ID),
    ("Review / Build #42", ["Review / Build #42", "Other"], THREAD_ID),
    ("Build", ["Build", "Build"], "Found 2"),
    ("Missing", ["Build", "Other"], "Found 0"),
    ("build", ["Build", "Other"], "Found 0"),
])
def test_exact_name_target_and_candidates(monkeypatch, target, names, expected):
    values = [thread(THREAD_ID, names[0]), thread(OTHER_ID, names[1], cwd="/other-repo")]
    monkeypatch.setattr(ding, "app_server", lambda path: server_calls(lambda *a: {"data": values}))
    if expected == THREAD_ID:
        assert ding.resolve_target(target, None, "/test.sock") == THREAD_ID
    else:
        with pytest.raises(ValueError, match=expected) as exc:
            ding.resolve_target(target, None, "/test.sock")
        assert "Candidates" in str(exc.value)
        assert THREAD_ID in str(exc.value)
        assert OTHER_ID in str(exc.value)


def test_issue_owner_precedes_later_session_comment(monkeypatch):
    monkeypatch.setattr(ding, "github_repository", lambda: "example/project")
    monkeypatch.setattr(ding, "gh_json", lambda arguments: {
        "body": f"Codex owner: {THREAD_ID}\nCodex session: {THREAD_ID}",
    })
    monkeypatch.setattr(ding, "gh_records", lambda resource: [
        {"body": f"Codex session: {OTHER_ID}"},
    ])
    assert ding.issue_session(42) == THREAD_ID


def test_issue_latest_owner_across_all_comment_pages(monkeypatch):
    calls = []

    def gh(arguments):
        calls.append(arguments)
        if arguments[0] == "repo":
            return {"nameWithOwner": "example/project"}
        if "--paginate" not in arguments:
            return {"body": f"Codex owner: {OTHER_ID}\nCodex owner: {THREAD_ID}\n"}
        return [[{"body": "Codex owner: invalid"}], [
            {"body": f"Codex owner: `{OTHER_ID}`\r\nCodex session: {THREAD_ID}\r\n"},
            {"body": f"Codex session: {THREAD_ID}"},
        ]]

    monkeypatch.setattr(ding, "gh_json", gh)
    assert ding.issue_session(42) == OTHER_ID
    assert "--slurp" in calls[-1]
    assert calls[-1][-1].endswith("/42/comments?per_page=100")
    monkeypatch.setattr(ding, "gh_records", lambda *a: [{"body": "Codex owner: invalid"}])
    with pytest.raises(ValueError, match="last Codex owner line"):
        ding.issue_session(42)


@pytest.fixture
def issue_data(monkeypatch):
    issue = {"body": f"Codex session: {THREAD_ID}", "created_at": "2026-01-01T00:00:00Z",
             "html_url": "https://github.com/example/project/issues/42"}
    comments = []
    monkeypatch.setattr(ding, "github_repository", lambda: "example/project")
    monkeypatch.setattr(ding, "gh_json", lambda arguments: issue)
    monkeypatch.setattr(ding, "gh_records", lambda resource: comments)
    monkeypatch.setattr(ding, "app_server", lambda *a: pytest.fail("unexpected App Server lookup"))
    return issue, comments


@pytest.mark.parametrize("body, comments, expected", [
    (f"Codex owner: {OTHER_ID}", [], OTHER_ID),
    (f"Codex session: invalid\nCodex owner: {THREAD_ID}",
     [f"Codex session: {OTHER_ID}", "Codex session: invalid"], THREAD_ID),
    (f"Codex owner: invalid\nCodex owner: {THREAD_ID}", [], THREAD_ID),
    (f"Codex owner: {OTHER_ID}", [f"Codex owner: {THREAD_ID}",
     f"Codex owner: {OTHER_ID}\nCodex owner: {THREAD_ID}", f"Codex session: {OTHER_ID}"], THREAD_ID),
    ("Codex owner: `01234567-89AB-4CDE-8F01-23456789ABCD`", [],
     "01234567-89ab-4cde-8f01-23456789abcd"),
])
def test_issue_owner_selection(issue_data, body, comments, expected):
    issue, records = issue_data
    issue["body"] = body
    records.extend({"body": text} for text in comments)
    assert ding.issue_session(42) == expected


@pytest.mark.parametrize("value", ["", "invalid"])
def test_latest_invalid_owner_refuses_without_session_fallback(issue_data, value):
    issue, comments = issue_data
    issue["body"] += f"\nCodex owner: {THREAD_ID}"
    comments.append({"body": f"Codex owner: {value}\nCodex session: {OTHER_ID}"})
    with pytest.raises(ValueError, match="last Codex owner line"):
        ding.issue_session(42)


@pytest.mark.parametrize("spellings", [
    ["01234567-89ab-4cde-8f01-23456789abcd"],
    ["01234567-89ab-4cde-8f01-23456789abcd", "01234567-89AB-4CDE-8F01-23456789ABCD",
     "{01234567-89ab-4cde-8f01-23456789abcd}", "0123456789ab4cde8f0123456789abcd"],
])
def test_issue_unique_session_normalizes_repeated_mentions(issue_data, spellings):
    issue, comments = issue_data
    issue["body"] = f"Codex session: {spellings[0]}"
    comments.extend({"body": f"Codex session: `{value}`"} for value in spellings)
    assert ding.issue_session(42) == "01234567-89ab-4cde-8f01-23456789abcd"


@pytest.mark.parametrize("body, comments, expected", [
    ("No references", [], "No Codex session line"),
    (f"Codex session: {THREAD_ID}", ["Codex session: invalid"], "not a valid UUID"),
    ("Codex session: invalid", [f"Codex session: {THREAD_ID}"], "not a valid UUID"),
    ("Codex session:", [], "not a valid UUID"),
])
def test_issue_without_owner_refuses_missing_or_malformed_references(issue_data, body, comments, expected):
    issue, records = issue_data
    issue["body"] = body
    records.extend({"body": text} for text in comments)
    with pytest.raises(ValueError, match=expected):
        ding.issue_session(42)


def test_issue_ambiguity_details_count_comments_and_use_created_at(issue_data, monkeypatch):
    issue, comments = issue_data
    issue["body"] += f"\nCodex session: {OTHER_ID}"
    comments.extend([
        {"body": f"Codex session: {OTHER_ID}\nCodex session: {{{OTHER_ID}}}",
         "created_at": "2026-01-02T00:00:00Z", "updated_at": "2026-03-01T00:00:00Z",
         "html_url": issue["html_url"] + "#issuecomment-1"},
        {"body": f"Codex session: {OTHER_ID}", "created_at": "2026-01-03T00:00:00Z",
         "updated_at": "2026-01-03T00:00:00Z", "html_url": issue["html_url"] + "#issuecomment-2"},
    ])
    name = 'Review\n"candidate"\t'
    calls = []

    def call(method, params):
        calls.append((method, params))
        assert method == "thread/list"
        return {"data": [thread(name=name), thread(OTHER_ID, "Other", status="notLoaded")]}

    monkeypatch.setattr(ding, "app_server", lambda path: server_calls(call))
    with pytest.raises(ValueError) as exc:
        ding.issue_session(42, "/test.sock")
    message = str(exc.value)
    assert "2 different Codex sessions" in message
    assert f"{THREAD_ID}\t{ding.display_cell(name)}\tidle\t0\t2026-01-01T00:00:00Z\t{issue['html_url']}" in message
    assert f"{OTHER_ID}\tOther\tnotLoaded\t2\t2026-01-03T00:00:00Z\t{issue['html_url']}#issuecomment-2" in message
    assert "2026-03-01" not in message
    assert "session UUID or exact session name" in message
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["socket", "list_and_reads", "missing_one"])
def test_issue_ambiguity_survives_unavailable_metadata(issue_data, monkeypatch, failure):
    issue, comments = issue_data
    comments.append({"body": f"Codex session: {OTHER_ID}", "created_at": "2026-01-02T00:00:00Z",
                     "html_url": issue["html_url"] + "#issuecomment-1"})

    def call(method, params):
        if method == "thread/list" and failure == "missing_one":
            return {"data": [thread()]}
        raise ValueError("candidate metadata unavailable")

    def unavailable(path):
        raise OSError("socket unavailable")

    monkeypatch.setattr(ding, "app_server", unavailable if failure == "socket"
                        else lambda path: server_calls(call))
    with pytest.raises(ValueError, match="2 different Codex sessions") as exc:
        ding.issue_session(42, "/test.sock")
    message = str(exc.value)
    assert f"{OTHER_ID}\tunknown\tunknown\t1\t2026-01-02T00:00:00Z\t{issue['html_url']}#issuecomment-1" in message
    assert THREAD_ID in message
    assert "session UUID or exact session name" in message


@pytest.mark.parametrize("returned_id, expected_name, expected_status", [
    (OTHER_ID, "Archived", "notLoaded"),
    (THREAD_ID, "unknown", "unknown"),
    ("invalid", "unknown", "unknown"),
])
def test_issue_ambiguity_reads_metadata_missing_from_list(
        issue_data, monkeypatch, returned_id, expected_name, expected_status):
    issue, comments = issue_data
    comments.append({"body": f"Codex session: {OTHER_ID}"})
    calls = []

    def call(method, params):
        calls.append((method, params))
        if method == "thread/list":
            return {"data": [thread()]}
        assert (method, params) == ("thread/read", {"threadId": OTHER_ID, "includeTurns": False})
        return {"thread": thread(returned_id, "Archived", status="notLoaded")}

    monkeypatch.setattr(ding, "app_server", lambda path: server_calls(call))
    with pytest.raises(ValueError) as exc:
        ding.issue_session(42, "/test.sock")
    assert f"{OTHER_ID}\t{expected_name}\t{expected_status}\t1\tunknown\tunknown" in str(exc.value)
    assert len(calls) == 2


@pytest.mark.parametrize("action", ["send", "run", "read", "status"])
@pytest.mark.parametrize("target", [
    ["#42"], ["--issue", "42"], ["example/project#42"],
    ["https://github.com/example/project/issues/42#issuecomment-123"],
    ["https://github.com/example/project/pull/42"],
])
def test_ambiguous_issue_refuses_every_command_before_execution(
        issue_data, monkeypatch, tmp_path, capsys, action, target):
    issue, comments = issue_data
    comments.append({"body": f"Codex session: {OTHER_ID}"})
    paths = []

    def server(path):
        paths.append(path)
        return server_calls(lambda method, params: {"data": [thread(), thread(OTHER_ID, "Other", status="active")]})

    monkeypatch.setattr(ding, "app_server", server)
    for function in ("deliver_message", "task_exit_code", "run_task", "query"):
        monkeypatch.setattr(ding, function, lambda *a, **kw: pytest.fail("ambiguous target must not execute"))
    argv = [action, *target, "--socket", "/explicit.sock"]
    record = tmp_path / "exit.jsonl"
    if action in {"send", "run"}:
        argv.extend(["--message", "must not be delivered"])
    if action == "run":
        argv.extend(["--record", str(record), "--", "must-not-start"])
    assert ding.main(argv) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "2 different Codex sessions" in captured.err
    assert THREAD_ID in captured.err and OTHER_ID in captured.err
    assert paths == ["/explicit.sock"]
    assert not record.exists()
    if action == "send":
        event, = records(tmp_path / "state/ding/sent.jsonl")
        assert event["thread_id"] is None
        assert event["outcome"] == "failed"
        assert event["target"] == "example/project#42"


@pytest.mark.parametrize("action", ["send", "run", "read", "status"])
@pytest.mark.parametrize("target", [
    ["#42"], ["--issue", "42"], ["example/project#42"],
    ["https://github.com/example/project/issues/42#issuecomment-123"],
    ["https://github.com/example/project/pull/42#discussion_r123"],
])
def test_issue_targets_work_for_every_command_and_log_canonical_target(
        issue_data, monkeypatch, tmp_path, action, target):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_REPO", "wrong/repository")
    discoveries, calls = [], []
    monkeypatch.setattr(ding, "github_repository", lambda: discoveries.append(True) or "example/project")

    def gh(arguments):
        assert arguments == ["api", "repos/example/project/issues/42"]
        return issue_data[0]

    def comments(resource):
        assert resource == "repos/example/project/issues/42/comments?per_page=100"
        return issue_data[1]

    def delivery(thread_id, message, path, client_id):
        calls.append((thread_id, message, path))
        return {"outcome": "started", "error": None}

    def run(args, command):
        calls.append((args.thread_id, args.message, args.socket))
        assert command == ["fake-task"]
        return 0

    def query(thread_id, path, *, status_only=False):
        calls.append((thread_id, path, status_only))
        return {"type": "idle"} if status_only else "final reply"

    monkeypatch.setattr(ding, "gh_json", gh)
    monkeypatch.setattr(ding, "gh_records", comments)
    monkeypatch.setattr(ding, "deliver_message", delivery)
    monkeypatch.setattr(ding, "run_task", run)
    monkeypatch.setattr(ding, "query", query)
    argv = [action, *target, "--socket", "/test.sock"]
    if action in {"send", "run"}:
        argv.extend(["--message", "test message"])
    if action == "run":
        argv.extend(["--record", str(tmp_path / "exit.jsonl"), "--", "fake-task"])
    assert ding.main(argv) == 0
    assert discoveries == ([True] if target[0] in {"#42", "--issue"} else [])
    assert calls == ([(THREAD_ID, "test message", "/test.sock")] if action in {"send", "run"}
                     else [(THREAD_ID, "/test.sock", action == "status")])
    if action == "send":
        intent, result = records(tmp_path / "state/ding/sent.jsonl")
        assert intent["target"] == result["target"] == "example/project#42"
        assert intent["thread_id"] == result["thread_id"] == THREAD_ID


@pytest.mark.parametrize("action", ["send", "run", "read", "status"])
def test_invalid_explicit_issue_refuses_every_command(monkeypatch, tmp_path, action):
    for function in ("github_repository", "gh_json", "gh_records", "app_server",
                     "deliver_message", "run_task", "query"):
        monkeypatch.setattr(ding, function, lambda *a, **kw: pytest.fail("invalid target must not execute"))
    argv = [action, "example/project#0", "--socket", "/test.sock"]
    if action in {"send", "run"}:
        argv.extend(["--message", "must not deliver"])
    if action == "run":
        argv.extend(["--record", str(tmp_path / "exit.jsonl"), "--", "must-not-start"])
    assert ding.main(argv) == 1
    assert not (tmp_path / "exit.jsonl").exists()
    if action == "send":
        event, = records(tmp_path / "state/ding/sent.jsonl")
        assert event["target"] == "example/project#0"
        assert event["outcome"] == "failed"


def test_issue_map_body_comments_and_optional_failure(monkeypatch):
    monkeypatch.setattr(ding, "github_repository", lambda: "example/project")

    def records(resource):
        if "issues/comments" in resource:
            return [{"issue_url": "https://api.github.com/repos/example/project/issues/2",
                     "body": f"Codex session: {OTHER_ID}"}]
        return [{"number": 1, "body": f"Codex session: {THREAD_ID}"},
                {"number": 2, "body": "Codex session: invalid"},
                {"number": 3, "body": f"Codex session: {THREAD_ID}", "pull_request": {}}]

    monkeypatch.setattr(ding, "gh_records", records)
    assert ding.issue_map() == {THREAD_ID: {1}, OTHER_ID: {2}}
    monkeypatch.setattr(ding, "github_repository", lambda: (_ for _ in ()).throw(FileNotFoundError("gh")))
    assert ding.issue_map() == {}


def test_issue_map_attributes_claimed_issues_only_to_latest_owner(monkeypatch):
    monkeypatch.setattr(ding, "github_repository", lambda: "example/project")
    issues = [
        {"number": 1, "body": f"Codex owner: {THREAD_ID}\nCodex session: {OTHER_ID}"},
        {"number": 2, "body": f"Codex session: {OTHER_ID}\nCodex session: {THREAD_ID}"},
        {"number": 3, "body": "Codex owner: invalid"},
        {"number": 4, "body": f"Codex owner: {THREAD_ID}", "pull_request": {}},
    ]
    comments = [
        {"issue_url": "https://api.github.com/repos/example/project/issues/1",
         "body": f"Codex session: {OTHER_ID}"},
        {"issue_url": "https://api.github.com/repos/example/project/issues/3",
         "body": f"Codex owner: {THREAD_ID}\nCodex owner: {{{OTHER_ID}}}"},
        {"issue_url": "https://api.github.com/repos/example/project/issues/3",
         "body": f"Codex session: {THREAD_ID}"},
    ]
    monkeypatch.setattr(ding, "gh_records", lambda resource:
                        comments if "issues/comments" in resource else issues)
    assert ding.issue_map() == {THREAD_ID: {1, 2}, OTHER_ID: {2, 3}}


@pytest.mark.parametrize("owner", ["", "invalid"])
def test_issue_map_omits_invalid_latest_owner_without_losing_other_issues(monkeypatch, capsys, owner):
    monkeypatch.setattr(ding, "github_repository", lambda: "example/project")
    issues = [
        {"number": 1, "body": f"Codex owner: {THREAD_ID}"},
        {"number": 2, "body": f"Codex owner: {OTHER_ID}\nCodex session: {OTHER_ID}"},
        {"number": 3, "body": f"Codex session: {OTHER_ID}"},
    ]
    comments = [{"issue_url": "https://api.github.com/repos/example/project/issues/2",
                 "body": f"Codex owner: {owner}\nCodex session: {THREAD_ID}"}]
    monkeypatch.setattr(ding, "gh_records", lambda resource:
                        comments if "issues/comments" in resource else issues)
    assert ding.issue_map() == {THREAD_ID: {1}, OTHER_ID: {3}}
    warning = capsys.readouterr().err
    assert len(warning.splitlines()) == 1
    assert "issue #2" in warning and "invalid Codex owner" in warning


def test_board_issue_column_uses_owner_attribution(monkeypatch, capsys):
    monkeypatch.setattr(ding, "github_repository", lambda: "example/project")
    monkeypatch.setattr(ding, "gh_records", lambda resource: [] if "issues/comments" in resource else [
        {"number": 42, "body": f"Codex owner: {THREAD_ID}\nCodex session: {OTHER_ID}"},
    ])

    def call(method, params):
        if method == "thread/list":
            return {"data": [thread(), thread(OTHER_ID, "Other")]}
        assert method == "thread/turns/list"
        return {"data": [{"items": [final("Ready")]}]}

    monkeypatch.setattr(ding, "app_server", lambda path: server_calls(call))
    assert ding.main(["board", "--all", "--socket", "/test.sock"]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    assert [row.split("\t")[3] for row in captured.out.splitlines()[1:]] == ["#42", ""]


def test_gh_allows_lookup_over_ten_seconds(monkeypatch):
    def delayed_lookup(argv, *, capture_output, text, timeout):
        if timeout < 12:
            raise subprocess.TimeoutExpired(argv, timeout)
        return subprocess.CompletedProcess(argv, 0, '{"nameWithOwner": "example/project"}')

    monkeypatch.setattr(ding.subprocess, "run", delayed_lookup)
    assert ding.github_repository() == "example/project"


def test_issue_lookup_failure_is_visible(monkeypatch, capsys):
    def failed_lookup():
        raise subprocess.TimeoutExpired(["gh", "repo", "view"], 60)

    monkeypatch.setattr(ding, "github_repository", failed_lookup)
    assert ding.issue_map() == {}
    captured = capsys.readouterr()
    assert not captured.out
    assert "issue" in captured.err.lower()
    assert "fail" in captured.err.lower()


@pytest.mark.parametrize("reply, expected", [
    ("状态: 完成\n在等: 无\n\nDetails", "状态: 完成\n在等: 无"),
    ("Summary\r\n \t\r\nDetails", "Summary"),
    ("Only one paragraph", "Only one paragraph"),
    ("", ""),
])
def test_reply_head(reply, expected):
    assert ding.reply_head(reply) == expected


def test_read_head_cli(monkeypatch, capsys):
    monkeypatch.setattr(ding, "query", lambda *a, **kw: "状态: 完成\n在等: 无\n\nLong details")
    assert ding.main(["read", THREAD_ID, "--head", "--socket", "/test.sock"]) == 0
    assert capsys.readouterr().out == "状态: 完成\n在等: 无\n"


@pytest.mark.parametrize("all_threads", [False, True])
def test_board_repo_worktree_scope_and_final_only(monkeypatch, tmp_path, capsys, all_threads):
    repo, worktree = tmp_path / "repo", tmp_path / "linked worktree"
    third_id = "00000000-0000-4000-8000-000000000003"
    values = [thread(cwd=str(repo / "src"), status="active"),
              thread(OTHER_ID, "Other", str(worktree / "src")),
              thread(third_id, "Outside", str(tmp_path / "repo-other"))]
    ws = Connection([
        {"result": {}},
        *([] if all_threads else [
            {"result": {"data": [value["id"] for value in values]}},
            *[{"result": {"thread": value}} for value in values],
        ]),
        {"result": {"data": values}},
        page([final("状态: 完成\n在等: 无\n\nLong details")]), page([]),
        *([page([final("Outside reply")])] if all_threads else []),
    ])
    monkeypatch.setattr(ding, "repository_roots", lambda: [repo, worktree])
    monkeypatch.setattr(ding, "app_server", lambda path: server_calls(ding.initialize(ws)))
    monkeypatch.setattr(ding, "issue_map", lambda: {THREAD_ID: {1}})
    assert ding.main(["board", "--socket", "/test.sock", *(["--all"] if all_threads else [])]) == 0
    output = capsys.readouterr().out
    lines = output.splitlines()
    assert len(lines) == (4 if all_threads else 3)
    assert lines[1].split("\t")[2:] == ["active", "#1", "状态: 完成 在等: 无"]
    assert lines[2].split("\t")[-1] == ""
    assert "first user message" not in output
    assert "Long details" not in output
    assert ("Outside" in output) == all_threads
    assert [request["method"] for request in ws.requests].count("initialize") == 1
    assert all(request["method"] in {"initialize", "initialized", "thread/list", "thread/turns/list",
                                                "thread/loaded/list", "thread/read"}
               for request in ws.requests)


def test_board_malformed_reply_fails_without_partial_output(monkeypatch, capsys):
    replies = iter([{"data": [thread()]}, {"data": [{"items": [final(None)]}]}])
    monkeypatch.setattr(ding, "app_server", lambda path: server_calls(lambda *a: next(replies)))
    assert ding.main(["board", "--all", "--socket", "/test.sock"]) == 1
    captured = capsys.readouterr()
    assert not captured.out
    assert "Malformed App Server final reply" in captured.err


def test_board_default_excludes_unloaded_threads(monkeypatch, tmp_path, capsys):
    values = [thread(cwd=str(tmp_path)),
              thread(OTHER_ID, "Unloaded", str(tmp_path), "notLoaded")]

    def call(method, params):
        if method == "thread/loaded/list":
            return {"data": [THREAD_ID]}
        if method == "thread/read":
            return {"thread": values[0]}
        if method == "thread/list":
            return {"data": values}
        assert method == "thread/turns/list"
        return {"data": [{"items": [final("Ready")]}]}

    monkeypatch.setattr(ding, "repository_roots", lambda: [tmp_path])
    monkeypatch.setattr(ding, "app_server", lambda path: server_calls(call))
    monkeypatch.setattr(ding, "issue_map", lambda: {})
    assert ding.main(["board", "--socket", "/test.sock"]) == 0
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 2
    assert "Ready" in output
    assert "Unloaded" not in output


def test_repository_roots_include_linked_worktrees(monkeypatch, tmp_path):
    repo = tmp_path / "repository"
    linked = tmp_path / "linked worktree"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "--allow-empty", "-qm", "initial"], check=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "--detach", "-q", str(linked)], check=True)
    subdirectory = linked / "src"
    subdirectory.mkdir()
    monkeypatch.chdir(subdirectory)
    assert set(ding.repository_roots()) == {repo, linked}


def test_issue_and_positional_target_are_exclusive():
    with pytest.raises(SystemExit) as exc:
        ding.main(["read", THREAD_ID, "--issue", "1"])
    assert exc.value.code == 2


@pytest.mark.parametrize("sender, expected", [
    (None, "hello\n世界"), ("reviewer", "[from reviewer]\nhello\n世界"),
])
def test_message_source_prefix(sender, expected):
    assert ding.message_text("hello\n世界", None, sender) == expected


@pytest.mark.parametrize("sender", ["", " ", "two\nlines", "two\rlines"])
def test_source_requires_one_nonempty_line(sender):
    with pytest.raises(ValueError, match="single line"):
        ding.message_text("message", None, sender)


@pytest.mark.parametrize("input_kind", ["file", "stdin"])
def test_send_long_message_inputs_and_private_log(tmp_path, monkeypatch, input_kind):
    message = 'text "quoted"; $(not-shell)\r\n世界\n'
    queued = []
    monkeypatch.setattr(ding, "deliver_message", lambda *args: queued.append(args[:3]) or {"outcome": "started", "error": None})
    if input_kind == "file":
        file = tmp_path / "message.txt"
        file.write_bytes(message.encode())
        options = ["--message-file", str(file)]
    else:
        monkeypatch.setattr(ding.sys, "stdin", io.StringIO(message))
        options = ["--message", "-"]
    assert ding.main(["send", THREAD_ID, "--socket", "/test.sock", *options, "--from", "reviewer"]) == 0
    actual = f"[from reviewer]\n{message}"
    assert queued == [(THREAD_ID, actual, "/test.sock")]
    journal = tmp_path / "state/ding/sent.jsonl"
    intent, event = records(journal)
    assert event["sender"] == "reviewer"
    assert event["thread_id"] == THREAD_ID
    assert event["message"] == actual
    assert event["outcome"] == "started"
    assert intent["client_message_id"] == event["client_message_id"]
    assert event["error"] is None
    assert event["timestamp_ns"] > 0
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.parent.stat().st_mode) == 0o700


def test_send_log_tightens_existing_file_permissions(tmp_path, monkeypatch):
    journal = tmp_path / "state/ding/sent.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"old": true}\n')
    journal.chmod(0o644)
    monkeypatch.setattr(ding, "deliver_message", lambda *args: {"outcome": "started", "error": None})
    assert ding.main(["send", THREAD_ID, "--socket", "/test.sock", "--message", "message"]) == 0
    assert records(journal)[0] == {"old": True}
    assert len(records(journal)) == 3
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600


@pytest.mark.parametrize("error, outcome, code", [
    (FileNotFoundError("socket missing"), "failed", 1),
    (TimeoutError("message confirmation timed out"), "unknown", 1),
    (KeyboardInterrupt(), "unknown", 130),
])
def test_send_failures_are_logged_without_retries(tmp_path, monkeypatch, capsys, error, outcome, code):
    calls = []

    def call(method, params):
        calls.append(method)
        if method == "thread/read" and outcome != "failed":
            return {"thread": thread()}
        if method == "thread/turns/list":
            return {"data": []}
        raise error

    monkeypatch.setattr(ding, "app_server", lambda path, **kwargs: server_calls(call))
    assert ding.main(["send", THREAD_ID, "--socket", "/test.sock", "--message", "secret-message"]) == code
    assert calls.count("thread/queue/add") <= 1
    intent, event = records(tmp_path / "state/ding/sent.jsonl")
    assert event["client_message_id"] == intent["client_message_id"]
    assert event["outcome"] == outcome
    assert event["error"]
    assert "secret-message" not in capsys.readouterr().err


def test_send_missing_target_is_logged_before_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(ding, "resolve_target", lambda *a: (_ for _ in ()).throw(ValueError("No candidates")))
    monkeypatch.setattr(ding, "deliver_message", lambda *a: pytest.fail("must not queue"))
    assert ding.main(["send", "Missing", "--message", "message"]) == 1
    event, = records(tmp_path / "state/ding/sent.jsonl")
    assert event["target"] == "Missing"
    assert event["thread_id"] is None
    assert event["outcome"] == "failed"


def test_send_result_log_failure_preserves_intent_without_repeat(monkeypatch, capsys, tmp_path):
    calls = []
    append = ding.append_jsonl

    def persist(path, event):
        if event["event"] == "send_result":
            raise OSError("disk full")
        append(path, event)

    monkeypatch.setattr(ding, "deliver_message", lambda *args: calls.append(args) or
                        {"outcome": "started", "error": None})
    monkeypatch.setattr(ding, "append_jsonl", persist)
    assert ding.main(["send", THREAD_ID, "--socket", "/test.sock", "--message", "message"]) == 1
    assert len(calls) == 1
    intent, = records(tmp_path / "state/ding/sent.jsonl")
    assert intent["event"] == "send_intent"
    error = capsys.readouterr().err
    assert "sent log failed" in error
    assert "delivery outcome: started" in error
    assert "do not blindly resend" in error


def test_send_unwritable_log_prevents_delivery(monkeypatch, tmp_path):
    state_file = tmp_path / "not-a-directory"
    state_file.touch()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_file))
    monkeypatch.setattr(ding, "deliver_message", lambda *a: pytest.fail("must not queue"))
    assert ding.main(["send", THREAD_ID, "--socket", "/test.sock", "--message", "message"]) == 1


def test_run_message_file_and_source_preserve_task_exit(tmp_path, monkeypatch):
    message = tmp_path / "message.txt"
    message.write_text("done\n世界", encoding="utf-8")
    journal = tmp_path / "exit.jsonl"
    calls = []
    monkeypatch.setattr(ding, "task_exit_code", lambda *args: (7, None))
    monkeypatch.setattr(ding, "query", lambda *args, **kwargs: {"type": "idle"})
    monkeypatch.setattr(ding, "deliver_message", lambda *args, **kwargs: calls.append(args[:3]) or {"outcome": "started", "error": None})
    assert ding.main(["run", THREAD_ID, "--socket", "/test.sock", "--message-file", str(message),
                      "--from", "worker", "--record", str(journal), "--", "task"]) == 7
    assert calls == [(THREAD_ID, "[from worker]\ndone\n世界", "/test.sock")]
    assert records(journal)[0]["exit_code"] == 7
    assert not (tmp_path / "state/ding/sent.jsonl").exists()


def test_message_file_error_prevents_task_or_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(ding, "task_exit_code", lambda *args: pytest.fail("must not start task"))
    assert ding.main(["run", THREAD_ID, "--message-file", str(tmp_path / "missing"),
                      "--record", str(tmp_path / "exit.jsonl"), "--", "task"]) == 1


def test_message_inputs_are_exclusive(tmp_path):
    with pytest.raises(SystemExit) as exc:
        ding.main(["send", THREAD_ID, "--message", "text", "--message-file", str(tmp_path / "file")])
    assert exc.value.code == 2


def test_rpc_waits_over_fifteen_seconds_but_notifications_do_not_reset_deadline(monkeypatch):
    elapsed = [0]
    monkeypatch.setattr(ding.time, "monotonic", lambda: elapsed[0])

    class DelayedConnection(Connection):
        def settimeout(self, timeout):
            self.timeout = timeout
            super().settimeout(timeout)

        def recv(self):
            delay = 20
            if self.timeout < delay:
                raise TimeoutError("Timed out")
            elapsed[0] += delay
            return super().recv()

    ws = DelayedConnection([{"result": {}}, {"result": {}},
                            '{"method": "notice"}', '{"method": "notice"}',
                            '{"method": "notice"}'])
    call = ding.initialize(ws)
    assert call("thread/read", {}) == {}
    with pytest.raises(TimeoutError, match="Timed out"):
        call("thread/read", {})
    assert elapsed[0] == 100


def test_thread_list_keeps_filters_on_every_page():
    calls = []

    def call(method, params):
        calls.append((method, params))
        return {"data": [], "nextCursor": "next" if len(calls) == 1 else None}

    assert ding.list_threads(call, cwd=["/repo/src"]) == []
    assert len(calls) == 2
    for method, params in calls:
        assert method == "thread/list"
        assert params["cwd"] == ["/repo/src"]
        assert params["archived"] is False
    assert calls[1][1]["cursor"] == "next"


def test_board_loaded_pagination_raw_paths_and_current_rows(tmp_path):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    raw_cwd = str(alias / "src")
    calls = []
    new_id = "00000000-0000-4000-8000-000000000003"
    values = [thread(new_id, "Newly loaded", raw_cwd, "active"),
              thread(OTHER_ID, "Unloaded", raw_cwd, "notLoaded"),
              thread(THREAD_ID, "Failed", raw_cwd, "systemError"),
              thread("00000000-0000-4000-8000-000000000004", "Outside", str(tmp_path))]

    def call(method, params):
        calls.append((method, params))
        if method == "thread/loaded/list":
            if "cursor" not in params:
                return {"data": [THREAD_ID], "nextCursor": "next"}
            assert params["cursor"] == "next"
            return {"data": [THREAD_ID, OTHER_ID]}
        if method == "thread/read":
            assert params["includeTurns"] is False
            return {"thread": thread(params["threadId"], cwd=raw_cwd, status="systemError")}
        assert method == "thread/list"
        assert params["cwd"] == [raw_cwd]
        assert params["archived"] is False
        return {"data": values}

    assert ding.board_threads(call, [repo]) == [ding.thread_metadata(values[0])]
    assert [params["threadId"] for method, params in calls if method == "thread/read"] == [
        THREAD_ID, OTHER_ID,
    ]


@pytest.mark.parametrize("loaded_page", [
    {}, {"data": None}, {"data": [None]}, {"data": [12]}, {"data": ["invalid"]},
    {"data": [], "nextCursor": ""}, {"data": [], "nextCursor": 1},
    {"data": [], "nextCursor": "repeated"},
])
def test_board_rejects_bad_loaded_pages(loaded_page, tmp_path):
    with pytest.raises(ValueError, match="Malformed App Server"):
        ding.board_threads(lambda *args: loaded_page, [tmp_path])


@pytest.mark.parametrize("metadata", [
    None, thread(OTHER_ID), thread(name=5), thread(cwd="relative"), thread(status=None),
])
def test_board_rejects_bad_loaded_metadata(metadata, tmp_path):
    def call(method, params):
        if method == "thread/loaded/list":
            return {"data": [THREAD_ID]}
        assert method == "thread/read"
        return {"thread": metadata}

    with pytest.raises(ValueError, match="Malformed App Server|different thread ID"):
        ding.board_threads(call, [tmp_path])


@pytest.mark.parametrize("case", ["roots", "loaded", "outside"])
def test_empty_board_never_falls_back_to_global_list(case, tmp_path):
    def call(method, params):
        if method == "thread/loaded/list":
            return {"data": [] if case == "loaded" else [THREAD_ID]}
        assert method == "thread/read"
        return {"thread": thread(cwd="/outside")}

    assert ding.board_threads(call, [] if case == "roots" else [tmp_path]) == []


@pytest.mark.parametrize("name", ['Quoted "name"', "Line\nbreak", "Back\\slash", "世界"])
def test_displayed_name_matches_globally(monkeypatch, name):
    calls = []

    def call(method, params):
        calls.append(params)
        assert method == "thread/list"
        assert "searchTerm" not in params
        assert "cwd" not in params
        return {"data": [thread(name=name)]}

    monkeypatch.setattr(ding, "app_server", lambda path: server_calls(call))
    assert ding.resolve_target(ding.display_cell(name), None, "/test.sock") == THREAD_ID
    assert len(calls) == 1


def test_name_search_preserves_global_ambiguity(monkeypatch):
    def call(method, params):
        assert "searchTerm" not in params
        assert "cwd" not in params
        if "cursor" not in params:
            return {"data": [thread()], "nextCursor": "next"}
        return {"data": [thread(OTHER_ID, cwd="/other-repo")]}

    monkeypatch.setattr(ding, "app_server", lambda path: server_calls(call))
    with pytest.raises(ValueError, match="Found 2") as exc:
        ding.resolve_target("Build", None, "/test.sock")
    assert OTHER_ID in str(exc.value)


@pytest.mark.parametrize("target", ["Missing", r"Invalid\q"])
def test_name_search_fallback_keeps_global_candidates(monkeypatch, target):
    calls = []

    def call(method, params):
        calls.append(params)
        return {"data": [] if "searchTerm" in params else [thread()]}

    monkeypatch.setattr(ding, "app_server", lambda path: server_calls(call))
    with pytest.raises(ValueError, match="Found 0") as exc:
        ding.resolve_target(target, None, "/test.sock")
    assert THREAD_ID in str(exc.value)
    assert "searchTerm" not in calls[-1]
    assert len(calls) == 1


@pytest.mark.parametrize("error", [FileNotFoundError("gh"), ValueError("bad JSON\nmore detail"),
                                  subprocess.TimeoutExpired(["gh"], 60)])
def test_board_issue_failure_warns_and_still_succeeds(monkeypatch, capsys, error):
    def fail():
        raise error

    monkeypatch.setattr(ding, "github_repository", fail)
    monkeypatch.setattr(ding, "app_server", lambda path: server_calls(lambda *a: {"data": []}))
    assert ding.main(["board", "--all", "--socket", "/test.sock"]) == 0
    output = capsys.readouterr()
    assert output.out == "NAME\tCWD\tSTATUS\tISSUE\tHEAD\n"
    assert len(output.err.splitlines()) == 1
    assert output.err.startswith("ding: issue lookup failed:")


@pytest.mark.parametrize("all_threads", [False, True])
def test_board_all_includes_unloaded_errors_and_outside(monkeypatch, tmp_path, capsys, all_threads):
    values = [thread(cwd=str(tmp_path)),
              thread(OTHER_ID, "Unloaded", str(tmp_path), "notLoaded"),
              thread("00000000-0000-4000-8000-000000000003", "Error", str(tmp_path), "systemError"),
              thread("00000000-0000-4000-8000-000000000004", "Outside", "/outside")]
    replies = []

    def call(method, params):
        if method == "thread/loaded/list":
            assert not all_threads
            return {"data": [THREAD_ID]}
        if method == "thread/read":
            assert not all_threads
            return {"thread": values[0]}
        if method == "thread/list":
            assert params["archived"] is False
            assert ("cwd" in params) is not all_threads
            return {"data": values}
        assert method == "thread/turns/list"
        replies.append(params["threadId"])
        return {"data": []}

    monkeypatch.setattr(ding, "repository_roots", lambda: [tmp_path])
    monkeypatch.setattr(ding, "app_server", lambda path: server_calls(call))
    monkeypatch.setattr(ding, "issue_map", lambda: {})
    assert ding.main(["board", "--socket", "/test.sock", *(["--all"] if all_threads else [])]) == 0
    assert replies == ([value["id"] for value in values] if all_threads else [THREAD_ID])
    assert len(capsys.readouterr().out.splitlines()) == (5 if all_threads else 2)


@pytest.mark.parametrize("returncode, stdout, stderr", [
    (1, "", "authentication failed"), (0, "invalid json", ""),
])
def test_gh_failure_leaves_issue_map_empty_with_warning(monkeypatch, capsys, returncode, stdout, stderr):
    def run(argv, *, capture_output, text, timeout):
        assert argv[:3] == ["gh", "repo", "view"]
        assert capture_output and text
        assert timeout == 60
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    monkeypatch.setattr(ding.subprocess, "run", run)
    assert ding.issue_map() == {}
    assert capsys.readouterr().err.startswith("ding: issue lookup failed:")


@pytest.mark.parametrize("case", ["text", "fragmented", "invalid-text", "invalid-close"])
def test_app_server_utf8_validation_and_fragments(tmp_path, monkeypatch, case):
    websocket = pytest.importorskip("websocket")
    frames = websocket.ABNF
    response = json.dumps({"id": 2, "result": {"answer": "中文"}}, ensure_ascii=False).encode("utf-8")

    def frame(data, opcode=frames.OPCODE_TEXT, fin=1):
        return frames(fin=fin, opcode=opcode, mask_value=0, data=data).format()

    if case == "fragmented":
        split = response.index("中".encode("utf-8")) + 1
        payload = frame(response[:split], fin=0) + frame(response[split:], frames.OPCODE_CONT)
    elif case == "invalid-text":
        payload = frame(b'\xff')
    elif case == "invalid-close":
        payload = frame(b'\x03\xe8\xff', frames.OPCODE_CLOSE)
    else:
        payload = frame(response)
    payload = frame(b'{"id":1,"result":{}}') + payload + frame(b'\x03\xe8', frames.OPCODE_CLOSE)
    path = str(tmp_path / "server")
    peers = []
    connections = []
    with socket.socket(socket.AF_UNIX) as server:
        server.bind(path)
        server.listen(1)

        def connect(url, **kwargs):
            peer, _ = server.accept()
            peers.append(peer)
            peer.sendall(payload)
            ws = websocket.WebSocket()
            ws.sock = kwargs["socket"]
            ws.connected = True
            connections.append(ws)
            return ws

        monkeypatch.setattr(websocket, "create_connection", connect)
        try:
            if case.startswith("invalid"):
                with pytest.raises(ValueError, match="utf-8|Invalid close frame"):
                    with ding.app_server(path) as call:
                        call("thread/read", {})
            else:
                with ding.app_server(path) as call:
                    assert call("thread/read", {}) == {"answer": "中文"}
            assert connections[0].cont_frame.skip_utf8_validation is True
            assert connections[0].frame_buffer.skip_utf8_validation is False
            assert not connections[0].connected
        finally:
            for peer in peers:
                peer.close()


MESSAGE_ID = "00000000-0000-4000-8000-000000000003"


def started_turn(client_id=MESSAGE_ID, status="inProgress"):
    return {"id": "turn-1", "status": status, "items": [
        {"type": "userMessage", "clientId": client_id, "content": [{"type": "text", "text": "message"}]},
    ]}


def delivery_server(monkeypatch, *, status="idle", resumed="idle", failure=None,
                    result_client=MESSAGE_ID, turn_status="inProgress", history=None):
    calls = []
    current_status = [status]
    submitted = [False]

    def call(method, params):
        calls.append((method, params))
        if method == "thread/read":
            return {"thread": thread(status=current_status[0])}
        if method == "thread/resume":
            assert params == {"threadId": THREAD_ID, "excludeTurns": True}
            current_status[0] = resumed
            return {"thread": thread(status=resumed)}
        if method == "thread/queue/add":
            submitted[0] = True
            assert params == {"threadId": THREAD_ID, "clientUserMessageId": MESSAGE_ID,
                              "input": [{"type": "text", "text": "message"}]}
            if failure == "add":
                raise OSError("lost add acknowledgement")
            return {"queuedSubmission": {"id": "submission-1", "clientUserMessageId": MESSAGE_ID,
                                         "input": params["input"]}}
        if method == "thread/queue/start":
            assert params == {"threadId": THREAD_ID, "queuedSubmissionId": "submission-1"}
            if failure == "start":
                raise OSError("lost start acknowledgement")
            return {"turn": started_turn(result_client, turn_status)}
        if method == "thread/turns/list":
            return {"data": (history or []) if submitted[0] else []}
        pytest.fail(f"unexpected mutation {method}")

    monkeypatch.setattr(ding, "app_server", lambda path, **kwargs: server_calls(call))
    return calls


@pytest.mark.parametrize("status", ["idle", "notLoaded"])
@pytest.mark.parametrize("turn_status", ["inProgress", "completed"])
def test_delivery_resumes_and_starts_exact_submission_without_codex(monkeypatch, status, turn_status):
    monkeypatch.setenv("PATH", "")
    calls = delivery_server(monkeypatch, status=status, turn_status=turn_status)
    result = ding.deliver_message(THREAD_ID, "message", "/test.sock", MESSAGE_ID, timeout=0.01)
    assert result["outcome"] == "started"
    assert result["turn_id"] == "turn-1"
    assert result["queued_submission_id"] == "submission-1"
    methods = [method for method, _ in calls]
    assert methods == (["thread/read", "thread/resume"] if status == "notLoaded" else ["thread/read"]) + [
        "thread/turns/list", "thread/queue/add", "thread/turns/list", "thread/read", "thread/queue/start",
    ]


def test_resume_active_race_skips_run_without_queue(monkeypatch):
    calls = delivery_server(monkeypatch, status="notLoaded", resumed="active")
    result = ding.deliver_message(THREAD_ID, "message", "/test.sock", MESSAGE_ID,
                                  skip_active=True, timeout=0.01)
    assert result["outcome"] == "skipped_active"
    assert [method for method, _ in calls] == ["thread/read", "thread/resume"]


@pytest.mark.parametrize("status, failure", [("idle", "add"), ("idle", "start"), ("active", "add")])
@pytest.mark.parametrize("observed", [False, True])
def test_lost_ack_reconciles_readonly_and_never_resends(monkeypatch, status, failure, observed):
    calls = delivery_server(monkeypatch, status=status, failure=failure,
                            history=[started_turn()] if observed else [])
    result = ding.deliver_message(THREAD_ID, "message", "/test.sock", MESSAGE_ID, timeout=0.01)
    assert result["outcome"] == ("started" if observed else "unknown")
    assert sum(method == "thread/queue/add" for method, _ in calls) == 1
    assert sum(method == "thread/queue/start" for method, _ in calls) <= 1


def test_unrelated_turn_does_not_confirm_own_message(monkeypatch):
    calls = delivery_server(monkeypatch, result_client=OTHER_ID, history=[started_turn(OTHER_ID)])
    result = ding.deliver_message(THREAD_ID, "message", "/test.sock", MESSAGE_ID, timeout=0.01)
    assert result["outcome"] == "unknown"
    assert sum(method == "thread/queue/start" for method, _ in calls) == 1


@pytest.mark.parametrize("initial_status", ["active", "notLoaded"])
@pytest.mark.parametrize("flags", [[], ["waitingOnUserInput"]])
def test_active_send_acknowledged_immediately(tmp_path, monkeypatch, capsys, initial_status, flags):
    calls = []
    record = tmp_path / "state/ding/sent.jsonl"
    monkeypatch.setattr(ding.time, "sleep", lambda seconds: pytest.fail("active send must not wait"))

    def call(method, params):
        calls.append(method)
        intent, = records(record)
        assert intent["event"] == "send_intent"
        if method in {"thread/read", "thread/resume"}:
            value = thread(status=initial_status if method == "thread/read" else "active")
            value["status"]["activeFlags"] = flags
            return {"thread": value}
        assert method == "thread/queue/add"
        assert params["clientUserMessageId"] == intent["client_message_id"]
        assert params["input"] == [{"type": "text", "text": intent["message"]}]
        return {"queuedSubmission": {"id": "own-q", "clientUserMessageId": intent["client_message_id"],
                                     "input": params["input"]}}

    monkeypatch.setattr(ding, "app_server", lambda path, **kwargs: server_calls(call))
    assert ding.main(["send", THREAD_ID, "--socket", "/test.sock", "--message", "message"]) == 0
    intent, result = records(record)
    assert result["outcome"] == "queued_behind_active"
    assert result["error"] is None
    assert result["queued_submission_id"] == "own-q"
    assert result["turn_id"] is None
    assert result["turn_status"] is None
    assert result["client_message_id"] == intent["client_message_id"]
    assert calls == (["thread/read", "thread/resume"] if initial_status == "notLoaded" else ["thread/read"]) + [
        "thread/queue/add",
    ]
    assert "queued_behind_active" in capsys.readouterr().err


@pytest.mark.parametrize("reject", ["add", "history", "start"])
def test_known_rejection_preserves_acknowledged_queue(monkeypatch, reject):
    calls = []

    def call(method, params):
        calls.append(method)
        if method == "thread/read":
            return {"thread": thread()}
        if method == "thread/queue/add":
            if reject == "add":
                raise ding.AppServerError("message rejected")
            return {"queuedSubmission": {"id": "q", "clientUserMessageId": MESSAGE_ID,
                                         "input": params["input"]}}
        if method == "thread/turns/list":
            if reject == "history" and "thread/queue/add" in calls:
                raise ding.AppServerError("history unavailable")
            return {"data": []}
        if method == "thread/queue/start":
            raise ding.AppServerError("session became active")
        pytest.fail(method)

    monkeypatch.setattr(ding, "app_server", lambda path, **kwargs: server_calls(call))
    result = ding.deliver_message(THREAD_ID, "message", "/test.sock", MESSAGE_ID, timeout=0.01)
    assert result["outcome"] == ("failed" if reject == "add" else "queued")
    assert calls.count("thread/queue/add") == 1
    assert calls.count("thread/queue/start") <= 1


@pytest.mark.parametrize("observed", [False, True])
def test_idle_send_becomes_active_accepts_queue_without_start_or_wait(monkeypatch, observed):
    calls = []
    reads = ["idle", "active"]
    submitted = [False]
    monkeypatch.setattr(ding.time, "sleep", lambda seconds: pytest.fail("active send must not wait"))

    def call(method, params):
        calls.append((method, params))
        if method == "thread/read":
            return {"thread": thread(status=reads.pop(0))}
        if method == "thread/queue/add":
            submitted[0] = True
            return {"queuedSubmission": {"id": "own-q", "clientUserMessageId": MESSAGE_ID,
                                         "input": params["input"]}}
        if method == "thread/turns/list":
            return {"data": [started_turn()] if observed and submitted[0] else []}
        pytest.fail(method)

    monkeypatch.setattr(ding, "app_server", lambda path, **kwargs: server_calls(call))
    result = ding.deliver_message(THREAD_ID, "message", "/test.sock", MESSAGE_ID, timeout=0.1)
    assert result["outcome"] == ("started" if observed else "queued_behind_active")
    assert result["error"] is None
    assert result["turn_id"] == ("turn-1" if observed else None)
    assert sum(method == "thread/queue/add" for method, _ in calls) == 1
    assert not any(method == "thread/queue/start" for method, _ in calls)


@pytest.mark.parametrize("field, value", [
    ("id", None), ("clientUserMessageId", OTHER_ID),
    ("input", [{"type": "text", "text": "different"}]),
])
@pytest.mark.parametrize("status", ["idle", "active"])
def test_invalid_submission_echo_never_starts_an_item(monkeypatch, field, value, status):
    calls = []

    def call(method, params):
        calls.append(method)
        if method == "thread/read":
            return {"thread": thread(status=status)}
        if method == "thread/queue/add":
            submission = {"id": "q", "clientUserMessageId": MESSAGE_ID, "input": params["input"]}
            submission[field] = value
            return {"queuedSubmission": submission}
        assert method == "thread/turns/list"
        return {"data": []}

    monkeypatch.setattr(ding, "app_server", lambda path, **kwargs: server_calls(call))
    result = ding.deliver_message(THREAD_ID, "message", "/test.sock", MESSAGE_ID, timeout=0.01)
    assert result["outcome"] == "unknown"
    assert calls.count("thread/queue/add") == 1
    assert "thread/queue/start" not in calls


def test_canonical_text_elements_echo_is_accepted(monkeypatch):
    def call(method, params):
        if method == "thread/read":
            return {"thread": thread()}
        if method == "thread/queue/add":
            return {"queuedSubmission": {"id": "q", "clientUserMessageId": MESSAGE_ID,
                                         "input": [{"type": "text", "text": "message", "text_elements": []}]}}
        if method == "thread/turns/list":
            return {"data": []}
        assert method == "thread/queue/start"
        turn = started_turn()
        turn["items"][0]["content"][0]["text_elements"] = []
        return {"turn": turn}

    monkeypatch.setattr(ding, "app_server", lambda path, **kwargs: server_calls(call))
    assert ding.deliver_message(THREAD_ID, "message", "/test.sock", MESSAGE_ID)["outcome"] == "started"


@pytest.mark.parametrize("field, value", [("id", ""), ("status", "unknown"), ("content", [])])
def test_invalid_own_turn_never_confirms_execution(monkeypatch, field, value):
    turn = started_turn()
    if field == "content":
        turn["items"][0][field] = value
    else:
        turn[field] = value
    delivery_server(monkeypatch, history=[turn])
    result = ding.deliver_message(THREAD_ID, "message", "/test.sock", MESSAGE_ID, timeout=0.01)
    assert result["outcome"] == "unknown"


@pytest.mark.parametrize("resumed", [thread(OTHER_ID), thread(status="notLoaded"), thread(status="systemError")])
def test_invalid_resume_never_delivers_message(monkeypatch, resumed):
    calls = []

    def call(method, params):
        calls.append(method)
        if method == "thread/read":
            return {"thread": thread(status="notLoaded")}
        assert method == "thread/resume"
        return {"thread": resumed}

    monkeypatch.setattr(ding, "app_server", lambda path, **kwargs: server_calls(call))
    assert ding.deliver_message(THREAD_ID, "message", "/test.sock", MESSAGE_ID)["outcome"] == "failed"
    assert calls == ["thread/read", "thread/resume"]


def test_history_confirmation_checks_all_pages_and_cursors():
    calls = []

    def call(method, params):
        assert method == "thread/turns/list"
        calls.append(params)
        if "cursor" not in params:
            return {"data": [started_turn(OTHER_ID)], "nextCursor": "older"}
        assert params["cursor"] == "older"
        return {"data": [started_turn(status="completed")]}

    assert ding.started_message(call, THREAD_ID, MESSAGE_ID, "message")["status"] == "completed"
    assert len(calls) == 2
    with pytest.raises(ValueError, match="pagination cursor"):
        ding.started_message(lambda *args: {"data": [], "nextCursor": "same"}, THREAD_ID, MESSAGE_ID, "message")


def test_delivery_rpc_uses_one_deadline_despite_notifications(monkeypatch):
    elapsed = [0]
    monkeypatch.setattr(ding.time, "monotonic", lambda: elapsed[0])
    ws = Connection([{"result": {}}, {"result": {}}])
    call = ding.initialize(ws, deadline=10)
    elapsed[0] = 11
    with pytest.raises(TimeoutError):
        call("thread/queue/add", {})
    assert [request["method"] for request in ws.requests] == ["initialize", "initialized"]


def test_confirmation_stops_at_presubmit_baseline_without_older_pages():
    calls = []
    baseline = started_turn(OTHER_ID)
    baseline["id"] = "before-submission"

    def call(method, params):
        calls.append(params)
        assert "cursor" not in params
        return {"data": [baseline], "nextCursor": "many-old-turns"}

    assert ding.started_message(call, THREAD_ID, MESSAGE_ID, "message",
                                after_turn_id="before-submission") is None
    assert ding.started_message(call, THREAD_ID, MESSAGE_ID, "message",
                                after_turn_id="before-submission") is None
    assert len(calls) == 2


@pytest.mark.parametrize("stage", ["status", "start"])
@pytest.mark.parametrize("invalid", [[], {}])
def test_malformed_delivery_status_preserves_task_exit(tmp_path, monkeypatch, stage, invalid):
    journal = tmp_path / "exit.jsonl"
    calls = []

    def call(method, params):
        calls.append(method)
        if method == "thread/read":
            return {"thread": thread(status=invalid if stage == "status" else "idle")}
        if method == "thread/turns/list":
            return {"data": []}
        if method == "thread/queue/add":
            return {"queuedSubmission": {"id": "own-item",
                "clientUserMessageId": params["clientUserMessageId"], "input": params["input"]}}
        assert method == "thread/queue/start"
        return {"turn": {"id": "own-turn", "status": invalid, "items": []}}

    monkeypatch.setattr(ding, "task_exit_code", lambda command: (7, None))
    monkeypatch.setattr(ding, "app_server", lambda path, **kwargs: server_calls(call))
    assert ding.main(["run", THREAD_ID, "--socket", "/test.sock", "--message", "message",
                      "--record", str(journal), "--", "task"]) == 7
    exit_event, notification = records(journal)
    assert exit_event["exit_code"] == 7
    assert notification["outcome"] == ("failed" if stage == "status" else "unknown")
    assert calls.count("thread/queue/add") == (0 if stage == "status" else 1)
    assert calls.count("thread/queue/start") == (0 if stage == "status" else 1)


@pytest.mark.parametrize("error", [None, "rejected", {}, {"code": -1}, {"code": True, "message": "bad"}])
def test_malformed_rpc_error_is_not_a_definite_rejection(error):
    call = ding.initialize(Connection([{"result": {}}, {"error": error}]))
    with pytest.raises(ValueError, match="Malformed App Server error") as exc:
        call("thread/queue/add", {})
    assert not isinstance(exc.value, ding.AppServerError)


def test_short_issue_target_outside_repository_explains_alternatives(monkeypatch):
    def missing():
        raise ValueError("gh failed: not a git repository")

    monkeypatch.setattr(ding, "github_repository", missing)
    with pytest.raises(ValueError, match=r"elsewhere use OWNER/REPO#42 or the issue URL"):
        ding.resolve_target("#42", None, None)
