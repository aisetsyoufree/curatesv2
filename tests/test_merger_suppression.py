"""AUD7-7: merger/closure in a roster import clears eligibility and suppresses offers.

Each test must FAIL without the fix and PASS with it. The tests do not mirror
the implementation: they describe externally observable behaviour that the
issue requires.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import select, update

from cu_rates.artifact_store import ArtifactStore
from cu_rates.contracts.audit import WithdrawalEntry
from cu_rates.contracts.enums import InstitutionStatus, WithdrawalScope
from cu_rates.db import open_operational_database, upgrade_database
from cu_rates.db.schema import (
    institutions,
    roster_import_entries,
    roster_imports,
    withdrawal_entries,
    withdrawal_lifts,
)
from cu_rates.pipeline.slice import CHARTER
from cu_rates.roster import RosterImportReport, import_roster
from cu_rates.snapshot import Denylist, SnapshotReader

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

FROZEN_V1 = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
FROZEN_V2 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
FROZEN_V3 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def _database(tmp_path: Path) -> Path:
    db_path = tmp_path / "operational.sqlite"
    upgrade_database(db_path)
    return db_path


def _denylist(tmp_path: Path) -> Denylist:
    denylist = Denylist(tmp_path / "independent" / "withdrawals.json")
    denylist.initialize()
    return denylist


def _at(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


def _store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "evidence")


# A minimal roster where charter 60001 starts active.
_V1_CSV = (
    b"Charter,Credit Union Name,State,Status,City,Date Inactive,"
    b"Successor Charter,As of Date\n"
    b"60001,Suppression Test CU,MA,Active,Boston,,,2026-09-01\n"
    b"60002,Successor CU,MA,Active,Cambridge,,,2026-09-01\n"
)

# V2: 60001 merged into 60002.
_V2_MERGED_CSV = (
    b"Charter,Credit Union Name,State,Status,City,Date Inactive,"
    b"Successor Charter,As of Date\n"
    b"60001,Suppression Test CU,MA,Merged,Boston,2026-09-15,60002,2026-10-01\n"
    b"60002,Successor CU,MA,Active,Cambridge,,,2026-10-01\n"
)

# V3: identical to V2 (idempotency check).
_V3_MERGED_CSV = (
    b"Charter,Credit Union Name,State,Status,City,Date Inactive,"
    b"Successor Charter,As of Date\n"
    b"60001,Suppression Test CU,MA,Merged,Boston,2026-09-15,60002,2026-11-01\n"
    b"60002,Successor CU,MA,Active,Cambridge,,,2026-11-01\n"
)

# V2 variant: 60001 liquidated instead.
_V2_LIQUIDATED_CSV = (
    b"Charter,Credit Union Name,State,Status,City,Date Inactive,"
    b"Successor Charter,As of Date\n"
    b"60001,Suppression Test CU,MA,Liquidated,Boston,2026-09-15,,2026-10-01\n"
    b"60002,Successor CU,MA,Active,Cambridge,,,2026-10-01\n"
)


# ---------------------------------------------------------------------------
# 1. Merger record clears eligibility
# ---------------------------------------------------------------------------
class TestMergerClearsEligibility:
    """An institution that merges must lose publication eligibility."""

    def test_unverified_institution_merger_clears_eligibility(self, tmp_path: Path) -> None:
        """Even an unverified institution's status must be updated and suppressed."""
        db_path = _database(tmp_path)
        store = _store(tmp_path)
        engine = open_operational_database(db_path)

        import_roster(
            _V1_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v1",
            clock=lambda: FROZEN_V1,
            denylist=_denylist(tmp_path),
        )

        v2_report = import_roster(
            _V2_MERGED_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v2",
            clock=lambda: FROZEN_V2,
            denylist=_denylist(tmp_path),
        )

        with engine.connect() as conn:
            inst = conn.execute(
                select(institutions).where(institutions.c.charter_number == 60001)
            ).one()
            # Status must be merged, eligibility cleared.
            assert inst.status == InstitutionStatus.MERGED.value
            assert inst.charter_verified == 0
            assert inst.successor_charter_number == 60002

        # Report must list the charter as suppressed.
        assert 60001 in v2_report.suppressed_charters

    def test_verified_institution_merger_clears_eligibility(self, tmp_path: Path) -> None:
        """A verified institution that merges must lose charter_verified immediately."""
        db_path = _database(tmp_path)
        store = _store(tmp_path)
        engine = open_operational_database(db_path)

        import_roster(
            _V1_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v1",
            clock=lambda: FROZEN_V1,
            denylist=_denylist(tmp_path),
        )

        # Manually verify the charter (simulating a human approval).
        with engine.begin() as conn:
            conn.execute(
                update(institutions)
                .where(institutions.c.charter_number == 60001)
                .values(charter_verified=1)
            )

        v2_report = import_roster(
            _V2_MERGED_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v2",
            clock=lambda: FROZEN_V2,
            denylist=_denylist(tmp_path),
        )

        with engine.connect() as conn:
            inst = conn.execute(
                select(institutions).where(institutions.c.charter_number == 60001)
            ).one()
            # Must be merged, eligibility cleared, even though it was verified.
            assert inst.status == InstitutionStatus.MERGED.value
            assert inst.charter_verified == 0
            assert inst.successor_charter_number == 60002

        # Must appear in both suppressed and review lists.
        assert 60001 in v2_report.suppressed_charters
        assert 60001 in v2_report.review_charters

    def test_liquidation_clears_eligibility(self, tmp_path: Path) -> None:
        """Liquidation is another ineligible status that must suppress."""
        db_path = _database(tmp_path)
        store = _store(tmp_path)
        engine = open_operational_database(db_path)

        import_roster(
            _V1_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v1",
            clock=lambda: FROZEN_V1,
            denylist=_denylist(tmp_path),
        )

        v2_report = import_roster(
            _V2_LIQUIDATED_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v2",
            clock=lambda: FROZEN_V2,
            denylist=_denylist(tmp_path),
        )

        with engine.connect() as conn:
            inst = conn.execute(
                select(institutions).where(institutions.c.charter_number == 60001)
            ).one()
            assert inst.status == InstitutionStatus.LIQUIDATED.value
            assert inst.charter_verified == 0

        assert 60001 in v2_report.suppressed_charters


