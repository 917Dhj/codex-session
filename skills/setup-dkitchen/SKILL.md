---
name: setup-dkitchen
description: Install the dkitchen multi-agent workflow into this repo, adapted to its work, its resources, and the user's preferences. Run once per repo, or again to change the setup.
disable-model-invocation: true
---

# Set up dkitchen

Install the kitchen into the current repo: house rules under `docs/agents/`, pointer blocks in `AGENTS.md` and `CLAUDE.md`, the repo's `.dkitchen.toml`, and repo scripts under `scripts/`. The user is the head chef: every rule you install exists to keep their attention on direction and decisions, so tailor the rules to what this repo really does rather than copying them.

This is a prompt-driven setup. Explore, present what you found, settle each section with the user, show the draft, then write. Nothing is written or posted before the user approves the draft in step 4.

## Sources

- **Templates**: the `kitchen/` folder of the dkitchen repository, `../../kitchen/` from this skill's base directory. Read [kitchen/README.md](../../kitchen/README.md) first: it maps each template to its installed path and defines the template markers and the default vocabulary. If the folder is missing because the skill was copied on its own, clone `https://github.com/917Dhj/dkitchen` at the tag matching `ding --version` (for example `v0.2.0`) into a temporary directory and use its `kitchen/`.
- **Reference repo scripts**: [scripts/](scripts/) in this skill. Each starts with a comment listing what to adapt.
- **Renderer**: [render.py](render.py) in this skill turns the templates into rule files for a chosen set of modules.
- **`ding`**: `ding --help`, and the guide at https://github.com/917Dhj/dkitchen/blob/main/docs/ding.md, including the `.dkitchen.toml` keys.

## 1. Explore

Read the repo's starting state; assume nothing.

