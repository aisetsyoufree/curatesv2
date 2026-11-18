"""A4 backup coverage, private WAL intake, and fail-closed restore regressions."""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from functools import cache
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from cu_rates.contracts.audit import WithdrawalEntry
from cu_rates.contracts.enums import WithdrawalScope
from cu_rates.db import open_operational_database
from cu_rates.ops.backup import BackupError, main, restore_backup, verify_backup
from cu_rates.snapshot import Denylist
from cu_rates.snapshot.builder import SnapshotPublisher

from .pub2_fixtures import reviewed_fixture
from .test_backup_restore import T0, T1, _init_live_env
from .test_public_snapshot import StoredFixture, release


def _entry(name: str = "one", *, hours: int = 0) -> WithdrawalEntry:
    return WithdrawalEntry(
        schema_version="1.0",
        withdrawal_id=name,
        scope=WithdrawalScope.OFFER,
        target_id=name,
        actor="test operator",
        withdrawn_at=T0 + timedelta(hours=hours),
        reason="test withdrawal",
    )


@cache
def _snapshot_files() -> dict[str, bytes]:
    # Produce a real admitted fixture release once; no invented public database format.
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        run = reviewed_fixture(root / "fixture")
        engine = open_operational_database(run.database)
        try:
            with engine.begin() as conn:
                row = StoredFixture(run, conn).row()
                publisher = SnapshotPublisher(root / "snapshots", allow_fixture=True)
                publisher.build(release("test-release", row), conn=conn, store=run.store)
        finally:
            engine.dispose()
        return {
            path.relative_to(publisher.root).as_posix(): path.read_bytes()
            for path in publisher.root.rglob("*")
            if path.is_file() and path.name != ".publisher.lock"
        }


def _sources(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "denylist-path": tmp_path / "operator" / "denylist.json",
        "automatic-denylist-path": tmp_path / "automatic" / "denylist.json",
        "intake-database": tmp_path / "private" / "intake.sqlite",
        "snapshot-root": tmp_path / "snapshots",
    }
    for key in ("denylist-path", "automatic-denylist-path"):
        ledger = Denylist(paths[key])
        ledger.initialize()
        ledger.withdraw(_entry())
    paths["intake-database"].parent.mkdir(mode=0o700)
    with closing(sqlite3.connect(paths["intake-database"])) as conn:
        conn.execute("CREATE TABLE contacts (reply TEXT)")
        conn.execute("INSERT INTO contacts VALUES ('private@example.test')")
        conn.commit()
    root = paths["snapshot-root"]
    for name, data in _snapshot_files().items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o400 if name.startswith("versions/") else 0o600)
    return paths


def _create(tmp_path: Path, sources: dict[str, Path], *, now: str | None = None) -> Path:
    database, artifacts = _init_live_env(tmp_path)
    argv = [
        "create",
        "--database",
        str(database),
        "--artifact-root",
        str(artifacts),
        "--destination",
        str(tmp_path / "backups"),
        "--now",
        now or T0.isoformat(),
    ]
    for key, path in sources.items():
        argv.extend([f"--{key}", str(path)])
    assert main(argv) == 0
    return tmp_path / "backups" / ("backup-20260105T130000Z" if now else "backup-20260105T120000Z")


