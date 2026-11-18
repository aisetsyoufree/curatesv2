"""Tests for operational backup and restore tooling."""

import fcntl
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

from cu_rates.artifact_store import ArtifactStore
from cu_rates.db.engine import open_operational_database
from cu_rates.db.migrate import upgrade_database
from cu_rates.ops.backup import (
    BackupError,
    create_backup,
    main,
    prune_backups,
    restore_backup,
    verify_backup,
)

T0 = datetime(2026, 1, 5, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)
T3 = T0 + timedelta(hours=3)


def _init_live_env(tmp_path: Path) -> tuple[Path, Path]:
    """Initialize a migrated operational database and artifact store."""
    live_dir = tmp_path / "live"
    live_dir.mkdir(parents=True, exist_ok=True)
    live_dir.chmod(0o700)

    db_path = live_dir / "cu-rates.db"
    upgrade_database(db_path)

    # Insert a record via the operational engine to ensure data persistence
    engine = open_operational_database(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO institutions ("
                "charter_number, schema_version, legal_name, status, "
                "successor_charter_number, charter_verified"
                ") VALUES (12345, '1.0', 'Test Credit Union', 'active', NULL, 1)"
            )
        )
    engine.dispose()

    art_root = live_dir / "artifacts"
    store = ArtifactStore(art_root)
    store.put(
        b"<html>evidence sample 1</html>",
        media_type="text/html; charset=utf-8",
        source_url="https://example.test/rates1",
        fetched_at=T0,
        http_status=200,
        fetch_run_id="run-1",
    )
    store.put(
        b"<html>evidence sample 2</html>",
        media_type="text/html; charset=utf-8",
        source_url="https://example.test/rates2",
        fetched_at=T1,
        http_status=200,
        fetch_run_id="run-2",
    )

    return db_path, art_root


def test_round_trip(tmp_path: Path) -> None:
    """Test full create, verify, restore round-trip with real migrated database and artifacts."""
    db_path, art_root = _init_live_env(tmp_path)
    dest = tmp_path / "backups"

    backup_dir = create_backup(db_path, art_root, dest, now=T0)
    assert backup_dir.is_dir()
    assert (backup_dir.stat().st_mode & 0o777) == 0o700
    assert backup_dir.name == "backup-20260105T120000Z"

    manifest_path = backup_dir / "manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["database"]["integrity_check"] == "ok"
    assert len(manifest["artifacts"]) == 4  # 2 blobs + 2 observation json records

    # Verification passes
    verify_backup(backup_dir)

    # Restore to empty target
    target = tmp_path / "restored"
    restore_backup(backup_dir, target)

    assert target.is_dir()
    assert (target.stat().st_mode & 0o777) == 0o700

    restored_db = target / db_path.name
    assert restored_db.is_file()
    assert (restored_db.stat().st_mode & 0o777) == 0o600

    restored_artifacts = target / "artifacts"
    assert restored_artifacts.is_dir()
    assert (restored_artifacts.stat().st_mode & 0o777) == 0o700

    # Restored database opens and contains our data
    restored_engine = open_operational_database(restored_db)
    with restored_engine.connect() as conn:
        res = conn.execute(
            text("SELECT legal_name FROM institutions WHERE charter_number = 12345")
        ).scalar_one()
        assert res == "Test Credit Union"
    restored_engine.dispose()

    # Restored artifact store reads observations and bodies cleanly
    restored_store = ArtifactStore(restored_artifacts)
    obs = list(restored_store.observations())
    assert len(obs) == 2
    assert restored_store.read(obs[0]) == b"<html>evidence sample 1</html>"
    assert restored_store.read(obs[1]) == b"<html>evidence sample 2</html>"


def test_tampering_artifact_byte_fails_verify(tmp_path: Path) -> None:
    db_path, art_root = _init_live_env(tmp_path)
    backup_dir = create_backup(db_path, art_root, tmp_path / "backups", now=T0)

    # Corrupt one artifact file
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    first_art_rel = manifest["artifacts"][0]["path"]
    art_file = backup_dir / "artifacts" / first_art_rel
    data = bytearray(art_file.read_bytes())
    data[0] ^= 0xFF
    art_file.chmod(0o600)
    art_file.write_bytes(data)
    art_file.chmod(0o400)

    with pytest.raises(BackupError, match="Artifact SHA-256 mismatch"):
        verify_backup(backup_dir)


