#!/usr/bin/env python3
"""Trial-merge a PR in a temporary worktree, run the given tests, and report what the reviewer must look at.

Usage: scripts/review_pr.py PR [test files...]

Reports the reviewed HEAD, whether the PR is a draft, the test result, hard-coded absolute paths in added
lines, names the PR deletes that are still imported, and tickets the merge will close; with an artifact
registry, also frozen files the PR changes. Exits nonzero when anything failed.
"""
# dkitchen reference script. Adapt when installing:
# - TEST_COMMAND: how this repo runs a list of test files.
# - prepare(): what a fresh worktree needs before tests run (links to data, submodules, env files).
# - REGISTRY: the artifact registry for the artifacts module, or None.
# - PROMPTS: the one top-level folder new prompt files belong in, for the model-text module, or None.
# - ABSOLUTE_PREFIXES: absolute path prefixes that code in this repo must never hard-code.
# - The import and string-literal checks read Python; replace or drop them for other languages.
from __future__ import annotations

import argparse
import ast
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import tempfile

TEST_COMMAND = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider"]
REGISTRY: str | None = "docs/artifacts/registry.yaml"
PROMPTS: str | None = "prompts"
ABSOLUTE_PREFIXES = ("/data/", "/root/", "/home/", "/tmp/", "/opt/")