# ---------------------------------------------------------------------------
# 2. Existing published offers are suppressed at read time
# ---------------------------------------------------------------------------
class TestExistingOffersAreSuppressed:
    """A withdrawal entry must be recorded so the reader can suppress offers."""

    def test_merger_creates_institution_withdrawal(self, tmp_path: Path) -> None:
        """The importer must record a withdrawal for the merged institution."""
        db_path = _database(tmp_path)
        store = _store(tmp_path)
        engine = open_operational_database(db_path)

        import_roster(
            _V1_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v1",
            clock=lambda: FROZEN_V1,
            denylist=_denylist(tmp_path),
        )

        import_roster(
            _V2_MERGED_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v2",
            clock=lambda: FROZEN_V2,
            denylist=_denylist(tmp_path),
        )

        with engine.connect() as conn:
            withdrawals = conn.execute(
                select(withdrawal_entries).where(
                    withdrawal_entries.c.scope == WithdrawalScope.INSTITUTION.value,
                    withdrawal_entries.c.target_id == "60001",
                )
            ).all()

            assert len(withdrawals) == 1
            w = withdrawals[0]
            assert w.actor == "roster-importer"
            assert "merged" in w.reason.lower()
            # The withdrawal must be in force (no lift recorded).
            lift = conn.execute(
                select(withdrawal_lifts).where(withdrawal_lifts.c.withdrawal_id == w.withdrawal_id)
            ).first()
            assert lift is None

    def test_withdrawal_matches_reader_denylist_shape(self, tmp_path: Path) -> None:
        """The withdrawal's scope + target_id must match what the reader checks."""
        db_path = _database(tmp_path)
        store = _store(tmp_path)
        engine = open_operational_database(db_path)

        import_roster(
            _V1_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v1",
            clock=lambda: FROZEN_V1,
            denylist=_denylist(tmp_path),
        )

        import_roster(
            _V2_MERGED_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v2",
            clock=lambda: FROZEN_V2,
            denylist=_denylist(tmp_path),
        )

        with engine.connect() as conn:
            w = conn.execute(
                select(withdrawal_entries).where(
                    withdrawal_entries.c.scope == WithdrawalScope.INSTITUTION.value,
                    withdrawal_entries.c.target_id == str(60001),
                )
            ).one()

            # The reader filters by (scope, target_id) pairs.
            # Institution scope uses str(charter_number).
            assert w.scope == WithdrawalScope.INSTITUTION.value
            assert w.target_id == "60001"


