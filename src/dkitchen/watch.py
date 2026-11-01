"""Wait for GitHub activity the orchestrator must handle, print it, and exit; read-only, no model calls.

Wakes on:
- executor comments whose status is not "in progress", or whose "waiting on" names a station or the user;
- idle executors still marked "in progress", after [watch] idle_minutes with no running job;
- unanswered designer/verifier requests older than [watch] design_answer_minutes, once per executor comment;
- comments from the user (no station signature);
- status labels being added, and newly opened PRs;
- stale tickets: no newer status label, last comment an executor "in progress" older than --stale-hours,
  no running `ding run` for the ticket, and no live tmux job whose session name holds issue<N>.
On tickets labelled awaiting-user, executor comments wake only for blocked, done, and waiting on design,
or when their "waiting on" names the orchestrator, designer, or verifier.
In orchestrator mode, verifier comments never wake on their own, since the verifier also messages it;
they are listed in the next batch for reference.
The state file keeps the time already reported, so rerunning the same command loses nothing.
One watch runs per state file; a second one exits at once.
With --for NAME, executor requests and explicit handoffs to that station wake; designer rulings do not.
Aliases and width-normalized names share the longest-name match. Configured paused executors never wake --for.
Awaiting-user labels always suppress idle/stale checks; other status labels suppress only when newer than the comment.
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
import unicodedata
from urllib.parse import quote

from dkitchen import truncate
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


def normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def signature_name(prefix: str, config: Config) -> str:
    return prefix.rstrip().removesuffix(config.stations.separator.rstrip()).strip()


def designer_stations(config: Config) -> list[str]:
    names = [config.stations.designer]
    names += [signature_name(signature, config) for signature in config.designer_signatures]
    unique = {}
    for name in names:
        if name:
            unique.setdefault(normalized(name), name)
    return list(unique.values())


def station_variants(station: str, config: Config) -> set[str]:
    target = normalized(station)
    result = {target}
    for name, aliases in config.stations.aliases.items():
        group = {normalized(name), *(normalized(alias) for alias in aliases)}
        if target in group:
            result.update(group)
    return result


def station_names(config: Config) -> list[str]:
    stations = config.stations
    names = [stations.orchestrator, *designer_stations(config), stations.verifier, stations.executor,
             stations.user, *stations.aliases]
    return list({normalized(name): name for name in names}.values())


def named(text: str, station: str, config: Config, *, markers: tuple[str, ...] = (), signature: bool = False) -> bool:
    text = normalized(text)
    targets = station_variants(station, config)
    stations = config.stations
    names = station_names(config)
    choices = targets | {variant for name in names for variant in station_variants(name, config)}
    pattern = "|".join(map(re.escape, sorted(choices, key=len, reverse=True)))
    for match in re.finditer(pattern, text):
        if match.group() not in targets:
            continue
        if not markers and not signature:
            return True
        before = text[:match.start()]
        if markers and before.endswith(markers):
            return True
        if (signature and not before.rsplit("\n", 1)[-1].strip()
                and text[match.end():].startswith(normalized(stations.separator).rstrip())):
            return True
    return False


def role(body: str, config: Config) -> str:
    """The station named at the start of the first line; otherwise the executor by a line starting with its
    session signature (`Codex session:`, or `Claude session:` for an executor run in Claude Code); else the user.
    A session signature quoted inside a line signs nothing."""
    stations = config.stations
    first_line = next((line for line in body.splitlines() if line.strip()), "")
    first_line = normalized(first_line)
    if config.merge.review_check and re.fullmatch(r"审过 [0-9a-fA-F]{8}:(?:通过|有出入,见下)", first_line):
        return stations.verifier
    first = re.sub(r"^[\s#*>]+", "", first_line)
    if (any(first_line.startswith(normalized(prefix)) or first.startswith(normalized(prefix))
            for prefix in config.designer_signatures)
            or any(first.startswith(variant + normalized(stations.separator))
                   for prefix in config.designer_signatures
                   for variant in station_variants(signature_name(prefix, config), config))):
        return stations.designer
    signed = next((name for name in (stations.verifier, stations.orchestrator)
                   if any(first.startswith(variant) for variant in station_variants(name, config))), None)
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
    names = lambda *who: any(waiting_for(body, name, config) for name in who)
    kitchen = (stations.orchestrator, *designer_stations(config), stations.verifier)
    if waiting_user:
        return state in {status.blocked, status.done, status.waiting_on_design} or names(*kitchen)
    return state not in {status.in_progress, status.paused} or names(*kitchen, stations.user)


def waiting_for(body: str, station: str, config: Config) -> bool:
    _, waiting = state_and_waiting(body, config)
    if waiting.startswith(config.status.none):
        return False
    return named(waiting, station, config)


def design_request(body: str, config: Config) -> list[str]:
    state, _ = state_and_waiting(body, config)
    if state == config.status.paused:
        return []
    designers = [name for name in designer_stations(config) if waiting_for(body, name, config)]
    return designers or ([config.stations.designer] if state == config.status.waiting_on_design else [])


def station_wakes(body: str, station: str, config: Config) -> bool:
    who = role(body, config)
    if who == config.stations.designer:
        return False
    if who == config.stations.executor:
        state, _ = state_and_waiting(body, config)
        if state == config.status.paused:
            return False
        if waiting_for(body, station, config):
            return True
        if (state == config.status.waiting_on_design
                and not any(waiting_for(body, name, config) for name in designer_stations(config))
                and station_variants(station, config) & station_variants(config.stations.designer, config)):
            return True
        next_step = fields(body, config).get(config.status.fields[3], "") if len(config.status.fields) > 3 else ""
        return named(next_step, station, config, markers=("交",))
    return named(body, station, config, markers=("@", "交"), signature=True)


def summary(body: str, config: Config) -> str:
    """The status block's first two lines, or the first line when there is no block."""
    count, names = len(config.status.fields), config.status.fields[:2]
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    picked = [line for line in lines[:count] if line.startswith(names)] or lines[:1]
    return truncate("; ".join(picked), 160)


