"""Message agent sessions and wake them after tasks via Codex App Server."""

import argparse
import ast
import json
import os
import re
import signal
import subprocess
from pathlib import Path
import socket
import sys
import time
from uuid import UUID, uuid4

from contextlib import contextmanager
from typing import Iterator

import fcntl


class AppServerError(ValueError):
    pass


class NoFinalReply(ValueError):
    pass


@contextmanager
def writer(path: Path, *, blocking: bool = False) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND, 0o600), "a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise RuntimeError(
                f"another writer is active for {path.parent}"
            ) from error
        yield


def append_jsonl(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def discover_socket(directory: Path) -> str:
    candidates = sorted(path for path in directory.glob("*") if path.is_socket())
    if len(candidates) != 1:
        raise ValueError(
            f"Found {len(candidates)} App Server sockets in {directory}; "
            "pass --socket with an existing App Server socket path."
        )
    return str(candidates[0])


def initialize(ws, *, deadline: float | None = None):
    request_id = 0

    def call(method, params):
        nonlocal request_id
        call_deadline = min(time.monotonic() + 60, deadline) if deadline is not None else time.monotonic() + 60
        request_id += 1
        remaining = call_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out reading the Codex session.")
        ws.settimeout(remaining)
        ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
        while True:
            remaining = call_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out reading the Codex session.")
            ws.settimeout(remaining)
            raw = ws.recv()
            if not raw:
                raise ValueError("App Server closed the connection before replying.")
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise ValueError("Malformed App Server response.")
            if response.get("id") != request_id:
                continue
            if "error" in response:
                error = response["error"]
                if (not isinstance(error, dict) or type(error.get("code")) is not int
                        or not isinstance(error.get("message"), str)):
                    raise ValueError("Malformed App Server error.")
                raise AppServerError(f"App Server error: {error}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise ValueError("Malformed App Server result.")
            return result

    call("initialize", {
        "clientInfo": {"name": "ding", "version": "1"},
        "capabilities": {"experimentalApi": True},
    })
    ws.send(json.dumps({"method": "initialized"}))
    return call


def read_status(call, thread_id: str) -> dict:
    result = call("thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = result.get("thread")
    status = thread.get("status") if isinstance(thread, dict) else None
    if not isinstance(status, dict) or not isinstance(status.get("type"), str):
        raise ValueError("Malformed App Server thread status.")
    return status


def read_reply(call, thread_id: str) -> str:
    cursor = None
    seen_cursors = set()
    while True:
        params = {
            "threadId": thread_id, "limit": 1,
            "sortDirection": "desc", "itemsView": "full",
        }
        if cursor is not None:
            params["cursor"] = cursor
        page = call("thread/turns/list", params)
        turns = page.get("data")
        if not isinstance(turns, list):
            raise ValueError("Malformed App Server turns page.")
        for turn in turns:
            if not isinstance(turn, dict) or not isinstance(turn.get("items"), list):
                raise ValueError("Malformed App Server turn.")
            for item in reversed(turn["items"]):
                if not isinstance(item, dict):
                    raise ValueError("Malformed App Server item.")
                if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                    if not isinstance(item.get("text"), str):
                        raise ValueError("Malformed App Server final reply.")
                    return item["text"]
        cursor = page.get("nextCursor")
        if cursor is None:
            raise NoFinalReply("No final reply found in this thread.")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise ValueError("Malformed App Server pagination cursor.")
        seen_cursors.add(cursor)


@contextmanager
def app_server(path: str, *, deadline: float | None = None):
    try:
        import websocket
    except ImportError as exc:
        raise ValueError("Missing websocket-client; reinstall dkitchen with its dependencies.") from exc
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            timeout = min(15, deadline - time.monotonic()) if deadline is not None else 15
            if timeout <= 0:
                raise TimeoutError("Timed out confirming message execution.")
            sock.settimeout(timeout)
            sock.connect(path)
            ws = websocket.create_connection(
                "ws://localhost/", socket=sock, timeout=timeout, suppress_origin=True,
            )
            # recv() strictly decodes text as UTF-8; keep frame validation for close reasons.
            ws.cont_frame.skip_utf8_validation = True
            try:
                yield initialize(ws, deadline=deadline)
            finally:
                ws.close()
    except websocket.WebSocketException as exc:
        raise ValueError(str(exc)) from exc


def query(thread_id: str, path: str, *, status_only: bool = False):
    with app_server(path) as call:
        return read_status(call, thread_id) if status_only else read_reply(call, thread_id)


def thread_metadata(thread: object) -> dict:
    if not isinstance(thread, dict):
        raise ValueError("Malformed App Server thread.")
    try:
        thread_id = str(UUID(thread["id"]))
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Malformed App Server thread ID.") from exc
    name, cwd, status = thread.get("name"), thread.get("cwd"), thread.get("status")
    if (name is not None and not isinstance(name, str)
            or not isinstance(cwd, str) or not os.path.isabs(cwd)
            or not isinstance(status, dict) or not isinstance(status.get("type"), str)):
        raise ValueError("Malformed App Server thread metadata.")
    return {"id": thread_id, "name": name or thread_id, "cwd": cwd, "status": status}


def list_threads(call, *, cwd: list[str] | None = None) -> list[dict]:
    threads = {}
    cursor = None
    seen_cursors = set()
    while True:
        params = {
            "limit": 100, "archived": False, "sortKey": "updated_at",
            "sortDirection": "desc", "modelProviders": [],
            "sourceKinds": ["cli", "vscode", "exec", "appServer", "subAgent",
                            "subAgentReview", "subAgentCompact", "subAgentThreadSpawn",
                            "subAgentOther", "unknown"],
            "useStateDbOnly": True,
        }
        if cursor is not None:
            params["cursor"] = cursor
        if cwd is not None:
            params["cwd"] = cwd
        page = call("thread/list", params)
        if not isinstance(page.get("data"), list):
            raise ValueError("Malformed App Server threads page.")
        for thread in page["data"]:
            metadata = thread_metadata(thread)
            threads.setdefault(metadata["id"], metadata)
        cursor = page.get("nextCursor")
        if cursor is None:
            return list(threads.values())
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise ValueError("Malformed App Server pagination cursor.")
        seen_cursors.add(cursor)


def board_threads(call, roots: list[Path]) -> list[dict]:
    if not roots:
        return []
    loaded = {}
    cursor = None
    seen_cursors = set()
    while True:
        params = {"limit": 100}
        if cursor is not None:
            params["cursor"] = cursor
        page = call("thread/loaded/list", params)
        if not isinstance(page.get("data"), list):
            raise ValueError("Malformed App Server loaded threads page.")
        for value in page["data"]:
            try:
                loaded[str(UUID(value))] = None
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("Malformed App Server loaded thread ID.") from exc
        cursor = page.get("nextCursor")
        if cursor is None:
            break
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise ValueError("Malformed App Server pagination cursor.")
        seen_cursors.add(cursor)
    directories = set()
    for thread_id in loaded:
        result = call("thread/read", {"threadId": thread_id, "includeTurns": False})
        thread = thread_metadata(result.get("thread"))
        if thread["id"] != thread_id:
            raise ValueError("App Server returned a different thread ID.")
        if any(Path(thread["cwd"]).resolve().is_relative_to(root) for root in roots):
            directories.add(thread["cwd"])
    if not directories:
        return []
    return [thread for thread in list_threads(call, cwd=sorted(directories))
            if thread["status"]["type"] in {"idle", "active"} and any(
                Path(thread["cwd"]).resolve().is_relative_to(root) for root in roots)]


def repository_roots() -> list[Path]:
    cwd = Path.cwd().resolve()
    try:
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return [cwd]
    if root.returncode:
        return [cwd]
    result = subprocess.run(["git", "-c", "core.quotePath=false", "worktree", "list", "--porcelain"],
                            capture_output=True, text=True, timeout=10, check=True)
    paths = [field[len("worktree "):] for field in result.stdout.splitlines()
             if field.startswith("worktree ")]
    return [Path(ast.literal_eval(path) if path.startswith('"') else path).resolve() for path in paths]


def gh_json(arguments: list[str]):
    result = subprocess.run(["gh", *arguments], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise ValueError(f"gh failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def gh_records(resource: str) -> list[dict]:
    pages = gh_json(["api", "--paginate", "--slurp", resource])
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("Malformed gh pagination result.")
    records = [record for page in pages for record in page]
    if any(not isinstance(record, dict) for record in records):
        raise ValueError("Malformed gh record.")
    return records


def github_repository() -> str:
    result = gh_json(["repo", "view", "--json", "nameWithOwner"])
    repo = result.get("nameWithOwner") if isinstance(result, dict) else None
    if not isinstance(repo, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("Malformed gh repository name.")
    return repo


def session_references(body: str | None, label: str = "session") -> list[str]:
    if body is None:
        return []
    if not isinstance(body, str):
        raise ValueError("Malformed gh issue body.")
    return [value.strip().strip("`") for value in re.findall(
        rf"(?m)^[ \t]*Codex {re.escape(label)}:[ \t]*([^\r\n]*)$", body.replace("\r\n", "\n"))]


def issue_references(issue: dict, comments: list[dict]) -> tuple[str | None, dict, bool]:
    owner, candidates, malformed = None, {}, False
    for index, record in enumerate([issue, *comments]):
        owners = session_references(record.get("body"), "owner")
        if owners:
            owner = owners[-1]
        references = set()
        for value in session_references(record.get("body")):
            try:
                references.add(str(UUID(value)))
            except ValueError:
                malformed = True
        for thread_id in references:
            candidate = candidates.setdefault(thread_id, {"comments": 0})
            if index:
                candidate["comments"] += 1
            candidate["timestamp"] = record.get("created_at")
            candidate["url"] = record.get("html_url")
    return owner, candidates, malformed


def issue_session(number: int, path: str | None = None) -> str:
    repo = github_repository()
    issue = gh_json(["api", f"repos/{repo}/issues/{number}"])
    if not isinstance(issue, dict):
        raise ValueError("Malformed gh issue.")
    owner, candidates, malformed = issue_references(
        issue, gh_records(f"repos/{repo}/issues/{number}/comments?per_page=100"))
    if owner is not None:
        try:
            return str(UUID(owner))
        except ValueError as exc:
            raise ValueError(f"The last Codex owner line in issue #{number} is not a valid UUID.") from exc
    if malformed:
        raise ValueError(f"A Codex session line in issue #{number} is not a valid UUID.")
    if not candidates:
        raise ValueError(f"No Codex session line in issue #{number}. Candidates: none.")
    if len(candidates) == 1:
        return next(iter(candidates))
    metadata = {}
    try:
        with app_server(socket_path(path)) as call:
            try:
                metadata = {thread["id"]: thread for thread in list_threads(call)}
            except (OSError, ValueError):
                pass
            for thread_id in candidates:
                if thread_id in metadata:
                    continue
                try:
                    result = call("thread/read", {"threadId": thread_id, "includeTurns": False})
                    thread = thread_metadata(result.get("thread"))
                    if thread["id"] == thread_id:
                        metadata[thread_id] = thread
                except (OSError, ValueError):
                    pass
    except (OSError, ValueError):
        pass
    rows = ["  ID\tNAME\tSTATUS\tCOMMENTS\tLAST_COMMENT\tLINK"]
    for thread_id, candidate in sorted(candidates.items()):
        thread = metadata.get(thread_id, {})
        cells = [thread_id, thread.get("name", "unknown"),
                 thread.get("status", {}).get("type", "unknown"), str(candidate["comments"]),
                 candidate["timestamp"], candidate["url"]]
        rows.append("  " + "\t".join(display_cell(cell) if isinstance(cell, str) and cell
                                  else "unknown" for cell in cells))
    raise ValueError(
        f"Issue #{number} references {len(candidates)} different Codex sessions without a Codex owner line. "
        "Candidates:\n" + "\n".join(rows) + "\nSpecify a session UUID or exact session name instead.")


def issue_map() -> dict[str, set[int]]:
    try:
        repo = github_repository()
        issues = gh_records(f"repos/{repo}/issues?state=all&per_page=100")
        comments = gh_records(f"repos/{repo}/issues/comments?per_page=100")
        records = {issue["number"]: (issue, []) for issue in issues if "pull_request" not in issue}
        for comment in comments:
            number = int(comment["issue_url"].rsplit("/", 1)[-1])
            if number in records:
                records[number][1].append(comment)
        mapping = {}
        for number, (issue, comments) in records.items():
            owner, candidates, _ = issue_references(issue, comments)
            if owner is not None:
                try:
                    candidates = [str(UUID(owner))]
                except ValueError:
                    print(f"ding: issue #{number} has an invalid Codex owner; omitted from board.",
                          file=sys.stderr)
                    continue
            for thread_id in candidates:
                mapping.setdefault(thread_id, set()).add(number)
        return mapping
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.TimeoutExpired) as exc:
        print(f"ding: issue lookup failed: {' '.join(str(exc).split())}", file=sys.stderr)
        return {}


def display_cell(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)[1:-1]


def resolve_target(target: str | None, issue: int | None, path: str | None) -> str:
    if issue is not None:
        return issue_session(issue, path)
    try:
        return str(UUID(target))
    except (ValueError, TypeError, AttributeError):
        pass
    if target and target.startswith("#"):
        if not re.fullmatch(r"#[1-9][0-9]*", target):
            raise ValueError("Issue target must be # followed by a positive issue number.")
        return issue_session(int(target[1:]), path)
    with app_server(socket_path(path)) as call:
        threads = list_threads(call)
    matches = [thread for thread in threads if display_cell(thread["name"]) == target]
    if len(matches) == 1:
        return matches[0]["id"]
    candidates = matches or threads
    listing = "\n".join(f"  {display_cell(thread['name'])} ({thread['id']}) {display_cell(thread['cwd'])}"
                        for thread in candidates) or "  none"
    raise ValueError(f"Found {len(matches)} sessions named {target!r}. Candidates:\n{listing}")


def reply_head(text: str) -> str:
    return re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n"), maxsplit=1)[0].strip()


def show_board(path: str, *, all_threads: bool = False) -> None:
    roots = [] if all_threads else repository_roots()
    with app_server(path) as call:
        threads = list_threads(call) if all_threads else board_threads(call, roots)
        rows = []
        for thread in threads:
            try:
                head = reply_head(read_reply(call, thread["id"]))
            except NoFinalReply:
                head = ""
            rows.append([thread["name"], thread["cwd"], thread["status"]["type"],
                         thread["id"], " ".join(head.splitlines())])
    mapping = issue_map()
    print("NAME\tCWD\tSTATUS\tISSUE\tHEAD")
    for name, cwd, status, thread_id, head in rows:
        issue = ",".join(f"#{number}" for number in sorted(mapping.get(thread_id, set())))
        print("\t".join(display_cell(cell) for cell in [name, cwd, status, issue, head]))


def socket_path(explicit: str | None) -> str:
    return explicit or discover_socket(Path(f"/tmp/codex-daemon-{os.getuid()}"))


def same_message(inputs: object, message: str) -> bool:
    return (isinstance(inputs, list) and len(inputs) == 1 and isinstance(inputs[0], dict)
            and inputs[0].get("type") == "text" and inputs[0].get("text") == message
            and inputs[0].get("text_elements", []) == [])


def message_turn(turn: object, client_id: str, message: str) -> dict | None:
    if (not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]
            or not isinstance(turn.get("status"), str)
            or turn.get("status") not in {"inProgress", "completed", "interrupted", "failed"}
            or not isinstance(turn.get("items"), list)):
        raise ValueError("Malformed App Server turn.")
    for item in turn["items"]:
        if not isinstance(item, dict):
            raise ValueError("Malformed App Server item.")
        if item.get("type") == "userMessage" and item.get("clientId") == client_id:
            if not same_message(item.get("content"), message):
                raise ValueError("App Server returned different message content.")
            return turn
    return None


def started_message(call, thread_id: str, client_id: str, message: str,
                    *, after_turn_id: str | None = None) -> dict | None:
    cursor = None
    seen = set()
    while True:
        params = {"threadId": thread_id, "limit": 100, "sortDirection": "desc", "itemsView": "full"}
        if cursor is not None:
            params["cursor"] = cursor
        page = call("thread/turns/list", params)
        if not isinstance(page.get("data"), list):
            raise ValueError("Malformed App Server turns page.")
        for turn in page["data"]:
            found = message_turn(turn, client_id, message)
            if found is not None:
                return found
            if turn["id"] == after_turn_id:
                return None
        cursor = page.get("nextCursor")
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise ValueError("Malformed App Server pagination cursor.")
        seen.add(cursor)


def delivery_status(call, thread_id: str, *, resume: bool = False) -> dict:
    params = {"threadId": thread_id, **({"excludeTurns": True} if resume else {"includeTurns": False})}
    result = call("thread/resume" if resume else "thread/read", params)
    thread = result.get("thread")
    if not isinstance(thread, dict) or thread.get("id") != thread_id:
        raise ValueError("App Server returned a different thread ID.")
    status = thread.get("status")
    if (not isinstance(status, dict) or not isinstance(status.get("type"), str)
            or status["type"] not in {"notLoaded", "idle", "active"}):
        raise ValueError("App Server session is not in a deliverable state.")
    if resume and status["type"] == "notLoaded":
        raise ValueError("App Server did not load the session.")
    return status


def deliver_message(thread_id: str, message: str, path: str, client_id: str,
                    *, skip_active: bool = False, timeout: float = 60) -> dict:
    result = {"outcome": "failed", "status": None, "error": None,
              "queued_submission_id": None, "turn_id": None, "turn_status": None}
    deadline = time.monotonic() + timeout
    mutation = None
    baseline = None

    def confirm(turn):
        result.update(outcome="started", turn_id=turn["id"], turn_status=turn["status"])
        return result

    try:
        with app_server(path, deadline=deadline) as call:
            result["status"] = delivery_status(call, thread_id)
            if result["status"]["type"] == "notLoaded":
                result["status"] = delivery_status(call, thread_id, resume=True)
            if skip_active and result["status"]["type"] == "active":
                result["outcome"] = "skipped_active"
                return result
            page = call("thread/turns/list", {
                "threadId": thread_id, "limit": 1, "sortDirection": "desc", "itemsView": "full",
            })
            if not isinstance(page.get("data"), list):
                raise ValueError("Malformed App Server turns page.")
            if page["data"]:
                message_turn(page["data"][0], client_id, message)
                baseline = page["data"][0]["id"]
            mutation = "add"
            result["outcome"] = "unknown"
            inputs = [{"type": "text", "text": message}]
            submission = call("thread/queue/add", {
                "threadId": thread_id, "clientUserMessageId": client_id, "input": inputs,
            }).get("queuedSubmission")
            if (not isinstance(submission, dict) or not isinstance(submission.get("id"), str)
                    or not submission["id"] or submission.get("clientUserMessageId") != client_id
                    or not same_message(submission.get("input"), message)):
                raise ValueError("Malformed App Server queued submission.")
            result.update(outcome="queued", queued_submission_id=submission["id"])
            while time.monotonic() < deadline:
                found = started_message(call, thread_id, client_id, message, after_turn_id=baseline)
                if found is not None:
                    return confirm(found)
                result["status"] = delivery_status(call, thread_id)
                if result["status"]["type"] == "idle" and mutation == "add":
                    mutation = "start"
                    result["outcome"] = "unknown"
                    turn = call("thread/queue/start", {
                        "threadId": thread_id, "queuedSubmissionId": submission["id"],
                    }).get("turn")
                    found = message_turn(turn, client_id, message)
                    if found is not None:
                        return confirm(found)
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as exc:
        result["error"] = str(exc) or type(exc).__name__
        if isinstance(exc, AppServerError):
            result["outcome"] = "queued" if result["queued_submission_id"] is not None else "failed"
        elif mutation is not None:
            result["outcome"] = "unknown"
        if mutation is not None and not isinstance(exc, KeyboardInterrupt) and time.monotonic() < deadline:
            try:
                # A lost acknowledgement cannot be retried safely on the same connection.
                with app_server(path, deadline=deadline) as call:
                    found = started_message(call, thread_id, client_id, message, after_turn_id=baseline)
                    if found is not None:
                        return confirm(found)
            except (OSError, ValueError, RuntimeError, KeyboardInterrupt):
                pass
    if result["outcome"] in {"queued", "unknown"} and result["error"] is None:
        result["error"] = "Timed out confirming message execution."
    return result


def message_text(message: str | None, message_file: str | None, sender: str | None) -> str:
    if message_file is not None:
        with Path(message_file).open(encoding="utf-8", newline="") as handle:
            message = handle.read()
    elif message == "-":
        message = sys.stdin.read()
    if sender is not None:
        if not sender.strip() or "\n" in sender or "\r" in sender:
            raise ValueError("--from must be a non-empty single line.")
        message = f"[from {sender}]\n{message}"
    return message


def send_task(args) -> int:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    record = state / "ding/sent.jsonl"
    record.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with writer(record, blocking=True):
        record.chmod(0o600)
    event = {
        "event": "send_intent", "client_message_id": str(uuid4()), "sender": args.sender,
        "target": args.thread_id if args.issue is None else f"#{args.issue}",
        "thread_id": None, "message": args.message, "outcome": "failed", "error": None,
    }
    try:
        event["thread_id"] = resolve_target(args.thread_id, args.issue, args.socket)
        path = socket_path(args.socket)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        event["error"] = str(exc) or type(exc).__name__
    if event["error"] is None:
        event["outcome"] = "pending"
    event["timestamp_ns"] = time.time_ns()
    with writer(record, blocking=True):
        append_jsonl(record, event)
    if event["error"]:
        print(f"ding: {event['error']}", file=sys.stderr)
        return 1
    delivery = deliver_message(event["thread_id"], args.message, path, event["client_message_id"])
    event.update(delivery, event="send_result", timestamp_ns=time.time_ns())
    try:
        with writer(record, blocking=True):
            append_jsonl(record, event)
    except OSError as exc:
        print(f"ding: sent log failed ({record}); delivery outcome: {event['outcome']}; "
              f"do not blindly resend: {exc}", file=sys.stderr)
        return 1
    warning = "; do not blindly resend" if event["outcome"] in {"queued", "unknown"} else ""
    detail = f"; {event['error']}" if event["error"] else ""
    print(f"ding: {event['outcome']}{warning}{detail}; record: {record}", file=sys.stderr)
    return 0 if event["outcome"] == "started" else (130 if event["error"] == "KeyboardInterrupt" else 1)


def task_exit_code(command: list[str]) -> tuple[int, str | None]:
    try:
        child = subprocess.Popen(command)
    except OSError as exc:
        return (127 if isinstance(exc, FileNotFoundError) else 126), str(exc)
    try:
        code = child.wait()
    except KeyboardInterrupt:
        child.send_signal(signal.SIGINT)
        try:
            code = child.wait(timeout=5)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            child.kill()
            child.wait()
            code = -signal.SIGINT
    return (128 - code if code < 0 else code), None


def run_task(args, command: list[str]) -> int:
    record = Path(args.record)
    run_id = str(uuid4())
    with writer(record):
        code, error = task_exit_code(command)
        client_id = str(uuid4())
        append_jsonl(record, {
            "event": "task_exit", "run_id": run_id, "thread_id": args.thread_id,
            "client_message_id": client_id, "message": args.message,
            "exit_code": code, "error": error, "timestamp_ns": time.time_ns(),
        })
        notification = {
            "event": "notification", "run_id": run_id, "thread_id": args.thread_id,
            "client_message_id": client_id, "outcome": "failed", "error": None,
        }
        try:
            path = socket_path(args.socket)
            notification.update(deliver_message(args.thread_id, args.message, path, client_id,
                                                skip_active=True))
        except (OSError, ValueError, KeyboardInterrupt) as exc:
            notification["error"] = str(exc) or type(exc).__name__
        notification["timestamp_ns"] = time.time_ns()
        try:
            append_jsonl(record, notification)
        except OSError as exc:
            print(f"ding: notification record failed ({record}): {exc}", file=sys.stderr)
            return code
        warning = "; do not blindly resend" if notification["outcome"] in {"queued", "unknown"} else ""
        detail = f"; {notification['error']}" if notification["error"] else ""
        print(f"ding: {notification['outcome']}{warning}{detail}; record: {record}", file=sys.stderr)
        return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ding", description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    board = commands.add_parser("board", help="List sessions and their final reply summaries")
    board.add_argument("--all", action="store_true", help="Include all non-archived sessions")
    board.add_argument("--socket", help="Existing local App Server Unix socket path")
    for action in ("read", "status", "send", "run"):
        sub = commands.add_parser(action)
        target = sub.add_mutually_exclusive_group(required=True)
        target.add_argument("thread_id", nargs="?", help="Session UUID, exact name, or quoted '#NUMBER'")
        target.add_argument("--issue", type=int, help="Issue's latest Codex owner, or its unique Codex session")
        sub.add_argument("--socket", help="Existing local App Server Unix socket path")
        if action == "read":
            sub.add_argument("--head", action="store_true", help="Print only the first paragraph of the final reply")
        if action in {"send", "run"}:
            message = sub.add_mutually_exclusive_group(required=True)
            message.add_argument("--message", help="Message text, or '-' to read stdin")
            message.add_argument("--message-file", help="UTF-8 message file")
            sub.add_argument("--from", dest="sender", help="Prefix the message with '[from NAME]' on its own line")
        if action == "run":
            sub.add_argument("--record", required=True, help="Append-only local exit/notification JSONL")
            sub.epilog = "Task command follows --, for example: run UUID --message TEXT --record exit.jsonl -- python task.py"
    argv = list(sys.argv[1:] if argv is None else argv)
    command = []
    if argv and argv[0] == "run" and "--" in argv:
        separator = argv.index("--")
        command = argv[separator + 1:]
        argv = argv[:separator]
    args = parser.parse_args(argv)
    if args.action != "board" and args.issue is not None and args.issue <= 0:
        parser.error("--issue must be a positive issue number")
    if args.action == "run" and not command:
        parser.error("run requires a task command after --")
    try:
        if args.action == "board":
            show_board(socket_path(args.socket), all_threads=args.all)
            return 0
        if args.action in {"send", "run"}:
            args.message = message_text(args.message, args.message_file, args.sender)
        if args.action == "send":
            return send_task(args)
        args.thread_id = resolve_target(args.thread_id, args.issue, args.socket)
        if args.action == "run":
            return run_task(args, command)
        path = socket_path(args.socket)
        result = query(args.thread_id, path, status_only=args.action == "status")
        if args.action == "read" and args.head:
            result = reply_head(result)
        print(json.dumps(result, ensure_ascii=False) if args.action == "status" else result)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"ding: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
