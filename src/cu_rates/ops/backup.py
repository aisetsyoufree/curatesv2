"""Private, verified recovery bundles for operational and optional publication state."""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import uuid
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from cu_rates.contracts.audit import WithdrawalEntry
from cu_rates.snapshot import Denylist, SnapshotError
from cu_rates.snapshot._database import read_version, verify_database

_LEDGER_PATHS = frozenset({"independent/denylist.json", "automatic/denylist.json"})


class BackupError(Exception):
    """Raised when backup, verification, restore, or prune fails."""


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _file_size(path: Path) -> int:
    return path.stat().st_size


def _safe_member(name: object, *, plain: bool = False) -> Path:
    """A manifest-supplied relative path that cannot leave the directory it is joined to.

    The manifest is untrusted input (a backup may be tampered with while still hashing
    correctly), so absolute paths, parent traversal, empty parts and NUL are refused;
    ``plain`` additionally requires a bare file name with no directory part.
    """
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name:
        raise BackupError(f"unsafe path in manifest: {name!r}")
    path = Path(name)
    parts = path.parts
    if path.is_absolute() or not parts or any(part in {"", ".", ".."} for part in parts):
        raise BackupError(f"unsafe path in manifest: {name!r}")
    if plain and len(parts) != 1:
        raise BackupError(f"unsafe path in manifest: {name!r}")
    if path.as_posix() != name:
        raise BackupError(f"unsafe aliased path in manifest: {name!r}")
    return path


def _check_no_symlinks(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)
        if dp.is_symlink():
            raise BackupError(f"Symlink directory rejected: {dp}")
        for d in dirnames:
            p = dp / d
            if p.is_symlink():
                raise BackupError(f"Symlink directory rejected: {p}")
        for f in filenames:
            p = dp / f
            if p.is_symlink():
                raise BackupError(f"Symlink file rejected: {p}")


def _require_regular(path: Path) -> None:
    # lstat before resolving, including intermediate components. Never follow an alias
    # supplied as an optional input or a restore target.
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise BackupError("Symlink path rejected")
    try:
        metadata = path.stat()
        regular = stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1
    except OSError as exc:
        raise BackupError("State file missing or inaccessible") from exc
    if not regular:
        raise BackupError("State input is not a regular file")


def _sqlite_integrity(path: Path) -> None:
    try:
        conn = sqlite3.connect(path.as_uri() + "?immutable=1&mode=ro", uri=True)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        # In particular, never include intake data or SQLite diagnostic payloads.
        raise BackupError("Database integrity check failed") from exc
    if not ok:
        raise BackupError("Database integrity check failed")


def _backup_sqlite(source: Path, target: Path) -> None:
    # Pre-create privately: a permissive caller umask must never expose reply contacts.
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error as exc:
        raise BackupError("Database online backup failed") from exc
    target.chmod(0o600)
    _sqlite_integrity(target)


def _copy_state(source: Path, target: Path, *, sealed: bool = False) -> None:
    _require_regular(source)
    before = _sha256_file(source)
    shutil.copy2(source, target)
    target.chmod(0o400 if sealed else 0o600)
    if _sha256_file(target) != before or _sha256_file(source) != before:
        raise BackupError("State changed or was corrupted during copy")


def _state_path(name: object) -> Path:
    rel = _safe_member(name)
    if rel.as_posix() in _LEDGER_PATHS | {"intake/intake.sqlite", "snapshots/current.json"}:
        return rel
    if (
        len(rel.parts) == 4
        and rel.parts[:2] == ("snapshots", "versions")
        and not rel.parts[2].startswith(".")
        and rel.parts[3] in {"published.sqlite", "version.json"}
    ):
        return rel
    raise BackupError("unsafe state path in manifest")


def _ledger_entries(path: Path) -> dict[str, WithdrawalEntry]:
    try:
        return {row.withdrawal_id: row for row in Denylist(path).entries()}
    except (OSError, SnapshotError, ValueError) as exc:
        raise BackupError("Withdrawal ledger missing or invalid") from exc


