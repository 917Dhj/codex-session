from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dkitchen import config, ding, orchestrate, refs, rule, watch


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


def test_session_name_template_defaults_and_fills_names_verbatim(tmp_path):
    assert settings(tmp_path).sessions.session_name(issue=57, name="Fix {x} 100%", repo="o/r") == \
        "【#57】Fix {x} 100%"
    loaded = settings(tmp_path, '[sessions]\nname = "{repo}#{issue:04d} {name}"')
    assert loaded.sessions.session_name(issue=7, name="Plot", repo="o/r") == "o/r#0007 Plot"


@pytest.mark.parametrize("template, error", [
    ("", "nonempty string"),
    ("{issue} {title}", r"uses \{title\}; use only \{issue\}, \{name\}, \{repo\}"),
    ("{} {name}", r"uses \{\}"),
    ("{name.upper}", r"uses \{name.upper\}"),
    ("{name!r}", "without a conversion"),
    ("{name", "not a valid template"),
    ("{name:d}", "not a valid template"),
    ("{issue:{name}}", "nested field"),
])
def test_bad_session_name_template_is_refused_on_load(tmp_path, template, error):
    with pytest.raises(ValueError, match=error):
        settings(tmp_path, f"[sessions]\nname = {json.dumps(template)}")


def test_board_status_falls_back_to_the_config_fields(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / config.FILE).write_text(CHINESE, encoding="utf-8")
    monkeypatch.delenv("DING_STATUS_FIELDS", raising=False)
    monkeypatch.setattr(ding, "unwrapped_tasks", lambda **kwargs: [])
    seen = {}
    monkeypatch.setattr(ding, "show_board", lambda path, all_threads, status, no_issue_lookup: seen.setdefault("fields", status))
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
    assert sends == [["send", "o/r#42", "--from", "Orchestrator", "--message",
                      "Orchestrator: New decision; it replaces earlier relays not yet carried out.\n"
                      "Go ahead with stage 2.", "--steer", "--supersede"]]
    assert len(calls) == comments
    if comments:
        assert calls[0][:5] == ("gh", "issue", "comment", "42", "--repo")
        assert calls[0][-1] == "Orchestrator: Go ahead with stage 2."
    else:
        assert "No comment was left" in capsys.readouterr().err


@pytest.mark.parametrize("queue, interrupt, tail", [
    (True, False, []), (False, True, ["--steer", "--supersede", "--interrupt"]),
])
def test_relay_queue_keeps_plain_message_and_interrupt_passes_through(tmp_path, monkeypatch, queue, interrupt, tail):
    loaded = settings(tmp_path, CHINESE + '\n[relay]\nsuperseding = "新决策，取代此前尚未执行的转达"\n')
    sends = []
    monkeypatch.setattr(orchestrate.ding, "main", lambda argv: sends.append(argv) or 0)
    args = SimpleNamespace(ticket=42, message="总编排：放行第 2 阶段。", message_file=None, comment=None,
                           no_comment=True, to=None, socket=None, queue=queue, interrupt=interrupt)
    assert orchestrate.relay(args, loaded) == 0
    text = "总编排：放行第 2 阶段。" if queue else "总编排：新决策，取代此前尚未执行的转达\n放行第 2 阶段。"
    assert sends == [["send", "o/r#42", "--from", "总编排", "--message", text, *tail]]


@pytest.mark.parametrize("listed, code", [("待合并：PR#7, PR#9", 0), ("待合并：PR#9", 1), ("", 1)])
def test_merge_accepts_a_pr_the_executor_lists_as_pending_merge(tmp_path, monkeypatch, capsys, listed, code):
    loaded = settings(tmp_path, CHINESE.replace("[status]\n", '[status]\npending_merge = "待合并"\n'))
    status = "状态：进行中\n在等：无\n关键数字：—\n下一步：跑 9B\n" + listed + "\n\nCodex session: x"
    calls = []
    monkeypatch.setattr(orchestrate, "call", fake_call([view(), thread_comments(status), (0, ""), view("MERGED")], calls))
    assert orchestrate.merge(SimpleNamespace(pr=7, sha="abc", ticket=42, no_ticket=False), loaded) == code
    assert ("待合并" in capsys.readouterr().out) == bool(listed)


def view(state="OPEN", head="abc123", draft=False, body=""):
    return 0, json.dumps({"isDraft": draft, "state": state, "headRefOid": head, "mergeCommit": {"oid": "m1"},
                          "body": body})


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
    assert orchestrate.merge(SimpleNamespace(pr=7, sha="abc", ticket=None, no_ticket=True), loaded) == code
    captured = capsys.readouterr()
    assert message in captured.out + captured.err
    if len(calls) > 1:
        assert calls[1][-2:] == ("--match-head-commit", "abc123")


def thread_comments(*bodies):
    return 0, json.dumps([[{"body": body, "html_url": f"https://c/{index}"} for index, body in enumerate(bodies)]])


SESSION = "Codex session: 00000000-0000-4000-8000-000000000001"
WAITING = f"状态：进行中\n在等：总编排审合PR7；核查核对绑定\n关键数字：1\n下一步：合入后启动\n{SESSION}"
WITHDRAWN = f"状态: 进行中\n在等: 无\n关键数字: 1\n下一步: 执行者更新PR7\n{SESSION}"


@pytest.mark.parametrize("latest, code", [(WAITING, 0), (WITHDRAWN, 1),
                                          (f"## 进展\n还在改\n{SESSION}", 1),
                                          (f"状态：完成\n在等：无（总编排已知）\n{SESSION}", 1)])
def test_merge_needs_the_executor_waiting_on_the_orchestrator(tmp_path, monkeypatch, capsys, latest, code):
    loaded = settings(tmp_path, CHINESE)
    calls = []
    comments = thread_comments(WITHDRAWN, "核查：通过", latest, "总编排：收到", "用户：好")
    responses = [view(), comments] + ([(0, ""), view("MERGED")] if code == 0 else [])
    monkeypatch.setattr(orchestrate, "call", fake_call(responses, calls))
    assert orchestrate.merge(SimpleNamespace(pr=7, sha="abc", ticket=236, no_ticket=False), loaded) == code
    captured = capsys.readouterr()
    assert calls[1][:4] == ("gh", "api", "--paginate", "--slurp")
    assert calls[1][4] == "repos/o/r/issues/236/comments?per_page=100"
    assert "#236 latest executor status (https://c/2):" in captured.out
    if code:
        assert len(calls) == 2
        assert "not waiting on 总编排" in captured.err and "Nothing merged" in captured.err
    else:
        assert calls[2][:3] == ("gh", "pr", "merge")
        assert "PR #7 merged as m1" in captured.out


@pytest.mark.parametrize("body, tickets, code, message", [
    ("修 #236 的入口，沿用 #322 的规则。", {236: WAITING, 322: None}, 0, "#236 latest executor status"),
    ("See https://github.com/o/r/issues/236 and PR #7.", {236: WAITING}, 0, "#236 latest executor status"),
    ("#236 和 #322", {236: WAITING, 322: WAITING}, 1, "mentioned in it: #236, #322; pass --ticket N"),
    ("No ticket here; &#39;quoted&#39; and o/r#5 are not references.", {}, 1, "mentioned in it: none"),
])
def test_merge_finds_the_one_ticket_with_executor_comments(tmp_path, monkeypatch, capsys, body, tickets, code, message):
    loaded = settings(tmp_path, CHINESE)
    calls = []
    responses = [view(body=body)]
    responses += [thread_comments(*([latest] if latest else ["总设计：答复"])) for latest in tickets.values()]
    responses += [(0, ""), view("MERGED")] if code == 0 else []
    monkeypatch.setattr(orchestrate, "call", fake_call(responses, calls))
    assert orchestrate.merge(SimpleNamespace(pr=7, sha="abc", ticket=None, no_ticket=False), loaded) == code
    captured = capsys.readouterr()
    assert message in captured.out + captured.err
    assert [call[4] for call in calls if call[:2] == ("gh", "api")] == [
        f"repos/o/r/issues/{number}/comments?per_page=100" for number in tickets]


CLAUDE = "Claude session: ToM agent person experience record implementation"


@pytest.mark.parametrize("bodies, code", [
    ((f"状态：进行中\n在等：总编排审合PR7\n关键数字：1\n下一步：合入后冒烟\n{CLAUDE}",), 0),
    ((WAITING, f"状态：进行中\n在等：无\n关键数字：1\n下一步：还在改PR7\n{CLAUDE}"), 1),
    ((f"状态：完成\n在等：无\n关键数字：1\n下一步：交给执行者\n{CLAUDE}", WAITING), 0),
])
def test_merge_reads_claude_and_codex_executors_by_the_latest_comment(tmp_path, monkeypatch, capsys, bodies, code):
    loaded = settings(tmp_path, CHINESE)
    responses = [view(), thread_comments("总设计：答复", *bodies)] + ([(0, ""), view("MERGED")] if code == 0 else [])
    monkeypatch.setattr(orchestrate, "call", fake_call(responses, []))
    assert orchestrate.merge(SimpleNamespace(pr=7, sha="abc", ticket=1020, no_ticket=False), loaded) == code
    assert f"#1020 latest executor status (https://c/{len(bodies)}):" in capsys.readouterr().out


def test_merge_refuses_a_ticket_without_executor_comments(tmp_path, monkeypatch, capsys):
    loaded = settings(tmp_path, CHINESE)
    monkeypatch.setattr(orchestrate, "call", fake_call([view(), thread_comments("总设计：答复")], []))
    assert orchestrate.merge(SimpleNamespace(pr=7, sha="abc", ticket=9, no_ticket=False), loaded) == 1
    assert "#9 has no executor comment" in capsys.readouterr().err


def test_merge_checks_head_and_draft_before_reading_the_ticket(tmp_path, monkeypatch, capsys):
    loaded = settings(tmp_path, CHINESE)
    calls = []
    monkeypatch.setattr(orchestrate, "call", fake_call([view(head="fff999")], calls))
    assert orchestrate.merge(SimpleNamespace(pr=7, sha="abc", ticket=236, no_ticket=False), loaded) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("argv, error", [
    (["merge", "7", "abc", "--ticket", "0"], "--ticket must be a positive issue number"),
    (["merge", "7", "abc", "--ticket", "3", "--no-ticket"], "not allowed with argument"),
])
def test_merge_ticket_options(capsys, argv, error):
    with pytest.raises(SystemExit) as exc:
        ding.main(argv)
    assert exc.value.code == 2
    assert error in capsys.readouterr().err


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
    ("Status: waiting on user\nWaiting on: User to approve; Orchestrator to merge PR #3\nKey numbers: 1\nNext: merge",
     True, True),
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


