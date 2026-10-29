"""Check the references in a draft comment: `path:line` and `sha256` against the files they cite, and ticket
numbers, PR links, and comment links against GitHub."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from dkitchen import truncate

REF = re.compile(r"([\w./\-]+\.\w+):(\d+)(?:[-–](\d+))?")
SPAN = re.compile(r"`([^`\n]+)`")
NEAR = 12  # characters allowed between a reference and the name it points at
SHA = re.compile(r"sha256[\s:=`]*`?([0-9a-f]{8,64})(?:…([0-9a-f]{4,64}))?", re.I)
PATH = re.compile(r"^[\w./\-]+\.\w+$")
DEFINITION = re.compile(r"\s*(?:async\s+)?(?:def|class)\s+(\w+)")
IDENTIFIER = re.compile(r"^[A-Za-z_][\w.]{3,}$")
URL = re.compile(r"https?://\S+")
LINK = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/(issues|pull)/(\d+)(?:#([\w-]+))?")
TICKET = re.compile(r"(?<![A-Za-z0-9_/&.-])(?:([\w.-]+/[\w.-]+)#|(?i:(PR)\s*)?#)(\d+)\b")
KINDS = ("file:line", "sha256", "ticket", "PR", "PR link", "ticket link", "comment link")


def github_api(path: str) -> dict | None:
    """A GitHub API record, or None when it does not exist; raises when GitHub cannot be asked."""
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
    if result.returncode:
        if "Not Found" in result.stderr or "HTTP 404" in result.stderr or "HTTP 410" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip() or f"gh api {path} failed")
    return json.loads(result.stdout)


class GitHub:
    """Looks up tickets, PRs, and comments once each; `api` is github_api unless a test passes another."""

    def __init__(self, repo: str | None, api=github_api, default=None) -> None:
        self.api, self.cache, self._repo, self.default = api, {}, repo, default

    @property
    def repo(self) -> str | None:
        """The repository bare #N means, looked up the first time a draft has one."""
        if self._repo is None and self.default is not None:
            self._repo, self.default = self.default(), None
        return self._repo

    def get(self, path: str) -> dict | None:
        if path not in self.cache:
            self.cache[path] = self.api(path)
        return self.cache[path]

    def problem(self, kind: str, repo: str, number: int, anchor: str | None = None) -> str | None:
        """Why the reference is wrong, or None when it is right."""
        issue = self.get(f"repos/{repo}/issues/{number}")
        if issue is None:
            return f"{repo}#{number} does not exist"
        is_pr = "pull_request" in issue
        if kind in {"PR", "PR link"} and not is_pr:
            return f"{repo}#{number} is an issue, not a PR"
        if kind == "ticket link" and is_pr:
            return f"{repo}#{number} is a PR; its link is /pull/{number}"
        if anchor is None:
            return None
        found = re.fullmatch(r"(issuecomment|discussion_r|pullrequestreview)-?(\d+)", anchor)
        if found is None:
            raise LookupError(f"link anchor #{anchor} is not a comment")
        style, ident = found.groups()
        path = {"issuecomment": f"repos/{repo}/issues/comments/{ident}",
                "discussion_r": f"repos/{repo}/pulls/comments/{ident}",
                "pullrequestreview": f"repos/{repo}/pulls/{number}/reviews/{ident}"}[style]
        comment = self.get(path)
        if comment is None:
            return f"comment {anchor} does not exist"
        owner = comment.get("issue_url") or comment.get("pull_request_url") or ""
        if style != "pullrequestreview" and not owner.endswith(f"/{number}"):
            return f"comment {anchor} belongs to #{owner.rsplit('/', 1)[-1]}, not #{number}"
        return None


