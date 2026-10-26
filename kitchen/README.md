# The kitchen

These are the house rules of dkitchen, as templates. The setup skill installs them into a repo's `docs/agents/` and adapts them to that repo; you can also read them here to see how the kitchen runs.

## Layout

| Template | Installed as | What it covers |
| --- | --- | --- |
| [core/agents-block.md](core/agents-block.md) | blocks in `AGENTS.md` and `CLAUDE.md` | The pointers every session reads |
| [core/roles.md](core/roles.md) | `docs/agents/roles.md` | Stations, who decides what, verification, results, relaying, signatures, PR review |
| [core/issue-tracker.md](core/issue-tracker.md) | `docs/agents/issue-tracker.md` | Tickets, executor comments, the status block, status labels |
| [core/messaging.md](core/messaging.md) | `docs/agents/messaging.md` | Messaging sessions with `ding` |
| [core/long-running-jobs.md](core/long-running-jobs.md) | `docs/agents/long-running-jobs.md` | `ding launch`, resumable jobs, compute and shared services |
| [modules/run-budgets.md](modules/run-budgets.md) | `docs/agents/run-budgets.md` | Run requests, ticket budgets, what needs the user's approval |
| [modules/model-text.md](modules/model-text.md) | `docs/agents/model-text.md` | Prompts and other text a model sees, owned by the designer |
| [modules/artifacts.md](modules/artifacts.md) | `docs/agents/artifacts.md` | The artifact registry and frozen files |
| [modules/unattended.md](modules/unattended.md) | `docs/agents/unattended.md` | Running while the user is away: the issue watcher and the orchestrator log |

Core files go into every kitchen. Each module is optional.

## Template markers

- `<!-- module:NAME -->` … `<!-- /module:NAME -->` wraps text that belongs to module `NAME`. When the module is installed, keep the text and drop the markers. When it is not, drop the text too.
- `<!-- fill: … -->` marks a gap the setup skill fills from the repo and the user's answers, such as hosts, GPUs, or shared services.

## Vocabulary

The rules and the `ding` commands share one vocabulary. The defaults are below. A kitchen in another language changes these words in the rules and in the repo's [`.dkitchen.toml`](../docs/ding.md#dkitchentoml) together, so that people, agents, and scripts keep agreeing.

| Word | Default | Read by |
| --- | --- | --- |
| Station names | Orchestrator, Designer, Verifier, Executor | Everyone |
| Signatures | A comment's first line starts with `Orchestrator:`, `Designer:`, or `Verifier:` (Markdown heading marks allowed). Executor comments carry a `Codex session:` line (`Claude session:` for a Claude Code executor). Anything else is the user's. | `ding watch`, `ding relay`, `ding log` |
| Owner line | `Codex owner: <session id>` | `ding` targets |
| Status fields | `Status`, `Waiting on`, `Key numbers`, `Next` | `ding board --status`, `ding watch` |
| Status values | `in progress`, `waiting on design`, `waiting on user`, `blocked`, `done` | `ding watch` |
| Nobody to wait on | `none` | `ding watch` |
| Designer reply type | `Type: actionable`, `Type: proposal, awaiting user` | Orchestrator, executors |
| Status labels | `status:awaiting-design`, `status:awaiting-user`, `status:blocked`, `status:done` | `ding watch`, `gh issue list` |

## Tools the rules call

Two layers:

- **`ding` commands** do the same thing in every repo and are installed with `pipx install dkitchen`: `ding relay`, `ding merge`, `ding log`, `ding watch`, `ding check-refs`, and the messaging commands in [the ding guide](../docs/ding.md).
- **Repo scripts** depend on the repo, so the setup skill writes them into the repo's `scripts/`, starting from reference versions: `review_pr.py` for PR review, and the checks some modules use.