def test_an_awaiting_user_ticket_wakes_when_the_comment_also_waits_on_the_kitchen(tmp_path):
    # #209 issuecomment-6179869282: still waiting on the user, but also asking the orchestrator to merge
    loaded = settings(tmp_path, CHINESE)
    body = executor("状态: 等用户\n在等: 用户批准说话人 1,329 次单次作答；总编排重审合入 PR #314\n关键数字: 0\n下一步: 合入")
    assert watch.executor_wakes(body, True, loaded)
    assert not watch.executor_wakes(executor("状态: 等用户\n在等: 用户批准\n关键数字: 0\n下一步: 等"), True, loaded)


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
    report = json.loads(captured.out)
    assert {key: report[key] for key in ("checked", "findings", "unchecked", "by_kind")} == {
        "checked": 1, "findings": 1, "unchecked": 0, "by_kind": {"file:line": 1}}
    assert report["wrong"][0].startswith("line 1 `pkg/work.py:6`")
    assert "checked 1 references (1 file:line): 1 wrong, 0 not checked" in captured.err


GITHUB = {
    "repos/o/r/issues/433": {"number": 433},
    "repos/o/r/issues/543": {"number": 543, "pull_request": {}},
    "repos/o/r/issues/comments/111": {"issue_url": "https://api.github.com/repos/o/r/issues/433"},
    "repos/o/r/issues/comments/222": {"issue_url": "https://api.github.com/repos/o/r/issues/535"},
}


def fake_github(path):
    if path == "repos/o/r/issues/9":
        raise RuntimeError("HTTP 502")
    return GITHUB.get(path)


def test_tickets_prs_and_comment_links_are_checked_against_github(tree):
    github = refs.GitHub("o/r", fake_github)
    counts = {}
    draft = ("见 #433 和 PR#543；#544 不存在，PR #433 不是 PR\n"
             "https://github.com/o/r/issues/433#issuecomment-111 https://github.com/o/r/issues/433#issuecomment-222\n"
             "https://github.com/o/r/pull/543 https://github.com/o/r/issues/543 #9")
    findings, notes, checked = refs.check(draft, refs.Source("HEAD", tree), 0, github, counts)
    assert findings == [
        "line 1 ticket #544: o/r#544 does not exist",
        "line 1 PR PR #433: o/r#433 is an issue, not a PR",
        "line 2 comment link https://github.com/o/r/issues/433#issuecomment-222: comment issuecomment-222 "
        "belongs to #535, not #433",
        "line 3 ticket link https://github.com/o/r/issues/543: o/r#543 is a PR; its link is /pull/543",
    ]
    assert notes == ["line 3 ticket #9: not checked (HTTP 502)"]
    assert counts == {"ticket": 2, "PR": 2, "comment link": 2, "PR link": 1, "ticket link": 1}
    assert checked == 8


def test_bare_line_references_are_checked_and_offline_lists_tickets(tree):
    findings, notes, checked = refs.check("pkg/work.py:99 改了；见 #433", refs.Source("HEAD", tree), 0)
    assert findings == ["line 1 `pkg/work.py:99`: beyond the end of the file (10 lines)"]
    assert notes == ["line 1 ticket #433: not checked (GitHub checks are off (--offline))"]
    assert checked == 1


def test_log_issue_must_be_positive(capsys):
    with pytest.raises(SystemExit) as exc:
        ding.main(["log", "--item", "a", "--issue", "0"])
    assert exc.value.code == 2


@pytest.mark.parametrize("text", [None, CHINESE])
def test_a_quoted_session_signature_signs_nothing(tmp_path, text):
    loaded = settings(tmp_path, text)
    stations = loaded.stations
    verifier = (f"## {stations.verifier}{stations.separator}#272 run request check\n\n"
                "| request body | ok: it starts with the status block and contains `Codex session:` |\n")
    assert watch.role(verifier, loaded) == stations.verifier
    assert watch.role("I checked it; the body has `Codex session:` in it.", loaded) == stations.user
    assert watch.role("Quoting `Claude session:` signs nothing.", loaded) == stations.user
    assert watch.role("Status: done\nWaiting on: none\n\nClaude session: impl", loaded) == stations.executor
    assert watch.role(executor("Status: done\nWaiting on: none\nKey numbers: 1\nNext: merge"), loaded) == stations.executor


RULING = CHINESE + """
[rule]
type_label = "类型"
types = ["可执行", "方案，等用户", "记录"]
notify = "#{ticket} 已答复，见票 {url}"
"""
REPLY = "总设计：结论\n类型：可执行\n\n正文 见 `pkg/work.py:9` holds `Ledger`\n"
URL = "https://github.com/o/r/issues/42#issuecomment-900"


def comment_pages(*bodies):
    return 0, json.dumps([[{"id": 100 + index, "body": body, "created_at": f"2026-10-28T0{index}:00:00Z",
                            "html_url": f"https://github.com/o/r/issues/42#issuecomment-{100 + index}"}
                           for index, body in enumerate(bodies)]])


