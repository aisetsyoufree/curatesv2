"""Persisted acquisition policy. Read current registry and independent stops per request."""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from cu_rates.contracts.audit import WithdrawalEntry
from cu_rates.contracts.base import ContractModel, HttpUrlText, Sha256Hex
from cu_rates.contracts.enums import WithdrawalScope
from cu_rates.contracts.sources import FetchProfile, RateSource, SourceApproval, profile_sha256
from cu_rates.db.schema import (
    fetch_profiles,
    institution_source_relationships,
    rate_sources,
    source_approvals,
    withdrawal_entries,
    withdrawal_lifts,
)
from cu_rates.snapshot import Denylist
from cu_rates.snapshot._files import (
    SnapshotError,
    atomic_write,
    read_bytes,
    regular_file,
    writer_lock,
)

TERMS_TTL = 90 * 24 * 60 * 60
_MAX_TERMS_BYTES = 8 * 1024 * 1024


class PolicyError(RuntimeError):
    """An acquisition policy could not be established safely."""


class TermsReview(SourceApproval):
    """A separate reviewer decision, persisted before collection is requested."""

    terms_url: HttpUrlText
    terms_sha256: Sha256Hex


class _Terms(ContractModel):
    version: Literal[1]
    entries: tuple[TermsReview, ...]


class TermsLedger:
    """Private operator-written review history; the gateway only reads it."""

    def __init__(self, path: Path) -> None:
        self.path = path.absolute()

    def read(self) -> tuple[TermsReview, ...]:
        ledger = _Terms.model_validate_json(read_bytes(self.path, _MAX_TERMS_BYTES))
        if len({row.approval_id for row in ledger.entries}) != len(ledger.entries):
            raise ValueError("duplicate terms review ID")
        return ledger.entries

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with writer_lock(self.path.with_name(f".{self.path.name}.lock")):
            try:
                self.path.lstat()
            except FileNotFoundError:
                atomic_write(
                    self.path, _Terms(version=1, entries=()).model_dump_json().encode(), mode=0o600
                )
            else:
                self.read()

    def record(self, review: TermsReview, *, denylist: Denylist) -> None:
        review = TermsReview.model_validate(review.model_dump())
        # A newly recorded prohibition must suppress existing public values first.
        if not review.approved:
            denylist.withdraw(
                WithdrawalEntry(
                    schema_version=review.schema_version,
                    withdrawal_id=review.approval_id,
                    scope=WithdrawalScope.SOURCE,
                    target_id=review.source_id,
                    actor=review.actor,
                    withdrawn_at=review.decided_at,
                    reason=review.rationale,
                )
            )
        with writer_lock(self.path.with_name(f".{self.path.name}.lock")):
            entries = self.read()
            prior = next((row for row in entries if row.approval_id == review.approval_id), None)
            if prior is not None:
                if prior != review:
                    raise ValueError("terms review ID already exists")
                return
            payload = _Terms(version=1, entries=(*entries, review)).model_dump_json().encode()
            if len(payload) > _MAX_TERMS_BYTES:
                raise ValueError("terms ledger exceeds its size limit")
            atomic_write(self.path, payload, mode=0o600)