def _check_snapshots(root: Path) -> None:
    try:
        pointer = read_version(root / "current.json")
        selected = root / "versions" / _safe_member(pointer.snapshot_id, plain=True)
        if read_version(selected / "version.json") != pointer:
            raise BackupError("Snapshot pointer does not match its version")
        for directory in (root / "versions").iterdir():
            version = read_version(directory / "version.json")
            if directory.name != version.snapshot_id:
                raise BackupError("Snapshot version directory does not match descriptor")
            verify_database(directory, version)
    except (OSError, SnapshotError, ValueError) as exc:
        raise BackupError("Snapshot state missing or invalid") from exc


def _create_state(temp: Path, inputs: dict[str, Path]) -> list[dict[str, object]]:
    names = {
        "denylist_path": "independent/denylist.json",
        "automatic_denylist_path": "automatic/denylist.json",
        "intake_database": "intake/intake.sqlite",
    }
    for key, name in names.items():
        if key not in inputs:
            continue
        target = temp / name
        target.parent.mkdir(mode=0o700)
        if key == "intake_database":
            _backup_sqlite(inputs[key], target)
        else:
            _copy_state(inputs[key], target)
            _ledger_entries(target)
    if "snapshot_root" in inputs:
        source = inputs["snapshot_root"]
        _check_no_symlinks(source)
        _require_regular(source / "current.json")
        pointer = (source / "current.json").read_bytes()
        target = temp / "snapshots"
        target.mkdir(mode=0o700)
        (target / "versions").mkdir(mode=0o700)
        # Published versions are immutable. Ignore the publisher lock and uncommitted
        # .build-* staging directories; they are not activated releases.
        for directory in sorted((source / "versions").iterdir()):
            if directory.name.startswith(".build-"):
                continue
            if not directory.is_dir():
                raise BackupError("Unexpected snapshot version member")
            if {p.name for p in directory.iterdir()} != {"published.sqlite", "version.json"}:
                raise BackupError("Unexpected file in committed snapshot version")
            for name in ("published.sqlite", "version.json"):
                dst = target / "versions" / directory.name / name
                dst.parent.mkdir(mode=0o700, exist_ok=True)
                _copy_state(directory / name, dst, sealed=True)
        _copy_state(source / "current.json", target / "current.json")
        if (target / "current.json").read_bytes() != pointer or (
            source / "current.json"
        ).read_bytes() != pointer:
            raise BackupError("Snapshot pointer changed during backup; retry")
        _check_snapshots(target)
    records = []
    for path in sorted(temp.rglob("*")):
        rel = path.relative_to(temp)
        if path.is_file() and rel.parts[0] in {"independent", "automatic", "intake", "snapshots"}:
            records.append(
                {"path": rel.as_posix(), "sha256": _sha256_file(path), "size": path.stat().st_size}
            )
    return records


def _verify_state(backup: Path, raw: object) -> set[Path]:
    if not isinstance(raw, list):
        raise BackupError("Missing state list in manifest")
    expected: set[Path] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise BackupError("Invalid state item in manifest")
        rel = _state_path(item.get("path"))
        path = backup / rel
        if path in expected:
            raise BackupError("Duplicate state path in manifest")
        _require_regular(path)
        if _file_size(path) != item.get("size") or _sha256_file(path) != item.get("sha256"):
            raise BackupError("State hash or size mismatch")
        if rel.as_posix() == "intake/intake.sqlite":
            _sqlite_integrity(path)
        if rel.as_posix() in _LEDGER_PATHS:
            _ledger_entries(path)
        expected.add(path)
    if any(path.is_relative_to(backup / "snapshots") for path in expected):
        _check_snapshots(backup / "snapshots")
    return expected


