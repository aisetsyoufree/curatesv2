"""Tests for cu-rates read-only health checks and CLI."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cu_rates.contracts.audit import WithdrawalEntry
from cu_rates.contracts.enums import WithdrawalScope
from cu_rates.contracts.publication import DEGRADE_AFTER, WITHDRAW_AFTER
from cu_rates.ops.backup import create_backup
from cu_rates.ops.health import HealthReport, check_health, main
from cu_rates.snapshot import Denylist, SnapshotReader

from .contracts import factories as make
from .test_public_snapshot import (
    _BIRTH,
    StoredFixture,
    release,
    setup_storage,
)
from .test_public_snapshot import (
    persisted_seed as persisted_seed,
)
from .test_public_snapshot import (
    stored as stored,
)


@dataclass
class HealthFixture:
    snapshot_root: Path
    denylist_path: Path
    denylist: Denylist
    backup_dir: Path
    database: Path
    now: datetime
    stored: StoredFixture


@pytest.fixture
def health_env(tmp_path: Path, stored: StoredFixture) -> HealthFixture:
    """Build a real snapshot, denylist, operational database, and backup."""
    # 1. Snapshot storage & real snapshot
    publisher, _reader, denylist = setup_storage(tmp_path)
    item = stored.row(now=_BIRTH)
    manifest = release("first", item)
    publisher.build(manifest, conn=stored.conn, store=stored.run.store)

    # 2. Database and artifacts
    database = stored.run.database
    artifacts_root = stored.run.store._root

    # 3. Real backup
    backup_dir = tmp_path / "backups"
    create_backup(database, artifacts_root, backup_dir, now=_BIRTH)

    return HealthFixture(
        snapshot_root=publisher.root,
        denylist_path=denylist.path,
        denylist=denylist,
        backup_dir=backup_dir,
        database=database,
        now=_BIRTH,
        stored=stored,
    )


def _hash_tree(root: Path) -> dict[str, str]:
    """Record SHA-256 digests of all files under a directory tree."""
    tree: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            tree[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return tree


def test_health_check_all_ok(health_env: HealthFixture) -> None:
    """All 5 checks pass with status 'ok' on valid storage and current timestamp."""
    report = check_health(
        snapshot_root=health_env.snapshot_root,
        denylist_path=health_env.denylist_path,
        backup_dir=health_env.backup_dir,
        database=health_env.database,
        now=health_env.now,
    )

    assert isinstance(report, HealthReport)
    assert report.ok is True
    assert len(report.checks) == 5

    check_map = {c.name: c for c in report.checks}
    assert set(check_map.keys()) == {
        "snapshot_readable",
        "snapshot_fresh",
        "denylist_readable",
        "backup_recent",
        "database_integrity",
    }
    for c in report.checks:
        assert c.status == "ok"
        assert len(c.detail) > 0


@pytest.mark.parametrize("automatic", [False, True], ids=["operator", "automatic"])
def test_health_rejects_ledgers_without_group_read(
    health_env: HealthFixture, tmp_path: Path, automatic: bool
) -> None:
    other = Denylist(tmp_path / "automatic" / "denylist.json")
    other.initialize()
    path = other.path if automatic else health_env.denylist_path
    path.chmod(0o600)  # Still readable by the health process; not by a separate web user.
    report = check_health(
        snapshot_root=health_env.snapshot_root,
        denylist_path=health_env.denylist_path,
        automatic_denylist_path=other.path,
        now=health_env.now,
    )
    name = "automatic_denylist_readable" if automatic else "denylist_readable"
    check = next(c for c in report.checks if c.name == name)
    assert not report.ok
    assert check.status == "fail"
    assert "group-readable" in check.detail
    assert "cu-rates" in check.detail


@pytest.mark.parametrize("automatic", [False, True], ids=["operator", "automatic"])
def test_health_rejects_ledger_group_mismatch(
    health_env: HealthFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, automatic: bool
) -> None:
    other = Denylist(tmp_path / "automatic" / "denylist.json")
    other.initialize()
    path = other.path if automatic else health_env.denylist_path
    real_stat = Path.stat

    def wrong_group(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        result = real_stat(self, follow_symlinks=follow_symlinks)
        if self == path:
            values = list(result)
            values[5] += 10000
            return os.stat_result(values)
        return result

    monkeypatch.setattr(Path, "stat", wrong_group)
    report = check_health(
        snapshot_root=health_env.snapshot_root,
        denylist_path=health_env.denylist_path,
        automatic_denylist_path=other.path,
        now=health_env.now,
    )
    name = "automatic_denylist_readable" if automatic else "denylist_readable"
    check = next(c for c in report.checks if c.name == name)
    assert not report.ok
    assert check.status == "fail"
    assert "group" in check.detail and "parent" in check.detail


def test_health_check_optional_components_omitted(health_env: HealthFixture) -> None:
    """When backup_dir and database are None, only snapshot and denylist checks run."""
    report = check_health(
        snapshot_root=health_env.snapshot_root,
        denylist_path=health_env.denylist_path,
        backup_dir=None,
        database=None,
        now=health_env.now,
    )

    assert report.ok is True
    check_names = tuple(c.name for c in report.checks)
    assert check_names == ("snapshot_readable", "snapshot_fresh", "denylist_readable")


def test_health_check_missing_snapshot_fails(health_env: HealthFixture, tmp_path: Path) -> None:
    """Missing snapshot root directory fails snapshot_readable and snapshot_fresh."""
    missing_root = tmp_path / "does_not_exist"
    report = check_health(
        snapshot_root=missing_root,
        denylist_path=health_env.denylist_path,
        backup_dir=health_env.backup_dir,
        database=health_env.database,
        now=health_env.now,
    )

    assert report.ok is False
    check_map = {c.name: c for c in report.checks}
    assert check_map["snapshot_readable"].status == "fail"
    assert check_map["snapshot_fresh"].status == "fail"
    assert check_map["denylist_readable"].status == "ok"


def test_health_check_stale_snapshot_warn_then_fail(
    health_env: HealthFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot freshness warns when older than DEGRADE_AFTER and fails beyond WITHDRAW_AFTER."""
    # Extend valid_until to isolate freshness/degrade policy from the fixture's 71-hour expiry
    reader = SnapshotReader(health_env.snapshot_root, health_env.denylist_path)
    initial = reader.read(at=health_env.now)
    extended_expiry = health_env.now + timedelta(hours=120)
    mocked_view = (
        initial.info,
        tuple(o.model_copy(update={"valid_until": extended_expiry}) for o in initial.offers),
    )
    monkeypatch.setattr("cu_rates.snapshot.reader.verify_database", lambda *_: mocked_view)
    monkeypatch.setattr("cu_rates.ops.health.verify_database", lambda *_: mocked_view)

    # 1. Warn threshold: age between DEGRADE_AFTER and WITHDRAW_AFTER
    now_warn = health_env.now + DEGRADE_AFTER + timedelta(hours=1)
    report_warn = check_health(
        snapshot_root=health_env.snapshot_root,
        denylist_path=health_env.denylist_path,
        backup_dir=health_env.backup_dir,
        database=health_env.database,
        now=now_warn,
    )
    assert report_warn.ok is True  # Warnings do not fail overall health
    fresh_warn = next(c for c in report_warn.checks if c.name == "snapshot_fresh")
    assert fresh_warn.status == "warn"
    assert "exceeds" in fresh_warn.detail

    # 2. Fail threshold: age beyond WITHDRAW_AFTER
    now_fail = health_env.now + WITHDRAW_AFTER + timedelta(hours=1)
    report_fail = check_health(
        snapshot_root=health_env.snapshot_root,
        denylist_path=health_env.denylist_path,
        backup_dir=health_env.backup_dir,
        database=health_env.database,
        now=now_fail,
    )
    assert report_fail.ok is False
    fresh_fail = next(c for c in report_fail.checks if c.name == "snapshot_fresh")
    assert fresh_fail.status == "fail"


