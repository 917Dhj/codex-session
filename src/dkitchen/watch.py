"""Wait for GitHub activity the orchestrator must handle, print it, and exit; read-only, no model calls.

Wakes on:
- executor comments whose status is not "in progress", or whose "waiting on" names a station or the user;
- idle executors still marked "in progress", after [watch] idle_minutes with no running job;
- comments from the user (no station signature);
- status labels being added, and newly opened PRs;
- stale tickets: no status label, last comment an executor "in progress" older than --stale-hours,
  no running `ding run` for the ticket, and no live tmux job whose session name holds issue<N>.
On tickets labelled awaiting-user, executor comments wake only for blocked, done, and waiting on design,
or when their "waiting on" names the orchestrator, designer, or verifier.
Verifier comments never wake on their own, since the verifier also messages the orchestrator;
they are listed in the next batch for reference.
The state file keeps the time already reported, so rerunning the same command loses nothing.
One watch runs per state file; a second one exits at once.
Station names, status words, and labels come from .dkitchen.toml.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from dkitchen.config import Config, Sessions

SHELLS = {"bash", "sh", "zsh", "dash", "fish"}


def gh(path: str) -> list[dict]:
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"gh api {path} failed")
    return json.loads(result.stdout)


def newer(path: str, field: str, since: str, pages: int = 3) -> list[dict]:
    """Pages of a newest-first listing until an item is older than `since`."""
    items = []
    for page in range(1, pages + 1):
        batch = gh(f"{path}&per_page=100&page={page}")
        items.extend(item for item in batch if item[field] >= since)
        if len(batch) < 100 or batch[-1][field] < since:
            break
    return items


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def now() -> str:
    return stamp(datetime.now(timezone.utc))


def issue_number(url: str) -> int:
    return int(url.rstrip("/").rsplit("/", 1)[1])


def role(body: str, config: Config) -> str:
    """The station named at the start of the first line; otherwise the executor by a line starting with its
    session signature (`Codex session:`, or `Claude session:` for an executor run in Claude Code); else the user.
    A session signature quoted inside a line signs nothing."""
    stations = config.stations
    first_line = next((line for line in body.splitlines() if line.strip()), "")
    first = re.sub(r"^[\s#*>]+", "", first_line)
    if first_line.startswith(config.designer_signatures) or first.startswith(config.designer_signatures):
        return stations.designer
    signed = next((name for name in (stations.verifier, stations.orchestrator)
                   if first.startswith(name)), None)
    if signed:
        return signed
    return stations.executor if re.search(r"(?m)^[ \t]*(?:Codex|Claude) session:", body) else stations.user


def fields(body: str, config: Config) -> dict[str, str]:
    """The status block's fields, with half- or full-width colons; an unlisted Pending merge field may follow."""
    count = len(config.status.fields) + (config.status.pending_merge not in config.status.fields)
    pairs = (re.split(r"[:：]", line, maxsplit=1) for line in body.splitlines()[:count])
    return {pair[0].strip(): pair[1].strip() for pair in pairs if len(pair) == 2}


def state_and_waiting(body: str, config: Config) -> tuple[str, str]:
    block, names = fields(body, config), config.status.fields
    return block.get(names[0], ""), block.get(names[1], "") if len(names) > 1 else ""


def executor_wakes(body: str, waiting_user: bool, config: Config) -> bool:
    state, waiting = state_and_waiting(body, config)
    status, stations = config.status, config.stations
    names = lambda *who: not waiting.startswith(status.none) and any(name in waiting for name in who)
    kitchen = (stations.orchestrator, stations.designer, stations.verifier)
    if waiting_user:
        return state in {status.blocked, status.done, status.waiting_on_design} or names(*kitchen)
    return state != status.in_progress or names(*kitchen, stations.user)