def collect(config: Config, repo: str, since: str, labels_since: str, seen: set[str],
            skip: set[int], station: str | None = None) -> list[tuple[str, str, bool]]:
    """(time, line, wakes) for every item after the given times; comment and PR times use `since`."""
    awaiting = config.labels.awaiting_user
    waiting = ({issue["number"] for issue in gh(f"repos/{repo}/issues?state=open&labels={awaiting}&per_page=100")}
               if station is None else set())
    verifier, user, executor = config.stations.verifier, config.stations.user, config.stations.executor
    found = []
    for comment in newer(f"repos/{repo}/issues/comments?since={since}&sort=updated&direction=desc", "updated_at", since):
        number, body = issue_number(comment["issue_url"]), comment["body"]
        who = role(body, config)
        if number in skip:
            continue
        if station is not None:
            if not station_wakes(body, station, config):
                continue
        elif not (who in {verifier, user} or (who == executor and executor_wakes(body, number in waiting, config))):
            continue
        kind = f"{who} comment" if comment["created_at"] >= since else f"{who} edited a comment"
        note = f" ({verifier.lower()} reports separately; for reference)" if who == verifier and station is None else ""
        found.append((comment["updated_at"], f"#{number} {kind}{note}: {summary(body, config)} {comment['html_url']}",
                      station is not None or who != verifier))
    if station is None:
        for event in newer(f"repos/{repo}/issues/events?", "created_at", labels_since):
            label = (event.get("label") or {}).get("name", "")
            if event["event"] == "labeled" and label.startswith(config.labels.prefix) and event["issue"]["number"] not in skip:
                found.append((event["created_at"], f"#{event['issue']['number']} label +{label} {event['issue']['html_url']}", True))
        for pull in newer(f"repos/{repo}/pulls?state=all&sort=created&direction=desc", "created_at", since, pages=1):
            found.append((pull["created_at"], f"PR #{pull['number']} opened: {truncate(pull['title'], 80)} {pull['html_url']}", True))
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
        raise RuntimeError(f"ding board failed: {truncate(result.stderr.strip(), 200)}")
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


def open_issues(repo: str):
    page, seen = 1, set()
    while True:
        issues = gh(f"repos/{repo}/issues?state=open&per_page=100&page={page}")
        for issue in issues:
            if issue["number"] not in seen:
                seen.add(issue["number"])
                yield issue
        if len(issues) < 100:
            return
        page += 1