def test_tampering_db_file_fails_verify(tmp_path: Path) -> None:
    db_path, art_root = _init_live_env(tmp_path)
    backup_dir = create_backup(db_path, art_root, tmp_path / "backups", now=T0)

    db_file = backup_dir / db_path.name
    data = bytearray(db_file.read_bytes())
    data[100] ^= 0xFF
    db_file.write_bytes(data)

    with pytest.raises(BackupError, match="Database SHA-256 mismatch"):
        verify_backup(backup_dir)


def test_tampering_manifest_fails_verify(tmp_path: Path) -> None:
    db_path, art_root = _init_live_env(tmp_path)
    backup_dir = create_backup(db_path, art_root, tmp_path / "backups", now=T0)

    manifest_file = backup_dir / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["database"]["size"] += 1
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupError, match="Database size mismatch"):
        verify_backup(backup_dir)


def test_extra_file_fails_verify(tmp_path: Path) -> None:
    db_path, art_root = _init_live_env(tmp_path)
    backup_dir = create_backup(db_path, art_root, tmp_path / "backups", now=T0)

    extra = backup_dir / "untracked_file.txt"
    extra.write_text("rogue", encoding="utf-8")

    with pytest.raises(BackupError, match="Extra file rejected in backup"):
        verify_backup(backup_dir)

    extra.unlink()

    extra_in_artifacts = backup_dir / "artifacts" / "rogue.txt"
    extra_in_artifacts.write_text("rogue", encoding="utf-8")
    with pytest.raises(BackupError, match="Extra file rejected in backup"):
        verify_backup(backup_dir)


def test_symlink_fails_verify(tmp_path: Path) -> None:
    db_path, art_root = _init_live_env(tmp_path)
    backup_dir = create_backup(db_path, art_root, tmp_path / "backups", now=T0)

    symlink_file = backup_dir / "link_to_db"
    symlink_file.symlink_to(backup_dir / db_path.name)

    with pytest.raises(BackupError, match=r"Symlink.*rejected"):
        verify_backup(backup_dir)


def test_restore_refuses_non_empty_target(tmp_path: Path) -> None:
    db_path, art_root = _init_live_env(tmp_path)
    backup_dir = create_backup(db_path, art_root, tmp_path / "backups", now=T0)

    target = tmp_path / "target_dir"
    target.mkdir()
    (target / "existing.txt").write_text("obstacle", encoding="utf-8")

    with pytest.raises(BackupError, match="Target directory exists and is non-empty"):
        restore_backup(backup_dir, target)


def test_corrupted_database_backup_refused(tmp_path: Path) -> None:
    _, art_root = _init_live_env(tmp_path)
    corrupted_db = tmp_path / "corrupted.db"
    # Write a broken SQLite header/page
    corrupted_db.write_bytes(b"SQLite format 3\x00" + b"\xff" * 2000)

    with pytest.raises(BackupError):
        create_backup(corrupted_db, art_root, tmp_path / "backups", now=T0)


def test_lock_contention_raises(tmp_path: Path) -> None:
    db_path, art_root = _init_live_env(tmp_path)
    dest = tmp_path / "backups"
    dest.mkdir(parents=True, exist_ok=True)
    lock_path = dest / ".backup.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    try:
        with pytest.raises(BackupError, match="Another backup holds the lock"):
            create_backup(db_path, art_root, dest, now=T0)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_prune_backups_preserves_newest_valid_and_ignores_foreign(tmp_path: Path) -> None:
    db_path, art_root = _init_live_env(tmp_path)
    dest = tmp_path / "backups"

    b0 = create_backup(db_path, art_root, dest, now=T0)
    b1 = create_backup(db_path, art_root, dest, now=T1)
    b2 = create_backup(db_path, art_root, dest, now=T2)

    # Corrupt the oldest backup b0
    (b0 / db_path.name).write_bytes(b"corrupted")

    # Add foreign directory and file to destination
    foreign_dir = dest / "other_dir"
    foreign_dir.mkdir()
    foreign_file = dest / "notes.txt"
    foreign_file.write_text("leave me alone", encoding="utf-8")
    lock_file = dest / ".backup.lock"
    assert lock_file.exists()

    # Prune with keep=1
    pruned = prune_backups(dest, keep=1, now=T3)

    # b2 (newest valid) must NOT be deleted
    assert b2.exists()
    verify_backup(b2)

    # Non-backup directory, file, and lock file must be untouched
    assert foreign_dir.is_dir()
    assert foreign_file.is_file()
    assert lock_file.is_file()

    # The older VALID backup is pruned; the corrupt one is kept for human inspection
    # (automatic deletion could destroy the only recoverable copy of something).
    assert b0.exists()
    assert b0 not in pruned
    assert not b1.exists()
    assert b1 in pruned

    with pytest.raises(BackupError, match=r"keep must be >= 1"):
        prune_backups(dest, keep=0, now=T3)


