"""Wait for GitHub activity the orchestrator must handle, print it, and exit; read-only, no model calls.

Wakes on:
- executor comments whose status is not "in progress", or whose "waiting on" names a station or the user;
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

from dkitchen.config import Config

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
    session signature; else the user. A session signature quoted inside a line signs nothing."""
    stations = config.stations
    first = next((line for line in body.splitlines() if line.strip()), "")
    first = re.sub(r"^[\s#*>]+", "", first)
    signed = next((name for name in (stations.designer, stations.verifier, stations.orchestrator)
                   if first.startswith(name)), None)
    if signed:
        return signed
    return stations.executor if re.search(r"(?m)^[ \t]*Codex session:", body) else stations.user


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


def board_running(output: str) -> set[int]:
    """Tickets whose session has a running `ding run`, from `ding board`'s ISSUE and RUN columns."""
    running = set()
    for line in output.splitlines():
        if not line.strip():
            break  # the unwrapped-process table follows a blank line
        cells = line.split("\t")
        if len(cells) >= 6 and cells[5] == "running":
            running.update(int(number) for number in re.findall(r"#(\d+)", cells[3]))
    return running


def running_issues(checkout: Path) -> set[int]:
    # The ISSUE column is filled only for sessions of the repository in the current directory.
    result = subprocess.run([sys.executable, "-m", "dkitchen.ding", "board"], capture_output=True, text=True,
                            timeout=120, cwd=checkout)
    if result.returncode:
        raise RuntimeError(f"ding board failed: {result.stderr.strip()[:200]}")
    return board_running(result.stdout)


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
    running = running_issues(checkout) | tmux_issues(tmux_panes()) if candidates else set()
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
    skip = set(args.skip_issue) | ({config.log.issue} if config.log.issue else set())
    # Label changes made while the orchestrator was handling the last batch are its own; skip them.
    labels_since = max(since, started) if path.exists() else since
    print(f"Watching {repo}: comments and PRs since {since}, labels since {labels_since}", file=sys.stderr)
    errors, next_stale = 0, 0.0
    while True:
        polled = now()
        try:
            found = collect(config, repo, since, labels_since, seen, skip)
            quiet = []
            if time.monotonic() >= next_stale:
                quiet = stale(config, repo, checkout, args.stale_hours, reported, skip)
                next_stale = time.monotonic() + args.stale_every
            errors = 0
        except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            errors += 1
            print(f"{polled} poll failed {errors}/{args.max_errors}: {error}", file=sys.stderr)
            if errors >= args.max_errors:
                print(f"{errors} polls failed in a row; the watch exits without advancing its state. "
                      "Rerun the same command once fixed.")
                return 2
            time.sleep(args.interval)
            continue
        if any(wakes for _, _, wakes in found) or quiet:
            if found:
                time.sleep(args.settle)
                polled = now()
                found = collect(config, repo, since, labels_since, seen, skip)
            for _, line in sorted([(time_, line) for time_, line, _ in found] + [(time_, line) for time_, line, _ in quiet]):
                print(line)
            print(f"{len(found) + len(quiet)} items ({since} to {polled}). "
                  "After handling them, start the watch again with the same command.")
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"since": polled, "seen": [line for time_, line, _ in found if time_ >= polled],
                                       "stale": sorted(reported | {id_ for _, _, id_ in quiet})}, ensure_ascii=False))
            tmp.replace(path)
            return 0
        time.sleep(args.interval)
