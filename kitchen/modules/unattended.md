# While the user is away

When the user is away, an issue watcher wakes the orchestrator, which moves forward only what existing approvals already cover. The test for every step: **if anyone would do it the same way, do it. If there are several reasonable ways and the choice affects results, cost, or direction, stop and wait for the user.** Approvals come only from the user's own words; the watcher, the orchestrator, and the designer never produce one.

## The watch

The orchestrator runs the watch in the background of its main checkout, always with the same command. After handling each batch it starts the watch again; that is the last step of every round.

```bash
ding watch
```

The watch only reads GitHub, `ding board`, and tmux; it calls no model. It exits, waking the orchestrator, about 5 minutes after the first of these appears, so that related changes arrive as one batch:

- an executor comment whose Status is not `in progress`, or whose Waiting on names a station or the user;
- a comment from the user;
- a `status:*` label being added;
- a new PR;
- an executor gone quiet: `in progress` for over 6 hours without a comment, with no running `ding run` and no live tmux job whose session name contains `issue<ticket number>`.

Verifier comments never wake it on their own, since the verifier also messages the orchestrator; they are listed in the next batch for reference. It skips the orchestrator log ticket set in `.dkitchen.toml`. One watch runs at a time; a second exits at once. `ding watch --help` lists the timing options.

A closed session or a host reboot stops the watch silently. When the user is back, the orchestrator's background tasks show whether it is still running.

## What the orchestrator moves forward

- **Next stage**: an executor reports a stage done, and the ticket already approves the next stage with no "wait for the designer" checkpoint: tell it to continue. With a checkpoint, forward the report to the designer.
- **Stuck or quiet executor**: hand it to the verifier to find the cause, then decide by this file and the list below.
- **Results**: forward them to the designer for interpretation.
- **New PR**: review and merge it as in [roles.md](roles.md#reviewing-prs). PRs that change frozen files, `docs/agents/`, rule or process files, or code a running job depends on wait for the user or the designer.
- **Finished tickets**: label and close them. Tickets whose exit conditions include something for the user, or whose results the designer has yet to interpret, stay open.
- **Host reboot**: reopen tunnels and have executors resume with their original commands. Shared services that went down wait for the user to restart them.

## What the designer can decide

Actionable, decided by the designer alone:

- adding detail to metrics and steps the ticket already has, such as how a statistic is counted;
- resolving places where a rule does not fit reality, such as the details of failure handling;
- spending within the ticket's approved budget<!-- module:model-text -->;
- adjusting model-visible text after an exploration step, as [model-text.md](model-text.md) allows<!-- /module:model-text -->.

Exit thresholds and decision rules set in advance stay as they are.

Waiting for the user: changing the goal, the dataset, or experimental conditions; changing decision rules and thresholds set in advance; research conclusions and paper claims; going over the approved budget; opening tickets in a new direction. When the verifier and the designer disagree, both views stay on the ticket for the user.

## The log

After each batch, the orchestrator records it on the pinned "Orchestrator log" ticket:

```bash
ding log --item '#42 told the executor to start stage 2; approved in the ticket budget' --pending '#57 needs a dataset choice'
```

The time is filled in, in UTC. Each automatic action gets one `--item`: the ticket, what was done, and which approval covered it. Each matter waiting for the user gets one `--pending`; leave it out when there are none.
