# Contributing

Contributions are welcome. PeterCodex intentionally keeps the runtime dependency surface small: Python standard library + Git + Codex CLI.

Before opening a pull request:

1. Run `python -m compileall plugins/petercodex-plugin/scripts tests`.
2. Run `python -m unittest discover -s tests -v`.
3. If you have a working Codex login, run `python tests/live_smoke.py`.
4. Do not introduce danger-full-access or `--dangerously-bypass-approvals-and-sandbox` as a default or implicit fallback.
5. Preserve explicit session-ID resume and Git-drift checks.
6. Treat timeouts and interrupted side effects as ambiguous until reconciled.

Changes to Codex CLI flags should be checked against the current upstream OpenAI Codex repository before release.
