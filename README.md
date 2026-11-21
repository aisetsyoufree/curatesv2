# CU-Rates.org V2

[![check](https://github.com/aisetsyoufree/curatesv2/actions/workflows/check.yml/badge.svg)](https://github.com/aisetsyoufree/curatesv2/actions/workflows/check.yml)

Educational, evidence-backed Massachusetts credit-union home-lending directory and
comparison project.

- Read [`MASTER.md`](MASTER.md) for the product, architecture, policies, stack, roles,
  and delivery rules.
- Use the [GitHub Project](https://github.com/users/aisetsyoufree/projects/1) and
  [Issues](https://github.com/aisetsyoufree/curatesv2/issues) for all stories and
  execution status.

The planned pilot uses operator-led, AI-assisted research and extraction, with
independent checking against saved lender evidence. The default branch includes
the website and a synthetic offline pipeline, not a verified real-lender release.
Candidate batch intake is tracked in [issue #159](https://github.com/aisetsyoufree/curatesv2/issues/159)
and is not part of the default branch. The website and publication gate do not
use a runtime model. AI workbooks are research inputs, not publishable data.
See `MASTER.md` decision D-23.

## Authoritative clone and runtime location

Use a full authoritative clone outside cloud-synchronized folders. An iCloud copy
is optional and never an implementation or runtime location. GitHub holds the
code and task history; a fresh clone does not recover private databases or
lender evidence. Keep the `.venv`, caches, databases, WAL/SHM files, Playwright
caches, and raw artifacts outside cloud-synchronized folders.

## Bootstrap

Requires [`uv`](https://docs.astral.sh/uv/). It installs the pinned Python 3.12
(`.python-version`) and the locked dependencies; no global packages are needed.

```sh
git clone https://github.com/aisetsyoufree/curatesv2.git ~/Code/cu-rates.org-v2
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
| `src/cu_rates/fetch_worker.py`, `fetch_robots.py`, `fetch_dialer.py` | Policy-gated collector and pinned dialer (live collection disabled) |
| `src/cu_rates/adapters/` | Rate source adapters (static HTML v2, registry, normalization) |
| `src/cu_rates/normalize/`, `validation/` | Deterministic normalization and validation/quarantine |
| `src/cu_rates/db/`, `alembic/` | Operational SQLite schema and frozen migrations |
| `src/cu_rates/roster/` | Source-versioned institution roster import |
| `src/cu_rates/pipeline/` | Offline vertical slice (slice-only extractor) |
| `src/cu_rates/snapshot/` | Atomic public snapshot builder, reader, publisher, and denylists |
| `src/cu_rates/web/` | Server-rendered website (comparison views reading sealed snapshots, `/credit-unions` profile pages, draft articles) |
| `src/cu_rates/intake/` | Private correction inbox database, triage CLI, and contact purging |
| `src/cu_rates/ops/` | Operational backup/restore, health inspection, and data quality checks |
| `src/cu_rates/review/` | Private operator review console CLI and local review server |
| `tests/` | Offline tests and synthetic fixtures (no live requests) |
| `scripts/check` | The quality gate |

## Governance

- [`MASTER.md`](MASTER.md) is the canonical specification; [`AGENTS.md`](AGENTS.md) and
  [`CONTRIBUTING.md`](CONTRIBUTING.md) describe how work is done.
- Task status lives only in the GitHub Project and Issues; pull requests and commits hold
  the code and review history.
- Branches under `archive/` preserve reviewed or unfinished work without merging it.
  Check the linked issue before resuming an archive. An archive branch is not a release
  or authorization to deploy, collect, or publish.
- Security issues: see [`SECURITY.md`](SECURITY.md). Licensing: see [`LICENSE`](LICENSE) and
  `MASTER.md` decision D-06.

## Offline registry validation

`cu_rates.registry_validation` rebuilds untrusted registry rows through the existing
schema and contracts and returns a typed, canonically ordered report with stable
reason codes and a SHA-256 digest. It performs no DNS or HTTP requests, migrations,
or database writes. Use a checkpointed offline database copy; a nonempty WAL is
rejected so immutable reads cannot silently miss pending changes.

```sh
uv run python -m cu_rates.registry_validation registry.sqlite \
  --as-of 2026-11-17T12:00:00+00:00 --output registry-report.json
```

The explicit evaluation time makes repeated reports reproducible. Standard output
contains `report` and `sha256`; exit status is 1 when findings exist, 0 otherwise.
`--output` is optional and is the only file written. Coverage counts one slot per
institution and product family: `absent` means uncataloged, `unknown` means unverified,
`verified` means a fresh evidenced product classification, and `blocked`,
`not_offered`, `no_public_rate`, `quote_only`, `login_required`, and `stale` remain
separate. Classification degrades after 72 hours; offer validity beyond 96 hours is
invalid. Coverage verification never authorizes publication.

MA scope relies on the existing MA-filtered roster import and its stored raw-file
hash. The frozen schema cannot retain a human identity-review citation or a direct
relationship-evidence link: verified flags therefore produce explicit missing-proof
findings. The validator does not invent those proofs, fetch the roster, or read raw
private artifact bytes; evidence checks establish stored reference consistency only.
