"""The orchestrator's routine GitHub writes, each checked step by step.

- merge: merge a reviewed PR at the reviewed HEAD and print the merge commit; refuses drafts and moved HEADs.
- relay: send a message to a ticket's executor with `ding send`, then leave the matching ticket comment.
- log: post one entry to the orchestrator log, stamped with the current UTC time.
Every comment is signed with the orchestrator's station name, so `ding watch` can tell who wrote it.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import subprocess
import sys

from dkitchen import ding
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


def merge(args, config: Config) -> int:
    repo = config.repository()

    def view() -> dict:
        result = call(config, "gh", "pr", "view", str(args.pr), "--repo", repo, "--json", "isDraft,state,headRefOid,mergeCommit")
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
    send = ["send", target, "--from", config.stations.orchestrator, "--message", signed(message, config)]
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
