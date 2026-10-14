# Blocks for AGENTS.md and CLAUDE.md

Every session loads these lines, so each one is a pointer to a doc under `docs/agents/`, worded by when to reach it. Add the first block to `AGENTS.md`. Add the second to `CLAUDE.md`, which should also import `AGENTS.md`.

## AGENTS.md

```markdown
- **Roles**: at session start, and before messaging another session or interpreting a result, read [docs/agents/roles.md](docs/agents/roles.md).
- **Long-running jobs** (Codex): start every tmux job with `ding launch`; compute and shared services are in [docs/agents/long-running-jobs.md](docs/agents/long-running-jobs.md).
- **GitHub issues** (Codex): append `Codex session: <CODEX_THREAD_ID>` to every issue body or comment you post, using your current session's ID. Open each comment with the status block<!-- module:run-budgets -->, and write run requests<!-- /module:run-budgets -->, as in [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md#executor-comments).
```

## CLAUDE.md

```markdown
@AGENTS.md

## Execution boundaries

- **Long jobs** (started with `ding launch`) belong to Codex: `ding` cannot wake a Claude Code session.
- **GPU runs**: Codex runs them unless the user explicitly authorizes otherwise.
```
