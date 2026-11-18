"""T014 integration checks over stored, synthetic offline evidence."""

from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import select

from cu_rates.contracts.audit import WithdrawalEntry
from cu_rates.contracts.base import CONTRACT_VERSION
from cu_rates.contracts.enums import OfferStatus, ProductFamily, ReviewVerdict, WithdrawalScope
from cu_rates.contracts.evidence import RawArtifact
from cu_rates.contracts.fingerprints import offer_fingerprints
from cu_rates.contracts.offers import PurchaseMortgageDetails
from cu_rates.contracts.publication import (
    APR_RULE_ID,
    Placement,
    PublicationOutcome,
    ReasonCode,
    evaluate_publication,
)
from cu_rates.contracts.sources import (
    ConditionalValidator,
    FetchProfile,
    FetchRun,
    NotModifiedObservation,
    profile_sha256,
)
from cu_rates.db import open_operational_database
from cu_rates.db.schema import (
    artifact_blobs,
    correction_evidence,
    corrections,
    extracted_candidates,
    extraction_runs,
    finding_evidence,
    product_findings,
    published_snapshots,
    slice_normalization_failures,
    slice_snapshot_baselines,
    validation_results,
)
from cu_rates.pipeline.extract import FIXTURES, layout_sha256, resolve_text
from cu_rates.pipeline.slice import (
    CHARTER,
    PRODUCT_ID,
    REVIEWER,
    SOURCE_ID,
    SliceRun,
    claim_text,
    classify_fixture,
    record_fixture_capture,
    run_fixture,
)
from cu_rates.pipeline.trusted import (
    TrustedView,
    insert_approval,
    insert_artifact,
    insert_capture,
    insert_profile,
    insert_reobservation,
    insert_review,
    insert_run,
    insert_validations,
    insert_withdrawal,
    load_publication_context,
    publish,
    stored_provenance,
)
from cu_rates.pipeline.validate import validate_offer
from cu_rates.snapshot import Denylist

from .pub2_fixtures import reviewed_fixture

R = ReasonCode
_DATED = "athol-style-dated"
_THIRTY = "purchase-30-year"
_FIFTEEN = "purchase-15-year"


def _dated_body() -> bytes:
    return (FIXTURES / "athol_style_mortgage_dated.html").read_bytes()


def _view(
    run: SliceRun, *, after: timedelta = timedelta(minutes=4), row: str = _THIRTY
) -> TrustedView:
    offer = run.offers[row]
    with open_operational_database(run.database).connect() as conn:
        return load_publication_context(
            conn, offer.offer_id, offer.observed_at + after, store=run.store
        )


def _count_snapshots(run: SliceRun) -> int:
    with open_operational_database(run.database).connect() as conn:
        return len(conn.execute(select(published_snapshots.c.snapshot_id)).all())


def _later_200(
    run: SliceRun,
    body: bytes,
    validator: ConditionalValidator | None = None,
    *,
    profile: FetchProfile | None = None,
) -> tuple[FetchRun, RawArtifact]:
    view = _view(run)
    current = view.context.fetch_runs[0]
    moment = run.offers[_THIRTY].observed_at + timedelta(hours=1)
    artifact = run.store.put(
        body,
        source_url=view.context.source.url,
        media_type="text/html; charset=utf-8",
        fetched_at=moment,
        http_status=200,
        fetch_run_id="synthetic-repeat-200",
    )
    fresh = current.model_copy(
        update={
            "run_id": artifact.fetch_run_id,
            "artifact_ids": (artifact.artifact_id,),
            "started_at": moment - timedelta(seconds=1),
            "finished_at": moment,
            "conditional_validator": validator,
        }
    )
    with open_operational_database(run.database).begin() as conn:
        if profile is not None:
            approval = view.context.source_approval
            assert approval is not None
            insert_profile(conn, profile)
            insert_approval(
                conn,
                approval.model_copy(
                    update={
                        "approval_id": "source-approval-revised-profile",
                        "approved_profile_sha256": profile_sha256(profile),
                        "decided_at": moment - timedelta(minutes=1),
                    }
                ),
                CHARTER,
            )
            fresh = fresh.model_copy(
                update={
                    "profile_id": profile.profile_id,
                    "profile_sha256": profile_sha256(profile),
                }
            )
        record_fixture_capture(conn, _DATED, run.store, fresh, artifact)
    return fresh, artifact


