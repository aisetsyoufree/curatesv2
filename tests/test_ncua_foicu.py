"""T021b: the importer reads the real NCUA FOICU layout (confirmed against the 2026-06 cycle).

Rows here are SYNTHETIC with the real header names and date format; the real file is private
(D-14) and never enters the repository.
"""

import hashlib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from cu_rates.artifact_store import ArtifactStore
from cu_rates.contracts.enums import InstitutionStatus
from cu_rates.db import open_operational_database, upgrade_database
from cu_rates.db.schema import institutions, roster_import_entries
from cu_rates.roster import import_roster
from cu_rates.roster.mapping import NCUA_FOICU_MAPPING, RosterColumnMapping
from cu_rates.roster.ncua_zip import NcuaZipError, main, read_roster
from cu_rates.snapshot import Denylist

HEADER = (
    '"CU_NUMBER","CYCLE_DATE","JOIN_NUMBER","RSSD","CU_TYPE","CU_NAME","CITY","STATE",'
    '"CharterState","STATE_CODE","ZIP_CODE","COUNTY_CODE","CONG_DIST","SMSA","ATTENTION_OF",'
    '"STREET","REGION","SE","DISTRICT","YEAR_OPENED","TOM_CODE","LIMITED_INC","ISSUE_DATE",'
    '"Peer_Group","Quarter_Flag","IsMDI","INSURED_DATE","AM_DateHeld"\n'
)
T = datetime(2026, 11, 18, tzinfo=UTC)


def row(charter: int, name: str, state: str = "MA", cycle: str = "6/30/2026 0:00:00") -> str:
    fields = [str(charter), cycle, "1", "0", "1", name, "Springfield", state, state]
    fields += ["25", "01101", "13", "1", "0", "", "1 Main St", "1", "", "1", "1950", "A", "0"]
    fields += ["1/1/1950 0:00:00", "5", "0", "0", "1/1/1951 0:00:00", ""]
    return ",".join(f'"{value}"' for value in fields) + "\n"


def csv_bytes(*rows: str) -> bytes:
    return (HEADER + "".join(rows)).encode()


@pytest.fixture
def parts(tmp_path: Path):  # type: ignore[no-untyped-def]
    database = tmp_path / "operational.sqlite"
    upgrade_database(database)
    denylist = Denylist(tmp_path / "independent" / "denylist.json")
    denylist.initialize()
    return open_operational_database(database), ArtifactStore(tmp_path / "evidence"), denylist


def do_import(parts, data: bytes, version: str = "ncua-test"):  # type: ignore[no-untyped-def]
    engine, store, denylist = parts
    return import_roster(
        data,
        engine_or_connection=engine,
        artifact_store=store,
        source_version=version,
        clock=lambda: T,
        mapping=NCUA_FOICU_MAPPING,
        denylist=denylist,
    )


def test_the_real_layout_imports_massachusetts_rows_as_active(parts) -> None:  # type: ignore[no-untyped-def]
    report = do_import(
        parts,
        csv_bytes(
            row(60001, "Example One CU"), row(60002, "Example Two CU"), row(70001, "Ohio CU", "OH")
        ),
    )
    assert report.errors == () and report.malformed_rows == ()
    assert (report.total_rows, report.ma_rows, report.skipped_non_ma_count) == (3, 2, 1)
    assert sorted(report.inserted_charters) == [60001, 60002]
    with parts[0].connect() as conn:
        rows = conn.execute(select(institutions)).all()
        dates = {r[0] for r in conn.execute(select(roster_import_entries.c.source_as_of_date))}
    assert {r.status for r in rows} == {InstitutionStatus.ACTIVE.value}
    assert all(r.charter_verified == 0 for r in rows)  # identity still needs a human review
    assert dates == {"2026-06-30"}  # NCUA's "6/30/2026 0:00:00" is normalized to an ISO date


@pytest.mark.parametrize("cycle", ["", "not a date", "2026-06-30", "13/45/2026 0:00:00"])
def test_a_bad_or_foreign_cycle_date_is_rejected_not_guessed(parts, cycle: str) -> None:  # type: ignore[no-untyped-def]
    report = do_import(parts, csv_bytes(row(60001, "Example One CU", cycle=cycle)))
    assert report.malformed_rows and not report.inserted_charters


def test_the_file_must_have_the_real_headers(parts) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError, match="missing required mapping columns"):
        do_import(parts, b"Charter,Name\n1,x\n")


