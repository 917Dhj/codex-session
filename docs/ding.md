# 🛎️ ding

`ding` is the bell in the kitchen. It creates Codex sessions, lets one agent session message another, and wakes a session when the long job it started has finished.

It talks to Codex sessions through the Codex App Server. Claude Code sessions can use it to message Codex sessions, but `ding` cannot wake a Claude Code session.

- [Before you start](#before-you-start)
- [A quick tour](#a-quick-tour)
- [Create a session: `new`](#create-a-session-new)
- [Choosing a session](#choosing-a-session)
- [Rename a session: `rename`](#rename-a-session-rename)
- [See what everyone is doing: `board`](#see-what-everyone-is-doing-board)
- [Read a session: `read`, `status`, and `info`](#read-a-session-read-status-and-info)
- [Send a message: `send`](#send-a-message-send)
- [Run a long job: `launch`](#run-a-long-job-launch)
- [Wrap a command yourself: `run`](#wrap-a-command-yourself-run)
- [Kitchen commands](#kitchen-commands)
- [Files `ding` keeps](#files-ding-keeps)
- [When a wake-up does not arrive](#when-a-wake-up-does-not-arrive)

## Before you start

You need:

- Linux or another POSIX system (`ding` uses Unix sockets and file locks), with `tmux` for `launch`
- Python 3.10 or later
- A Codex App Server running under your user. `ding` uses its experimental queue API, tested with `codex-cli 0.162.0`; other Codex versions may behave differently.
- The `gh` CLI, logged in, if you want to address sessions by GitHub issue

Install with pipx:

```bash
pipx install dkitchen
```

Or into a virtual environment:

```bash
python3 -m venv ~/.venvs/dkitchen
~/.venvs/dkitchen/bin/python -m pip install dkitchen
export PATH="$HOME/.venvs/dkitchen/bin:$PATH"
```

`ding` finds the App Server by looking for a single socket under `/tmp/codex-daemon-<your uid>`. If there are several, pick one with `--socket PATH` (for `run` and `launch`, put it before the `--`). For sessions on another machine, SSH there and run `ding` on that machine.

## A quick tour

```bash
ding new --cwd /work/repo --name 'Evaluation runner' --message 'Review the evaluation config.'
ding board                                           # sessions in this repo, their tickets and jobs
ding read --head '#42'                               # first paragraph of the latest reply on ticket #42
ding send '#42' --from orchestrator --message 'GPU 5 is free now; go ahead with stage 2.'
ding launch eval-42 -- python evaluate.py            # run in tmux; wake me when it exits
```

## Create a session: `new`

```bash
session_id=$(ding new --cwd /work/repo --name 'Evaluation runner' --message 'Review the evaluation config.')
printf '%s\n' 'Review the evaluation config.' | ding new --cwd /work/repo --name 'Evaluation runner' --message -
ding new --cwd /work/repo --name 'Evaluation runner' --message-file brief.md
ding new --cwd /work/worktree-57 --issue 57 --name 'Ablation' --message-file brief.md   # named 【#57】Ablation
```

`new` creates a saved, interactive session through the existing Codex Desktop SSH App Server. Run it on the same host and under the same user as that App Server. The session appears in Desktop and can be continued there.

`--cwd`, `--name`, and one of `--message` or `--message-file` are required. The working directory must exist. `ding` resolves its path and uses that directory, including a subdirectory or a directory outside Git. The title and first message must contain non-whitespace text; their text is sent unchanged. `--message -` reads the first message from stdin, and `--message-file PATH` reads it from a UTF-8 file. `--socket PATH` selects an existing App Server socket.

`--issue N` starts the session for ticket `#N` in the repository of `--cwd`. `--name` is still required and should be a short title; `ding` fills it into the `[sessions] name` template from [.dkitchen.toml](#dkitchentoml), by default `【#{issue}】{name}`, so `--issue 57 --name 'Ablation'` names the session `【#57】Ablation`. `ding` writes nothing to GitHub. Instead, the first message opens with one paragraph asking the new session to claim the ticket:

```text
You own ticket 917Dhj/my-repo#57. Before anything else, post a comment on it with the line `Codex owner: 019a2b3c-...`.
```

Until that comment is posted, `'#57'` does not resolve to the new session; pass its UUID instead.

A ticket has one executor. `--issue N` reads the ticket's `Codex owner:` lines first and refuses when any of those sessions is not archived. Give each run its own sub-ticket instead, or pass `--replace` when the ticket really changes hands (the new owner line then wins).

`--parent N` opens that sub-ticket for you: a new ticket titled `--title` (default: `--name`), whose body is the `[sessions] parent` line (default `Parent: #N`), attached to `#N` as a GitHub sub-issue. The new session then owns the sub-ticket exactly as with `--issue`. This is the one form of `new` that writes to GitHub. If the ticket opens but cannot be attached, no session starts, and the error names the new ticket.

```bash
ding new --cwd /work/worktree-57 --parent 57 --title '27B v9 run' --name '27B v9' --message-file brief.md
```

After the App Server accepts the first turn, `new` prints only the session UUID to stdout and exits with code 0. It does not wait for the reply. Use that UUID to continue or inspect the session:

```bash
ding status "$session_id"
ding read "$session_id"
ding send "$session_id" --message 'Also check the dataset paths.'
```

On failure, `new` exits with a nonzero code and prints the failed stage to stderr, along with the UUID if creation was confirmed. A lost or malformed creation reply can leave a session whose UUID is unknown. Inspect Desktop or `ding board` before retrying. If naming or the first turn fails, the created session remains available. `ding` never retries these requests or deletes the session.

## Choosing a session

Every command that talks to one session (`read`, `status`, `info`, `rename`, `send`, `run`) takes a target in any of these forms:

| Target | Example |
| --- | --- |
| Session UUID | `019a2b3c-...` |
| Exact session name, as shown by `board` | `'Evaluation runner'` |
| Issue in the current repo | `'#42'` or `--issue 42` |
| Issue in any repo | `917Dhj/my-repo#42` |
| Issue or PR link | `https://github.com/917Dhj/my-repo/issues/42` |

Quote `'#42'`, or your shell will treat it as a comment. Links to `/pull/42` work too; query strings and `#issuecomment-...` fragments are ignored.

**How an issue finds its session.** Executors sign their issue comments with a line naming their session:

```text
Codex owner: 019a2b3c-...      # "this ticket is mine"
Codex session: 019a2b3c-...    # "this comment came from me"
```

`ding` looks at the issue body and every comment:

1. If any `Codex owner:` line exists, the **latest** one wins. When a ticket changes hands, the new executor posts its own owner line.
2. Otherwise, all `Codex session:` lines must name the same session.
3. If they name different sessions, `ding` refuses and lists the candidates with their status and latest comment. Pick one by UUID or name.

When a name matches several sessions, or none, `ding` shows the candidates instead of guessing.

A Claude Code session working as the executor signs with `Claude session: <name>`. `ding watch` and `ding merge` count such a comment as an executor comment, but `ding` never delivers messages to it.

## Rename a session: `rename`

```bash
ding rename 019a2b3c-0000-4000-8000-000000000001 --name 'Evaluation runner'
ding rename 'Evaluation runner' --name 'Evaluation complete'
ding rename --issue 42 --name 'Ticket 42 executor'
ding rename https://github.com/917Dhj/my-repo/issues/42 --name 'Ticket 42 executor' --socket /path/to/app-server.sock
```

The new name must contain non-whitespace text. `ding` sends the supplied name unchanged, including Unicode and leading or trailing spaces. The session keeps its UUID and history, and no model turn starts. On confirmed success, stdout contains only the session UUID followed by a newline.

Targets use the same rules as other session commands. Ambiguous names or issues are refused before renaming. `--socket PATH` selects an existing App Server socket as usual.

If the connection fails before confirmation, the name may already have changed. `ding` reports an error and does not retry. Inspect the current name with `ding board --all` before deciding whether to try again.

## See what everyone is doing: `board`

```bash
ding board
```

```text
NAME              CWD                     STATUS  ISSUE  HEAD                          RUN
eval-runner       /work/repo              active  #42    Stage 1 finished.             running
data-cleanup      /work/repo/.wt/issue57  idle    #57    Merged the loader fix.        exited(0)
```

| Column | What it shows |
| --- | --- |
| `NAME` | Session name, or its UUID if it has none |
| `CWD` | The session's working directory |
| `STATUS` | `active` (working), `idle`, `notLoaded`, or `unavailable` if the App Server cannot be reached |
| `ISSUE` | The ticket parsed from `[sessions] name`, otherwise tickets this session owns or has signed comments on |
| `HEAD` | The first paragraph of its latest final reply |
| `RUN` | The state of its most recent wrapped job ([states below](#job-states)) |

By default the board shows loaded sessions and sessions with registered jobs in the current Git repo and its worktrees (or the current directory outside Git). Options:

- `--all` shows every non-archived session in every directory. It is slower.
- `--status` replaces `HEAD` with the status block from the session's latest issue comment. Otherwise it falls back to the final reply.

### Status blocks

A status block is the first few lines of an executor's comment, one field per line, in a fixed order:

```text
Status: running
Waiting on: none
Key numbers: 412/500 samples
Next: finish stage 1, then post results
```

`board --status` shows it only when every field is present, in order, with a non-empty value. The default fields are `Status`, `Waiting on`, `Key numbers`, and `Next`. To use your own, set them once for your environment, or per command:

```bash
export DING_STATUS_FIELDS='状态,在等,关键数字,下一步'
ding board --status --status-fields 'State,Blocked by,Next'
```

`board` only reads. It never restores a session or retries a message.

### Jobs nobody will be woken for

On Linux, `board` ends with a second table of long-running processes on this machine that were started without `ding`:

```text
UNWRAPPED_PID  CPU%   AGE_SECONDS  TMUX_SESSION  TMUX_WINDOW  COMMAND
48211          97.3   5420         exp           0            python train.py --config big.yaml
```

When a job finishes, nobody gets woken for it. Restart it with `ding launch`, or tell the session that owns it to check on it.

What counts:

- **Inside tmux**, each pane contributes its main workload once it is older than 10 minutes, whatever its CPU use. Idle shells are skipped.
- **Outside tmux**, a process must also average at least 10% of one CPU core over its lifetime.
- Tune both with `--min-age SECONDS` and `--min-cpu PERCENT` (values over 100 are fine for multicore jobs).

Editors, language servers, Codex, Claude, tmux itself, and SSH tunnels are ignored, as is the tmux session named `ssh_tunnel`.

This is a heuristic. Outside tmux it can miss GPU jobs that use little CPU, and it never looks at GPUs or other machines.

## Read a session: `read`, `status`, and `info`

```bash
ding read '#42'            # the latest final reply, in full
ding read --head '#42'     # only its first paragraph
ding status '#42'          # the App Server's status for the session, as JSON
ding info '#42'            # the session's ID, name, working directory, and status, as JSON
```

`info` prints one line of JSON, for example after `new` or `rename` to check the name, or to see which session a UUID or ticket points to:

```json
{"id": "019a2b3c-...", "name": "【#42】Ablation", "cwd": "/work/worktree-42", "status": {"type": "idle", "activeFlags": []}}
```

`name` is `null` for a session that has never been named. `info` only reads: it starts no model turn and writes nothing to GitHub.

## Send a message: `send`

```bash
ding send '#42' --from orchestrator --message 'Stage 2 is approved within the ticket budget.'
ding send '#42' --from orchestrator --message-file note.md
printf '%s\n' 'Short note' | ding send '#42' --message -
```

`--from NAME` adds a first line `[from NAME]`, so the receiver knows who is talking. Message files are read as UTF-8.

What happens depends on what the receiver is doing:

| Receiver | Result | Exit code | Meaning |
| --- | --- | --- | --- |
| Idle (or not loaded) | `started` | 0 | `ding` restored the session if needed, and the message began running within 60 seconds. |
| Busy | `queued_behind_active` | 0 | The message waits in the session's queue and runs after the current turn ends normally. |
| Unclear | `queued` or `unknown` | non-zero | Delivery could not be confirmed. **Do not resend yet.** |
| Error | `failed` | non-zero | Nothing was delivered. |

Neither `started` nor `queued_behind_active` means the work is done. Look for the receiver's reply on the ticket.

When a new decision replaces what the receiver is busy with, three options change how a busy receiver gets it:

- `--steer` inserts the message into the receiver's current turn (App Server `turn/steer`), so it is read at the model's next step instead of after the turn ends. The result is `steered` (exit 0). Long shell commands are polled every few seconds, so the message usually lands within seconds even mid-command; the receiver may then abandon that command. If the turn ends just before the insertion, the message is queued as usual.
- `--supersede` (needs `--from`) first deletes this sender's earlier messages to the same session that are still waiting in its queue, found through the `queued_submission_id`s in the send log. Each deletion is logged as a `superseded` event. Messages from other senders and the user's own queue are left alone.
- `--interrupt` first stops the current turn, waits for the session to become idle, then starts the message as a new turn. Use it only when the current work is known to be obsolete.

A queued message only runs after a normal turn end. If the receiver is waiting for user input, it runs once that turn completes; if the turn is interrupted or paused for budget, the message stays in the queue.

**Before resending,** check the send log and the receiver's history. Each `send` gets a new message ID, so a blind resend can make the receiver do the work twice. `ding` itself never retries a delivery.

Every send is written to `~/.local/state/ding/sent.jsonl` before delivery, with its ID, time, sender, target, and full text. The result is appended when known. Issue targets are logged as `OWNER/REPO#42`.

## Run a long job: `launch`

From inside a Codex session:

```bash
ding launch train -- python train.py --config big.yaml
```

```text
ding: created ding-train/train; record: /home/you/.local/state/ding/launch-records/<id>.jsonl
```

This:

1. creates a new tmux session `ding-train` in your current directory;
2. runs the command there, wrapped by [`ding run`](#wrap-a-command-yourself-run);
3. when the command exits, wakes the Codex session that launched it with `Task train exited; inspect <record path>`.

Then end your turn. The wake-up will come.

Options:

- `--message TEXT`, `--message-file PATH`, or `--message -` set your own wake-up text. `--from NAME` works as in `send`.
- `--progress PATH` names the task's `progress.json` (schema `progress-v1`: `current` stage with `done`/`total`, `rate_per_hour`, `eta`, `updated`). `ding board` then adds a PROGRESS column, such as `answer 120/500 34/h ETA 10-20 05:10`, and marks a running task whose file has not been updated for 10 minutes `STALE 14m`. `ding` only reads the file; the task writes it.
- `--record PATH` writes the job's record somewhere you choose.
- `--socket PATH` picks the App Server.

Things to know:

- **Names** are 1 to 64 letters, digits, `_`, or `-`, starting with a letter or digit. A name already in use is an error; `ding` never reuses or replaces a tmux session.
- **The environment** inside tmux carries only `PATH`, `HOME`, `XDG_STATE_HOME`, `PYTHONPATH`, and the session ID from your shell. Anything else may come from tmux's own environment, so pass what the job needs explicitly:

  ```bash
  ding launch train -- env CUDA_VISIBLE_DEVICES=0 OMP_NUM_THREADS=8 python train.py
  ```

- **The pane stays open** after the job exits, so you can read the output. Watch it with `tmux attach -t ding-train`, and close it when you are done with `tmux kill-session -t =ding-train`.
- **A successful launch** means the tmux session was created, not that the job finished.
- **If `launch` times out,** creation is unconfirmed. Check `tmux ls` and the record before trying again.
- `launch` needs `CODEX_THREAD_ID`, which Codex sets in its shells. From anywhere else, use `run`.

### Optional repository preflight

Configure a command in the repository root's `.dkitchen.toml` to check a launch before its tmux session is created:

```toml
[launch]
preflight = ["python", "/abs/path/check.py"]
preflight_timeout = 300  # seconds; default 300
```

`preflight` is an argv list, executed without a shell in the launch working directory. Its stdout and stderr pass through. It inherits the caller's environment plus:

| Variable | Value |
| --- | --- |
| `DING_LAUNCH_NAME` | The launch name |
| `DING_LAUNCH_COMMAND` | The task argv encoded as JSON |
| `DING_LAUNCH_CWD` | The absolute launch working directory |
| `DING_PREFLIGHT_RECEIPT` | An absolute `<launch-id>.preflight.json` path beside the launch record |

The hook may write a receipt at that path. `ding` treats its bytes as opaque and prints its path and SHA-256 when it exists. For example, `check.py` can use:

```python
import os
from pathlib import Path

# Exit nonzero when a repository check fails.
Path(os.environ["DING_PREFLIGHT_RECEIPT"]).write_text('{"ok": true}\n')
```

Exit 0 permits the launch. A nonzero exit or timeout refuses it without creating a tmux session, and reports `preflight refused (exit N); receipt: <path>`. Timeouts are recorded with exit code 124 and `timed_out: true`; a missing executable is recorded with exit code 127.

The launch JSONL contains a `preflight` event with the hook's `argv`, `exit_code`, `duration_seconds`, `receipt_path`, `receipt_sha256` (null if absent), and `launch_id`. This event remains available even when the hook refuses the launch. `ding board` continues to read the job's exit and notification events from the same record.

To bypass the check, supply a nonempty reason:

```bash
ding launch train --skip-preflight "Approved maintenance exception" -- python train.py
```

The `preflight` event records `skip_reason` and a null exit code. Without a configured hook or an override, launch behavior is unchanged.

## Wrap a command yourself: `run`

`run` is what `launch` uses inside tmux. Use it directly when you want to manage the terminal yourself, or to wake a session other than your own:

```bash
ding run '#42' --message 'Training finished; see exit.jsonl' --record "$PWD/exit.jsonl" -- python train.py
```

Keep the job in the foreground of `run`. When it exits, `run`:

1. writes a `task_exit` event to the record, with the exit code;
2. delivers the message to the target exactly as `send` does;
3. writes a `notification` event with the delivery result;
4. exits with the job's own exit code, whether or not the notification worked.

It never reruns the job. The wrapped job and everything it starts see `DING_RUN_ID`, the ID of this run.

### Job states

`board` reads these records to fill its `RUN` column:

| `RUN` | Meaning |
| --- | --- |
| `running` | The wrapper is alive and the job has not exited. |
| `exited(CODE)` | The job exited with `CODE`, and the wake-up started, was queued behind active work, or is still being sent. |
| `notify_failed` | The job exited, but the wake-up failed or could not be confirmed. Tell the session yourself. |
| `lost` | The wrapper died before the job exited, for example after a reboot or `kill -9`. |
| `unknown` | The record could not be read. `board` prints a warning. |

## Kitchen commands

These commands carry out the kitchen's routine steps, as the house rules in [kitchen/](../kitchen/) describe them. They read the repo's vocabulary from [`.dkitchen.toml`](#dkitchentoml).

| Command | Who runs it | What it does |
| --- | --- | --- |
| `ding relay 42 --message TEXT [--comment TEXT]` | Orchestrator | Sends the message to ticket #42's executor and, only once delivery is confirmed, leaves it (or `--comment`) on the ticket, signed. The message opens with the `[relay] superseding` line and is sent with `--steer --supersede`: it enters the executor's current turn and deletes the orchestrator's relays still queued for it. `--queue` queues it plainly instead; `--interrupt` also stops the current turn first. `--to SESSION` picks the executor when the ticket has no owner line; `--no-comment` only sends. |
| `ding merge 57 a1b2c3d [--ticket 42]` | Orchestrator | Merges PR #57 only if its HEAD is still the reviewed `a1b2c3d`, it is not a draft, and the ticket's executor is waiting on the orchestrator or lists PR #57 as pending merge (see below). Then it confirms the merge and prints the merge commit. |
| `ding log --item TEXT [--pending TEXT]` | Orchestrator | Posts a signed, UTC-stamped entry to the orchestrator log ticket: one line per `--item`, then the matters waiting for the user. |
| `ding night open\|show\|close` | Orchestrator opens/closes; any station reads | Records night orders on `[log] issue`, shows the open orders and their link, or closes them. Orders expire at `--until`. Calls no model. |
| `ding watch [--for NAME]` | A station, in the background | Waits for GitHub activity, prints it as one batch, and exits. `--for NAME` selects requests and explicit handoffs to that station. See [While the user is away](../kitchen/modules/unattended.md). |
| `ding check-refs draft.md` | Verifier | Checks every `file:line` in a draft comment, in backticks or not (a name written right next to it must appear on that line), and every `sha256`, against `origin/main` or `--root DIR`. It also asks GitHub that each `#N` ticket, `PR#N`, issue or PR link, and comment link exists, that a `PR#N` or `/pull/` link is a PR, and that a comment link's comment belongs to the ticket in the link (`--repo` for bare `#N`, `--offline` to skip). It prints how many references of each kind it checked, and lists every wrong one and every one it could not check, also in its JSON line. Exits 1 when something is wrong. Give several drafts to check them one by one: each line then names its file, the JSON lines carry a `draft` key, and the exit status is nonzero (1 for a wrong reference, 2 for an unreadable file) if any draft fails. |
| `ding rule 42 draft.md [--reply-to LINK_OR_ID] [--notify SESSION] [--force]`<br>`ding rule 42 body.md --edit-body --base base.md` | Designer | Posts the designer's ruling on ticket #42. Nothing is sent unless the draft's line 1 starts with one of `[rule] signatures`, line 2 with the `[rule] type_label` and one of `[rule] types`, and `check-refs` finds nothing wrong (it takes `--ref`, `--root`, `--tolerance`, `--repo`, `--offline`). With `--reply-to`, it stops and lists the link and first line of every comment that came after the one answered; `--force` posts anyway. Without it, it shows the latest comment's time and first line as a hint. It posts with `gh issue comment` and prints only the comment URL on stdout. `--notify SESSION` then sends `[rule] notify` plus the draft's complete stripped first nonempty line to a Codex session with `ding send`; if that fails, the error says so and the comment stays. `ding` cannot message Claude Code sessions, so `--notify` refuses a `uds:` or `local_` address before posting; tell those with `SendMessage`, using the printed URL. `--edit-body` replaces the ticket body instead: it stops unless the current body equals the `--base` file (line endings and trailing newlines aside), edits with `gh issue edit`, then reads the body back and fails unless it matches the draft byte for byte, ignoring trailing newlines. The line 1 and 2 check applies to comments only. |

**Before merging, `ding merge` reads the executor's latest status block.** The ticket is `--ticket N`. Without it, `ding` takes the one ticket mentioned in the PR body (as `#N` or a link) that has executor comments, and refuses when there are none or several. Executor comments are those signed `Codex session:` or `Claude session:`; when both kinds are on the ticket, the latest comment of either counts. It prints that ticket's latest executor status block, and merges only when its second field (*Waiting on*) names the orchestrator, or its optional `[status] pending_merge` field (default *Pending merge*, for example `Pending merge: PR#57, PR#58`) lists this PR. The executor lists a PR there once it needs nothing more on it, so the merge does not wait for the executor to come back from its next task. An executor that has since said it is still changing the PR, or that waits on nobody, blocks the merge, even if the review already passed. Use `--no-ticket` for a PR that belongs to no ticket. There is no override: when the executor cannot answer, the user merges by hand.

`ding watch` keeps its place in `~/.local/state/ding/watch/OWNER-REPO.json`, so rerunning the same command after handling a batch loses nothing. Only one watch runs per state file. It always skips the orchestrator log ticket; add `--skip-issue N` for others. On an independent schedule, it also checks for executors still marked in progress whose attributed board sessions are all `idle` or `notLoaded`, with no running `ding run` or live `issue<N>` tmux job. After `[watch] idle_minutes` (default 45), it reports the ticket once per executor comment. The check interval is `idle_minutes / 3`, capped at 15 minutes; a longer `--interval` is shortened to meet that deadline. `--stale-every` controls only the existing stale check. Awaiting-user tickets, `--skip-issue`, and the log ticket are excluded; another `status:` label suppresses idle/stale checks only when its latest addition is after the executor comment; a ticket without an attributed session uses only the existing stale check.

`ding board` does not expose session update times. Watch therefore saves when it first observes all the idle conditions, and waits from the later of that observation and the executor comment's update. This observation survives restarts. A new executor comment or a failed idle condition resets the period. Setting `[watch] idle_minutes = 0` disables this wake; the stale check remains enabled.

Watch calls `ding board --no-issue-lookup`: only session names matching the configured `[sessions] name` template are attributed to tickets. Other session names remain unattributed, and watch never requests the whole-repository ownership scan. Plain `ding board` retains GitHub owner/candidate attribution for unmatched names. Nonzero board exits, failed GitHub calls, and timeouts, including a re-collect after `--settle`, use the same bounded retry loop. Only `--max-errors` consecutive failed polls stop it with exit 2; its reporting cursor is not advanced. A failed re-collect prints no partial batch and retries from the same cursor.

A designer or another station can run its own watch:

```bash
ding watch --for Designer
ding watch --for '总设计（训练）'
```

Use the station's configured name. This mode wakes for executor comments whose waiting-on field names the station, including on awaiting-user tickets, or whose Next field contains `交NAME`. An executor marked `[status] waiting_on_design` with no designer named in its waiting field wakes the default `[stations] designer`.

Verifier, orchestrator, and user comments wake the station only when they contain `@NAME`, `交NAME`, or a line starting with `NAME` followed by the configured separator. Designer comments, including the station's own rulings, never trigger a station watch. A configured `[status] paused` executor never triggers `--for`; the orchestrator treats that status like in progress.

A station watch does not query labels, new PRs, stale/idle checks, the unanswered-design backstop, board, or tmux. `--settle`, `--skip-issue`, the log-ticket exclusion, and bounded error retries still apply.

Names are compared after width normalization, so `总设计(训练)` matches `总设计（训练）`. Matching includes `[stations.aliases]` and excludes occurrences inside longer configured names or aliases; trailing signature separators are removed before comparison. For example, when `总设计（训练）：` is configured, `--for 总设计` does not wake for a comment waiting only on `总设计（训练）`; `--for '总设计（训练）'` does.

Aliases are optional and apply to both station watches and the orchestrator's waiting-field test:

```toml
[stations.aliases]
"总设计（训练）" = ["训练设计"]

[status]
paused = "暂停"

[watch]
design_answer_minutes = 30
```

Each alias value must be a nonempty list of nonempty names. Thus `训练设计` also matches `训练设计师`, while a general `总设计` watch does not match a training-designer request.

The orchestrator's watch checks each ticket's latest executor comment for an unanswered request on the idle-check cadence. After `[watch] design_answer_minutes` (default 30), it reports that comment if its state is waiting on design or its waiting field names a designer, unless a later designer comment exists. The line names the designer and links to the executor comment. Configured paused requests are excluded. Reported IDs are saved as `design_reported`, so restarting does not repeat them. Set this key to 0 to disable the backstop; it remains enabled when only idle wakes are disabled. Existing open requests can be reported even when older than `--since`.

The default state file is `OWNER-REPO.for-NAME.json` under the watch state directory, with `NAME` percent-encoded for filenames. It is separate from `OWNER-REPO.json`, so station and orchestrator watches can run concurrently. An explicit `--state` overrides this choice; the same file still permits only one watch. Omitting `--for` selects the orchestrator watch and retains its default state path.

Terminal comment hints keep the first 100 characters and add `…` when shortened. Watch summaries keep the first 160 characters and PR titles the first 80, also adding `…` when shortened. Notification text, including `{first_line}`, carries the complete stripped first nonempty line.

Run `ding watch --help` for the timing options.

### Night orders: `night open`, `night show`, `night close`

Night orders live on `[log] issue`. The orchestrator collects the user's approvals with `/goodnight` before the user leaves, and closes and reports them with `/goodmorning` when the user returns. Both skills are user-invoked and run in the orchestrator session; their station names and rules come from the current repo.

```bash
ding night open --until 2026-10-31T08:00:00Z --body-file orders.md
ding night show
ding night close
```

`open` takes a nonempty UTF-8 file containing the user's reply verbatim and the numbered list it answers. The CLI adds the first line, `[night] heading` followed by `, until <time>)`; put only the reply and list in the file. `--until` must be a future ISO time with an explicit timezone; it is recorded in UTC. Open orders block another `open`; `--replace` posts a closing line first, then the new orders. The posted comment URLs are printed. A failed close prevents the replacement; if posting the replacement fails after closing, the old orders remain closed.

`show` prints the open orders unchanged, followed by their comment link. Orders are open only until their end time and while no later closing line exists. With no open orders, it prints exactly `no night orders open` and exits 0. Every station runs it before writing "waiting on the user", then acts only on orders that cover the case. The command reads only the log ticket's comments, newest first in pages of 100, stopping at the latest opening or closing marker. Usually this takes one request; older pages are read only when the latest page contains no marker. No local state is kept, and no model is called.

`close` posts `[night] closed` followed by ` at <time UTC>` for the open orders. If they have expired or are already closed, it prints `no night orders open` and posts nothing. GitHub read failures return nonzero rather than treating the orders as absent. `[night] heading` and `closed` must be nonempty single-line strings; without `[night]`, the English defaults below apply. Changing these words also changes which existing markers can be recognized.

### .dkitchen.toml

Put this file at the repo root to change the kitchen's words. Every key is optional; these are the defaults:

```toml
repo = "owner/name"                  # default: the repository gh sees here

[stations]
orchestrator = "Orchestrator"
designer = "Designer"
verifier = "Verifier"
executor = "Executor"
user = "User"
separator = ": "                     # written after a station name to sign a comment

# [stations.aliases]
# "Training designer" = ["Training"]  # optional equivalent names

[status]
fields = ["Status", "Waiting on", "Key numbers", "Next"]   # first: the state; second: who it waits on
in_progress = "in progress"
waiting_on_design = "waiting on design"
# paused = "paused"                 # optional; no paused word is assumed when absent
blocked = "blocked"
done = "done"
none = "none"                        # a "Waiting on" value that names nobody
pending_merge = "Pending merge"      # optional field: PRs the executor is ready to have merged

[labels]
prefix = "status:"
awaiting_user = "status:awaiting-user"

[log]
issue = 1                            # the pinned orchestrator log ticket
pending = "Waiting for the user"

[night]
heading = "Orchestrator: user decision (night orders"   # followed by ", until <time>)"
closed = "Orchestrator: night orders closed"

[sessions]
name = "【#{issue}】{name}"           # session name for `ding new --issue N --name NAME`
parent = "Parent: #{parent}"         # first line of a sub-ticket from `ding new --parent N`

[relay]
superseding = "New decision; it replaces earlier relays not yet carried out."   # first line of a `ding relay` message

[rule]                               # `ding rule`: a ruling is the designer's signature, then this line
# signatures = ["Designer: ", "Training designer: "]  # default: designer + separator
type_label = "Type"                  # line 2 reads `<type_label><separator><one of types>`
types = ["actionable", "proposal, awaiting user", "record"]
notify = "#{ticket} answered, see the ticket {url}"   # the `--notify` message; `{first_line}` is also available

[watch]
idle_minutes = 45                    # 0 disables idle-executor wakes
design_answer_minutes = 30           # 0 disables unanswered-design reminders
```

`[sessions] name` may use `{issue}`, `{name}` (the `--name` text), and `{repo}` (`OWNER/REPO`), with an optional format spec such as `{issue:04d}`. Any other field is an error when the file is read. The `--name` text is filled in as is, so braces in it are kept. `ding new` without `--issue` and `ding rename` use `--name` unchanged.

`[watch] idle_minutes` and `design_answer_minutes` must be nonnegative finite numbers of minutes; unknown `[watch]` keys are errors.

`[rule] signatures` lists the full first-line prefixes accepted as designer signatures by both `ding rule` and `ding watch`. It must be a nonempty list of nonempty strings. `ding rule` matches prefixes verbatim, including spaces; `ding watch` also recognizes width-normalized forms and configured aliases. When omitted, it defaults to `[stations] designer` plus `[stations] separator`.

For example, a repository with two designers can set:

```toml
[rule]
signatures = ["总设计：", "总设计（训练）："]
```

`ding watch` recognizes either prefix as the designer; verifier and orchestrator matching is unchanged. With no list configured, it requires the full default signature, so a line beginning with only the designer's name no longer counts as a designer comment.

`ding board --status` also uses `[status] fields`, after `--status-fields` and `DING_STATUS_FIELDS`. A misspelled key is an error, so a typo cannot silently fall back to a default.

## Files `ding` keeps

Everything lives under `${XDG_STATE_HOME:-~/.local/state}/ding/`, readable only by you.

| Path | Contents |
| --- | --- |
| `sent.jsonl` | Every `send`: what was sent, to whom, and the result |
| `runs.jsonl` | One `run_start` line per wrapped job, used by `board` to find jobs |
| `launch-records/<id>.jsonl` | Default record for each `launch`: optional `preflight`, then `task_exit` and `notification` events |
| `launch-records/<id>.message.txt` | The wake-up message for that launch |
| `watch/OWNER-REPO.json` | Where `ding watch` left off |
| `run-locks/<run id>.lock` | Held while a wrapper is alive; this is how `board` tells `running` from `lost` |

A `runs.jsonl` line looks like this:

```json
{"event":"run_start","run_id":"…","thread_id":"…","record":"/abs/exit.jsonl","pid":1234,"started_ns":1791799200000000000,"cwd":"/abs/launch/dir"}
```

## When a wake-up does not arrive

A job wakes its session exactly once, after it exits. You will not get that wake-up if:

- the wrapper itself was killed with `kill -9`, or the machine rebooted (`board` shows `lost`);
- the job is stuck and never exits (`board` still shows `running`);
- delivery failed (`board` shows `notify_failed`; the record says why).

Don't schedule follow-up messages in advance to cover these cases. Check `ding board`, read the record, and decide from there.