# ---------------------------------------------------------------------------
# 3. Re-import is idempotent
# ---------------------------------------------------------------------------
class TestReimportIdempotency:
    """Importing the same merger state again must not create duplicate withdrawals."""

    def test_same_status_reimport_no_duplicate_withdrawal(self, tmp_path: Path) -> None:
        db_path = _database(tmp_path)
        store = _store(tmp_path)
        engine = open_operational_database(db_path)

        import_roster(
            _V1_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v1",
            clock=lambda: FROZEN_V1,
            denylist=_denylist(tmp_path),
        )

        import_roster(
            _V2_MERGED_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v2",
            clock=lambda: FROZEN_V2,
            denylist=_denylist(tmp_path),
        )

        # V3: same merged status, newer as-of date.
        v3_report = import_roster(
            _V3_MERGED_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v3",
            clock=lambda: FROZEN_V3,
            denylist=_denylist(tmp_path),
        )

        with engine.connect() as conn:
            withdrawals = conn.execute(
                select(withdrawal_entries).where(
                    withdrawal_entries.c.scope == WithdrawalScope.INSTITUTION.value,
                    withdrawal_entries.c.target_id == "60001",
                )
            ).all()

            # Only one withdrawal from the first transition.
            assert len(withdrawals) == 1

        # Every inactive record is reconciled without duplicating suppression.
        assert 60001 in v3_report.suppressed_charters
        assert 60001 in v3_report.review_charters

    def test_institution_stays_ineligible_after_reimport(self, tmp_path: Path) -> None:
        """Re-importing merged status must not accidentally restore eligibility."""
        db_path = _database(tmp_path)
        store = _store(tmp_path)
        engine = open_operational_database(db_path)

        import_roster(
            _V1_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v1",
            clock=lambda: FROZEN_V1,
            denylist=_denylist(tmp_path),
        )
        import_roster(
            _V2_MERGED_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v2",
            clock=lambda: FROZEN_V2,
            denylist=_denylist(tmp_path),
        )
        import_roster(
            _V3_MERGED_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v3",
            clock=lambda: FROZEN_V3,
            denylist=_denylist(tmp_path),
        )

        with engine.connect() as conn:
            inst = conn.execute(
                select(institutions).where(institutions.c.charter_number == 60001)
            ).one()
            assert inst.status == InstitutionStatus.MERGED.value
            assert inst.charter_verified == 0


