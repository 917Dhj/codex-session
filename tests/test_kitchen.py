from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dkitchen import config, ding, orchestrate, refs, watch


CHINESE = """
repo = "o/r"

[stations]
orchestrator = "总编排"
designer = "总设计"
verifier = "核查"
executor = "执行者"
user = "用户"
separator = "："

[status]
fields = ["状态", "在等", "关键数字", "下一步"]
in_progress = "进行中"
waiting_on_design = "等设计"
blocked = "阻塞"
done = "完成"
none = "无"

[log]
issue = 246
pending = "待用户决定"
"""


def settings(tmp_path, text=None):
    if text is not None:
        (tmp_path / config.FILE).write_text(text, encoding="utf-8")
    return config.load(tmp_path)


def test_defaults_without_a_config_file(tmp_path):
    loaded = settings(tmp_path)
    assert loaded.root == tmp_path and loaded.repo is None
    assert loaded.status.fields == ("Status", "Waiting on", "Key numbers", "Next")
    assert loaded.stations.signature(loaded.stations.orchestrator) == "Orchestrator: "


def test_config_file_sets_the_vocabulary(tmp_path):
    loaded = settings(tmp_path, CHINESE)
    assert loaded.repository() == "o/r"
    assert loaded.status.fields == ("状态", "在等", "关键数字", "下一步")
    assert loaded.stations.signature(loaded.stations.orchestrator) == "总编排："
    assert loaded.log.issue == 246


@pytest.mark.parametrize("text, error", [
    ("colour = 1", "unknown keys: colour"),
    ("[status]\nfeilds = ['A']", r"unknown keys in \[status\]: feilds"),
    ("[status]\nfields = []", "nonempty list"),
    ("[log]\nissue = 0", "positive issue number"),
    ("[stations]\ndesigner = ''", "nonempty string"),
    ("repo = 'not a repo'", "owner/name"),
    ("[status", "dkitchen.toml"),
])
def test_bad_config_is_refused_with_its_key(tmp_path, text, error):
    with pytest.raises(ValueError, match=error):
        settings(tmp_path, text)


def test_board_status_falls_back_to_the_config_fields(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / config.FILE).write_text(CHINESE, encoding="utf-8")
    monkeypatch.delenv("DING_STATUS_FIELDS", raising=False)
    monkeypatch.setattr(ding, "unwrapped_tasks", lambda **kwargs: [])
    seen = {}
    monkeypatch.setattr(ding, "show_board", lambda path, all_threads, status: seen.setdefault("fields", status))
    assert ding.main(["board", "--status"]) == 0
    assert seen["fields"] == ("状态", "在等", "关键数字", "下一步")
    monkeypatch.setenv("DING_STATUS_FIELDS", "A,B")
    seen.clear()
    assert ding.main(["board", "--status"]) == 0
    assert seen["fields"] == ("A", "B")


def test_log_entry_is_signed_with_utc_time_and_pending_line(tmp_path):
    moment = datetime(2026, 10, 14, 17, 5, tzinfo=timezone(timedelta(hours=8)))
    english = settings(tmp_path)
    assert orchestrate.log_body(["Merged PR #1"], [], moment, english) == (
        "Orchestrator: 09:05 UTC\n- Merged PR #1\n- Waiting for the user: none")
    chinese = settings(tmp_path, CHINESE)
    assert orchestrate.log_body(["a", "b"], ["#234 复核", "#215 方向"], moment, chinese).endswith(
        "- b\n- 待用户决定：#234 复核; #215 方向")
    assert orchestrate.signed(" 总编排：已转告", chinese) == "总编排：已转告"
    assert orchestrate.signed("已转告", chinese) == "总编排：已转告"
    assert orchestrate.signed("Passed on.", english) == "Orchestrator: Passed on."


def fake_call(responses, calls):
    def call(settings, *args):
        calls.append(args)
        returncode, stdout = responses.pop(0)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="boom" if returncode else "")
    return call


