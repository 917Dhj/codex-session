# Registered artifacts

Formal artifacts are outputs other work builds on: prepared datasets, frozen request packs, results a paper will cite. Each is registered in `docs/artifacts/registry.yaml`, and tickets and messages cite it by its registered name.

```yaml
# status: current | superseded (a superseded entry names its replacement in superseded_by)
# depends_on: registered names and source files its readers verify; changing one is a frozen-file change.
- name: eval-set-v2-prepared
  path: <!-- fill: artifact root -->/issue42-eval/prepared/pack.json
  summary: '#42 prepared evaluation pack: 500 questions, deduplicated; CPU only'
  issue: 42
  status: current
  sha256: <digest of path>
  depends_on:
    - eval-set-v1-prepared
    - scripts/prepare_eval.py
```

- **Registering**: the executor that produces a formal artifact adds its entry in its own PR. New formal artifacts are written under <!-- fill: artifact root, e.g. outputs/ in the main checkout -->.
- **Merging**: the orchestrator checks the entry when merging and marks any version it replaces `superseded`.
- **Frozen files**: every `depends_on` path of a `current` entry is frozen. The review script reports PRs that change one, and the orchestrator confirms with the designer before merging. An executor whose fix would touch a frozen file stops and asks.