def _restore_ledger_guard(backup: Path, target: Path, *, allow_rollback: bool) -> None:
    for name in _LEDGER_PATHS:
        current, saved = target / name, backup / name
        if not current.exists():
            continue
        _require_regular(current)
        if not saved.is_file():
            raise BackupError("Existing ledger has no backed-up replacement")
        if allow_rollback:
            continue
        old = _ledger_entries(current)
        new = _ledger_entries(saved)
        for key, row in old.items():
            # A lift may be added in a newer backup, but never removed or changed.
            # All other withdrawal fields are immutable. Counts/timestamps alone
            # cannot detect disjoint histories or changed suppression targets.
            candidate = new.get(key)
            if candidate is None:
                raise BackupError(
                    "Restore would lower withdrawal ledger; explicit rollback required"
                )
            # Values are WithdrawalEntry records; avoid dumping them in diagnostics.
            if row != candidate and (
                row.lifted_at is not None
                or row.model_dump(exclude={"lifted_at", "lifted_by"})
                != candidate.model_dump(exclude={"lifted_at", "lifted_by"})
            ):
                raise BackupError(
                    "Restore would lower withdrawal ledger; explicit rollback required"
                )


def _check_restore_target(
    backup: Path, target: Path, *, replace_ledgers: bool, allow_rollback: bool
) -> None:
    if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
        raise BackupError("Symlink restore target rejected")
    target = target.resolve()
    if target == backup or target.is_relative_to(backup) or backup.is_relative_to(target):
        raise BackupError("Restore target cannot overlap backup")
    if not target.exists():
        return
    if not target.is_dir():
        raise BackupError(f"Target exists and is not a directory: {target}")
    _check_no_symlinks(target)
    if not any(target.iterdir()):
        return
    if not replace_ledgers:
        raise BackupError(f"Target directory exists and is non-empty: {target}")
    for path in target.rglob("*"):
        rel = path.relative_to(target).as_posix()
        if not (
            (path.is_dir() and rel in {"independent", "automatic"})
            or (
                path.is_file()
                and rel
                in _LEDGER_PATHS
                | {"independent/.denylist.json.lock", "automatic/.denylist.json.lock"}
            )
        ):
            raise BackupError("Target directory exists and is non-empty beyond ledgers")
    _restore_ledger_guard(backup, target, allow_rollback=allow_rollback)


