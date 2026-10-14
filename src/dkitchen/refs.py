"""Check the `path:line` and `sha256` references in a draft comment against the files they cite."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

REF = re.compile(r"([\w./\-]+\.\w+):(\d+)(?:[-–](\d+))?")
SPAN = re.compile(r"`([^`\n]+)`")
NEAR = 12  # characters allowed between a reference and the name it points at
SHA = re.compile(r"sha256[\s:=`]*`?([0-9a-f]{8,64})(?:…([0-9a-f]{4,64}))?", re.I)
PATH = re.compile(r"^[\w./\-]+\.\w+$")
DEFINITION = re.compile(r"\s*(?:async\s+)?(?:def|class)\s+(\w+)")
IDENTIFIER = re.compile(r"^[A-Za-z_][\w.]{3,}$")


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


def check(draft: str, source: Source, tolerance: int) -> tuple[list[str], list[str], int]:
    """Findings (wrong), notes (could not be checked), and how many references were checked."""
    findings, notes, checked = [], [], 0
    for number, line in enumerate(draft.split("\n"), 1):
        spans = list(SPAN.finditer(line))
        names = names_by_reference(line, spans)
        for index, span in enumerate(spans):
            match = REF.fullmatch(span.group(1))
            if not match:
                continue
            name, first, last = match.groups()
            path = source.resolve(name)
            if path is None:
                notes.append(f"line {number} `{name}:{first}`: no such file, or several files share the name; not checked")
                continue
            checked += 1
            lines = source.read(path).decode("utf-8", "replace").splitlines()
            start, end = int(first), int(last or first)
            if not 1 <= start <= end <= len(lines):
                findings.append(f"line {number} `{name}:{first}`: beyond the end of the file ({len(lines)} lines)")
                continue
            window = "\n".join(lines[max(start - 1 - tolerance, 0):end + tolerance])
            findings.extend(f"line {number} `{name}:{first}`: `{word}` is not on the cited line, which reads "
                            f"{lines[start - 1].strip()[:80]!r}"
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
            digest = hashlib.sha256(source.read(path)).hexdigest()
            prefix, suffix = match.group(1).lower(), (match.group(2) or "").lower()
            if not digest.startswith(prefix) or not digest.endswith(suffix):
                findings.append(f"line {number} `{path}` has sha256 {digest[:8]}…{digest[-4:]}, the draft says {match.group(0)}")
    return findings, notes, checked


def check_refs(args) -> int:
    try:
        findings, notes, checked = check(Path(args.draft).read_text(encoding="utf-8"), Source(args.ref, args.root),
                                         args.tolerance)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"ding: {error}", file=sys.stderr)
        return 2
    for line in [*notes, *findings]:
        print(line, file=sys.stderr)
    print(f"checked {checked} references: {len(findings)} wrong, {len(notes)} not checked", file=sys.stderr)
    print(json.dumps({"checked": checked, "findings": len(findings), "unchecked": len(notes)}))
    return int(bool(findings))