def comments_backwards(repo: str, issue: dict):
    for page in range((issue["comments"] + 99) // 100, 0, -1):
        yield from reversed(gh(f"repos/{repo}/issues/{issue['number']}/comments?per_page=100&page={page}"))


def label_suppresses(config: Config, repo: str, issue: dict, comment: dict) -> bool:
    labels = {label["name"] for label in issue["labels"] if label["name"].startswith(config.labels.prefix)}
    if config.labels.awaiting_user in {label["name"] for label in issue["labels"]}:
        return True
    if not labels:
        return False
    added = {}
    page = 1
    while True:
        events = gh(f"repos/{repo}/issues/{issue['number']}/events?per_page=100&page={page}")
        for event in events:
            name = (event.get("label") or {}).get("name")
            if event["event"] == "labeled" and name in labels:
                added[name] = max(added.get(name, ""), event["created_at"])
        if len(events) < 100:
            break
        page += 1
    return any(added.get(name, "") > comment["created_at"] for name in labels)


def answered_by(body: str, station: str, config: Config) -> bool:
    who = role(body, config)
    first = normalized(next((line for line in body.splitlines() if line.strip()), ""))
    first = re.sub(r"^[\s#*>]+", "", first)
    if who == config.stations.designer:
        for prefix in sorted(config.designer_signatures, key=len, reverse=True):
            if first.startswith(normalized(prefix)):
                return bool(station_variants(station, config) & station_variants(signature_name(prefix, config), config))
    elif station_variants(station, config) & station_variants(who, config):
        return True
    return named(first, station, config, signature=True)


def unanswered_waits(config: Config, repo: str, minutes: float, skip: set[int], *,
                     stations: list[str] | None = None, design_fallback: bool = False) -> list[dict]:
    names = station_names(config) if stations is None else stations
    moment = datetime.now(timezone.utc)
    found = []
    for issue in open_issues(repo):
        if "pull_request" in issue or issue["number"] in skip or not issue["comments"]:
            continue
        answered = set()
        for comment in comments_backwards(repo, issue):
            who = role(comment["body"], config)
            if who != config.stations.executor:
                answered.update(name for name in names if answered_by(comment["body"], name, config))
                if design_fallback and who == config.stations.designer:
                    # Existing watch reminders accept a reply from any designer station.
                    answered.update(set(names) & set(designer_stations(config)))
                if len(answered) == len(names):
                    break
                continue
            if state_and_waiting(comment["body"], config)[0] == config.status.paused:
                break
            implicit = design_request(comment["body"], config) if design_fallback else []
            elapsed = (moment - datetime.fromisoformat(comment["created_at"].replace("Z", "+00:00"))).total_seconds() / 60
            if elapsed > minutes:
                for name in names:
                    if name not in answered and (waiting_for(comment["body"], name, config) or name in implicit):
                        found.append({"ticket": issue["number"], "station": name, "minutes": elapsed,
                                      "summary": summary(comment["body"], config), "url": comment["html_url"],
                                      "comment_id": comment["id"], "created_at": comment["created_at"]})
            break
    return sorted(found, key=lambda item: (item["created_at"], item["ticket"], item["station"]))


def pr_waits(config: Config, repo: str, skip: set[int]) -> list[dict]:
    if not config.merge.review_check:
        return []
    from dkitchen import orchestrate
    owner, name = repo.split("/", 1)
    query = """query($owner:String!, $name:String!, $endCursor:String) {
      repository(owner:$owner, name:$name) {
        defaultBranchRef { name }
        pullRequests(states:OPEN, first:100, after:$endCursor) {
          pageInfo { hasNextPage endCursor }
          nodes { number state isDraft baseRefName headRefOid updatedAt url
            labels(first:100) { nodes { name } pageInfo { hasNextPage } }
            commits(last:1) { nodes { commit { oid statusCheckRollup {
              contexts(first:100) { pageInfo { hasNextPage } nodes {
                ... on CheckRun { name status conclusion detailsUrl }
                ... on StatusContext { context state targetUrl }
              } }
            } } } }
          }
        }
      }
    }"""
    response = orchestrate.call(config, "gh", "api", "graphql", "--paginate", "--slurp",
                                "-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}")
    if response.returncode:
        raise RuntimeError(f"cannot read PR readiness: {response.stderr.strip()}")
    found = []
    for page in json.loads(response.stdout):
        if page.get("errors"):
            raise RuntimeError(f"cannot read PR readiness: {page['errors']}")
        repository = page["data"]["repository"]
        branch = repository["defaultBranchRef"]["name"]
        for pr in repository["pullRequests"]["nodes"]:
            if pr["number"] in skip:
                continue
            commit = pr["commits"]["nodes"][-1]["commit"]
            checks = {}
            if commit["oid"] != pr["headRefOid"]:
                raise RuntimeError(f"PR #{pr['number']} HEAD moved during readiness query")
            rollup = commit["statusCheckRollup"]
            contexts = rollup["contexts"] if rollup else {"nodes": [], "pageInfo": {"hasNextPage": False}}
            if contexts["pageInfo"]["hasNextPage"]:
                checks = orchestrate.head_checks(config, repo, pr["headRefOid"])
            else:
                for check in contexts["nodes"]:
                    if "context" in check:
                        key, state, url = check["context"], check["state"].lower(), check["targetUrl"]
                    else:
                        key, url = check["name"], check["detailsUrl"]
                        state = (check["conclusion"] or "pending").lower() if check["status"] == "COMPLETED" else "pending"
                    checks.setdefault(key, []).append((state, url))
            result = orchestrate.readiness(config, repo, pr, branch, checks)
            if pr["isDraft"] and result["review"] != "failure":
                continue
            if pr["labels"]["pageInfo"]["hasNextPage"]:
                raise RuntimeError(f"PR #{pr['number']} has too many labels to route its review safely")
            reviewers = [label["name"][7:].strip() for label in pr["labels"]["nodes"]
                         if label["name"].startswith("review:") and label["name"][7:].strip()]
            reviewer = reviewers[0] if reviewers else config.stations.verifier
            kind = "blocked"
            if result["ready"]:
                kind = "ready"
            elif result["review"] == "failure":
                kind = "review-failed"
            elif result["review"] in {"missing", "pending"}:
                kind = "review-missing"
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(pr["updatedAt"].replace("Z", "+00:00"))).total_seconds() / 60
            found.append({"ticket": pr["number"], "head": pr["headRefOid"], "kind": kind,
                          "station": reviewer if kind == "review-missing" else config.stations.orchestrator,
                          "minutes": elapsed, "created_at": pr["updatedAt"], "url": pr["url"],
                          "summary": kind + (": " + "; ".join(result["reasons"]) if result["reasons"] else ""),
                          "pending_only": result["pending_only"], "reasons": result["reasons"]})
    return found