def test_dated_review_then_verified_304_rank_only_30y(tmp_path: Path) -> None:
    run = reviewed_fixture(tmp_path)
    another = reviewed_fixture(tmp_path / "replay")
    original, shorter = run.offers[_THIRTY], run.offers[_FIFTEEN]
    assert set(run.offers) == {_THIRTY, _FIFTEEN}
    assert original.status is OfferStatus.CANDIDATE
    assert isinstance(shorter.details, PurchaseMortgageDetails)
    assert shorter.details.term_months == 180
    assert run.first[_THIRTY].reasons == ()  # D-21: first publication is automatic
    assert run.first[_THIRTY].placement is Placement.RANKED
    # D-21: the 15-year row no longer waits for a first-publication review, but its terms differ
    # from the 30-year scenario, so it can never rank.
    assert not run.first[_FIFTEEN].ranking_eligible
    assert run.first[_FIFTEEN].placement is not Placement.RANKED
    assert run.reviewed_snapshot is not None and run.refreshed_snapshot is not None
    assert run.refreshed_snapshot.supersedes == run.reviewed_snapshot.snapshot_id
    assert run.refreshed_snapshot.entries == run.reviewed_snapshot.entries
    assert len(run.refreshed_snapshot.entries) == 1
    assert run.refreshed_snapshot.entries[0].offer_id == original.offer_id
    assert run.refreshed_snapshot == another.refreshed_snapshot
    assert run.refresh is not None and run.refresh.outcome is PublicationOutcome.AUTO_PUBLISH
    assert run.refresh.ranking_eligible and _count_snapshots(run) == 2
    assert original.effective_at is not None
    assert original.effective_at < original.observed_at
    assert any(
        claim_text(run.store, p) == "2026-11-16T09:00:00Z" for p in original.iter_provenance()
    )
    assert all(claim_text(run.store, p) == p.source_text for p in original.iter_provenance())
    view = _view(run)
    assert view.offer.observed_at > original.observed_at
    assert view.context.known_artifacts[0].fetched_at == original.observed_at
    assert view.context.known_artifacts[0].artifact_id == sha256(_dated_body()).hexdigest()


def test_undated_or_unreviewed_bytes_never_get_a_snapshot(tmp_path: Path) -> None:
    undated = run_fixture(tmp_path / "undated", "athol-style")
    assert R.EFFECTIVE_DATE_MISSING in undated.first[_THIRTY].reasons
    assert undated.reviewed_snapshot is None and _count_snapshots(undated) == 0
    altered = _dated_body().replace(b"6.375%", b"6.376%")
    run = run_fixture(tmp_path / "altered", body=altered)
    assert run.first[_THIRTY].outcome is PublicationOutcome.QUARANTINE
    note_rate = run.offers[_THIRTY].note_rate
    assert note_rate is not None
    assert note_rate.value == Decimal("6.376")
    assert run.reviewed_snapshot is None and _count_snapshots(run) == 0


@pytest.mark.parametrize(
    ("case", "charter", "source", "finding_count", "offer_count"),
    [
        ("stoughton-style", 9000003, "stoughton-style.lending", 3, 0),
        ("worcester-style", 9000002, "worcester-style.lending", 2, 1),
    ],
)
def test_other_fixtures_persist_their_own_page_findings(
    tmp_path: Path,
    case: str,
    charter: int,
    source: str,
    finding_count: int,
    offer_count: int,
) -> None:
    run = run_fixture(tmp_path, case)
    assert all(offer.family is ProductFamily.HOME_EQUITY_LOAN for offer in run.offers.values())
    assert all(offer.institution_charter == charter for offer in run.offers.values())
    assert len(run.offers) == offer_count
    assert len(run.findings) == finding_count
    assert all((f.institution_charter, f.source_id) == (charter, source) for f in run.findings)
    with open_operational_database(run.database).connect() as conn:
        assert len(conn.execute(select(product_findings)).all()) == finding_count
        assert len(conn.execute(select(finding_evidence)).all()) == finding_count
        extraction = conn.execute(select(extraction_runs)).one()
        assert extraction.status == "succeeded"
        assert extraction.candidate_count == len(conn.execute(select(extracted_candidates)).all())
    assert _count_snapshots(run) == 0