STRING = re.compile(r'''\#.*|(?i:[rubf]{0,2})(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''')
CLOSING = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+(?:#|https://github\.com/[^\s/]+/[^\s/]+/issues/)(\d+)")


def prepare(worktree: Path, root: Path) -> None:
    """Get a fresh worktree of this repo ready for its tests; the main checkout is `root`."""


def added_line_warnings(diff: str) -> list[str]:
    warnings = []
    path, line_number, new_file = "", 0, False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            new_file = False
        elif line.startswith("new file mode "):
            new_file = True
        elif line.startswith("+++ b/") or line.startswith('+++ "b/'):
            path = line[4:].split("\t", 1)[0]
            if path.startswith('"'):
                path = ast.literal_eval(path)
            path = path[2:]
            parts = PurePosixPath(path).parts
            if PROMPTS and new_file and PROMPTS in parts[:-1] and parts[0] != PROMPTS:
                warnings.append(f"{path}: new prompt file outside the top-level {PROMPTS}/")
        elif line.startswith("@@ "):
            line_number = int(re.search(r"\+(\d+)", line).group(1))
        elif line.startswith("+"):
            if path.endswith(".py"):
                for match in STRING.finditer(line[1:]):
                    literal = match.group()
                    if literal.startswith("#"):
                        break
                    value = re.sub(r'''(?i)^[rubf]*["']''', "", literal)
                    if value.startswith(ABSOLUTE_PREFIXES):
                        warnings.append(f"{path}:{line_number}: hard-coded absolute path {literal}")
            line_number += 1
        elif line.startswith(" "):
            line_number += 1
    return warnings


def frozen_dependencies(records: list[dict]) -> dict[str, list[str]]:
    dependencies: dict[str, list[str]] = {}
    for record in records:
        if record["status"] == "current":
            for path in record.get("depends_on", []):
                names = dependencies.setdefault(path, [])
                if record["name"] not in names:
                    names.append(record["name"])
    return dependencies


def module_name(path: str) -> str:
    parts = PurePosixPath(path).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def top_level_names(source: str) -> set[str]:
    """Names another module can import: definitions, assignments and re-exported imports."""
    names: set[str] = set()
    pending = list(ast.parse(source).body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, (ast.If, ast.Try)):
            pending.extend(child for child in ast.iter_child_nodes(node) if isinstance(child, ast.stmt))
    return names


def broken_imports(removed: dict[str, set[str] | None], sources: dict[str, str]) -> list[str]:
    """Imports in the merged tree of names the PR removed; a module mapped to None was deleted."""
    warnings = []
    for path, source in sorted(sources.items()):
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        name = module_name(path).split(".")
        package = name if PurePosixPath(path).name == "__init__.py" else name[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                targets = [(alias.name, None) for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = package[:len(package) - node.level + 1] if node.level else []
                module = ".".join([*base, *([node.module] if node.module else [])])
                targets = [(module, alias.name) for alias in node.names]
            else:
                continue
            for module, imported in targets:
                if module in removed and (removed[module] is None or imported in removed[module]):
                    warnings.append(f"{path}:{node.lineno}: imports the deleted {module}" + (f".{imported}" if imported else ""))
                elif imported and f"{module}.{imported}" in removed and removed[f"{module}.{imported}"] is None:
                    warnings.append(f"{path}:{node.lineno}: imports the deleted {module}.{imported}")
    return warnings


def unrun_tests_warning(returncode: int, output: str) -> str | None:
    last = output.rstrip().rsplit("\n", 1)[-1]
    if returncode == 5:
        return "no tests were collected: the given tests do not exist, or were filtered out"
    if returncode == 0 and "passed" not in last:
        return "none of the given tests actually ran (all skipped)"
    return None


def closing_references(*texts: str) -> list[str]:
    return sorted({f"#{number}" for text in texts for number in CLOSING.findall(text)}, key=lambda ref: int(ref[1:]))


def run(*args: str, cwd: Path) -> str:
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args)}\n{result.stdout}")
    return result.stdout


def registry_records(root: Path, base: str) -> list[dict]:
    """Registry entries on the base branch and in the checkout, so a PR cannot lift protections by editing it."""
    if not REGISTRY:
        return []
    import yaml

    records = []
    shown = subprocess.run(["git", "show", f"{base}:{REGISTRY}"], cwd=root, text=True, capture_output=True)
    try:
        if shown.returncode == 0:
            records += yaml.safe_load(shown.stdout) or []
        if (root / REGISTRY).is_file():
            records += yaml.safe_load((root / REGISTRY).read_text()) or []
    except yaml.YAMLError as error:
        raise ValueError(f"{REGISTRY}: {error}") from error
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pr", type=int)
    parser.add_argument("tests", nargs="*")
    args = parser.parse_args()
    summary = [f"PR #{args.pr}", "Tests: not run"]
    status = 0
    root = Path(__file__).resolve().parents[1]
    worktree = None

    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        run("git", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main", cwd=root)
        run("git", "fetch", "origin", f"pull/{args.pr}/head", cwd=root)
        head = run("git", "rev-parse", "FETCH_HEAD", cwd=root).strip()
        summary[0] += f" HEAD {head}"
        if run("gh", "pr", "view", str(args.pr), "--json", "isDraft", "--jq", ".isDraft", cwd=root).strip() == "true":
            summary.append("Draft: yes. Merging will fail; its author marks it ready with gh pr ready first")
        base = run("git", "rev-parse", "origin/main", cwd=root).strip()
        dependencies = frozen_dependencies(registry_records(root, base))
        pr_base = run("gh", "pr", "view", str(args.pr), "--json", "baseRefOid", "--jq", ".baseRefOid", cwd=root).strip()
        run("git", "fetch", "origin", pr_base, cwd=root)
        merge_base = run("git", "merge-base", pr_base, head, cwd=root).strip()
        diff = run("git", "-c", "core.quotePath=false", "diff", "--no-color", "--no-textconv", "--no-ext-diff",
                   "--no-renames", "--unified=0", merge_base, head, cwd=root)
        changed = run("git", "diff", "--name-only", "-z", "--no-renames", merge_base, head, cwd=root).split("\0")
        with tempfile.TemporaryDirectory(prefix="review-pr-") as temporary:
            worktree = Path(temporary) / "worktree"
            try:
                run("git", "worktree", "add", "--detach", str(worktree), base, cwd=root)
                prepare(worktree, root)
                result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "merge", "--no-edit", "--no-commit", head],
                                        cwd=worktree, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                if result.returncode:
                    conflicts = run("git", "diff", "--name-only", "--diff-filter=U", cwd=worktree).strip()
                    raise RuntimeError(f"merge failed\n{conflicts}\n{result.stdout}")
                summary.append("Trial merge: clean")
                if args.tests:
                    result = subprocess.run([*TEST_COMMAND, *args.tests], cwd=worktree,
                                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    summary[1] = f"Tests: exit code {result.returncode}\n{result.stdout.rstrip()}"
                    warning = unrun_tests_warning(result.returncode, result.stdout)
                    if warning:
                        summary[1] += f"\nTest warning: {warning}"
                    status = int(result.returncode != 0 or warning is not None)
                if REGISTRY:
                    frozen = [f"{path}: {', '.join(dependencies[path])}" for path in changed if path in dependencies]
                    summary.append("Frozen files changed:\n" + ("\n".join(frozen) or "none"))
                summary.append("Added-line warnings:\n" + ("\n".join(added_line_warnings(diff)) or "none"))
                removed = {}
                for path in changed:
                    if path.endswith(".py"):
                        old = subprocess.run(["git", "show", f"{merge_base}:{path}"], cwd=root, text=True, capture_output=True)
                        new = subprocess.run(["git", "show", f"{head}:{path}"], cwd=root, text=True, capture_output=True)
                        if old.returncode == 0:
                            removed[module_name(path)] = (None if new.returncode
                                                          else top_level_names(old.stdout) - top_level_names(new.stdout))
                tracked = run("git", "ls-files", "-z", "*.py", cwd=worktree).split("\0")
                sources = {path: (worktree / path).read_text() for path in tracked if path and (worktree / path).is_file()}
                summary.append("Deleted but still imported:\n" + ("\n".join(broken_imports(removed, sources)) or "none"))
                description = run("gh", "pr", "view", str(args.pr), "--json", "title,body", "--jq", '.title + "\\n" + .body', cwd=root)
                commits = run("git", "log", "--format=%B", f"{merge_base}..{head}", cwd=root)
                summary.append("Tickets the merge will close:\n" + (" ".join(closing_references(description, commits)) or "none"))
            finally:
                run("git", "worktree", "remove", "--force", str(worktree), cwd=root)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, ImportError, KeyboardInterrupt) as error:
        summary.append(f"Error: {error}")
        status = 1
    finally:
        try:
            if worktree is not None:
                run("git", "worktree", "prune", cwd=root)
        except (OSError, RuntimeError) as error:
            summary.append(f"Cleanup failed: {error}")
            status = 1
        signal.signal(signal.SIGTERM, previous)
        print("\n".join(summary))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