def pr_wakes(config: Config, repo: str, station: str | None, reported: set[str], observed: dict,
             skip: set[int]) -> tuple[list[tuple[str, str, str]], dict]:
    found, pending = [], {}
    moment = now()
    target = station or config.stations.orchestrator
    orchestrator = bool(station_variants(target, config) & station_variants(config.stations.orchestrator, config))
    for item in pr_waits(config, repo, skip):
        kind = item["kind"]
        key = f"{item['ticket']}:{item['head']}:{kind}"
        since = observed.get(key, moment)
        pending[key] = since
        line = f"PR #{item['ticket']} {item['summary']}; waiting on {item['station']} {item['url']}"
        if (not (kind == "blocked" and item["pending_only"]) and key not in reported and
                station_variants(target, config) & station_variants(item["station"], config)):
            found.append((moment, line, key))
        elapsed = (datetime.fromisoformat(moment.replace("Z", "+00:00")) -
                   datetime.fromisoformat(since.replace("Z", "+00:00"))).total_seconds() / 60
        backstop = key + ":overdue"
        if (orchestrator and kind in {"ready", "review-missing", "blocked"} and config.watch.design_answer_minutes
                and elapsed > config.watch.design_answer_minutes and backstop not in reported):
            found.append((moment, line + f"; overdue for {elapsed:.0f} minutes", backstop))
    return found, pending


def unrouted_waits(config: Config, repo: str, checkout: Path, reported: set[int],
                   skip: set[int]) -> list[tuple[str, str, int]]:
    if not config.watch.unrouted_wait_minutes:
        return []
    cutoff = stamp(datetime.now(timezone.utc) - timedelta(minutes=config.watch.unrouted_wait_minutes))
    candidates = []
    for issue in open_issues(repo):
        if "pull_request" in issue or issue["number"] in skip or not issue["comments"]:
            continue
        latest = None
        for comment in comments_backwards(repo, issue):
            latest = latest or comment
            if role(comment["body"], config) != config.stations.executor:
                continue
            state, waiting = state_and_waiting(comment["body"], config)
            if (state == config.status.in_progress and waiting and not waiting.startswith(config.status.none)
                    and not any(named(waiting, name, config) for name in station_names(config))
                    and comment["id"] not in reported
                    and max(latest.get("updated_at", latest["created_at"]),
                            comment.get("updated_at", comment["created_at"])) < cutoff):
                candidates.append((issue, comment))
            break
    running = running_issues(checkout, config.sessions, repo) | tmux_issues(tmux_panes()) if candidates else set()
    return [(comment["created_at"], f"#{issue['number']} unrouted wait for over "
             f"{config.watch.unrouted_wait_minutes:g} minutes, with no running ding run or tmux job: "
             f"{summary(comment['body'], config)} {comment['html_url']}", comment["id"])
            for issue, comment in candidates if issue["number"] not in running]


