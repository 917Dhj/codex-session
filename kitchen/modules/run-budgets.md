# Run budgets and approvals

Model runs cost money and GPU time, so each ticket that runs models works inside one budget the user approved. Inside it, executors move without asking; outside it, they stop.

## Run requests

A comment asking to run models adds these fields right after the status block:

```text
Scope: <which data, how many samples, which stages>
Model calls: <count>
Endpoint: <model and where it is served>
Concurrency: <parallel requests>
Cost cap: <the pause line, and how it was computed>
GPU hours: <GPUs × hours>
```

## What needs approval

- **Ticket budget**: a ticket that runs models states one budget for the whole ticket (model calls, cost cap, GPU hours), and the user approves it once on the ticket. Within it, each stage starts as soon as its request comment is posted, and the comment gives the running total against the budget. Going over the budget, or changing the model or endpoint, falls under the rules below.
- **Cost cap**: set the pause line at or above the expected cost, plus what requests already in flight can still spend (concurrency × the cost of one request), plus a margin. Show that sum in the request.
- **Hosted API calls**: wait for the user's approval.
- **GPU inference or evaluation of at most <!-- fill: limit, e.g. 500 --> samples or model calls**: approved in advance when the code is merged on main, it runs on our GPUs ([long-running-jobs.md](long-running-jobs.md#compute)) against a service that is already running, and the request comment is posted before starting. Report the result when it finishes.
- **Training, service starts, and anything larger**: wait for the user's approval.

## Checking the numbers

The verifier checks a ticket's premises before dispatch and spot-checks requests before approval, as in [roles.md](roles.md#verification). Spending reported on a ticket is recomputed from the saved receipts with the repo's ledger script, never by hand. <!-- fill: the ledger script and how to call it, or drop this paragraph if the repo keeps no receipts. -->
