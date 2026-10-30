---
name: goodnight
description: Before the user goes offline, collect every decision the next hours will need, get the user's approvals and boundaries in one pass, and post them as night orders. Run in the orchestrator session.
disable-model-invocation: true
---

# Goodnight

The user is about to be away for several hours, usually overnight. Your job is to write **night orders**: the captain's orders for the watch. They list what the kitchen may do without waking anyone, and when to stop. A night where a run waits on the user while cards sit idle is the failure this skill exists to prevent. Such a wait usually comes from something the user would have approved in a word, if anyone had asked before they left.

The orchestrator runs this skill, because the orchestrator is the station that stops during the night. It schedules cards, starts services and releases runs. The designers own their lines' technical calls. You gather their stops, add your own, and give the user one list.

Words in this file follow the repo's vocabulary in `.dkitchen.toml` and its `docs/agents/` rules. Read "designer" as every designer station the repo has, for example a general designer and a training designer.

## 1. Ask the designers

Send this question to every designer station now, before anything else, so their answers arrive while you work. Use the repo's messaging, as in `docs/agents/messaging.md`. Translate the message into the repo's working language, keeping its meaning:

> Night orders: the user goes offline for about <N> hours, until <time UTC>. For your line over that window, list every point where work would stop to wait on the user. For each point, give one of these:
> (a) the approval you want, with its scope (spend, card-hours, service, endpoint);
> (b) a decision rule I can apply without you, written as "if <observable>, then <action>; otherwise <action>";
> (c) "park until morning".
> Also list what must stop the line no matter what. Reply under your signature within 10 minutes. I will paste your text verbatim.

Then start a 10-minute timer in the background, `sleep 600`, so a missing reply cannot leave you waiting.

## 2. Read the state

Build the picture from the sources, not from memory. Your context may be hours old.

- `ding board`: sessions, running jobs, and their states.
- Open tickets labelled awaiting-user. Also the latest executor comments whose waiting field names the user.
- Each running job's remaining work and expected finish. Use its `progress.json` or log if the repo keeps one.
- Free cards and running shared services on every host the repo uses. The commands are in `docs/agents/long-running-jobs.md`.
- The `--pending` items in recent entries on the orchestrator log ticket.
- Standing approvals the repo already records. The user granted these once for good, so they need no night order. Do not ask again.

## 3. Trace the window

Walk the window forward for every running job and every ticket that is ready to start. Note each **stop**: a point where work would wait on the user. Typical stops:

- a job finishes and the next stage needs a release;
- a run needs a card, a service start or restart, or an endpoint;
- spend passes a ticket's approved budget;
- a result arrives and the next step depends on reading it;
- a merge waits on a rule file;
- cards fall idle with nothing approved to fill them.

For the last one, ask the user for a **fill order**: which approved work goes onto free cards, in which order.

Done when every running job and every ready ticket reaches one of two ends within the window: it keeps running past the window, or it halts at a named stop. Include each designer's stops as they arrive.

## 4. Settle each stop

Give every stop exactly one disposition:

- **Approve**: what, and the limit, for example card-hours, ¥ or $, a host, or a service.
- **Rule**: "if <observable>, then <action>; otherwise <action>". The observable is something a station can read without judgment, such as an exit code, a gate in `summary.json`, or a count.
- **Park**: it waits for the morning, and the user knows that now.

Then list the **hard stops**: things that halt work whatever the orders say. Start from the repo's rules for what the user decides. Typical hard stops are changing the goal, a dataset, conditions or decision thresholds, touching a held-out test set, changing model-visible text, top-ups, and runaway spend. Add the designers' lists.

A designer who has not replied when the timer fires gets one line: "<designer>: no reply; its line is parked until morning". Do not wait longer: the user is about to leave.

## 5. Ask the user once

Show one list in the user's language, short enough to read in two minutes. Order it like this:

1. **Approvals**, numbered, each a yes/no question with its limit.
2. **Rules**, numbered, each with its source: the designer's verbatim text marked with that designer's name, or yours.
3. **Fill order** for idle cards.
4. **Parked**: what will wait, and what each wait costs, for example "cards 4–7 idle from about 03:00".
5. **Hard stops**.
6. **Window**: until <time UTC>.

Lead with your recommendation so the user can answer in a word, for example "all yes, except 3". Apply their edits and show only the changed lines again.

## 6. Post the night orders

Post one comment on the orchestrator log ticket. Its first line follows the repo's form for recording a user decision, for example `<orchestrator signature>: user decision (night orders, until <time UTC>)`. Below it:

- the user's reply, pasted verbatim;
- the final list, numbered as the user saw it.

Use `ding night open --until <time> --body-file <file>` when the installed `ding` has it. Give a future ISO time with a timezone, such as `2026-10-31T08:00:00Z`; the UTF-8 body file contains only the verbatim reply and final list, since the command adds the first line from `[night] heading`. If the installed command is unavailable, use `gh issue comment` with that first line included. The user's verbatim reply is what makes the orders an approval. Your list only says what the reply refers to.

Tell each designer, in one line, that the night orders are posted, with the link. Then restart your watch. During the night, act on the orders as `docs/agents/` describes: every `ding log --item` you write names the order number it relied on.
