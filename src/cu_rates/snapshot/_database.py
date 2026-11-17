"""Common SQLite format verification for staging, rollback, and public reads."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from pydantic import ValidationError

from cu_rates.contracts.audit import PublishedSnapshot, SnapshotEntry

from ._files import SnapshotError, read_bytes, regular_file, require_directory
from ._models import PublicOffer, SnapshotInfo, Version

DB_NAME = "published.sqlite"
POINTER = "current.json"
DESCRIPTOR = "version.json"
VERSIONS = "versions"
APPLICATION_ID = 0x43555232  # CUR2
FORMAT_VERSION = 1


def read_version(path: Path) -> Version:
    try:
        return Version.model_validate_json(read_bytes(path, 4096))
    except (OSError, ValidationError) as exc:
        raise SnapshotError("snapshot pointer or version descriptor missing or invalid") from exc


def no_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            path.with_name(path.name + suffix).lstat()
        except FileNotFoundError:
            continue
        raise SnapshotError("a versioned snapshot must not have SQLite sidecars")


def database_digest(path: Path) -> str:
    no_sidecars(path)
    with regular_file(path) as fd, os.fdopen(os.dup(fd), "rb") as stream:
        if os.fstat(fd).st_mode & 0o222:
            raise SnapshotError("snapshot database must be sealed read-only")
        header = stream.read(100)
        if (
            not header.startswith(b"SQLite format 3\x00")
            or header[18:20] != b"\x01\x01"
            or int.from_bytes(header[68:72], "big") != APPLICATION_ID
        ):
            raise SnapshotError("not a rollback-journal CU Rates public snapshot")
        stream.seek(0)
        return hashlib.file_digest(stream, "sha256").hexdigest()


def snapshot_entry(offer: PublicOffer) -> SnapshotEntry:
    return SnapshotEntry(
        offer_id=offer.offer_id,
        structural_sha256=offer.fingerprints.structural,
        value_sha256=offer.fingerprints.value,
    )


def verify_database(
    directory: Path, version: Version
) -> tuple[SnapshotInfo, tuple[PublicOffer, ...]]:
    """Verify the sealed file and the contract manifest rebuilt from actual public rows."""
    path = directory / DB_NAME
    try:
        require_directory(directory)
        if database_digest(path) != version.database_sha256:
            raise SnapshotError("public database digest mismatch")
        uri = path.absolute().as_uri() + "?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            if (
                conn.execute("PRAGMA user_version").fetchone() != (FORMAT_VERSION,)
                or conn.execute("PRAGMA journal_mode").fetchone() != ("delete",)
                or conn.execute("PRAGMA quick_check").fetchall() != [("ok",)]
            ):
                raise SnapshotError("public SQLite format or integrity check failed")
            metadata = conn.execute("SELECT payload FROM snapshot_info").fetchall()
            if len(metadata) != 1:
                raise SnapshotError("snapshot metadata is missing or ambiguous")
            info = SnapshotInfo.model_validate_json(metadata[0][0])
            if info.snapshot_id != version.snapshot_id:
                raise SnapshotError("snapshot ID does not match the selected version")
            offers = []
            for offer_id, payload in conn.execute(
                "SELECT offer_id, payload FROM public_offers ORDER BY offer_id"
            ):
                offer = PublicOffer.model_validate_json(payload)
                if (
                    offer_id != offer.offer_id
                    or not offer.observed_at <= info.published_at < offer.valid_until
                ):
                    raise SnapshotError("offer row does not match the manifest or release time")
                offers.append(offer)
        PublishedSnapshot(
            **info.model_dump(), entries=tuple(snapshot_entry(offer) for offer in offers)
        )
        return info, tuple(offers)
    except (OSError, sqlite3.Error, ValidationError) as exc:
        raise SnapshotError("cannot verify public snapshot database or manifest") from exc