# ---------------------------------------------------------------------------
# 4. Restoration requires a recorded review
# ---------------------------------------------------------------------------
class TestRestorationRequiresRecordedReview:
    """Nothing is auto-restored. Only an explicit withdrawal lift can restore."""

    def test_no_auto_restore_on_active_reimport(self, tmp_path: Path) -> None:
        """If a later roster showed the CU active again, withdrawal stays in force."""
        db_path = _database(tmp_path)
        store = _store(tmp_path)
        engine = open_operational_database(db_path)

        import_roster(
            _V1_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v1",
            clock=lambda: FROZEN_V1,
            denylist=_denylist(tmp_path),
        )
        import_roster(
            _V2_MERGED_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v2",
            clock=lambda: FROZEN_V2,
            denylist=_denylist(tmp_path),
        )

        # Hypothetical V3 that shows the institution active again (reversal).
        # This is an unusual roster error that should not auto-restore.
        v3_active_csv = (
            b"Charter,Credit Union Name,State,Status,City,Date Inactive,"
            b"Successor Charter,As of Date\n"
            b"60001,Suppression Test CU,MA,Active,Boston,,,2026-11-01\n"
            b"60002,Successor CU,MA,Active,Cambridge,,,2026-11-01\n"
        )
        import_roster(
            v3_active_csv,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v3",
            clock=lambda: FROZEN_V3,
            denylist=_denylist(tmp_path),
        )

        with engine.connect() as conn:
            # The withdrawal from the merger must still be in force (no lift).
            withdrawals = conn.execute(
                select(withdrawal_entries).where(
                    withdrawal_entries.c.scope == WithdrawalScope.INSTITUTION.value,
                    withdrawal_entries.c.target_id == "60001",
                )
            ).all()
            assert len(withdrawals) >= 1
            for w in withdrawals:
                lift = conn.execute(
                    select(withdrawal_lifts).where(
                        withdrawal_lifts.c.withdrawal_id == w.withdrawal_id
                    )
                ).first()
                # No lift was auto-recorded.
                assert lift is None

    def test_withdrawal_can_be_lifted_by_explicit_human_action(self, tmp_path: Path) -> None:
        """A withdrawal can only be restored by inserting a lift record."""
        db_path = _database(tmp_path)
        store = _store(tmp_path)
        engine = open_operational_database(db_path)

        import_roster(
            _V1_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v1",
            clock=lambda: FROZEN_V1,
            denylist=_denylist(tmp_path),
        )
        import_roster(
            _V2_MERGED_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v2",
            clock=lambda: FROZEN_V2,
            denylist=_denylist(tmp_path),
        )

        with engine.begin() as conn:
            w = conn.execute(
                select(withdrawal_entries.c.withdrawal_id).where(
                    withdrawal_entries.c.scope == WithdrawalScope.INSTITUTION.value,
                    withdrawal_entries.c.target_id == "60001",
                )
            ).one()

            # Simulate explicit human lift.
            lift_time = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
            conn.execute(
                withdrawal_lifts.insert().values(
                    withdrawal_id=w.withdrawal_id,
                    lifted_at=lift_time,
                    lifted_by="human-reviewer",
                )
            )

        with engine.connect() as conn:
            lift = conn.execute(
                select(withdrawal_lifts).where(withdrawal_lifts.c.withdrawal_id == w.withdrawal_id)
            ).one()
            assert lift.lifted_by == "human-reviewer"
            assert lift.lifted_at is not None


def test_merger_without_a_denylist_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    engine = open_operational_database(_database(tmp_path))
    store = _store(tmp_path)
    import_roster(
        _V1_CSV,
        engine_or_connection=engine,
        artifact_store=store,
        source_version="v1",
        clock=lambda: FROZEN_V1,
    )
    with pytest.raises(ValueError, match="denylist"):
        import_roster(
            _V2_MERGED_CSV,
            engine_or_connection=engine,
            artifact_store=store,
            source_version="v2",
            clock=lambda: FROZEN_V2,
        )
    with engine.connect() as conn:
        status = conn.execute(
            select(institutions.c.status).where(institutions.c.charter_number == 60001)
        ).scalar_one()
        assert status == InstitutionStatus.ACTIVE.value
        assert not conn.execute(select(withdrawal_entries)).all()


_HEADER = "Charter,Credit Union Name,State,Status,City,Date Inactive,Successor Charter,As of Date\n"
_MOMENT = _BIRTH + timedelta(minutes=1)


def _snapshot_roster(
    status: str,
    *,
    charter: int = CHARTER,
    successor: str = "60002",
    as_of: str = "2026-11-16",
) -> bytes:
    inactive = "2026-11-15" if status in {"Merged", "Liquidated"} else ""
    other = 60002 if charter == CHARTER else CHARTER
    names = {CHARTER: "Example North Charter", 60002: "Successor CU"}
    return (
        _HEADER
        + f"{charter},{names[charter]},MA,{status},Boston,{inactive},{successor},{as_of}\n"
        + f"{other},{names[other]},MA,Active,Cambridge,,,{as_of}\n"
    ).encode()


@pytest.fixture
def public_snapshot(tmp_path: Path, stored: StoredFixture) -> tuple[SnapshotReader, Denylist]:
    publisher, reader, denylist = setup_storage(tmp_path)
    item = stored.row()
    publisher.build(release("before-import", item), conn=stored.conn, store=stored.run.store)
    assert {o.institution_charter for o in reader.read(at=_MOMENT).offers} == {CHARTER}
    return reader, denylist