def test_optional_state_round_trip_and_private_wal_intake(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    # Keep the writer open: copying the SQLite main file alone loses the committed row.
    with closing(sqlite3.connect(sources["intake-database"])) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO contacts VALUES ('wal-private@example.test')")
        writer.commit()
        backup = _create(tmp_path, sources)
    manifest = json.loads((backup / "manifest.json").read_text())
    paths = {item["path"] for item in manifest["state"]}
    assert paths == {
        "independent/denylist.json",
        "automatic/denylist.json",
        "intake/intake.sqlite",
        "snapshots/current.json",
        "snapshots/versions/test-release/published.sqlite",
        "snapshots/versions/test-release/version.json",
    }
    target = tmp_path / "restored"
    restore_backup(backup, target)
    for base in (backup, target):
        for rel in ("independent/denylist.json", "automatic/denylist.json", "intake/intake.sqlite"):
            assert (base / rel).stat().st_mode & 0o777 == 0o600
        assert (base / "intake").stat().st_mode & 0o777 == 0o700
        with closing(sqlite3.connect(base / "intake/intake.sqlite")) as conn:
            assert conn.execute("SELECT count(*) FROM contacts").fetchone() == (2,)
        assert Denylist(base / "independent/denylist.json").entries() == (_entry(),)
        assert Denylist(base / "automatic/denylist.json").entries() == (_entry(),)
        assert (base / "snapshots/current.json").read_bytes() == sources["snapshot-root"].joinpath(
            "current.json"
        ).read_bytes()
        assert (
            base / "snapshots/versions/test-release/published.sqlite"
        ).stat().st_mode & 0o222 == 0


@pytest.mark.parametrize(
    "rel",
    [
        "independent/denylist.json",
        "automatic/denylist.json",
        "intake/intake.sqlite",
        "snapshots/current.json",
        "snapshots/versions/test-release/published.sqlite",
    ],
)
def test_state_tampering_blocks_verify_restore_and_prune(tmp_path: Path, rel: str) -> None:
    backup = _create(tmp_path, _sources(tmp_path))
    newer_root = tmp_path / "newer"
    newer_root.mkdir()
    newer = _create(newer_root, _sources(newer_root))
    newer.rename(backup.parent / "backup-20260105T130000Z")
    victim = backup / rel
    victim.chmod(0o600)
    data = bytearray(victim.read_bytes())
    data[0] ^= 1
    victim.write_bytes(data)
    with pytest.raises(BackupError, match="mismatch"):
        verify_backup(backup)
    target = tmp_path / "restored"
    with pytest.raises(BackupError):
        restore_backup(backup, target)
    assert not target.exists()
    assert main(["prune", str(backup.parent), "--keep", "1"]) == 2
    assert backup.exists()


@pytest.mark.parametrize("ledger", ["independent", "automatic"])
@pytest.mark.parametrize("change", ["more", "newer", "same-count", "invalid"])
def test_restore_never_lowers_existing_ledger(tmp_path: Path, ledger: str, change: str) -> None:
    backup = _create(tmp_path, _sources(tmp_path))
    target = tmp_path / "restore"
    path = target / ledger / "denylist.json"
    live = Denylist(path)
    live.initialize()
    live.withdraw(_entry("other") if change == "same-count" else _entry())
    if change == "more":
        live.withdraw(_entry("two", hours=1))
    elif change == "newer":
        live.lift("one", at=T1, actor="test operator")
    elif change == "invalid":
        path.write_text("broken")
    before = path.read_bytes()
    assert main(["restore", str(backup), str(target), "--replace-ledgers"]) == 1
    assert path.read_bytes() == before
    assert (
        main(["restore", str(backup), str(target), "--replace-ledgers", "--allow-ledger-rollback"])
        == 0
    )
    assert live.entries() == (_entry(),)


def test_restore_overwrite_is_limited_to_ledgers(tmp_path: Path) -> None:
    backup = _create(tmp_path, _sources(tmp_path))
    target = tmp_path / "restored"
    restore_backup(backup, target)
    assert (
        main(["restore", str(backup), str(target), "--replace-ledgers", "--allow-ledger-rollback"])
        == 1
    )
    assert (target / "intake/intake.sqlite").exists()


@pytest.mark.parametrize(
    "bad",
    [
        "../../outside",
        "/tmp/outside",
        "intake/../independent/denylist.json",
        "artifacts/file",
        "intake/intake.sqlite",
    ],
)
def test_state_manifest_paths_and_aliases_rejected(tmp_path: Path, bad: str) -> None:
    backup = _create(tmp_path, _sources(tmp_path))
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["state"].append({**manifest["state"][0], "path": bad})
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(BackupError):
        verify_backup(backup)
    with pytest.raises(BackupError):
        restore_backup(backup, tmp_path / "target")


@pytest.mark.parametrize(
    "key", ["denylist-path", "automatic-denylist-path", "intake-database", "snapshot-root"]
)
def test_optional_source_missing_or_symlink_refused(tmp_path: Path, key: str) -> None:
    sources = _sources(tmp_path)
    original = sources[key]
    sources[key] = tmp_path / "missing"
    database, artifacts = _init_live_env(tmp_path)
    argv = [
        "create",
        str(database),
        str(artifacts),
        str(tmp_path / "backups"),
        f"--{key}",
        str(sources[key]),
    ]
    assert main(argv) == 1
    sources[key].symlink_to(original, target_is_directory=original.is_dir())
    assert main(argv) == 1
    assert not list((tmp_path / "backups").glob("backup-*"))


def test_intake_integrity_is_independent_of_manifest_hash(tmp_path: Path) -> None:
    backup = _create(tmp_path, _sources(tmp_path))
    path = backup / "intake/intake.sqlite"
    path.write_bytes(b"not sqlite")
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    item = next(row for row in manifest["state"] if row["path"] == "intake/intake.sqlite")
    item.update(size=path.stat().st_size, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(BackupError, match="integrity"):
        verify_backup(backup)


@pytest.mark.parametrize(
    "key", ["denylist-path", "automatic-denylist-path", "intake-database", "snapshot-root"]
)
def test_each_optional_input_works_alone(tmp_path: Path, key: str) -> None:
    sources = _sources(tmp_path)
    backup = _create(tmp_path, {key: sources[key]})
    target = tmp_path / "restored"
    restore_backup(backup, target)
    verify_backup(target)
    assert len(json.loads((target / "manifest.json").read_text())["state"]) == (
        3 if key == "snapshot-root" else 1
    )


def test_equal_or_older_destination_ledgers_can_be_replaced(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    Denylist(sources["denylist-path"]).lift("one", at=T1, actor="test operator")
    backup = _create(tmp_path, sources)
    target = tmp_path / "restored"
    for directory in ("independent", "automatic"):
        ledger = Denylist(target / directory / "denylist.json")
        ledger.initialize()
        ledger.withdraw(_entry())
    assert main(["restore", str(backup), str(target)]) == 1  # default overwrite protection
    assert main(["restore", str(backup), str(target), "--replace-ledgers"]) == 0
    assert Denylist(target / "independent/denylist.json").entries()[0].lifted_at == T1


def test_missing_backed_up_ledger_cannot_erase_survivor(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    backup = _create(tmp_path, {"intake-database": sources["intake-database"]})
    target = tmp_path / "restored"
    ledger = Denylist(target / "independent/denylist.json")
    ledger.initialize()
    ledger.withdraw(_entry())
    assert (
        main(["restore", str(backup), str(target), "--replace-ledgers", "--allow-ledger-rollback"])
        == 1
    )
    assert ledger.entries() == (_entry(),)


@pytest.mark.parametrize("kind", ["symlink-file", "symlink-directory", "hardlink", "missing"])
def test_state_file_aliases_and_missing_files_refused(tmp_path: Path, kind: str) -> None:
    backup = _create(tmp_path, _sources(tmp_path))
    victim = backup / "automatic/denylist.json"
    if kind == "symlink-directory":
        victim.unlink()
        victim.parent.rmdir()
        victim.parent.symlink_to(backup / "independent", target_is_directory=True)
    else:
        victim.unlink()
        if kind == "symlink-file":
            victim.symlink_to(backup / "independent/denylist.json")
        elif kind == "hardlink":
            victim.hardlink_to(backup / "independent/denylist.json")
    with pytest.raises(BackupError):
        verify_backup(backup)
    with pytest.raises(BackupError):
        restore_backup(backup, tmp_path / "restored")


@pytest.mark.parametrize("overlap", ["same", "inside", "parent", "symlink", "dot-parent"])
def test_restore_aliases_and_overlap_refused(tmp_path: Path, overlap: str) -> None:
    backup = _create(tmp_path, _sources(tmp_path))
    target = {
        "same": backup,
        "inside": backup / "restore",
        "parent": backup.parent,
        "symlink": tmp_path / "link",
        "dot-parent": backup / ".." / backup.name / "restore",
    }[overlap]
    if overlap == "symlink":
        target.symlink_to(tmp_path / "missing", target_is_directory=True)
    with pytest.raises(BackupError):
        restore_backup(backup, target)
    assert (backup / "manifest.json").exists()


def test_source_alias_and_destination_recursion_refused(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    database, artifacts = _init_live_env(tmp_path)
    alias = tmp_path / "alias.json"
    alias.hardlink_to(sources["denylist-path"])
    common = ["create", str(database), str(artifacts), str(tmp_path / "backups")]
    assert (
        main(
            [
                *common,
                "--denylist-path",
                str(sources["denylist-path"]),
                "--automatic-denylist-path",
                str(alias),
            ]
        )
        == 1
    )
    assert (
        main(
            [
                "create",
                str(database),
                str(artifacts),
                str(sources["snapshot-root"] / "backups"),
                "--snapshot-root",
                str(sources["snapshot-root"]),
            ]
        )
        == 1
    )


def test_snapshot_staging_ignored_but_pointer_changes_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    sources = _sources(tmp_path)
    root = sources["snapshot-root"]
    staging = root / "versions/.build-incomplete"
    staging.mkdir()
    (staging / "unfinished").write_bytes(b"incomplete")
    backup = _create(tmp_path, sources)
    assert not (backup / "snapshots/versions/.build-incomplete").exists()
    real_copy = shutil.copy2

    def changed_pointer(src: Path, dst: Path) -> Path:
        result = Path(real_copy(src, dst))
        if src == root / "versions/test-release/published.sqlite":
            pointer = root / "current.json"
            pointer.write_bytes(pointer.read_bytes() + b" ")
        return result

    monkeypatch.setattr("cu_rates.ops.backup.shutil.copy2", changed_pointer)
    database, artifacts = _init_live_env(tmp_path / "second")
    assert (
        main(
            [
                "create",
                str(database),
                str(artifacts),
                str(tmp_path / "second-backups"),
                "--snapshot-root",
                str(root),
            ]
        )
        == 1
    )
    assert not list((tmp_path / "second-backups").glob("backup-*"))


def test_private_modes_and_cli_diagnostics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import os

    sources = _sources(tmp_path)
    prior = os.umask(0)
    try:
        backup = _create(tmp_path, sources)
        for directory in (backup, *[path for path in backup.rglob("*") if path.is_dir()]):
            assert directory.stat().st_mode & 0o777 == 0o700
    finally:
        os.umask(prior)
    output = capsys.readouterr()
    assert "private@example.test" not in output.out + output.err
    sources["intake-database"].write_bytes(b"not sqlite: private@example.test")
    database, artifacts = _init_live_env(tmp_path / "second")
    assert (
        main(
            [
                "create",
                str(database),
                str(artifacts),
                str(tmp_path / "second-backups"),
                "--intake-database",
                str(sources["intake-database"]),
            ]
        )
        == 1
    )
    output = capsys.readouterr()
    assert "private@example.test" not in output.out + output.err


@pytest.mark.parametrize(
    "rel",
    [
        "independent/denylist.json",
        "automatic/denylist.json",
        "intake/intake.sqlite",
        "snapshots/current.json",
        "snapshots/versions/test-release/version.json",
    ],
)
def test_manifest_hashes_detect_valid_state_changes(tmp_path: Path, rel: str) -> None:
    backup = _create(tmp_path, _sources(tmp_path))
    path = backup / rel
    path.chmod(0o600)
    if rel == "intake/intake.sqlite":
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("UPDATE contacts SET reply='changed@example.test'")
            conn.commit()
    else:
        # Whitespace changes preserve validity and semantics, so only the manifest
        # hash/size check can detect this tampering (including valid ledger JSON).
        path.write_bytes(path.read_bytes() + b" ")
    path.chmod(0o400 if "/versions/" in rel else 0o600)
    with pytest.raises(BackupError, match="mismatch"):
        verify_backup(backup)
    assert main(["prune", str(backup.parent), "--keep", "1"]) == 2
    assert backup.exists()


@pytest.mark.parametrize("kind", ["duplicate", "artifact-alias", "normalized-alias"])
def test_hash_consistent_state_aliases_rejected(tmp_path: Path, kind: str) -> None:
    backup = _create(tmp_path, _sources(tmp_path))
    path = backup / "manifest.json"
    manifest = json.loads(path.read_text())
    item = dict(manifest["state"][0])
    if kind == "artifact-alias":
        item = dict(manifest["artifacts"][0])
        item["path"] = "artifacts/" + item["path"]
    elif kind == "normalized-alias":
        item["path"] = item["path"].replace("/", "//")
        manifest["state"].pop(0)
    manifest["state"].append(item)
    path.write_text(json.dumps(manifest))
    with pytest.raises(BackupError):
        verify_backup(backup)


def test_failed_restore_swap_preserves_surviving_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup = _create(tmp_path, _sources(tmp_path))
    target = tmp_path / "restored"
    ledger = Denylist(target / "independent/denylist.json")
    ledger.initialize()
    ledger.withdraw(_entry())
    before = ledger.path.read_bytes()
    real_rename = Path.rename

    def fail_restore(path: Path, destination: Path) -> Path:
        if path.name.startswith(".tmp-restore-"):
            raise OSError("injected rename failure")
        return real_rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_restore)
    assert main(["restore", str(backup), str(target), "--replace-ledgers"]) == 1
    assert ledger.path.read_bytes() == before
    assert not list(tmp_path.glob(".tmp-restore-*"))
    assert not list(tmp_path.glob(".previous-restore-*"))


def test_changed_state_copy_cannot_be_resealed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    sources = _sources(tmp_path)
    database, artifacts = _init_live_env(tmp_path)
    real_copy = shutil.copy2

    def corrupt_state(src: Path, dst: Path) -> Path:
        result = Path(real_copy(src, dst))
        if src == sources["denylist-path"]:
            dst.write_bytes(dst.read_bytes() + b" ")
        return result

    monkeypatch.setattr("cu_rates.ops.backup.shutil.copy2", corrupt_state)
    assert (
        main(
            [
                "create",
                str(database),
                str(artifacts),
                str(tmp_path / "backups"),
                "--denylist-path",
                str(sources["denylist-path"]),
            ]
        )
        == 1
    )
    assert not list((tmp_path / "backups").glob("*backup-*"))


def test_snapshot_pointer_must_be_regular_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    sources = _sources(tmp_path)
    pointer = sources["snapshot-root"] / "current.json"
    pointer.unlink()
    os.mkfifo(pointer, 0o600)
    database, artifacts = _init_live_env(tmp_path)
    real_read = Path.read_bytes

    def no_fifo_read(path: Path) -> bytes:
        assert path != pointer, "attempted to read a FIFO instead of rejecting it"
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", no_fifo_read)
    assert (
        main(
            [
                "create",
                str(database),
                str(artifacts),
                str(tmp_path / "backups"),
                "--snapshot-root",
                str(sources["snapshot-root"]),
            ]
        )
        == 1
    )