def test_health_check_zero_visible_offers_while_snapshot_lists_offers_fails(
    health_env: HealthFixture,
) -> None:
    """When all offers are withdrawn by denylist, snapshot_fresh fails closed."""
    health_env.denylist.withdraw(
        WithdrawalEntry(
            schema_version=make.SV,
            withdrawal_id="withdraw-test-snapshot",
            scope=WithdrawalScope.SNAPSHOT,
            target_id="first",
            actor="operator-test",
            withdrawn_at=health_env.now,
            reason="Emergency withdrawal of test snapshot",
        )
    )

    report = check_health(
        snapshot_root=health_env.snapshot_root,
        denylist_path=health_env.denylist_path,
        backup_dir=health_env.backup_dir,
        database=health_env.database,
        now=health_env.now,
    )

    assert report.ok is False
    check_map = {c.name: c for c in report.checks}
    assert check_map["snapshot_readable"].status == "ok"
    assert check_map["snapshot_fresh"].status == "fail"
    assert "zero visible offers" in check_map["snapshot_fresh"].detail
    assert check_map["denylist_readable"].status == "ok"
    assert "1 in-force suppression(s)" in check_map["denylist_readable"].detail


def test_health_check_invalid_denylist_fails(health_env: HealthFixture) -> None:
    """Corrupted denylist file causes denylist_readable to fail."""
    health_env.denylist_path.write_text("not json content", encoding="utf-8")

    report = check_health(
        snapshot_root=health_env.snapshot_root,
        denylist_path=health_env.denylist_path,
        backup_dir=health_env.backup_dir,
        database=health_env.database,
        now=health_env.now,
    )

    assert report.ok is False
    check_map = {c.name: c for c in report.checks}
    assert check_map["denylist_readable"].status == "fail"