@pytest.mark.parametrize("sent, comments", [(0, 1), (1, 0)])
def test_relay_comments_only_after_confirmed_delivery(tmp_path, monkeypatch, capsys, sent, comments):
    loaded = settings(tmp_path, 'repo = "o/r"')
    sends, calls = [], []
    monkeypatch.setattr(orchestrate.ding, "main", lambda argv: sends.append(argv) or sent)
    monkeypatch.setattr(orchestrate, "call", fake_call([(0, "https://comment")], calls))
    args = SimpleNamespace(ticket=42, message="Go ahead with stage 2.", message_file=None, comment=None,
                           no_comment=False, to=None, socket=None)
    assert orchestrate.relay(args, loaded) == sent
    assert sends == [["send", "o/r#42", "--from", "Orchestrator", "--message", "Orchestrator: Go ahead with stage 2."]]
    assert len(calls) == comments
    if comments:
        assert calls[0][:5] == ("gh", "issue", "comment", "42", "--repo")
        assert calls[0][-1] == "Orchestrator: Go ahead with stage 2."
    else:
        assert "No comment was left" in capsys.readouterr().err


def view(state="OPEN", head="abc123", draft=False):
    return 0, json.dumps({"isDraft": draft, "state": state, "headRefOid": head, "mergeCommit": {"oid": "m1"}})


@pytest.mark.parametrize("responses, code, message", [
    ([view(), (0, ""), view("MERGED")], 0, "PR #7 merged as m1"),
    ([view(head="fff999")], 1, "review it again"),
    ([view(draft=True)], 1, "draft"),
    ([view("MERGED")], 1, "already merged"),
    ([view(), (0, ""), view()], 1, "find out why"),
])
def test_merge_only_at_the_reviewed_head(tmp_path, monkeypatch, capsys, responses, code, message):
    loaded = settings(tmp_path, 'repo = "o/r"')
    calls = []
    monkeypatch.setattr(orchestrate, "call", fake_call(responses, calls))
    assert orchestrate.merge(SimpleNamespace(pr=7, sha="abc"), loaded) == code
    captured = capsys.readouterr()
    assert message in captured.out + captured.err
    if len(calls) > 1:
        assert calls[1][-2:] == ("--match-head-commit", "abc123")


def test_log_needs_a_ticket(tmp_path, capsys):
    args = SimpleNamespace(item=["a"], pending=[], issue=None)
    assert orchestrate.log(args, settings(tmp_path)) == 1
    assert "[log] issue" in capsys.readouterr().err


def test_live_tmux_jobs_count_as_running_but_shells_and_dead_panes_do_not():
    panes = ("ding-issue235-r3-8|0|python\n"
             "ding-issue193-B008-audit|1|sh\n"
             "issue209-stage1-concurrency8-r2|0|bash\n"
             "ding-issue272-a-v2-xhigh-full|0|python\n"
             "download_model|0|python\n")
    assert watch.tmux_issues(panes) == {235, 272}


def test_board_running_reads_only_the_session_table():
    output = ("NAME\tCWD\tSTATUS\tISSUE\tHEAD\tRUN\n"
              "a\t/r\tactive\t#3,#5\thead\trunning\n"
              "b\t/r\tidle\t#4\thead\texited(0)\n"
              "\nUNWRAPPED_PID\tCPU%\tAGE_SECONDS\tTMUX_SESSION\tissue9\trunning\n")
    assert watch.board_running(output) == {3, 5}


def test_second_watch_on_the_same_state_exits(tmp_path):
    state = tmp_path / "watch.json"
    args = SimpleNamespace(state=state, checkout=None, since=None, skip_issue=[])
    with (tmp_path / "watch.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert watch.watch(args, settings(tmp_path, 'repo = "o/r"')) == 3
    assert not state.exists()


def comment(number, body):
    return {"issue_url": f"https://api.github.com/repos/o/r/issues/{number}", "body": body,
            "created_at": "2026-10-14T09:00:00Z", "updated_at": "2026-10-14T09:00:00Z", "html_url": f"u{number}"}


@pytest.mark.parametrize("text, verifier, user", [
    (None, "## Verifier: checked before the run\npass", "I filled in the review"),
    (CHINESE, "## 核查：运行前核对\n通过", "我填好了复核"),
])
def test_verifier_comments_are_listed_without_waking(tmp_path, monkeypatch, text, verifier, user):
    loaded = settings(tmp_path, text)
    comments = [comment(272, verifier), comment(234, user)]
    monkeypatch.setattr(watch, "gh", lambda path: comments if "/issues/comments" in path and "page=1" in path else [])
    found = watch.collect(loaded, "o/r", "2026-10-14T08:00:00Z", "2026-10-14T08:00:00Z", set(), set())
    stations = loaded.stations
    assert [(line.split(":")[0].split(" (")[0], wakes) for _, line, wakes in found] == [
        (f"#234 {stations.user} comment", True), (f"#272 {stations.verifier} comment", False)]


def executor(block):
    return block + "\n\nCodex session: 00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("block, waiting_user, wakes", [
    ("Status: in progress\nWaiting on: none\nKey numbers: 1\nNext: run", False, False),
    ("Status: in progress\nWaiting on: Designer, for the prompt\nKey numbers: 1\nNext: wait", False, True),
    ("Status: done\nWaiting on: none\nKey numbers: 1\nNext: merge", False, True),
    ("Status: waiting on user\nWaiting on: User\nKey numbers: 1\nNext: wait", True, False),
    ("Status: blocked\nWaiting on: none\nKey numbers: 1\nNext: fix", True, True),
])
def test_executor_comments_wake_by_status_and_waiting_on(tmp_path, block, waiting_user, wakes):
    loaded = settings(tmp_path)
    assert watch.role(executor(block), loaded) == "Executor"
    assert watch.executor_wakes(executor(block), waiting_user, loaded) is wakes


