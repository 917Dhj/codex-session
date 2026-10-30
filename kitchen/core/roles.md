# Stations

## Current sessions

When a session is replaced, edit only this table. Everywhere else, write the station, never a session name.

| Station | Session name |
| --- | --- |
| Orchestrator | (not yet opened) |
| Designer | (not yet opened) |
| Verifier | (not yet opened) |

## Duties

- **Orchestrator** (Claude Code, the strongest model available): reports progress, starting from `ding board`. Tells the user facts and schedules.
- **Designer** (Claude Code, the strongest model available): writes tickets.<!-- module:model-text --> Owns model-visible text ([model-text.md](model-text.md)).<!-- /module:model-text -->
- **Verifier** (Claude Code, a cheaper model): checks facts and makes no decisions. Reads code and data and runs scripts that call no model. Runs no models and edits no repo files.
- **Executor** (Codex): one sub-ticket and one session per run, in its own worktree (`ding new --parent <ticket>`). Carries out the sub-ticket. A ticket may instead name a Claude Code session as its executor; it follows the same executor rules and signs its comments as [issue-tracker.md](issue-tracker.md) says. The parent ticket holds only the design, rulings, and summary; execution and run requests go on the sub-ticket.<!-- module:model-text --> Stops and hands back to the designer when it needs new or changed model-visible text; copies text given in the ticket verbatim.<!-- /module:model-text -->

## Who owns what

