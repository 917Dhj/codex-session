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
ding read UUID
ding read --head 'Exact session name'
ding status '#186'
ding send --issue 186 --message-file message.txt --from coordinator
printf '%s\n' 'Your message' | ding send UUID --message -
ding run UUID --message 'Your message' --record exit.jsonl -- python task.py
```

`board` prints one tab-separated row per non-archived session with its name, working directory, status, issue numbers, and the first paragraph of its latest final reply. By default, it includes only loaded sessions with `idle` or `active` status in directories under the current Git repository and its linked worktrees. Outside Git, it uses the current directory. `--all` includes all non-archived sessions in every directory, including unloaded sessions. With `gh` available, an issue with a `Codex owner: UUID` line is attributed only to its latest owner across the body and comments. Other issues are attributed to every valid `Codex session: UUID` reference. An invalid latest owner prints a warning and omits that issue. A failed lookup prints a warning to stderr and leaves that column blank. Sessions without a final reply have a blank summary.

Every target accepts a UUID, an exact name displayed by `board`, a quoted `'#186'`, or `--issue 186`. Names are matched across all non-archived sessions. Missing or ambiguous names report candidates. Issue targets use the latest `Codex owner: UUID` line across the body and comments. Without an owner, all `Codex session: UUID` references must identify one session; repeated mentions of the same UUID are allowed. Multiple distinct sessions cause refusal before sending, starting a task, or querying a reply/status. The error lists each candidate's UUID, App Server name and status, comment count, and latest comment creation time and link. A body-only reference has zero comments and uses the issue's creation time and link. Unavailable session metadata is shown as `unknown`. An invalid latest owner or an owner-free invalid session reference also causes refusal. Specify a UUID or exact session name to resolve ambiguity. Quote `#186` so the shell does not treat it as a comment. A session without a name displays its UUID. Control characters and quotes are escaped in board cells and candidate details.

`read` prints the latest final assistant reply. `read --head` prints only the lines before its first blank line. `status` prints the App Server status as JSON. `send` restores an unloaded session through the existing App Server and queues one message. An active session returns exit code 0 immediately after the App Server acknowledges the matching queue item, with outcome `queued_behind_active`. The App Server automatically starts queued messages after the current turn finishes normally. A turn waiting for user input must finish first; an interruption or budget pause leaves queued messages pending. Queue acceptance does not confirm execution. For an idle session, `send` starts its exact queue item and confirms that the matching message began execution within 60 seconds, including turns that completed during confirmation. That returns exit code 0 with outcome `started`. Neither outcome confirms that the work completed successfully.

Both `send` and `run` require either `--message TEXT` or `--message-file PATH`. `--message -` reads stdin. Files use UTF-8. `--from NAME` adds a first line of `[from NAME]` to the message.

`send` persists a unique client message ID, timestamp, sender, target, and full message before delivery, then appends the result to `${XDG_STATE_HOME:-~/.local/state}/ding/sent.jsonl`. The file has private permissions, and concurrent sends serialize their log writes. Results distinguish `started`, `queued_behind_active`, `queued`, `unknown`, and `failed`, with queue item and turn IDs when available. `queued_behind_active` records the acknowledged queue item without claiming a turn ID. A `queued` or `unknown` result returns a nonzero exit code and warns against resending. A lost acknowledgement triggers read-only history reconciliation; delivery mutations are never retried. A result log failure also warns against resending. The durable intent remains available when the result cannot be saved.

## Long tasks

Run long jobs in tmux. Capture your own session ID before starting tmux and pass it to the wrapper explicitly.

```bash
thread_id="${CODEX_THREAD_ID:?Run this from the owning Codex session}"
tmux new-session -d -s long-job ding run "$thread_id" \
  --message 'Your message' --record "$PWD/exit.jsonl" -- python task.py
```

Keep the wrapped command in the foreground. `run` records the task's exit, message, and unique client message ID before checking the session. If its status is `active`, notification is skipped permanently, including when restoring an unloaded session reveals active work. An idle or restored session receives the same delivery and execution confirmation as `send`. A failed status check prevents delivery. The wrapper preserves the task's exit code regardless of notification success. Each invocation appends `task_exit` and `notification` JSONL records with a shared `run_id` and client message ID. The wrapper does not rerun the task.

Wakeups use the existing App Server after the task exits. The wrapper adds at most one message and starts at most its own queue item once. It has no CLI fallback or automatic replay.

The CLI discovers a single socket under `/tmp/codex-daemon-<uid>`. To choose one explicitly, add `--socket /path/to/socket` before the wrapped command's `--`. For a remote host, run the CLI there over SSH.

A session can become active between checking and sending. After a matching queue acknowledgement, observing that state returns `queued_behind_active` without waiting or explicitly starting the item. Check the delivery record and session history before resending; a new command creates a new message ID and can duplicate work. Killing the wrapper with SIGKILL or shutting down its host prevents notification. Failure to persist the initial exit record returns an error and prevents notification.

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest tests/test_codex_session.py tests/test_records.py
```

CI runs the tests on Python 3.10, 3.11, and 3.12. Tests never send messages to real sessions.
