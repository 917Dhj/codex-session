<p align="center">
  <img src="https://raw.githubusercontent.com/917Dhj/dkitchen/main/docs/assets/dkitchen.png" width="240" alt="Two pixel-art robot chefs: the big one rings a service bell, the small one answers with a pan">
</p>

<h1 align="center">dkitchen</h1>

<p align="center">
  <b>English</b> | <a href="https://github.com/917Dhj/dkitchen/blob/main/README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <b>You decide what to cook. Your agents run the kitchen.</b> 🧑‍🍳
</p>

<p align="center">
  <a href="https://pypi.org/project/dkitchen/"><img src="https://img.shields.io/pypi/v/dkitchen" alt="PyPI"></a>
  <a href="https://pypi.org/project/dkitchen/"><img src="https://img.shields.io/pypi/pyversions/dkitchen" alt="Python"></a>
  <a href="https://github.com/917Dhj/dkitchen/actions/workflows/tests.yml"><img src="https://github.com/917Dhj/dkitchen/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <a href="https://github.com/917Dhj/dkitchen/blob/main/LICENSE"><img src="https://img.shields.io/github/license/917Dhj/dkitchen" alt="License"></a>
  <a href="https://x.com/dahuoji09"><img src="https://img.shields.io/badge/follow-%40dahuoji09-000000?logo=x" alt="X: @dahuoji09"></a>
</p>

dkitchen is the multi-agent workflow I use every day to run my research repo. It gives a set of Claude Code and Codex sessions fixed stations, a shared way to hand off work through GitHub issues, clear rules for when to stop and ask you, and the tools that tie it together. You keep the decisions only you can make: what to research, what to try, what to spend.

## 🍽️ Why a kitchen

AI agents made execution cheap. The bottleneck moved to you.

With one agent, you supervise every step. With several, you become the message bus: copying context between sessions, checking whether a job finished, remembering which session owns which task, approving small things all day. Your GPUs sit idle between runs because nobody noticed the last one ended. By evening you have touched everything and thought about nothing.

A Michelin kitchen solves the same problem. The head chef designs the menu and tastes the dishes. Everyone else has a station, every order is a ticket, and a bell rings when a dish is ready. The head chef never chops onions, and never loses track of the kitchen.

dkitchen sets up your agents the same way. You are the head chef.

## 🧑‍🍳 The kitchen

<p align="center">
  <img src="https://raw.githubusercontent.com/917Dhj/dkitchen/main/docs/assets/kitchen.gif" width="100%" alt="A pixel-art kitchen: you talk a design through with the designer, the verifier checks the ticket, the orchestrator rings the bell to start a Codex executor on free GPUs, reviews and merges a PR, and brings you one decision to approve">
</p>

| Station | In a kitchen | What it does | Runs on |
| --- | --- | --- | --- |
| 👑 **You** | Head chef | Choose the direction, approve designs and budgets, decide what the results mean for your paper. | You |
| 🤖 **Orchestrator** | Sous chef at the pass | Schedules tickets, GPUs, and concurrency. Relays messages, reviews and merges PRs, reports facts to you. | Claude Code · Claude Opus 5.5 |
| 🤔 **Designer** | Recipe developer | Writes tickets, designs methods and every piece of text a model will see, interprets results. | Claude Code · Claude Opus 5.5 |
| 🔍 **Verifier** | Taster | Checks facts against code and data before money is spent. Reports what it found and never makes the call. | Claude Code · Claude Sonnet 5.5 |
| 🔪 **Executors** | Line cooks | One fresh session per ticket, in its own worktree. Implements, runs, and reports on the ticket. | Codex · GPT-6.1 Sol |

Each station has written rules for what it owns, what it hands off, and when it must stop. When something fits no station, the agents ask you.

## 🎫 How a ticket moves through the kitchen

1. **You and the designer agree on what to do.** The designer writes it up as a GitHub issue: the goal, the exact steps, and the budget the work may spend.
2. **The verifier checks the ticket's premises** against the code and data: field names, dataset ranges, call counts, cost estimates.
3. **The orchestrator dispatches it** to a new executor session and picks free GPUs.
4. **The executor works and reports on the ticket.** Every comment opens with the same four lines: status, who it is waiting on, the key numbers, and what happens next. Long jobs start with `ding launch`; when the job exits, `ding` wakes the executor so it can read the output and continue.
5. **Results go to the designer** for interpretation. The orchestrator reviews the PR, merges it, and logs what it did and why.
6. **You read a short report** and make the next call.

Every decision, request, and result lives on a ticket, so any session (or you, a week later) can pick up the story from GitHub.

## ✋ What stays with you

The rules write down which calls belong to the head chef:

- the research direction, how datasets are used, and what the paper claims;
- model runs, paid API calls, or service starts beyond what a ticket's approved budget covers.

Agents propose on these and wait. An approval only counts when it is in your own words. Neither the orchestrator nor the designer can approve on your behalf.

Everything else the kitchen handles on its own, by one rule: **if anyone would do it the same way, the agents do it. If the choice changes the results, the cost, or the direction, they stop and wait for you.**

