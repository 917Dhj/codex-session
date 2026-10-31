"""The orchestrator's routine GitHub writes, each checked step by step.

- merge: merge a reviewed PR at the reviewed HEAD and print the merge commit; refuses drafts, moved HEADs,
  wrong bases, and unsuccessful required checks; legacy ticket gates apply without review_check.
- relay: send a message to a ticket's executor with `ding send`, then leave the matching ticket comment. By default
  the message goes into the executor's current turn and replaces the orchestrator's relays still queued for it.
- log: post one entry to the orchestrator log, stamped with the current UTC time.
Relay and log comments carry the orchestrator signature; review comments identify the reviewed commit.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
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


def head_checks(config: Config, repo: str, sha: str) -> dict:
    """Read the latest check runs and statuses on the exact reviewed commit, without polling."""
    if not (config.merge.required_checks or config.merge.review_check):
        return {}
    checks = {}
    for endpoint in ("check-runs?filter=latest&per_page=100", "status?per_page=100"):
        result = call(config, "gh", "api", "--paginate", "--slurp", f"repos/{repo}/commits/{sha}/{endpoint}")
        if result.returncode:
            raise RuntimeError(f"cannot read required checks on {sha}: {result.stderr.strip()}")
        for page in json.loads(result.stdout):
            if endpoint.startswith("check-runs"):
                for run in page["check_runs"]:
                    if run["head_sha"] == sha:
                        conclusion = run["conclusion"] if run["status"] == "completed" else "pending"
                        checks.setdefault(run["name"], []).append((conclusion, run["html_url"]))
            elif page["sha"] == sha:
                for status in page["statuses"]:
                    checks.setdefault(status["context"], []).append((status["state"], status["target_url"]))
    return checks


def default_branch(config: Config, repo: str) -> str:
    result = call(config, "gh", "api", f"repos/{repo}")
    if result.returncode:
        raise RuntimeError(f"cannot read default branch: {result.stderr.strip()}")
    branch = json.loads(result.stdout)["default_branch"]
    if not isinstance(branch, str) or not branch:
        raise ValueError("repository has no default branch")
    return branch


def readiness(config: Config, repo: str, pr: dict, branch: str) -> dict:
    """GitHub facts on this HEAD, shared by merge, watch and waits."""
    reasons = []
    if pr["state"] != "OPEN":
        reasons.append(f"PR is {pr['state']}")
    if pr["isDraft"]:
        reasons.append("PR is a draft")
    if pr["baseRefName"] != branch:
        reasons.append(f"PR base is {pr['baseRefName']}, not repository default branch {branch}")
    sha = pr["headRefOid"]
    checks = head_checks(config, repo, sha)
    required = config.merge.required_checks + ((config.merge.review_check,) if config.merge.review_check else ())
    for name in dict.fromkeys(required):
        for state, url in checks.get(name, [("missing", None)]):
            if state in {"pending", "missing", None}:
                reasons.append(f"check {name} has not finished on {sha} ({state or 'pending'})"
                               + (f": {url}" if url else ""))
            elif state != "success":
                reasons.append(f"check {name} is {state} on {sha}" + (f": {url}" if url else ""))
    review = [state for state, _ in checks.get(config.merge.review_check, [])]
    review_state = "missing"
    if review:
        review_state = "success" if all(state == "success" for state in review) else "pending"
        if "failure" in review:
            review_state = "failure"
    return {"ready": not reasons, "reasons": reasons, "review": review_state}


def review(args, config: Config) -> int:
    if not config.merge.review_check:
        return fail("ding review requires [merge] review_check")
    repo = config.repository()
    result = call(config, "gh", "pr", "view", str(args.pr), "--repo", repo, "--json", "headRefOid")
    if result.returncode:
        return fail(f"cannot read PR HEAD: {result.stderr.strip()}")
    try:
        sha = json.loads(result.stdout)["headRefOid"]
        if not isinstance(sha, str) or not sha:
            raise ValueError("missing HEAD")
    except (ValueError, KeyError, TypeError) as error:
        return fail(f"cannot read PR HEAD: {error}")
    if not args.sha or not sha.startswith(args.sha):
        return fail(f"PR #{args.pr} HEAD is now {sha}, not the reviewed {args.sha}; review it again")
    verdict = "通过" if args.verdict == "pass" else "有出入，见下"
    body = f"审过 {sha[:8]}：{verdict}\n\n{Path(args.body_file).read_text(encoding='utf-8')}"
    result = call(config, "gh", "api", f"repos/{repo}/issues/{args.pr}/comments", "-f", f"body={body}")
    if result.returncode:
        return fail(f"review comment failed: {result.stderr.strip()}; no status written")
    try:
        url = json.loads(result.stdout)["html_url"]
        if not isinstance(url, str) or not url:
            raise ValueError("missing comment URL")
    except (ValueError, KeyError, TypeError) as error:
        return fail(f"cannot read review comment response: {error}; inspect PR #{args.pr} before retrying; no status written")
    result = call(config, "gh", "api", f"repos/{repo}/statuses/{sha}",
                  "-f", f"state={'success' if args.verdict == 'pass' else 'failure'}",
                  "-f", f"context={config.merge.review_check}", "-f", f"target_url={url}")
    if result.returncode:
        return fail(f"review status failed: {result.stderr.strip()}; comment exists at {url}")
    print(f"{url}\n{config.merge.review_check}: {args.verdict} on {sha}")
    return 0


def merge(args, config: Config) -> int:
    repo = config.repository()

    def view() -> dict:
        result = call(config, "gh", "pr", "view", str(args.pr), "--repo", repo, "--json", "isDraft,state,headRefOid,mergeCommit,body,baseRefName")
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        return json.loads(result.stdout)

    try:
        pr = view()
        if pr["state"] == "MERGED":
            return fail(f"PR #{args.pr} was already merged as {pr['mergeCommit']['oid']}; nothing done")
        if not args.sha or not pr["headRefOid"].startswith(args.sha):
            return fail(f"PR #{args.pr} HEAD is now {pr['headRefOid']}, not the reviewed {args.sha}; review it again")
        if pr["isDraft"]:
            return fail(f"PR #{args.pr} is a draft and cannot be merged; its author marks it ready with gh pr ready first")
        if pr["state"] != "OPEN":
            return fail(f"PR #{args.pr} is {pr['state']} and cannot be merged")
        branch = default_branch(config, repo)
        if pr["baseRefName"] != branch:
            return fail(f"PR base is {pr['baseRefName']}, not repository default branch {branch}")
        if config.merge.review_check and (getattr(args, "ticket", None) is not None or getattr(args, "no_ticket", False)):
            print("ding: --ticket/--no-ticket ignored because [merge] review_check is set")
        if not config.merge.review_check and not getattr(args, "no_ticket", False):
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
        failures = readiness(config, repo, pr, branch)["reasons"]
        if failures:
            return fail("PR readiness blocks merge:\n" + "\n".join(failures))
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