def test_create_backup_rejects_symlink_in_artifact_store(tmp_path: Path) -> None:
    db_path, art_root = _init_live_env(tmp_path)
    symlink_in_store = art_root / "blobs" / "bad_link"
    symlink_in_store.symlink_to(db_path)

    with pytest.raises(BackupError, match=r"Symlink.*rejected"):
        create_backup(db_path, art_root, tmp_path / "backups", now=T0)


def test_backup_inside_artifact_root_or_db_dir_refused(tmp_path: Path) -> None:
    db_path, art_root = _init_live_env(tmp_path)

    # Destination inside artifact root
    dest_in_art = art_root / "backups"
    with pytest.raises(BackupError, match="Destination cannot be inside artifact_root"):
        create_backup(db_path, art_root, dest_in_art, now=T0)

    # Destination inside database directory
    dest_in_db = db_path.parent / "backups"
    with pytest.raises(BackupError, match="Destination cannot be inside the database directory"):
        create_backup(db_path, art_root, dest_in_db, now=T0)


def test_cli_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, art_root = _init_live_env(tmp_path)
    dest = tmp_path / "cli_backups"

    # CLI create
    ret = main(
        [
            "create",
            "--database",
            str(db_path),
            "--artifact-root",
            str(art_root),
            "--destination",
            str(dest),
            "--now",
            T0.isoformat(),
        ]
    )
    assert ret == 0
    captured = capsys.readouterr()
    assert "Created backup:" in captured.out

    backup_dir = dest / "backup-20260105T120000Z"
    assert backup_dir.is_dir()

    # CLI verify
    ret = main(["verify", str(backup_dir)])
    assert ret == 0
    assert "Verified backup:" in capsys.readouterr().out

    # CLI restore
    target = tmp_path / "cli_restored"
    ret = main(["restore", str(backup_dir), str(target)])
    assert ret == 0
    assert "Restored backup to:" in capsys.readouterr().out
    assert (target / db_path.name).is_file()

    # CLI prune
    ret = main(["prune", str(dest), "--keep", "1"])
    assert ret == 0
    assert "Pruned 0 backup(s)" in capsys.readouterr().out

    # CLI error handling on invalid argument
    ret = main(["verify", str(tmp_path / "nonexistent")])
    assert ret == 1
    assert "BackupError:" in capsys.readouterr().err


def _rewrite_manifest(backup_dir: Path, mutate: "object") -> None:
    manifest_file = backup_dir / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    mutate(manifest)  # type: ignore[operator]
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")


def _entry_for(path: Path, name: str) -> dict[str, object]:
    import hashlib

    data = path.read_bytes()
    return {
        "path": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "created_at": T0.isoformat(),
    }


@pytest.mark.parametrize("kind", ["parent-traversal", "absolute"])
def test_manifest_artifact_paths_cannot_escape_the_backup(tmp_path: Path, kind: str) -> None:
    """A tampered manifest with correct hashes must not read or write outside the backup."""
    db_path, art_root = _init_live_env(tmp_path)
    backup_dir = create_backup(db_path, art_root, tmp_path / "backups", now=T0)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"not part of any backup")
    name = "../../outside.bin" if kind == "parent-traversal" else str(outside)
    _rewrite_manifest(backup_dir, lambda m: m["artifacts"].append(_entry_for(outside, name)))
    with pytest.raises(BackupError, match="unsafe"):
        verify_backup(backup_dir)
    target = tmp_path / "restored"
    with pytest.raises(BackupError, match="unsafe"):
        restore_backup(backup_dir, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".tmp-restore-*"))


