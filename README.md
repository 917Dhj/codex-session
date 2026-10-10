# codex-session

Read and message Codex sessions from any project. Wrap a foreground job to notify its owning session after exit.

This is an independent project, not an official OpenAI tool.

## Installation

Requires Python 3.10 or later and a POSIX system. Tested on Linux. The CLI uses Unix sockets and `fcntl` locks.

```bash
pipx install git+https://github.com/917Dhj/codex-session.git
pipx ensurepath
```

Without pipx, install into a virtual environment and add its bin directory to your shell's `PATH`.

```bash
python3 -m venv ~/.local/share/codex-session
~/.local/share/codex-session/bin/python -m pip install git+https://github.com/917Dhj/codex-session.git
export PATH="$HOME/.local/share/codex-session/bin:$PATH"
```

A compatible Codex App Server must already be running under the same user. The installed `codex` command must support `queue --remote` for notifications. These interfaces use an experimental API and may change with Codex versions.

## Use

Replace `UUID` with the target session's thread UUID.

```bash
codex-session read UUID
codex-session status UUID
codex-session send UUID --message "Your message"
codex-session run UUID --message "Your message" --record exit.jsonl -- python task.py
```

`read` prints the latest final assistant reply. `status` prints the App Server status as JSON. `send` calls `codex queue` directly, including when the session is active.

Run long jobs in tmux. Capture your own session ID before starting tmux and pass it to the wrapper explicitly.

```bash
thread_id="${CODEX_THREAD_ID:?Run this from the owning Codex session}"
tmux new-session -d -s long-job codex-session run "$thread_id" \
  --message "Your message" --record "$PWD/exit.jsonl" -- python task.py
```

Keep the wrapped command in the foreground. `run` records the task's exit before checking the session. If its status is `active`, notification is skipped permanently. Any other status or a failed query causes one `codex queue` attempt. The wrapper preserves the task's exit code regardless of notification success. Each invocation appends `task_exit` and `notification` JSONL records with a shared `run_id`. The wrapper does not rerun the task.

Wakeups use `codex queue` after the task exits. Do not prequeue follow-up messages or use `turn/start(toolOutput)` as a wakeup.

The CLI discovers a single socket under `/tmp/codex-daemon-<uid>`. To choose one explicitly, add `--socket /path/to/socket` before `--`. For a remote host, run the CLI there over SSH.

An active session can miss a notification. A session can become active between checking and sending, so a message can still queue. Killing the wrapper with SIGKILL or shutting down its host prevents notification. Failure to persist the initial exit record returns an error and prevents notification.
