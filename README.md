# CU-Rates.org V2

[![check](https://github.com/aisetsyoufree/curatesv2/actions/workflows/check.yml/badge.svg)](https://github.com/aisetsyoufree/curatesv2/actions/workflows/check.yml)

Educational, evidence-backed Massachusetts credit-union home-lending directory and
comparison project.

- Read [`MASTER.md`](MASTER.md) for the product, architecture, policies, stack, roles,
  and delivery rules.
- Use the [GitHub Project](https://github.com/users/aisetsyoufree/projects/1) and
  [Issues](https://github.com/aisetsyoufree/curatesv2/issues) for all stories and
  execution status.

The pilot uses operator-led, AI-assisted research and extraction, with independent
checking against saved lender evidence. The website and publication gate do not use a
runtime model. Offline model batches can be staged as private candidates only; the
publication hookup is separate, and a workbook cannot publish rates. See
`MASTER.md` decision D-23 and the
[release roadmap](https://github.com/aisetsyoufree/curatesv2/issues/157).

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
| `src/cu_rates/fetch_worker.py`, `fetch_robots.py`, `fetch_dialer.py` | Policy-gated collector and pinned dialer (live collection disabled) |
| `src/cu_rates/adapters/` | Rate source adapters (static HTML v2, registry, normalization) |
| `src/cu_rates/normalize/`, `validation/` | Deterministic normalization and validation/quarantine |
| `src/cu_rates/db/`, `alembic/` | Operational SQLite schema and frozen migrations |
| `src/cu_rates/roster/` | Source-versioned institution roster import |
| `src/cu_rates/rate_batch/` | Offline private candidate intake and evidence checks |
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

## Offline rate batch intake

`cu_rates.rate_batch` provides bounded intake for offline model-extracted candidate
rates and coverage findings (M4 / decision D-23). It reconciles candidates against
identity bindings in the operational database and verifies field-level evidence locators
against the private Content-Addressed Evidence Store (`ArtifactStore`).

Verified items remain **candidate-only**; intake cannot approve, publish, or synthesize
evidence. Rows with ambiguous, unsupported, or unverified evidence fail closed to auditable
holds with stable reason codes.

Pass UTF-8 JSON or JSONL at schema version `1.0`, up to 10 MiB and 5,000 items.
Compute the envelope digest with `cu_rates.rate_batch.compute_batch_digest` before
adding `batch_digest`. Supply decimal strings and the actual UTC capture time;
use `points_not_applicable` only for an explicit, located source disclosure.
Record source, charter, run and artifact IDs from existing private evidence. The
database must already be migrated, and the artifact store must already exist.
Use candidate and finding IDs that are unique to the capture and row.
Never attach financial data, lender captures, or batch payloads to a public issue.

```sh
# Dry run reconciliation
uv run python -m cu_rates.rate_batch batch.json \
  --database operational.sqlite --store evidence/ --dry-run

# Candidate-only staging
uv run python -m cu_rates.rate_batch batch.json \
  --database operational.sqlite --store evidence/
```

- **Replay**: Re-importing identical batch bytes is an idempotent no-op. Reusing an existing
  `batch_id` with altered contents raises a conflict error.
- **Transactions**: Mid-import errors roll back completely; no partial SQLite state is left.
- **Dry run**: SQLite is opened read-only. It may manage normal WAL sidecars, but no
  batch rows, schema, or artifact-store directories are created.
- **Isolation**: Public tables (`normalized_offers`, `products`, `fetch_runs`, `source_approvals`)
  are never mutated by batch intake.
