"""Post the designer's ruling on a ticket: check the draft, guard against overwriting, post, and notify.

Steps, each stopping the command before anything is sent when it fails:
- the draft opens with the designer's signature and a type line (`[rule]` in .dkitchen.toml);
- `ding check-refs` finds nothing wrong in it;
- with --reply-to, no comment came after the one answered (--force posts anyway); without it, the latest
  comment is shown as a hint;
- with --edit-body, the ticket body still equals the baseline file (--base).
Then it posts with gh and prints only the URL gh returns. A failed notification is reported but never undoes
the comment.
"""
from __future__ import annotations

import contextlib
import difflib
import json
import re
import sys
from types import SimpleNamespace

from dkitchen import ding, refs
from dkitchen.config import Config
from dkitchen.orchestrate import call, fail

CLAUDE_ADDRESS = re.compile(r"^(?:uds|bridge|did):|^local_")
COMMENT_URL = re.compile(r"^https://\S+#issuecomment-\d+$")
COMMENT_ID = re.compile(r"(?:#issuecomment-)?(\d+)$")


def first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")[:100]


def format_problem(draft: str, config: Config) -> str | None:
    """Why the draft's first two lines are wrong, or None when they are right."""
    stations, rule = config.stations, config.rule
    lines = draft.split("\n")
    if not lines[0].startswith(config.designer_signatures):
        signatures = " or ".join(repr(signature) for signature in config.designer_signatures)
        return f"line 1 must start with {signatures}, not {lines[0][:40]!r}"
    label = f"{rule.type_label}{stations.separator}"
    if len(lines) < 2 or not lines[1].startswith(label):
        return f"line 2 must start with {label!r}, not {(lines[1] if len(lines) > 1 else '')[:40]!r}"
    kind = lines[1][len(label):].strip()
    if kind not in rule.types:
        return f"line 2 type {kind!r} must be one of: {', '.join(rule.types)}"
    return None


def comments_of(config: Config, repo: str, ticket: int) -> list[dict]:
    result = call(config, "gh", "api", "--paginate", "--slurp", f"repos/{repo}/issues/{ticket}/comments?per_page=100")
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "cannot read the ticket's comments")
    return [comment for page in json.loads(result.stdout) for comment in page]


def comment_id(reply_to: str, repo: str, ticket: int) -> int:
    """The id in a comment URL or a bare id; refuses a link to another ticket."""
    if reply_to.startswith("http"):
        link = refs.LINK.fullmatch(reply_to.strip())
        if link is None or not link.group(4) or not link.group(4).startswith("issuecomment-"):
            raise ValueError(f"--reply-to {reply_to!r} is not an issue comment link")
        if (link.group(1), int(link.group(3))) != (repo, ticket):
            raise ValueError(f"--reply-to {reply_to!r} is not on {repo}#{ticket}")
    found = COMMENT_ID.search(reply_to.strip())
    if found is None:
        raise ValueError(f"--reply-to {reply_to!r} is neither a comment link nor a comment id")
    return int(found.group(1))


def guard_reply(config: Config, repo: str, ticket: int, args) -> int | None:
    """Stop (1) when a comment came after the one answered; otherwise show what the ticket's last comment is."""
    comments = comments_of(config, repo, ticket)
    if args.reply_to is None:
        if comments:
            latest = comments[-1]
            print(f"latest comment on #{ticket}: {latest['created_at']} {first_line(latest['body'])!r} "
                  f"({latest['html_url']})", file=sys.stderr)
        else:
            print(f"#{ticket} has no comments yet", file=sys.stderr)
        return None
    target = comment_id(args.reply_to, repo, ticket)
    ids = [comment["id"] for comment in comments]
    if target not in ids:
        raise ValueError(f"comment {target} is not among the comments of {repo}#{ticket}")
    newer = comments[ids.index(target) + 1:]
    if not newer:
        return None
    for comment in newer:
        print(f"newer: {comment['html_url']} {first_line(comment['body'])!r}", file=sys.stderr)
    if getattr(args, "force", False):
        print(f"{len(newer)} newer comment(s) after the one answered; posting anyway (--force)", file=sys.stderr)
        return None
    return fail(f"{len(newer)} comment(s) came after the one you answer; read them, then rerun (--force posts anyway). "
                "Nothing posted")


