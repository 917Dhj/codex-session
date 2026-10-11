import builtins
import json
import io
import stat
import socket
import subprocess
from contextlib import contextmanager

import pytest

import ding


THREAD_ID = "00000000-0000-4000-8000-000000000001"
OTHER_ID = "00000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def isolated_state_and_no_real_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    run = subprocess.run

    def guarded_run(argv, *args, **kwargs):
        if list(argv[:2]) == ["codex", "queue"]:
            pytest.fail("tests must not queue messages to real sessions")
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)


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
    assert ding.read_reply(ding.initialize(ws), THREAD_ID) == "answer"
    assert [r["method"] for r in ws.requests] == [
        "initialize", "initialized", "thread/turns/list",
    ]
    assert ws.requests[0]["params"]["capabilities"] == {"experimentalApi": True}
    assert ws.requests[2]["params"] == {
        "threadId": THREAD_ID, "limit": 10, "sortDirection": "desc", "itemsView": "full",
    }


def test_pagination():
    ws = Connection([{"result": {}}, page([], "next"), page([final("answer")])])
    assert ding.read_reply(ding.initialize(ws), THREAD_ID) == "answer"
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


def test_send_direct_queue_argv(monkeypatch):
    calls = []

    def queue(argv, *, timeout):
        calls.append(argv)
        assert timeout == 60
        return ding.subprocess.CompletedProcess(argv, 9)

    monkeypatch.setattr(ding.subprocess, "run", queue)
    monkeypatch.setattr(ding, "query", lambda *a, **kw: pytest.fail("send must not query status"))
    message = 'text "quoted"; $(not-shell)\nnext'
    assert ding.main(["send", THREAD_ID, "--socket", "/test.sock", "--message", message]) == 9
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

    monkeypatch.setattr(ding, "task_exit_code", child)
    monkeypatch.setattr(ding, "query", query)
    monkeypatch.setattr(ding, "queue_message", queue)
    assert ding.main([
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
    monkeypatch.setattr(ding, "task_exit_code", lambda command: (0, None))

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

    monkeypatch.setattr(ding, "discover_socket", discover)
    monkeypatch.setattr(ding, "query", failed_query)
    monkeypatch.setattr(ding, "queue_message", queue)
    assert ding.main(["run", THREAD_ID, "--message", "m", "--record", str(path), "--", "task"]) == 0
    assert queued == (["/resolved.sock"] if resolved else [None])
    assert records(path)[1]["status_error"]


@pytest.mark.parametrize("queue_result", [8, FileNotFoundError("codex missing")])
def test_real_child_output_and_queue_failure(tmp_path, monkeypatch, capfd, queue_result):
    path = tmp_path / "record.jsonl"
    counter = tmp_path / "calls.txt"
    queue_calls = []
    monkeypatch.setattr(ding, "query", lambda *args, **kwargs: {"type": "idle"})

    def queue(*args):
        queue_calls.append(args)
        if isinstance(queue_result, Exception):
            raise queue_result
        return queue_result

    monkeypatch.setattr(ding, "queue_message", queue)
    code = "import pathlib,sys; pathlib.Path(sys.argv[1]).open('a').write('run\\n'); print('out'); print('err', file=sys.stderr); sys.exit(6)"
    assert ding.main([
        "run", THREAD_ID, "--socket", "/test.sock", "--message", "m", "--record", str(path),
        "--", ding.sys.executable, "-c", code, str(counter),
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
    monkeypatch.setattr(ding, "query", lambda *args, **kwargs: {"type": "idle"})

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(ding, "queue_message", interrupt)
    assert ding.main([
        "run", THREAD_ID, "--socket", "/test.sock", "--message", "m", "--record", str(path), "--", "task",
    ]) == 7
    assert records(path)[1]["queue_error"] == "KeyboardInterrupt"


def test_notification_record_failure_preserves_task_exit(tmp_path, monkeypatch, capsys):
    path = tmp_path / "record.jsonl"
    append_jsonl = ding.append_jsonl
    monkeypatch.setattr(ding, "task_exit_code", lambda command: (7, None))
    monkeypatch.setattr(ding, "query", lambda *args, **kwargs: {"type": "active"})

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


def test_uuid_and_issue_targets_are_local(monkeypatch):
    monkeypatch.setattr(ding, "app_server", lambda *a: pytest.fail("must not connect"))
    monkeypatch.setattr(ding, "socket_path", lambda *a: pytest.fail("must not discover socket"))
    issues = []

    def issue_session(number):
        issues.append(number)
        return OTHER_ID

    monkeypatch.setattr(ding, "issue_session", issue_session)
    assert ding.resolve_target(THREAD_ID.upper(), None, None) == THREAD_ID
    assert ding.resolve_target("#42", None, None) == OTHER_ID
    assert ding.resolve_target(None, 7, None) == OTHER_ID
    assert issues == [42, 7]
    with pytest.raises(ValueError, match="positive issue number"):
        ding.resolve_target("#zero", None, None)


@pytest.mark.parametrize("target, names, expected", [
    ("Build", ["Build", "Other"], THREAD_ID),
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


def test_issue_last_reference_across_all_comment_pages(monkeypatch):
    calls = []

    def gh(arguments):
        calls.append(arguments)
        if arguments[0] == "repo":
            return {"nameWithOwner": "example/project"}
        if "--paginate" not in arguments:
            return {"body": f"Codex session: {THREAD_ID}\n"}
        return [[{"body": "No session here"}], [{"body": f"Codex session: {OTHER_ID}\r\n"}]]

    monkeypatch.setattr(ding, "gh_json", gh)
    assert ding.issue_session(42) == OTHER_ID
    assert "--slurp" in calls[-1]
    assert calls[-1][-1].endswith("/42/comments?per_page=100")
    monkeypatch.setattr(ding, "gh_records", lambda *a: [{"body": "Codex session: invalid"}])
    with pytest.raises(ValueError, match="last Codex session line"):
        ding.issue_session(42)


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
        {"result": {}}, {"result": {"data": values}},
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
    assert all(request["method"] in {"initialize", "initialized", "thread/list", "thread/turns/list"}
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
    monkeypatch.setattr(ding, "queue_message", lambda *args: queued.append(args) or 0)
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
    event, = records(journal)
    assert event["sender"] == "reviewer"
    assert event["thread_id"] == THREAD_ID
    assert event["message"] == actual
    assert event["outcome"] == "queued"
    assert event["queue_exit_code"] == 0
    assert event["error"] is None
    assert event["timestamp_ns"] > 0
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.parent.stat().st_mode) == 0o700


def test_send_log_tightens_existing_file_permissions(tmp_path, monkeypatch):
    journal = tmp_path / "state/ding/sent.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"old": true}\n')
    journal.chmod(0o644)
    monkeypatch.setattr(ding, "queue_message", lambda *args: 0)
    assert ding.main(["send", THREAD_ID, "--socket", "/test.sock", "--message", "message"]) == 0
    assert records(journal)[0] == {"old": True}
    assert len(records(journal)) == 2
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600


@pytest.mark.parametrize("error, outcome, code", [
    (FileNotFoundError("codex missing"), "queue_failed", 1),
    (subprocess.TimeoutExpired(["codex", "queue", "secret-message"], 60), "unknown", 1),
    (KeyboardInterrupt(), "unknown", 130),
])
def test_send_failures_are_logged_without_retries(tmp_path, monkeypatch, capsys, error, outcome, code):
    calls = []

    def failed_queue(*args):
        calls.append(args)
        raise error

    monkeypatch.setattr(ding, "queue_message", failed_queue)
    assert ding.main(["send", THREAD_ID, "--socket", "/test.sock", "--message", "secret-message"]) == code
    assert len(calls) == 1
    event, = records(tmp_path / "state/ding/sent.jsonl")
    assert event["outcome"] == outcome
    assert event["queue_exit_code"] is None
    assert event["error"]
    assert "secret-message" not in capsys.readouterr().err


def test_send_missing_target_is_logged_before_any_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(ding, "resolve_target", lambda *a: (_ for _ in ()).throw(ValueError("No candidates")))
    monkeypatch.setattr(ding, "queue_message", lambda *a: pytest.fail("must not queue"))
    assert ding.main(["send", "Missing", "--message", "message"]) == 1
    event, = records(tmp_path / "state/ding/sent.jsonl")
    assert event["target"] == "Missing"
    assert event["thread_id"] is None
    assert event["outcome"] == "queue_failed"


def test_send_success_with_log_failure_reports_queue_outcome(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ding, "queue_message", lambda *args: calls.append(args) or 0)
    monkeypatch.setattr(ding, "append_jsonl", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    assert ding.main(["send", THREAD_ID, "--socket", "/test.sock", "--message", "message"]) == 1
    assert len(calls) == 1
    error = capsys.readouterr().err
    assert "sent log failed" in error
    assert "queue outcome: queued" in error
    assert "exit code: 0" in error


def test_send_unwritable_log_prevents_queue(monkeypatch, tmp_path):
    state_file = tmp_path / "not-a-directory"
    state_file.touch()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_file))
    monkeypatch.setattr(ding, "queue_message", lambda *a: pytest.fail("must not queue"))
    assert ding.main(["send", THREAD_ID, "--socket", "/test.sock", "--message", "message"]) == 1


def test_run_message_file_and_source_preserve_task_exit(tmp_path, monkeypatch):
    message = tmp_path / "message.txt"
    message.write_text("done\n世界", encoding="utf-8")
    journal = tmp_path / "exit.jsonl"
    calls = []
    monkeypatch.setattr(ding, "task_exit_code", lambda *args: (7, None))
    monkeypatch.setattr(ding, "query", lambda *args, **kwargs: {"type": "idle"})
    monkeypatch.setattr(ding, "queue_message", lambda *args: calls.append(args) or 0)
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