def _snapshot_import(
    stored: StoredFixture,
    denylist: Denylist,
    csv: bytes,
    *,
    moment: datetime = _MOMENT,
) -> RosterImportReport:
    return import_roster(
        csv,
        engine_or_connection=stored.conn,
        artifact_store=stored.run.store,
        source_version="reconciliation-test",
        clock=_at(moment),
        denylist=denylist,
    )


@pytest.mark.parametrize("status", ["Merged", "Liquidated", "Converted"])
@pytest.mark.parametrize("verified", [False, True])
def test_inactive_import_hides_real_snapshot_and_queues_every_charter(
    stored: StoredFixture,
    public_snapshot: tuple[SnapshotReader, Denylist],
    status: str,
    verified: bool,
) -> None:
    reader, denylist = public_snapshot
    stored.conn.execute(
        update(institutions)
        .where(institutions.c.charter_number == CHARTER)
        .values(charter_verified=int(verified))
    )
    csv = _snapshot_roster(status, successor="60002" if status == "Merged" else "")
    report = _snapshot_import(stored, denylist, csv)
    assert report.suppressed_charters == (CHARTER,)
    assert report.review_charters == (CHARTER,)
    assert (
        stored.conn.execute(
            select(institutions.c.charter_verified).where(institutions.c.charter_number == CHARTER)
        ).scalar_one()
        == 0
    )
    assert reader.read(at=_MOMENT).offers == ()
    # No snapshot rebuild or pointer change is needed to suppress existing offers.
    assert reader.read(at=_MOMENT).info.snapshot_id == "before-import"
    assert ("institution", "60002") not in denylist.active_targets(_MOMENT)


@pytest.mark.parametrize("status", ["Merged", "Liquidated", "Converted"])
@pytest.mark.parametrize("exact_replay", [False, True])
def test_legacy_same_status_and_artifact_replays_repair_missing_protection(
    stored: StoredFixture,
    public_snapshot: tuple[SnapshotReader, Denylist],
    status: str,
    exact_replay: bool,
) -> None:
    reader, denylist = public_snapshot
    successor = "60002" if status == "Merged" else ""
    csv = _snapshot_roster(status, successor=successor)
    if exact_replay:
        # A legacy import predates independent suppression. Its append-only
        # artifact/history remain valid; repair must not duplicate either row.
        artifact_id = sha256(csv).hexdigest()
        stored.run.store.put(
            csv,
            media_type="text/csv; charset=utf-8",
            source_url="https://www.ncua.gov/data/credit-union-roster.csv",
            fetched_at=_BIRTH,
            http_status=200,
            fetch_run_id="legacy-roster",
        )
        stored.conn.execute(
            roster_imports.insert().values(
                import_id="legacy-roster",
                schema_version="1.0",
                source_version="reconciliation-test",
                source_as_of_date="2026-11-16",
                retrieved_at=_BIRTH,
                artifact_id=artifact_id,
                row_count=2,
                imported_count=1,
            )
        )
        stored.conn.execute(
            roster_import_entries.insert().values(
                import_id="legacy-roster",
                charter_number=CHARTER,
                schema_version="1.0",
                legal_name="Example North Charter",
                status=status.lower(),
                successor_charter_number=60002 if successor else None,
                source_as_of_date="2026-11-16",
            )
        )
    stored.conn.execute(
        update(institutions)
        .where(institutions.c.charter_number == CHARTER)
        .values(
            status=status.lower(),
            charter_verified=1,
            successor_charter_number=60002 if successor else None,
        )
    )
    report = _snapshot_import(stored, denylist, csv)
    assert report.is_noop is exact_replay
    assert report.review_charters == (CHARTER,)
    assert reader.read(at=_MOMENT).offers == ()
    assert (
        stored.conn.execute(
            select(institutions.c.charter_verified).where(institutions.c.charter_number == CHARTER)
        ).scalar_one()
        == 0
    )
    ledger = denylist.path.read_bytes()
    _snapshot_import(stored, denylist, csv, moment=_MOMENT + timedelta(seconds=1))
    assert denylist.path.read_bytes() == ledger
    assert len(stored.conn.execute(select(withdrawal_entries)).all()) == 1


