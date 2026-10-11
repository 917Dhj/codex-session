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

A compatible Codex App Server must already run under the same user. The installed `codex` command must support `queue --remote` for notifications. These interfaces use an experimental API and may change with Codex versions.

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

`board` prints one tab-separated row per non-archived session with its name, working directory, status, issue numbers, and the first paragraph of its latest final reply. By default, it includes only loaded sessions with `idle` or `active` status in directories under the current Git repository and its linked worktrees. Outside Git, it uses the current directory. `--all` includes all non-archived sessions in every directory, including unloaded sessions. With `gh` available, issue numbers come from `Codex session:` lines in the current GitHub repository's issue bodies and comments. A failed lookup prints a warning to stderr and leaves that column blank. Sessions without a final reply have a blank summary.

Every target accepts a UUID, an exact name displayed by `board`, a quoted `'#186'`, or `--issue 186`. Names are matched across all non-archived sessions. Missing or ambiguous names report candidates. Issue targets use the last `Codex session:` line across the body and all comments. Quote `#186` so the shell does not treat it as a comment. A session without a name displays its UUID. Control characters and quotes are escaped in board cells.

`read` prints the latest final assistant reply. `read --head` prints only the lines before its first blank line. `status` prints the App Server status as JSON. `send` calls `codex queue` directly, including when the session is active.

Both `send` and `run` require either `--message TEXT` or `--message-file PATH`. `--message -` reads stdin. Files use UTF-8. `--from NAME` adds a first line of `[from NAME]` to the message.

`send` appends the timestamp, sender, target, full message, and queue result to `${XDG_STATE_HOME:-~/.local/state}/ding/sent.jsonl`. The file has private permissions, and concurrent sends serialize their log writes. Queue success followed by a log failure is reported explicitly; repeating the command may send the message twice. A queue timeout or interruption records an unknown delivery outcome.

## Long tasks

Run long jobs in tmux. Capture your own session ID before starting tmux and pass it to the wrapper explicitly.

```bash
thread_id="${CODEX_THREAD_ID:?Run this from the owning Codex session}"
tmux new-session -d -s long-job ding run "$thread_id" \
  --message 'Your message' --record "$PWD/exit.jsonl" -- python task.py
```

Keep the wrapped command in the foreground. `run` records the task's exit before checking the session. If its status is `active`, notification is skipped permanently. This is intentional: an active session can inspect the task without a wakeup. Any other status or a failed query causes one `codex queue` attempt. The wrapper preserves the task's exit code regardless of notification success. Each invocation appends `task_exit` and `notification` JSONL records with a shared `run_id`. The wrapper does not rerun the task.

Wakeups use `codex queue` after the task exits. Do not prequeue follow-up messages or use `turn/start(toolOutput)` as a wakeup.

The CLI discovers a single socket under `/tmp/codex-daemon-<uid>`. To choose one explicitly, add `--socket /path/to/socket` before the wrapped command's `--`. For a remote host, run the CLI there over SSH.

A session can become active between checking and sending, so a message can still queue. Killing the wrapper with SIGKILL or shutting down its host prevents notification. Failure to persist the initial exit record returns an error and prevents notification.

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest tests/test_codex_session.py tests/test_records.py
```

CI runs the tests on Python 3.10, 3.11, and 3.12. Tests never send messages to real sessions.