def notify(config: Config, args, ticket: int, url: str, draft: str) -> int:
    message = config.rule.notify.format(ticket=ticket, url=url, first_line=first_line(draft))
    if "{first_line}" not in config.rule.notify:
        message += "\n" + first_line(draft)
    send = ["send", args.notify, "--from", config.stations.designer, "--message", message]
    try:
        with contextlib.redirect_stdout(sys.stderr):
            code = ding.main(send)
    except SystemExit as error:
        code = error.code or 1
    if code:
        return fail(f"posted {url}, but the notification to {args.notify} failed (ding send exit {code}); "
                    "the comment stays. Send the message by hand")
    return 0


def normal(text: str) -> str:
    return text.replace("\r\n", "\n").rstrip("\r\n")


def edit_body(config: Config, repo: str, ticket: int, args, draft: str) -> int:
    """Replace the ticket body when it still equals the --base file, then check what was written back."""
    path = f"repos/{repo}/issues/{ticket}"

    def current() -> str:
        result = call(config, "gh", "api", path, "--jq", ".body")
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"cannot read {repo}#{ticket}")
        return result.stdout[:-1] if result.stdout.endswith("\n") else result.stdout

    body = current()
    with open(args.base, encoding="utf-8") as handle:
        base = handle.read()
    if normal(body) != normal(base):
        changes = [line for line in difflib.unified_diff(normal(base).split("\n"), normal(body).split("\n"),
                                                         "--base", "ticket now", lineterm="", n=0)
                   if line[:1] in "+-" and line[:3] not in {"+++", "---"}][:6]
        print("\n".join(changes), file=sys.stderr)
        return fail(f"the body of {repo}#{ticket} changed since {args.base}; merge the change into the draft and "
                    "take a new baseline. Nothing edited")
    result = call(config, "gh", "issue", "edit", str(ticket), "--repo", repo, "--body-file", args.draft)
    if result.returncode:
        return fail(f"edit failed: {result.stderr.strip()}")
    url = result.stdout.strip()
    if current().rstrip("\n").encode("utf-8") != draft.rstrip("\n").encode("utf-8"):
        return fail(f"edited {url}, but the body read back differs from the draft; check the ticket before relying on it")
    print(url)
    return 0 if args.notify is None else notify(config, args, ticket, url, draft)


def rule(args, config: Config) -> int:
    ticket = args.ticket
    if args.notify and CLAUDE_ADDRESS.search(args.notify):
        return fail(f"--notify {args.notify}: ding cannot message a Claude Code session; use SendMessage for it. "
                    "Nothing posted")
    try:
        with open(args.draft, encoding="utf-8") as handle:
            draft = handle.read()
    except (OSError, UnicodeDecodeError) as error:
        return fail(str(error))
    if not draft.strip():
        return fail(f"{args.draft} is empty")
    if not args.edit_body:
        problem = format_problem(draft, config)
        if problem:
            return fail(f"{args.draft}: {problem}. Nothing posted")
    try:
        repo = args.repo or config.repository()
        args.repo = repo
        if refs.check_refs(_refs_args(args), emit_json=False):
            return fail("check-refs found wrong or unreadable references; fix the draft. Nothing posted")
        if args.edit_body:
            return edit_body(config, repo, ticket, args, draft)
        stop = guard_reply(config, repo, ticket, args)
        if stop is not None:
            return stop
        result = call(config, "gh", "issue", "comment", str(ticket), "--repo", repo, "--body-file", args.draft)
    except (RuntimeError, ValueError, OSError, KeyError, TypeError) as error:
        return fail(f"{error}. Nothing posted")
    url = result.stdout.strip()
    if result.returncode:
        return fail(f"comment failed: {result.stderr.strip()}")
    if not COMMENT_URL.match(url):
        return fail(f"gh did not print a comment link (got {url!r}); look at the ticket before posting again")
    print(url)
    return 0 if args.notify is None else notify(config, args, ticket, url, draft)


def _refs_args(args):
    """What `check_refs` reads from the options `rule` shares with `check-refs`."""
    return SimpleNamespace(draft=[args.draft], ref=args.ref, root=args.root, tolerance=args.tolerance,
                           repo=args.repo, offline=args.offline)
