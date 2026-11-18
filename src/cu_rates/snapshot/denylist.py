"""Independent emergency withdrawal ledger. Missing/broken state blocks public reads."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import ValidationError, model_validator

from cu_rates.contracts.audit import WithdrawalEntry
from cu_rates.contracts.base import ContractModel

from ._files import SnapshotError, atomic_write, read_bytes, writer_lock

_MAX_LEDGER_BYTES = 8 * 1024 * 1024


class _Ledger(ContractModel):
    version: Literal[1]
    entries: tuple[WithdrawalEntry, ...]

    @model_validator(mode="after")
    def _unique_entries(self) -> Self:
        if len({item.withdrawal_id for item in self.entries}) != len(self.entries):
            raise ValueError("duplicate withdrawal ID")
        return self


class Denylist:
    """Operator API. Initialize once; withdraw/lift without the collector or publisher.

    Store on a trusted local filesystem in a separately writable operator directory.
    The web user needs read access only. Actor and rationale never enter snapshot views.
    """

    def __init__(self, path: Path) -> None:
        self.path = path.absolute()

    def _read(self) -> _Ledger:
        try:
            return _Ledger.model_validate_json(read_bytes(self.path, _MAX_LEDGER_BYTES))
        except (OSError, ValidationError) as exc:
            raise SnapshotError("withdrawal ledger missing or invalid") from exc

    def _save(self, value: _Ledger) -> None:
        payload = value.model_dump_json().encode()
        if len(payload) > _MAX_LEDGER_BYTES:
            raise SnapshotError("withdrawal ledger exceeds its size limit")
        atomic_write(self.path, payload, mode=0o640)

    @property
    def _lock(self) -> Path:
        return self.path.with_name(f".{self.path.name}.lock")

    def initialize(self) -> None:
        """Explicitly establish an empty ledger; never recreate a missing ledger on read."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with writer_lock(self._lock):
            try:
                self.path.lstat()
            except FileNotFoundError:
                self._save(_Ledger(version=1, entries=()))
            else:
                self._read()

    def withdraw(self, entry: WithdrawalEntry) -> None:
        entry = WithdrawalEntry.model_validate(entry.model_dump())
        if entry.lifted_at is not None:
            raise ValueError("record a withdrawal before lifting it")
        with writer_lock(self._lock):
            ledger = self._read()
            prior = next(
                (row for row in ledger.entries if row.withdrawal_id == entry.withdrawal_id), None
            )
            if prior is not None:
                if prior != entry:
                    raise ValueError(
                        "withdrawal ID already exists with different content or a lift"
                    )
                return  # Retrying after a database failure never duplicates suppression.
            self._save(_Ledger(version=1, entries=(*ledger.entries, entry)))

    def lift(self, withdrawal_id: str, *, at: datetime, actor: str) -> None:
        with writer_lock(self._lock):
            ledger = self._read()
            found = next(
                (row for row in ledger.entries if row.withdrawal_id == withdrawal_id), None
            )
            if found is None or found.lifted_at is not None:
                raise ValueError("withdrawal does not exist or has already been lifted")
            lifted = WithdrawalEntry.model_validate(
                {**found.model_dump(), "lifted_at": at, "lifted_by": actor}
            )
            self._save(
                _Ledger(
                    version=1,
                    entries=tuple(lifted if row == found else row for row in ledger.entries),
                )
            )

    def active_targets(self, at: datetime) -> frozenset[tuple[str, str]]:
        """Read the current ledger on every request, including when the publisher is down."""
        return frozenset(
            (row.scope.value, row.target_id) for row in self._read().entries if row.in_force_at(at)
        )
