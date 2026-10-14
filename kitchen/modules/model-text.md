# Model-visible text

Model-visible text is everything a model under study reads: prompts, tool definitions, templates, and schemas. A change to it changes the experiment, so it has one owner, the designer, and it is fixed on the ticket before anything runs.

- **The designer writes it** on the ticket, verbatim. When a ticket says to explore first and wait for the designer before continuing, the designer may adjust the text and parameters within the approved budget after the exploration, posting the change on the ticket and telling the user afterwards. Exit thresholds and decision rules stay as they are.
- **Executors copy it verbatim** from the ticket. When the work needs new or changed text, they stop with `Status: waiting on design`.
- **Before asking for a design review**, the executor renders the requests the model will receive and runs `scripts/check_requests.py <request files> --prompt <prompt file>…` on them. It fixes what the script reports, and puts the script's last line in the comment. A finding that comes from the dataset's own text stays, and the comment names it.
- **Before merging**, when the ticket gives prompt text, the orchestrator runs `scripts/check_prompt_in_issue.py <prompt file> <ticket>` to confirm every paragraph of the prompt appears verbatim on the ticket.

<!-- fill: where prompts live in this repo, e.g. a top-level prompts/ folder, and whether the review script flags prompts added elsewhere. -->
