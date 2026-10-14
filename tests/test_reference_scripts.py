"""The setup skill's reference repo scripts, loaded from their files as an installed repo would run them."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/setup-dkitchen/scripts"


def load(name):
    spec = spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


review_pr, check_requests, check_prompt_in_issue = (load(name) for name in ("review_pr", "check_requests", "check_prompt_in_issue"))


@pytest.mark.parametrize("name", ["review_pr", "check_requests", "check_prompt_in_issue"])
def test_scripts_print_help(name):
    result = subprocess.run([sys.executable, str(SCRIPTS / f"{name}.py"), "--help"], capture_output=True, text=True)
    assert result.returncode == 0 and "usage:" in result.stdout


def test_added_lines_warn_only_for_paths_and_new_nested_prompts():
    diff = '''diff --git a/example.py b/example.py
--- a/example.py
+++ b/example.py
@@ -2,2 +2,6 @@
-old = "/data/removed"
+path = Path(r"/data/new")
+home = f'/home/{user}'
+# ignored = "/root/comment"
+url = "https://example.test/tmp/x"
+text = 'The path is "/opt/embedded"'
 kept = "/tmp/unchanged"
'''
    diff += '''diff --git a/pkg/prompts/new.md b/pkg/prompts/new.md
new file mode 100644
--- /dev/null
+++ b/pkg/prompts/new.md
@@ -0,0 +1 @@
+hello
'''
    diff += '''diff --git a/prompts/ok.md b/prompts/ok.md
new file mode 100644
--- /dev/null
+++ b/prompts/ok.md
@@ -0,0 +1 @@
+hello
'''
    warnings = review_pr.added_line_warnings(diff)
    assert [warning.split(":")[0] for warning in warnings] == ["example.py", "example.py", "pkg/prompts/new.md"]
    assert warnings[0].startswith("example.py:2: hard-coded absolute path")


def test_added_path_with_spaces_and_patch_tab():
    diff = 'diff --git a/has space.py b/has space.py\n+++ b/has space.py\t\n@@ -0,0 +1 @@\n+path = "/tmp/new"\n'
    assert review_pr.added_line_warnings(diff) == ['has space.py:1: hard-coded absolute path "/tmp/new"']


def test_current_frozen_files_keep_all_artifact_names():
    records = [{"name": "first", "status": "current", "depends_on": ["shared.py"]},
               {"name": "second", "status": "current", "depends_on": ["shared.py", "only.py"]},
               {"name": "old", "status": "superseded", "depends_on": ["old.py"]}]
    assert review_pr.frozen_dependencies(records) == {"shared.py": ["first", "second"], "only.py": ["second"]}


def test_removed_names_still_imported_are_reported():
    before = "DATA = 1\nKEPT = 2\ndef gone(): pass\n"
    removed = {"pkg.paths": review_pr.top_level_names(before) - review_pr.top_level_names("KEPT = 2\n"), "pkg.dead": None}
    sources = {"pkg/user.py": "from .paths import DATA, KEPT\nfrom pkg import dead\n",
               "docs/report.py": "from pkg.paths import gone\nimport pkg.dead\nfrom pkg.paths import KEPT\n"}
    assert review_pr.broken_imports(removed, sources) == [
        "docs/report.py:1: imports the deleted pkg.paths.gone", "docs/report.py:2: imports the deleted pkg.dead",
        "pkg/user.py:1: imports the deleted pkg.paths.DATA", "pkg/user.py:2: imports the deleted pkg.dead"]


def test_unrun_tests_and_closing_keywords_are_reported():
    assert review_pr.unrun_tests_warning(5, "no tests ran in 0.01s") is not None
    assert review_pr.unrun_tests_warning(0, "3 skipped in 0.02s") is not None
    assert review_pr.unrun_tests_warning(0, "2 passed, 1 skipped in 0.02s") is None
    assert review_pr.unrun_tests_warning(1, "1 failed in 0.02s") is None
    body = "Report for the C step.\n\nCloses #165 and fixes: #7; see #99.\nResolved https://github.com/o/r/issues/12"
    assert review_pr.closing_references(body, "refs #3") == ["#7", "#12", "#165"]


def test_request_check_reports_rendering_slips_and_missing_prompt():
    request = {"messages": [{"role": "system", "content": "Answer the question."},
                            {"role": "user", "content": [{"type": "text", "text": '" Okay."\nWow.'},
                                                         {"type": "image_url", "image_url": {"url": "frame.jpg"}}]}]}
    names = [name for name, _ in check_requests.check(request, ["Answer the {thing}.", "Missing {x} text"], [], ["Wow"])]
    assert names == ["whitespace inside quotes", "missing prompt text", "text the model must not see"]
    clean = {"messages": [{"role": "user", "content": 'C1: "Okay."\n    voice: calm'}]}
    assert check_requests.check(clean, [], [], []) == []
    assert check_requests.check({**clean, "ground_truth": "A"}, [], [], []) == [("answer field in the request", "/ground_truth")]


def test_paragraphs_preserve_leading_space_and_match_one_source():
    prompt = "First line  \n  indented\t\n \t\nSecond\nline\n\nMissing\n"
    result = check_prompt_in_issue.paragraph_matches(prompt, ["First line\n  indented\nSecond", "line", "Second  \nline\t"])
    assert result == [("First line\n  indented", 0), ("Second\nline", 2), ("Missing", None)]
    assert check_prompt_in_issue.paragraph_matches("  leading", ["leading"]) == [("  leading", None)]
    assert check_prompt_in_issue.paragraph_matches("", ["anything"]) == []


def test_render_keeps_chosen_modules_and_leaves_no_markers(tmp_path):
    spec = spec_from_file_location("render", SCRIPTS.parent / "render.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    text = ("Lead.\n\n<!-- module:artifacts -->\n- frozen\n<!-- /module:artifacts -->\n- always\n"
            "Stop when<!-- module:model-text --> text changes,<!-- /module:model-text --> or scope changes.\n")
    assert module.render(text, {"artifacts"}) == "Lead.\n\n- frozen\n- always\nStop when or scope changes.\n"
    assert module.render(text, {"model-text"}) == "Lead.\n\n- always\nStop when text changes, or scope changes.\n"
    with pytest.raises(ValueError, match="unknown module"):
        module.render("<!-- module:nope -->x<!-- /module:nope -->", set())
    with pytest.raises(ValueError, match="unbalanced"):
        module.render("<!-- module:artifacts -->x", {"artifacts"})


@pytest.mark.parametrize("modules", ["", "run-budgets,model-text,artifacts,unattended", "unattended"])
def test_rendered_kitchen_has_no_markers_and_no_broken_links(tmp_path, modules):
    import re
    out = tmp_path / "docs/agents"
    result = subprocess.run([sys.executable, str(SCRIPTS.parent / "render.py"), "--modules", modules, "--out", str(out)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "## AGENTS.md" in result.stdout and "## CLAUDE.md" in result.stdout and "module:" not in result.stdout
    for path in out.glob("*.md"):
        text = path.read_text()
        assert "module:" not in text, path
        for link in re.findall(r"\]\(([\w.-]+\.md)(?:#[\w-]+)?\)", text):
            assert (out / link).exists(), (path, link)