@pytest.fixture
def ruling(tree, tmp_path, monkeypatch):
    """`ding rule` run offline in a Chinese-vocabulary repo whose gh and ding send are doubles."""
    (tmp_path / config.FILE).write_text(RULING, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    state = SimpleNamespace(calls=[], sends=[], send_code=0, draft=tmp_path / "draft.md")
    state.draft.write_text(REPLY, encoding="utf-8")

    def run(responses, *options, text=None):
        if text is not None:
            state.draft.write_text(text, encoding="utf-8")
        monkeypatch.setattr(rule, "call", fake_call(list(responses), state.calls))
        return real_main(["rule", "42", str(state.draft), "--root", str(tree), "--offline", *options])

    real_main = ding.main

    def main(argv):
        if argv[0] != "send":
            return real_main(argv)
        state.sends.append(argv)
        print("started")
        return state.send_code

    monkeypatch.setattr(ding, "main", main)

    state.run = run
    return state


def test_rule_posts_and_prints_only_the_url(ruling, capsys):
    assert ruling.run([comment_pages("用户：请裁定", "核查：通过"), (0, URL + "\n")], "--reply-to", "101") == 0
    captured = capsys.readouterr()
    assert captured.out == URL + "\n"
    assert "latest comment" not in captured.err
    assert ruling.calls[0][:4] == ("gh", "api", "--paginate", "--slurp")
    assert ruling.calls[1] == ("gh", "issue", "comment", "42", "--repo", "o/r", "--body-file", str(ruling.draft))
    assert ruling.sends == []


def test_rule_without_reply_to_shows_the_latest_comment(ruling, capsys):
    assert ruling.run([comment_pages("用户：请裁定", "核查：通过\n第二行"), (0, URL)]) == 0
    captured = capsys.readouterr()
    assert captured.out == URL + "\n"
    assert "latest comment on #42: 2026-10-28T01:00:00Z '核查：通过'" in captured.err


@pytest.mark.parametrize("text, message", [
    ("核查：结论\n类型：可执行\n", "line 1 must start with '总设计：'"),
    ("总设计：结论\n", "line 2 must start with '类型：'"),
    ("总设计：结论\n正文\n类型：可执行\n", "line 2 must start with '类型：'"),
    ("总设计：结论\n类型：大概可以\n", "must be one of: 可执行, 方案，等用户, 记录"),
])
def test_rule_stops_when_the_first_two_lines_are_wrong(ruling, capsys, text, message):
    assert ruling.run([], text=text) == 1
    assert message in capsys.readouterr().err
    assert ruling.calls == [] and ruling.sends == []


def test_rule_accepts_each_type(ruling, capsys):
    for kind in ("方案，等用户", "记录"):
        assert ruling.run([comment_pages("用户：问"), (0, URL)], text=f"总设计：结论\n类型：{kind}\n") == 0


def test_rule_stops_on_a_wrong_reference_and_posts_nothing(ruling, capsys):
    assert ruling.run([], text="总设计：结论\n类型：可执行\n`pkg/work.py:6` holds `Ledger`\n") == 1
    captured = capsys.readouterr()
    assert "wrong: line 3 `pkg/work.py:6`" in captured.err and "Nothing posted" in captured.err
    assert captured.out == "" and ruling.calls == [] and ruling.sends == []


def test_rule_stops_when_a_comment_came_after_the_one_answered(ruling, capsys):
    pages = comment_pages("用户：请裁定", "用户：补充一点", "核查：新数字")
    link = "https://github.com/o/r/issues/42#issuecomment-100"
    assert ruling.run([pages], "--reply-to", link, "--notify", "#42") == 1
    captured = capsys.readouterr()
    assert captured.out == "" and len(ruling.calls) == 1 and ruling.sends == []
    assert "newer: https://github.com/o/r/issues/42#issuecomment-101 '用户：补充一点'" in captured.err
    assert "newer: https://github.com/o/r/issues/42#issuecomment-102 '核查：新数字'" in captured.err
    assert "--force" in captured.err


def test_rule_force_posts_despite_newer_comments(ruling, capsys):
    assert ruling.run([comment_pages("用户：请裁定", "用户：补充一点"), (0, URL)], "--reply-to", "100", "--force") == 0
    captured = capsys.readouterr()
    assert captured.out == URL + "\n" and "posting anyway" in captured.err


@pytest.mark.parametrize("reply_to, message", [
    ("999", "comment 999 is not among"),
    ("https://github.com/o/r/issues/43#issuecomment-100", "is not on o/r#42"),
    ("https://github.com/o/r/issues/42", "not an issue comment link"),
])
def test_rule_refuses_a_reply_target_that_is_not_on_the_ticket(ruling, capsys, reply_to, message):
    assert ruling.run([comment_pages("用户：请裁定")], "--reply-to", reply_to) == 1
    assert message in capsys.readouterr().err and len(ruling.calls) <= 1


def test_rule_reports_a_failed_notification_but_keeps_the_comment(ruling, capsys):
    ruling.send_code = 1
    assert ruling.run([comment_pages("用户：请裁定"), (0, URL)], "--reply-to", "100", "--notify", "#42") == 1
    captured = capsys.readouterr()
    assert captured.out == URL + "\n"
    assert "the comment stays" in captured.err and "failed" in captured.err
    assert ruling.sends == [["send", "#42", "--from", "总设计", "--message", f"#42 已答复，见票 {URL}\n总设计：结论"]]
    assert [call[:3] for call in ruling.calls] == [("gh", "api", "--paginate"), ("gh", "issue", "comment")]


def test_rule_notifies_a_codex_session_and_keeps_stdout_clean(ruling, capsys):
    assert ruling.run([comment_pages("用户：请裁定"), (0, URL)], "--notify", "00000000-0000-4000-8000-000000000001") == 0
    captured = capsys.readouterr()
    assert captured.out == URL + "\n" and "started" in captured.err
    assert ruling.sends[0][:2] == ["send", "00000000-0000-4000-8000-000000000001"]


def test_rule_refuses_a_claude_session_before_posting(ruling, capsys):
    assert ruling.run([], "--notify", "uds:/tmp/cc-socks/1.sock") == 1
    assert "cannot message a Claude Code session" in capsys.readouterr().err
    assert ruling.calls == [] and ruling.sends == []


def test_rule_reports_a_failed_post_and_a_non_url_answer(ruling, capsys):
    assert ruling.run([comment_pages("用户：问"), (1, "")]) == 1
    assert "comment failed: boom" in capsys.readouterr().err
    assert ruling.run([comment_pages("用户：问"), (0, "created\n")], "--notify", "#42") == 1
    assert "did not print a comment link" in capsys.readouterr().err and ruling.sends == []


BODY = "票面\n第二行\n"


def test_edit_body_writes_only_over_the_baseline_and_verifies(ruling, tmp_path, capsys):
    base = tmp_path / "base.md"
    base.write_text(BODY, encoding="utf-8")
    new = "新票面 `pkg/work.py:9` holds `Ledger`\n"
    responses = [(0, BODY.replace("\n", "\r\n") + "\n"), (0, "https://github.com/o/r/issues/42\n"), (0, new)]
    assert ruling.run(responses, "--edit-body", "--base", str(base), text=new) == 0
    assert capsys.readouterr().out == "https://github.com/o/r/issues/42\n"
    assert ruling.calls[0] == ("gh", "api", "repos/o/r/issues/42", "--jq", ".body")
    assert ruling.calls[1] == ("gh", "issue", "edit", "42", "--repo", "o/r", "--body-file", str(ruling.draft))


def test_edit_body_stops_when_the_body_changed(ruling, tmp_path, capsys):
    base = tmp_path / "base.md"
    base.write_text(BODY, encoding="utf-8")
    assert ruling.run([(0, "票面\n别人改了\n")], "--edit-body", "--base", str(base), text="新票面\n") == 1
    captured = capsys.readouterr()
    assert "+别人改了" in captured.err and "Nothing edited" in captured.err and len(ruling.calls) == 1


def test_edit_body_reports_a_body_that_reads_back_differently(ruling, tmp_path, capsys):
    base = tmp_path / "base.md"
    base.write_text(BODY, encoding="utf-8")
    responses = [(0, BODY), (0, "https://github.com/o/r/issues/42\n"), (0, "新票面 被改了\n")]
    assert ruling.run(responses, "--edit-body", "--base", str(base), text="新票面\n") == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "read back differs" in captured.err


@pytest.mark.parametrize("options, error", [
    (["--edit-body"], "--edit-body and --base go together"),
    (["--base", "x"], "--edit-body and --base go together"),
    (["--edit-body", "--base", "x", "--reply-to", "1"], "belong to comments"),
    (["--force"], "--force goes with --reply-to"),
])
def test_rule_option_combinations(capsys, options, error):
    with pytest.raises(SystemExit) as exc:
        ding.main(["rule", "42", "draft.md", *options])
    assert exc.value.code == 2 and error in capsys.readouterr().err


def test_rule_help_explains_the_steps(capsys):
    with pytest.raises(SystemExit):
        ding.main(["rule", "--help"])
    out = capsys.readouterr().out
    for word in ("--reply-to", "--notify", "--edit-body", "--base", "--force", "check-refs", "SendMessage"):
        assert word in out


def test_check_refs_reports_each_draft_and_fails_when_any_is_wrong(tree, capsys):
    good, bad = tree / "good.md", tree / "bad.md"
    good.write_text("`pkg/work.py:9` holds `Ledger`\n")
    bad.write_text("`pkg/work.py:6` holds `Ledger`\n")
    assert ding.main(["check-refs", str(good), "--root", str(tree)]) == 0
    assert "draft" not in json.loads(capsys.readouterr().out)
    assert ding.main(["check-refs", str(good), str(bad), str(good), "--root", str(tree)]) == 1
    captured = capsys.readouterr()
    reports = [json.loads(line) for line in captured.out.splitlines()]
    assert [(r["draft"], r["findings"]) for r in reports] == [(str(good), 0), (str(bad), 1), (str(good), 0)]
    assert f"wrong: {bad}: line 1 `pkg/work.py:6`" in captured.err
    assert f"{good}: checked 1 references (1 file:line): 0 wrong" in captured.err


def test_check_refs_keeps_going_past_an_unreadable_draft(tree, capsys):
    good = tree / "good.md"
    good.write_text("`pkg/work.py:9` holds `Ledger`\n")
    assert ding.main(["check-refs", str(tree / "missing.md"), str(good), "--root", str(tree)]) == 2
    captured = capsys.readouterr()
    assert "missing.md" in captured.err and json.loads(captured.out)["draft"] == str(good)


@pytest.mark.parametrize("text, error", [
    ("[rule]\ntypes = []", "types must be a nonempty list"),
    ("[rule]\nnotify = 'no link'", "holding {url}"),
    ("[rule]\nnotify = '{url} {oops}'", "not a valid template"),
])
def test_rule_config_is_checked(tmp_path, text, error):
    with pytest.raises(ValueError, match=error):
        settings(tmp_path, text)


def test_rule_defaults_are_english(tmp_path):
    loaded = settings(tmp_path)
    assert loaded.rule.type_label == "Type" and "actionable" in loaded.rule.types
    assert rule.format_problem("Designer: x\nType: record\n", loaded) is None


def test_launch_config_preserves_argv_and_defaults(tmp_path):
    assert settings(tmp_path).launch == config.Launch()
    loaded = settings(tmp_path, '[launch]\npreflight = ["python", "", "two words"]')
    assert loaded.launch.preflight == ("python", "", "two words")
    assert loaded.launch.preflight_timeout == 300


@pytest.mark.parametrize("text, error", [
    ('preflight = "python check.py"', "argv list"),
    ('preflight = []', "argv list"),
    ('preflight = [""]', "argv list"),
    ('preflight = ["python", 1]', "argv list"),
    ('preflight = ["python", "\\u0000"]', "argv list"),
    ('preflight_timeout = 0', "positive finite"),
    ('preflight_timeout = -1', "positive finite"),
    ('preflight_timeout = true', "positive finite"),
    ('preflight_timeout = "300"', "positive finite"),
    ('preflight_timeout = inf', "positive finite"),
    ('preflight_timeout = nan', "positive finite"),
    ('prefligth = ["python"]', "unknown keys"),
])
def test_bad_launch_config_is_refused(tmp_path, text, error):
    with pytest.raises(ValueError, match=error):
        settings(tmp_path, "[launch]\n" + text)


@pytest.mark.parametrize("text", [None, CHINESE])
def test_default_designer_signature_is_shared_and_requires_separator(tmp_path, text):
    loaded = settings(tmp_path, text)
    stations = loaded.stations
    signature = stations.signature(stations.designer)
    assert loaded.designer_signatures == (signature,)
    assert rule.format_problem(f"{signature}decision\nType{stations.separator}record\n", loaded) is None
    assert watch.role(f"## {signature}decision", loaded) == stations.designer
    for prefix in (stations.designer, stations.designer + " (other): "):
        assert rule.format_problem(f"{prefix}\nType{stations.separator}record\n", loaded) is not None
        assert watch.role(prefix, loaded) == stations.user
    assert watch.role(stations.verifier + " report", loaded) == stations.verifier
    assert watch.role(stations.orchestrator + " update", loaded) == stations.orchestrator


@pytest.mark.parametrize("prefix", ["总设计：", "总设计（训练）："])
def test_rule_and_watch_share_configured_designer_signatures(tmp_path, prefix):
    loaded = settings(tmp_path, RULING + 'signatures = ["总设计：", "总设计（训练）："]\n')
    assert rule.format_problem(f"{prefix}结论\n类型：可执行\n", loaded) is None
    assert watch.role(f"{prefix}结论", loaded) == loaded.stations.designer
    problem = rule.format_problem("其他设计：结论\n类型：可执行\n", loaded)
    assert "'总设计：'" in problem and "'总设计（训练）：'" in problem
    assert watch.role("其他设计：结论", loaded) == loaded.stations.user


@pytest.mark.parametrize("value", ['[]', '[1]', '["Designer: ", false]', '[""]', '["   "]', '"Designer: "'])
def test_invalid_rule_signatures_are_rejected_on_load(tmp_path, value):
    with pytest.raises(ValueError, match=r"\[rule\] signatures must be a nonempty list"):
        settings(tmp_path, f"[rule]\nsignatures = {value}")


def test_explicit_signatures_replace_default_and_preserve_spaces(tmp_path):
    loaded = settings(tmp_path, '[rule]\nsignatures = ["Training designer: "]')
    assert loaded.rule.signatures == ("Training designer: ",)
    assert rule.format_problem("Training designer: decision\nType: record", loaded) is None
    assert watch.role("Training designer: decision", loaded) == loaded.stations.designer
    for prefix in ("Training designer:", "Designer: decision"):
        assert rule.format_problem(f"{prefix}\nType: record", loaded) is not None
        assert watch.role(prefix, loaded) == loaded.stations.user


@pytest.mark.parametrize("prefix", ["总设计：", "总设计（训练）："])
def test_rule_command_accepts_each_configured_signature(ruling, tmp_path, prefix):
    (tmp_path / config.FILE).write_text(RULING + 'signatures = ["总设计：", "总设计（训练）："]\n')
    assert ruling.run([comment_pages("用户：问"), (0, URL)], text=f"{prefix}结论\n类型：可执行\n") == 0
    assert len(ruling.calls) == 2


def test_rule_command_refuses_unlisted_signature_before_posting(ruling, tmp_path, capsys):
    (tmp_path / config.FILE).write_text(RULING + 'signatures = ["总设计：", "总设计（训练）："]\n')
    assert ruling.run([], text="总设计（其他）：结论\n类型：可执行\n") == 1
    error = capsys.readouterr().err
    assert "'总设计：'" in error and "'总设计（训练）：'" in error
    assert ruling.calls == [] and ruling.sends == []


@pytest.mark.parametrize("signature", ["  Design: ", "# Design: "])
def test_configured_signature_is_matched_before_watch_markdown_normalization(tmp_path, signature):
    loaded = settings(tmp_path, f"[rule]\nsignatures = {json.dumps([signature])}")
    body = f"{signature}decision\nType: record"
    assert rule.format_problem(body, loaded) is None
    assert watch.role(body, loaded) == loaded.stations.designer


def test_watch_does_not_report_a_configured_designer_as_user(tmp_path, monkeypatch):
    loaded = settings(tmp_path, RULING + 'signatures = ["总设计：", "总设计（训练）："]\n')
    comments = [comment(24, "总设计（训练）：结论"), comment(25, "总设计（其他）：提问")]
    monkeypatch.setattr(watch, "gh", lambda path: comments if "/issues/comments" in path and "page=1" in path else [])
    found = watch.collect(loaded, "o/r", "2026-10-14T08:00:00Z", "2026-10-14T08:00:00Z", set(), set())
    assert len(found) == 1
    assert "#25 用户 comment" in found[0][1] and found[0][2] is True


@pytest.mark.parametrize("recover", [True, False])
def test_watch_post_settle_timeout_is_retried_without_losing_batch(tmp_path, monkeypatch, capsys, recover):
    import subprocess
    loaded = config.Config(root=tmp_path, repo="o/r")
    monkeypatch.setattr(config, "load", lambda: loaded)
    monkeypatch.setattr(watch, "stale", lambda *args: [])
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: None)
    pulls = []
    original_since = "2026-10-14T08:00:00Z"
    state = tmp_path / "watch.json"
    state.write_text(json.dumps({"since": original_since, "seen": [], "stale": []}))
    original = state.read_bytes()

    def gh(path):
        assert state.read_bytes() == original
        if "/issues/comments?" in path:
            return [comment(26, "User question")]
        if "/pulls?" in path:
            pulls.append(path)
            if len(pulls) % 2 == 0 and (not recover or len(pulls) == 2):
                raise subprocess.TimeoutExpired(["gh", "api", path], 60)
        return []

    monkeypatch.setattr(watch, "gh", gh)
    result = ding.main(["watch", "--state", str(state), "--interval", "0", "--settle", "0", "--max-errors", "2"])
    assert result == (0 if recover else 2)
    captured = capsys.readouterr()
    assert "poll failed 1/2" in captured.err
    assert len(pulls) == 4
    if recover:
        assert captured.out.count("#26 User comment") == 1
        assert json.loads(state.read_text())["since"] != original_since
    else:
        assert state.read_bytes() == original
        assert "#26 User comment" not in captured.out


