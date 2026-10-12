# dkitchen

`ding` passes messages between agent sessions and wakes a session when a task finishes. The current backend is Codex App Server.

This is an independent project, not an official OpenAI tool. The package is `dkitchen`, the Python module is `ding`, and the command is `ding`. The transitional `codex-session` command uses the same entry point and will be removed in a later release.

`ding` may conflict with other CLI commands of the same name, including [liviu-/ding](https://github.com/liviu-/ding) and [dingdone](https://pypi.org/project/dingdone/).

## Installation

Requires Python 3.10 or later and a POSIX system. Tested on Linux with `codex-cli 0.162.0`. The CLI uses Unix sockets and `fcntl` locks.

```bash
pipx install git+https://github.com/917Dhj/dkitchen.git
pipx ensurepath
```

For an existing pipx installation, run `pipx uninstall codex-session` before installing the renamed package.

Without pipx, install into a virtual environment and add its bin directory to your shell's `PATH`.

```bash
python3 -m venv ~/.local/share/dkitchen
~/.local/share/dkitchen/bin/python -m pip install git+https://github.com/917Dhj/dkitchen.git
export PATH="$HOME/.local/share/dkitchen/bin:$PATH"
```

A compatible Codex App Server must already run under the same user. Notifications use the App Server `thread/resume`, `thread/queue/add`, and `thread/queue/start` interfaces available in the tested version. The `codex` executable is not required in the calling shell or tmux `PATH`. These interfaces use an experimental API and may change with Codex versions.

## Sessions and messages

```bash
ding board
ding board --all
ding board --status
ding board --min-age 600 --min-cpu 10
ding read UUID
ding read --head 'Exact session name'
ding status '#186'
ding status '917Dhj/Human-centric-agents#186'
ding status https://github.com/917Dhj/Human-centric-agents/issues/186
ding send --issue 186 --message-file message.txt --from coordinator
printf '%s\n' 'Your message' | ding send UUID --message -
ding run UUID --message 'Your message' --record exit.jsonl -- python task.py
ding launch training -- python task.py
```

`board` prints tab-separated columns `NAME`, `CWD`, `STATUS`, `ISSUE`, `HEAD`, and `RUN`. `HEAD` contains the first paragraph of the latest final reply. Sessions without a final reply have a blank summary. `RUN` contains the most recently registered run's state, or is blank for sessions without a registered run.

By default, `board` includes loaded sessions with `idle` or `active` status and registered run targets, including unloaded sessions. The scope is the current Git repository and its linked worktrees, or the current directory outside Git. Registered targets use the session's directory when available and the run's launch directory otherwise. `--all` includes all non-archived App Server sessions and registered targets in every directory. If the App Server is unavailable, registered targets still appear with their UUID, launch directory, and `unavailable` session status. The board only reads sessions and records. It never restores a session or retries a notification.

With `gh` available, the board looks up issues in the current repository. An issue with a `Codex owner: UUID` line belongs only to its latest owner across the body and comments. Other issues belong to every valid `Codex session: UUID` reference. An invalid latest owner prints a warning and omits that issue. A failed lookup prints a warning to stderr and leaves `ISSUE` blank.

`board --status` replaces `HEAD` with the latest matching executor comment's opening four lines, flattened into one cell. The lines must have nonempty values for `状态:`, `在等:`, `关键数字:`, and `下一步:` in that order. A comment's last `Codex owner` line takes precedence over its `Codex session` lines. Comments match canonical session UUIDs, and the newest comment across the session's mapped issues wins. If that comment has no complete status block, `HEAD` uses the final reply. Failed comment lookups or invalid matching timestamps also use the final reply with a warning. Each distinct visible issue gets one `gh issue view NUMBER --repo OWNER/REPO --json comments` call, with at most four calls at once. Ordinary `board` does not fetch individual issue comments.

On Linux, every `board` invocation also checks local processes owned by the current effective user. When it finds unwrapped work, it appends a separate table with `UNWRAPPED_PID`, `CPU%`, `AGE_SECONDS`, `TMUX_SESSION`, `TMUX_WINDOW`, and `COMMAND`. This check covers the local host regardless of the session table's repository scope or `--all`. Findings remain visible if the session lookup fails; the command still returns that lookup's failure code.

The default thresholds are 600 seconds of process age and 10 percent lifetime CPU usage per core. `--min-age SECONDS` and `--min-cpu PERCENT` accept finite nonnegative values, including CPU thresholds over 100 for multicore jobs. The check uses one `/proc` snapshot without a sampling delay. A process is wrapped when it or a verifiable ancestor is an actual `ding run` or `codex-session run` invocation, including older wrappers without an environment marker. A marker alone does not hide a process after its wrapper dies. Broken or cyclic ancestry, inaccessible processes, zombies, and known infrastructure processes are omitted. Infrastructure includes tmux servers, SSH tunnels, Codex, Claude, common editors, and language services; their workload children are checked independently. The entire `ssh_tunnel` tmux session is excluded. tmux context comes from the ancestor pane PID or a mapped `TMUX_PANE`; it is blank when unavailable. An unavailable `/proc` audit prints a warning.

This is a conservative CPU heuristic, not a complete task inventory. It can miss GPU jobs that use little CPU, short jobs, low-utilization jobs, and processes whose ancestry cannot be read. It does not query GPUs or remote hosts.

Every target (`send`, `run`, `read`, and `status`) accepts a UUID, an exact name displayed by `board`, a quoted `'#186'`, `--issue 186`, `OWNER/REPO#186`, or `https://github.com/OWNER/REPO/issues/186`. GitHub `/pull/186` links are treated as issues, and a trailing slash, query strings such as `?notification_referrer_id=...`, and fragments such as `#issuecomment-123` are ignored. Qualified issue numbers and links use their explicit repository regardless of the current directory; `'#186'` and `--issue 186` use the current directory's repository. Names are matched across all non-archived sessions. Missing or ambiguous names report candidates. Issue targets use the latest `Codex owner: UUID` line across the body and comments. Without an owner, all `Codex session: UUID` references must identify one session; repeated mentions of the same UUID are allowed. Multiple distinct sessions cause refusal before sending, starting a task, or querying a reply/status. The error lists each candidate's UUID, App Server name and status, comment count, and latest comment creation time and link. A body-only reference has zero comments and uses the issue's creation time and link. Unavailable session metadata is shown as `unknown`. An invalid latest owner or an owner-free invalid session reference also causes refusal. Specify a UUID or exact session name to resolve ambiguity. Quote `#186` so the shell does not treat it as a comment. A session without a name displays its UUID. Control characters and quotes are escaped in board cells and candidate details.

`read` prints the latest final assistant reply. `read --head` prints only the lines before its first blank line. `status` prints the App Server status as JSON. `send` restores an unloaded session through the existing App Server and queues one message. An active session returns exit code 0 immediately after the App Server acknowledges the matching queue item, with outcome `queued_behind_active`. The App Server automatically starts queued messages after the current turn finishes normally. A turn waiting for user input must finish first; an interruption or budget pause leaves queued messages pending. Queue acceptance does not confirm execution. For an idle session, `send` starts its exact queue item and confirms that the matching message began execution within 60 seconds, including turns that completed during confirmation. That returns exit code 0 with outcome `started`. Neither outcome confirms that the work completed successfully.

Both `send` and `run` require either `--message TEXT` or `--message-file PATH`. `--message -` reads stdin. Files use UTF-8. `--from NAME` adds a first line of `[from NAME]` to the message.

`send` persists a unique client message ID, timestamp, sender, target, and full message before delivery, then appends the result to `${XDG_STATE_HOME:-~/.local/state}/ding/sent.jsonl`. Issue targets are recorded as `OWNER/REPO#186`, including short `'#186'` and `--issue 186` inputs. The file has private permissions, and concurrent sends serialize their log writes. Results distinguish `started`, `queued_behind_active`, `queued`, `unknown`, and `failed`, with queue item and turn IDs when available. `queued_behind_active` records the acknowledged queue item without claiming a turn ID. A `queued` or `unknown` result returns a nonzero exit code and warns against resending. A lost acknowledgement triggers read-only history reconciliation; delivery mutations are never retried. A result log failure also warns against resending. The durable intent remains available when the result cannot be saved.

## Long tasks

From the owning Codex session, use `launch` to start a wrapped long job in a dedicated detached tmux session.

```bash
ding launch training -- python task.py
ding launch evaluation --message 'Evaluation finished; inspect exit.jsonl' \
  --record "$PWD/exit.jsonl" -- python evaluate.py
tmux attach -t ding-training
```

`launch NAME` creates tmux session `ding-NAME` and its first window `NAME` in the caller's current directory. Names contain 1-64 letters, digits, underscores, or hyphens and start with a letter or digit. Existing names cause an error; sessions are never reused or replaced. The pane may close when the wrapper exits. A successful launch reports tmux creation and the absolute record path; it does not mean the task has finished. A launch timeout leaves creation unconfirmed and warns against retrying before inspecting tmux and records.

`launch` reads and validates `CODEX_THREAD_ID` before invoking tmux, and passes the canonical UUID explicitly to `run`. It accepts `--message`, `--message-file`, `--from`, `--record`, and `--socket`. With no message option, the notification is `Task NAME exited; inspect ABSOLUTE_RECORD_PATH`. The default record is `${XDG_STATE_HOME:-~/.local/state}/ding/launch-records/LAUNCH_UUID.jsonl`. Input files, stdin, and the sender prefix are materialized once in the caller into a retained UTF-8 message file `LAUNCH_UUID.message.txt` in that directory, with mode `0600` and directory mode `0700`. Explicit record and socket paths are made absolute before entering tmux.

The inner wrapper uses the current absolute Python interpreter and module file. Each new tmux session receives the caller's `CODEX_THREAD_ID`, `PATH` (or the system default when absent), `HOME`, effective absolute `XDG_STATE_HOME`, and `PYTHONPATH` (empty when absent). Global tmux environment settings are unchanged. Other variables may still come from tmux's environment; pass task-specific settings explicitly, for example `ding launch training -- env CUDA_VISIBLE_DEVICES=0 python train.py`.

Direct `ding run UUID --message TEXT --record PATH -- COMMAND` remains available. Both forms export `DING_RUN_ID` to the wrapped child and its descendants, using the exact registry `run_id` and replacing any inherited outer marker. The caller's environment is unchanged.

Keep the wrapped command in the foreground. `run` records the task's exit, message, and unique client message ID before checking the session. If its status is `active`, notification is skipped permanently, including when restoring an unloaded session reveals active work. An idle or restored session receives the same delivery and execution confirmation as `send`. A failed status check prevents delivery. The wrapper preserves the task's exit code regardless of notification success. Each invocation appends `task_exit` and `notification` JSONL records with a shared `run_id` and client message ID. The wrapper does not rerun the task.

Before starting the command, `run` appends a `run_start` entry to `${XDG_STATE_HOME:-~/.local/state}/ding/runs.jsonl`. Failure to register prevents the command from starting. The registry has mode `0600`, and its `ding` directory has mode `0700`. Concurrent registrations serialize this durable append. Each line has this shape.

```json
{"event":"run_start","run_id":"UUID","thread_id":"CANONICAL_SESSION_UUID","record":"/absolute/exit.jsonl","pid":1234,"started_ns":1791799200000000000,"cwd":"/absolute/launch/directory"}
```

The registry stores one start event per invocation. `board` uses the last valid registration for each session, then matches `task_exit` and `notification` records by `run_id` and `thread_id`. Older records without a registry entry are not discovered. Invalid registry rows print warnings and are skipped.

Each invocation holds a private lock at `ding/run-locks/RUN_ID.lock` until the wrapper exits after notification. The lock descriptor does not pass to the task. Process death or a reboot releases the lock, and PID reuse cannot make that invocation appear alive. The token files remain after exit so readers can probe the same invocation without creating or replacing files.

`RUN` is `running` while the lock is held and no exit is recorded. It is `lost` when no exit is recorded and the lock is absent or released. After a recorded task exit, `exited(CODE)` means notification is still pending in a live wrapper, started, acknowledged behind active work, or skipped because the session was active. `notify_failed` means a failed or unconfirmed notification, including `queue_failed`, `failed`, `unknown`, or `queued`. A dead wrapper with an exit but no notification receipt also shows `notify_failed`. Missing or empty records provide no exit evidence. Unreadable records or corrupt complete events show `unknown` with a warning. An incomplete trailing JSON event is ignored with a warning, preserving earlier complete receipts.

Wakeups use the existing App Server after the task exits. The wrapper adds at most one message and starts at most its own queue item once. It has no CLI fallback or automatic replay.

The CLI discovers a single socket under `/tmp/codex-daemon-<uid>`. To choose one explicitly, add `--socket /path/to/socket` before the wrapped command's `--`. For a remote host, run the CLI there over SSH.

A session can become active between checking and sending. After a matching queue acknowledgement, observing that state returns `queued_behind_active` without waiting or explicitly starting the item. Check the delivery record and session history before resending; a new command creates a new message ID and can duplicate work. Killing the wrapper with SIGKILL or shutting down its host prevents notification. Failure to persist the initial exit record returns an error and prevents notification.

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest tests/test_codex_session.py tests/test_records.py
```

CI runs the tests on Python 3.10, 3.11, and 3.12. Tests never send messages to real sessions.