def test_parse_and_normalization_failures_are_visible_with_evidence(tmp_path: Path) -> None:
    wrong_page = (FIXTURES / "stoughton_style_consumer_loans.html").read_bytes()
    failed = run_fixture(tmp_path / "wrong", body=wrong_page)
    with open_operational_database(failed.database).connect() as conn:
        extraction = conn.execute(select(extraction_runs)).one()
        assert extraction.status == "failed" and "wrong source page" in extraction.error
        # The identity-review evidence page is also stored; the wrong source page must be too.
        assert sha256(wrong_page).hexdigest() in set(
            conn.execute(select(artifact_blobs.c.artifact_id)).scalars()
        )
    assert not failed.offers
    invalid_apr = _dated_body().replace(b"6.441%", b"6.100%")
    quarantined = run_fixture(tmp_path / "invalid", body=invalid_apr)
    assert set(quarantined.offers) == {_FIFTEEN}
    assert _count_snapshots(quarantined) == 0
    with open_operational_database(quarantined.database).connect() as conn:
        failure = conn.execute(select(slice_normalization_failures)).one()
        assert failure.row_id == _THIRTY and "APR is below" in failure.reason
        assert (
            "6.100%"
            in conn.execute(
                select(extracted_candidates.c.raw_value).where(
                    extracted_candidates.c.extraction_run_id == failure.extraction_run_id
                )
            )
            .scalars()
            .all()
        )


def test_extracts_complete_cells_and_rejects_unsupported_statements() -> None:
    body = _dated_body()
    # 16.375 is a different, fully parsed percent, never the substring 6.375.
    changed = body.replace(b"6.375%", b"16.375%").replace(b"6.441%", b"16.441%")
    offers, _ = classify_fixture(_DATED, changed)
    assert offers[0].note_rate is not None and offers[0].note_rate.value == Decimal("16.375")
    with pytest.raises(ValueError, match="one percentage"):
        classify_fixture(_DATED, body.replace(b"6.375%", b"above 6.375%"))
    with pytest.raises(ValueError, match="fixture assumption"):
        classify_fixture(_DATED, body.replace(b"Credit score 780", b"Not for credit score 780"))
    with pytest.raises(ValueError, match="rate row"):
        classify_fixture(_DATED, body.replace(b"Term 30 years", b"Term 15 years"))
    assert layout_sha256(changed) == layout_sha256(body)
    assert layout_sha256(
        body.replace(b'<th scope="col">APR', b'<th scope="col">Fee')
    ) != layout_sha256(body)


def test_locators_obey_sibling_element_and_attribute_semantics() -> None:
    body = (FIXTURES / "worcester_style_home_lending.html").read_bytes()
    url = "https://housing-partner.example/programs/example-housing"
    assert resolve_text(body, "a[href]") == "Example Housing Partner program information"
    assert resolve_text(body, f"//a[@href='{url}']/@href", "xpath") == url
    assert (
        resolve_text(b'<tr id="t"><th>A</th><td>B<em>C</em>D</td></tr>', "#t > td:nth-child(2)")
        == "BCD"
    )
    with pytest.raises(ValueError, match="does not resolve"):
        resolve_text(
            body.replace(b"<h2>First mortgage</h2>", b"<h2>First mortgage</h2><div></div>"),
            "h2:nth-of-type(1) + p",
        )
    with pytest.raises(ValueError, match="uniquely"):
        resolve_text(b'<a href="x">One</a><a href="y">Two</a>', "a[href]")
    with pytest.raises(ValueError, match="unresolvable"):
        resolve_text(b'<tr id="t"><th>A</th><td>B</td></tr>', "#t > td:nth-child(1)")


