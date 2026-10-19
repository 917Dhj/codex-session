"""Message agent sessions and wake them after tasks via Codex App Server."""

import argparse
import ast
from importlib import metadata
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
from pathlib import Path
import socket
import sys
import time
from uuid import UUID, uuid4

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import datetime
from typing import Iterator

import fcntl


ISSUE_REPOSITORY = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
STATUS_FIELDS = "Status,Waiting on,Key numbers,Next"
SHELL_PROGRAMS = {"sh", "bash", "dash", "zsh", "fish", "ksh", "csh", "tcsh"}


class AppServerError(ValueError):
    pass


class AppServerUnavailable(ValueError):
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
                raise AppServerUnavailable("App Server closed the connection before replying.")
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


def read_info(call, thread_id: str) -> dict:
    """A session's ID, name (None when unnamed), working directory, and status."""
    result = call("thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = result.get("thread")
    info = thread_metadata(thread)
    if info["id"] != thread_id:
        raise ValueError("App Server returned a different thread.")
    return {**info, "name": thread.get("name") or None}


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
        raise AppServerUnavailable(str(exc)) from exc


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


def ownership_note(repo: str, number: int, thread_id: str) -> str:
    return (f"You own ticket {repo}#{number}. Before anything else, post a comment on it "
            f"with the line `Codex owner: {thread_id}`.\n\n")


def ticket_owners(repo: str, number: int) -> list[str]:
    """Every distinct session a `Codex owner:` line on the ticket has named, oldest first."""
    issue = gh_json(["api", f"repos/{repo}/issues/{number}"])
    if not isinstance(issue, dict):
        raise ValueError("Malformed gh issue.")
    owners = []
    for record in [issue, *gh_records(f"repos/{repo}/issues/{number}/comments?per_page=100")]:
        for value in session_references(record.get("body"), "owner"):
            try:
                owner = str(UUID(value))
            except ValueError:
                continue
            if owner not in owners:
                owners.append(owner)
    return owners


def live_owners(call, owners: list[str]) -> list[dict]:
    """The owners whose sessions are not archived."""
    if not owners:
        return []
    live = {thread["id"]: thread for thread in list_threads(call)}
    return [live[owner] for owner in owners if owner in live]


def create_sub_issue(repo: str, parent: int, title: str, first_line: str) -> int:
    """Open a ticket whose body starts with first_line and attach it to parent as a sub-issue."""
    child = gh_json(["api", f"repos/{repo}/issues", "-X", "POST", "-f", f"title={title}", "-f", f"body={first_line}\n"])
    if not isinstance(child, dict) or not isinstance(child.get("number"), int) or not isinstance(child.get("id"), int):
        raise ValueError("Malformed gh issue.")
    try:
        gh_json(["api", f"repos/{repo}/issues/{parent}/sub_issues", "-X", "POST", "-F", f"sub_issue_id={child['id']}"])
    except ValueError as exc:
        raise ValueError(f"opened #{child['number']} but could not attach it to #{parent} ({exc}); attach it by hand, "
                         f"then rerun with --issue {child['number']}") from exc
    print(f"ding: opened #{child['number']} under #{parent}", file=sys.stderr)
    return child["number"]


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


def issue_session(number: int, path: str | None = None, repo: str | None = None) -> str:
    repo = repo if repo is not None else github_repository()
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


def normalize_target(target: str | None, issue: int | None) -> str | None:
    if issue is not None:
        target = f"#{issue}"
    if target and target.startswith("#"):
        if not re.fullmatch(r"#[1-9][0-9]*", target):
            raise ValueError("Issue target must be # followed by a positive issue number.")
        try:
            target = github_repository() + target
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise ValueError(
                f"'{target}' needs a GitHub repository in the current directory; "
                f"elsewhere use OWNER/REPO{target} or the issue URL. ({' '.join(str(exc).split())})"
            ) from exc
    if target and ("://" in target or target.startswith(("http:", "https:"))):
        match = re.fullmatch(
            rf"https://github\.com/({ISSUE_REPOSITORY})/(?:issues|pull)/([1-9][0-9]*)/?(?:\?[^#\s]*)?(?:#[^\r\n]*)?",
            target)
        if not match:
            raise ValueError("Issue URL must be https://github.com/OWNER/REPO/issues/NUMBER or /pull/NUMBER.")
        target = f"{match[1]}#{match[2]}"
    if target and re.match(r"[^\s/]*/", target) and "#" in target:
        match = re.fullmatch(rf"({ISSUE_REPOSITORY})#([1-9][0-9]*)", target)
        if not match or any(segment in {".", ".."} for segment in match[1].split("/")):
            raise ValueError("Issue target must be OWNER/REPO# followed by a positive issue number.")
    return target


def resolve_target(target: str | None, issue: int | None, path: str | None) -> str:
    target = normalize_target(target, issue)
    try:
        return str(UUID(target))
    except (ValueError, TypeError, AttributeError):
        pass
    if target and (match := re.fullmatch(rf"({ISSUE_REPOSITORY})#([1-9][0-9]*)", target)):
        return issue_session(int(match[2]), path, repo=match[1])
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


def run_registry() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "ding/runs.jsonl"


@contextmanager
def registered_run(thread_id: str, record: Path, progress: Path | None = None) -> Iterator[str]:
    registry = run_registry()
    registry.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    registry.parent.chmod(0o700)
    locks = registry.parent / "run-locks"
    locks.mkdir(exist_ok=True, mode=0o700)
    locks.chmod(0o700)
    run_id = str(uuid4())
    with writer(locks / f"{run_id}.lock"):
        _fsync_directory(locks)
        with writer(registry, blocking=True):
            registry.chmod(0o600)
            with registry.open("r+b") as handle:
                if handle.seek(0, os.SEEK_END):
                    handle.seek(-1, os.SEEK_END)
                    if handle.read(1) != b"\n":
                        handle.write(b"\n")
            append_jsonl(registry, {
                "event": "run_start", "run_id": run_id, "thread_id": thread_id,
                "record": str(record.resolve()), "pid": os.getpid(),
                "started_ns": time.time_ns(), "cwd": str(Path.cwd()),
                **({"progress": str(progress.resolve())} if progress is not None else {}),
            })
        yield run_id


def latest_runs() -> dict[str, dict]:
    registry = run_registry()
    try:
        lines = registry.read_bytes().splitlines()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        print(f"ding: run registry unavailable ({registry}): {exc}", file=sys.stderr)
        return {}
    runs = {}
    for number, line in enumerate(lines, 1):
        try:
            run = json.loads(line)
            if (not isinstance(run, dict) or run.get("event") != "run_start"
                    or any(not isinstance(run.get(key), str) for key in ("run_id", "thread_id", "record", "cwd"))
                    or str(UUID(run["run_id"])) != run["run_id"]
                    or str(UUID(run["thread_id"])) != run["thread_id"]
                    or not Path(run["record"]).is_absolute() or not Path(run["cwd"]).is_absolute()
                    or type(run.get("pid")) is not int or run["pid"] <= 0
                    or type(run.get("started_ns")) is not int or run["started_ns"] <= 0
                    or "progress" in run and (not isinstance(run["progress"], str)
                                              or not Path(run["progress"]).is_absolute())):
                raise ValueError("invalid run_start")
        except (ValueError, TypeError, AttributeError, UnicodeError) as exc:
            print(f"ding: invalid run registry row {number} ({registry}): {exc}", file=sys.stderr)
            continue
        runs[run["thread_id"]] = run
    return runs


def run_state(run: dict) -> str:
    try:
        alive = False
        try:
            with (run_registry().parent / "run-locks" / f"{run['run_id']}.lock").open("r") as token:
                try:
                    fcntl.flock(token.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    alive = True
        except FileNotFoundError:
            pass
        try:
            raw = Path(run["record"]).read_bytes()
        except FileNotFoundError:
            raw = b""
        lines = raw.splitlines()
        task_exit, notification = None, None
        for index, line in enumerate(lines):
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeError):
                if index == len(lines) - 1 and not raw.endswith(b"\n"):
                    print(f"ding: ignoring incomplete run record tail ({run['record']})", file=sys.stderr)
                    break
                raise
            if (not isinstance(event, dict) or not isinstance(event.get("run_id"), str)
                    or not event["run_id"] or not isinstance(event.get("event"), str)):
                raise ValueError("invalid record event")
            if event.get("run_id") != run["run_id"]:
                continue
            if event.get("thread_id") != run["thread_id"]:
                raise ValueError("record thread_id does not match run")
            if event.get("event") == "task_exit":
                if type(event.get("exit_code")) is not int:
                    raise ValueError("invalid task exit code")
                task_exit = event
            elif event.get("event") == "notification":
                if not isinstance(event.get("outcome"), str) or event["outcome"] not in {
                        "started", "queued_behind_active", "skipped_active", "queue_failed",
                        "failed", "unknown", "queued"}:
                    raise ValueError("invalid notification outcome")
                notification = event
        if task_exit is None:
            if notification is not None:
                raise ValueError("notification without task_exit")
            return "running" if alive else "lost"
        if notification is not None:
            if notification["outcome"] in {"queue_failed", "failed", "unknown", "queued"}:
                return "notify_failed"
        elif not alive:
            return "notify_failed"
        return f"exited({task_exit['exit_code']})"
    except (OSError, ValueError, UnicodeError) as exc:
        print(f"ding: run evidence unavailable ({run['record']}): {exc}", file=sys.stderr)
        return "unknown"


STALE_PROGRESS = 600


def short_time(value: object) -> str:
    """An ISO time as MM-DD HH:MM in its own zone, or the text unchanged."""
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).strftime("%m-%d %H:%M")
    except ValueError:
        return str(value)


