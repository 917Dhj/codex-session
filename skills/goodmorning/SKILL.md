---
name: goodmorning
description: When the user is back, close the night orders and report what needs their decision, how the orders were used, where work stalled, and what moved. Run in the orchestrator session.
disable-model-invocation: true
---

# Goodmorning

The user is back. Their attention is the scarcest thing in the kitchen, so the report leads with what only they can do. Progress comes last. The night is over, so the night orders end now: from this moment, approvals come from the user directly again.

Words follow the repo's vocabulary in `.dkitchen.toml` and `docs/agents/`.

## 1. Close the orders

Find the latest night orders on the orchestrator log ticket, with `ding night show` or by searching the ticket. Close them before anything else, with `ding night close` or one comment: `<orchestrator signature>: night orders closed at <time UTC>`. Stations that check the orders from now on find them closed.

If no orders were posted, say so in one line and report the same way, counting the night from the user's last message.

## 2. Gather

Gather the following from the sources, for the time since the orders opened:

- the orchestrator log entries: each `--item` and the order number it relied on, and each `--pending`;
- tickets with new comments, especially those now waiting on the user or blocked;
- `ding board` and each job's state;
- the hours each card sat idle, and why: what it was waiting for. Use job start and end times from logs or run records. Mark any figure you estimated.

## 3. Report

Use the user's language and this order. Leave out a section that is empty.

1. **Decisions for you**: each a question with a link and your recommendation, so the user can answer in a word.
2. **Stalls**: what stopped, when, why, and the idle card-hours it cost. A stall that happened despite the orders is the most useful line in the report, because it shows a gap in tonight's goodnight trace.
3. **Orders used**: for each numbered order, used (where) or unused. Flag any action that went beyond an order, with its log line.
4. **Progress**: one line per ticket that moved, with a link. Results go to the designer's reading where one exists; link it rather than interpreting.
5. **Make standing?**: orders that were used and would read the same every night. An example is "the query-vector service may start on any free card". Propose each as a standing rule. On the user's yes, it goes into the repo's rules as they direct, and future goodnights stop asking it.

## 4. After the report

Record the user's answers as user decisions on their tickets, the repo's usual way, and relay them. Then restart your watch.
