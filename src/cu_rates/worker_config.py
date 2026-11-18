"""Collection-worker configuration and a read-only check of it (issue #115).

Live collection is not approved (D-15/D-18) and the worker unit stays masked, so this module does
NOT start a worker. It names every path the worker needs, builds the policy registry from them
WITHOUT creating anything, and verifies that each one is present, readable and sane, so that the
day collection is approved there is one place that says what is wired where, and a missing or
unreadable piece fails closed instead of being created on demand.

    python -m cu_rates.worker_config check        # reads the process environment, prints JSON
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from cu_rates.fetch_policy import FetchRegistry, TermsLedger
from cu_rates.snapshot import Denylist

# environment variable -> the path's role
SETTINGS = (
    "OPERATIONAL_DB",
    "TERMS_LEDGER_PATH",
    "DENYLIST_PATH",
    "AUTOMATIC_DENYLIST_PATH",
    "WORKER_STATE_DIR",
    "ARTIFACT_ROOT",
)


class WorkerConfigError(ValueError):
    """A setting is missing or malformed; the worker must not start."""


@dataclass(frozen=True)
class WorkerPaths:
    operational_db: Path
    terms_ledger: Path  # operator-written, worker read-only
    denylist: Path  # operator-owned independent ledger, worker read-only
    automatic_denylist: Path  # worker-owned automatic suppressions (issue #118)
    state_dir: Path  # worker-private scheduling state
    artifact_root: Path  # content-addressed raw evidence


def _absolute(name: str, value: str | None) -> Path:
    if (
        value is None
        or value == ""
        or not value.startswith("/")
        or value.startswith("//")
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
        or ".." in PurePosixPath(value).parts
    ):
        raise WorkerConfigError(f"{name} must be set to an absolute path without traversal")
    return Path(value)


def paths_from_environment(environ: Mapping[str, str] | None = None) -> WorkerPaths:
    """Every path is required: the worker never falls back to a default location."""
    source = os.environ if environ is None else environ
    values = {name: _absolute(name, source.get(name)) for name in SETTINGS}
    paths = WorkerPaths(
        operational_db=values["OPERATIONAL_DB"],
        terms_ledger=values["TERMS_LEDGER_PATH"],
        denylist=values["DENYLIST_PATH"],
        automatic_denylist=values["AUTOMATIC_DENYLIST_PATH"],
        state_dir=values["WORKER_STATE_DIR"],
        artifact_root=values["ARTIFACT_ROOT"],
    )
    if paths.denylist == paths.automatic_denylist:
        raise WorkerConfigError(
            "AUTOMATIC_DENYLIST_PATH must differ from DENYLIST_PATH: the operator ledger is "
            "read-only to the worker, so its own blocks need a ledger of its own"
        )
    return paths


def build_registry(paths: WorkerPaths) -> FetchRegistry:
    """The worker's policy view. Construction opens and creates nothing."""
    return FetchRegistry(
        paths.operational_db,
        TermsLedger(paths.terms_ledger),
        Denylist(paths.denylist),
        automatic=Denylist(paths.automatic_denylist),
    )


@dataclass(frozen=True)
class Check:
    name: str
    status: Literal["ok", "fail"]
    detail: str


@dataclass(frozen=True)
class ConfigReport:
    checks: tuple[Check, ...]

    @property
    def ok(self) -> bool:
        return all(check.status == "ok" for check in self.checks)


def _private_directory(path: Path) -> str | None:
    try:
        info = path.lstat()
    except OSError:
        return "does not exist"
    if not stat.S_ISDIR(info.st_mode):
        return "is not a real directory"
    if info.st_mode & 0o077:
        return "is accessible to group or others"
    if info.st_uid != os.getuid():
        return "is not owned by the current user"
    return None


def check_config(paths: WorkerPaths) -> ConfigReport:
    """Read-only. Never creates, repairs or writes any of the paths; never raises."""
    checks: list[Check] = []

    def run(name: str, probe: object) -> None:
        try:
            problem = probe()  # type: ignore[operator]
        except Exception as exc:  # any failure is a failed check, never a crash
            problem = f"{type(exc).__name__}: {exc}"
        checks.append(
            Check(name, "ok" if problem is None else "fail", "ok" if problem is None else problem)
        )

    def database() -> str | None:
        if not paths.operational_db.is_file():
            return "operational database does not exist"
        with closing(
            sqlite3.connect(f"file:{paths.operational_db}?mode=ro", uri=True)
        ) as connection:
            connection.execute("SELECT 1 FROM source_approvals LIMIT 1")
        return None

    def terms() -> str | None:
        TermsLedger(paths.terms_ledger).read()
        return None

    def ledger(path: Path) -> str | None:
        Denylist(path).entries()
        return None

    run("operational_database", database)
    run("terms_ledger", terms)
    run("operator_denylist", lambda: ledger(paths.denylist))
    run("automatic_denylist", lambda: ledger(paths.automatic_denylist))
    run("state_directory", lambda: _private_directory(paths.state_dir))
    run("artifact_root", lambda: _private_directory(paths.artifact_root))
    run(
        "ledgers_are_separate",
        lambda: (
            None
            if paths.denylist.parent != paths.automatic_denylist.parent
            else "the operator and automatic ledgers share a directory"
        ),
    )
    return ConfigReport(tuple(checks))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments != ["check"]:
        print("usage: python -m cu_rates.worker_config check", file=sys.stderr)
        return 2
    try:
        report = check_config(paths_from_environment())
    except WorkerConfigError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                "ok": report.ok,
                "checks": [
                    {"name": c.name, "status": c.status, "detail": c.detail} for c in report.checks
                ],
            }
        )
    )
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
