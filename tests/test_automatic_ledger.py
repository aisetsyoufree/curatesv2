"""#118: the worker records its own blocks in a separate ledger the public reader also enforces.

The operator ledger stays operator-only (the worker cannot write it); the public reader combines
both, and once the automatic ledger is configured a missing or invalid one blocks public reads.
"""

from datetime import timedelta
from pathlib import Path

import pytest

from cu_rates.contracts.audit import WithdrawalEntry
from cu_rates.contracts.enums import WithdrawalScope
from cu_rates.ops.health import check_health
from cu_rates.snapshot import Denylist, SnapshotError, SnapshotReader
from cu_rates.web.config import validated_config

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


def _entry(scope: WithdrawalScope, target: str, at, ident: str = "auto-1") -> WithdrawalEntry:  # type: ignore[no-untyped-def]
    return WithdrawalEntry(
        schema_version=make.SV,
        withdrawal_id=ident,
        scope=scope,
        target_id=target,
        actor="fetch-worker",
        withdrawn_at=at,
        reason="blocked by the source",
    )


@pytest.fixture
def env(tmp_path: Path, stored: StoredFixture):  # type: ignore[no-untyped-def]
    publisher, _reader, operator = setup_storage(tmp_path)
    item = stored.row(now=_BIRTH)
    publisher.build(release("first", item), conn=stored.conn, store=stored.run.store)
    automatic = Denylist(tmp_path / "automatic" / "denylist.json")
    automatic.initialize()
    reader = SnapshotReader(publisher.root, operator.path, automatic_path=automatic.path)
    return publisher, operator, automatic, reader, item


def test_an_automatic_suppression_hides_offers_for_the_public(env) -> None:  # type: ignore[no-untyped-def]
    _publisher, _operator, automatic, reader, item = env
    moment = _BIRTH + timedelta(minutes=1)
    assert reader.read(at=moment).offers
    automatic.withdraw(_entry(WithdrawalScope.SOURCE, item.offer.source_id, moment))
    assert reader.read(at=moment - timedelta(microseconds=1)).offers
    assert reader.read(at=moment).offers == ()
    # Lifting is an operator action on that ledger and restores it.
    automatic.lift("auto-1", at=moment + timedelta(minutes=1), actor="operator")
    assert reader.read(at=moment + timedelta(minutes=1)).offers


def test_both_ledgers_are_enforced_independently(env) -> None:  # type: ignore[no-untyped-def]
    _publisher, operator, automatic, reader, item = env
    moment = _BIRTH + timedelta(minutes=1)
    operator.withdraw(_entry(WithdrawalScope.OFFER, item.offer.offer_id, moment, "op-1"))
    assert reader.read(at=moment).offers == ()
    operator.lift("op-1", at=moment + timedelta(minutes=1), actor="owner")
    assert reader.read(at=moment + timedelta(minutes=1)).offers
    automatic.withdraw(_entry(WithdrawalScope.PRODUCT, item.offer.product_id, moment))
    assert reader.read(at=moment + timedelta(minutes=2)).offers == ()


def test_a_missing_or_invalid_automatic_ledger_fails_closed(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    publisher, operator, automatic, _reader, _item = env
    moment = _BIRTH + timedelta(minutes=1)
    automatic.path.write_text("{not json")
    broken = SnapshotReader(publisher.root, operator.path, automatic_path=automatic.path)
    with pytest.raises(SnapshotError):
        broken.read(at=moment)
    absent = SnapshotReader(
        publisher.root, operator.path, automatic_path=tmp_path / "nowhere" / "denylist.json"
    )
    with pytest.raises(SnapshotError):
        absent.read(at=moment)


def test_without_an_automatic_ledger_the_reader_behaves_as_before(env) -> None:  # type: ignore[no-untyped-def]
    publisher, operator, _automatic, _reader, _item = env
    plain = SnapshotReader(publisher.root, operator.path)
    assert plain.read(at=_BIRTH + timedelta(minutes=1)).offers


def test_the_health_check_enforces_the_same_ledgers_as_the_site(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    publisher, operator, automatic, _reader, _item = env
    moment = _BIRTH + timedelta(minutes=1)
    ok = check_health(
        snapshot_root=publisher.root,
        denylist_path=operator.path,
        automatic_denylist_path=automatic.path,
        now=moment,
    )
    assert ok.ok and any(c.name == "automatic_denylist_readable" for c in ok.checks)
    automatic.path.write_text("{not json")
    bad = check_health(
        snapshot_root=publisher.root,
        denylist_path=operator.path,
        automatic_denylist_path=automatic.path,
        now=moment,
    )
    assert not bad.ok
    names = {c.name for c in bad.checks if c.status == "fail"}
    assert {"snapshot_readable", "automatic_denylist_readable"} <= names


def test_the_web_setting_must_be_an_absolute_normalized_path() -> None:
    assert validated_config({"AUTOMATIC_DENYLIST_PATH": "/var/lib/cu-rates/automatic/d.json"})
    for bad in ("relative.json", "/x/../y.json", "//host/x", ""):
        with pytest.raises(ValueError, match="AUTOMATIC_DENYLIST_PATH"):
            validated_config({"AUTOMATIC_DENYLIST_PATH": bad})
