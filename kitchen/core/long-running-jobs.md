# Long-running jobs

Start every CPU, GPU, or API job that runs in tmux with `ding launch`, however short you expect it to be. It opens tmux session `ding-NAME`, reads the session's `CODEX_THREAD_ID`, and wraps the job with `ding run`; when the job exits, it wakes the session that launched it.

```bash
ding launch issue42-stage1 --message "$message" --record /absolute/path/task-exit.jsonl -- env OMP_NUM_THREADS=8 bash /absolute/path/task.sh
```

After launching, end your turn: the job's exit wakes you. Look at progress only when the user asks.

- **Environment**: tmux carries over only `PATH`, `HOME`, `PYTHONPATH`, `XDG_STATE_HOME`, and the session ID. Pass anything else the job needs as `env K=V` in the command.
- **After exit**: the pane stays open so you can read the output and errors. The executor that launched it closes it with `tmux kill-session -t =ding-NAME` when done.
- **Unwrapped jobs**: the end of `ding board` lists tmux jobs on this host that have run for over 10 minutes without `ding`. The orchestrator asks the executor that owns one to relaunch it with `ding launch`.
- **Missed wake-ups**: the wrapper killed with SIGKILL, a host reboot, or a job that hangs. The wake-up comes once, when the job exits, so follow-up messages are never queued in advance.
- **Several App Servers**: add `--socket PATH` before the `--`. For another host, SSH there first.

## Resumable jobs

Every long job is resumable: rerunning the original command continues it. This is what lets the kitchen recover from a crash or reboot without asking anyone.

- A result counts only once it is durably on disk, written atomically. A killed job loses no committed result and counts none twice.
- On resume, the job first checks that its inputs and config still match the existing outputs. On a mismatch or corrupt output, it keeps the originals and stops with a clear error. Otherwise it skips committed results and redoes only unfinished work.
- A clear failure of one sample is final: record it, move on, and keep it in the evaluation's denominator. Service, environment, resource, and unknown failures are retried a bounded number of times, then the job pauses with the work left pending, ready for a rerun.

## Compute

<!-- fill: the hosts and GPUs this repo may use, how to tell a GPU is free on each, and which GPUs belong to other people. -->

Other users' processes are never touched. Processes under our own account are ours, but before stopping one, confirm it is not another ticket's job.

## Shared services

<!-- fill: a table of shared services (model servers, databases, tunnels) with columns Service | Where | Owner, and the command that checks their load, if the repo has one. -->

- **Restarts and parameter changes** of shared services are the user's. Before one, the clients are told to stop at a checkpoint with SIGINT; afterwards they resume with their original commands.
- **Tunnels**: when a tunnel is down, the orchestrator, or the executor that needs it, reopens it. <!-- fill: where tunnels run and the command for each. -->
- **A host reboot** takes the tmux jobs and tunnels with it, and `ding run` has no chance to write exit records: the job's exit record is empty and its process is gone. Reopen the tunnels first, then have each ticket resume with its original command.
