# Changelog

## 0.5.15

- `ding watch` keeps every reported line whose GitHub time is at or after the new `since`, so items stamped ahead of the clock are reported once. The state file now stores `seen` as line-to-time; older list states still load.

## 0.5.13

- `ding merge` supports optional `[merge] required_checks`, defaulting to `[]`. Every required name must succeed on the reviewed HEAD across the latest check runs and commit statuses. Missing, pending, failed, neutral and skipped results refuse immediately with no bypass; API/read errors also refuse.

## 0.5.12

- `ding waits` lists overdue unanswered waits on all configured stations and the user, with station/alias filtering, a minute threshold, and JSON output.
- The orchestrator's watch also reminds once per executor comment about unanswered verifier waits, using the shared waits query.
- `ding watch` refuses stdout redirected to `/dev/null` with exit 3 before polling or touching its state; pipes and files remain supported.

## 0.5.11

- `ding night open|show|close` records time-limited night orders on the orchestrator log ticket, with configurable headings and explicit replacement of open orders.
- User-invoked `goodnight` and `goodmorning` skills collect approvals for the offline window and report their use when the user returns.
- Unattended-work templates require stations to check night orders before waiting on the user and name the order behind each automatic action in the log.

## 0.5.10

- Watch name matching supports `[stations.aliases]` and width-normalized brackets/colons, with longest configured-name precedence.
- Station watches recognize Next-field handoffs, explicit mentions from non-designers, and default-designer requests. Configured `[status] paused` executors do not wake them.
- Older status labels no longer hide idle or stale executor comments; awaiting-user labels continue to suppress these checks.
- The orchestrator reports unanswered designer requests after `[watch] design_answer_minutes`, default 30, with persistent per-comment deduplication. Set 0 to disable it.

## 0.5.9

- `ding watch --for NAME` watches only executor comments waiting on that station, with longest configured-name matching and separate default state files.
- Station watches skip label/PR queries and stale/idle checks, including board and tmux calls.

## 0.5.8

- `ding rule --notify` sends the complete stripped first nonempty line, including through `{first_line}` templates.
- Shortened terminal hints, watch summaries, PR titles, and diagnostic excerpts end with `…`.

## 0.5.7

- `ding watch` reports idle executors still marked in progress after `[watch] idle_minutes`, default 45 minutes. Idle checks run every third of the threshold, capped at 15 minutes, independently of stale checks. First-observation times survive restarts; setting 0 disables the new wake.
- Watch uses `ding board --no-issue-lookup` to attribute sessions only by `[sessions] name`, without a whole-repository scan. Non-template session names remain unattributed. Plain board retains its GitHub fallback; watch retries nonzero board exits.
- GitHub errors during post-settle collection and stale/idle checks count toward `--max-errors` without advancing the reporting cursor or printing partial batches.

## 0.5.6

- `[rule] signatures` configures the designer prefixes shared by `ding rule` and `ding watch`.
- Without `signatures`, `ding watch` now requires the designer name plus the configured separator, rather than the bare designer name. Verifier and orchestrator matching is unchanged.
