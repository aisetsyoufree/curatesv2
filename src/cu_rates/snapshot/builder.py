"""Worker-side admission and atomic activation; never called by the web process."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit, urlunsplit

from pydantic import JsonValue, TypeAdapter, ValidationError
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from cu_rates.artifact_store import ArtifactStore, ArtifactStoreError
from cu_rates.contracts.audit import PublishedSnapshot, ReviewDecision, snapshot_manifest_sha256
from cu_rates.contracts.base import ContractModel, Identifier
from cu_rates.contracts.enums import OfferStatus, ReviewSubject
from cu_rates.contracts.fingerprints import OfferSnapshot, offer_fingerprints
from cu_rates.contracts.offers import NormalizedOffer
from cu_rates.contracts.publication import (
    REVIEW_REASONS,
    Placement,
    PublicationContext,
    PublicationOutcome,
    ReasonCode,
    corroborated_paths,
    evaluate_publication,
    publication_evidence_sha256,
)
from cu_rates.contracts.sources import ExtractedCandidate
from cu_rates.pipeline.slice import verified_offer_candidates
from cu_rates.pipeline.trusted import load_publication_context
from cu_rates.validation import RULE_IDS, ValidationContext, validate_offer

from ._database import (
    APPLICATION_ID,
    DB_NAME,
    DESCRIPTOR,
    FORMAT_VERSION,
    POINTER,
    VERSIONS,
    database_digest,
    read_version,
    snapshot_entry,
    verify_database,
)
from ._files import (
    SnapshotError,
    atomic_write,
    regular_file,
    require_directory,
    sync_directory,
    writer_lock,
)
from ._models import (
    PublicAssumption,
    PublicCondition,
    PublicEvidence,
    PublicOffer,
    PublicTier,
    SnapshotInfo,
    Version,
)

_ID = TypeAdapter(Identifier)


class SnapshotCandidate(ContractModel):
    """Internal projection input; the publisher constructs it from persisted evidence."""

    offer: NormalizedOffer
    context: PublicationContext
    candidates: tuple[ExtractedCandidate, ...]
    current_review: ReviewDecision | None = None


def _load_candidate(
    conn: Connection, store: ArtifactStore, offer_id: str, manifest: PublishedSnapshot
) -> SnapshotCandidate:
    view = load_publication_context(conn, offer_id, manifest.published_at, store=store)
    # The trusted loader re-verifies every separately persisted capture against its
    # stored bytes and locators. Never accept a caller's offer, context, or review.
    primary = next(
        (
            artifact
            for artifact in view.context.known_artifacts
            if artifact.fetched_at == view.offer.observed_at
            and artifact.artifact_id in {ref.artifact_id for ref in view.offer.iter_provenance()}
        ),
        None,
    )
    if not view.context.known_artifacts or (
        primary is None and view.offer.status is OfferStatus.QUARANTINED
    ):
        raise SnapshotError(f"stored evidence bytes missing or unverifiable: {offer_id}")
    if primary is None:
        # T045 does not accept a refreshed 304 observation in place of a direct capture.
        raise SnapshotError(f"T045 refused offer without a direct stored capture: {offer_id}")
    candidates, _ = verified_offer_candidates(
        view.context.source, view.offer, store.read(primary), primary
    )
    return SnapshotCandidate(
        offer=view.offer,
        context=view.context,
        candidates=candidates,
        current_review=view.current_review,
    )


def _public_url(url: str) -> str:
    # Query values (including quote inputs and tracking IDs) are never public.
    parsed = urlsplit(url)
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise SnapshotError("invalid public source URL")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _without_provenance(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {k: _without_provenance(v) for k, v in value.items() if k != "provenance"}
    if isinstance(value, list):
        return [_without_provenance(v) for v in value]
    return value


def _public_projection(row: SnapshotCandidate, placement: Placement, reviewed: bool) -> PublicOffer:
    offer, ctx = row.offer, row.context
    if offer.valid_until is None:
        raise SnapshotError("offer has no independently enforceable expiry")
    details: dict[str, JsonValue] = {
        key: _without_provenance(value)
        for key, value in offer.details.model_dump(mode="json").items()
    }
    facts = OfferSnapshot.from_offer(offer)
    evidence = {
        PublicEvidence(path=path, artifact_id=ref.artifact_id, locator=ref.locator)
        for path, ref in offer.iter_labeled_provenance()
    }
    paths = corroborated_paths(offer, ctx)
    evidence.update(
        PublicEvidence(path=p.path, artifact_id=p.artifact_id, locator=p.provenance.locator)
        for p in paths
    )
    # Additive metadata in the existing public details map preserves format-1 readers.
    # Excerpts and private extraction text remain excluded from public exports.
    details["publication_evidence"] = [
        {
            "path": p.path,
            "artifact_id": p.artifact_id,
            "content_sha256": p.content_sha256,
            "capture_id": p.capture_id,
            "observed_at": p.observed_at.isoformat(),
            "source_id": p.source_id,
            "locator": p.provenance.locator.model_dump(mode="json"),
        }
        for p in paths
    ]
    return PublicOffer(
        offer_id=offer.offer_id,
        product_id=offer.product_id,
        source_id=offer.source_id,
        institution_charter=offer.institution_charter,
        institution_name=ctx.institution.legal_name,
        source_url=_public_url(ctx.source.url),
        family=offer.family,
        purpose=offer.purpose,
        scenario_id=offer.scenario_id,
        placement=placement,
        review_status="reviewed" if reviewed else "automatic",
        effective_at=offer.effective_at,
        observed_at=offer.observed_at,
        valid_until=offer.valid_until,
        rates=facts.rates,
        points=facts.points,
        details=details,
        assumptions=tuple(
            PublicAssumption(dimension=match.dimension, state=match.state, stated=match.stated)
            for match in offer.scenario_matches
        ),
        conditions=tuple(
            PublicCondition(
                condition_id=item.condition_id, kind=item.kind, scenario_state=item.scenario_state
            )
            for item in offer.conditions
        ),
        tiers=tuple(PublicTier(tier_id=item.tier_id, bounds=item.bounds) for item in offer.tiers),
        headline_is_as_low_as=offer.headline_is_as_low_as,
        evidence=tuple(sorted(evidence, key=lambda item: item.model_dump_json())),
        fingerprints=facts.fingerprints,
    )


def _admit(row: SnapshotCandidate, manifest: PublishedSnapshot) -> PublicOffer:
    offer = row.offer
    ctx = PublicationContext.model_validate(
        {
            **row.context.model_dump(),
            "evaluated_at": manifest.published_at,
            "snapshot_id": manifest.snapshot_id,
        }
    )
    if row.context.evaluated_at != manifest.published_at:
        raise SnapshotError("worker inputs must be read as of snapshot generation")
    if offer.status not in (OfferStatus.APPROVED, OfferStatus.PUBLISHED):
        raise SnapshotError(f"offer is not in a publishable state: {offer.offer_id}")
    review = row.current_review
    if review is not None and not (
        review.approved
        and review.subject is ReviewSubject.OFFER
        and review.subject_id == offer.offer_id
        and review.decided_at <= ctx.evaluated_at
    ):
        raise SnapshotError(
            f"current offer review is not applicable and approved: {offer.offer_id}"
        )

    decision = evaluate_publication(offer, ctx)
    reviewed = (
        decision.outcome is PublicationOutcome.REQUIRES_REVIEW
        and set(decision.reasons) & REVIEW_REASONS == {ReasonCode.FIRST_PUBLICATION}
        and review is not None
        and offer.observed_at <= review.decided_at
        and review.review_id in manifest.review_ids
        and review.reviewed_fingerprints == offer_fingerprints(offer)
        and review.reviewed_evidence_sha256
        == publication_evidence_sha256(offer, corroborated_paths(offer, ctx))
    )
    if not (decision.publishable_now or reviewed) or decision.placement is Placement.NONE:
        reasons = ", ".join(reason.value for reason in decision.reasons)
        raise SnapshotError(f"publication gate refused offer: {offer.offer_id}: {reasons}")

    proof = validate_offer(
        offer.model_copy(update={"status": OfferStatus.CANDIDATE, "published_at": None}),
        ValidationContext(
            evaluated_at=ctx.evaluated_at,
            source=ctx.source,
            product=ctx.product,
            fetch_runs=ctx.fetch_runs,
            artifacts=ctx.known_artifacts,
            candidates=row.candidates,
        ),
    )
    if (
        proof.quarantine is not None
        or tuple(result.rule_id for result in proof.results) != RULE_IDS
        or not all(result.passed for result in proof.results)
        or not proof.for_publication(offer)
    ):
        raise SnapshotError(f"T045 refused offer: {offer.offer_id}")
    placement = decision.placement
    if placement is Placement.RANKED and (decision.degraded or proof.comparison_blockers):
        placement = Placement.BROWSE_ONLY
    return _public_projection(row, placement, reviewed)


def _write_database(path: Path, info: SnapshotInfo, offers: tuple[PublicOffer, ...]) -> None:
    with closing(sqlite3.connect(path)) as conn:
        if conn.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise SnapshotError("could not enable SQLite rollback journaling")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
        conn.execute(f"PRAGMA user_version={FORMAT_VERSION}")
        with conn:
            conn.execute(
                "CREATE TABLE snapshot_info (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                "payload TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE public_offers "
                "(offer_id TEXT PRIMARY KEY NOT NULL, payload TEXT NOT NULL)"
            )
            conn.execute("INSERT INTO snapshot_info VALUES (1, ?)", (info.model_dump_json(),))
            conn.executemany(
                "INSERT INTO public_offers VALUES (?, ?)",
                ((row.offer_id, row.model_dump_json()) for row in offers),
            )
    path.chmod(0o444)
    with regular_file(path) as fd:
        os.fsync(fd)


class SnapshotPublisher:
    """Build, verify, seal, and activate explicit manifests on one local filesystem.

    Version IDs cannot be reused. The current pointer is the commit point. Deployment
    must make this tree writable only by the publisher, with the website read-only.
    Failed admission, staging, or verification never changes the current pointer.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.absolute()

    def _activate(self, version: Version) -> None:
        atomic_write(self.root / POINTER, version.model_dump_json().encode(), mode=0o644)

    def _prepare(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        require_directory(self.root)
        versions = self.root / VERSIONS
        versions.mkdir(exist_ok=True)
        require_directory(versions)
        return versions

    def build(
        self, manifest: PublishedSnapshot, *, conn: Connection, store: ArtifactStore
    ) -> SnapshotInfo:
        """Reload all declared offers from worker storage; refuse any unverifiable release.

        The caller owns the connection's read transaction. No in-memory captures,
        contexts, extracted claims, or review decisions are accepted by this API.
        """
        try:
            manifest = PublishedSnapshot.model_validate(manifest.model_dump())
            offers = tuple(
                sorted(
                    (
                        _admit(_load_candidate(conn, store, entry.offer_id, manifest), manifest)
                        for entry in manifest.entries
                    ),
                    key=lambda o: o.offer_id,
                )
            )
            entries = tuple(snapshot_entry(offer) for offer in offers)
            if (
                entries != manifest.entries
                or snapshot_manifest_sha256(entries) != manifest.manifest_sha256
            ):
                raise SnapshotError("admitted offers do not match the declared manifest")
            info = SnapshotInfo.model_validate(
                manifest.model_dump(exclude={"entries", "review_ids"})
            )
            versions = self._prepare()
            with writer_lock(self.root / ".publisher.lock"):
                pointer = self.root / POINTER
                try:
                    pointer.lstat()
                except FileNotFoundError:
                    active = None
                else:
                    active = read_version(pointer).snapshot_id
                if active != manifest.supersedes:
                    raise SnapshotError("manifest predecessor is not the current snapshot")
                target = versions / info.snapshot_id
                try:
                    target.lstat()
                except FileNotFoundError:
                    pass
                else:
                    raise SnapshotError("snapshot version already exists")
                with TemporaryDirectory(dir=versions, prefix=".build-") as staging:
                    stage = Path(staging) / "sealed"
                    stage.mkdir(mode=0o755)
                    _write_database(stage / DB_NAME, info, offers)
                    version = Version(
                        version=1,
                        snapshot_id=info.snapshot_id,
                        database_sha256=database_digest(stage / DB_NAME),
                    )
                    if verify_database(stage, version) != (info, offers):
                        raise SnapshotError(
                            "staged public rows differ from the admitted projection"
                        )
                    atomic_write(stage / DESCRIPTOR, version.model_dump_json().encode(), mode=0o444)
                    os.rename(stage, target)
                    sync_directory(versions)
                self._activate(version)
            return info
        except (
            ArtifactStoreError,
            OSError,
            sqlite3.Error,
            SQLAlchemyError,
            ValueError,
            ValidationError,
        ) as exc:
            raise SnapshotError("snapshot build could not verify persisted evidence") from exc

    def rollback(self, snapshot_id: str) -> SnapshotInfo:
        """Verify a retained version before pointing to it; never rewrite its database."""
        try:
            snapshot_id = _ID.validate_python(snapshot_id)
            versions = self._prepare()
            with writer_lock(self.root / ".publisher.lock"):
                directory = versions / snapshot_id
                require_directory(directory)
                version = read_version(directory / DESCRIPTOR)
                if version.snapshot_id != snapshot_id:
                    raise SnapshotError("rollback descriptor names a different version")
                info, _ = verify_database(directory, version)
                self._activate(version)
            return info
        except (OSError, ValueError) as exc:
            raise SnapshotError("rollback version could not be verified") from exc