def summary(body: str, config: Config) -> str:
    """The status block's first two lines, or the first line when there is no block."""
    count, names = len(config.status.fields), config.status.fields[:2]
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    picked = [line for line in lines[:count] if line.startswith(names)] or lines[:1]
    return "; ".join(picked)[:160]


def collect(config: Config, repo: str, since: str, labels_since: str, seen: set[str],
            skip: set[int]) -> list[tuple[str, str, bool]]:
    """(time, line, wakes) for every item after the given times; comment and PR times use `since`."""
    awaiting = config.labels.awaiting_user
    waiting = {issue["number"] for issue in gh(f"repos/{repo}/issues?state=open&labels={awaiting}&per_page=100")}
    verifier, user, executor = config.stations.verifier, config.stations.user, config.stations.executor
    found = []
    for comment in newer(f"repos/{repo}/issues/comments?since={since}&sort=updated&direction=desc", "updated_at", since):
        number, body = issue_number(comment["issue_url"]), comment["body"]
        who = role(body, config)
        if number in skip:
            continue
        if who in {verifier, user} or (who == executor and executor_wakes(body, number in waiting, config)):
            kind = f"{who} comment" if comment["created_at"] >= since else f"{who} edited a comment"
            note = f" ({verifier.lower()} reports separately; for reference)" if who == verifier else ""
            found.append((comment["updated_at"], f"#{number} {kind}{note}: {summary(body, config)} {comment['html_url']}",
                          who != verifier))
    for event in newer(f"repos/{repo}/issues/events?", "created_at", labels_since):
        label = (event.get("label") or {}).get("name", "")
        if event["event"] == "labeled" and label.startswith(config.labels.prefix) and event["issue"]["number"] not in skip:
            found.append((event["created_at"], f"#{event['issue']['number']} label +{label} {event['issue']['html_url']}", True))
    for pull in newer(f"repos/{repo}/pulls?state=all&sort=created&direction=desc", "created_at", since, pages=1):
        found.append((pull["created_at"], f"PR #{pull['number']} opened: {pull['title'][:80]} {pull['html_url']}", True))
    # Times have one-second resolution: the boundary second is rechecked and its reported lines skipped.
    return sorted({item for item in found if item[1] not in seen})


def board_tickets(cells: list[str], sessions: Sessions, repo: str) -> set[int]:
    name = json.loads('"' + cells[0] + '"')
    number = sessions.issue_number(name, repo)
    return {number} if number is not None else {int(value) for value in re.findall(r"#(\d+)", cells[3])}


def board_running(output: str, sessions: Sessions = Sessions(), repo: str = "") -> set[int]:
    """Tickets with a running `ding run`, using template names before the board ISSUE column."""
    running = set()
    for line in output.splitlines():
        if not line.strip():
            break  # the unwrapped-process table follows a blank line
        cells = line.split("\t")
        if len(cells) >= 6 and cells[5] == "running":
            running.update(board_tickets(cells, sessions, repo))
    return running


def board_output(checkout: Path) -> str:
    # The ISSUE column is filled only for sessions of the repository in the current directory.
    result = subprocess.run([sys.executable, "-m", "dkitchen.ding", "board", "--no-issue-lookup"],
                            capture_output=True, text=True,
                            timeout=120, cwd=checkout)
    if result.returncode:
        raise RuntimeError(f"ding board failed: {result.stderr.strip()[:200]}")
    return result.stdout


def running_issues(checkout: Path, sessions: Sessions = Sessions(), repo: str = "") -> set[int]:
    return board_running(board_output(checkout), sessions, repo)


def tmux_issues(panes: str) -> set[int]:
    """Tickets with a live tmux job: a pane still running something other than a shell, in a session named issue<N>."""
    running = set()
    for line in panes.splitlines():
        name, dead, command = (line.split("|") + ["", ""])[:3]
        match = re.search(r"issue(\d+)", name)
        if match and dead == "0" and command not in SHELLS:
            running.add(int(match.group(1)))
    return running