def create_backup(
    database: Path,
    artifact_root: Path,
    destination: Path,
    *,
    now: datetime,
    denylist_path: Path | None = None,
    automatic_denylist_path: Path | None = None,
    intake_database: Path | None = None,
    snapshot_root: Path | None = None,
) -> Path:
    """Create a private backup, optionally including ledgers, intake and snapshot state.

    Parameters
    ----------
    database:
        Absolute path to the live operational SQLite database.
    artifact_root:
        Absolute path to the content-addressed artifact store root.
    destination:
        Absolute path to the destination directory where backup-<timestamp> is written.
    now:
        Timestamp used for naming the backup directory and manifest timestamp.
    """
    database = Path(database).resolve()
    artifact_root = Path(artifact_root).resolve()
    destination = Path(destination).resolve()

    inputs = {
        key: path.absolute()
        for key, path in {
            "denylist_path": denylist_path,
            "automatic_denylist_path": automatic_denylist_path,
            "intake_database": intake_database,
            "snapshot_root": snapshot_root,
        }.items()
        if path is not None
    }
    try:
        for key, path in inputs.items():
            if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
                raise BackupError("Optional input must be an absolute path without symlinks")
            path = path.resolve()
            inputs[key] = path
            root = path if key == "snapshot_root" else path.parent
            if destination == root or destination.is_relative_to(root):
                raise BackupError("Destination cannot be inside a state input directory")
            if key == "snapshot_root":
                if not path.is_dir():
                    raise BackupError("Snapshot root does not exist")
            else:
                _require_regular(path)
        files = [database, *(path for key, path in inputs.items() if key != "snapshot_root")]
        for index, path in enumerate(files):
            if any(path.samefile(other) for other in files[:index]):
                raise BackupError("Backup inputs cannot alias each other")
    except OSError as exc:
        raise BackupError("State input missing or inaccessible") from exc
    if database.name in {
        "manifest.json",
        "artifacts",
        "independent",
        "automatic",
        "intake",
        "snapshots",
    }:
        raise BackupError("Database filename aliases a reserved backup member")

    if not database.is_file():
        raise BackupError(f"Operational database does not exist: {database}")
    if not artifact_root.is_dir():
        raise BackupError(f"Artifact root does not exist: {artifact_root}")

    db_dir = database.parent
    if destination == artifact_root or destination.is_relative_to(artifact_root):
        raise BackupError(f"Destination cannot be inside artifact_root: {destination}")
    if destination == db_dir or destination.is_relative_to(db_dir):
        raise BackupError(f"Destination cannot be inside the database directory: {destination}")

    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.chmod(0o700)

    # Acquire exclusive non-blocking lock on destination/.backup.lock
    lock_path = destination / ".backup.lock"
    try:
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise BackupError(f"Failed to open lock file {lock_path}: {exc}") from exc

    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise BackupError(f"Another backup holds the lock on {destination}: {exc}") from exc

        now_utc = now.astimezone(UTC) if now.tzinfo is not None else now.replace(tzinfo=UTC)
        timestamp_str = now_utc.strftime("%Y%m%dT%H%M%SZ")
        final_dir = destination / f"backup-{timestamp_str}"
        if final_dir.exists():
            raise BackupError(f"Backup directory already exists: {final_dir}")

        temp_dir = destination / f".tmp-backup-{uuid.uuid4().hex}"
        temp_dir.mkdir(mode=0o700)

        try:
            # Both databases use the online API, including committed WAL pages.
            db_filename = database.name
            temp_db_path = temp_dir / db_filename
            _backup_sqlite(database, temp_db_path)
            integrity_result = "ok"

            db_sha256 = _sha256_file(temp_db_path)
            db_size = _file_size(temp_db_path)

            # 2. Copy artifacts preserving relative path
            temp_artifacts_dir = temp_dir / "artifacts"
            temp_artifacts_dir.mkdir(mode=0o700, exist_ok=True)

            _check_no_symlinks(artifact_root)

            artifact_records: list[dict[str, object]] = []
            for dirpath, _, filenames in os.walk(artifact_root):
                dp = Path(dirpath)
                for f in sorted(filenames):
                    src_file = dp / f
                    if not src_file.is_file() or src_file.is_symlink():
                        continue
                    rel_path = src_file.relative_to(artifact_root)
                    dst_file = temp_artifacts_dir / rel_path
                    dst_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    source_sha = _sha256_file(src_file)
                    shutil.copy2(src_file, dst_file)
                    dst_file.chmod(0o400)

                    file_sha = _sha256_file(dst_file)
                    file_size = _file_size(dst_file)
                    if file_sha != source_sha or _sha256_file(src_file) != source_sha:
                        raise BackupError(
                            f"artifact changed or was corrupted during copy: {rel_path}"
                        )
                    st = src_file.stat()
                    created_at = datetime.fromtimestamp(st.st_mtime, UTC).isoformat()
                    artifact_records.append(
                        {
                            "path": rel_path.as_posix(),
                            "sha256": file_sha,
                            "size": file_size,
                            "created_at": created_at,
                        }
                    )

            artifact_records.sort(key=lambda r: str(r["path"]))

            state = _create_state(temp_dir, inputs)
            # Legacy calls remain v1; optional state requires a v2-aware verifier.
            manifest = {
                "version": 2 if inputs else 1,
                "created_at": now_utc.isoformat(),
                "database": {
                    "filename": db_filename,
                    "path": db_filename,
                    "sha256": db_sha256,
                    "size": db_size,
                    "integrity_check": integrity_result,
                },
                "artifacts": artifact_records,
            }
            if inputs:
                manifest["state"] = state
            manifest_path = temp_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            manifest_path.chmod(0o600)

            for directory in temp_dir.rglob("*"):
                if directory.is_dir():
                    directory.chmod(0o700)
            verify_backup(temp_dir)
            # Atomically rename into place
            temp_dir.rename(final_dir)
            _sync_dir(destination)
            final_dir.chmod(0o700)
            return final_dir

        except Exception:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
    finally:
        with suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        with suppress(OSError):
            os.close(lock_fd)