def test_health_check_backup_variations(health_env: HealthFixture, tmp_path: Path) -> None:
    """Covers: no backup, tampered backup, and aged backup thresholds."""
    # Case 1: No backup in directory -> fail
    empty_backup_dir = tmp_path / "empty_backups"
    empty_backup_dir.mkdir()
    report_none = check_health(
        snapshot_root=health_env.snapshot_root,
        denylist_path=health_env.denylist_path,
        backup_dir=empty_backup_dir,
        database=health_env.database,
        now=health_env.now,
    )
    assert report_none.ok is False
    assert next(c for c in report_none.checks if c.name == "backup_recent").status == "fail"

    # Case 2: Tampered backup (corrupted manifest hash) -> fail
    tampered_dir = tmp_path / "tampered_backups"
    tampered_dir.mkdir()
    # Copy valid backup into tampered_dir
    valid_backup_subdir = next(health_env.backup_dir.glob("backup-*"))
    tampered_sub = tampered_dir / valid_backup_subdir.name
    import shutil

    shutil.copytree(valid_backup_subdir, tampered_sub)
    # Corrupt a file in the backup
    db_in_backup = next(tampered_sub.glob("*.sqlite"))
    db_in_backup.write_bytes(b"corrupted backup db bytes")

    report_tampered = check_health(
        snapshot_root=health_env.snapshot_root,
        denylist_path=health_env.denylist_path,
        backup_dir=tampered_dir,
        database=health_env.database,
        now=health_env.now,
    )
    assert report_tampered.ok is False
    assert next(c for c in report_tampered.checks if c.name == "backup_recent").status == "fail"

    # Case 3: Backup older than 36h but <= 7 days -> warn
    now_backup_warn = health_env.now + timedelta(hours=48)
    report_warn = check_health(
        snapshot_root=health_env.snapshot_root,
        denylist_path=health_env.denylist_path,
        backup_dir=health_env.backup_dir,
        database=health_env.database,
        now=now_backup_warn,
    )
    assert report_warn.ok is True
    check_warn = next(c for c in report_warn.checks if c.name == "backup_recent")
    assert check_warn.status == "warn"
    assert "exceeds 36 hours" in check_warn.detail

    # Case 4: Backup older than 7 days -> fail
    now_backup_fail = health_env.now + timedelta(days=8)
    report_old_fail = check_health(
        snapshot_root=health_env.snapshot_root,
        denylist_path=health_env.denylist_path,
        backup_dir=health_env.backup_dir,
        database=health_env.database,
        now=now_backup_fail,
    )
    assert report_old_fail.ok is False
    check_fail = next(c for c in report_old_fail.checks if c.name == "backup_recent")
    assert check_fail.status == "fail"
    assert "exceeds 7 days" in check_fail.detail