def tmux_panes() -> str:
    """`tmux list-panes` for all sessions; empty when no tmux server is running."""
    try:
        result = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{session_name}|#{pane_dead}|#{pane_current_command}"],
                                capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return ""
    return result.stdout if result.returncode == 0 else ""


def stale(config: Config, repo: str, checkout: Path, hours: float, reported: set[int],
          skip: set[int]) -> list[tuple[str, str, int]]:
    """(time, line, comment id) for in-progress tickets whose executor has gone quiet."""
    cutoff = stamp(datetime.now(timezone.utc) - timedelta(hours=hours))
    candidates = [issue for issue in gh(f"repos/{repo}/issues?state=open&per_page=100")
                  if "pull_request" not in issue and issue["number"] not in skip and issue["comments"]
                  and issue["updated_at"] < cutoff
                  and not any(label["name"].startswith(config.labels.prefix) for label in issue["labels"])]
    running = running_issues(checkout, config.sessions, repo) | tmux_issues(tmux_panes()) if candidates else set()
    found = []
    for issue in candidates:
        if issue["number"] in running:
            continue
        last = gh(f"repos/{repo}/issues/{issue['number']}/comments?per_page=1&page={issue['comments']}")[-1]
        if (last["id"] not in reported and role(last["body"], config) == config.stations.executor
                and state_and_waiting(last["body"], config)[0] == config.status.in_progress):
            found.append((last["created_at"], f"#{issue['number']} executor silent for over {hours:g} hours, with no "
                                              f"running ding run or tmux job: {summary(last['body'], config)} "
                                              f"{last['html_url']}", last["id"]))
    return found


