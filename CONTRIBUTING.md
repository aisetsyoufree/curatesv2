# Contributing

Read [`AGENTS.md`](AGENTS.md) and [`MASTER.md`](MASTER.md) first. In short:

1. Work from one GitHub Issue on a branch named `task/<ID>-<short-name>`.
2. Run `scripts/check` and `uv run ruff format --check .` before requesting review.
3. Never publish or invent financial data; keep the fail-closed provenance rules.
4. Record the commit or PR, test results, impact, and risks on the issue. The GitHub
   Project and Issues own task state; do not add task ledgers or review reports to the
   repository.
5. A different reviewer than the author checks risky changes; merges happen by pull
   request after checks pass.