def progress_summary(path: str, now: float | None = None) -> str:
    """One board cell from a progress-v1 file: stage done/total, rate, ETA, and a mark when it went quiet."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != "progress-v1":
            return "progress: unknown format"
    except FileNotFoundError:
        return "progress: no file yet"
    except (OSError, ValueError, UnicodeError):
        return "progress: unreadable"
    current = data.get("current") if isinstance(data.get("current"), dict) else None
    parts = []
    if current:
        total = current.get("total")
        parts.append(f"{current.get('name') or data.get('stage') or '?'} {current.get('done', '?')}/"
                     f"{'?' if total is None else total}")
        if current.get("failed"):
            parts.append(f"failed {current['failed']}")
    elif data.get("stage"):
        parts.append(str(data["stage"]))
    rate = data.get("rate_per_hour")
    if isinstance(rate, (int, float)) and not isinstance(rate, bool):
        parts.append(f"{rate:.0f}/h" if rate >= 10 else f"{rate:.1f}/h")
    eta = data.get("eta")
    if isinstance(eta, dict) and (eta.get("beijing") or eta.get("utc")):
        parts.append(f"ETA {short_time(eta.get('beijing') or eta.get('utc'))}")
    state = data.get("state")
    if state != "running":
        parts.append(str(state))
    else:
        updated = data.get("updated", {}).get("utc") if isinstance(data.get("updated"), dict) else None
        try:
            age = (time.time() if now is None else now) - datetime.fromisoformat(
                str(updated).replace("Z", "+00:00")).timestamp()
        except ValueError:
            age = None
        if age is None:
            parts.append("STALE? no update time")
        elif age > STALE_PROGRESS:
            parts.append(f"STALE {int(age // 60)}m")
    return " ".join(parts) or "progress: empty"


def executor_comment_heads(mapping: dict[str, set[int]], fields: tuple[str, ...]) -> dict[str, str]:
    numbers = sorted({number for issues in mapping.values() for number in issues})
    if not numbers:
        return {}
    try:
        repo = github_repository()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ding: executor comment lookup failed: {exc}", file=sys.stderr)
        return {}

    def fetch(number):
        try:
            result = gh_json(["issue", "view", str(number), "--repo", repo, "--json", "comments"])
            comments = result.get("comments") if isinstance(result, dict) else None
            if (not isinstance(comments, list) or any(not isinstance(comment, dict)
                    or not isinstance(comment.get("body"), str) for comment in comments)):
                raise ValueError("Malformed gh comments.")
            return number, comments
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            print(f"ding: executor comment lookup failed for #{number}: {exc}", file=sys.stderr)
            return number, None

    latest, uncertain = {}, set()
    with ThreadPoolExecutor(max_workers=4) as pool:
        for number, comments in pool.map(fetch, numbers):
            if comments is None:
                uncertain.update(thread_id for thread_id, issues in mapping.items() if number in issues)
                continue
            for ordinal, comment in enumerate(comments):
                owners = session_references(comment["body"], "owner")
                values = owners[-1:] if owners else session_references(comment["body"])
                ids = set()
                for value in values:
                    try:
                        ids.add(str(UUID(value)))
                    except ValueError:
                        continue
                ids = {thread_id for thread_id in ids if number in mapping.get(thread_id, set())}
                if not ids:
                    continue
                try:
                    created = datetime.fromisoformat(comment.get("createdAt", "").replace("Z", "+00:00"))
                    if created.tzinfo is None:
                        raise ValueError("missing timezone")
                    timestamp = created.timestamp()
                except (ValueError, TypeError, AttributeError, OverflowError, OSError):
                    print(f"ding: invalid executor comment timestamp for #{number}; using final reply", file=sys.stderr)
                    uncertain.update(ids)
                    continue
                order = timestamp, ordinal, number
                for thread_id in ids:
                    if thread_id not in latest or order >= latest[thread_id][0]:
                        latest[thread_id] = order, comment["body"]
    heads = {}
    for thread_id, (_, body) in latest.items():
        lines = body.replace("\r\n", "\n").lstrip().splitlines()
        if thread_id not in uncertain and len(lines) >= len(fields) and all(
                re.match(rf"^[ \t]*{re.escape(label)}:[ \t]*\S", line) for label, line in zip(fields, lines)):
            heads[thread_id] = " ".join(line.strip() for line in lines[:len(fields)])
    return heads


def process_snapshot(proc: Path = Path("/proc")) -> dict[int, dict]:
    try:
        uptime = float((proc / "uptime").read_text().split()[0])
        ticks = os.sysconf("SC_CLK_TCK")
        directories = list(proc.iterdir())
        if not math.isfinite(uptime) or uptime < 0 or ticks <= 0:
            raise ValueError("invalid process clock")
    except (OSError, ValueError, IndexError) as exc:
        print(f"ding: local process audit unavailable ({proc}): {exc}", file=sys.stderr)
        return {}
    processes = {}
    for directory in directories:
        if not directory.name.isdecimal():
            continue
        try:
            fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
            age = uptime - int(fields[19]) / ticks
            cpu = (int(fields[11]) + int(fields[12])) / ticks / age * 100 if age > 0 else 0
            raw = (directory / "cmdline").read_bytes()
            parts = raw.split(b"\0") if raw else []
            if parts and parts[-1] == b"":
                parts.pop()
            argv = [part.decode("utf-8", errors="replace") for part in parts]
            uid = directory.stat().st_uid
            current = (directory / "stat").read_text().rsplit(")", 1)[1].split()
            if int(current[19]) != int(fields[19]):
                continue
            processes[int(directory.name)] = {
                "pid": int(directory.name), "ppid": int(fields[1]), "state": current[0],
                "age": age, "cpu": cpu, "argv": argv, "uid": uid,
            }
        except (OSError, ValueError, IndexError, ArithmeticError):
            continue
    return processes


def process_command(argv: list[str]) -> tuple[str, list[str]]:
    if not argv:
        return "", []
    executable = Path(argv[0]).name
    if re.fullmatch(r"(?:python|pypy)(?:\d+(?:\.\d+)*)?", executable):
        index = 1
        while index < len(argv):
            value = argv[index]
            if value == "--":
                index += 1
                break
            if not value.startswith("-"):
                break
            if value == "-" or value.startswith("--"):
                return argv[0], []
            for offset, option in enumerate(value[1:], 2):
                if option == "c":
                    return argv[0], []
                if option == "m":
                    if value[offset:]:
                        return value[offset:], argv[index + 1:]
                    return (argv[index + 1], argv[index + 2:]) if index + 1 < len(argv) else (argv[0], [])
                if option in {"W", "X"}:
                    if not value[offset:]:
                        index += 1
                    break
                if option not in "bBdEiIOPqRsSuvx":
                    return argv[0], []
            index += 1
        if index < len(argv):
            return argv[index], argv[index + 1:]
    elif executable in {"node", "nodejs"}:
        index = 1
        while index < len(argv) and argv[index].startswith("-"):
            value = argv[index]
            if value == "--":
                index += 1
                break
            if (value.split("=", 1)[0] in {"--eval", "--print"}
                    or value.startswith(("-e", "-p"))):
                return argv[0], []
            if value in {"-r", "--require", "--loader", "--import", "--title"}:
                index += 2
            elif (value.startswith("-r") or value.split("=", 1)[0] in {
                    "--require", "--loader", "--import", "--title", "--inspect", "--inspect-brk"}
                  or value in {"--enable-source-maps", "--no-warnings", "--trace-warnings"}):
                index += 1
            else:
                return argv[0], []
        if index < len(argv):
            return argv[index], argv[index + 1:]
    return argv[0], argv[1:]


def is_run_wrapper(argv: list[str]) -> bool:
    program, arguments = process_command(argv)
    return Path(program).name in {"ding", "ding.py", "codex-session"} and bool(arguments) and arguments[0] == "run"


def infrastructure_process(argv: list[str]) -> bool:
    program, arguments = process_command(argv)
    name = Path(program).name
    if name == "ssh":
        forwarding = False
        no_session = False
        destination = False
        index = 0
        while index < len(arguments):
            value = arguments[index]
            if value == "--":
                return ((no_session and (destination or index + 1 < len(arguments)))
                        or forwarding and index + (1 if destination else 2) == len(arguments))
            if not value.startswith("-") or value == "-":
                if destination:
                    return no_session
                destination = True
                index += 1
                continue
            for offset, option in enumerate(value[1:], 2):
                if option == "N":
                    no_session = True
                elif option in "BbcDEeFIiJLlmOopPQRSWw":
                    forwarding |= option in "DLRW"
                    if not value[offset:]:
                        index += 1
                        if index == len(arguments):
                            return False
                    break
                elif option not in "46AaCfGgKkMNnqsTtVvXxYy":
                    return False
            index += 1
        return (no_session or forwarding) and destination
    return (name.startswith("tmux:") or name in {
        "tmux", "sshd", "autossh", "cloudflared", "codex", "codex.js", "codex.py",
        "claude", "claude.js", "code", "code-server", "vscode-server", "server-main.js",
        "vim", "nvim", "emacs", "zed", "sublime_text", "idea", "pycharm",
        "clangd", "gopls", "rust-analyzer", "pylsp", "pyright", "pyright-langserver",
        "pyright-langserver.js", "tsserver", "tsserver.js", "typescript-language-server",
        "eslintServer.js", "vscode-json-languageserver", "yaml-language-server",
    } or bool(re.fullmatch(r"codex-[A-Za-z0-9_-]+", name))
        or (name == "cli.js" and "claude-code" in Path(program).parts)
        or bool({".vscode-server", ".vscode-remote"}.intersection(Path(program).parts)))


def tmux_panes() -> dict[int, dict]:
    tmux = shutil.which("tmux")
    if tmux is None:
        return {}
    try:
        result = subprocess.run([tmux, "list-panes", "-a", "-F",
                                 "#{pane_pid}\t#{pane_id}\t#{session_name}\t#{window_name}"],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {}
    panes = {}
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) == 4 and fields[0].isdecimal():
                panes[int(fields[0])] = {"pane_id": fields[1], "session": fields[2], "window": fields[3]}
    return panes


def interactive_shell(argv: list[str]) -> bool:
    if not argv or Path(argv[0]).name.lstrip("-") not in SHELL_PROGRAMS:
        return False
    arguments = iter(argv[1:])
    for value in arguments:
        if value == "--":
            return next(arguments, None) is None
        if value in {"--rcfile", "--init-file"}:
            next(arguments, None)
        elif not value.startswith(("-", "+")) or value == "-":
            return False
        elif not value.startswith("--"):
            for offset, option in enumerate(value[1:], 2):
                if option == "c" and value.startswith("-"):
                    return False
                if option in "oO":
                    if not value[offset:]:
                        next(arguments, None)
                    break
    return True


def unwrapped_processes(processes: dict[int, dict], panes: dict[int, dict], *,
                        min_age: float, min_cpu: float, proc: Path = Path("/proc")) -> list[dict]:
    pane_ids = {pane["pane_id"]: pane for pane in panes.values()}
    wrapper_parents, other_parents = set(), set()
    for child in processes.values():
        if child["state"] not in {"Z", "X", "x"}:
            parents = wrapper_parents if is_run_wrapper(child["argv"]) else other_parents
            parents.add(child["ppid"])
    wrapper_only_parents = wrapper_parents - other_parents
    findings = []
    pane_findings = {}
    for pid, process in sorted(processes.items()):
        if (process["uid"] != os.geteuid() or process["state"] in {"Z", "X", "x"} or not process["argv"]
                or process["age"] <= 0
                or infrastructure_process(process["argv"])):
            continue
        ancestors, seen, current, wrapped, complete = [], set(), pid, False, False
        pane = None
        pane_chain = []
        while current and current not in seen:
            seen.add(current)
            ancestor = processes.get(current)
            if ancestor is None:
                break
            ancestors.append(current)
            if is_run_wrapper(ancestor["argv"]):
                wrapped = True
                break
            if pane is None and current in panes:
                pane = panes[current]
                pane_chain = ancestors.copy()
            if current == 1 or ancestor["ppid"] == 0:
                complete = True
                break
            current = ancestor["ppid"]
        if wrapped or not complete:
            continue
        fallback = pane is None
        if pane is None:
            for offset, ancestor_pid in enumerate(ancestors):
                try:
                    environment = (proc / str(ancestor_pid) / "environ").read_bytes().split(b"\0")
                except OSError:
                    continue
                pane_id = next((value[len(b"TMUX_PANE="):].decode("utf-8", errors="replace")
                                for value in environment if value.startswith(b"TMUX_PANE=")), None)
                if pane_id in pane_ids:
                    if pane is None or pane["pane_id"] == pane_id:
                        pane = pane_ids[pane_id]
                        pane_chain = ancestors[:offset + 1]
        if pane is not None:
            if pane["session"] == "ssh_tunnel":
                continue
            for depth, ancestor_pid in enumerate(reversed(pane_chain)):
                candidate = processes[ancestor_pid]
                if (not candidate["argv"] or interactive_shell(candidate["argv"])
                        or (ancestor_pid in wrapper_only_parents
                            and Path(candidate["argv"][0]).name.lstrip("-") in SHELL_PROGRAMS)
                        or infrastructure_process(candidate["argv"])):
                    continue
                process = candidate
                break
            else:
                continue
            if (process["uid"] != os.geteuid() or process["state"] in {"Z", "X", "x"}
                    or process["age"] <= 0 or process["age"] < min_age):
                continue
            rank = (fallback, depth, process["pid"])
            previous = pane_findings.get(pane["pane_id"])
            if previous is None or rank < previous[0]:
                pane_findings[pane["pane_id"]] = (rank, {**process, "session": pane["session"],
                                                       "window": pane["window"]})
        elif process["age"] >= min_age and process["cpu"] >= min_cpu:
            findings.append({**process, "session": "", "window": ""})
    findings.extend(row for _, row in pane_findings.values())
    return sorted(findings, key=lambda row: row["pid"])


def unwrapped_tasks(*, min_age: float = 600, min_cpu: float = 10) -> list[dict]:
    return unwrapped_processes(process_snapshot(), tmux_panes(), min_age=min_age, min_cpu=min_cpu)


def show_unwrapped(findings: list[dict]) -> None:
    if not findings:
        return
    print("\nUNWRAPPED_PID\tCPU%\tAGE_SECONDS\tTMUX_SESSION\tTMUX_WINDOW\tCOMMAND")
    for process in findings:
        cells = [str(process["pid"]), f"{process['cpu']:.1f}", f"{process['age']:.0f}",
                 process["session"], process["window"], shlex.join(process["argv"])]
        print("\t".join(display_cell(cell) for cell in cells))


def nonnegative_number(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number") from exc
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("must be a finite nonnegative number")
    return number


def show_board(path: str | None, *, all_threads: bool = False, status: tuple[str, ...] | None = None) -> None:
    roots = [] if all_threads else repository_roots()
    runs = latest_runs()
    try:
        server_path = socket_path(path)
    except (OSError, ValueError) as exc:
        if not runs:
            raise
        print(f"ding: App Server unavailable: {exc}", file=sys.stderr)
        server_path = None
    try:
        with ExitStack() as stack:
            call = stack.enter_context(app_server(server_path)) if server_path else None
            threads = (list_threads(call) if all_threads else board_threads(call, roots)) if call else []
            known = {thread["id"] for thread in threads}
            for thread_id, run in runs.items():
                if thread_id in known:
                    continue
                thread = {"id": thread_id, "name": thread_id, "cwd": run["cwd"],
                          "status": {"type": "unavailable"}}
                if call:
                    try:
                        thread = thread_metadata(call("thread/read", {"threadId": thread_id, "includeTurns": False}).get("thread"))
                        if thread["id"] != thread_id:
                            raise ValueError("App Server returned a different thread ID.")
                    except (AppServerError, OSError) as exc:
                        print(f"ding: run session metadata unavailable ({thread_id}): {exc}", file=sys.stderr)
                if all_threads or any(Path(thread["cwd"]).resolve().is_relative_to(root) for root in roots):
                    threads.append(thread)
            rows = []
            for thread in threads:
                head = ""
                if call:
                    try:
                        head = reply_head(read_reply(call, thread["id"]))
                    except NoFinalReply:
                        pass
                    except (AppServerError, OSError) as exc:
                        if thread["id"] not in runs:
                            raise
                        print(f"ding: run session reply unavailable ({thread['id']}): {exc}", file=sys.stderr)
                rows.append([thread["name"], thread["cwd"], thread["status"]["type"],
                             thread["id"], " ".join(head.splitlines())])
    except (AppServerUnavailable, OSError) as exc:
        if not runs:
            raise
        print(f"ding: App Server unavailable: {exc}", file=sys.stderr)
        rows = [[thread_id, run["cwd"], "unavailable", thread_id, ""]
                for thread_id, run in runs.items()
                if all_threads or any(Path(run["cwd"]).resolve().is_relative_to(root) for root in roots)]
    mapping = issue_map()
    heads = executor_comment_heads({row[3]: mapping.get(row[3], set()) for row in rows}, status) if status else {}
    shown = any("progress" in run for run in runs.values())
    print("NAME\tCWD\tSTATUS\tISSUE\tHEAD\tRUN" + ("\tPROGRESS" if shown else ""))
    for name, cwd, status, thread_id, head in rows:
        issue = ",".join(f"#{number}" for number in sorted(mapping.get(thread_id, set())))
        state = run_state(runs[thread_id]) if thread_id in runs else ""
        cells = [name, cwd, status, issue, heads.get(thread_id, head), state]
        if shown:
            cells.append(progress_summary(runs[thread_id]["progress"]) if "progress" in runs.get(thread_id, {}) else "")
        print("\t".join(display_cell(cell) for cell in cells))


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


def active_turn(call, thread_id: str) -> str | None:
    """The ID of the session's turn in progress, or None."""
    page = call("thread/turns/list", {"threadId": thread_id, "limit": 1, "sortDirection": "desc",
                                      "itemsView": "notLoaded"})
    turns = page.get("data")
    if not isinstance(turns, list) or (turns and not isinstance(turns[0], dict)):
        raise ValueError("Malformed App Server turns page.")
    if turns and turns[0].get("status") == "inProgress" and isinstance(turns[0].get("id"), str) and turns[0]["id"]:
        return turns[0]["id"]
    return None


