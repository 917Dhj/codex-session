#!/usr/bin/env python3
"""Check that every paragraph of a prompt file appears verbatim on a ticket, in its body or a comment.

Usage: scripts/check_prompt_in_issue.py PROMPT_FILE TICKET

Trailing whitespace is ignored; everything else must match. Exits 1 when a paragraph is missing.
"""
# dkitchen reference script for the model-text module; it usually needs no changes.
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


def normalize(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.split("\n"))


def paragraph_matches(prompt: str, texts: list[str]) -> list[tuple[str, int | None]]:
    """Each paragraph of the prompt, with the index of the first text holding it, or None."""
    paragraphs = [part for part in re.split(r"\n\n+", normalize(prompt).strip("\n")) if part]
    sources = [normalize(text) for text in texts]
    return [(part, next((i for i, text in enumerate(sources) if part in text), None)) for part in paragraphs]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prompt", type=Path)
    parser.add_argument("ticket", type=int)
    args = parser.parse_args()
    try:
        result = subprocess.run(["gh", "issue", "view", str(args.ticket), "--json", "body,comments"],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=True)
        issue = json.loads(result.stdout)
        matches = paragraph_matches(args.prompt.read_text(), [issue["body"], *(c["body"] for c in issue["comments"])])
        if not matches:
            raise ValueError("the prompt file has no paragraphs")
        for number, (_, location) in enumerate(matches, 1):
            label = "missing" if location is None else ("found (body)" if location == 0 else f"found (comment {location})")
            print(f"paragraph {number}: {label}")
        return int(any(location is None for _, location in matches))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"error: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
