# Messaging sessions (ding)

`ding` shows what executor sessions are doing and sends them messages. Install it with `pipx install dkitchen`; the full guide is at https://github.com/917Dhj/dkitchen/blob/main/docs/ding.md. It reaches Codex sessions only. Claude Code sessions message each other with the `SendMessage` tool.

```bash
ding board
ding read --head '#42'
ding status '#42'
ding send '#42' --from Orchestrator --message-file /path/message.md
```

- **Targets**: a session UUID, a session name, or a ticket: `OWNER/REPO#42` or the ticket's URL (independent of the current directory), or `'#42'` (this directory's repo; quote the `#`). A ticket resolves to its latest `Codex owner:` line. With no owner line and comments from more than one session, `ding` refuses and lists the candidates.
- **`board`**: loaded sessions of this repo and its worktrees, with name, status, ticket, and the first paragraph of the latest reply. `--all` lists every session and is slow.
- **`read`**: the latest final reply; `--head` prints only its first paragraph. **`status`**: the session's status as JSON.
- **`send`**: when the receiver is idle, `ding` confirms the message started running; when it is busy, `ding` returns once the message is queued, and the message runs after the current turn. Started is not finished: the result is the receiver's reply on the ticket. On a nonzero exit (`queued`, `unknown`), check the send log before sending again, since a resend can make the receiver do the work twice. `--from` names the sender; long messages go in `--message-file`. Every send is logged in `~/.local/state/ding/sent.jsonl`.
