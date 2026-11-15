# CU-Rates.org V2

Clean-room V2 of the educational, evidence-backed Massachusetts credit-union
home-lending directory and comparison project.

Start with [`PROJECT.md`](PROJECT.md). It defines the objective, scope, architecture,
technology stack, model responsibilities, task ledger, and contributor workflow.

## Authoritative clone and runtime location

The authoritative active clone is `~/Code/cu-rates.org-v2`, a full clone (not a
`git worktree`) outside iCloud. All integration happens there. The copy in the iCloud
folder is a checkpoint mirror updated only by the integrator after accepted commits;
it is not an implementation or runtime location. The `.venv`, caches, databases,
WAL/SHM files, Playwright caches, and raw artifacts must never live in a
cloud-synchronized folder.

## Bootstrap

Requires [`uv`](https://docs.astral.sh/uv/). It installs the pinned Python 3.12
(`.python-version`) and the locked dependencies; no global packages are needed.

```sh
git clone <repository-url> ~/Code/cu-rates.org-v2
cd ~/Code/cu-rates.org-v2
uv sync --locked
```

## Quality checks

One command runs everything and stops at the first failure:

```sh
scripts/check
```

It runs these, which can also be run individually:

```sh
uv run ruff check .
uv run mypy src tests
uv run pytest
```