@pytest.mark.parametrize("tamper", ["delete", "same-size corruption"])
def test_blob_metadata_alone_cannot_feed_a_snapshot(tmp_path: Path, tamper: str) -> None:
    run = reviewed_fixture(tmp_path)
    view = _view(run)
    artifact = view.context.known_artifacts[0]
    blob = run.store._blob_path(artifact.artifact_id)
    if tamper == "delete":
        blob.unlink()
    else:
        blob.chmod(0o600)
        blob.write_bytes(b"!" * artifact.byte_length)
        blob.chmod(0o400)
    after = _view(run)
    decision = evaluate_publication(after.offer, after.context)
    assert artifact.artifact_id not in {a.artifact_id for a in after.context.known_artifacts}
    assert R.ARTIFACT_NOT_STORED in decision.reasons
    with open_operational_database(run.database).begin() as conn:
        assert (
            publish(
                conn,
                after.offer.offer_id,
                store=run.store,
                evaluated_at=after.context.evaluated_at,
                snapshot_id="unsafe",
            )
            is None
        )
    assert _count_snapshots(run) == 2


def test_valid_blob_does_not_make_a_forged_claim_locator_valid(tmp_path: Path) -> None:
    run = reviewed_fixture(tmp_path)
    current = _view(run)
    direct = run.offers[_THIRTY]
    assert direct.note_rate is not None and direct.apr is not None
    original = current.context.fetch_runs[0]
    moment = direct.observed_at + timedelta(minutes=10)
    artifact = run.store.put(
        _dated_body(),
        source_url=current.context.source.url,
        media_type="text/html; charset=utf-8",
        fetched_at=moment,
        http_status=200,
        fetch_run_id="valid-body-forged-locator",
    )
    fresh = original.model_copy(
        update={
            "run_id": artifact.fetch_run_id,
            "started_at": moment,
            "finished_at": moment,
        }
    )
    # APR deliberately cites the note-rate cell. The actual bytes, hash and locator all
    # resolve, but the offered APR magnitude is not the value of that locator.
    forged = direct.model_copy(
        update={
            "observed_at": moment,
            "valid_until": moment + timedelta(hours=71),
            "apr": direct.apr.model_copy(update={"provenance": direct.note_rate.provenance}),
        }
    )
    with open_operational_database(run.database).begin() as conn:
        insert_run(conn, fresh)
        insert_artifact(conn, artifact)
        insert_capture(conn, forged, fresh.run_id)
        insert_validations(conn, validate_offer(forged, moment))
    view = _view(run, after=timedelta(minutes=11))
    assert run.store.read(artifact) == _dated_body()
    assert not any(p.capture_id == fresh.run_id for p in view.context.evidence_paths)
    assert not evaluate_publication(view.offer, view.context).publishable_now


@pytest.mark.parametrize(
    "scope", [WithdrawalScope.SOURCE, WithdrawalScope.OFFER, WithdrawalScope.PRODUCT]
)
def test_each_denylist_scope_independently_suppresses(
    tmp_path: Path, scope: WithdrawalScope
) -> None:
    run = reviewed_fixture(tmp_path)
    view = _view(run)
    offer, now = view.offer, view.context.evaluated_at
    assert evaluate_publication(offer, view.context).ranking_eligible
    target = {
        WithdrawalScope.SOURCE: SOURCE_ID,
        WithdrawalScope.OFFER: offer.offer_id,
        WithdrawalScope.PRODUCT: PRODUCT_ID,
    }[scope]
    with open_operational_database(run.database).begin() as conn:
        insert_withdrawal(
            conn,
            WithdrawalEntry(
                schema_version=CONTRACT_VERSION,
                withdrawal_id=f"wd-{scope.value}",
                scope=scope,
                target_id=target,
                actor=REVIEWER,
                withdrawn_at=now,
                reason="Synthetic withdrawal.",
            ),
            denylist=Denylist(tmp_path / "independent" / "withdrawals.json"),
        )
    after = _view(run)
    decision = evaluate_publication(after.offer, after.context)
    assert len(after.context.withdrawals) == 1
    assert decision.outcome is PublicationOutcome.SUPPRESS and R.DENYLISTED in decision.reasons