class Source:
    """Files as saved in a git revision, or as they sit under a directory."""

    def __init__(self, ref: str, root: Path | None) -> None:
        self.ref, self.root = ref, root
        command = ["git", "-C", str(root), "ls-files"] if root else ["git", "ls-tree", "-r", "--name-only", ref]
        listing = subprocess.run(command, capture_output=True, text=True)
        self.names = listing.stdout.split("\n") if listing.returncode == 0 else []

    def resolve(self, name: str) -> str | None:
        """The one tracked path that is `name` or ends with `/name`; None when absent or ambiguous."""
        if self.root and (self.root / name).is_file() or name in self.names:
            return name
        matches = [n for n in self.names if n.endswith("/" + name)]
        return matches[0] if len(matches) == 1 else None

    def read(self, path: str) -> bytes:
        if self.root:
            return (self.root / path).read_bytes()
        return subprocess.run(["git", "show", f"{self.ref}:{path}"], capture_output=True, check=True).stdout


def names_by_reference(line: str, spans: list[re.Match[str]]) -> dict[int, list[str]]:
    """Identifiers keyed by the index of the reference span they belong to: the reference just before the
    name ("`f.py:9` holds `name`"), else the one just after it, with at most NEAR characters between."""
    def is_reference(index: int) -> bool:
        return 0 <= index < len(spans) and bool(REF.fullmatch(spans[index].group(1)))

    names: dict[int, list[str]] = {}
    for index, span in enumerate(spans):
        if not IDENTIFIER.match(span.group(1)) or PATH.match(span.group(1)):
            continue
        before = is_reference(index - 1) and len(line[spans[index - 1].end():span.start()]) <= NEAR
        after = is_reference(index + 1) and len(line[span.end():spans[index + 1].start()]) <= NEAR
        if before or after:
            names.setdefault(index - 1 if before else index + 1, []).append(span.group(1))
    return names


def enclosing_definition(lines: list[str], start: int) -> str | None:
    """Name of the nearest `def` or `class` at or above the 1-based line `start`."""
    for text in reversed(lines[:start]):
        match = DEFINITION.match(text)
        if match:
            return match.group(1)
    return None


def bare_references(line: str, spans: list[re.Match[str]]) -> list[re.Match[str]]:
    """`path:line` references written outside backticks and outside links."""
    covered = [(m.start(), m.end()) for m in [*spans, *URL.finditer(line)]]
    return [m for m in REF.finditer(line)
            if not any(start <= m.start() < end for start, end in covered)
            and (m.start() == 0 or not re.match(r"[\w./\-]", line[m.start() - 1]))]