@pytest.fixture
def idle_ticket(tmp_path, monkeypatch):
    class Clock(datetime):
        moment = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.moment

    monkeypatch.setattr(watch, "datetime", Clock)
    loaded = config.Config(root=tmp_path, repo="o/r")
    last = {"id": 2601, "body": executor("Status: in progress\nWaiting on: none\nKey numbers: 1\nNext: check"),
            "created_at": "2026-10-28T10:00:00Z", "updated_at": "2026-10-28T10:00:00Z",
            "html_url": "https://github.com/o/r/issues/26#issuecomment-2601"}
    issue = {"number": 26, "comments": 1, "labels": [], "updated_at": last["updated_at"]}
    state = SimpleNamespace(config=loaded, clock=Clock, last=last, issues=[issue], comments=[last],
                            board="NAME\tCWD\tSTATUS\tISSUE\tHEAD\tRUN\n【#26】worker\t/r\tidle\t#26\thead\t\n", panes="")
    calls = []

    def gh(path):
        calls.append(path)
        if "/issues/26/comments?" in path:
            return state.comments
        if "/issues?state=open&per_page=100" in path:
            return state.issues
        raise AssertionError(path)

    monkeypatch.setattr(watch, "gh", gh)
    state.board_reader = watch.board_output
    monkeypatch.setattr(watch, "board_output", lambda checkout: state.board)
    monkeypatch.setattr(watch, "tmux_panes", lambda: state.panes)
    state.calls = calls
    return state


def check_idle(ticket, observed=None, reported=None, skip=None):
    return watch.idle(ticket.config, "o/r", ticket.config.root,
                      observed or {}, reported or set(), skip or set())


def test_idle_executor_wakes_after_threshold_and_reports_comment_summary(idle_ticket):
    ticket = idle_ticket
    found, observed = check_idle(ticket)
    assert found == []
    assert observed == {"26": {"comment_id": 2601, "since": "2026-10-28T12:00:00Z"}}
    ticket.clock.moment += timedelta(minutes=46)
    found, observed = check_idle(ticket, observed)
    assert len(found) == 1 and found[0][2] == 2601
    assert "#26 executor idle for 46 minutes while still in progress" in found[0][1]
    assert "no running ding run or tmux job" in found[0][1]
    assert "Status: in progress; Waiting on: none" in found[0][1]
    assert ticket.last["html_url"] in found[0][1]


@pytest.mark.parametrize("condition", ["active", "unknown", "run", "tmux", "no_session", "done", "label", "skip"])
def test_idle_wake_requires_every_condition_and_resets_observation(idle_ticket, condition):
    ticket = idle_ticket
    _, observed = check_idle(ticket)
    ticket.clock.moment += timedelta(hours=1)
    skip = set()
    if condition in {"active", "unknown"}:
        ticket.board += f"【#26】other\t/r\t{condition}\t#26\thead\t\n"
    elif condition == "run":
        ticket.board = ticket.board.replace("head\t\n", "head\trunning\n")
    elif condition == "tmux":
        ticket.panes = "ding-issue26-job|0|python\n"
    elif condition == "no_session":
        ticket.board = ticket.board.replace("#26", "#27")
    elif condition == "done":
        ticket.last["body"] = ticket.last["body"].replace("in progress", "done")
    elif condition == "label":
        ticket.issues[0]["labels"] = [{"name": "status:awaiting-user"}]
    elif condition == "skip":
        skip = {26}
    found, pending = check_idle(ticket, observed, skip=skip)
    assert found == [] and pending == {}


@pytest.mark.parametrize("minutes", [44, 45])
def test_idle_threshold_must_be_exceeded(idle_ticket, minutes):
    _, observed = check_idle(idle_ticket)
    idle_ticket.clock.moment += timedelta(minutes=minutes)
    assert check_idle(idle_ticket, observed)[0] == []


def test_idle_accepts_not_loaded_sessions_and_ignores_shell_or_dead_tmux(idle_ticket):
    ticket = idle_ticket
    ticket.board += "【#26】other\t/r\tnotLoaded\t#26\thead\t\n"
    ticket.panes = "ding-issue26-shell|0|bash\nding-issue26-done|1|python\n"
    _, observed = check_idle(ticket)
    ticket.clock.moment += timedelta(hours=1)
    assert len(check_idle(ticket, observed)[0]) == 1


def test_idle_reports_once_per_comment_and_new_comment_starts_a_new_period(idle_ticket):
    ticket = idle_ticket
    _, observed = check_idle(ticket)
    ticket.clock.moment += timedelta(hours=1)
    found, observed = check_idle(ticket, observed)
    assert len(found) == 1
    reported = {found[0][2]}
    assert check_idle(ticket, observed, reported)[0] == []
    ticket.last.update(id=2602, created_at="2026-10-28T13:00:00Z", updated_at="2026-10-28T13:00:00Z")
    found, observed = check_idle(ticket, observed, reported)
    assert found == [] and observed["26"]["comment_id"] == 2602
    ticket.clock.moment += timedelta(minutes=46)
    assert check_idle(ticket, observed, reported)[0][0][2] == 2602


def test_idle_uses_latest_executor_comment_even_after_a_user_reply(idle_ticket):
    ticket = idle_ticket
    ticket.comments.append({"id": 2602, "body": "User followup"})
    ticket.issues[0]["comments"] = 2
    _, observed = check_idle(ticket)
    ticket.clock.moment += timedelta(hours=1)
    assert check_idle(ticket, observed)[0][0][2] == 2601


def test_idle_never_ages_before_the_latest_executor_comment_update(idle_ticket):
    ticket = idle_ticket
    _, observed = check_idle(ticket)
    ticket.clock.moment += timedelta(hours=1)
    ticket.last["updated_at"] = "2026-10-28T12:50:00Z"
    assert check_idle(ticket, observed)[0] == []


def test_idle_disabled_does_not_query_github_board_or_tmux(tmp_path, monkeypatch):
    loaded = settings(tmp_path, '[watch]\nidle_minutes = 0')
    def unexpected(*args):
        pytest.fail("disabled idle check must not query")
    for name in ("gh", "board_output", "tmux_panes"):
        monkeypatch.setattr(watch, name, unexpected)
    assert watch.idle(loaded, "o/r", tmp_path, {}, set(), set()) == ([], {})


@pytest.mark.parametrize("value", ['-1', '"45"', 'true', 'nan', 'inf'])
def test_bad_watch_idle_minutes_is_rejected(tmp_path, value):
    with pytest.raises(ValueError, match=r"\[watch\] idle_minutes must be a nonnegative finite number"):
        settings(tmp_path, f"[watch]\nidle_minutes = {value}")


def test_watch_config_defaults_and_unknown_keys(tmp_path):
    assert settings(tmp_path).watch.idle_minutes == 45
    assert settings(tmp_path, '[watch]\nidle_minutes = 0.5').watch.idle_minutes == 0.5
    with pytest.raises(ValueError, match=r"unknown keys in \[watch\]: idle_minute"):
        settings(tmp_path, '[watch]\nidle_minute = 1')


def test_idle_observation_survives_restart_without_advancing_since(idle_ticket, monkeypatch, capsys):
    ticket = idle_ticket
    monkeypatch.setattr(config, "load", lambda: ticket.config)
    monkeypatch.setattr(watch, "stale", lambda *args: [])
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: None)
    calls = []

    def collect(*args):
        calls.append(None)
        if len(calls) > 1:
            raise RuntimeError("offline")
        return []

    monkeypatch.setattr(watch, "collect", collect)
    path = ticket.config.root / "watch.json"
    arguments = ["watch", "--state", str(path), "--since", "2026-10-28T10:00:00Z",
                 "--interval", "0", "--settle", "0", "--stale-every", "0", "--max-errors", "1"]
    assert ding.main(arguments) == 2
    state = json.loads(path.read_text())
    assert state["since"] == "2026-10-28T10:00:00Z"
    assert state["idle_observed"]["26"]["since"] == "2026-10-28T12:00:00Z"
    assert state["labels_since"] == "2026-10-28T10:00:00Z"
    def resumed_collect(config, repo, since, labels_since, seen, skip):
        assert since == labels_since == "2026-10-28T10:00:00Z"
        return []
    monkeypatch.setattr(watch, "collect", resumed_collect)
    ticket.clock.moment += timedelta(hours=1)
    assert ding.main(arguments) == 0
    state = json.loads(path.read_text())
    assert state["idle_reported"] == [2601]
    assert "labels_since" not in state
    assert state["stale"] == []
    assert capsys.readouterr().out.count("#26 executor idle") == 1
    assert check_idle(ticket, state["idle_observed"], set(state["idle_reported"]))[0] == []


