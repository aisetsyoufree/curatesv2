# CU-Rates.org V2

[![check](https://github.com/aisetsyoufree/curatesv2/actions/workflows/check.yml/badge.svg)](https://github.com/aisetsyoufree/curatesv2/actions/workflows/check.yml)

Educational, evidence-backed Massachusetts credit-union home-lending directory and
comparison project.

- Read [`MASTER.md`](MASTER.md) for the product, architecture, policies, stack, roles,
  and delivery rules.
- Use the [GitHub Project](https://github.com/users/aisetsyoufree/projects/1) and
  [Issues](https://github.com/aisetsyoufree/curatesv2/issues) for all stories and
  execution status.

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

Formatting is checked separately in CI and before every merge:

```sh
uv run ruff format --check .
```

## Project layout

| Path | Contents |
|---|---|
| `src/cu_rates/contracts/` | Versioned domain contracts, taxonomy, fingerprints, and the publication gate |
| `src/cu_rates/artifact_store.py` | Immutable, content-addressed raw evidence store |
| `src/cu_rates/fetch_worker.py`, `fetch_robots.py` | Policy-gated collector (live transport disabled) |
| `src/cu_rates/normalize/`, `validation/` | Deterministic normalization and validation/quarantine |
| `src/cu_rates/db/`, `alembic/` | Operational SQLite schema and frozen migrations |
| `src/cu_rates/roster/` | Source-versioned institution roster import |
| `src/cu_rates/pipeline/` | Offline vertical slice (slice-only extractor) |
| `src/cu_rates/snapshot/` | Atomic public snapshot builder and reader, when merged |
| `src/cu_rates/web/` | Server-rendered website foundation and draft educational articles |
| `tests/` | Offline tests and synthetic fixtures (no live requests) |
| `scripts/check` | The quality gate |

## Governance

- [`MASTER.md`](MASTER.md) is the canonical specification; [`AGENTS.md`](AGENTS.md) and
  [`CONTRIBUTING.md`](CONTRIBUTING.md) describe how work is done.
- Task status lives only in the GitHub Project and Issues; pull requests and commits hold
  the code and review history.
- Security issues: see [`SECURITY.md`](SECURITY.md). Licensing: see [`LICENSE`](LICENSE) and
  `MASTER.md` decision D-06.