This is what lets you walk away. While you are in class, asleep, or reading papers, an issue watcher wakes the orchestrator when an executor finishes a stage, gets stuck, or goes quiet; when you leave a comment; or when a PR opens. The orchestrator moves forward what is already approved and leaves a list of decisions for when you are back.

## ⚖️ One pool of resources

A research repo has more resources than any single agent sees: GPUs on several servers, a shared model server, CPU time, an API budget, tokens, and hours in the day. In dkitchen the orchestrator, running on the strongest model you have, manages them as one pool.

- 🖥️ **Compute.** It schedules tickets across free GPUs and checks the shared model server's load before raising concurrency.
- 💰 **Money.** Each ticket that runs models carries one approved budget for calls, cost, and GPU hours. Stages within the budget start right away, and anything beyond it comes back to you.
- 🪙 **Tokens.** One session per ticket keeps each context small, and fact-checking runs on a cheaper model.
- ⏱️ **Time.** Jobs wake their owner when they exit, so nothing waits for someone to look.

## 🧰 The tools

### 🛎️ `ding`, the bell

`ding` passes messages between agent sessions and wakes a session when its long job ends.

```bash
ding new --cwd . --issue 42 --name 'Ablation' --message-file brief.md   # start a Codex session for #42; prints its UUID
ding board                                            # who is working on what, in this repo
ding send '#42' --from orchestrator --message-file note.md
ding launch train -- python train.py                  # run in tmux, wake me when it exits
```

You can address a session by its UUID, its name, or a GitHub issue such as `'#42'`; `ding` finds the session that owns that ticket. On Linux, `ding board` also lists long-running processes that were started without `ding`, so you can spot jobs nobody will be woken for.

`ding` talks to Codex sessions through the Codex App Server. The full guide is in [docs/ding.md](https://github.com/917Dhj/dkitchen/blob/main/docs/ding.md).

### 📋 The kitchen's routines

The stations' everyday steps are `ding` commands too, so they run the same careful way every time:

- `ding relay` sends a message to a ticket's executor, and records it on the ticket only once delivery is confirmed.
- `ding merge` merges a PR only at the HEAD that was reviewed, and only once the ticket's executor is waiting on the orchestrator to merge it.
- `ding watch` waits in the orchestrator's background and wakes it when a ticket needs attention.
- `ding waits` lists overdue unanswered waits, including ones still pending after a prior watch batch.
- `ding log` keeps the orchestrator's log of what it did while you were away.
- `ding night` opens, shows, and closes time-limited night orders on that log. Run `/goodnight` and `/goodmorning` in the orchestrator session to collect the orders and report their use.
- `ding check-refs` checks every file, line, and digest the verifier cites.

### 📜 The house rules

The rules every station follows live in [kitchen/](https://github.com/917Dhj/dkitchen/blob/main/kitchen/README.md): who owns what, how tickets are written, when to stop and ask, and optional modules for run budgets, model prompts, registered artifacts, and unattended work.

## 🚀 Get started

### Requirements

- Linux or another POSIX system, with `tmux`
- Python 3.10 or later
- A GitHub repo and the `gh` CLI
- Claude Code, and Codex with its App Server running under the same user

### 1. Install `ding`

```bash
pipx install dkitchen
```

### 2. Add the setup skill to Claude Code

```bash
claude plugin marketplace add 917Dhj/dkitchen
claude plugin install dkitchen@dkitchen
```

### 3. Set up your kitchen

In Claude Code, inside your repo, run:

```text
/setup-dkitchen
```

The skill reads your repo to learn what you work on and what you already have: agent instructions, tests, GPUs, model calls, long jobs. Then it asks, one topic at a time, about language, stations, the calls that stay with you, optional modules, and your compute. It shows you every rule, script, and GitHub change it plans to make, and writes them only after you approve. Run it again any time to change the setup.

## ❓ FAQ

**Is it safe to let agents run unattended?**
They stop for anything that changes direction, results, or spending, and approvals only come from your own words on a ticket. Every action is a comment or a PR on GitHub, so you can audit the night's work over coffee.

**Will it fit my repo?**
The rules were written for computational research, and most of them carry over to any repo with long-running jobs. The setup skill adapts them to your repo.

**Do I need both Claude Code and Codex?**
For now, yes. That is the combination I run every day.

**Isn't this too much process for a small project?**
Start with the orchestrator and executors, then add the designer and verifier when you want them.

## 💡 About

The *d* is my mark. dkitchen is how I run my own kitchen, written down so you can run yours. It started in my research repo, which moved 171 issues and 111 merged PRs through this workflow in its first four weeks.

The Michelin kitchen metaphor comes from Lauren Tan, known online as [Poteto](https://github.com/poteto). Poteto's way of running agents inspired much of this workflow. The way tickets live on GitHub issues started from the issue tracker setup in [Matt Pocock's skills](https://github.com/mattpocock/skills). Thank you both. 🙏

## License

[MIT](https://github.com/917Dhj/dkitchen/blob/main/LICENSE)
