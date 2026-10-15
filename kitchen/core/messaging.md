# Messaging sessions (ding)

`ding` shows what executor sessions are doing, sends them messages, and starts new Codex sessions. Install it with `pipx install dkitchen`; the full guide is at https://github.com/917Dhj/dkitchen/blob/main/docs/ding.md. It reaches Codex sessions only. Claude Code sessions message each other with the `SendMessage` tool.

```bash
ding new --cwd /path/worktree --issue 42 --name 'Ablation' --message-file /path/brief.md
ding board
ding read --head '#42'
ding status '#42'
ding send '#42' --from Orchestrator --message-file /path/message.md
```

- **Targets**: a session UUID, a session name, or a ticket: `OWNER/REPO#42` or the ticket's URL (independent of the current directory), or `'#42'` (this directory's repo; quote the `#`). A ticket resolves to its latest `Codex owner:` line. With no owner line and comments from more than one session, `ding` refuses and lists the candidates.
- **`new`**: creates a saved Codex session in `--cwd`, names it, sends the first message, and prints only the session UUID. It does not wait for the reply. On failure it prints the failed stage and, if known, the UUID; check `ding board` before retrying, since a retry can create a second session. For a ticket's executor, add `--issue N` and keep `--name` to a few words: the repo's `.dkitchen.toml` adds the ticket prefix (by default `【#42】Ablation`), and the first message asks the session to post its `Codex owner:` line. Until it does, address it by UUID.
- **`board`**: loaded sessions of this repo and its worktrees, with name, status, ticket, and the first paragraph of the latest reply. `--all` lists every session and is slow.
- **`read`**: the latest final reply; `--head` prints only its first paragraph. **`status`**: the session's status as JSON.
- **`send`**: when the receiver is idle, `ding` confirms the message started running; when it is busy, `ding` returns once the message is queued, and the message runs after the current turn. Started is not finished: the result is the receiver's reply on the ticket. On a nonzero exit (`queued`, `unknown`), check the send log before sending again, since a resend can make the receiver do the work twice. `--from` names the sender; long messages go in `--message-file`. Every send is logged in `~/.local/state/ding/sent.jsonl`.
