# 🛎️ ding

`ding` is the bell in the kitchen. It lets one agent session message another, and it wakes a session when the long job it started has finished.

It talks to Codex sessions through the Codex App Server. Claude Code sessions can use it to message Codex sessions, but `ding` cannot wake a Claude Code session.

- [Before you start](#before-you-start)
- [A quick tour](#a-quick-tour)
- [Choosing a session](#choosing-a-session)
- [See what everyone is doing: `board`](#see-what-everyone-is-doing-board)
- [Read a session: `read` and `status`](#read-a-session-read-and-status)
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
ding board                                           # sessions in this repo, their tickets and jobs
ding read --head '#42'                               # first paragraph of the latest reply on ticket #42
ding send '#42' --from orchestrator --message 'GPU 5 is free now; go ahead with stage 2.'
ding launch eval-42 -- python evaluate.py            # run in tmux; wake me when it exits
```

## Choosing a session

Every command that talks to one session (`read`, `status`, `send`, `run`) takes a target in any of these forms:

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
| `ISSUE` | The tickets this session owns or has signed comments on |
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

## Read a session: `read` and `status`

```bash
ding read '#42'            # the latest final reply, in full
ding read --head '#42'     # only its first paragraph
ding status '#42'          # the App Server's status for the session, as JSON
```

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
| `ding relay 42 --message TEXT [--comment TEXT]` | Orchestrator | Sends the message to ticket #42's executor and, only once delivery is confirmed, leaves it (or `--comment`) on the ticket, signed. `--to SESSION` picks the executor when the ticket has no owner line; `--no-comment` only sends. |
| `ding merge 57 a1b2c3d` | Orchestrator | Merges PR #57 only if its HEAD is still the reviewed `a1b2c3d` and it is not a draft, then confirms the merge and prints the merge commit. |
| `ding log --item TEXT [--pending TEXT]` | Orchestrator | Posts a signed, UTC-stamped entry to the orchestrator log ticket: one line per `--item`, then the matters waiting for the user. |
| `ding watch` | Orchestrator, in the background | Waits for GitHub activity the orchestrator must handle, prints it as one batch, and exits. See [While the user is away](../kitchen/modules/unattended.md). |
| `ding check-refs draft.md` | Verifier | Checks every `` `file:line` `` in a draft comment (a name written right next to it must appear on that line) and every `sha256`, against `origin/main` or `--root DIR`. Exits 1 when something is wrong. |

`ding watch` keeps its place in `~/.local/state/ding/watch/OWNER-REPO.json`, so rerunning the same command after handling a batch loses nothing. Only one watch runs per state file. It always skips the orchestrator log ticket; add `--skip-issue N` for others. Run `ding watch --help` for the timing options.

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

[status]
fields = ["Status", "Waiting on", "Key numbers", "Next"]   # first: the state; second: who it waits on
in_progress = "in progress"
waiting_on_design = "waiting on design"
blocked = "blocked"
done = "done"
none = "none"                        # a "Waiting on" value that names nobody

[labels]
prefix = "status:"
awaiting_user = "status:awaiting-user"

[log]
issue = 1                            # the pinned orchestrator log ticket
pending = "Waiting for the user"
```

`ding board --status` also uses `[status] fields`, after `--status-fields` and `DING_STATUS_FIELDS`. A misspelled key is an error, so a typo cannot silently fall back to a default.

## Files `ding` keeps

Everything lives under `${XDG_STATE_HOME:-~/.local/state}/ding/`, readable only by you.

| Path | Contents |
| --- | --- |
| `sent.jsonl` | Every `send`: what was sent, to whom, and the result |
| `runs.jsonl` | One `run_start` line per wrapped job, used by `board` to find jobs |
| `launch-records/<id>.jsonl` | Default record for each `launch`: `task_exit` and `notification` events |
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