@pytest.mark.parametrize("stage", ["collect", "stale", "idle"])
@pytest.mark.parametrize("recover", [True, False])
def test_watch_bounds_errors_in_every_poll_phase(tmp_path, monkeypatch, capsys, stage, recover):
    import subprocess
    loaded = config.Config(root=tmp_path, repo="o/r")
    monkeypatch.setattr(config, "load", lambda: loaded)
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: None)
    item = ("2026-10-28T12:00:00Z", "#26 recovered", True)
    monkeypatch.setattr(watch, "collect", lambda *args: [item] if stage == "collect" else [])
    monkeypatch.setattr(watch, "stale", lambda *args: [])
    monkeypatch.setattr(watch, "idle", lambda *args: ([], {}))
    monkeypatch.setattr(watch, "unanswered_design", lambda *args: [])
    attempts = []

    def fail_then_recover(*args):
        attempts.append(None)
        if len(attempts) == 1 or not recover:
            if stage == "stale":
                raise RuntimeError("gh api failed")
            raise subprocess.TimeoutExpired(["gh", "api"], 60)
        if stage == "collect":
            return [item]
        return [(item[0], item[1], 2601)] if stage == "stale" else ([(item[0], item[1], 2601)], {})

    monkeypatch.setattr(watch, stage, fail_then_recover)
    path = tmp_path / "watch.json"
    result = ding.main(["watch", "--state", str(path), "--interval", "0", "--settle", "0", "--max-errors", "2"])
    assert result == (0 if recover else 2)
    captured = capsys.readouterr()
    assert "poll failed 1/2" in captured.err
    if recover:
        assert captured.out.count("#26 recovered") == 1
        assert path.exists()
    else:
        assert len(attempts) == 2
        assert "poll failed 2/2" in captured.err
        assert not path.exists()


def test_idle_paginates_back_to_the_latest_executor_comment(idle_ticket, monkeypatch):
    ticket = idle_ticket
    ticket.issues[0]["comments"] = 101
    gh = watch.gh
    pages = []
    def comments(path):
        if "/issues/26/comments?" in path:
            pages.append(path)
            return [{"body": "User followup"}] if path.endswith("page=2") else [ticket.last]
        return gh(path)
    monkeypatch.setattr(watch, "gh", comments)
    _, observed = check_idle(ticket)
    ticket.clock.moment += timedelta(hours=1)
    assert check_idle(ticket, observed)[0][0][2] == 2601
    assert len(pages) == 4


def test_idle_wake_skips_log_issue(idle_ticket, monkeypatch, capsys):
    from dataclasses import replace
    ticket = idle_ticket
    ticket.config = replace(ticket.config, log=config.Log(issue=26))
    monkeypatch.setattr(config, "load", lambda: ticket.config)
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(watch, "stale", lambda *args: [])
    calls = []
    def collect(*args):
        calls.append(None)
        if len(calls) > 1:
            return [("2026-10-28T12:00:00Z", "other event", True)]
        return []
    monkeypatch.setattr(watch, "collect", collect)
    path = ticket.config.root / "watch.json"
    assert ding.main(["watch", "--state", str(path), "--interval", "0", "--settle", "0"]) == 0
    assert json.loads(path.read_text())["idle_observed"] == {}
    assert "#26 executor idle" not in capsys.readouterr().out


def test_stale_keeps_its_existing_silence_rule_and_comment_deduplication(idle_ticket):
    ticket = idle_ticket
    ticket.clock.moment += timedelta(hours=6)
    found = watch.stale(ticket.config, "o/r", ticket.config.root, 6, set(), set())
    assert len(found) == 1 and found[0][2] == 2601
    assert "executor silent for over 6 hours" in found[0][1]
    assert watch.stale(ticket.config, "o/r", ticket.config.root, 6, {2601}, set()) == []
    ticket.panes = "ding-issue26-job|0|python\n"
    assert watch.stale(ticket.config, "o/r", ticket.config.root, 6, set(), set()) == []


def test_idle_and_stale_use_template_names_with_an_empty_issue_column(idle_ticket):
    ticket = idle_ticket
    ticket.board = "NAME\tCWD\tSTATUS\tISSUE\tHEAD\tRUN\n【#26】worker\t/r\tidle\t\thead\t\n"
    _, observed = check_idle(ticket)
    ticket.clock.moment += timedelta(hours=1)
    assert check_idle(ticket, observed)[0][0][2] == 2601
    ticket.clock.moment += timedelta(hours=5)
    ticket.board = ticket.board.replace("head\t\n", "head\trunning\n")
    assert watch.stale(ticket.config, "o/r", ticket.config.root, 6, set(), set()) == []


@pytest.mark.parametrize("code", [0, 1])
def test_watch_board_skips_lookup_and_checks_exit_code(tmp_path, monkeypatch, code):
    output = "NAME\tCWD\tSTATUS\tISSUE\tHEAD\tRUN\nworker\t/r\tidle\t\thead\t\n"
    def board(argv, **kwargs):
        assert argv[-2:] == ["board", "--no-issue-lookup"]
        return SimpleNamespace(returncode=code, stdout=output, stderr="board failed" if code else "")
    monkeypatch.setattr(watch.subprocess, "run", board)
    if code:
        with pytest.raises(RuntimeError, match="board failed"):
            watch.board_output(tmp_path)
    else:
        assert watch.board_output(tmp_path) == output


def test_idle_schedule_is_independent_of_hourly_stale_check(idle_ticket, monkeypatch, capsys):
    from dataclasses import replace
    ticket = idle_ticket
    ticket.config = replace(ticket.config, watch=config.Watch(idle_minutes=15))
    monkeypatch.setattr(config, "load", lambda: ticket.config)
    monkeypatch.setattr(watch, "collect", lambda *args: [])
    start = ticket.clock.moment
    monkeypatch.setattr(watch.time, "monotonic", lambda: (ticket.clock.moment - start).total_seconds())
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: setattr(
        ticket.clock, "moment", ticket.clock.moment + timedelta(seconds=seconds)))
    stale_checks = []
    monkeypatch.setattr(watch, "stale", lambda *args: stale_checks.append(ticket.clock.moment) or [])
    result = ding.main(["watch", "--state", str(ticket.config.root / "watch.json"),
                        "--interval", "7200", "--stale-every", "3600", "--settle", "0"])
    assert result == 0
    assert ticket.clock.moment - start == timedelta(minutes=20)
    assert stale_checks == [start]
    assert "#26 executor idle" in capsys.readouterr().out


@pytest.mark.parametrize("template, name, repo, expected", [
    ("【#{issue}】{name}", "【#868】worker", "", 868),
    ("【#{issue}】{name}", "【#868】two\nlines", "", 868),
    ("【#{issue}】{name}", "【#0】worker", "", None),
    ("【#{issue}】{name}", "【# 】worker", "", None),
    ("【#{issue}】{name}", "Legacy worker", "", None),
    ("{repo}#{issue:04d} {name}", "o/r#0868 Plot", "o/r", 868),
    ("{repo}#{issue:04d} {name}", "other/repo#0868 Plot", "o/r", None),
    ("{{x}} {issue:^6d} :: {name}", "{x}   26   :: Plot", "", 26),
    ("{name} #{issue}", "Worker #868", "", 868),
    ("#{issue} {name} #{issue}", "#26 Worker #26", "", 26),
    ("#{issue}", "#26", "", 26),
    ("{name}", "Worker #868", "", None),
    ("{issue}{name}", "868worker", "", None),
])
def test_session_template_ticket_parsing(template, name, repo, expected):
    assert config.Sessions(name=template).issue_number(name, repo) == expected


@pytest.mark.parametrize("phase", ["stale", "idle"])
def test_board_error_is_a_failed_poll_in_both_checks(idle_ticket, monkeypatch, capsys, phase):
    ticket = idle_ticket
    ticket.clock.moment += timedelta(hours=6)
    monkeypatch.setattr(config, "load", lambda: ticket.config)
    monkeypatch.setattr(watch, "collect", lambda *args: [])
    if phase == "idle":
        monkeypatch.setattr(watch, "stale", lambda *args: [])
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: None)
    def board_failure(*args):
        raise RuntimeError("ding board failed: App Server unavailable")
    monkeypatch.setattr(watch, "board_output", board_failure)
    path = ticket.config.root / "watch.json"
    assert ding.main(["watch", "--state", str(path), "--interval", "0", "--max-errors", "2"]) == 2
    assert not path.exists()
    output = capsys.readouterr()
    assert "poll failed 1/2" in output.err and "poll failed 2/2" in output.err
    assert "#26 executor" not in output.out


@pytest.mark.parametrize("minutes, every", [(3, 60), (45, 900), (120, 900)])
def test_idle_cadence_is_capped_independently_of_poll_interval(idle_ticket, monkeypatch, minutes, every):
    from dataclasses import replace
    ticket = idle_ticket
    ticket.config = replace(ticket.config, watch=config.Watch(idle_minutes=minutes))
    monkeypatch.setattr(config, "load", lambda: ticket.config)
    monkeypatch.setattr(watch, "collect", lambda *args: [])
    monkeypatch.setattr(watch, "stale", lambda *args: [])
    start = ticket.clock.moment
    monkeypatch.setattr(watch.time, "monotonic", lambda: (ticket.clock.moment - start).total_seconds())
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: setattr(
        ticket.clock, "moment", ticket.clock.moment + timedelta(seconds=seconds)))
    checks = []
    def idle(*args):
        checks.append((ticket.clock.moment - start).total_seconds())
        found = [(watch.now(), "idle wake", 2601)] if len(checks) == 3 else []
        return found, {}
    monkeypatch.setattr(watch, "idle", idle)
    assert ding.main(["watch", "--state", str(ticket.config.root / "watch.json"),
                      "--interval", "7200", "--stale-every", "3600"]) == 0
    assert checks == [0, every, 2 * every]