@pytest.mark.parametrize("change", ["rejection", "profile digest"])
def test_new_source_decision_beats_matching_historical_304(tmp_path: Path, change: str) -> None:
    run = reviewed_fixture(tmp_path)
    view = _view(run)
    assert evaluate_publication(view.offer, view.context).ranking_eligible
    approval = view.context.source_approval
    assert approval is not None
    later = view.context.evaluated_at + timedelta(seconds=1)
    with open_operational_database(run.database).begin() as conn:
        insert_approval(
            conn,
            approval.model_copy(
                update={
                    "approval_id": "athol-style.approval-superseded",
                    "decided_at": later,
                    **(
                        {"decision": ReviewVerdict.REJECTED}
                        if change == "rejection"
                        else {"approved_profile_sha256": "cd" * 32}
                    ),
                }
            ),
            CHARTER,
        )
    earlier = _view(run)
    assert evaluate_publication(earlier.offer, earlier.context).ranking_eligible
    after = _view(run, after=timedelta(minutes=5))
    decision = evaluate_publication(after.offer, after.context)
    assert after.offer.observed_at == run.offers[_THIRTY].observed_at
    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert R.PROFILE_UNAPPROVED in decision.reasons


def test_offer_rejection_supersedes_historical_review(tmp_path: Path) -> None:
    run = reviewed_fixture(tmp_path)
    view = _view(run)
    assert view.current_review is not None
    with open_operational_database(run.database).begin() as conn:
        insert_review(
            conn,
            view.current_review.model_copy(
                update={
                    "review_id": "athol-style.rejected",
                    "verdict": ReviewVerdict.REJECTED,
                    "decided_at": view.context.evaluated_at,
                }
            ),
            CHARTER,
        )
    after = _view(run)
    assert after.current_review is not None and not after.current_review.approved
    assert after.context.prior is not None and after.context.prior.review is None
    assert R.OFFER_QUARANTINED in evaluate_publication(after.offer, after.context).reasons


def test_pending_correction_and_its_evidence_load_before_publication(tmp_path: Path) -> None:
    run = reviewed_fixture(tmp_path)
    view = _view(run)
    assert view.offer.note_rate is not None
    evidence = view.offer.note_rate.provenance
    with open_operational_database(run.database).begin() as conn:
        conn.execute(
            corrections.insert(),
            {
                "correction_id": "pending-correction",
                "schema_version": CONTRACT_VERSION,
                "offer_id": view.offer.offer_id,
                "field_path": "note_rate",
                "previous_value": "6.375",
                "corrected_value": "6.500",
                "actor": REVIEWER,
                "corrected_at": view.context.evaluated_at,
                "reason": "Synthetic dispute",
            },
        )
        conn.execute(
            correction_evidence.insert(),
            {
                "correction_id": "pending-correction",
                "provenance_id": stored_provenance(evidence),
            },
        )
    after = _view(run)
    assert after.context.corrections[0].evidence == (evidence,)
    verdict = evaluate_publication(after.offer, after.context)
    assert verdict.outcome is PublicationOutcome.REQUIRES_REVIEW
    assert R.CORRECTION_PENDING in verdict.reasons


@pytest.mark.parametrize("kind", ["weak_etag", "last_modified"])
def test_matching_non_strong_304_chain_does_not_advance_observation(
    tmp_path: Path, kind: str
) -> None:
    run = reviewed_fixture(tmp_path)
    validator = (
        ConditionalValidator(kind="etag", value='W/"weak-but-exact"')
        if kind == "weak_etag"
        else ConditionalValidator(kind="last_modified", value="Mon, 16 Nov 2026 16:00:00 GMT")
    )
    current, artifact = _later_200(run, _dated_body(), validator)
    assert current.profile_sha256 is not None
    moment = artifact.fetched_at + timedelta(hours=70, minutes=30)
    observation = NotModifiedObservation(
        schema_version=CONTRACT_VERSION,
        run_id=f"matching-{kind}",
        source_id=current.source_id,
        profile_id=current.profile_id,
        profile_sha256=current.profile_sha256,
        git_sha=current.git_sha,
        started_at=moment,
        observed_at=moment,
        original_run_id=current.run_id,
        original_artifact_id=artifact.artifact_id,
        validator=validator,
        response_etag=validator.value,
    )
    with open_operational_database(run.database).begin() as conn:
        insert_reobservation(conn, observation)
    after = _view(run, after=timedelta(hours=73))
    assert after.context.reobservations[-1] == observation
    assert after.offer.observed_at == artifact.fetched_at
    assert R.VALIDITY_EXPIRED in evaluate_publication(after.offer, after.context).reasons