def waits(args, config: Config) -> int:
    names = station_names(config)
    if args.station is not None:
        names = [name for name in names if station_variants(name, config) & station_variants(args.station, config)]
    minutes = config.watch.design_answer_minutes if args.minutes is None else args.minutes
    found = unanswered_waits(config, config.repository(), minutes, set(), stations=names) if names else []
    found += [item for item in pr_waits(config, config.repository(), set())
              if item["minutes"] > minutes and (args.station is None or
                 station_variants(args.station, config) & station_variants(item["station"], config))]
    found.sort(key=lambda item: (item["created_at"], item["ticket"], item["station"]))
    if args.json:
        print(json.dumps(found, ensure_ascii=False))
    else:
        for item in found:
            print(f"{'PR ' if 'head' in item else ''}#{item['ticket']} waiting on {item['station']} for {item['minutes']:.0f} minutes: "
                  f"{item['summary']} {item['url']}")
    return 0


def unanswered_design(config: Config, repo: str, reported: set[int], skip: set[int]) -> list[tuple[str, str, int]]:
    if config.watch.design_answer_minutes == 0:
        return []
    designers = designer_stations(config)
    found = {}
    for item in unanswered_waits(config, repo, config.watch.design_answer_minutes, skip,
                                 stations=[*designers, config.stations.verifier], design_fallback=True):
        if item["comment_id"] not in reported:
            found.setdefault(item["comment_id"], (item, []))[1].append(item["station"])
    return [(item["created_at"], f"#{item['ticket']} waiting on {', '.join(names)} for {item['minutes']:.0f} minutes "
             f"with no {'designer' if all(name in designers for name in names) else 'station'} reply: "
             f"{item['summary']} {item['url']}", item["comment_id"]) for item, names in found.values()]


def stale(config: Config, repo: str, checkout: Path, hours: float, reported: set[int],
          skip: set[int]) -> list[tuple[str, str, int]]:
    """(time, line, comment id) for in-progress tickets whose executor has gone quiet."""
    cutoff = stamp(datetime.now(timezone.utc) - timedelta(hours=hours))
    candidates = [issue for issue in open_issues(repo)
                  if "pull_request" not in issue and issue["number"] not in skip and issue["comments"]
                  and issue["updated_at"] < cutoff
                  and not any(label["name"] == config.labels.awaiting_user for label in issue["labels"])]
    running = running_issues(checkout, config.sessions, repo) | tmux_issues(tmux_panes()) if candidates else set()
    found = []
    for issue in candidates:
        if issue["number"] in running:
            continue
        last = gh(f"repos/{repo}/issues/{issue['number']}/comments?per_page=1&page={issue['comments']}")[-1]
        if (last["id"] not in reported and role(last["body"], config) == config.stations.executor
                and state_and_waiting(last["body"], config)[0] == config.status.in_progress
                and not label_suppresses(config, repo, issue, last)):
            found.append((last["created_at"], f"#{issue['number']} executor silent for over {hours:g} hours, with no "
                                              f"running ding run or tmux job: {summary(last['body'], config)} "
                                              f"{last['html_url']}", last["id"]))
    return found


def idle(config: Config, repo: str, checkout: Path, observed: dict, reported: set[int],
         skip: set[int]) -> tuple[list[tuple[str, str, int]], dict]:
    """Idle in-progress executors, and first-observation times when board has no session update time."""
    if config.watch.idle_minutes == 0:
        return [], {}
    issues = [issue for issue in open_issues(repo)
              if "pull_request" not in issue and issue["number"] not in skip and issue["comments"]
              and not any(label["name"] == config.labels.awaiting_user for label in issue["labels"])]
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
        last = next((comment for comment in comments_backwards(repo, issue)
                     if role(comment["body"], config) == config.stations.executor), None)
        if (last is None or last["id"] in reported
                or state_and_waiting(last["body"], config)[0] != config.status.in_progress
                or label_suppresses(config, repo, issue, last)):
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


def default_state(repo: str, station: str | None = None) -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    suffix = ".for-" + quote(station, safe="") if station is not None else ""
    return state / "ding/watch" / f"{repo.replace('/', '-')}{suffix}.json"


