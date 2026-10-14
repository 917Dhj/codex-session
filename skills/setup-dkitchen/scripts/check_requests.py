#!/usr/bin/env python3
"""Check rendered model requests before they go to the designer for review.

Usage: scripts/check_requests.py REQUEST.json... [--prompt FILE]... [--prompt-any FILE...]... [--forbid-file FILE]

Reports rendering slips (stray whitespace inside quotes, trailing whitespace, tabs or carriage returns,
empty quotes, unreplaced {placeholders}, runs of blank lines), prompt text a request is missing, phrases
no request may contain, and answer-like fields left in a request. The last line is the summary to paste
into the ticket.
"""
# dkitchen reference script for the model-text module. Adapt when installing:
# - payload() and visible_texts(): how this repo saves requests and what the model reads in them.
#   The defaults read OpenAI-style chat requests: "messages" with string or text-block content, plus "tools".
# - ANSWER_KEYS: field names that would leak answers or labels to the model.
# - LINE_RULES: rendering slips that matter for this repo's text.
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any

ANSWER_KEYS = {"answer", "answers", "ground_truth", "gold", "label", "labels", "correct", "reference_answer", "rubric"}
PLACEHOLDER = re.compile(r"\{[A-Za-z_]+\}")
LINE_RULES = (("whitespace inside quotes", re.compile(r'(?:^|[\]:] )"[ \t]|[ \t]"$')),
              ("trailing whitespace", re.compile(r"[ \t]$")),
              ("tab or carriage return", re.compile(r"[\t\r]")),
              ("empty quotes", re.compile(r'(?:^|[\]:] )""$')),
              ("unreplaced placeholder", PLACEHOLDER))


def payload(saved: Any) -> Any:
    """The object sent to the model: the file itself, or its "request" field when the file wraps it."""
    return saved["request"] if isinstance(saved, dict) and "messages" not in saved else saved


def visible_texts(request: Any) -> list[str]:
    """Every text the model reads: message strings, text blocks, and tool definitions."""
    messages = request["messages"] if isinstance(request, dict) else [{"content": request}]
    texts = []
    for message in messages:
        content = message["content"]
        texts.extend([content] if isinstance(content, str) else [b["text"] for b in content if b.get("type") == "text"])
    if isinstance(request, dict):
        texts.extend(json.dumps(tool, ensure_ascii=False) for tool in request.get("tools", []))
    return texts


def template_pattern(template: str) -> re.Pattern[str]:
    parts = PLACEHOLDER.split(template.rstrip("\n"))
    return re.compile(".*?".join(re.escape(part) for part in parts), re.S)


def text_findings(text: str) -> list[tuple[str, str]]:
    findings = [(name, line) for line in text.split("\n") for name, rule in LINE_RULES if rule.search(line)]
    if "\n\n\n" in text:
        findings.append(("two or more blank lines in a row", ""))
    return findings


def answer_keys(value: Any, path: str = "") -> list[str]:
    if isinstance(value, dict):
        return [found for key, item in value.items() for found in
                ([f"{path}/{key}"] if key.lower() in ANSWER_KEYS else []) + answer_keys(item, f"{path}/{key}")]
    if isinstance(value, list):
        return [found for n, item in enumerate(value) for found in answer_keys(item, f"{path}/{n}")]
    return []


def check(request: Any, required: list[str], alternatives: list[list[str]], forbidden: list[str]) -> list[tuple[str, str]]:
    texts = visible_texts(request)

    def present(template: str) -> bool:
        return any(template_pattern(template).search(text) for text in texts)

    findings = [(name, line) for text in texts if not text.startswith('{"type": "function"')
                for name, line in text_findings(text)]
    findings.extend(("missing prompt text", template.split("\n")[0]) for template in required if not present(template))
    findings.extend(("missing prompt text (one of several)", group[0].split("\n")[0])
                    for group in alternatives if not any(map(present, group)))
    findings.extend(("text the model must not see", phrase) for phrase in forbidden if any(phrase in text for text in texts))
    findings.extend(("answer field in the request", key) for key in answer_keys(request))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("requests", type=Path, nargs="+",
                        help='request JSON with "messages" (bare or under "request"), or a JSON list of content blocks')
    parser.add_argument("--prompt", type=Path, action="append", default=[], help="prompt file whose text every request must contain")
    parser.add_argument("--prompt-any", type=Path, nargs="+", action="append", default=[],
                        help="prompt files of which every request must contain at least one")
    parser.add_argument("--forbid-file", type=Path, help="one phrase per line that no request may contain, such as reference answers")
    parser.add_argument("--examples", type=int, default=3, help="lines to print per finding type")
    args = parser.parse_args()
    try:
        required = [path.read_text() for path in args.prompt]
        alternatives = [[path.read_text() for path in group] for group in args.prompt_any]
        forbidden = [line for line in args.forbid_file.read_text().split("\n") if line.strip()] if args.forbid_file else []
        failed = 0
        for path in args.requests:
            findings = check(payload(json.loads(path.read_text())), required, alternatives, forbidden)
            failed += bool(findings)
            for name in dict.fromkeys(name for name, _ in findings):
                lines = [line for found, line in findings if found == name]
                print(f"{path}: {name} × {len(lines)}")
                for line in lines[:args.examples]:
                    print(f"    {line[:160]!r}")
        print(f"checked {len(args.requests)} requests, {failed} with findings")
        return int(failed > 0)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"error: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