def test_manifest_database_name_cannot_be_a_path(tmp_path: Path) -> None:
    db_path, art_root = _init_live_env(tmp_path)
    backup_dir = create_backup(db_path, art_root, tmp_path / "backups", now=T0)
    outside = tmp_path / "outside.db"
    outside.write_bytes((backup_dir / db_path.name).read_bytes())

    def mutate(m: dict[str, dict[str, object]]) -> None:
        m["database"]["filename"] = "../../outside.db"
        m["database"]["path"] = "../../outside.db"

    _rewrite_manifest(backup_dir, mutate)
    with pytest.raises(BackupError, match="unsafe"):
        verify_backup(backup_dir)


def test_an_artifact_entry_cannot_alias_a_file_outside_the_artifacts_directory(
    tmp_path: Path,
) -> None:
    db_path, art_root = _init_live_env(tmp_path)
    backup_dir = create_backup(db_path, art_root, tmp_path / "backups", now=T0)
    _rewrite_manifest(
        backup_dir,
        lambda m: m["artifacts"].append(_entry_for(backup_dir / db_path.name, db_path.name)),
    )
    with pytest.raises(BackupError, match="Artifact file missing"):
        verify_backup(backup_dir)


def test_a_copy_that_differs_from_its_source_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil as real_shutil

    db_path, art_root = _init_live_env(tmp_path)
    dest = tmp_path / "backups"

    def corrupting_copy(src: Path, dst: Path) -> object:
        real_shutil.copyfile(src, dst)
        with open(dst, "ab") as handle:
            handle.write(b"x")
        return dst

    monkeypatch.setattr("cu_rates.ops.backup.shutil.copy2", corrupting_copy)
    with pytest.raises(BackupError, match="copy"):
        create_backup(db_path, art_root, dest, now=T0)
    assert not list(dest.glob("backup-*"))
    assert not list(dest.glob(".tmp-backup-*"))


def test_cli_prune_reports_corrupt_backups_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path, art_root = _init_live_env(tmp_path)
    dest = tmp_path / "backups"
    old = create_backup(db_path, art_root, dest, now=T0)
    create_backup(db_path, art_root, dest, now=T1)
    (old / db_path.name).write_bytes(b"corrupted")
    assert main(["prune", "--destination", str(dest), "--keep", "5"]) == 2
    assert str(old) in capsys.readouterr().err
    assert old.exists()


def test_restore_refuses_a_tampered_backup_and_creates_nothing(tmp_path: Path) -> None:
    db_path, art_root = _init_live_env(tmp_path)
    backup_dir = create_backup(db_path, art_root, tmp_path / "backups", now=T0)
    victim = next(p for p in (backup_dir / "artifacts").rglob("*") if p.is_file())
    victim.chmod(0o600)
    victim.write_bytes(victim.read_bytes() + b"tampered")
    target = tmp_path / "restored"
    with pytest.raises(BackupError, match="mismatch"):
        restore_backup(backup_dir, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".tmp-restore-*"))


def test_a_hash_consistent_but_internally_corrupt_database_fails_verification(
    tmp_path: Path,
) -> None:
    """The integrity check is independent of the hashes: re-sealing corruption is not enough."""
    import hashlib

    db_path, art_root = _init_live_env(tmp_path)
    backup_dir = create_backup(db_path, art_root, tmp_path / "backups", now=T0)
    backed_up = backup_dir / db_path.name
    data = bytearray(backed_up.read_bytes())
    for offset in range(4096, min(len(data), 4096 + 512)):
        data[offset] = 0xFF
    backed_up.write_bytes(bytes(data))

    def reseal(m: dict[str, dict[str, object]]) -> None:
        m["database"]["sha256"] = hashlib.sha256(bytes(data)).hexdigest()
        m["database"]["size"] = len(data)

    _rewrite_manifest(backup_dir, reseal)
    with pytest.raises(BackupError, match="integrity"):
        verify_backup(backup_dir)
