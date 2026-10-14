# Tickets: GitHub issues

Every piece of work is a ticket: a GitHub issue in this repo, handled with `gh`. The ticket holds the whole story: the task, decisions, requests, and results. Anything said elsewhere that matters is written back to the ticket.

## Executor comments

When you take over a ticket, post a comment with the line `Codex owner: <CODEX_THREAD_ID>`. `ding` delivers messages for the ticket to the latest owner line, so a new executor taking over posts its own.

Every comment you post carries `Codex session: <CODEX_THREAD_ID>`, with your current session's ID.

Open every comment with the status block, and put the details after it inside `<details>`:

```text
Status: in progress | waiting on design | waiting on user | blocked | done
Waiting on: <who, for what; or none>
Key numbers: <the numbers the reader needs, on one line>
Next: <what happens next, and who does it>
```

Each field takes one line. Digests, process IDs, and paths go inside `<details>`.

A designer reply marked `Type: proposal, awaiting user` waits for the user: act on it only after the user decides.

When Status is `waiting on design` or `waiting on user`, the first sentences after the block, before `<details>`, say in plain words what needs deciding, the options, and the one you recommend.

When you stop, set the label that matches Status and remove the previous one. When you resume, remove it. `gh issue list --label <label>` then shows who is waiting on whom.

| Status | Label | Meaning |
| --- | --- | --- |
| waiting on design | `status:awaiting-design` | Needs a design answer<!-- module:model-text --> or model-visible text<!-- /module:model-text --> from the designer |
| waiting on user | `status:awaiting-user` | Needs the user's approval or manual review |
| blocked | `status:blocked` | Stopped on a failure or another ticket |
| done | `status:done` | Work finished; PR open or merged |
<!-- module:run-budgets -->

A comment asking to run models adds the run request fields from [run-budgets.md](run-budgets.md) right after the status block.
<!-- /module:run-budgets -->

## Failures you fix yourself

A failure that involves no model call and leaves committed results untouched, such as a wrong path, a missing package, or a tool version: fix it, rerun, and note it in one line of your next comment.

Stop and wait when the fix<!-- module:artifacts --> touches a frozen file,<!-- /module:artifacts --><!-- module:model-text --> changes model-visible text,<!-- /module:model-text --> needs model calls or cost beyond the ticket, or changes the ticket's scope.