def test_health_check_corrupted_database_fails(
    health_env: HealthFixture,
    tmp_path: Path,
) -> None:
    """Corrupted SQLite operational database fails database_integrity."""
    corrupt_db = tmp_path / "corrupt.sqlite"
    corrupt_db.write_bytes(b"not a valid sqlite file header")

    report = check_health(
        snapshot_root=health_env.snapshot_root,
        denylist_path=health_env.denylist_path,
        backup_dir=health_env.backup_dir,
        database=corrupt_db,
        now=health_env.now,
    )

    assert report.ok is False
    check_map = {c.name: c for c in report.checks}
    assert check_map["database_integrity"].status == "fail"


def test_health_check_never_modifies_any_file(
    health_env: HealthFixture,
    tmp_path: Path,
) -> None:
    """Health check must never write, lock, or modify any input file or directory."""
    before_hashes = _hash_tree(tmp_path)

    report = check_health(
        snapshot_root=health_env.snapshot_root,
        denylist_path=health_env.denylist_path,
        backup_dir=health_env.backup_dir,
        database=health_env.database,
        now=health_env.now,
    )
    assert report.ok is True

    after_hashes = _hash_tree(tmp_path)
    assert before_hashes == after_hashes


def test_health_check_unhandled_exception_becomes_fail(
    health_env: HealthFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any unhandled exception inside a check must be caught and recorded as a 'fail' check."""

    def _crash(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated catastrophic crash")

    monkeypatch.setattr(SnapshotReader, "read", _crash)

    report = check_health(
        snapshot_root=health_env.snapshot_root,
        denylist_path=health_env.denylist_path,
        backup_dir=health_env.backup_dir,
        database=health_env.database,
        now=health_env.now,
    )

    assert isinstance(report, HealthReport)
    assert report.ok is False
    readable_check = next(c for c in report.checks if c.name == "snapshot_readable")
    assert readable_check.status == "fail"
    assert "simulated catastrophic crash" in readable_check.detail


def test_health_check_cli_exit_codes_and_json_shape(
    health_env: HealthFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI prints one JSON object and exits 0 on success/warning, 1 on failure."""
    # 1. Success case -> exit code 0
    exit_code = main(
        [
            "--snapshot-root",
            str(health_env.snapshot_root),
            "--denylist",
            str(health_env.denylist_path),
            "--backup-dir",
            str(health_env.backup_dir),
            "--database",
            str(health_env.database),
            "--now",
            health_env.now.isoformat(),
        ]
    )
    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert isinstance(payload["checks"], list)
    assert len(payload["checks"]) == 5
    for item in payload["checks"]:
        assert "name" in item
        assert "status" in item
        assert "detail" in item

    # 2. Warning case (stale snapshot warn threshold) -> exit code 0, status is warn
    reader = SnapshotReader(health_env.snapshot_root, health_env.denylist_path)
    initial = reader.read(at=health_env.now)
    extended_expiry = health_env.now + timedelta(hours=120)
    mocked_view = (
        initial.info,
        tuple(o.model_copy(update={"valid_until": extended_expiry}) for o in initial.offers),
    )
    monkeypatch.setattr("cu_rates.snapshot.reader.verify_database", lambda *_: mocked_view)
    monkeypatch.setattr("cu_rates.ops.health.verify_database", lambda *_: mocked_view)

    now_warn = health_env.now + DEGRADE_AFTER + timedelta(hours=1)
    exit_code_warn = main(
        [
            "--snapshot-root",
            str(health_env.snapshot_root),
            "--denylist",
            str(health_env.denylist_path),
            "--backup-dir",
            str(health_env.backup_dir),
            "--database",
            str(health_env.database),
            "--now",
            now_warn.isoformat(),
        ]
    )
    assert exit_code_warn == 0
    payload_warn = json.loads(capsys.readouterr().out)
    assert payload_warn["ok"] is True
    fresh_item = next(item for item in payload_warn["checks"] if item["name"] == "snapshot_fresh")
    assert fresh_item["status"] == "warn"

    # 3. Failure case -> exit code 1
    now_fail = health_env.now + WITHDRAW_AFTER + timedelta(hours=1)
    exit_code_fail = main(
        [
            "--snapshot-root",
            str(health_env.snapshot_root),
            "--denylist",
            str(health_env.denylist_path),
            "--backup-dir",
            str(health_env.backup_dir),
            "--database",
            str(health_env.database),
            "--now",
            now_fail.isoformat(),
        ]
    )
    assert exit_code_fail == 1
    payload_fail = json.loads(capsys.readouterr().out)
    assert payload_fail["ok"] is False


def test_health_check_relative_path_fails(tmp_path: Path) -> None:
    """Relative paths must fail the check and never raise unhandled errors."""
    report = check_health(
        snapshot_root=Path("relative/snapshot"),
        denylist_path=Path("relative/denylist"),
        now=datetime.now(UTC),
    )
    assert report.ok is False
    readable = next(c for c in report.checks if c.name == "snapshot_readable")
    assert readable.status == "fail"
    assert "absolute path" in readable.detail


def _backup_check(env: HealthFixture, backup_dir: Path, now: datetime) -> object:
    report = check_health(
        snapshot_root=env.snapshot_root,
        denylist_path=env.denylist_path,
        backup_dir=backup_dir,
        database=env.database,
        now=now,
    )
    return next(c for c in report.checks if c.name == "backup_recent")


def test_a_corrupt_newest_backup_is_not_hidden_by_an_older_good_one(
    health_env: HealthFixture,
) -> None:
    """Review: only the newest backup is judged; an older valid one must not mask it."""
    newer = create_backup(
        health_env.database,
        health_env.stored.run.store._root,
        health_env.backup_dir,
        now=health_env.now + timedelta(hours=1),
    )
    (newer / health_env.database.name).write_bytes(b"corrupted newest backup")
    check = _backup_check(health_env, health_env.backup_dir, health_env.now + timedelta(hours=2))
    assert check.status == "fail"  # type: ignore[attr-defined]
    assert newer.name in check.detail  # type: ignore[attr-defined]
    assert "failed verification" in check.detail  # type: ignore[attr-defined]


def test_only_the_newest_backup_is_verified_on_each_run(
    health_env: HealthFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review: verification hashes every artifact; do not repeat it for every retained copy."""
    for hours in (1, 2):
        create_backup(
            health_env.database,
            health_env.stored.run.store._root,
            health_env.backup_dir,
            now=health_env.now + timedelta(hours=hours),
        )
    calls: list[Path] = []
    from cu_rates.ops.backup import verify_backup as real

    def counting(path: Path) -> None:
        calls.append(path)
        real(path)

    monkeypatch.setattr("cu_rates.ops.health.verify_backup", counting)
    check = _backup_check(health_env, health_env.backup_dir, health_env.now + timedelta(hours=3))
    assert check.status == "ok"  # type: ignore[attr-defined]
    assert [c.name for c in calls] == [max(p.name for p in health_env.backup_dir.glob("backup-*"))]


def test_a_directory_without_any_backups_fails(health_env: HealthFixture, tmp_path: Path) -> None:
    (tmp_path / "only-notes").mkdir()
    (tmp_path / "only-notes" / "notes.txt").write_text("not a backup")
    check = _backup_check(health_env, tmp_path / "only-notes", health_env.now)
    assert check.status == "fail"  # type: ignore[attr-defined]
