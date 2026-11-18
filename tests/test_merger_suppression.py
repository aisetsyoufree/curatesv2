"""AUD7-7: merger/closure in a roster import clears eligibility and suppresses offers.

Each test must FAIL without the fix and PASS with it. The tests do not mirror
the implementation: they describe externally observable behaviour that the
issue requires.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, update

from cu_rates.artifact_store import ArtifactStore
from cu_rates.contracts.enums import InstitutionStatus, WithdrawalScope
from cu_rates.db import open_operational_database, upgrade_database
from cu_rates.db.schema import institutions, withdrawal_entries, withdrawal_lifts
from cu_rates.roster import import_roster
from cu_rates.snapshot import Denylist

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

        # V3 did not transition again: not suppressed.
        assert 60001 not in v3_report.suppressed_charters

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


def test_merger_suppresses_public_reads_via_the_independent_denylist(tmp_path: Path) -> None:
    """The reader enforces only the independent denylist, so it must hold the suppression."""
    engine = open_operational_database(_database(tmp_path))
    store, denylist = _store(tmp_path), _denylist(tmp_path)
    steps: tuple[tuple[bytes, str, datetime], ...] = (
        (_V1_CSV, "v1", FROZEN_V1),
        (_V2_MERGED_CSV, "v2", FROZEN_V2),
    )
    for csv, version, moment in steps:
        import_roster(
            csv,
            engine_or_connection=engine,
            artifact_store=store,
            source_version=version,
            clock=_at(moment),
            denylist=denylist,
        )
    assert ("institution", "60001") in denylist.active_targets(FROZEN_V2)
    assert ("institution", "60002") not in denylist.active_targets(FROZEN_V2)


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