def deliver_message(thread_id: str, message: str, path: str, client_id: str,
                    *, timeout: float = 60, steer: bool = False, interrupt: bool = False) -> dict:
    """Start the message as a new turn, or queue it behind the turn in progress.

    interrupt first stops the turn in progress; steer inserts the message into it instead of queueing,
    and falls back to the queue when the turn ends before the insertion lands.
    """
    result = {"outcome": "failed", "status": None, "error": None,
              "queued_submission_id": None, "turn_id": None, "turn_status": None}
    deadline = time.monotonic() + timeout
    mutation = None
    baseline = None
    inputs = [{"type": "text", "text": message}]

    def confirm(turn):
        result.update(outcome="steered" if mutation == "steer" else "started",
                      turn_id=turn["id"], turn_status=turn["status"])
        return result

    try:
        with app_server(path, deadline=deadline) as call:
            result["status"] = delivery_status(call, thread_id)
            if result["status"]["type"] == "notLoaded":
                result["status"] = delivery_status(call, thread_id, resume=True)
            if interrupt and result["status"]["type"] == "active":
                turn_id = active_turn(call, thread_id)
                if turn_id is not None:
                    call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
                    result["interrupted_turn_id"] = turn_id
                    while result["status"]["type"] == "active" and time.monotonic() < deadline:
                        time.sleep(min(1, max(0, deadline - time.monotonic())))
                        result["status"] = delivery_status(call, thread_id)
            if steer and result["status"]["type"] == "active":
                turn_id = active_turn(call, thread_id)
                if turn_id is not None:
                    mutation = "steer"
                    result["outcome"] = "unknown"
                    try:
                        steered = call("turn/steer", {"threadId": thread_id, "expectedTurnId": turn_id,
                                                      "clientUserMessageId": client_id, "input": inputs})
                    except AppServerError as exc:
                        # The turn ended or changed first; nothing was inserted, so queue instead.
                        mutation, result["outcome"], result["steer_error"] = None, "failed", str(exc)
                        result["status"] = delivery_status(call, thread_id)
                    else:
                        if steered.get("turnId") != turn_id:
                            raise ValueError("App Server steered a different turn.")
                        result.update(outcome="steered", turn_id=turn_id, turn_status="inProgress")
                        return result
            if result["status"]["type"] == "idle":
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
            submission = call("thread/queue/add", {
                "threadId": thread_id, "clientUserMessageId": client_id, "input": inputs,
            }).get("queuedSubmission")
            if (not isinstance(submission, dict) or not isinstance(submission.get("id"), str)
                    or not submission["id"] or submission.get("clientUserMessageId") != client_id
                    or not same_message(submission.get("input"), message)):
                raise ValueError("Malformed App Server queued submission.")
            result.update(outcome="queued", queued_submission_id=submission["id"])
            if result["status"]["type"] == "active":
                result["outcome"] = "queued_behind_active"
                return result
            while time.monotonic() < deadline:
                found = started_message(call, thread_id, client_id, message, after_turn_id=baseline)
                if found is not None:
                    return confirm(found)
                result["status"] = delivery_status(call, thread_id)
                if result["status"]["type"] == "active" and mutation == "add":
                    result["outcome"] = "queued_behind_active"
                    return result
                if result["status"]["type"] == "idle" and mutation == "add":
                    mutation = "start"
                    result["outcome"] = "unknown"
                    turn = call("thread/queue/start", {
                        "threadId": thread_id, "queuedSubmissionId": submission["id"],
                    }).get("turn")
                    found = message_turn(turn, client_id, message)
                    if found is not None:
                        return confirm(found)
                time.sleep(min(1, max(0, deadline - time.monotonic())))
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


