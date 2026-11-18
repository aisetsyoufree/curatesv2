"""A3: operator writes keep ledgers private and readable by the web-reader group."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cu_rates.contracts.enums import WithdrawalScope
from cu_rates.snapshot import Denylist

from .contracts import factories as make


def test_root_ledger_writes_assign_parent_group_before_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "independent"
    directory.mkdir(mode=0o755)
    path = directory / "denylist.json"
    real_stat = Path.stat
    real_replace = os.replace
    group = directory.stat().st_gid + 10000
    assignments: list[tuple[int, int]] = []

    def parent_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        result = real_stat(self, follow_symlinks=follow_symlinks)
        if self == directory:
            values = list(result)
            values[5] = group
            return os.stat_result(values)
        return result

    def chown(fd: int, uid: int, gid: int) -> None:
        # Inject the privileged operation; this test runs without root or skips.
        assert stat.S_ISREG(os.fstat(fd).st_mode)
        assignments.append((uid, gid))

    def replace(source: Path, target: Path) -> None:
        assert assignments, "web-reader group must be assigned before replacement"
        assert assignments[-1] == (-1, group)
        assert len(assignments) == len(replacements) + 1
        assert stat.S_IMODE(source.stat().st_mode) == 0o640
        replacements.append(target)
        real_replace(source, target)

    replacements: list[Path] = []
    monkeypatch.setattr(Path, "stat", parent_stat)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "fchown", chown)
    monkeypatch.setattr(os, "replace", replace)
    ledger = Denylist(path)
    ledger.initialize()
    # An older install may have used 0644; every rewrite must remove world read.
    path.chmod(0o644)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    entry = make.withdrawal(WithdrawalScope.SOURCE, "synthetic-source", withdrawn_at=now)
    ledger.withdraw(entry)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert ledger.active_targets(now) == frozenset({("source", "synthetic-source")})
    ledger.lift(entry.withdrawal_id, at=now + timedelta(seconds=1), actor="operator")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert not ledger.active_targets(now + timedelta(seconds=1))
    assert assignments == [(-1, group)] * 3


def test_failed_group_assignment_keeps_previous_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Denylist(tmp_path / "denylist.json")
    ledger.initialize()
    before = ledger.path.read_bytes()
    before_mode = stat.S_IMODE(ledger.path.stat().st_mode)

    def refused(fd: int, uid: int, gid: int) -> None:
        raise PermissionError("cannot assign web-reader group")

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "fchown", refused)
    entry = make.withdrawal(WithdrawalScope.SOURCE, "synthetic-source")
    with pytest.raises(PermissionError, match="web-reader group"):
        ledger.withdraw(entry)
    assert ledger.path.read_bytes() == before
    assert stat.S_IMODE(ledger.path.stat().st_mode) == before_mode
    assert not list(tmp_path.glob(".denylist.json-*"))


def test_unprivileged_writes_need_no_chown_and_remain_group_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected(fd: int, uid: int, gid: int) -> None:
        pytest.fail("unprivileged writes must not need chown")

    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "fchown", unexpected)
    ledger = Denylist(tmp_path / "denylist.json")
    ledger.initialize()
    ledger.withdraw(make.withdrawal(WithdrawalScope.SOURCE, "synthetic-source"))
    assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o640
    assert ledger.path.stat().st_gid == tmp_path.stat().st_gid
