"""Offline-testable backup and restore tooling for the database and artifact store."""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import uuid
from collections.abc import Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path


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


def create_backup(
    database: Path,
    artifact_root: Path,
    destination: Path,
    *,
    now: datetime,
) -> Path:
    """Create an offline-testable backup of the database and artifact store.

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
            # 1. Copy database using SQLite online backup API from read-only URI
            db_filename = database.name
            temp_db_path = temp_dir / db_filename
            src_uri = f"file:{database.as_posix()}?mode=ro"

            try:
                src_conn = sqlite3.connect(src_uri, uri=True)
            except sqlite3.Error as exc:
                raise BackupError(f"Failed to open source database: {exc}") from exc

            try:
                dest_conn = sqlite3.connect(temp_db_path.as_posix())
                try:
                    src_conn.backup(dest_conn)
                    dest_conn.execute("PRAGMA journal_mode=DELETE")
                finally:
                    dest_conn.close()
            except sqlite3.Error as exc:
                raise BackupError(f"Database online backup failed: {exc}") from exc
            finally:
                src_conn.close()

            temp_db_path.chmod(0o600)

            # Check integrity of copied database
            try:
                check_conn = sqlite3.connect(
                    f"file:{temp_db_path.as_posix()}?immutable=1&mode=ro", uri=True
                )
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
            integrity_result = rows[0][0]

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

            # 3. Write manifest.json (version 1)
            manifest = {
                "version": 1,
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
            manifest_path = temp_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            manifest_path.chmod(0o600)

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

    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise BackupError(f"Unsupported manifest format or version: {manifest.get('version')}")

    db_meta = manifest.get("database")
    if not isinstance(db_meta, dict):
        raise BackupError("Missing database section in manifest")

    db_name = db_meta.get("filename") or db_meta.get("path")
    if not db_name or not isinstance(db_name, str):
        raise BackupError("Database filename missing in manifest")
    _safe_member(db_name, plain=True)

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


def restore_backup(backup_dir: Path, target: Path) -> None:
    """Restore a verified backup into the target path.

    Parameters
    ----------
    backup_dir:
        Path to the backup directory to restore.
    target:
        Path to the target directory. Must be non-existent or empty.

    Raises
    ------
    BackupError:
        If verification fails or target exists and is non-empty.
    """
    backup_dir = Path(backup_dir).resolve()
    target = Path(target).resolve()

    # 1. Verify backup first
    verify_backup(backup_dir)

    # 2. Refuse if target exists and is non-empty
    if target.exists():
        if not target.is_dir() or target.is_symlink():
            raise BackupError(f"Target exists and is not a directory: {target}")
        if any(target.iterdir()):
            raise BackupError(f"Target directory exists and is non-empty: {target}")

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

        # Restore manifest
        dst_manifest = temp_target / "manifest.json"
        shutil.copy2(backup_dir / "manifest.json", dst_manifest)
        dst_manifest.chmod(0o600)

        # Enforce 0700 permissions on all restored directories
        temp_target.chmod(0o700)
        for dirpath, dirnames, _ in os.walk(temp_target):
            for d in dirnames:
                os.chmod(os.path.join(dirpath, d), 0o700)

        # Atomically rename into place
        if target.exists():
            target.rmdir()
        temp_target.rename(target)
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
            res = create_backup(database, artifact_root, destination, now=now)
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

            restore_backup(backup_dir, target)
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

    except BackupError as exc:
        sys.stderr.write(f"BackupError: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
