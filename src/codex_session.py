"""Read, inspect, and message Codex sessions; notify after a foreground task exits."""

import argparse
import json
import os
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


@contextmanager
def writer(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
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


def initialize(ws):
    deadline = time.monotonic() + 60
    request_id = 0

    def call(method, params):
        nonlocal request_id
        request_id += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Timed out reading the Codex session.")
        ws.settimeout(min(15, remaining))
        ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out reading the Codex session.")
            ws.settimeout(min(15, remaining))
            raw = ws.recv()
            if not raw:
                raise ValueError("App Server closed the connection before replying.")
            response = json.loads(raw)
            if not isinstance(response, dict):
                raise ValueError("Malformed App Server response.")
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise ValueError(f"App Server error: {response['error']}")
            result = response.get("result")
            if not isinstance(result, dict):
                raise ValueError("Malformed App Server result.")
            return result

    call("initialize", {
        "clientInfo": {"name": "codex_session", "version": "1"},
        "capabilities": {"experimentalApi": True},
    })
    ws.send(json.dumps({"method": "initialized"}))
    return call


def read_status(ws, thread_id: str) -> dict:
    call = initialize(ws)
    result = call("thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = result.get("thread")
    status = thread.get("status") if isinstance(thread, dict) else None
    if not isinstance(status, dict) or not isinstance(status.get("type"), str):
        raise ValueError("Malformed App Server thread status.")
    return status


def read_reply(ws, thread_id: str) -> str:
    call = initialize(ws)
    cursor = None
    seen_cursors = set()
    while True:
        params = {
            "threadId": thread_id, "limit": 10,
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
            raise ValueError("No final reply found in this thread.")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise ValueError("Malformed App Server pagination cursor.")
        seen_cursors.add(cursor)


def query(thread_id: str, path: str, *, status_only: bool = False):
    try:
        import websocket
    except ImportError as exc:
        raise ValueError("Missing websocket-client; reinstall codex-session with its dependencies.") from exc
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(15)
            sock.connect(path)
            ws = websocket.create_connection(
                "ws://localhost/", socket=sock, timeout=15, suppress_origin=True,
            )
            try:
                return read_status(ws, thread_id) if status_only else read_reply(ws, thread_id)
            finally:
                ws.close()
    except websocket.WebSocketException as exc:
        raise ValueError(str(exc)) from exc


def socket_path(explicit: str | None) -> str:
    return explicit or discover_socket(Path(f"/tmp/codex-daemon-{os.getuid()}"))


def queue_message(thread_id: str, message: str, path: str | None) -> int:
    return subprocess.run([
        "codex", "queue", "--remote", f"unix://{path or ''}",
        "--thread", thread_id, "--message", message,
    ], timeout=60).returncode


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
        append_jsonl(record, {
            "event": "task_exit", "run_id": run_id, "thread_id": args.thread_id,
            "exit_code": code, "error": error,
            "timestamp_ns": time.time_ns(),
        })
        notification = {
            "event": "notification", "run_id": run_id, "thread_id": args.thread_id,
            "status": None, "status_error": None, "queue_exit_code": None,
            "queue_error": None,
        }
        path = args.socket
        try:
            path = socket_path(path)
            notification["status"] = query(args.thread_id, path, status_only=True)
        except (OSError, ValueError, KeyboardInterrupt) as exc:
            notification["status_error"] = str(exc) or type(exc).__name__
        status = notification["status"]
        if isinstance(status, dict) and status.get("type") == "active":
            notification["outcome"] = "skipped_active"
        else:
            try:
                queue_code = queue_message(args.thread_id, args.message, path)
                notification["queue_exit_code"] = queue_code
                notification["outcome"] = "queued" if queue_code == 0 else "queue_failed"
            except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                notification["queue_error"] = str(exc) or type(exc).__name__
                notification["outcome"] = "queue_failed"
        notification["timestamp_ns"] = time.time_ns()
        try:
            append_jsonl(record, notification)
        except OSError as exc:
            print(f"codex-session: notification record failed ({record}): {exc}", file=sys.stderr)
            return code
        print(f"codex-session: {notification['outcome']}; record: {record}", file=sys.stderr)
        return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("read", "status", "send", "run"):
        sub = commands.add_parser(action)
        sub.add_argument("thread_id", help="Codex thread UUID")
        sub.add_argument("--socket", help="Existing local App Server Unix socket path")
        if action in {"send", "run"}:
            sub.add_argument("--message", required=True)
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
    try:
        args.thread_id = str(UUID(args.thread_id))
    except ValueError:
        parser.error("thread_id must be a valid UUID")
    if args.action == "run" and not command:
        parser.error("run requires a task command after --")
    try:
        if args.action == "run":
            return run_task(args, command)
        path = socket_path(args.socket)
        if args.action == "send":
            code = queue_message(args.thread_id, args.message, path)
            return 128 - code if code < 0 else code
        result = query(args.thread_id, path, status_only=args.action == "status")
        print(json.dumps(result, ensure_ascii=False) if args.action == "status" else result)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"codex-session: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