def watch(args, config: Config) -> int:
    try:
        discarded = os.path.samestat(os.fstat(sys.stdout.fileno()), os.stat(os.devnull))
    except (AttributeError, OSError, ValueError):
        discarded = False
    if discarded:
        print("ding: watch stdout is /dev/null; use a pipe or a file so its batch is not lost", file=sys.stderr)
        return 3
    repo = config.repository()
    station = getattr(args, "station", None)
    if station is not None and station_variants(station, config) & station_variants(config.stations.orchestrator, config):
        station = None
    collect_options = {"station": station} if station is not None else {}
    path = args.state or default_state(repo, station)
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
    since, seen, reported = state["since"], state["seen"], set(state.get("stale", []))
    seen = seen if isinstance(seen, dict) else dict.fromkeys(seen, since)
    observed, idle_reported = state.get("idle_observed", {}), set(state.get("idle_reported", []))
    design_reported = set(state.get("design_reported", []))
    unrouted_reported = set(state.get("unrouted_reported", []))
    pr_reported, pr_observed = set(state.get("pr_reported", [])), state.get("pr_observed", {})
    skip = set(args.skip_issue) | ({config.log.issue} if config.log.issue else set())
    # Label changes made while the orchestrator was handling the last batch are its own; skip them.
    labels_since = state.get("labels_since", max(since, started) if path.exists() else since)
    detail = (f"comments and PRs since {since}, labels since {labels_since}" if station is None
              else f"requests and handoffs to {station!r} since {since}")
    print(f"Watching {repo}: {detail}", file=sys.stderr)
    errors = 0
    next_stale = next_idle = 0.0 if station is None else float("inf")
    threshold = config.watch.idle_minutes or config.watch.design_answer_minutes or config.watch.unrouted_wait_minutes
    idle_every = min(threshold * 20, 900) if station is None and threshold else float("inf")
    while True:
        polled = now()
        try:
            found = collect(config, repo, since, labels_since, seen, skip, **collect_options)
            pr_found, pr_pending = pr_wakes(config, repo, station, pr_reported, pr_observed, skip)
            quiet, idle_found, design_found, pending, stale_ids = [], [], [], observed, set()
            unrouted_found = []
            checked_at = time.monotonic()
            checked_stale, checked_idle = checked_at >= next_stale, checked_at >= next_idle
            if checked_stale:
                quiet = stale(config, repo, checkout, args.stale_hours, reported, skip)
                stale_ids = {id_ for _, _, id_ in quiet}
            if checked_idle:
                idle_found, pending = idle(config, repo, checkout, observed, idle_reported, skip)
                design_found = unanswered_design(config, repo, design_reported, skip)
                quiet += [item for item in idle_found if item[2] not in {entry[2] for entry in quiet}]
                quiet += design_found
                unrouted_found = unrouted_waits(config, repo, checkout, unrouted_reported, skip)
                quiet += unrouted_found
            quiet += pr_found
            if any(wakes for _, _, wakes in found) or quiet:
                if found:
                    time.sleep(args.settle)
                    polled = now()
                    found = collect(config, repo, since, labels_since, seen, skip, **collect_options)
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
        changed = pending != observed or pr_pending != pr_observed
        observed, pr_observed = pending, pr_pending
        if wakes:
            for _, line in sorted([(time_, line) for time_, line, _ in found] + [(time_, line) for time_, line, _ in quiet]):
                print(line)
            print(f"{len(found) + len(quiet)} items ({since} to {polled}). "
                  "After handling them, start the watch again with the same command.")
            state.pop("labels_since", None)
            # A line stamped after the poll stays in the next query window, so it stays seen until the clock passes it.
            lines = {**seen, **{line: time_ for time_, line, _ in found}}
            state.update(since=polled, seen={line: time_ for line, time_ in lines.items() if time_ >= polled},
                         stale=sorted(reported | stale_ids),
                         idle_reported=sorted(idle_reported | {id_ for _, _, id_ in idle_found}),
                         design_reported=sorted(design_reported | {id_ for _, _, id_ in design_found}),
                         unrouted_reported=sorted(unrouted_reported | {id_ for _, _, id_ in unrouted_found}),
                         pr_reported=sorted(pr_reported | {key for _, _, key in pr_found}))
        if changed and not wakes:
            state["labels_since"] = labels_since
        if wakes or changed:
            state["idle_observed"] = observed
            state["pr_observed"] = pr_observed
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False))
            tmp.replace(path)
        if wakes:
            return 0
        time.sleep(min(args.interval, max(0, next_stale - time.monotonic()),
                       max(0, next_idle - time.monotonic())))