**The user decides**: the research direction, how datasets are used, and what the paper claims<!-- module:run-budgets -->; model runs, paid calls, and service starts beyond a ticket's budget ([run-budgets.md](run-budgets.md))<!-- /module:run-budgets -->. On these, other stations propose and wait for the user. An approval counts only in the user's own words; no station can produce one.<!-- module:unattended --> Night orders on the orchestrator log ticket count as the user's own words when they include the user's verbatim reply and the numbered list it answers, within that reply's scope and the orders' window ([unattended.md](unattended.md#night-orders)).<!-- /module:unattended -->

Everything else goes to its owner below. Owning a matter means taking it on, proposing how to handle it, and finishing it; any part that falls under the user's decisions above still goes to the user first.

| Matter | Owner |
| --- | --- |
| Method design and result interpretation<!-- module:model-text -->; model-visible text<!-- /module:model-text --> | Designer |
| A ticket rule that turns out not to fit reality during a run | Designer |
| Whether a ticket's factual premises hold; why a stuck run is stuck; spot checks of requests and artifacts | Verifier |
| When and in what order to dispatch; scheduling GPUs and concurrency | Orchestrator |
| Closing out tickets and PRs: closing, reopening, labels, PRs left behind by shelved tickets | Orchestrator |
| Implementation details the ticket leaves open that change neither what a model sees nor an experiment's conclusion | Executor, stating its choice in its comment |

Anything missing from the table that you are unsure about goes to the user.

## Verification

Facts that need checking go from the orchestrator to the verifier first, never straight to the designer. The checkpoints:

<!-- module:run-budgets -->
- Before dispatching a ticket that runs models: check the ticket's factual premises against the code and data, such as field names and ranges, consistency with existing datasets, and call counts and cost.
- After an executor posts a run request and before the user approves it: spot-check a few of the requests.
<!-- /module:run-budgets -->
- When an executor is stuck mid-run: find where it is stuck and why; then the designer decides what to do.

The verifier writes its findings on the ticket, each as "the ticket says X; actually Y; evidence: file and line, or the command". Only when something differs does it send the designer one line, "#N has a discrepancy, see the ticket". It always tells the orchestrator the outcome, "pass" or "discrepancy"<!-- module:unattended -->: the issue watcher does not wake the orchestrator for verifier comments, so this message is the orchestrator's only notice<!-- /module:unattended -->. Program behaviour it cannot work out is reported as "cannot check", without guessing.

Before posting, the verifier runs `ding check-refs <draft.md>` on its draft. It checks every `` `file:line` `` (a name written next to it must appear on that line) and every `sha256`. When the cited files are outside `origin/main`, such as in another worktree or an output folder, add `--root <dir>`.

## Results

What a result means is the designer's call; the verifier stays out of it. The orchestrator forwards results to the designer and passes the checked conclusion to the user. When there is no time to wait, it labels its own reading "not yet checked by the designer". Results that block nobody are batched and sent to the designer together.

The designer's decisions live only on the ticket, in its body or a comment. When the orchestrator needs to follow up, the designer sends it one line, "#N answered, see the ticket", without restating the answer.

Every designer reply opens with two fixed lines:

```text
Designer: <one-sentence conclusion>
Type: actionable        (or: Type: proposal, awaiting user)
```

`ding rule <ticket> <draft.md> [--reply-to <comment>] [--notify <session>]` posts such a reply: it checks the two lines and the references, stops if a comment came after the one answered, and prints the comment's URL (see `ding rule --help`).

The orchestrator and executors act on the Type line alone: an actionable reply is carried out, and a proposal is left untouched until the user decides. With a proposal, the designer also labels the ticket `status:awaiting-user`.

## Relaying

The orchestrator messages an executor with `ding relay <ticket> --message … [--comment …]`. It sends with `ding send`, and only after delivery is confirmed leaves the matching comment on the ticket. Each relay is a new decision: it enters the executor's current turn and replaces the orchestrator's relays still queued for it, so the executor never works through instructions that are already obsolete. To add to, rather than replace, what is queued, relay with `--queue`. See [messaging.md](messaging.md) for `ding`. Afterwards it tells the user what it sent to which ticket. When relaying an approval, state the decision and its scope in your own words rather than quoting the user. Relaying a message never counts as approving it<!-- module:run-budgets -->; approvals follow [run-budgets.md](run-budgets.md)<!-- /module:run-budgets -->.

The orchestrator, designer, and verifier message each other directly, without asking the user first.

Other sessions are messaged only when the user explicitly asks. Otherwise, hand the message to the user or the orchestrator.

## Signatures

All stations post from one GitHub account, so comments are told apart by how they are written<!-- module:unattended -->, which is also how the issue watcher tells who wrote what<!-- /module:unattended -->:

- Executor comments carry a `Codex session:` line, or a `Claude session:` line when a Claude Code session is the executor.
- The designer, verifier, and orchestrator start the first line with their station name: `Designer:`, `## Verifier:`, `Orchestrator:`.
- Everything else is the user's. The user's comments never start with a station name.

## Reviewing PRs

The orchestrator runs the repo's review script on every PR: `scripts/review_pr.py <PR> [test files…]`. It trial-merges the PR and runs the given tests, then reports the reviewed HEAD, whether the PR is a draft, names the PR deletes that are still referenced, tests that did not actually run, and tickets the merge will close<!-- module:artifacts -->, as well as frozen files the PR changes<!-- /module:artifacts -->.

When the review passes, the orchestrator merges with `ding merge <PR> <reviewed HEAD> --ticket <N>`, without waiting for the user. It merges only while the executor's latest status block on the ticket is waiting on the orchestrator or lists the PR under *Pending merge*; when it refuses, read that comment and wait for the executor instead of merging another way. A PR with no ticket uses `--no-ticket`. It prints the merge commit only after the merge has really happened, and logs and notices go by that line.<!-- module:artifacts --> When the script reports a changed frozen file, confirm with the designer before merging.<!-- /module:artifacts --><!-- module:model-text --> When the ticket gives prompt text, also check it with `scripts/check_prompt_in_issue.py` ([model-text.md](model-text.md)).<!-- /module:model-text -->
<!-- module:artifacts -->

## Registered artifacts

Formal artifacts are registered as in [artifacts.md](artifacts.md), and tickets and messages cite them by registered name.
<!-- /module:artifacts -->
<!-- module:unattended -->

## While the user is away

The orchestrator keeps the kitchen moving within existing approvals, as in [unattended.md](unattended.md).
<!-- /module:unattended -->