def test_changed_rate_goes_through_gate_against_actual_historical_baseline(tmp_path: Path) -> None:
    run = reviewed_fixture(tmp_path)
    changed = _dated_body().replace(b"6.375%", b"7.000%").replace(b"6.441%", b"7.066%")
    _, artifact = _later_200(run, changed)
    view = _view(run, after=timedelta(hours=1, minutes=1))
    assert view.context.known_artifacts[0] == artifact
    assert view.context.prior is not None
    assert view.context.prior.snapshot.fingerprints == offer_fingerprints(run.offers[_THIRTY])
    assert (
        offer_fingerprints(view.offer).structural
        == view.context.prior.snapshot.fingerprints.structural
    )
    decision = evaluate_publication(view.offer, view.context)
    assert decision.outcome is PublicationOutcome.QUARANTINE
    assert R.RATE_MOVEMENT_EXCEEDS_LIMIT in decision.reasons
    assert R.INSUFFICIENT_EVIDENCE_PATHS in decision.reasons
    with open_operational_database(run.database).begin() as conn:
        assert (
            publish(
                conn,
                view.offer.offer_id,
                store=run.store,
                evaluated_at=view.context.evaluated_at,
                snapshot_id="changed",
            )
            is None
        )
        assert len(conn.execute(select(slice_snapshot_baselines)).all()) == 2


def test_unchanged_200_and_repeated_validations_choose_the_latest_once(tmp_path: Path) -> None:
    run = reviewed_fixture(tmp_path)
    fresh, artifact = _later_200(run, _dated_body())
    moment = artifact.fetched_at + timedelta(minutes=1)
    view = _view(run, after=timedelta(hours=1, minutes=2))
    with open_operational_database(run.database).begin() as conn:
        extra = validate_offer(view.offer, moment)
        insert_validations(conn, extra)
        insert_validations(conn, extra)
        assert len(
            conn.execute(
                select(validation_results).where(
                    validation_results.c.offer_id == view.offer.offer_id,
                    validation_results.c.evaluated_at == moment,
                )
            ).all()
        ) == len(extra)
    after = _view(run, after=timedelta(hours=1, minutes=2))
    assert after.context.known_artifacts[0] == artifact
    assert after.context.source_fingerprints[0].fetch_run_id == fresh.run_id
    assert set(after.context.validations) == set(extra)
    assert {item.rule_id for item in after.context.validations} >= {APR_RULE_ID}
    assert evaluate_publication(after.offer, after.context).ranking_eligible
    expired = _view(run, after=timedelta(hours=98))
    assert R.STALE_WITHDRAWN in evaluate_publication(expired.offer, expired.context).reasons


def test_new_200_uses_its_own_approved_profile_and_not_the_oldest_run(tmp_path: Path) -> None:
    run = reviewed_fixture(tmp_path)
    old = _view(run).context.approved_profile
    assert old is not None
    profile = old.model_copy(update={"profile_id": "synthetic-new-profile", "timeout_seconds": 12})
    _, artifact = _later_200(run, _dated_body(), profile=profile)
    view = _view(run, after=timedelta(hours=1, minutes=1))
    assert view.context.known_artifacts[0] == artifact
    assert view.context.approved_profile == profile
    assert not evaluate_publication(view.offer, view.context).publishable_now
    assert (
        R.EVIDENCE_PATH_NON_AUTHORITATIVE in evaluate_publication(view.offer, view.context).reasons
    )


def test_unsupported_supplemental_assumptions_never_rank_even_if_core_matches(
    tmp_path: Path,
) -> None:
    run = reviewed_fixture(tmp_path)
    body = _dated_body().replace(b"No autopay discount", b"Autopay discount required")
    _later_200(run, body)
    view = _view(run, after=timedelta(hours=1, minutes=1))
    assert view.offer.conditions[0].description == "Autopay discount required"
    assert not any(
        s.fetch_run_id == "synthetic-repeat-200" for s in view.context.verified_scenario_statements
    )
    decision = evaluate_publication(view.offer, view.context)
    # The autopay assumption is a first-class canonical dimension: a source that
    # requires autopay is a scenario conflict, not merely an unstated assumption.
    assert R.SCENARIO_CONFLICT in decision.reasons
    assert R.STRUCTURAL_CHANGE in decision.reasons
    assert not decision.publishable_now
