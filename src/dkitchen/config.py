"""Per-repo kitchen vocabulary, read from .dkitchen.toml at the repository root.

Every key is optional. The defaults match the English templates in kitchen/:

    repo = "owner/name"                  # default: the repository gh sees here

    [stations]
    orchestrator = "Orchestrator"
    designer = "Designer"
    verifier = "Verifier"
    executor = "Executor"
    user = "User"
    separator = ": "                     # written after a station name to sign a comment, e.g. "：" in Chinese

    [status]
    fields = ["Status", "Waiting on", "Key numbers", "Next"]   # first: the state; second: who it waits on
    in_progress = "in progress"
    waiting_on_design = "waiting on design"
    blocked = "blocked"
    done = "done"
    none = "none"                        # a Waiting on value naming nobody
    pending_merge = "Pending merge"      # optional field listing PRs ready to merge, e.g. "PR#12, PR#14"

    [labels]
    prefix = "status:"
    awaiting_user = "status:awaiting-user"

    [log]
    issue = 1                            # the pinned orchestrator log ticket
    pending = "Waiting for the user"

    [sessions]
    name = "【#{issue}】{name}"           # `ding new --issue N --name NAME`; also {repo}
    parent = "Parent: #{parent}"         # first line of a sub-ticket opened by `ding new --parent N`

    [relay]
    superseding = "New decision; it replaces earlier relays not yet carried out."   # first line of a default relay

    [rule]                               # `ding rule`: the designer's reply opens with the designer's signature,
    # signatures = ["Designer: ", "Training designer: "]  # default: designer + separator
    type_label = "Type"                  # then a line `<type_label><separator><one of types>`
    types = ["actionable", "proposal, awaiting user", "record"]
    notify = "#{ticket} answered, see the ticket {url}"   # message `--notify` sends; also {first_line}

    [watch]
    idle_minutes = 45                    # 0 disables idle-executor wakes
"""
from __future__ import annotations

from dataclasses import dataclass, fields as dataclass_fields, replace
from pathlib import Path
import math
import re
import string
import subprocess
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from dkitchen import ding

FILE = ".dkitchen.toml"


@dataclass(frozen=True)
class Stations:
    orchestrator: str = "Orchestrator"
    designer: str = "Designer"
    verifier: str = "Verifier"
    executor: str = "Executor"
    user: str = "User"
    separator: str = ": "

    def signature(self, name: str) -> str:
        return f"{name}{self.separator}"


@dataclass(frozen=True)
class Status:
    fields: tuple[str, ...] = ding.status_fields(ding.STATUS_FIELDS)
    in_progress: str = "in progress"
    waiting_on_design: str = "waiting on design"
    blocked: str = "blocked"
    done: str = "done"
    none: str = "none"
    pending_merge: str = "Pending merge"


@dataclass(frozen=True)
class Labels:
    prefix: str = "status:"
    awaiting_user: str = "status:awaiting-user"


@dataclass(frozen=True)
class Log:
    issue: int | None = None
    pending: str = "Waiting for the user"


@dataclass(frozen=True)
class Sessions:
    name: str = "【#{issue}】{name}"
    parent: str = "Parent: #{parent}"

    def session_name(self, *, issue: int, name: str, repo: str) -> str:
        return self.name.format(issue=issue, name=name, repo=repo)

    def issue_number(self, name: str, repo: str = "") -> int | None:
        """The ticket encoded by the template, or None when it cannot be recovered reliably."""
        pattern, captured, previous = "", set(), None
        for literal, field, spec, _ in string.Formatter().parse(self.name):
            pattern += re.escape(literal)
            if field is None:
                continue
            if not literal and {previous, field} == {"issue", "name"}:
                return None
            if field == "repo":
                pattern += re.escape(format(repo, spec))
            elif field in captured:
                pattern += f"(?P={field})"
            else:
                pattern += f"(?P<{field}>{' *[0-9]+ *' if field == 'issue' else '.*?'})"
                captured.add(field)
            previous = field
        match = re.fullmatch(pattern, name, flags=re.DOTALL)
        if match is None or "issue" not in captured:
            return None
        number = int(match["issue"])
        if number <= 0 or self.session_name(issue=number, name=match.groupdict().get("name", ""), repo=repo) != name:
            return None
        return number


@dataclass(frozen=True)
class Relay:
    superseding: str = "New decision; it replaces earlier relays not yet carried out."


@dataclass(frozen=True)
class Rule:
    type_label: str = "Type"
    types: tuple[str, ...] = ("actionable", "proposal, awaiting user", "record")
    notify: str = "#{ticket} answered, see the ticket {url}"
    signatures: tuple[str, ...] = ()


@dataclass(frozen=True)
class Launch:
    preflight: tuple[str, ...] = ()
    preflight_timeout: float = 300


@dataclass(frozen=True)
class Watch:
    idle_minutes: float = 45


SESSION_FIELDS = ("issue", "name", "repo")