@pytest.mark.parametrize("successor", ["", "bogus", str(CHARTER), "cycle", "closed"])
@pytest.mark.parametrize("verified", [False, True])
def test_rejected_successors_still_hide_known_inactive_charters(
    stored: StoredFixture,
    public_snapshot: tuple[SnapshotReader, Denylist],
    successor: str,
    verified: bool,
) -> None:
    reader, denylist = public_snapshot
    stored.conn.execute(
        update(institutions)
        .where(institutions.c.charter_number == CHARTER)
        .values(charter_verified=int(verified))
    )
    csv = _snapshot_roster("Merged", successor=successor)
    if successor in {"cycle", "closed"}:
        csv = _snapshot_roster("Merged").replace(
            b"60002,Successor CU,MA,Active,Cambridge,,,2026-11-16",
            (
                f"60002,Successor CU,MA,Merged,Cambridge,2026-11-15,{CHARTER},2026-11-16"
                if successor == "cycle"
                else "60002,Successor CU,MA,Liquidated,Cambridge,2026-11-15,,2026-11-16"
            ).encode(),
        )
    report = _snapshot_import(stored, denylist, csv)
    assert report.malformed_rows
    assert CHARTER in report.suppressed_charters
    assert CHARTER in report.review_charters
    row = stored.conn.execute(
        select(institutions).where(institutions.c.charter_number == CHARTER)
    ).one()
    assert row.charter_verified == 0
    assert row.status == "active"  # Successor details are still rejected.
    assert row.successor_charter_number is None
    assert reader.read(at=_MOMENT).offers == ()


def test_merger_suppression_does_not_hide_successor_snapshot(
    stored: StoredFixture, public_snapshot: tuple[SnapshotReader, Denylist]
) -> None:
    reader, denylist = public_snapshot
    # The real fixture offer belongs to the successor in this scenario.
    stored.conn.execute(
        institutions.insert().values(
            charter_number=60002,
            schema_version="1.0",
            legal_name="Successor CU",
            status="active",
            charter_verified=1,
        )
    )
    report = _snapshot_import(
        stored, denylist, _snapshot_roster("Merged", charter=60002, successor=str(CHARTER))
    )
    assert report.suppressed_charters == (60002,)
    assert {o.institution_charter for o in reader.read(at=_MOMENT).offers} == {CHARTER}
    assert (
        stored.conn.execute(
            select(institutions.c.charter_verified).where(institutions.c.charter_number == CHARTER)
        ).scalar_one()
        == 1
    )


@pytest.mark.parametrize("status", ["Merged", "Liquidated", "Converted"])
@pytest.mark.parametrize("failure", ["after-ledger", "outer-rollback"])
def test_rollback_retry_repairs_db_without_duplicate_immutable_ledger_entry(
    stored: StoredFixture,
    public_snapshot: tuple[SnapshotReader, Denylist],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    status: str,
) -> None:
    reader, denylist = public_snapshot
    csv = _snapshot_roster(status, successor="60002" if status == "Merged" else "")
    real_withdraw = denylist.withdraw

    def crash(entry: WithdrawalEntry) -> None:
        real_withdraw(entry)
        assert reader.read(at=_MOMENT).offers == ()
        raise RuntimeError("after independent suppression")

    if failure == "after-ledger":
        monkeypatch.setattr(denylist, "withdraw", crash)
        with pytest.raises(RuntimeError, match="after independent"):
            _snapshot_import(stored, denylist, csv)
        monkeypatch.setattr(denylist, "withdraw", real_withdraw)
    else:
        with pytest.raises(RuntimeError, match="outer rollback"), stored.conn.begin_nested():
            _snapshot_import(stored, denylist, csv)
            raise RuntimeError("outer rollback")
    assert stored.conn.execute(select(withdrawal_entries)).all() == []
    assert stored.conn.execute(select(roster_imports)).all() == []
    ledger = denylist.path.read_bytes()
    original = denylist.entries()
    assert len(original) == 1
    assert original[0].withdrawal_id == f"roster-{sha256(csv).hexdigest()}.suppress.{CHARTER}"
    assert reader.read(at=_MOMENT).offers == ()
    # Retry in a later process/time must preserve ID, actor, reason and timestamp.
    for seconds in (1, 2):
        _snapshot_import(stored, denylist, csv, moment=_MOMENT + timedelta(seconds=seconds))
        assert denylist.path.read_bytes() == ledger
        rows = stored.conn.execute(select(withdrawal_entries)).mappings().all()
        assert len(rows) == 1
        assert dict(rows[0]) == original[0].model_dump(exclude={"lifted_at", "lifted_by"})
        assert reader.read(at=_MOMENT + timedelta(seconds=seconds)).offers == ()