def new_task(args) -> int:
    thread_id = None
    stage = "validation"
    try:
        cwd = Path(args.cwd).expanduser().resolve() if args.cwd else None
        if cwd is None or not cwd.is_dir():
            raise ValueError("--cwd must name an existing directory.")
        message = message_text(args.message, args.message_file, None)
        if not message.strip():
            raise ValueError("The first message (--message or --message-file) must not be empty.")
        stage = "connection"
        with app_server(socket_path(args.socket)) as call:
            if args.issue is not None and not getattr(args, "replace", False):
                stage = "owner check"
                owners = live_owners(call, ticket_owners(args.repo, args.issue))
                if owners:
                    named = ", ".join(f"{owner['id']} ({owner.get('name') or 'unnamed'})" for owner in owners)
                    raise ValueError(f"#{args.issue} already has an executor that is not archived: {named}. "
                                     f"Open a sub-ticket for the new run with --parent {args.issue}, "
                                     "or pass --replace to hand the ticket to a new session")
            stage = "thread/start"
            thread = call(stage, {"cwd": str(cwd), "ephemeral": False}).get("thread")
            try:
                thread_id = str(UUID(thread["id"]))
            except (KeyError, ValueError, TypeError, AttributeError) as exc:
                raise ValueError("Malformed App Server thread ID.") from exc
            stage = "thread/name/set"
            call(stage, {"threadId": thread_id, "name": args.name})
            if args.issue is not None:
                message = ownership_note(args.repo, args.issue, thread_id) + message
            stage = "turn/start"
            turn = call(stage, {"threadId": thread_id,
                                "input": [{"type": "text", "text": message}]}).get("turn")
            if (not isinstance(turn, dict) or not isinstance(turn.get("id"), str)
                    or not turn["id"] or turn.get("status") not in ("inProgress", "completed")):
                raise ValueError("App Server did not return an accepted turn.")
        print(thread_id)
        return 0
    except (OSError, ValueError, RuntimeError, KeyboardInterrupt) as exc:
        recovery = ""
        if thread_id is not None:
            recovery = f"; session {thread_id}; inspect Desktop or ding board before retrying"
        elif stage == "thread/start":
            recovery = "; session creation is unconfirmed; inspect Desktop or ding board before retrying"
        print(f"ding: new {stage}: {str(exc) or type(exc).__name__}{recovery}", file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1


def queued_sends(record: Path, thread_id: str, sender: str) -> dict[str, str]:
    """Queued submission ID -> client message ID for this sender's sends to this session that were queued."""
    found = {}
    try:
        lines = record.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return found
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (isinstance(event, dict) and event.get("event") == "send_result" and event.get("thread_id") == thread_id
                and event.get("sender") == sender and isinstance(event.get("queued_submission_id"), str)):
            found[event["queued_submission_id"]] = event.get("client_message_id")
    return found


def supersede_queued(thread_id: str, path: str, sent: dict[str, str]) -> list[dict]:
    """Delete the session's still-queued submissions among `sent`; return what was found and deleted."""
    clients = {client: submission for submission, client in sent.items() if client}
    removed = []
    with app_server(path) as call:
        cursor, seen, queued = None, set(), []
        while True:
            page = call("thread/queue/list", {"threadId": thread_id, **({"cursor": cursor} if cursor else {})})
            if not isinstance(page.get("data"), list):
                raise ValueError("Malformed App Server queue page.")
            queued += page["data"]
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise ValueError("Malformed App Server pagination cursor.")
            seen.add(cursor)
        for submission in queued:
            if not isinstance(submission, dict) or not isinstance(submission.get("id"), str):
                raise ValueError("Malformed App Server queued submission.")
            if submission["id"] not in sent and submission.get("clientUserMessageId") not in clients:
                continue
            deleted = call("thread/queue/delete", {"threadId": thread_id, "queuedSubmissionId": submission["id"]})
            removed.append({"queued_submission_id": submission["id"],
                            "client_message_id": submission.get("clientUserMessageId"),
                            "deleted": deleted.get("deleted") is True})
    return removed


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
    modes = {name: True for name in ("steer", "supersede", "interrupt") if getattr(args, name, False)}
    event.update(modes)
    try:
        event["target"] = normalize_target(args.thread_id, args.issue)
        event["thread_id"] = resolve_target(event["target"], None, args.socket)
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
    if modes.pop("supersede", False):
        superseded = {"event": "superseded", "client_message_id": event["client_message_id"],
                      "sender": args.sender, "thread_id": event["thread_id"], "removed": [], "error": None}
        try:
            superseded["removed"] = supersede_queued(event["thread_id"], path,
                                                     queued_sends(record, event["thread_id"], args.sender))
        except (OSError, ValueError, RuntimeError) as exc:
            superseded["error"] = str(exc) or type(exc).__name__
            print(f"ding: could not remove earlier queued messages ({superseded['error']}); sending anyway",
                  file=sys.stderr)
        superseded["timestamp_ns"] = time.time_ns()
        with writer(record, blocking=True):
            append_jsonl(record, superseded)
        deleted = [item["queued_submission_id"] for item in superseded["removed"] if item["deleted"]]
        if deleted:
            print(f"ding: removed {len(deleted)} earlier queued message(s) from {args.sender}: "
                  f"{', '.join(deleted)}", file=sys.stderr)
    delivery = deliver_message(event["thread_id"], args.message, path, event["client_message_id"], **modes)
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
    return 0 if event["outcome"] in {"started", "steered", "queued_behind_active"} else (
        130 if event["error"] == "KeyboardInterrupt" else 1)


def task_exit_code(command: list[str], *, env: dict | None = None) -> tuple[int, str | None]:
    try:
        child = subprocess.Popen(command, env=env)
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
    progress = Path(args.progress) if getattr(args, "progress", None) else None
    with writer(record), registered_run(args.thread_id, record, progress) as run_id:
        child_env = os.environ.copy()
        child_env["DING_RUN_ID"] = run_id
        code, error = task_exit_code(command, env=child_env)
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
            notification.update(deliver_message(args.thread_id, args.message, path, client_id))
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


def launch_task(args, command: list[str]) -> int:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.name):
        raise ValueError("launch name must be 1-64 letters, digits, underscores or hyphens, starting with a letter or digit")
    try:
        thread_id = str(UUID(os.environ.get("CODEX_THREAD_ID", "")))
    except ValueError as exc:
        raise ValueError("launch requires a valid CODEX_THREAD_ID in the calling session") from exc
    tmux = shutil.which("tmux")
    if tmux is None:
        raise ValueError("tmux executable not found in the caller's PATH")
    launch_id = str(uuid4())
    launch_records = (run_registry().parent / "launch-records").resolve()
    record = (Path(args.record) if args.record else launch_records / f"{launch_id}.jsonl").resolve()
    message = message_text(args.message if args.message is not None else
                           f"Task {args.name} exited; inspect {record}", args.message_file, args.sender)
    inner = [os.path.abspath(sys.executable), str(Path(__file__).resolve()), "run", thread_id,
             "--record", str(record)]
    if getattr(args, "progress", None):
        inner += ["--progress", str(Path(args.progress).resolve())]
    launch_records.mkdir(parents=True, exist_ok=True, mode=0o700)
    launch_records.chmod(0o700)
    message_path = launch_records / f"{launch_id}.message.txt"
    with os.fdopen(os.open(message_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600),
                   "w", encoding="utf-8", newline="") as handle:
        handle.write(message)
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(launch_records)
    inner.extend(["--message-file", str(message_path)])
    if args.socket is not None:
        inner.extend(["--socket", str(Path(args.socket).resolve())])
    inner.extend(["--", *command])
    environment = {
        "CODEX_THREAD_ID": thread_id, "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME") or str(Path.home()),
        "XDG_STATE_HOME": str(run_registry().parent.parent.resolve()),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }
    arguments = [str(Path(tmux).absolute()), "new-session", "-d", "-s", f"ding-{args.name}",
                 "-n", args.name, "-c", str(Path.cwd())]
    for key, value in environment.items():
        arguments.extend(["-e", f"{key}={value}"])
    prelude = (f'{shlex.quote(str(Path(tmux).absolute()))} set-option -p -t "$TMUX_PANE" '
               f'remain-on-exit on && exec {shlex.join(inner)}')
    arguments.extend(["-P", "-F", "#{session_name}\t#{window_name}", "/bin/sh", "-c", prelude])
    # tmux treats a trailing semicolon as a command separator even in argv values.
    arguments = [value[:-1] + r"\;" if value.endswith(";") else value for value in arguments]
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("tmux launch timed out; task creation is unconfirmed; inspect tmux and run records before retrying") from exc
    if result.returncode:
        raise ValueError(f"tmux launch failed: {result.stderr.strip() or result.returncode}")
    print(f"ding: created ding-{args.name}/{args.name}; record: {record}")
    return 0