- **GitHub**: `git remote -v` and `gh repo view`. The kitchen runs on GitHub issues, so a repo without a GitHub remote stops here: tell the user, and offer to continue once one exists.
- **Agents and tools**: `ding --version`, which must report 0.2.0 or later (when it is missing or errors, offer `pipx install dkitchen` or `pipx upgrade dkitchen`), `claude --version`, `codex --version`, a Codex App Server socket under `/tmp/codex-daemon-$(id -u)`, `tmux -V`, `gh auth status`. The kitchen currently needs both Claude Code and Codex: Claude Code for the orchestrator, designer, and verifier, Codex for executors.
- **Existing setup**: `AGENTS.md`, `CLAUDE.md`, `.claude/CLAUDE.md`, `docs/agents/`, `.dkitchen.toml`, `.claude/settings.json`. A `<!-- dkitchen:start -->` block or a `.dkitchen.toml` means dkitchen is already installed: this run updates it in place. Files from other setups, such as Matt Pocock's `docs/agents/issue-tracker.md`, are merged, never overwritten.
- **The work**: the README, the main code, how tests run (CI files, `pyproject.toml`, `package.json`, `Makefile`), and the signals for each module in [Modules](#modules): model calls and paid APIs, prompts, training or evaluation scripts, output folders other work builds on, long jobs.
- **Resources**: `nvidia-smi` or the equivalent, other hosts mentioned in docs or SSH config, shared services such as model servers and the tunnels to them.
- **Language**: the language the user writes to you in, and the language of the repo's docs.

Done when you can name, with evidence, each prerequisite's state, every existing file you would touch, the repo's test command, and a recommendation for each module.

## 2. Present the findings

Summarize what is present and what is missing in a short list. Missing prerequisites come first, each with the command that fixes it. When Claude Code or Codex is unavailable, say plainly that this version of dkitchen needs both, and ask whether to stop or to install the rules anyway.

## 3. Settle each section

Take the sections in order: one section, one answer, then the next. Lead each with your recommendation so the user can accept it in a word, and skip any section that exploration already settled.

**A. Language.** Recommend the language the user writes to you in. For any language other than English, the installed rules are written in that language, and the vocabulary in [kitchen/README.md](../../kitchen/README.md#vocabulary) (station names, signature separator, status fields and values, the word for nobody, the log's pending label and night-order markers) is translated once and used identically in the rules and in `.dkitchen.toml`. Show the translated vocabulary as a table and get it confirmed: scripts match these words exactly.

**B. Stations.** Recommend the full kitchen (orchestrator, designer, verifier, executors) for research repos. The minimal kitchen is an orchestrator and executors: the orchestrator also writes tickets and interprets results, the designer's reply format is dropped, and the orchestrator runs the verifier's checks itself. Ask which model each Claude Code station runs; the defaults are the strongest available model for the orchestrator and designer and a cheaper one for the verifier.

**C. What stays with the user.** Show the default list from the roles template (research direction, how datasets are used, what the paper claims, spending beyond a ticket's budget) and ask what to add or remove. For a repo that is not a research project, rewrite the first three in the repo's own terms.

**D. Modules.** Recommend each module from the signals you found, as a multi-select:

| Module | Recommend when |
| --- | --- |
| `run-budgets` | The repo runs models on GPUs or calls paid APIs |
| `model-text` | The repo studies or depends on what models are told: prompts, tool definitions, request templates |
| `artifacts` | Outputs such as prepared datasets or result packs are built on by later work and must stay frozen |
| `unattended` | The user wants the kitchen to keep moving while they are away |

**E. Resources.** Fill the `<!-- fill: … -->` gaps of the chosen templates: hosts and GPUs and how to tell one is free, shared services with their owners and tunnels, the pre-approved run limit for `run-budgets` (default 500 samples or calls), the artifact root for `artifacts`, where prompts live for `model-text`. Propose values from exploration and let the user correct them.

**F. Repo scripts.** Propose how `scripts/review_pr.py` runs this repo's tests and prepares a trial-merge worktree, and which module scripts to install: `check_requests.py` and `check_prompt_in_issue.py` for `model-text`. For `run-budgets`, a ledger script only if the repo already saves per-request receipts; otherwise drop the ledger paragraph.

Done when every section has an answer from the user, or was settled by exploration and you said so.

## 4. Draft and confirm

Show the user:

- the list of files to create or change, with the full text of each new `docs/agents/` file and a diff for each existing file;
- the `.dkitchen.toml`, holding only keys that differ from the defaults, plus `[log] issue` for `unattended`;
- the adapted repo scripts;
- the GitHub changes: the four `status:*` labels from [kitchen/core/issue-tracker.md](../../kitchen/core/issue-tracker.md), and for `unattended` a pinned "Orchestrator log" ticket;
- an optional `.claude/settings.json` allow list for the commands the stations run all day: `ding *`, `gh issue *`, `gh pr *`, and the repo scripts.

Let the user edit anything before you write. Done when the user approves the draft, including each GitHub change by name.

## 5. Write

Apply exactly the approved draft.

**Rules.** Render the templates with this skill's [render.py](render.py), which keeps the chosen modules' marked text, drops the rest, removes every module marker, and prints the two pointer blocks:

```bash
python <this skill's base directory>/render.py --modules run-budgets,unattended --out docs/agents
```

Then edit the rendered files:

- Fill every `<!-- fill: … -->` with the answers from section E. Where a gap does not apply, such as a repo with no tunnels or shared services, remove the sentence or section around it, and reword the sentences that still mention it.
- For a language other than English, translate every file, using the confirmed vocabulary word for word.
- For the minimal kitchen, rewrite the designer's and verifier's duties into the orchestrator's as settled in section B, and drop the sections that only concern them.
- When a `docs/agents/` file already exists from another setup, keep its sections that dkitchen does not cover, and replace or add dkitchen's.

**Pointer blocks.** Wrap each block from [kitchen/core/agents-block.md](../../kitchen/core/agents-block.md) in `<!-- dkitchen:start -->` and `<!-- dkitchen:end -->`, replacing an existing block in place. Put the first block in `AGENTS.md`, creating the file if needed, because Codex reads `AGENTS.md`. Put the second in the `CLAUDE.md` Claude Code already loads (root or `.claude/`), creating a root `CLAUDE.md` if there is none, and make sure it imports `AGENTS.md` (`@AGENTS.md`, or `@../AGENTS.md` from `.claude/`).

**Settings and scripts.** Write `.dkitchen.toml` and the repo scripts, then make the approved GitHub changes with `gh label create` and, for `unattended`, `gh issue create` and `gh issue pin`, writing the new ticket's number into `[log] issue`.

Done when every approved file exists with its approved content and every approved GitHub change is made.

## 6. Verify

Run each check and fix what fails:

- `grep -rn '<!-- module:\|<!-- /module:\|<!-- fill:' docs/agents AGENTS.md CLAUDE.md .claude/CLAUDE.md` finds nothing.
- Every relative link in the installed files resolves to an existing file.
- `ding board --status` runs in the repo, and `ding watch --help` prints help.
- Each installed script runs with `--help`, and `scripts/review_pr.py` imports cleanly.
- `gh label list --search status:` shows the four labels.

Done when every check passes, or the user has been told exactly which one fails and why.

## 7. Hand over

Tell the user, briefly:

1. which sessions to open: the orchestrator, designer, and verifier in Claude Code, executors in Codex (one per ticket, in its own worktree);
2. to put those session names into the table at the top of `docs/agents/roles.md`;
3. how to start: describe the first piece of work to the designer, who writes it as a ticket;
4. for `unattended`, that the orchestrator starts `ding watch` in the background at the end of each round;
5. that the rules are theirs to edit, and rerunning `/setup-dkitchen` changes modules, stations, or language.

## Modules

Signals to look for in step 1:

- `run-budgets`: GPU training or evaluation scripts, API keys or clients for model providers (OpenAI, Anthropic, vLLM endpoints), cost or token accounting.
- `model-text`: prompt files or folders, request templates, tool or function schemas, code that renders chat messages.
- `artifacts`: an `outputs/` or `results/` folder referenced by later scripts, dataset preparation stages, digests or manifests of outputs.
- `unattended`: long jobs, several parallel workstreams, or the user saying they want progress while away.