@pytest.mark.parametrize("unmatched_status", ["idle", "active"])
def test_watch_mixed_session_names_skip_failing_repository_scan(idle_ticket, monkeypatch, capsys, unmatched_status):
    import contextlib
    import io
    import subprocess
    ticket = idle_ticket
    root = ticket.config.root
    monkeypatch.setattr(config, "load", lambda: ticket.config)
    monkeypatch.setattr(watch, "collect", lambda *args: [])
    monkeypatch.setattr(watch, "stale", lambda *args: [])
    monkeypatch.setattr(watch, "board_output", ticket.board_reader)
    monkeypatch.setattr(ding, "repository_roots", lambda: [root])
    monkeypatch.setattr(ding, "latest_runs", lambda: {})
    monkeypatch.setattr(ding, "unwrapped_tasks", lambda **kwargs: [])
    threads = [{"id": str(index), "name": name, "cwd": str(root), "status": {"type": "idle"}}
               for index, name in enumerate(["【#26】worker", "检查 gml_10 系统盘"])]
    threads[1]["status"]["type"] = unmatched_status
    @contextlib.contextmanager
    def server(path):
        yield lambda *args: {}
    monkeypatch.setattr(ding, "app_server", server)
    monkeypatch.setattr(ding, "socket_path", lambda path: "/mock.sock")
    monkeypatch.setattr(ding, "board_threads", lambda *args: threads)
    monkeypatch.setattr(ding, "read_reply", lambda *args: "")
    lookups = []
    def failing_scan():
        lookups.append(None)
        raise subprocess.TimeoutExpired(["gh", "api", "repository-wide scan"], 60)
    monkeypatch.setattr(ding, "issue_map", failing_scan)
    def board(argv, **kwargs):
        assert argv[:4] == [watch.sys.executable, "-m", "dkitchen.ding", "board"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = ding.main(argv[3:])
        if code == 0:
            rows = [line.split("\t") for line in stdout.getvalue().splitlines()[1:]]
            assert [row[3] for row in rows] == ["#26", ""]
        return SimpleNamespace(returncode=code, stdout=stdout.getvalue(), stderr=stderr.getvalue())
    monkeypatch.setattr(watch.subprocess, "run", board)
    path = root / "watch.json"
    path.write_text(json.dumps({"since": "2026-10-28T10:00:00Z", "seen": [], "stale": [],
                               "idle_observed": {"26": {"comment_id": 2601, "since": "2026-10-28T11:00:00Z"}}}))
    assert ding.main(["watch", "--state", str(path), "--max-errors", "1"]) == 0
    assert lookups == []
    output = capsys.readouterr()
    assert "#26 executor idle" in output.out
    assert "poll failed" not in output.err
    assert json.loads(path.read_text())["idle_reported"] == [2601]


@pytest.mark.parametrize("template", ["#{ticket} 已答复，见票 {url}", "#{ticket} {first_line} {url}"])
def test_rule_notify_preserves_the_complete_178_character_first_line(ruling, tmp_path, template):
    first = "总设计：" + "结" * 174
    assert len(first) == 178
    configured = RULING.replace('notify = "#{ticket} 已答复，见票 {url}"', f"notify = {json.dumps(template)}")
    (tmp_path / config.FILE).write_text(configured)
    assert ruling.run([comment_pages("用户：问"), (0, URL)], "--notify", "#42",
                      text=first + "  \n类型：可执行\n正文\n") == 0
    expected = template.format(ticket=42, first_line=first, url=URL)
    if "{first_line}" not in template:
        expected += "\n" + first
    assert ruling.sends == [["send", "#42", "--from", "总设计", "--message", expected]]


def test_rule_first_line_is_complete_stripped_and_skips_empty_lines():
    first = "结" * 178
    assert rule.first_line("\n  \n " + first + "  \nnext") == first
    assert rule.first_line("\n \n") == ""


@pytest.mark.parametrize("mode", ["latest", "newer"])
def test_rule_long_comment_hints_add_ellipsis(ruling, capsys, mode):
    first = "结" * 178
    if mode == "latest":
        assert ruling.run([comment_pages(first + "\nnext"), (0, URL)]) == 0
    else:
        assert ruling.run([comment_pages("问", first + "\nnext")], "--reply-to", "100") == 1
    error = capsys.readouterr().err
    assert repr(first[:100] + "…") in error
    assert first not in error


@pytest.mark.parametrize("length", [0, 39, 40, 80, 100, 101, 160, 178])
def test_display_truncation_only_marks_shortened_text(length):
    from dkitchen import truncate
    text = "结" * length
    assert truncate(text, 100) == (text if length <= 100 else text[:100] + "…")


@pytest.mark.parametrize("body", ["short", "结" * 160, "结" * 178,
                                   "Status: " + "x" * 100 + "\nWaiting on: " + "y" * 100])
def test_watch_summary_marks_truncation_only_when_needed(tmp_path, body):
    full = body.replace("\n", "; ")
    expected = full[:160] + "…" if len(full) > 160 else full
    assert watch.summary(body, settings(tmp_path)) == expected


@pytest.mark.parametrize("length", [79, 80, 81])
def test_watch_pr_title_marks_truncation(tmp_path, monkeypatch, length):
    title = "题" * length
    pull = {"number": 29, "title": title, "created_at": "2026-10-29T00:00:00Z", "html_url": "https://github.com/o/r/pull/29"}
    monkeypatch.setattr(watch, "gh", lambda path: [pull] if "/pulls?" in path else [])
    result = watch.collect(settings(tmp_path), "o/r", "2026-10-28T00:00:00Z", "2026-10-28T00:00:00Z", set(), set())
    shown = title[:80] + "…" if length > 80 else title
    assert result == [(pull["created_at"], f"PR #29 opened: {shown} {pull['html_url']}", True)]


def test_diagnostic_excerpt_truncations_are_marked(tmp_path, monkeypatch):
    loaded = settings(tmp_path)
    assert repr("x" * 40 + "…") in rule.format_problem("x" * 41, loaded)
    assert repr("x" * 40 + "…") in rule.format_problem("Designer: decision\n" + "x" * 41, loaded)
    monkeypatch.setattr(watch.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=1, stderr="x" * 201))
    with pytest.raises(RuntimeError, match="x{200}…$"):
        watch.board_output(tmp_path)


def test_reference_display_excerpts_are_marked(tree):
    (tree / "pkg/long.py").write_text("x" * 81 + "\n")
    findings, _, _ = refs.check("`pkg/long.py:1` holds `Ledger`", refs.Source("HEAD", tree), 0)
    assert findings[0].endswith(repr("x" * 80 + "…"))
    def offline(*args):
        raise RuntimeError("x" * 121)
    github = SimpleNamespace(repo="o/r", problem=offline)
    _, notes, _ = refs.check("see #42", refs.Source("HEAD", tree), 0, github)
    assert notes == [f"line 1 ticket #42: not checked ({'x' * 120}…)"]


def test_rule_baseline_diff_marks_omitted_lines(ruling, tmp_path, capsys):
    base = tmp_path / "base.md"
    base.write_text("\n".join(f"old {index}" for index in range(10)))
    current = "\n".join(f"new {index}" for index in range(10))
    assert ruling.run([(0, current)], "--edit-body", "--base", str(base), text="new body\n") == 1
    error = capsys.readouterr().err
    assert "-old 5\n…\n" in error
    assert "-old 6" not in error


@pytest.fixture
def station_settings(tmp_path):
    return settings(tmp_path, RULING + 'signatures = ["总设计：", "总设计（训练）："]\n')


@pytest.mark.parametrize("station, waiting, signed, expected", [
    ("总设计", "总设计", True, True),
    ("总设计", "总设计（训练）", True, False),
    ("总设计（训练）", "总设计（训练）", True, True),
    ("总设计", "核查", True, False),
    ("总设计", "无", True, False),
    ("总设计", "总设计", False, False),
    ("总设计", "总设计（训练）和总设计", True, True),
    ("总设计", "总设计和总设计（训练）", True, True),
])
def test_station_collect_wakes_only_matching_executor_comments(station_settings, monkeypatch,
                                                              station, waiting, signed, expected):
    body = f"状态：进行中\n在等：{waiting}\n关键数字：1\n下一步：等"
    if signed:
        body = executor(body)
    calls = []
    def gh(path):
        calls.append(path)
        assert "/issues/comments?" in path, "station watches must not query labels, PRs or ticket lists"
        return [comment(30, body)]
    monkeypatch.setattr(watch, "gh", gh)
    result = watch.collect(station_settings, "o/r", "2026-10-14T08:00:00Z", "2026-10-14T08:00:00Z",
                           set(), set(), station=station)
    assert bool(result) is expected
    assert len(calls) == 1
    if expected:
        assert result[0][2] is True and "#30 执行者 comment" in result[0][1]


def test_station_match_uses_all_station_names_and_strips_signature_separators(station_settings):
    from dataclasses import replace
    loaded = replace(station_settings, stations=replace(station_settings.stations, verifier="总设计（审稿）"))
    for longer in ("总设计（训练）", "总设计（审稿）"):
        body = executor(f"状态：进行中\n在等：{longer}\n关键数字：1\n下一步：等")
        assert not watch.waiting_for(body, "总设计", loaded)
        assert watch.waiting_for(body, longer, loaded)
    english = config.Config(root=loaded.root, rule=config.Rule(signatures=("Design+Ops: ", "Design+Ops (training): ")))
    body = executor("Status: in progress\nWaiting on: Design+Ops (training)\nKey numbers: 1\nNext: wait")
    assert not watch.waiting_for(body, "Design+Ops", english)
    assert watch.waiting_for(body, "Design+Ops (training)", english)


@pytest.mark.parametrize("state", ["进行中", "等用户", "完成"])
def test_station_watch_matching_comment_wakes_even_on_awaiting_user_ticket(station_settings, monkeypatch, state):
    item = comment(30, executor(f"状态：{state}\n在等：总设计\n关键数字：1\n下一步：等"))
    item["issue"] = {"labels": [{"name": station_settings.labels.awaiting_user}]}
    def gh(path):
        assert "/issues/comments?" in path
        return [item]
    monkeypatch.setattr(watch, "gh", gh)
    assert watch.collect(station_settings, "o/r", "2026-10-14T08:00:00Z", "2026-10-14T08:00:00Z",
                         set(), set(), station="总设计")[0][2] is True