def status_fields(value: str) -> tuple[str, ...]:
    fields = tuple(field.strip() for field in value.split(","))
    if not all(fields):
        raise argparse.ArgumentTypeError("must be comma-separated nonempty field names")
    return fields


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ding", description=__doc__)
    try:
        installed = metadata.version("dkitchen")
    except metadata.PackageNotFoundError:
        installed = "unknown"
    parser.add_argument("--version", action="version", version=f"ding {installed}")
    commands = parser.add_subparsers(dest="action", required=True)
    sub = commands.add_parser("new", help="Create a saved Codex session and start its first message")
    sub.add_argument("--cwd", required=True, help="Existing working directory for the session")
    sub.add_argument("--name", required=True, help="Session title; with --issue, filled into the "
                                                   "[sessions] name template")
    owned = sub.add_mutually_exclusive_group()
    owned.add_argument("--issue", type=int, help="Ticket this session owns: formats --name with the [sessions] "
                                                 "name template and asks the session to post its Codex owner line; "
                                                 "refused while another unarchived session owns it")
    owned.add_argument("--parent", type=int, help="First open a sub-ticket of this ticket for the run, then own it "
                                                  "as with --issue")
    sub.add_argument("--title", help="With --parent: the sub-ticket's title (default: --name)")
    sub.add_argument("--replace", action="store_true", help="With --issue: take over a ticket another session owns")
    message = sub.add_mutually_exclusive_group(required=True)
    message.add_argument("--message", help="First message text, or '-' to read stdin")
    message.add_argument("--message-file", help="UTF-8 file holding the first message")
    sub.add_argument("--socket", help="Existing local App Server Unix socket path")
    board = commands.add_parser("board", help="List sessions, run states, and local unwrapped tasks")
    board.add_argument("--all", action="store_true", help="Include all non-archived sessions")
    board.add_argument("--status", action="store_true", help="Show the latest executor comment status block")
    board.add_argument("--status-fields", type=status_fields,
                       help="Comma-separated field names that open a status block (default: $DING_STATUS_FIELDS, "
                            f"then [status] fields in .dkitchen.toml, then '{STATUS_FIELDS}')")
    board.add_argument("--socket", help="Existing local App Server Unix socket path")
    board.add_argument("--min-age", type=nonnegative_number, default=600,
                       help="Minimum age in seconds for the local unwrapped process audit (default: 600)")
    board.add_argument("--min-cpu", type=nonnegative_number, default=10,
                       help="Minimum lifetime CPU percent per core outside tmux panes (default: 10)")
    for action, summary in (("read", "Print a session's latest final reply"),
                            ("status", "Print a session's App Server status as JSON"),
                            ("info", "Print a session's ID, name, working directory, and status as JSON"),
                            ("rename", "Change a session's name without starting a model turn"),
                            ("send", "Send a message to a session"),
                            ("run", "Run a command and message a session when it exits"),
                            ("launch", "Run a command in a new tmux session and wake this Codex session when it exits")):
        sub = commands.add_parser(action, help=summary)
        if action == "launch":
            sub.add_argument("name", help="Name of the dedicated ding-NAME tmux session and its first window")
        else:
            target = sub.add_mutually_exclusive_group(required=True)
            target.add_argument("thread_id", nargs="?", help="Session UUID, exact name, '#NUMBER', OWNER/REPO#NUMBER, or GitHub issue/pull URL")
            target.add_argument("--issue", type=int, help="Issue's latest Codex owner, or its unique Codex session")
        sub.add_argument("--socket", help="Existing local App Server Unix socket path")
        if action == "read":
            sub.add_argument("--head", action="store_true", help="Print only the first paragraph of the final reply")
        if action == "rename":
            sub.add_argument("--name", required=True, help="New session name, preserved exactly as supplied")
        if action in {"send", "run", "launch"}:
            message = sub.add_mutually_exclusive_group(required=action != "launch")
            message.add_argument("--message", help="Message text, or '-' to read stdin")
            message.add_argument("--message-file", help="UTF-8 message file")
            sub.add_argument("--from", dest="sender", help="Prefix the message with '[from NAME]' on its own line")
        if action == "send":
            sub.add_argument("--steer", action="store_true",
                             help="If the session is busy, insert the message into its current turn instead of queueing it")
            sub.add_argument("--supersede", action="store_true",
                             help="First delete this --from sender's earlier messages still queued for the session")
            sub.add_argument("--interrupt", action="store_true",
                             help="First stop the session's current turn; only when that work is known to be obsolete")
        if action in {"run", "launch"}:
            sub.add_argument("--record", required=action == "run", help="Append-only local exit/notification JSONL")
            sub.add_argument("--progress", help="The task's progress.json (progress-v1), shown by `ding board`")
            sub.epilog = ("Task command follows --, for example: launch NAME -- python task.py" if action == "launch" else
                          "Task command follows --, for example: run UUID --message TEXT --record exit.jsonl -- python task.py")
    sub = commands.add_parser("relay", help="Send a message to a ticket's executor, then leave it on the ticket")
    sub.add_argument("ticket", type=int, help="Ticket number in this repository")
    message = sub.add_mutually_exclusive_group(required=True)
    message.add_argument("--message", help="What the executor receives, or '-' to read stdin")
    message.add_argument("--message-file", help="UTF-8 message file")
    sub.add_argument("--comment", help="Ticket comment (default: the message itself)")
    sub.add_argument("--no-comment", action="store_true", help="Send only; leave no ticket comment")
    sub.add_argument("--to", help="Executor session UUID or name, when the ticket has no Codex owner line")
    sub.add_argument("--queue", action="store_true",
                     help="Queue behind the executor's current turn and keep earlier queued relays "
                          "(default: insert into the current turn and delete them)")
    sub.add_argument("--interrupt", action="store_true",
                     help="First stop the executor's current turn; only when that work is known to be obsolete")
    sub.add_argument("--socket", help="Existing local App Server Unix socket path")
    sub = commands.add_parser("merge", help="Merge a reviewed PR at the reviewed HEAD")
    sub.add_argument("pr", type=int, help="PR number")
    sub.add_argument("sha", help="The HEAD that was reviewed (full or prefix)")
    ticket = sub.add_mutually_exclusive_group()
    ticket.add_argument("--ticket", type=int, help="The PR's ticket, whose executor must be waiting on the "
                                                   "orchestrator (default: the one ticket the PR body mentions "
                                                   "that has executor comments)")
    ticket.add_argument("--no-ticket", action="store_true", help="Merge a PR that belongs to no ticket")
    sub = commands.add_parser("log", help="Post an entry to the orchestrator log ticket")
    sub.add_argument("--item", action="append", required=True, help="One automatic action; repeat for more")
    sub.add_argument("--pending", action="append", default=[], help="One matter waiting for the user; repeat for more")
    sub.add_argument("--issue", type=int, help="Orchestrator log ticket (default: [log] issue in .dkitchen.toml)")
    sub = commands.add_parser("watch", help="Wait for GitHub activity the orchestrator must handle, then exit",
                                description="Wait for GitHub activity the orchestrator must handle, print it, and exit. "
                                            "Read-only; calls no model.")
    sub.add_argument("--state", type=Path, help="JSON file holding the last reported time "
                                                  "(default: ~/.local/state/ding/watch/OWNER-REPO.json)")
    sub.add_argument("--since", help="Start time for the first run (ISO, UTC; default: now)")
    sub.add_argument("--skip-issue", type=int, action="append", default=[],
                       help="Ticket to ignore; the orchestrator log ticket is always ignored")
    sub.add_argument("--checkout", type=Path, help="Checkout where `ding board` lists ticket sessions (default: this repository)")
    sub.add_argument("--interval", type=float, default=90, help="Seconds between polls (default: 90)")
    sub.add_argument("--settle", type=float, default=300, help="After the first trigger, seconds to wait to batch more (default: 300)")
    sub.add_argument("--stale-hours", type=float, default=6, help="Executor silence that counts as stale (default: 6)")
    sub.add_argument("--stale-every", type=float, default=3600, help="Seconds between stale checks (default: 3600)")
    sub.add_argument("--max-errors", type=int, default=10, help="Consecutive failed polls before exiting (default: 10)")
    sub = commands.add_parser("check-refs", help="Check `file:line`, sha256, ticket, PR, and comment references "
                                                 "in a draft comment")
    sub.add_argument("draft", help="Draft comment file")
    where = sub.add_mutually_exclusive_group()
    where.add_argument("--ref", default="origin/main", help="Git revision the references point into (default: origin/main)")
    where.add_argument("--root", type=Path, help="Directory to read files from instead, such as a worktree or output folder")
    sub.add_argument("--tolerance", type=int, default=0, help="Lines around a reference that may hold its name (default: 0)")
    sub.add_argument("--repo", help="Repository that bare #N numbers refer to (default: repo in .dkitchen.toml, "
                                    "then the one gh sees)")
    sub.add_argument("--offline", action="store_true", help="Do not ask GitHub; list ticket and link references as "
                                                            "not checked")
    argv = list(sys.argv[1:] if argv is None else argv)
    command = []
    if argv and argv[0] in {"run", "launch"} and "--" in argv:
        separator = argv.index("--")
        command = argv[separator + 1:]
        argv = argv[:separator]
    args = parser.parse_args(argv)
    if args.action == "rename" and not args.name.strip():
        parser.error("--name must contain non-whitespace text")
    if getattr(args, "issue", None) is not None and args.issue <= 0:
        parser.error("--issue must be a positive issue number")
    if getattr(args, "parent", None) is not None and args.parent <= 0:
        parser.error("--parent must be a positive issue number")
    if args.action == "merge" and args.ticket is not None and args.ticket <= 0:
        parser.error("--ticket must be a positive issue number")
    if args.action in {"run", "launch"} and not command:
        parser.error(f"{args.action} requires a task command after --")
    try:
        if args.action == "new":
            if not args.name.strip():
                parser.error("--name must contain non-whitespace text")
            if args.parent is None and (args.title is not None or args.replace and args.issue is None):
                parser.error("--title goes with --parent, and --replace with --issue")
            if (args.issue is not None or args.parent is not None) and args.cwd and Path(args.cwd).expanduser().is_dir():
                from dkitchen import config
                settings = config.load(Path(args.cwd).expanduser().resolve())
                args.repo = settings.repository()
                if args.parent is not None:
                    args.message, args.message_file = message_text(args.message, args.message_file, None), None
                    if not args.message.strip():
                        raise ValueError("The first message (--message or --message-file) must not be empty.")
                    args.issue = create_sub_issue(args.repo, args.parent, (args.title or args.name).strip(),
                                                  settings.sessions.parent.format(parent=args.parent))
                args.name = settings.sessions.session_name(issue=args.issue, name=args.name, repo=args.repo)
            return new_task(args)
        if args.action == "check-refs":
            from dkitchen import refs
            return refs.check_refs(args)
        if args.action in {"relay", "merge", "log", "watch"}:
            from dkitchen import config, orchestrate, watch
            settings = config.load()
            handler = watch.watch if args.action == "watch" else getattr(orchestrate, args.action)
            return handler(args, settings)
        if args.action == "board":
            fields = args.status_fields
            if args.status and fields is None:
                environment = os.environ.get("DING_STATUS_FIELDS")
                try:
                    fields = status_fields(environment) if environment is not None else None
                except argparse.ArgumentTypeError as exc:
                    parser.error(f"DING_STATUS_FIELDS {exc}")
                if fields is None:
                    from dkitchen import config
                    fields = config.load().status.fields
            findings = unwrapped_tasks(min_age=args.min_age, min_cpu=args.min_cpu)
            try:
                show_board(args.socket, all_threads=args.all, status=fields if args.status else None)
            finally:
                show_unwrapped(findings)
            return 0
        if args.action == "launch":
            return launch_task(args, command)
        if args.action in {"send", "run"}:
            args.message = message_text(args.message, args.message_file, args.sender)
        if args.action == "send":
            if args.supersede and args.sender is None:
                parser.error("--supersede needs --from, to tell whose queued messages to delete")
            return send_task(args)
        args.thread_id = resolve_target(args.thread_id, args.issue, args.socket)
        if args.action == "run":
            return run_task(args, command)
        path = socket_path(args.socket)
        if args.action == "rename":
            with app_server(path) as call:
                call("thread/name/set", {"threadId": args.thread_id, "name": args.name})
            print(args.thread_id)
            return 0
        if args.action == "info":
            with app_server(path) as call:
                print(json.dumps(read_info(call, args.thread_id), ensure_ascii=False))
            return 0
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
