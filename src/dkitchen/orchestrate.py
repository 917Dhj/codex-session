"""The orchestrator's routine GitHub writes, each checked step by step.

- merge: merge a reviewed PR at the reviewed HEAD and print the merge commit; refuses drafts, moved HEADs,
  and tickets whose executor's latest status block neither waits on the orchestrator nor lists the PR as
  pending merge.
- relay: send a message to a ticket's executor with `ding send`, then leave the matching ticket comment. By default
  the message goes into the executor's current turn and replaces the orchestrator's relays still queued for it.
- log: post one entry to the orchestrator log, stamped with the current UTC time.
Every comment is signed with the orchestrator's station name, so `ding watch` can tell who wrote it.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import subprocess
import sys

from dkitchen import ding, watch
from dkitchen.config import Config


def signed(text: str, config: Config) -> str:
    signature = config.stations.signature(config.stations.orchestrator)
    text = text.strip()
    return text if text.startswith(signature.rstrip()) else signature + text


def log_body(items: list[str], pending: list[str], moment: datetime, config: Config) -> str:
    stations = config.stations
    lines = [f"{stations.signature(stations.orchestrator)}{moment.astimezone(timezone.utc):%H:%M} UTC",
             *(f"- {item}" for item in items),
             f"- {config.log.pending}{stations.separator}{'; '.join(pending) or config.status.none}"]
    return "\n".join(lines)


def call(config: Config, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=config.root, text=True, capture_output=True)


def fail(message: str) -> int:
    print(f"ding: {message}", file=sys.stderr)
    return 1


def comment(config: Config, repo: str, issue: int, body: str) -> int:
    result = call(config, "gh", "issue", "comment", str(issue), "--repo", repo, "--body", body)
    if result.returncode:
        return fail(f"comment failed: {result.stderr.strip()}")
    print(result.stdout.strip())
    return 0


def executor_status(config: Config, repo: str, issue: int) -> dict | None:
    """The latest executor comment on a ticket: its status block, whether it waits on the orchestrator, and its link."""
    result = call(config, "gh", "api", "--paginate", "--slurp", f"repos/{repo}/issues/{issue}/comments?per_page=100")
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    comments = [comment for page in json.loads(result.stdout) for comment in page]
    executor = [comment for comment in comments if watch.role(comment["body"], config) == config.stations.executor]
    if not executor:
        return None
    latest = executor[-1]
    names = config.status.fields
    block = "\n".join(line for line in latest["body"].splitlines()[:len(names)] if line.strip())
    _, waiting = watch.state_and_waiting(latest["body"], config)
    ready = not waiting.startswith(config.status.none) and config.stations.orchestrator in waiting
    listed = watch.fields(latest["body"], config).get(config.status.pending_merge, "")
    pending = {int(number) for number in re.findall(r"(?i)(?:PR\s*#?|#)\s*(\d+)", listed)}
    if listed and config.status.pending_merge not in names:
        block += "\n" + next(line for line in latest["body"].splitlines() if line.startswith(config.status.pending_merge))
    return {"block": block, "url": latest["html_url"], "ready": ready, "pending_merge": pending}


def ticket_references(body: str, repo: str, pr: int) -> list[int]:
    """Ticket numbers a PR body mentions, as #N or as a link to this repository."""
    links = rf"https://github\.com/{re.escape(repo)}/(?:issues|pull)/(\d+)"
    numbers = {int(n) for n in re.findall(rf"(?<![\w/&])#(\d+)\b", body or "")}
    numbers |= {int(n) for n in re.findall(links, body or "")}
    return sorted(numbers - {pr})


def merge(args, config: Config) -> int:
    repo = config.repository()

    def view() -> dict:
        result = call(config, "gh", "pr", "view", str(args.pr), "--repo", repo, "--json", "isDraft,state,headRefOid,mergeCommit,body")
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        return json.loads(result.stdout)

    try:
        pr = view()
        if pr["state"] == "MERGED":
            return fail(f"PR #{args.pr} was already merged as {pr['mergeCommit']['oid']}; nothing done")
        if not pr["headRefOid"].startswith(args.sha):
            return fail(f"PR #{args.pr} HEAD is now {pr['headRefOid']}, not the reviewed {args.sha}; review it again")
        if pr["isDraft"]:
            return fail(f"PR #{args.pr} is a draft and cannot be merged; its author marks it ready with gh pr ready first")
        if not getattr(args, "no_ticket", False):
            ticket, status = getattr(args, "ticket", None), None
            if ticket is None:
                found = {}
                for number in ticket_references(pr.get("body"), repo, args.pr):
                    candidate = executor_status(config, repo, number)
                    if candidate is not None:
                        found[number] = candidate
                if len(found) != 1:
                    listed = ", ".join(f"#{number}" for number in found) or "none"
                    return fail(f"cannot tell PR #{args.pr}'s ticket: tickets with executor comments mentioned in it: "
                                f"{listed}; pass --ticket N, or --no-ticket for a PR without one")
                (ticket, status), = found.items()
            else:
                status = executor_status(config, repo, ticket)
                if status is None:
                    return fail(f"#{ticket} has no executor comment; pass the PR's ticket with --ticket, "
                                "or --no-ticket for a PR without one")
            print(f"#{ticket} latest executor status ({status['url']}):\n{status['block']}")
            if not status["ready"] and args.pr not in status.get("pending_merge", ()):
                return fail(f"#{ticket}'s executor is not waiting on {config.stations.orchestrator} to merge, and its "
                            f"{config.status.pending_merge} field does not list PR #{args.pr}; read its latest "
                            "comment and wait until it asks for the merge. Nothing merged")
        result = call(config, "gh", "pr", "merge", str(args.pr), "--repo", repo, "--merge",
                      "--match-head-commit", pr["headRefOid"])
        if result.returncode:
            return fail(f"merge failed: {(result.stderr or result.stdout).strip()}")
        pr = view()
    except (RuntimeError, ValueError, KeyError, TypeError) as error:
        return fail(f"cannot read PR #{args.pr}: {error}")
    if pr["state"] != "MERGED":
        return fail(f"the merge command succeeded, but PR #{args.pr} is {pr['state']}; find out why before logging it")
    print(f"PR #{args.pr} merged as {pr['mergeCommit']['oid']}")
    return 0


def relay(args, config: Config) -> int:
    repo = config.repository()
    message = ding.message_text(args.message, args.message_file, None)
    target = args.to or f"{repo}#{args.ticket}"
    queue = getattr(args, "queue", False)
    text = signed(message, config)
    if not queue:
        signature = config.stations.signature(config.stations.orchestrator)
        text = f"{signature}{config.relay.superseding}\n{text[len(signature.rstrip()):].lstrip()}"
    send = ["send", target, "--from", config.stations.orchestrator, "--message", text]
    if not queue:
        send += ["--steer", "--supersede"]
    if getattr(args, "interrupt", False):
        send += ["--interrupt"]
    if args.socket:
        send += ["--socket", args.socket]
    if ding.main(send):
        return fail("ding send did not confirm delivery; check ~/.local/state/ding/sent.jsonl before sending again. "
                    "No comment was left.")
    if args.no_comment:
        return 0
    return comment(config, repo, args.ticket, signed(args.comment or message, config))


def log(args, config: Config) -> int:
    issue = args.issue or config.log.issue
    if issue is None:
        return fail("no orchestrator log ticket; set [log] issue in .dkitchen.toml or pass --issue")
    return comment(config, config.repository(), issue, log_body(args.item, args.pending, datetime.now(timezone.utc), config))
