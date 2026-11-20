"""Private offline rate batch stager (M4 / T046).

Reconciles candidate rate rows and explicit absence findings against DB identity
bindings and private Content-Addressed Evidence Store (ArtifactStore).
Enforces idempotent replay, conflict detection on altered payloads, candidate-only
staging, auditable reason codes for holds/rejects, and atomic transaction rollback.
Public normalized tables remain strictly untouched.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, insert, select
from sqlalchemy.exc import IntegrityError, OperationalError

from cu_rates.artifact_store import (
    ArtifactCorruptError,
    ArtifactNotFoundError,
    ArtifactStore,
    ArtifactStoreError,
)
from cu_rates.contracts.enums import RunStatus
from cu_rates.db.schema import (
    fetch_runs,
    institution_source_relationships,
    institutions,
    products,
    rate_sources,
    staged_rate_batches,
    staged_rate_candidates,
    staged_rate_findings,
)
from cu_rates.rate_batch.locators import verify_batch_item_locators
from cu_rates.rate_batch.models import (
    MAX_BATCH_BYTES,
    BatchCandidate,
    BatchConflictError,
    BatchDisposition,
    BatchFinding,
    BatchItemType,
    HoldReason,
    OversizedInputError,
    RateBatchEnvelope,
    RejectReason,
)
from cu_rates.rate_batch.parser import parse_rate_batch


@dataclass(frozen=True)
class BatchItemReport:
    """Disposition of an individual candidate or finding in an imported batch."""

    item_id: str
    item_type: BatchItemType
    disposition: BatchDisposition
    hold_reason: HoldReason | None = None
    reject_reason: RejectReason | None = None
    detail: str | None = None


@dataclass(frozen=True)
class BatchImportReport:
    """Summary and reconciliation of an offline rate batch intake."""

    batch_id: str
    batch_digest: str
    is_noop: bool = False
    dry_run: bool = False
    submitted_count: int = 0
    staged_count: int = 0
    held_count: int = 0
    rejected_count: int = 0
    already_imported_count: int = 0
    candidate_count: int = 0
    finding_count: int = 0
    candidates_staged: int = 0
    candidates_held: int = 0
    candidates_rejected: int = 0
    findings_staged: int = 0
    findings_held: int = 0
    findings_rejected: int = 0
    hold_reasons: dict[str, int] = field(default_factory=dict)
    reject_reasons: dict[str, int] = field(default_factory=dict)
    items: tuple[BatchItemReport, ...] = field(default_factory=tuple)
    imported_at: datetime | None = None

    @property
    def submitted(self) -> int:
        return self.submitted_count

    @property
    def staged(self) -> int:
        return self.staged_count

    @property
    def held(self) -> int:
        return self.held_count

    @property
    def rejected(self) -> int:
        return self.rejected_count

    @property
    def already_imported(self) -> int:
        return self.already_imported_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "batch_digest": self.batch_digest,
            "is_noop": self.is_noop,
            "dry_run": self.dry_run,
            "submitted_count": self.submitted_count,
            "staged_count": self.staged_count,
            "held_count": self.held_count,
            "rejected_count": self.rejected_count,
            "already_imported_count": self.already_imported_count,
            "candidate_count": self.candidate_count,
            "finding_count": self.finding_count,
            "candidates_staged": self.candidates_staged,
            "candidates_held": self.candidates_held,
            "candidates_rejected": self.candidates_rejected,
            "findings_staged": self.findings_staged,
            "findings_held": self.findings_held,
            "findings_rejected": self.findings_rejected,
            "hold_reasons": self.hold_reasons,
            "reject_reasons": self.reject_reasons,
            "imported_at": self.imported_at.isoformat() if self.imported_at else None,
        }


class RateBatchStager:
    """Stager for offline rate candidate batches.

    Validates untrusted model extractions against immutable private store evidence
    and existing operational DB registry bindings. Writes only to candidate-only
    staging tables in SQLite.
    """

    def __init__(
        self,
        engine_or_connection: Engine | Connection,
        artifact_store: ArtifactStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._target = engine_or_connection
        self._store = artifact_store
        self._clock = clock or (lambda: datetime.now(UTC))

    def stage(
        self,
        content: str | bytes,
        *,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> BatchImportReport:
        """Stage a batch payload (JSON or JSONL string or bytes).

        Args:
            content: Raw batch file content.
            dry_run: When True, performs all validation without writing to SQLite.
            now: Reference instant for future timestamp validation.

        Returns:
            BatchImportReport summarizing reconciliation and auditable holds.

        Raises:
            OversizedInputError: When content exceeds MAX_BATCH_BYTES.
            BatchConflictError: When batch_id already exists with different contents.
            HashTamperingError: When content digest mismatches claimed digest.
            ForbiddenFieldError: When publication/approval fields are present.
            ValueError: On malformed JSON, syntax, float, or datetime errors.
        """
        raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
        if len(raw_bytes) > MAX_BATCH_BYTES:
            raise OversizedInputError(
                f"batch payload size {len(raw_bytes)} bytes exceeds limit {MAX_BATCH_BYTES}"
            )
        raw_bytes_hash = hashlib.sha256(raw_bytes).hexdigest()
        eval_now = now if now is not None else self._clock()

        envelope = parse_rate_batch(raw_bytes, now=eval_now)

        if not isinstance(self._target, Engine):
            # Caller-owned Connection: writes go in a savepoint and are never
            # committed or rolled back here; the caller decides the outcome.
            raw = self._target.connection.driver_connection
            if not dry_run and (not isinstance(raw, sqlite3.Connection) or not raw.in_transaction):
                # A logical begin on legacy SQLite may defer the physical BEGIN.
                # Releasing an outermost savepoint there would commit early.
                raise ValueError("caller connection must already hold a SQLite transaction")
            return self._stage_with_connection(
                self._target, envelope, raw_bytes_hash, dry_run=dry_run, eval_now=eval_now
            )

        # Engine-owned transaction. The first SELECT autobegins one (the
        # operational engine uses autocommit=False), so the writes sit in a
        # savepoint and this method commits the outer transaction itself.
        # A concurrent writer can win between our read and our write; retry once
        # on a fresh connection so the replay/conflict check sees its commit.
        attempts = 2
        for attempt in range(attempts):
            with self._target.connect() as conn:
                try:
                    report = self._stage_with_connection(
                        conn, envelope, raw_bytes_hash, dry_run=dry_run, eval_now=eval_now
                    )
                    if dry_run:
                        conn.rollback()
                    else:
                        conn.commit()
                    return report
                except (IntegrityError, OperationalError):
                    conn.rollback()
                    if dry_run or attempt == attempts - 1:
                        raise
        raise AssertionError("unreachable")  # pragma: no cover

    def _replay_report(
        self,
        conn: Connection,
        envelope: RateBatchEnvelope,
        raw_bytes_hash: str,
        *,
        dry_run: bool,
        eval_now: datetime,
    ) -> BatchImportReport | None:
        """No-op report for an identical stored batch; conflict error if it differs."""
        existing = conn.execute(
            select(
                staged_rate_batches.c.batch_digest,
                staged_rate_batches.c.raw_bytes_hash,
                staged_rate_batches.c.item_count,
                staged_rate_batches.c.imported_at,
            ).where(staged_rate_batches.c.batch_id == envelope.batch_id)
        ).first()
        if existing is None:
            return None
        if (
            existing.batch_digest != envelope.batch_digest
            or existing.raw_bytes_hash != raw_bytes_hash
        ):
            raise BatchConflictError(
                f"batch_id {envelope.batch_id!r} already exists with different contents"
            )
        return BatchImportReport(
            batch_id=envelope.batch_id,
            batch_digest=envelope.batch_digest,
            is_noop=True,
            dry_run=dry_run,
            submitted_count=existing.item_count,
            already_imported_count=existing.item_count,
            candidate_count=len(envelope.candidates),
            finding_count=len(envelope.findings),
            imported_at=existing.imported_at,
        )

    @contextmanager
    def _write_guard(
        self,
        conn: Connection,
        envelope: RateBatchEnvelope,
        raw_bytes_hash: str,
        eval_now: datetime,
        replayed: list[BatchImportReport],
    ) -> Iterator[None]:
        """Savepoint around all staging inserts; never commits ``conn``.

        A failed write leaves no partial rows. If a concurrent writer already
        staged this batch, report an identical replay as a no-op, raise
        BatchConflictError for changed contents, and otherwise re-raise.
        """
        try:
            with conn.begin_nested():
                yield
        except (IntegrityError, OperationalError):
            report = self._replay_report(
                conn, envelope, raw_bytes_hash, dry_run=False, eval_now=eval_now
            )
            if report is None:
                raise
            replayed.append(report)

    def _stage_with_connection(
        self,
        conn: Connection,
        envelope: RateBatchEnvelope,
        raw_bytes_hash: str,
        *,
        dry_run: bool,
        eval_now: datetime,
    ) -> BatchImportReport:
        # 1. Replay / Conflict check
        replay = self._replay_report(
            conn, envelope, raw_bytes_hash, dry_run=dry_run, eval_now=eval_now
        )
        if replay is not None:
            return replay

        total_submitted = len(envelope.candidates) + len(envelope.findings)

        # 2. Check observations against DB and ArtifactStore. A capture is
        # identified by charter, source, and run, so one CU's capture can never
        # vouch for another CU's rows. Conflicting duplicate declarations fail.
        obs_dispositions: dict[tuple[int, str, str], tuple[RejectReason | None, str | None]] = {}
        declared_obs: dict[tuple[int, str, str], Any] = {}
        conflicting_obs: set[tuple[int, str, str]] = set()
        for obs in envelope.observations:
            key = (obs.institution_charter, obs.source_id, obs.fetch_run_id)
            prior = declared_obs.get(key)
            if prior is not None:
                if prior != obs:
                    conflicting_obs.add(key)
                continue
            declared_obs[key] = obs
            reason: RejectReason | None = None
            detail: str | None = None

            # a. Institution in DB
            inst_row = conn.execute(
                select(institutions.c.charter_number).where(
                    institutions.c.charter_number == obs.institution_charter
                )
            ).first()
            if inst_row is None:
                reason = RejectReason.INSTITUTION_NOT_FOUND
                detail = f"institution {obs.institution_charter} not found in database"
            else:
                # b. Source in DB
                src_row = conn.execute(
                    select(rate_sources.c.source_id, rate_sources.c.url).where(
                        rate_sources.c.source_id == obs.source_id
                    )
                ).first()
                if src_row is None:
                    reason = RejectReason.SOURCE_NOT_FOUND
                    detail = f"rate source {obs.source_id!r} not found in database"
                elif src_row.url != obs.source_url:
                    reason = RejectReason.CROSS_REFERENCE_MISMATCH
                    detail = f"source URL {obs.source_url!r} does not match DB URL {src_row.url!r}"
                else:
                    # c. Product in DB (if specified on observation)
                    if obs.product_id is not None:
                        prod_row = conn.execute(
                            select(products.c.product_id).where(
                                products.c.institution_charter == obs.institution_charter,
                                products.c.product_id == obs.product_id,
                            )
                        ).first()
                        if prod_row is None:
                            reason = RejectReason.PRODUCT_NOT_FOUND
                            detail = (
                                f"product {obs.product_id!r} not found for charter "
                                f"{obs.institution_charter}"
                            )

                    if reason is None:
                        # d. Relationship in DB
                        rel_stmt = select(institution_source_relationships.c.relationship_id).where(
                            institution_source_relationships.c.institution_charter
                            == obs.institution_charter,
                            institution_source_relationships.c.source_id == obs.source_id,
                        )
                        if obs.product_id is not None:
                            rel_stmt = rel_stmt.where(
                                (institution_source_relationships.c.product_id.is_(None))
                                | (institution_source_relationships.c.product_id == obs.product_id)
                            )
                        rel_row = conn.execute(rel_stmt).first()
                        if rel_row is None:
                            reason = RejectReason.RELATIONSHIP_NOT_FOUND
                            detail = (
                                f"no relationship binding charter {obs.institution_charter} to "
                                f"source {obs.source_id!r}"
                            )
                        else:
                            # e. FetchRun in DB
                            fr_row = conn.execute(
                                select(
                                    fetch_runs.c.run_id,
                                    fetch_runs.c.status,
                                    fetch_runs.c.started_at,
                                    fetch_runs.c.finished_at,
                                ).where(
                                    fetch_runs.c.run_id == obs.fetch_run_id,
                                    fetch_runs.c.source_id == obs.source_id,
                                )
                            ).first()
                            if fr_row is None:
                                reason = RejectReason.FETCH_RUN_NOT_FOUND
                                detail = f"fetch_run {obs.fetch_run_id!r} not found in database"
                            elif (
                                fr_row.status != RunStatus.SUCCEEDED.value
                                or not fr_row.started_at <= obs.observed_at <= fr_row.finished_at
                            ):
                                reason = RejectReason.CROSS_REFERENCE_MISMATCH
                                detail = "observation lies outside a successful recorded run"
                            else:
                                # f. ArtifactStore observation check
                                try:
                                    stored_obs = self._store.get_observation(
                                        obs.fetch_run_id, src_row.url
                                    )
                                    if stored_obs.artifact_id != obs.artifact_id:
                                        reason = RejectReason.HASH_TAMPERING
                                        detail = (
                                            f"claimed artifact digest {obs.artifact_id} does not "
                                            "match store observation digest "
                                            f"{stored_obs.artifact_id}"
                                        )
                                    elif stored_obs.source_url != obs.source_url:
                                        reason = RejectReason.CROSS_REFERENCE_MISMATCH
                                        detail = "store observation source_url mismatch"
                                    elif stored_obs.fetched_at != obs.observed_at:
                                        reason = RejectReason.CROSS_REFERENCE_MISMATCH
                                        detail = "store observation fetched_at mismatch"
                                    elif stored_obs.media_type != obs.media_type:
                                        reason = RejectReason.CROSS_REFERENCE_MISMATCH
                                        detail = "store observation media_type mismatch"
                                    elif stored_obs.http_status != 200:
                                        reason = RejectReason.CROSS_REFERENCE_MISMATCH
                                        detail = "intake requires a successful full-body capture"
                                except ArtifactNotFoundError:
                                    reason = RejectReason.FETCH_RUN_NOT_FOUND
                                    detail = (
                                        f"observation ({obs.fetch_run_id}, {src_row.url}) not "
                                        f"found in store"
                                    )
                                except (ArtifactCorruptError, ArtifactStoreError) as exc:
                                    reason = RejectReason.HASH_TAMPERING
                                    detail = f"store error reading observation: {exc}"

            obs_dispositions[key] = (reason, detail)

        for key in conflicting_obs:
            obs_dispositions[key] = (
                RejectReason.CROSS_REFERENCE_MISMATCH,
                "conflicting duplicate observation declarations",
            )

        # 3. Verify candidates
        candidate_records: list[
            tuple[
                BatchCandidate,
                BatchDisposition,
                HoldReason | None,
                RejectReason | None,
                str | None,
            ]
        ] = []
        for cand in envelope.candidates:
            key = (cand.institution_charter, cand.source_id, cand.fetch_run_id)
            obs_err, obs_detail = obs_dispositions.get(
                key, (RejectReason.CROSS_REFERENCE_MISMATCH, "undeclared observation")
            )
            declared = declared_obs.get(key)
            if (
                obs_err is None
                and declared is not None
                and declared.artifact_id != cand.artifact_id
            ):
                obs_err = RejectReason.CROSS_REFERENCE_MISMATCH
                obs_detail = "candidate artifact_id does not match its declared capture"
            if obs_err is not None:
                candidate_records.append(
                    (cand, BatchDisposition.REJECTED, None, obs_err, obs_detail)
                )
                continue

            # Candidate product in DB
            prod_row = conn.execute(
                select(products.c.product_id, products.c.family).where(
                    products.c.institution_charter == cand.institution_charter,
                    products.c.product_id == cand.product_id,
                )
            ).first()
            if prod_row is None:
                candidate_records.append(
                    (
                        cand,
                        BatchDisposition.REJECTED,
                        None,
                        RejectReason.PRODUCT_NOT_FOUND,
                        (
                            f"product {cand.product_id!r} not found for charter "
                            f"{cand.institution_charter}"
                        ),
                    )
                )
                continue
            if prod_row.family != cand.product_family.value:
                candidate_records.append(
                    (
                        cand,
                        BatchDisposition.REJECTED,
                        None,
                        RejectReason.CROSS_REFERENCE_MISMATCH,
                        "candidate product family does not match the registered product",
                    )
                )
                continue

            # Relationship for candidate's product
            rel_row = conn.execute(
                select(institution_source_relationships.c.relationship_id).where(
                    institution_source_relationships.c.institution_charter
                    == cand.institution_charter,
                    institution_source_relationships.c.source_id == cand.source_id,
                    (institution_source_relationships.c.product_id.is_(None))
                    | (institution_source_relationships.c.product_id == cand.product_id),
                )
            ).first()
            if rel_row is None:
                candidate_records.append(
                    (
                        cand,
                        BatchDisposition.REJECTED,
                        None,
                        RejectReason.RELATIONSHIP_NOT_FOUND,
                        f"no relationship for product {cand.product_id!r}",
                    )
                )
                continue

            # Verify locators against store
            disp, hold_reason, detail = verify_batch_item_locators(
                cand, self._store, review=envelope.independent_review
            )
            candidate_records.append((cand, disp, hold_reason, None, detail))

        # 4. Verify findings
        finding_records: list[
            tuple[
                BatchFinding,
                BatchDisposition,
                HoldReason | None,
                RejectReason | None,
                str | None,
            ]
        ] = []
        for finding in envelope.findings:
            key = (finding.institution_charter, finding.source_id, finding.fetch_run_id)
            obs_err, obs_detail = obs_dispositions.get(
                key, (RejectReason.CROSS_REFERENCE_MISMATCH, "undeclared observation")
            )
            declared = declared_obs.get(key)
            if (
                obs_err is None
                and declared is not None
                and declared.artifact_id != finding.artifact_id
            ):
                obs_err = RejectReason.CROSS_REFERENCE_MISMATCH
                obs_detail = "finding artifact_id does not match its declared capture"
            if obs_err is not None:
                finding_records.append(
                    (finding, BatchDisposition.REJECTED, None, obs_err, obs_detail)
                )
                continue

            disp, hold_reason, detail = verify_batch_item_locators(
                finding, self._store, review=envelope.independent_review
            )
            finding_records.append((finding, disp, hold_reason, None, detail))

        # 6. Aggregate reconciliation
        staged_count = sum(
            1 for _, disp, _, _, _ in candidate_records if disp == BatchDisposition.STAGED
        ) + sum(1 for _, disp, _, _, _ in finding_records if disp == BatchDisposition.STAGED)
        held_count = sum(
            1 for _, disp, _, _, _ in candidate_records if disp == BatchDisposition.HELD
        ) + sum(1 for _, disp, _, _, _ in finding_records if disp == BatchDisposition.HELD)
        rejected_count = sum(
            1 for _, disp, _, _, _ in candidate_records if disp == BatchDisposition.REJECTED
        ) + sum(1 for _, disp, _, _, _ in finding_records if disp == BatchDisposition.REJECTED)

        cands_staged = sum(
            1 for _, disp, _, _, _ in candidate_records if disp == BatchDisposition.STAGED
        )
        cands_held = sum(
            1 for _, disp, _, _, _ in candidate_records if disp == BatchDisposition.HELD
        )
        cands_rejected = sum(
            1 for _, disp, _, _, _ in candidate_records if disp == BatchDisposition.REJECTED
        )

        finds_staged = sum(
            1 for _, disp, _, _, _ in finding_records if disp == BatchDisposition.STAGED
        )
        finds_held = sum(1 for _, disp, _, _, _ in finding_records if disp == BatchDisposition.HELD)
        finds_rejected = sum(
            1 for _, disp, _, _, _ in finding_records if disp == BatchDisposition.REJECTED
        )

        hold_reason_counts: Counter[str] = Counter()
        reject_reason_counts: Counter[str] = Counter()
        item_reports: list[BatchItemReport] = []

        for cand, disp, hold_r, rej_r, det in candidate_records:
            if hold_r:
                hold_reason_counts[hold_r.value] += 1
            if rej_r:
                reject_reason_counts[rej_r.value] += 1
            item_reports.append(
                BatchItemReport(
                    item_id=cand.candidate_id,
                    item_type=BatchItemType.CANDIDATE,
                    disposition=disp,
                    hold_reason=hold_r,
                    reject_reason=rej_r,
                    detail=det,
                )
            )

        for finding, disp, hold_r, rej_r, det in finding_records:
            if hold_r:
                hold_reason_counts[hold_r.value] += 1
            if rej_r:
                reject_reason_counts[rej_r.value] += 1
            item_reports.append(
                BatchItemReport(
                    item_id=finding.finding_id,
                    item_type=BatchItemType.FINDING,
                    disposition=disp,
                    hold_reason=hold_r,
                    reject_reason=rej_r,
                    detail=det,
                )
            )

        # 7. Atomic transaction write (nonmutating on dry_run)
        replayed: list[BatchImportReport] = []
        if not dry_run:
            with self._write_guard(conn, envelope, raw_bytes_hash, eval_now, replayed):
                conn.execute(
                    insert(staged_rate_batches).values(
                        batch_id=envelope.batch_id,
                        schema_version=envelope.schema_version,
                        batch_digest=envelope.batch_digest,
                        operator=envelope.operator,
                        model_name=envelope.model_name,
                        tool_name=envelope.tool_name,
                        recipe_version=envelope.recipe_version,
                        prompt_version=envelope.prompt_version,
                        submitted_at=envelope.submitted_at,
                        imported_at=eval_now,
                        raw_bytes_hash=raw_bytes_hash,
                        item_count=total_submitted,
                        staged_count=staged_count,
                        held_count=held_count,
                        rejected_count=rejected_count,
                        independent_review_json=(
                            envelope.independent_review.model_dump(mode="json")
                            if envelope.independent_review
                            else None
                        ),
                    )
                )

                for cand, disp, hold_r, rej_r, det in candidate_records:
                    conn.execute(
                        insert(staged_rate_candidates).values(
                            candidate_id=cand.candidate_id,
                            batch_id=envelope.batch_id,
                            row_id=cand.row_id,
                            institution_charter=cand.institution_charter,
                            product_id=cand.product_id,
                            source_id=cand.source_id,
                            fetch_run_id=cand.fetch_run_id,
                            artifact_id=cand.artifact_id,
                            disposition=disp.value,
                            hold_reason=hold_r.value if hold_r else None,
                            reject_reason=rej_r.value if rej_r else None,
                            detail=det,
                            product_family=cand.product_family.value,
                            loan_purpose=(cand.loan_purpose.value if cand.loan_purpose else None),
                            term_months=cand.term_months,
                            note_rate=cand.note_rate,
                            apr=cand.apr,
                            points=cand.points,
                            raw_text=cand.raw_text,
                            payload_json=cand.model_dump(mode="json"),
                            staged_at=eval_now,
                        )
                    )

                for finding, disp, hold_r, rej_r, det in finding_records:
                    conn.execute(
                        insert(staged_rate_findings).values(
                            finding_id=finding.finding_id,
                            batch_id=envelope.batch_id,
                            institution_charter=finding.institution_charter,
                            source_id=finding.source_id,
                            fetch_run_id=finding.fetch_run_id,
                            artifact_id=finding.artifact_id,
                            disposition=disp.value,
                            hold_reason=hold_r.value if hold_r else None,
                            reject_reason=rej_r.value if rej_r else None,
                            detail=det,
                            label=finding.label,
                            scope=finding.scope.value,
                            family=finding.family.value if finding.family else None,
                            rate_availability=(
                                finding.rate_availability.value
                                if finding.rate_availability
                                else None
                            ),
                            raw_text=finding.raw_text,
                            payload_json=finding.model_dump(mode="json"),
                            staged_at=eval_now,
                        )
                    )

        if replayed:
            return replayed[0]

        return BatchImportReport(
            batch_id=envelope.batch_id,
            batch_digest=envelope.batch_digest,
            is_noop=False,
            dry_run=dry_run,
            submitted_count=total_submitted,
            staged_count=staged_count,
            held_count=held_count,
            rejected_count=rejected_count,
            already_imported_count=0,
            candidate_count=len(envelope.candidates),
            finding_count=len(envelope.findings),
            candidates_staged=cands_staged,
            candidates_held=cands_held,
            candidates_rejected=cands_rejected,
            findings_staged=finds_staged,
            findings_held=finds_held,
            findings_rejected=finds_rejected,
            hold_reasons=dict(hold_reason_counts),
            reject_reasons=dict(reject_reason_counts),
            items=tuple(item_reports),
            imported_at=eval_now,
        )