def test_chinese_status_words_and_full_width_colons(tmp_path):
    loaded = settings(tmp_path, CHINESE)
    body = executor("状态：进行中\n在等：总设计，要提示词\n关键数字：1\n下一步：等")
    assert watch.executor_wakes(body, False, loaded)
    assert watch.summary(body, loaded) == "状态：进行中; 在等：总设计，要提示词"
    assert not watch.executor_wakes(executor("状态: 进行中\n在等: 无\n关键数字: 1\n下一步: 跑"), False, loaded)


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "work.py").write_text("import os\n\n\ndef reserve(key):\n    if key:\n        raise ValueError(key)\n\n\nclass Ledger:\n    pass\n")
    return tmp_path


def check(draft, tree, tolerance=0):
    return refs.check(draft, refs.Source("HEAD", tree), tolerance)


def test_line_reference_must_hold_the_name_beside_it(tree):
    findings, notes, checked = check("`pkg/work.py:9` holds `Ledger`; `pkg/work.py:6` holds `Ledger`", tree)
    assert checked == 2 and notes == []
    assert len(findings) == 1 and findings[0].startswith("line 1 `pkg/work.py:6`") and "`Ledger`" in findings[0]
    assert check("`pkg/work.py:5-6` uses `key`", tree)[0] == []


def test_name_of_the_enclosing_function_counts_and_tolerance_widens(tree):
    assert check("`pkg/work.py:6` in `reserve` refuses", tree)[0] == []
    assert check("`pkg/work.py:5` has `raise`", tree)[0] != []
    assert check("`pkg/work.py:5` has `raise`", tree, tolerance=1)[0] == []


def test_reference_beyond_the_file_or_to_a_missing_file(tree):
    findings, notes, checked = check("`pkg/work.py:99` and `pkg/gone.py:1`", tree)
    assert findings == ["line 1 `pkg/work.py:99`: beyond the end of the file (10 lines)"]
    assert notes == ["line 1 `pkg/gone.py:1`: no such file, or several files share the name; not checked"]
    assert checked == 1


def test_names_far_from_the_reference_and_times_in_prose_are_ignored(tree):
    assert check("`pkg/work.py:9` was there before 07:47, see also the notes on `Missing`", tree) == ([], [], 1)


def test_sha256_prefix_and_suffix_are_checked_against_the_cited_file(tree):
    digest = hashlib.sha256((tree / "pkg" / "work.py").read_bytes()).hexdigest()
    assert check(f"`pkg/work.py` sha256 `{digest[:8]}…{digest[-4:]}`", tree)[0] == []
    findings, _, checked = check(f"`pkg/work.py` sha256 `{digest[:7]}0`", tree)
    assert checked == 1 and findings and "sha256" in findings[0]
    findings, notes, checked = check(f"sha256 `{digest[:8]}`", tree)
    assert (findings, checked) == ([], 0) and len(notes) == 1


def test_check_refs_command_reports_and_exits_nonzero_on_findings(tree, capsys):
    draft = tree / "draft.md"
    draft.write_text("`pkg/work.py:6` holds `Ledger`\n")
    assert ding.main(["check-refs", str(draft), "--root", str(tree)]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"checked": 1, "findings": 1, "unchecked": 0}
    assert "checked 1 references: 1 wrong, 0 not checked" in captured.err


def test_log_issue_must_be_positive(capsys):
    with pytest.raises(SystemExit) as exc:
        ding.main(["log", "--item", "a", "--issue", "0"])
    assert exc.value.code == 2