def check_github(number: int, line: str, github: GitHub | None, counts: dict[str, int],
                 findings: list[str], notes: list[str]) -> int:
    """Check the line's ticket numbers, PR numbers, and GitHub links; return how many were checked."""
    references = []
    for match in LINK.finditer(line):
        repo, place, ticket, anchor = match.groups()
        kind = "comment link" if anchor else ("PR link" if place == "pull" else "ticket link")
        references.append((kind, repo, int(ticket), anchor, match.group(0)))
    masked = URL.sub(lambda m: " " * len(m.group(0)), line)
    for match in TICKET.finditer(masked):
        repo, pr, ticket = match.groups()
        references.append(("PR" if pr else "ticket", repo, int(ticket), None, match.group(0)))
    checked = 0
    for kind, repo, ticket, anchor, text in references:
        repo = repo or (github.repo if github else None)
        if github is None or repo is None:
            reason = "GitHub checks are off (--offline)" if github is None else "cannot tell the repository"
            notes.append(f"line {number} {kind} {text}: not checked ({reason})")
            continue
        try:
            problem = github.problem(kind, repo, ticket, anchor)
        except (LookupError, RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
            notes.append(f"line {number} {kind} {text}: not checked ({truncate(' '.join(str(error).split()), 120)})")
            continue
        checked += 1
        counts[kind] = counts.get(kind, 0) + 1
        if problem:
            findings.append(f"line {number} {kind} {text}: {problem}")
    return checked


def check(draft: str, source: Source, tolerance: int, github: GitHub | None = None,
          counts: dict[str, int] | None = None) -> tuple[list[str], list[str], int]:
    """Findings (wrong), notes (could not be checked), and how many references were checked; counts, when
    given, collects the checked references by kind. Without github, ticket and link references are not read."""
    findings, notes, checked = [], [], 0
    counts = {} if counts is None else counts
    for number, line in enumerate(draft.split("\n"), 1):
        spans = list(SPAN.finditer(line))
        names = names_by_reference(line, spans)
        located = [(index, REF.fullmatch(span.group(1))) for index, span in enumerate(spans)]
        located += [(None, match) for match in bare_references(line, spans)]
        for index, match in located:
            if not match:
                continue
            name, first, last = match.groups()
            path = source.resolve(name)
            if path is None:
                notes.append(f"line {number} `{name}:{first}`: no such file, or several files share the name; not checked")
                continue
            checked += 1
            counts["file:line"] = counts.get("file:line", 0) + 1
            lines = source.read(path).decode("utf-8", "replace").splitlines()
            start, end = int(first), int(last or first)
            if not 1 <= start <= end <= len(lines):
                findings.append(f"line {number} `{name}:{first}`: beyond the end of the file ({len(lines)} lines)")
                continue
            window = "\n".join(lines[max(start - 1 - tolerance, 0):end + tolerance])
            findings.extend(f"line {number} `{name}:{first}`: `{word}` is not on the cited line, which reads "
                            f"{truncate(lines[start - 1].strip(), 80)!r}"
                            for word in names.get(index, [])
                            if word not in window and word != enclosing_definition(lines, start))
        for match in SHA.finditer(line):
            cited = [p for p in (m.group(1) for m in spans if m.end() <= match.start()) if PATH.match(p)]
            path = source.resolve(cited[-1]) if cited else None
            if path is None:
                notes.append(f"line {number} sha256 `{match.group(1)}`: no file path found before it; not checked "
                             "(add --root for files outside git)")
                continue
            checked += 1
            counts["sha256"] = counts.get("sha256", 0) + 1
            digest = hashlib.sha256(source.read(path)).hexdigest()
            prefix, suffix = match.group(1).lower(), (match.group(2) or "").lower()
            if not digest.startswith(prefix) or not digest.endswith(suffix):
                findings.append(f"line {number} `{path}` has sha256 {digest[:8]}…{digest[-4:]}, the draft says {match.group(0)}")
        checked += check_github(number, line, github, counts, findings, notes)
    return findings, notes, checked


def default_repository() -> str | None:
    try:
        from dkitchen import config
        return config.load().repository()
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def report(path: str, args, source: Source, github: GitHub | None, many: bool, emit_json: bool = True) -> int:
    """Check one draft and report it on stderr (and as a JSON line on stdout); 1 when something is wrong, 2 when
    the draft cannot be read. With several drafts every line names its file."""
    label = f"{path}: " if many else ""
    counts: dict[str, int] = {}
    try:
        findings, notes, checked = check(Path(path).read_text(encoding="utf-8"), source, args.tolerance, github, counts)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"ding: {error}", file=sys.stderr)
        return 2
    for line in findings:
        print(f"wrong: {label}{line}", file=sys.stderr)
    for line in notes:
        print(f"not checked: {label}{line}", file=sys.stderr)
    kinds = ", ".join(f"{counts[kind]} {kind}" for kind in KINDS if counts.get(kind)) or "none"
    print(f"{label}checked {checked} references ({kinds}): {len(findings)} wrong, {len(notes)} not checked", file=sys.stderr)
    if emit_json:
        print(json.dumps({**({"draft": path} if many else {}), "checked": checked, "findings": len(findings),
                          "unchecked": len(notes), "by_kind": counts, "wrong": findings, "not_checked": notes},
                         ensure_ascii=False))
    return int(bool(findings))


def check_refs(args, emit_json: bool = True) -> int:
    """Report each draft in turn; 0 when none has a wrong reference, 1 when one does, 2 when one cannot be read."""
    drafts = [args.draft] if isinstance(args.draft, (str, Path)) else list(args.draft)
    github = None if getattr(args, "offline", False) else GitHub(getattr(args, "repo", None), default=default_repository)
    try:
        source = Source(args.ref, args.root)
    except OSError as error:
        print(f"ding: {error}", file=sys.stderr)
        return 2
    return max(report(path, args, source, github, len(drafts) > 1, emit_json) for path in drafts)