def test_station_watch_skips_other_triggers_and_honors_settle(station_settings, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "load", lambda: station_settings)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    calls, sleeps = [], []
    items = [comment(30, executor("状态：进行中\n在等：总设计\n关键数字：1\n下一步：等")),
             comment(31, "核查：报告"), comment(32, "用户问题")]
    def gh(path):
        calls.append(path)
        assert "/issues/comments?" in path
        return items
    monkeypatch.setattr(watch, "gh", gh)
    def forbidden(*args, **kwargs):
        pytest.fail("station watch must not consult stale, idle, board or tmux")
    for name in ("stale", "idle", "board_output", "tmux_panes"):
        monkeypatch.setattr(watch, name, forbidden)
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: sleeps.append(seconds))
    assert ding.main(["watch", "--for", "总设计", "--settle", "7",
                      "--since", "2026-10-14T08:00:00Z"]) == 0
    assert len(calls) == 2 and sleeps == [7]
    output = capsys.readouterr().out
    assert output.count("#30 执行者 comment") == 1
    assert "#31" not in output and "#32" not in output
    assert watch.default_state("o/r", "总设计").exists()
    assert not watch.default_state("o/r").exists()


def test_station_watch_retains_skip_issue_and_seen_filters(station_settings, monkeypatch):
    body = executor("状态：进行中\n在等：总设计\n关键数字：1\n下一步：等")
    monkeypatch.setattr(watch, "gh", lambda path: [comment(30, body)])
    args = (station_settings, "o/r", "2026-10-14T08:00:00Z", "2026-10-14T08:00:00Z")
    item = watch.collect(*args, set(), set(), station="总设计")[0]
    assert watch.collect(*args, {item[1]}, set(), station="总设计") == []
    assert watch.collect(*args, set(), {30}, station="总设计") == []


def test_station_and_orchestrator_watches_run_concurrently_with_separate_default_states(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    loaded = config.Config(root=tmp_path, repo="o/r")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    barrier = Barrier(2)
    def collect(*args, **kwargs):
        barrier.wait(timeout=5)
        return [(watch.now(), "matching comment", True)]
    monkeypatch.setattr(watch, "collect", collect)
    monkeypatch.setattr(watch, "stale", lambda *args: [])
    monkeypatch.setattr(watch, "idle", lambda *args: ([], {}))
    monkeypatch.setattr(watch, "unanswered_design", lambda *args: [])
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: None)
    def run(station):
        args = SimpleNamespace(state=None, checkout=None, since=None, skip_issue=[], station=station,
                               interval=90, settle=0, stale_hours=6, stale_every=3600, max_errors=2)
        return watch.watch(args, loaded)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, [None, "Designer"])) == [0, 0]
    default, designer = watch.default_state("o/r"), watch.default_state("o/r", "Designer")
    assert default != designer
    assert default.exists() and designer.exists()
    assert default.with_suffix(".lock") != designer.with_suffix(".lock")


def test_station_default_state_name_stays_inside_watch_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = watch.default_state("o/r", "../../Designer/训练")
    assert path.parent == tmp_path / "ding/watch"
    assert path != watch.default_state("o/r")
    assert watch.default_state("o/r", "Designer A") != watch.default_state("o/r", "Designer/A")


def test_station_watch_explicit_state_keeps_the_existing_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "load", lambda: config.Config(root=tmp_path, repo="o/r"))
    state = tmp_path / "shared.json"
    with state.with_suffix(".lock").open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert ding.main(["watch", "--for", "Designer", "--state", str(state)]) == 3
    assert not state.exists()


@pytest.mark.parametrize("arguments", [["--for"], ["--for", ""], ["--for", "   "]])
def test_station_watch_requires_a_nonempty_name(arguments):
    with pytest.raises(SystemExit) as error:
        ding.main(["watch", *arguments])
    assert error.value.code == 2


def test_station_watch_help_describes_its_scope(capsys):
    with pytest.raises(SystemExit):
        ding.main(["watch", "--help"])
    text = capsys.readouterr().out
    assert "--for NAME" in text
    assert "executor comments waiting on" in text
    assert "labels, PRs, stale/idle checks" in text


def test_station_watch_retries_post_settle_timeout_without_duplicate_output(station_settings, monkeypatch, tmp_path, capsys):
    import subprocess
    monkeypatch.setattr(config, "load", lambda: station_settings)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: None)
    calls = []
    def gh(path):
        assert "/issues/comments?" in path
        calls.append(path)
        if len(calls) == 2:
            raise subprocess.TimeoutExpired(["gh", "api", path], 60)
        return [comment(30, executor("状态：进行中\n在等：总设计\n关键数字：1\n下一步：等"))]
    monkeypatch.setattr(watch, "gh", gh)
    assert ding.main(["watch", "--for", "总设计", "--since", "2026-10-14T08:00:00Z",
                      "--interval", "0", "--settle", "0", "--max-errors", "2"]) == 0
    output = capsys.readouterr()
    assert len(calls) == 4
    assert "poll failed 1/2" in output.err
    assert output.out.count("#30 执行者 comment") == 1


@pytest.mark.parametrize("prefix", ["核查：", "总设计：", "总编排：", "用户："])
def test_station_watch_ignores_non_executor_roles_even_when_waiting_on_target(station_settings, monkeypatch, prefix):
    body = prefix + "报告\n在等：总设计\n关键数字：1\n下一步：等"
    monkeypatch.setattr(watch, "gh", lambda path: [comment(30, body)])
    assert watch.collect(station_settings, "o/r", "2026-10-14T08:00:00Z", "2026-10-14T08:00:00Z",
                         set(), set(), station="总设计") == []


@pytest.fixture
def routed_settings(tmp_path):
    return settings(tmp_path, RULING.replace('[status]\n', '[status]\npaused = "暂停"\n') + '''
signatures = ["总设计：", "总设计（训练）："]
[stations.aliases]
"总设计（训练）" = ["训练设计"]
''')


@pytest.mark.parametrize("body, training, general", [
    (executor("状态: 进行中\n在等: 核查和训练设计裁定…\n关键数字: 1\n下一步: 等"), True, False),
    (executor("状态: 进行中\n在等: 总设计(训练)\n关键数字: 1\n下一步: 等"), True, False),
    (executor("状态: 暂停\n在等: 用户/训练设计重新排期\n关键数字: 1\n下一步: 交总设计"), False, False),
    (executor("状态: 等设计\n在等: 裁定 X\n关键数字: 1\n下一步: 等"), False, True),
    (executor("状态: 进行中\n在等: 无\n关键数字: 1\n下一步: 交总设计决定"), False, True),
    ("核查：结论\n交总设计决定", False, True),
    ("总编排：请求\n@总设计 请裁定", False, True),
    ("用户请求\n总设计：请裁定", False, True),
    ("用户请求\n@训练设计 请裁定", True, False),
    ("总设计：自己的裁定\n交总设计", False, False),
    ("总设计(训练):自己的裁定\n@训练设计", False, False),
])
def test_issue32_mention_replays(routed_settings, body, training, general):
    assert watch.station_wakes(body, "总设计（训练）", routed_settings) is training
    assert watch.station_wakes(body, "总设计", routed_settings) is general


def test_alias_matching_is_shared_with_orchestrator_and_normalizes_width(routed_settings):
    body = executor("状态: 进行中\n在等: 训练设计师\n关键数字: 1\n下一步: 等")
    assert watch.waiting_for(body, "总设计（训练）", routed_settings)
    assert watch.waiting_for(body, "训练设计", routed_settings)
    assert not watch.waiting_for(body, "总设计", routed_settings)
    assert watch.executor_wakes(body, False, routed_settings)
    paused = body.replace("进行中", "暂停")
    assert watch.executor_wakes(paused, False, routed_settings)  # Same waiting-name test as in progress.
    assert not watch.executor_wakes(executor("状态: 暂停\n在等: 无"), False, routed_settings)


@pytest.mark.parametrize("text", [
    '[stations]\naliases = "wrong"',
    '[stations.aliases]\nDesigner = []',
    '[stations.aliases]\nDesigner = [1]',
    '[stations.aliases]\nDesigner = [""]',
    '[stations.aliases]\n"" = ["D"]',
    '[watch]\ndesign_answer_minutes = -1',
    '[watch]\ndesign_answer_minutes = true',
    '[watch]\ndesign_answer_minutes = inf',
])
def test_issue32_config_validation(tmp_path, text):
    with pytest.raises(ValueError, match="aliases|design_answer_minutes"):
        settings(tmp_path, text)


@pytest.mark.parametrize("kind", ["idle", "stale"])
@pytest.mark.parametrize("after", [False, True])
def test_status_label_only_suppresses_when_added_after_executor(idle_ticket, monkeypatch, kind, after):
    ticket = idle_ticket
    ticket.issues[0]["labels"] = [{"name": "status:blocked"}]
    original = watch.gh
    def gh(path):
        if "/events?" in path:
            return [{"event": "labeled", "label": {"name": "status:blocked"},
                     "created_at": "2026-10-28T11:00:00Z" if after else "2026-10-28T09:00:00Z"}]
        return original(path)
    monkeypatch.setattr(watch, "gh", gh)
    if kind == "idle":
        _, observed = check_idle(ticket)
        ticket.clock.moment += timedelta(hours=1)
        result, _ = check_idle(ticket, observed)
    else:
        ticket.clock.moment += timedelta(hours=6)
        result = watch.stale(ticket.config, "o/r", ticket.config.root, 6, set(), set())
    assert bool(result) is not after


@pytest.fixture
def unanswered_ticket(idle_ticket, routed_settings):
    ticket = idle_ticket
    ticket.config = routed_settings
    ticket.last.update(created_at="2026-10-28T11:29:00Z", updated_at="2026-10-28T11:29:00Z",
                       body=executor("状态: 等设计\n在等: 裁定 X\n关键数字: 1\n下一步: 等"))
    return ticket


def test_unanswered_design_reports_default_and_alias_targets_once(unanswered_ticket):
    ticket = unanswered_ticket
    found = watch.unanswered_design(ticket.config, "o/r", set(), set())
    assert len(found) == 1 and found[0][2] == 2601
    assert "waiting on 总设计 for 31 minutes with no designer reply" in found[0][1]
    assert watch.unanswered_design(ticket.config, "o/r", {2601}, set()) == []
    ticket.last["body"] = executor("状态: 进行中\n在等: 训练设计\n关键数字: 1\n下一步: 等")
    found = watch.unanswered_design(ticket.config, "o/r", set(), set())
    assert "waiting on 总设计（训练）" in found[0][1]