def idle(config: Config, repo: str, checkout: Path, observed: dict, reported: set[int],
         skip: set[int]) -> tuple[list[tuple[str, str, int]], dict]:
    """Idle in-progress executors, and first-observation times when board has no session update time."""
    if config.watch.idle_minutes == 0:
        return [], {}
    issues = [issue for issue in gh(f"repos/{repo}/issues?state=open&per_page=100")
              if "pull_request" not in issue and issue["number"] not in skip and issue["comments"]
              and not any(label["name"].startswith(config.labels.prefix) for label in issue["labels"])]
    if not issues:
        return [], {}
    output = board_output(checkout)
    sessions = {}
    for line in output.splitlines()[1:]:
        if not line.strip():
            break
        cells = line.split("\t")
        if len(cells) >= 6:
            for number in board_tickets(cells, config.sessions, repo):
                sessions.setdefault(number, []).append(cells[2])
    running = board_running(output, config.sessions, repo) | tmux_issues(tmux_panes())
    moment = datetime.now(timezone.utc)
    found, pending = [], {}
    for issue in issues:
        number = issue["number"]
        if (number in running or not sessions.get(number)
                or any(status not in {"idle", "notLoaded"} for status in sessions[number])):
            continue
        last = None
        for page in range((issue["comments"] + 99) // 100, 0, -1):
            comments = gh(f"repos/{repo}/issues/{number}/comments?per_page=100&page={page}")
            last = next((comment for comment in reversed(comments)
                         if role(comment["body"], config) == config.stations.executor), None)
            if last:
                break
        if (last is None or last["id"] in reported
                or state_and_waiting(last["body"], config)[0] != config.status.in_progress):
            continue
        previous = observed.get(str(number), {})
        since = previous.get("since") if previous.get("comment_id") == last["id"] else None
        # Board exposes no session update time; persist when all idle conditions were first observed.
        since = since or stamp(moment)
        pending[str(number)] = {"comment_id": last["id"], "since": since}
        began = max(datetime.fromisoformat(since.replace("Z", "+00:00")),
                    datetime.fromisoformat(last.get("updated_at", last["created_at"]).replace("Z", "+00:00")))
        minutes = (moment - began).total_seconds() / 60
        if minutes > config.watch.idle_minutes:
            found.append((stamp(moment), f"#{number} executor idle for {minutes:.0f} minutes while still in progress, "
                                        f"with no running ding run or tmux job: {summary(last['body'], config)} "
                                        f"{last['html_url']}", last["id"]))
    return found, pending


def default_state(repo: str) -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return state / "ding/watch" / f"{repo.replace('/', '-')}.json"


def watch(args, config: Config) -> int:
    repo = config.repository()
    path = args.state or default_state(repo)
    checkout = args.checkout or config.root
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(".lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"ding: another watch is using {path}; stop it before starting a new one", file=sys.stderr)
        return 3
    started = now()
    state = (json.loads(path.read_text()) if path.exists()
             else {"since": args.since or started, "seen": [], "stale": []})
    since, seen, reported = state["since"], set(state["seen"]), set(state.get("stale", []))
    observed, idle_reported = state.get("idle_observed", {}), set(state.get("idle_reported", []))
    skip = set(args.skip_issue) | ({config.log.issue} if config.log.issue else set())
    # Label changes made while the orchestrator was handling the last batch are its own; skip them.
    labels_since = state.get("labels_since", max(since, started) if path.exists() else since)
    print(f"Watching {repo}: comments and PRs since {since}, labels since {labels_since}", file=sys.stderr)
    errors, next_stale, next_idle = 0, 0.0, 0.0
    idle_every = min(config.watch.idle_minutes * 20, 900) if config.watch.idle_minutes else float("inf")
    while True:
        polled = now()
        try:
            found = collect(config, repo, since, labels_since, seen, skip)
            quiet, idle_found, pending, stale_ids = [], [], observed, set()
            checked_at = time.monotonic()
            checked_stale, checked_idle = checked_at >= next_stale, checked_at >= next_idle
            if checked_stale:
                quiet = stale(config, repo, checkout, args.stale_hours, reported, skip)
                stale_ids = {id_ for _, _, id_ in quiet}
            if checked_idle:
                idle_found, pending = idle(config, repo, checkout, observed, idle_reported, skip)
                quiet += [item for item in idle_found if item[2] not in {entry[2] for entry in quiet}]
            if any(wakes for _, _, wakes in found) or quiet:
                if found:
                    time.sleep(args.settle)
                    polled = now()
                    found = collect(config, repo, since, labels_since, seen, skip)
            errors = 0
        except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            errors += 1
            print(f"{polled} poll failed {errors}/{args.max_errors}: {error}", file=sys.stderr)
            if errors >= args.max_errors:
                print(f"{errors} polls failed in a row; the watch exits without advancing its state. "
                      "Rerun the same command once fixed.")
                return 2
            time.sleep(min(args.interval, idle_every))
            continue
        if checked_stale:
            next_stale = time.monotonic() + args.stale_every
        if checked_idle:
            next_idle = checked_at + idle_every
        wakes = any(wakes for _, _, wakes in found) or bool(quiet)
        changed = pending != observed
        observed = pending
        if wakes:
            for _, line in sorted([(time_, line) for time_, line, _ in found] + [(time_, line) for time_, line, _ in quiet]):
                print(line)
            print(f"{len(found) + len(quiet)} items ({since} to {polled}). "
                  "After handling them, start the watch again with the same command.")
            state.pop("labels_since", None)
            state.update(since=polled, seen=[line for time_, line, _ in found if time_ >= polled],
                         stale=sorted(reported | stale_ids),
                         idle_reported=sorted(idle_reported | {id_ for _, _, id_ in idle_found}))
        if changed and not wakes:
            state["labels_since"] = labels_since
        if wakes or changed:
            state["idle_observed"] = observed
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False))
            tmp.replace(path)
        if wakes:
            return 0
        time.sleep(min(args.interval, max(0, next_stale - time.monotonic()),
                       max(0, next_idle - time.monotonic())))