def test_a_verified_charter_missing_from_a_later_cycle_is_suppressed_not_closed(parts) -> None:  # type: ignore[no-untyped-def]
    """The file lists active credit unions only, so absence is a question, never a closure."""
    engine, _store, denylist = parts
    do_import(parts, csv_bytes(row(60001, "Example One CU"), row(60002, "Example Two CU")), "q2")
    with engine.begin() as conn:
        conn.execute(institutions.update().values(charter_verified=1))
    later = csv_bytes(row(60002, "Example Two CU", cycle="9/30/2026 0:00:00"))
    report = do_import(parts, later, "q3")
    assert report.quarantined_charters == (60001,)
    with engine.connect() as conn:
        status, verified = conn.execute(
            select(institutions.c.status, institutions.c.charter_verified).where(
                institutions.c.charter_number == 60001
            )
        ).one()
    assert status == "active" and verified == 0  # nothing invents a merger or a successor
    assert ("institution", "60001") in denylist.active_targets(T)
    assert ("institution", "60002") not in denylist.active_targets(T)


def test_a_mapping_has_a_status_column_or_an_implied_status_never_both() -> None:
    with pytest.raises(ValueError, match="either a status column or an implied status"):
        RosterColumnMapping(status=None)
    with pytest.raises(ValueError, match="either a status column or an implied status"):
        RosterColumnMapping(implied_status=InstitutionStatus.ACTIVE)
    assert NCUA_FOICU_MAPPING.iso_date("6/30/2026 0:00:00") == "2026-06-30"
    assert NCUA_FOICU_MAPPING.iso_date("6/30/2026") is None
    assert RosterColumnMapping().iso_date("2026-06-30") == "2026-06-30"


# ---- the ZIP reader -------------------------------------------------------------------------


def make_zip(path: Path, members: dict[str, bytes], *, symlink: str | None = None) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16
            archive.writestr(info, "FOICU.txt")
    return path


def test_the_zip_reader_returns_the_roster_and_the_whole_files_hash(tmp_path: Path) -> None:
    data = csv_bytes(row(60001, "Example One CU"))
    archive = make_zip(tmp_path / "q.zip", {"FOICU.txt": data, "Readme.txt": b"help"})
    got, digest = read_roster(archive)
    assert got == data and digest == hashlib.sha256(archive.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ({"Readme.txt": b"help"}, "exactly one"),
        ({"FOICU.txt": b"a", "sub/FOICU.txt": b"b"}, "exactly one"),
        ({"../FOICU.txt": b"x"}, "unsafe member name"),
        ({"/abs/FOICU.txt": b"x"}, "unsafe member name"),
    ],
)
def test_unsafe_or_incomplete_zips_are_refused(
    tmp_path: Path, members: dict[str, bytes], message: str
) -> None:
    with pytest.raises(NcuaZipError, match=message):
        read_roster(make_zip(tmp_path / "bad.zip", members))


def test_symlink_members_and_garbage_and_oversize_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(NcuaZipError, match="unsafe member name"):
        read_roster(make_zip(tmp_path / "l.zip", {"FOICU.txt": b"x"}, symlink="link.txt"))
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"this is not a zip")
    with pytest.raises(NcuaZipError, match="not a valid ZIP"):
        read_roster(junk)
    from cu_rates.roster import ncua_zip

    monkeypatch.setattr(ncua_zip, "MAX_MEMBER_BYTES", 10)
    with pytest.raises(NcuaZipError, match="size limit"):
        read_roster(make_zip(tmp_path / "big.zip", {"FOICU.txt": b"x" * 100}))
    monkeypatch.setattr(ncua_zip, "MAX_ZIP_BYTES", 10)
    with pytest.raises(NcuaZipError, match="size limit"):
        read_roster(make_zip(tmp_path / "big2.zip", {"FOICU.txt": b"x" * 100}))


def test_the_cli_imports_and_records_the_zip_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    archive = make_zip(
        tmp_path / "q.zip",
        {"FOICU.txt": csv_bytes(row(60001, "Example One CU"), row(70001, "X", "OH"))},
    )
    database = tmp_path / "operational.sqlite"
    upgrade_database(database)
    Denylist(tmp_path / "independent" / "d.json").initialize()
    args = [
        "--zip",
        str(archive),
        "--database",
        str(database),
        "--artifact-root",
        str(tmp_path / "evidence"),
        "--denylist",
        str(tmp_path / "independent" / "d.json"),
    ]
    assert main([*args, "--dry-run"], now=T) == 0
    assert hashlib.sha256(archive.read_bytes()).hexdigest() in capsys.readouterr().out
    assert main(args, now=T) == 0
    assert "Massachusetts 1" in capsys.readouterr().out
    assert main(["--zip", "rel.zip", *args[2:]], now=T) == 1  # relative paths are refused
