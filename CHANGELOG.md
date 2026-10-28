# Changelog

## 0.5.7

- `ding watch` reports idle executors still marked in progress after `[watch] idle_minutes`, default 45 minutes. Idle checks run every third of the threshold, capped at 15 minutes, independently of stale checks. First-observation times survive restarts; setting 0 disables the new wake.
- Board and watch prefer ticket numbers parsed from `[sessions] name`, falling back to GitHub attribution for unmatched names. Watch retries attribution failures instead of continuing with an empty mapping.
- GitHub errors during post-settle collection and stale/idle checks count toward `--max-errors` without advancing the reporting cursor or printing partial batches.

## 0.5.6

- `[rule] signatures` configures the designer prefixes shared by `ding rule` and `ding watch`.
- Without `signatures`, `ding watch` now requires the designer name plus the configured separator, rather than the bare designer name. Verifier and orchestrator matching is unchanged.
