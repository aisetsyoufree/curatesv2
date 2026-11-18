"""Operator import of the NCUA 5300 call-report quarterly ZIP (D-14: downloaded by the owner).

    python -m cu_rates.roster.ncua_zip --zip /abs/call-report-data-YYYY-MM.zip \\
        --database /abs/operational.sqlite --artifact-root /abs/artifacts \\
        --denylist /abs/denylist.json

The ZIP is untrusted input: only the roster member (FOICU.txt) is read, it is size-capped, and
member names are checked; nothing is written to disk besides the operational import itself. The
ZIP's SHA-256 is recorded in the import's source version so the exact file is identifiable
later. The raw file stays private; only normalized fields are published (D-14).
"""

from __future__ import annotations

import argparse
import hashlib
import posixpath
import sys
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from cu_rates.artifact_store import ArtifactStore
from cu_rates.db import open_operational_database
from cu_rates.roster import import_roster
from cu_rates.roster.mapping import NCUA_FOICU_MAPPING
from cu_rates.snapshot import Denylist

ROSTER_MEMBER = "FOICU.txt"
MAX_ZIP_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 200
SOURCE_URL = "https://www.ncua.gov/analysis/Pages/call-report-data/quarterly-data.aspx"


class NcuaZipError(ValueError):
    """The ZIP is not a usable NCUA quarterly file; nothing was imported."""


def read_roster(zip_path: Path) -> tuple[bytes, str]:
    """(FOICU.txt bytes, SHA-256 of the whole ZIP). Raises NcuaZipError on anything unsafe."""
    if not zip_path.is_file() or zip_path.is_symlink():
        raise NcuaZipError("the ZIP must be a regular file")
    if zip_path.stat().st_size > MAX_ZIP_BYTES:
        raise NcuaZipError("the ZIP is larger than the size limit")
    digest = hashlib.sha256()
    with zip_path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    try:
        archive = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise NcuaZipError("not a valid ZIP archive") from exc
    with archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            raise NcuaZipError("the ZIP has too many members")
        for info in members:
            name = info.filename
            if (
                name.startswith(("/", "\\"))
                or ".." in posixpath.normpath(name).split("/")
                or info.external_attr >> 28 == 0xA  # a symbolic link
            ):
                raise NcuaZipError(f"unsafe member name: {name!r}")
        matches = [i for i in members if posixpath.basename(i.filename).casefold() == "foicu.txt"]
        if len(matches) != 1:
            raise NcuaZipError(f"expected exactly one {ROSTER_MEMBER}, found {len(matches)}")
        info = matches[0]
        if info.file_size > MAX_MEMBER_BYTES:
            raise NcuaZipError(f"{ROSTER_MEMBER} is larger than the size limit")
        with archive.open(info) as member:
            data = member.read(MAX_MEMBER_BYTES + 1)
        if len(data) > MAX_MEMBER_BYTES or len(data) != info.file_size:
            raise NcuaZipError(f"{ROSTER_MEMBER} does not match its declared size")
    return data, digest.hexdigest()


def main(argv: Sequence[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cu_rates.roster.ncua_zip", description=__doc__)
    for name in ("--zip", "--database", "--artifact-root", "--denylist"):
        parser.add_argument(name, type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="read and validate only")
    args = parser.parse_args(argv)
    try:
        for path in (args.zip, args.database, args.artifact_root, args.denylist):
            if not path.is_absolute():
                raise NcuaZipError(f"{path} must be an absolute path")
        data, zip_sha = read_roster(args.zip)
        if args.dry_run:
            print(f"ok: {ROSTER_MEMBER} {len(data)} bytes; zip sha256 {zip_sha}")
            return 0
        report = import_roster(
            data,
            engine_or_connection=open_operational_database(args.database),
            artifact_store=ArtifactStore(args.artifact_root),
            source_version=f"ncua-5300 zip-sha256:{zip_sha}",
            clock=(lambda: now) if now is not None else (lambda: datetime.now(UTC)),
            mapping=NCUA_FOICU_MAPPING,
            source_url=SOURCE_URL,
            denylist=Denylist(args.denylist),
        )
    except (NcuaZipError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"rows {report.total_rows}, Massachusetts {report.ma_rows}, "
        f"inserted {len(report.inserted_charters)}, updated {len(report.updated_charters)}, "
        f"held for review {len(report.review_charters)}, "
        f"quarantined {len(report.quarantined_charters)}, errors {len(report.errors)}"
    )
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