def test_snapshot_restoration_requires_explicit_recorded_human_lift(
    stored: StoredFixture, public_snapshot: tuple[SnapshotReader, Denylist]
) -> None:
    reader, denylist = public_snapshot
    _snapshot_import(stored, denylist, _snapshot_roster("Merged"))
    later = _MOMENT + timedelta(minutes=1)
    _snapshot_import(
        stored, denylist, _snapshot_roster("Active", successor="", as_of="2026-11-17"), moment=later
    )
    assert reader.read(at=later).offers == ()
    assert stored.conn.execute(select(withdrawal_lifts)).all() == []
    (entry,) = denylist.entries()
    # A DB-only lift cannot restore the independent reader either.
    stored.conn.execute(
        withdrawal_lifts.insert().values(
            withdrawal_id=entry.withdrawal_id,
            lifted_at=later,
            lifted_by="human-reviewer",
        )
    )
    assert reader.read(at=later).offers == ()
    denylist.lift(entry.withdrawal_id, at=later, actor="human-reviewer")
    assert {o.institution_charter for o in reader.read(at=later).offers} == {CHARTER}
    (lifted,) = denylist.entries()
    assert lifted.lifted_by == "human-reviewer"
    assert lifted.lifted_at == later


@pytest.mark.parametrize("status", ["Merged", "Liquidated", "Converted"])
def test_newer_same_status_import_clears_reverification_and_reuses_suppression(
    stored: StoredFixture, public_snapshot: tuple[SnapshotReader, Denylist], status: str
) -> None:
    reader, denylist = public_snapshot
    successor = "60002" if status == "Merged" else ""
    _snapshot_import(stored, denylist, _snapshot_roster(status, successor=successor))
    ledger = denylist.path.read_bytes()
    stored.conn.execute(
        update(institutions)
        .where(institutions.c.charter_number == CHARTER)
        .values(charter_verified=1)
    )
    report = _snapshot_import(
        stored,
        denylist,
        _snapshot_roster(status, successor=successor, as_of="2026-11-17"),
        moment=_MOMENT + timedelta(seconds=1),
    )
    assert report.suppressed_charters == (CHARTER,)
    assert report.review_charters == (CHARTER,)
    assert (
        stored.conn.execute(
            select(institutions.c.charter_verified).where(institutions.c.charter_number == CHARTER)
        ).scalar_one()
        == 0
    )
    assert denylist.path.read_bytes() == ledger
    assert len(stored.conn.execute(select(withdrawal_entries)).all()) == 1
    assert reader.read(at=_MOMENT + timedelta(seconds=1)).offers == ()


@pytest.mark.parametrize("status", ["Merged", "Liquidated", "Converted"])
def test_stale_inactive_roster_cannot_change_identity_or_suppress_snapshot(
    stored: StoredFixture, public_snapshot: tuple[SnapshotReader, Denylist], status: str
) -> None:
    reader, denylist = public_snapshot
    _snapshot_import(stored, denylist, _snapshot_roster("Active", successor="", as_of="2026-11-17"))
    report = _snapshot_import(
        stored, denylist, _snapshot_roster(status, successor="60002" if status == "Merged" else "")
    )
    assert CHARTER in report.stale_charters
    assert report.suppressed_charters == ()
    assert denylist.entries() == ()
    row = stored.conn.execute(
        select(institutions).where(institutions.c.charter_number == CHARTER)
    ).one()
    assert row.status == "active"
    assert row.charter_verified == 1
    assert {o.institution_charter for o in reader.read(at=_MOMENT).offers} == {CHARTER}