@pytest.mark.parametrize("reply", ["总设计：答复", "总设计（训练）：答复", "总设计(训练):答复", "训练设计：答复"])
def test_designer_reply_prevents_backstop(unanswered_ticket, reply):
    ticket = unanswered_ticket
    ticket.comments.append({"id": 2602, "body": reply, "created_at": "2026-10-28T11:40:00Z"})
    ticket.issues[0]["comments"] = 2
    assert watch.unanswered_design(ticket.config, "o/r", set(), set()) == []


def test_unanswered_design_does_not_report_paused_or_premature_request(unanswered_ticket):
    ticket = unanswered_ticket
    ticket.last["created_at"] = "2026-10-28T11:30:00Z"
    assert watch.unanswered_design(ticket.config, "o/r", set(), set()) == []
    ticket.last.update(created_at="2026-10-28T11:00:00Z", body=executor("状态: 暂停\n在等: 训练设计"))
    assert watch.unanswered_design(ticket.config, "o/r", set(), set()) == []


def test_verifier_handoff_wakes_station_but_remains_reference_for_orchestrator(routed_settings, monkeypatch):
    item = comment(32, "核查：结论\n交总设计决定")
    def gh(path):
        return [item] if "/issues/comments?" in path else []
    monkeypatch.setattr(watch, "gh", gh)
    args = (routed_settings, "o/r", "2026-10-14T08:00:00Z", "2026-10-14T08:00:00Z", set(), set())
    station = watch.collect(*args, station="总设计")
    orchestrator = watch.collect(*args)
    assert station[0][2] is True and "for reference" not in station[0][1]
    assert orchestrator[0][2] is False and "for reference" in orchestrator[0][1]


def test_backstop_persists_ids_and_does_not_repeat_after_restart(unanswered_ticket, monkeypatch, capsys):
    ticket = unanswered_ticket
    monkeypatch.setattr(config, "load", lambda: ticket.config)
    monkeypatch.setattr(watch, "collect", lambda *args: [])
    monkeypatch.setattr(watch, "stale", lambda *args: [])
    monkeypatch.setattr(watch, "idle", lambda *args: ([], {}))
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: None)
    path = ticket.config.root / "backstop.json"
    args = ["watch", "--state", str(path), "--since", "2026-10-28T10:00:00Z", "--settle", "0"]
    assert ding.main(args) == 0
    assert json.loads(path.read_text())["design_reported"] == [2601]
    monkeypatch.setattr(watch, "collect", lambda *args: [(watch.now(), "other activity", True)])
    assert ding.main(args) == 0
    output = capsys.readouterr().out
    assert output.count("no designer reply") == 1
    assert "other activity" in output


def test_backstop_runs_on_idle_cadence_instead_of_each_poll(unanswered_ticket, monkeypatch):
    ticket = unanswered_ticket
    ticket.last["created_at"] = "2026-10-28T12:00:00Z"
    monkeypatch.setattr(config, "load", lambda: ticket.config)
    monkeypatch.setattr(watch, "collect", lambda *args: [])
    monkeypatch.setattr(watch, "stale", lambda *args: [])
    monkeypatch.setattr(watch, "idle", lambda *args: ([], {}))
    start = ticket.clock.moment
    monkeypatch.setattr(watch.time, "monotonic", lambda: (ticket.clock.moment - start).total_seconds())
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: setattr(
        ticket.clock, "moment", ticket.clock.moment + timedelta(seconds=seconds)))
    check = watch.unanswered_design
    checks = []
    def traced(*args):
        checks.append((ticket.clock.moment - start).total_seconds())
        return check(*args)
    monkeypatch.setattr(watch, "unanswered_design", traced)
    assert ding.main(["watch", "--state", str(ticket.config.root / "cadence.json"),
                      "--interval", "60", "--settle", "0"]) == 0
    assert checks == [0, 900, 1800, 2700]


def test_for_watch_never_runs_design_backstop(routed_settings, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "load", lambda: routed_settings)
    monkeypatch.setattr(watch, "gh", lambda path: [comment(32, executor("状态: 进行中\n在等: 训练设计"))])
    def forbidden(*args):
        pytest.fail("--for must not consult periodic checks")
    for name in ("unanswered_design", "idle", "stale", "board_output", "tmux_panes"):
        monkeypatch.setattr(watch, name, forbidden)
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: None)
    assert ding.main(["watch", "--for", "总设计（训练）", "--state", str(tmp_path / "station.json"),
                      "--since", "2026-10-14T08:00:00Z", "--settle", "0"]) == 0


def test_backstop_can_be_disabled_and_does_not_depend_on_idle_enabled(unanswered_ticket, monkeypatch):
    from dataclasses import replace
    ticket = unanswered_ticket
    disabled = replace(ticket.config, watch=config.Watch(design_answer_minutes=0))
    original = watch.gh
    def forbidden(*args):
        pytest.fail("disabled backstop must not query GitHub")
    monkeypatch.setattr(watch, "gh", forbidden)
    assert watch.unanswered_design(disabled, "o/r", set(), set()) == []
    monkeypatch.setattr(watch, "gh", original)
    ticket.config = replace(ticket.config, watch=config.Watch(idle_minutes=0))
    monkeypatch.setattr(config, "load", lambda: ticket.config)
    monkeypatch.setattr(watch, "collect", lambda *args: [])
    monkeypatch.setattr(watch, "stale", lambda *args: [])
    assert ding.main(["watch", "--state", str(ticket.config.root / "idle-off.json")]) == 0


def test_backstop_reads_only_latest_page_when_it_contains_a_designer_reply(unanswered_ticket, monkeypatch):
    ticket = unanswered_ticket
    ticket.issues[0]["comments"] = 201
    original = watch.gh
    pages = []
    def gh(path):
        if "/comments?" in path:
            pages.append(path)
            assert path.endswith("page=3")
            return [{"id": 2602, "body": "总设计：已答复"}]
        return original(path)
    monkeypatch.setattr(watch, "gh", gh)
    assert watch.unanswered_design(ticket.config, "o/r", set(), set()) == []
    assert len(pages) == 1


@pytest.mark.parametrize("phase", ["idle", "stale"])
def test_label_history_failure_retries_without_advancing_state(idle_ticket, monkeypatch, phase):
    import subprocess
    ticket = idle_ticket
    ticket.clock.moment += timedelta(hours=6)
    ticket.issues[0]["labels"] = [{"name": "status:blocked"}]
    monkeypatch.setattr(config, "load", lambda: ticket.config)
    monkeypatch.setattr(watch, "collect", lambda *args: [])
    if phase == "idle":
        monkeypatch.setattr(watch, "stale", lambda *args: [])
    original = watch.gh
    def gh(path):
        if "/events?" in path:
            raise subprocess.TimeoutExpired(["gh", "api", path], 60)
        return original(path)
    monkeypatch.setattr(watch, "gh", gh)
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: None)
    path = ticket.config.root / "errors.json"
    assert ding.main(["watch", "--state", str(path), "--max-errors", "2", "--interval", "0"]) == 2
    assert not path.exists()


def test_backstop_api_failure_uses_bounded_retry_and_preserves_state(unanswered_ticket, monkeypatch):
    import subprocess
    ticket = unanswered_ticket
    monkeypatch.setattr(config, "load", lambda: ticket.config)
    monkeypatch.setattr(watch, "collect", lambda *args: [])
    monkeypatch.setattr(watch, "stale", lambda *args: [])
    monkeypatch.setattr(watch, "idle", lambda *args: ([], {}))
    original = watch.gh
    def gh(path):
        if "/comments?" in path:
            raise subprocess.TimeoutExpired(["gh", "api", path], 60)
        return original(path)
    monkeypatch.setattr(watch, "gh", gh)
    monkeypatch.setattr(watch.time, "sleep", lambda seconds: None)
    path = ticket.config.root / "backstop-errors.json"
    initial = json.dumps({"since": "2026-10-28T10:00:00Z", "seen": [], "stale": [], "design_reported": []})
    path.write_text(initial)
    assert ding.main(["watch", "--state", str(path), "--max-errors", "2", "--interval", "0"]) == 2
    assert path.read_text() == initial


def test_open_issue_pagination_reaches_older_tickets_without_duplicates(monkeypatch):
    paths = []
    def gh(path):
        paths.append(path)
        return [{"number": number} for number in range(1, 101)] if path.endswith("page=1") else [{"number": 100}, {"number": 318}]
    monkeypatch.setattr(watch, "gh", gh)
    assert [issue["number"] for issue in watch.open_issues("o/r")] == [*range(1, 101), 318]
    assert len(paths) == 2


def test_optional_config_defaults_are_backward_compatible(tmp_path):
    loaded = settings(tmp_path)
    assert loaded.stations.aliases == {}
    assert loaded.status.paused is None
    assert loaded.watch.design_answer_minutes == 30
    assert settings(tmp_path, '[watch]\ndesign_answer_minutes = 0').watch.design_answer_minutes == 0


def test_issue32_watch_help_covers_new_rules(capsys):
    with pytest.raises(SystemExit):
        ding.main(["watch", "--help"])
    help_text = " ".join(capsys.readouterr().out.split())
    assert "[stations.aliases]" in help_text
    assert "[status] paused" in help_text
    assert "design_answer_minutes" in help_text
    assert "awaiting-user" in help_text and "status labels older" in help_text


def test_backstop_reports_new_requests_after_a_previous_designer_reply(unanswered_ticket):
    ticket = unanswered_ticket
    older_reply = {"id": 2600, "body": "总设计：之前已答复", "created_at": "2026-10-28T11:00:00Z"}
    ticket.comments.insert(0, older_reply)
    ticket.issues[0]["comments"] = 2
    assert watch.unanswered_design(ticket.config, "o/r", set(), set())[0][2] == 2601
    assert watch.unanswered_design(ticket.config, "o/r", set(), {26}) == []


def test_label_history_uses_latest_addition_and_keeps_unknown_history_suppressed(idle_ticket, monkeypatch):
    ticket = idle_ticket
    issue = ticket.issues[0]
    issue["labels"] = [{"name": "status:blocked"}]
    events = [{"event": "labeled", "label": {"name": "status:blocked"}, "created_at": "2026-10-28T09:00:00Z"},
              {"event": "unlabeled", "label": {"name": "status:blocked"}, "created_at": "2026-10-28T09:30:00Z"},
              {"event": "labeled", "label": {"name": "status:blocked"}, "created_at": "2026-10-28T11:00:00Z"}]
    monkeypatch.setattr(watch, "gh", lambda path: events)
    assert watch.label_suppresses(ticket.config, "o/r", issue, ticket.last)
    events.clear()
    assert watch.label_suppresses(ticket.config, "o/r", issue, ticket.last)