def verify_backup(backup_dir: Path) -> None:
    """Verify a backup directory against its manifest.

    Parameters
    ----------
    backup_dir:
        Path to the backup directory to verify.

    Raises
    ------
    BackupError:
        If any hash, size, file, symlink, or integrity check fails.
    """
    backup_dir = Path(backup_dir).resolve()
    if not backup_dir.is_dir():
        raise BackupError(f"Backup directory does not exist: {backup_dir}")
    if backup_dir.is_symlink():
        raise BackupError(f"Backup directory cannot be a symlink: {backup_dir}")

    manifest_path = backup_dir / "manifest.json"
    if manifest_path.is_symlink():
        raise BackupError(f"manifest.json cannot be a symlink: {manifest_path}")
    if not manifest_path.is_file():
        raise BackupError(f"manifest.json not found in {backup_dir}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BackupError(f"Failed to parse manifest.json: {exc}") from exc

    if not isinstance(manifest, dict) or manifest.get("version") not in {1, 2}:
        raise BackupError("Unsupported manifest format or version")

    db_meta = manifest.get("database")
    if not isinstance(db_meta, dict):
        raise BackupError("Missing database section in manifest")

    db_name = db_meta.get("filename") or db_meta.get("path")
    if not db_name or not isinstance(db_name, str):
        raise BackupError("Database filename missing in manifest")
    _safe_member(db_name, plain=True)
    if (
        db_name in {"manifest.json", "artifacts", "independent", "automatic", "intake", "snapshots"}
        or db_meta.get("path") != db_name
    ):
        raise BackupError("unsafe or aliased database path in manifest")

    db_path = backup_dir / db_name
    if db_path.is_symlink():
        raise BackupError(f"Database cannot be a symlink: {db_path}")
    if not db_path.is_file():
        raise BackupError(f"Database file missing: {db_path}")

    # Verify database hash and size
    actual_db_size = _file_size(db_path)
    expected_db_size = db_meta.get("size")
    if actual_db_size != expected_db_size:
        raise BackupError(
            f"Database size mismatch: expected {expected_db_size}, got {actual_db_size}"
        )

    actual_db_sha = _sha256_file(db_path)
    expected_db_sha = db_meta.get("sha256")
    if actual_db_sha != expected_db_sha:
        raise BackupError(
            f"Database SHA-256 mismatch: expected {expected_db_sha}, got {actual_db_sha}"
        )

    # Re-run PRAGMA integrity_check
    try:
        check_conn = sqlite3.connect(f"file:{db_path.as_posix()}?immutable=1&mode=ro", uri=True)
        try:
            cursor = check_conn.cursor()
            cursor.execute("PRAGMA integrity_check")
            rows = cursor.fetchall()
        finally:
            check_conn.close()
    except sqlite3.Error as exc:
        raise BackupError(f"Database integrity check failed: {exc}") from exc

    if len(rows) != 1 or rows[0][0] != "ok":
        raise BackupError(f"Database integrity check failed: {rows}")

    # Verify artifacts
    raw_artifacts = manifest.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise BackupError("Missing artifacts list in manifest")

    expected_files: set[Path] = {manifest_path.resolve(), db_path.resolve()}

    for item in raw_artifacts:
        if not isinstance(item, dict):
            raise BackupError("Invalid artifact item in manifest")
        rel_str = item.get("path")
        if not rel_str or not isinstance(rel_str, str):
            raise BackupError("Artifact path missing in manifest")
        art_path = backup_dir / "artifacts" / _safe_member(rel_str)

        if art_path.is_symlink():
            raise BackupError(f"Artifact cannot be a symlink: {art_path}")
        if not art_path.is_file():
            raise BackupError(f"Artifact file missing: {art_path}")

        expected_size = item.get("size")
        actual_size = _file_size(art_path)
        if actual_size != expected_size:
            raise BackupError(
                f"Artifact size mismatch for {rel_str}: expected {expected_size}, got {actual_size}"
            )

        expected_sha = item.get("sha256")
        actual_sha = _sha256_file(art_path)
        if actual_sha != expected_sha:
            raise BackupError(
                f"Artifact SHA-256 mismatch for {rel_str}: "
                f"expected {expected_sha}, got {actual_sha}"
            )

        expected_files.add(art_path.resolve())

    if manifest["version"] == 2 or "state" in manifest:
        expected_files.update(_verify_state(backup_dir, manifest.get("state")))

    # Reject extra files, missing files, or any symlinks
    for dirpath, dirnames, filenames in os.walk(backup_dir, followlinks=False):
        dp = Path(dirpath)
        if dp.is_symlink():
            raise BackupError(f"Symlink directory rejected: {dp}")
        for d in dirnames:
            p = dp / d
            if p.is_symlink():
                raise BackupError(f"Symlink directory rejected: {p}")
        for f in filenames:
            p = dp / f
            if p.is_symlink():
                raise BackupError(f"Symlink file rejected: {p}")
            if not p.is_file():
                continue
            if p.resolve() not in expected_files:
                raise BackupError(f"Extra file rejected in backup: {p}")


def restore_backup(
    backup_dir: Path,
    target: Path,
    *,
    replace_ledgers: bool = False,
    allow_ledger_rollback: bool = False,
) -> None:
    """Restore a verified backup into the target path.

    Parameters
    ----------
    backup_dir:
        Path to the backup directory to restore.
    target:
        Path to the target directory. Must be non-existent or empty by default.
        ``replace_ledgers`` permits only surviving ledgers (and writer locks); loss
        of their history additionally requires ``allow_ledger_rollback``. All writers
        must be stopped during this offline recovery operation.

    Raises
    ------
    BackupError:
        If verification fails or target exists and is non-empty.
    """
    backup_dir = Path(backup_dir).resolve()
    target = Path(target).absolute()

    # 1. Verify backup first
    verify_backup(backup_dir)

    _check_restore_target(
        backup_dir, target, replace_ledgers=replace_ledgers, allow_rollback=allow_ledger_rollback
    )

    target_parent = target.parent
    target_parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    temp_target = target_parent / f".tmp-restore-{uuid.uuid4().hex}"
    temp_target.mkdir(mode=0o700)

    try:
        manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
        db_name = _safe_member(manifest["database"]["path"], plain=True).name

        # Restore database
        src_db = backup_dir / db_name
        dst_db = temp_target / db_name
        shutil.copy2(src_db, dst_db)
        dst_db.chmod(0o600)

        # Restore artifacts
        src_artifacts = backup_dir / "artifacts"
        if src_artifacts.is_dir():
            dst_artifacts = temp_target / "artifacts"
            dst_artifacts.mkdir(mode=0o700, exist_ok=True)
            for artifact in manifest.get("artifacts", []):
                rel = _safe_member(artifact["path"])
                src_art_file = src_artifacts / rel
                dst_art_file = dst_artifacts / rel
                dst_art_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copy2(src_art_file, dst_art_file)
                dst_art_file.chmod(0o400)

            # Ensure standard store staging directory exists
            (dst_artifacts / "staging").mkdir(mode=0o700, exist_ok=True)
            (dst_artifacts / "blobs").mkdir(mode=0o700, exist_ok=True)
            (dst_artifacts / "observations").mkdir(mode=0o700, exist_ok=True)

        # Restore optional state only at fixed, validated paths within staging.
        for item in manifest.get("state", []):
            rel = _state_path(item["path"])
            dst = temp_target / rel
            dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(backup_dir / rel, dst)
            dst.chmod(0o400 if rel.parts[:2] == ("snapshots", "versions") else 0o600)

        # Restore manifest
        dst_manifest = temp_target / "manifest.json"
        shutil.copy2(backup_dir / "manifest.json", dst_manifest)
        dst_manifest.chmod(0o600)

        # Enforce 0700 permissions on all restored directories
        temp_target.chmod(0o700)
        for dirpath, dirnames, _ in os.walk(temp_target):
            for d in dirnames:
                os.chmod(os.path.join(dirpath, d), 0o700)

        verify_backup(temp_target)
        # Recheck immediately before changing a ledger-only destination. Restore is
        # offline: stop all ledger writers/readers first, as documented in the runbook.
        _check_restore_target(
            backup_dir,
            target,
            replace_ledgers=replace_ledgers,
            allow_rollback=allow_ledger_rollback,
        )
        previous = target_parent / f".previous-restore-{uuid.uuid4().hex}"
        if target.exists():
            target.rename(previous)
        try:
            temp_target.rename(target)
        except Exception:
            if previous.exists():
                previous.rename(target)
            raise
        if previous.exists():
            shutil.rmtree(previous)
        _sync_dir(target_parent)
        target.chmod(0o700)

    except Exception:
        shutil.rmtree(temp_target, ignore_errors=True)
        raise


def prune_backups(
    destination: Path,
    *,
    keep: int,
    now: datetime,
) -> tuple[Path, ...]:
    """Prune old valid backups; see ``prune_report`` for the corrupt ones left behind."""
    return prune_report(destination, keep=keep, now=now)[0]


def prune_report(
    destination: Path,
    *,
    keep: int,
    now: datetime,
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Prune older backups from destination, keeping the newest `keep` valid ones.

    Never deletes the newest valid backup. Never deletes anything that is not a
    backup-* directory with a manifest. Enforces keep >= 1.

    Parameters
    ----------
    destination:
        Directory containing backups.
    keep:
        Number of newest valid backups to retain (>= 1).
    now:
        Current timestamp.

    Returns
    -------
    tuple[tuple[Path, ...], tuple[Path, ...]]:
        The pruned backup directories, and the backups that FAILED verification and were
        deliberately left in place (a human must look at them).
    """
    del now  # Unused, timestamps are derived from backup directories and manifests
    destination = Path(destination).resolve()
    if not destination.is_dir():
        raise BackupError(f"Destination is not a directory: {destination}")

    if keep < 1:
        raise BackupError(f"keep must be >= 1, got {keep}")

    # Find candidate backup directories: must be backup-* directory containing manifest.json
    candidates: list[Path] = []
    for item in destination.iterdir():
        if item.is_symlink() or not item.is_dir():
            continue
        if not item.name.startswith("backup-"):
            continue
        manifest_path = item / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            continue
        candidates.append(item)

    # Sort descending (newest first)
    def _sort_key(p: Path) -> tuple[str, str]:
        created_at = ""
        try:
            m = json.loads((p / "manifest.json").read_text(encoding="utf-8"))
            created_at = str(m.get("created_at", ""))
        except Exception:
            pass
        return (created_at, p.name)

    candidates.sort(key=_sort_key, reverse=True)

    # Validate candidates
    valid_backups: list[Path] = []
    invalid_backups: list[Path] = []
    for c in candidates:
        try:
            verify_backup(c)
            valid_backups.append(c)
        except BackupError:
            invalid_backups.append(c)

    if not valid_backups:
        return (), tuple(invalid_backups)

    to_keep = set(valid_backups[:keep])
    newest_valid = valid_backups[0]

    # Only VALID backups older than the newest `keep` are deleted. A backup that fails
    # verification is left in place for a human: deleting it automatically could destroy
    # the only recoverable copy and would hide the fact that backups are failing.
    pruned: list[Path] = []
    for c in valid_backups:
        if c in to_keep or c == newest_valid:
            continue
        shutil.rmtree(c)
        pruned.append(c)

    return tuple(pruned), tuple(invalid_backups)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for backup operations."""
    parser = argparse.ArgumentParser(
        prog="cu_rates.ops.backup",
        description="Backup and restore tooling for operational database and artifact store",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = subparsers.add_parser("create", help="Create a new backup")
    p_create.add_argument("pos_database", nargs="?", type=Path, help="Operational database path")
    p_create.add_argument("pos_artifact_root", nargs="?", type=Path, help="Artifact store root")
    p_create.add_argument("pos_destination", nargs="?", type=Path, help="Destination directory")
    p_create.add_argument("--database", type=Path, dest="opt_database")
    p_create.add_argument("--artifact-root", "--artifacts", type=Path, dest="opt_artifact_root")
    p_create.add_argument("--destination", type=Path, dest="opt_destination")
    p_create.add_argument("--now", type=datetime.fromisoformat, default=None)

    for flag in ("denylist-path", "automatic-denylist-path", "intake-database", "snapshot-root"):
        p_create.add_argument(f"--{flag}", type=Path)

    # verify
    p_verify = subparsers.add_parser("verify", help="Verify a backup")
    p_verify.add_argument("pos_backup_dir", nargs="?", type=Path, help="Backup directory")
    p_verify.add_argument("--backup-dir", type=Path, dest="opt_backup_dir")

    # restore
    p_restore = subparsers.add_parser("restore", help="Restore a backup into target")
    p_restore.add_argument("pos_backup_dir", nargs="?", type=Path, help="Backup directory")
    p_restore.add_argument("pos_target", nargs="?", type=Path, help="Target restore directory")
    p_restore.add_argument("--backup-dir", type=Path, dest="opt_backup_dir")
    p_restore.add_argument("--target", type=Path, dest="opt_target")

    p_restore.add_argument(
        "--replace-ledgers",
        action="store_true",
        help="Permit replacing a target containing only withdrawal ledgers",
    )
    p_restore.add_argument(
        "--allow-ledger-rollback",
        action="store_true",
        help="Explicitly permit losing newer withdrawal history",
    )

    # prune
    p_prune = subparsers.add_parser("prune", help="Prune old backups")
    p_prune.add_argument("pos_destination", nargs="?", type=Path, help="Destination directory")
    p_prune.add_argument("--destination", type=Path, dest="opt_destination")
    p_prune.add_argument(
        "--keep", type=int, default=5, help="Number of backups to keep (default: 5)"
    )
    p_prune.add_argument("--now", type=datetime.fromisoformat, default=None)

    args = parser.parse_args(argv)

    try:
        if args.command == "create":
            database = args.opt_database or args.pos_database
            artifact_root = args.opt_artifact_root or args.pos_artifact_root
            destination = args.opt_destination or args.pos_destination
            if not database or not artifact_root or not destination:
                parser.error("database, artifact_root, and destination are required")
            if not database.is_absolute():
                raise BackupError(f"database path must be absolute: {database}")
            if not artifact_root.is_absolute():
                raise BackupError(f"artifact_root path must be absolute: {artifact_root}")
            if not destination.is_absolute():
                raise BackupError(f"destination path must be absolute: {destination}")

            now = args.now or datetime.now(UTC)
            optional = {
                key: getattr(args, key)
                for key in (
                    "denylist_path",
                    "automatic_denylist_path",
                    "intake_database",
                    "snapshot_root",
                )
            }
            if any(path is not None and not path.is_absolute() for path in optional.values()):
                raise BackupError("Optional input paths must be absolute")
            res = create_backup(database, artifact_root, destination, now=now, **optional)
            print(f"Created backup: {res}")

        elif args.command == "verify":
            backup_dir = args.opt_backup_dir or args.pos_backup_dir
            if not backup_dir:
                parser.error("backup_dir is required")
            if not backup_dir.is_absolute():
                raise BackupError(f"backup_dir path must be absolute: {backup_dir}")

            verify_backup(backup_dir)
            print(f"Verified backup: {backup_dir}")

        elif args.command == "restore":
            backup_dir = args.opt_backup_dir or args.pos_backup_dir
            target = args.opt_target or args.pos_target
            if not backup_dir or not target:
                parser.error("backup_dir and target are required")
            if not backup_dir.is_absolute():
                raise BackupError(f"backup_dir path must be absolute: {backup_dir}")
            if not target.is_absolute():
                raise BackupError(f"target path must be absolute: {target}")

            restore_backup(
                backup_dir,
                target,
                replace_ledgers=args.replace_ledgers,
                allow_ledger_rollback=args.allow_ledger_rollback,
            )
            print(f"Restored backup to: {target}")

        elif args.command == "prune":
            destination = args.opt_destination or args.pos_destination
            if not destination:
                parser.error("destination is required")
            if not destination.is_absolute():
                raise BackupError(f"destination path must be absolute: {destination}")

            now = args.now or datetime.now(UTC)
            pruned, invalid = prune_report(destination, keep=args.keep, now=now)
            print(f"Pruned {len(pruned)} backup(s): {', '.join(str(p) for p in pruned)}")
            if invalid:
                sys.stderr.write(
                    "BackupError: backups failed verification and were kept: "
                    f"{', '.join(str(p) for p in invalid)}\n"
                )
                return 2

        return 0

    except (BackupError, OSError) as exc:
        sys.stderr.write(f"BackupError: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
