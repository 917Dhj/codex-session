"""Night orders live in the orchestrator log's comments, with no local state or model calls."""
from __future__ import annotations

from datetime import datetime, timezone
import json

from dkitchen.config import Config
from dkitchen.orchestrate import call, comment, fail


QUERY = """query($owner: String!, $name: String!, $number: Int!, $before: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(last: 100, before: $before) {
        nodes { body url }
        pageInfo { hasPreviousPage startCursor }
      }
    }
  }
}"""


def end_time(value: str) -> datetime:
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            raise ValueError("missing timezone")
        return moment.astimezone(timezone.utc)
    except ValueError as error:
        raise ValueError("night orders need an ISO time with timezone, e.g. 2026-10-31T08:00:00Z") from error


def open_orders(config: Config, repo: str, moment: datetime) -> dict | None:
    owner, name = repo.split("/")
    before = None
    while True:
        arguments = ["gh", "api", "graphql", "-f", f"query={QUERY}", "-f", f"owner={owner}",
                     "-f", f"name={name}", "-F", f"number={config.log.issue}"]
        if before is not None:
            arguments += ["-f", f"before={before}"]
        result = call(config, *arguments)
        if result.returncode:
            raise RuntimeError(f"cannot read night orders: {result.stderr.strip()}")
        try:
            data = json.loads(result.stdout)
            if data.get("errors"):
                raise ValueError(data["errors"])
            comments = data["data"]["repository"]["issue"]["comments"]
            page = comments["pageInfo"]
            for item in reversed(comments["nodes"]):
                first = item["body"].split("\n", 1)[0]
                if first == config.night.closed or first.startswith(config.night.closed + " at "):
                    return None
                prefix = config.night.heading + ", until "
                if first.startswith(prefix):
                    if not first.endswith(")"):
                        raise ValueError("night orders heading must end with ')'")
                    return item if end_time(first[len(prefix):-1]) > moment else None
            if not page["hasPreviousPage"]:
                return None
            cursor = page["startCursor"]
            if not cursor or cursor == before:
                raise ValueError("missing or repeated comments cursor")
            before = cursor
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"cannot read night orders: {error}") from error


def night(args, config: Config) -> int:
    if config.log.issue is None:
        return fail("no orchestrator log ticket; set [log] issue in .dkitchen.toml")
    moment = datetime.now(timezone.utc)
    if args.night_action == "open":
        until = end_time(args.until)
        if until <= moment:
            return fail("--until must be in the future")
        body = args.body_file.read_text(encoding="utf-8")
        if not body.strip():
            return fail("night orders body file must not be empty")
    repo = config.repository()
    current = open_orders(config, repo, moment)
    if args.night_action == "show":
        print(f"{current['body']}\n{current['url']}" if current else "no night orders open")
        return 0
    if args.night_action == "open" and current and not args.replace:
        return fail(f"night orders are already open: {current['url']}; use --replace to close and replace them")
    if current:
        result = comment(config, repo, config.log.issue, f"{config.night.closed} at {moment:%Y-%m-%dT%H:%M:%SZ}")
        if result:
            return result
    if args.night_action == "open":
        heading = f"{config.night.heading}, until {until.isoformat().replace('+00:00', 'Z')})"
        return comment(config, repo, config.log.issue, f"{heading}\n\n{body}")
    if not current:
        print("no night orders open")
    return 0
