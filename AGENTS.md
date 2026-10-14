# Development

- Install with `python -m pip install -e '.[dev]'`.
- Run `python -m pytest tests/test_codex_session.py tests/test_records.py tests/test_kitchen.py tests/test_reference_scripts.py` after code changes.
- Tests must not send messages to real Codex sessions or write to GitHub.