class FetchRegistry:
    """Read-only view of operational approvals plus independently persisted policy.

    Paths are deployment configuration, never per-fetch arguments. No read creates a
    database or an empty ledger. A latest rejection revokes older approval; ambiguous
    simultaneous decisions fail closed. Approvals require quarterly reauthorization.
    """

    def __init__(
        self,
        database: Path,
        terms: TermsLedger,
        denylist: Denylist,
        *,
        automatic: Denylist | None = None,
    ) -> None:
        self.database = database.absolute()
        self.terms = terms
        self.denylist = denylist  # operator-owned: read here, never written by the worker
        self.automatic = automatic  # worker-owned: where the worker records its own blocks

    @property
    def suppression(self) -> Denylist:
        """Where the worker writes an automatic block: its own ledger when one is configured
        (production), else the single ledger (tests and the offline slice)."""
        return self.automatic if self.automatic is not None else self.denylist

    def authorize(self, source: RateSource, profile: FetchProfile, at: datetime) -> TermsReview:
        try:
            return self._authorize(source, profile, at)
        except (OSError, ValueError, ValidationError, SQLAlchemyError, SnapshotError) as exc:
            raise PolicyError("policy-state-unavailable") from exc

    def _authorize(self, source: RateSource, profile: FetchProfile, at: datetime) -> TermsReview:
        with regular_file(self.database):
            pass
        engine = create_engine(
            "sqlite://",
            creator=lambda: sqlite3.connect(
                f"{self.database.as_uri()}?mode=ro", uri=True, autocommit=False
            ),
            poolclass=NullPool,
        )
        try:
            with engine.begin() as conn:
                stored_source = (
                    conn.execute(
                        select(rate_sources).where(rate_sources.c.source_id == source.source_id)
                    )
                    .mappings()
                    .one()
                )
                stored_profile = (
                    conn.execute(
                        select(fetch_profiles).where(
                            fetch_profiles.c.profile_id == profile.profile_id
                        )
                    )
                    .mappings()
                    .one()
                )
                if (
                    RateSource.model_validate(dict(stored_source)) != source
                    or FetchProfile.model_validate(dict(stored_profile)) != profile
                ):
                    raise PolicyError("withdrawn-or-unsupported-profile")
                rows = (
                    conn.execute(
                        select(source_approvals)
                        .where(
                            source_approvals.c.source_id == source.source_id,
                            source_approvals.c.decided_at <= at,
                        )
                        .order_by(source_approvals.c.decided_at.desc())
                    )
                    .mappings()
                    .all()
                )
                if not rows or (len(rows) > 1 and rows[0]["decided_at"] == rows[1]["decided_at"]):
                    raise PolicyError("source-not-approved")
                data = dict(rows[0])
                name, version = data.pop("adapter_name"), data.pop("adapter_version")
                data["adapter"] = None if name is None else {"name": name, "version": version}
                data["approved_origins"] = data.pop("approved_origins_json") or ()
                approval = SourceApproval.model_validate(data)
                if (
                    not approval.approved
                    or approval.source_url != source.url
                    or approval.approved_profile_sha256 != profile_sha256(profile)
                    or (at - approval.decided_at).total_seconds() >= TERMS_TTL
                ):
                    raise PolicyError("source-not-approved")
                targets = {("source", source.source_id)}
                for row in conn.execute(
                    select(institution_source_relationships).where(
                        institution_source_relationships.c.source_id == source.source_id
                    )
                ):
                    targets.add(("institution", str(row.institution_charter)))
                    if row.product_id is not None:
                        targets.add(("product", row.product_id))
                lifts = {row.withdrawal_id: row for row in conn.execute(select(withdrawal_lifts))}
                for row in conn.execute(select(withdrawal_entries)):
                    lift = lifts.get(row.withdrawal_id)
                    entry = WithdrawalEntry.model_validate(
                        {
                            **dict(row._mapping),
                            "lifted_at": None if lift is None else lift.lifted_at,
                            "lifted_by": None if lift is None else lift.lifted_by,
                        }
                    )
                    if entry.in_force_at(at) and (entry.scope.value, entry.target_id) in targets:
                        raise PolicyError("source-withdrawn")
        finally:
            engine.dispose()
        if self.denylist.active_targets(at) & targets:
            raise PolicyError("source-withdrawn")
        if self.automatic is not None and self.automatic.active_targets(at) & targets:
            raise PolicyError("source-withdrawn")
        terms = sorted(
            (
                row
                for row in self.terms.read()
                if row.source_id == source.source_id and row.decided_at <= at
            ),
            key=lambda row: row.decided_at,
            reverse=True,
        )
        if not terms or (len(terms) > 1 and terms[0].decided_at == terms[1].decided_at):
            raise PolicyError("source-not-approved")
        review = terms[0]
        if (
            not review.approved
            or review.source_url != source.url
            or review.approval_id == approval.approval_id
        ):
            raise PolicyError("source-not-approved")
        if (at - review.decided_at).total_seconds() >= TERMS_TTL:
            raise PolicyError("terms-review-expired")
        return review