def session_template(value: object) -> str:
    """A session name template using only SESSION_FIELDS, checked when the config is read."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{FILE}: [sessions] name must be a nonempty string")
    allowed = ", ".join(f"{{{field}}}" for field in SESSION_FIELDS)
    try:
        for _, field, spec, conversion in string.Formatter().parse(value):
            if field is None:
                continue
            if field not in SESSION_FIELDS:
                raise ValueError(f"{FILE}: [sessions] name uses {{{field}}}; use only {allowed}")
            if conversion or "{" in spec:
                raise ValueError(f"{FILE}: [sessions] name: write {{{field}}} without a conversion or nested field")
        Sessions(value).session_name(issue=1, name="name", repo="owner/name")
    except ValueError as exc:
        if str(exc).startswith(FILE):
            raise
        raise ValueError(f"{FILE}: [sessions] name is not a valid template: {exc}") from exc
    return value


@dataclass(frozen=True)
class Config:
    root: Path
    repo: str | None = None
    stations: Stations = Stations()
    status: Status = Status()
    labels: Labels = Labels()
    log: Log = Log()
    sessions: Sessions = Sessions()
    relay: Relay = Relay()
    rule: Rule = Rule()
    launch: Launch = Launch()
    watch: Watch = Watch()

    @property
    def designer_signatures(self) -> tuple[str, ...]:
        return self.rule.signatures or (self.stations.signature(self.stations.designer),)

    def repository(self) -> str:
        """The configured repository, or the one gh sees from the repository root."""
        if self.repo:
            return self.repo
        result = subprocess.run(["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                                cwd=self.root, capture_output=True, text=True, timeout=60)
        repo = result.stdout.strip()
        if result.returncode or not repo:
            raise ValueError(f"cannot tell the GitHub repository; set repo in {FILE}: {result.stderr.strip()}")
        return repo


def section(cls, values: object, name: str):
    if not isinstance(values, dict):
        raise ValueError(f"{FILE}: [{name}] must be a table")
    known = {field.name: field for field in dataclass_fields(cls)}
    unknown = sorted(set(values) - set(known))
    if unknown:
        raise ValueError(f"{FILE}: unknown keys in [{name}]: {', '.join(unknown)}")
    result = cls()
    for key, value in values.items():
        default = getattr(result, key)
        if key in {"fields", "types", "signatures"}:
            if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
                kind = {"fields": "field names", "types": "words", "signatures": "signatures"}[key]
                raise ValueError(f"{FILE}: [{name}] {key} must be a nonempty list of {kind}")
            value = tuple(value) if key == "signatures" else tuple(item.strip() for item in value)
        elif key == "preflight":
            if (not isinstance(value, list) or not value
                    or not all(isinstance(item, str) and "\0" not in item for item in value)
                    or not value[0].strip()):
                raise ValueError(f"{FILE}: [launch] preflight must be a nonempty argv list with a nonempty executable")
            value = tuple(value)
        elif key == "preflight_timeout":
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{FILE}: [launch] preflight_timeout must be a positive finite number of seconds")
        elif key == "idle_minutes":
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError(f"{FILE}: [watch] idle_minutes must be a nonnegative finite number of minutes")
        elif key == "notify":
            if not isinstance(value, str) or "{url}" not in value:
                raise ValueError(f"{FILE}: [rule] notify must be a string holding {{url}}")
            try:
                value.format(ticket=1, url="u", first_line="l")
            except (ValueError, KeyError, IndexError) as exc:
                raise ValueError(f"{FILE}: [rule] notify is not a valid template: {exc}") from exc
        elif key == "name" and cls is Sessions:
            value = session_template(value)
        elif key == "parent" and cls is Sessions:
            if not isinstance(value, str) or "{parent}" not in value:
                raise ValueError(f"{FILE}: [sessions] parent must be a string holding {{parent}}")
            try:
                value.format(parent=1)
            except (ValueError, KeyError, IndexError) as exc:
                raise ValueError(f"{FILE}: [sessions] parent is not a valid template: {exc}") from exc
        elif key == "issue":
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{FILE}: [{name}] issue must be a positive issue number")
        elif not isinstance(value, str) or (not value.strip() and key != "separator"):
            raise ValueError(f"{FILE}: [{name}] {key} must be a nonempty string")
        result = replace(result, **{key: value})
    return result


def repository_root(cwd: Path | None = None) -> Path:
    """The Git work tree holding cwd, or cwd itself outside Git."""
    cwd = cwd or Path.cwd()
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True)
    return Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else cwd


def load(cwd: Path | None = None) -> Config:
    root = repository_root(cwd)
    path = root / FILE
    if not path.exists():
        return Config(root=root)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    sections = {"stations": Stations, "status": Status, "labels": Labels, "log": Log, "sessions": Sessions, "relay": Relay,
                "rule": Rule, "launch": Launch, "watch": Watch}
    unknown = sorted(set(data) - {"repo", *sections})
    if unknown:
        raise ValueError(f"{FILE}: unknown keys: {', '.join(unknown)}")
    repo = data.get("repo")
    if repo is not None and (not isinstance(repo, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo)):
        raise ValueError(f"{FILE}: repo must look like owner/name")
    return Config(root=root, repo=repo, **{name: section(cls, data[name], name)
                                            for name, cls in sections.items() if name in data})
