"""Independent guards for issue #78. All values and captures are fictional fixtures."""

from datetime import timedelta
from decimal import Decimal, localcontext
from typing import Any

import pytest
from pydantic import ValidationError, create_model

from cu_rates.contracts.base import ContractModel
from cu_rates.contracts.publication import (
    EvidencePath,
    PublicationContext,
    PublicationDecision,
    _pair_reasons,
    evaluate_publication,
    publication_evidence_sha256,
)
from cu_rates.contracts.publication import (
    ReasonCode as R,
)

from . import factories as f


@pytest.mark.parametrize("field", ["artifact_id", "content_sha256", "capture_id", "observed_at"])
def test_each_duplicate_identifier_alone_is_rejected(field: str) -> None:
    first, second = f.evidence_path(), f.corroborating_path()
    second = second.model_copy(update={field: getattr(first, field)})
    for other in {"artifact_id", "content_sha256", "capture_id", "observed_at"} - {field}:
        assert getattr(first, other) != getattr(second, other)
    assert _pair_reasons(first, second) == {R.DUPLICATE_EVIDENCE_CAPTURE}
    offer = f.purchase_offer(apr=None, points=None)
    decision = evaluate_publication(offer, f.context(offer, evidence_paths=(first, second)))
    assert R.DUPLICATE_EVIDENCE_CAPTURE in decision.reasons
    assert not decision.publishable_now


@pytest.mark.parametrize(
    "field,value",
    [
        ("artifact_id", "cc" * 32),
        ("content_sha256", "cc" * 32),
        ("capture_id", "not-stored"),
        ("source_id", "not-stored"),
        ("source_class", "web_search"),
        ("observed_at", f.T0 - timedelta(minutes=20)),
        ("provenance", f.provenance(artifact_id="cc" * 32)),
    ],
)
def test_self_declared_html_cannot_replace_provenance(field: str, value: Any) -> None:
    offer = f.purchase_offer(apr=None, points=None)
    first, second = f.evidence_path(), f.corroborating_path()
    second = second.model_copy(update={field: value})
    decision = evaluate_publication(offer, f.context(offer, evidence_paths=(first, second)))
    assert not decision.publishable_now
    assert R.EVIDENCE_PATH_NON_AUTHORITATIVE in decision.reasons


def test_verified_source_kind_controls_authority() -> None:
    offer = f.purchase_offer(apr=None, points=None)
    ctx = f.context(offer).model_copy(update={"source": f.rate_source(evidence_kind="json")})
    assert R.EVIDENCE_PATH_NON_AUTHORITATIVE in evaluate_publication(offer, ctx).reasons


def test_derived_capture_is_not_independent() -> None:
    first, second = f.evidence_path(), f.corroborating_path(derived_from=(f.ARTIFACT_ID,))
    assert _pair_reasons(first, second) == {R.DUPLICATE_EVIDENCE_CAPTURE}


@pytest.mark.parametrize("field", ["scenario_id", "scenario_statements"])
def test_absent_scenario_never_matches_itself(field: str) -> None:
    offer = f.purchase_offer(apr=None, points=None)
    paths: tuple[EvidencePath, ...] = (f.evidence_path(), f.corroborating_path())
    update = {field: None if field == "scenario_id" else ()}
    paths = tuple(p.model_copy(update=update) for p in paths)
    if field == "scenario_id":
        offer = offer.model_copy(update={"scenario_id": None, "scenario_matches": ()})
    decision = evaluate_publication(offer, f.context(offer, evidence_paths=paths))
    assert R.EVIDENCE_PATH_SCENARIO_MISMATCH in decision.reasons
    assert not decision.publishable_now


@pytest.mark.parametrize("boundary", ["future", "stale", "offer_window"])
def test_each_capture_is_bound_to_evaluation_and_offer_time(boundary: str) -> None:
    from cu_rates.contracts.publication import _path_reasons

    offer = f.purchase_offer(apr=None, points=None)
    ctx = f.context(offer)
    p = f.corroborating_path()
    if boundary == "future":
        ctx = ctx.model_copy(update={"evaluated_at": p.observed_at - timedelta(seconds=1)})
    elif boundary == "stale":
        # Pair is only one hour apart but the earlier observation is now >96h old.
        ctx = ctx.model_copy(update={"evaluated_at": f.T0 + timedelta(hours=95, minutes=30)})
    else:
        offer = offer.model_copy(update={"observed_at": p.observed_at - timedelta(seconds=1)})
    assert R.EVIDENCE_PATH_TIME_OUTSIDE_WINDOW in _path_reasons(p, offer, ctx)


@pytest.mark.parametrize(
    "value", [6.125, float("nan"), "NaN", "Infinity", "6.125000000000000000000000000001"]
)
def test_path_values_reject_floats_nonfinite_and_excess_precision(value: object) -> None:
    with pytest.raises(ValidationError):
        EvidencePath.model_validate({**f.evidence_path().model_dump(), "value": value})


def test_exact_comparison_never_rounds_in_decimal_context() -> None:
    offer = f.purchase_offer(apr=None, points=None)
    value = Decimal("6.125000000000000000000000000001")
    # Bypass parsing deliberately: even an unvalidated caller cannot gain equality
    # by Decimal.normalize() rounding this number down in the current context.
    bad = f.corroborating_path().model_copy(
        update={
            "value": value,
            "provenance": f.provenance(f"{value}%", artifact_id=f.ARTIFACT_ID_2),
        }
    )
    with localcontext() as ctx:
        ctx.prec = 28
        result = evaluate_publication(
            offer, f.context(offer, evidence_paths=(f.evidence_path(), bad))
        )
    assert R.EVIDENCE_PATH_VALUE_MISMATCH in result.reasons
    assert not result.publishable_now


def test_review_digest_binds_both_full_capture_chains() -> None:
    offer = f.purchase_offer()
    paths = f.evidence_paths_for(offer)
    expected = publication_evidence_sha256(offer, paths)
    for field, replacement in (
        ("capture_id", "changed"),
        ("content_sha256", "cc" * 32),
        ("observed_at", f.T0 - timedelta(minutes=2)),
        ("provenance", f.provenance("different locator text")),
    ):
        changed = (*paths[:-1], paths[-1].model_copy(update={field: replacement}))
        assert publication_evidence_sha256(offer, changed) != expected


def test_publication_protocol_negotiates_old_readers_and_writers() -> None:
    ctx = f.context()
    assert ctx.publication_version == "2.0"
    # Frozen 1.0 readers forbid unknown keys. New records are intentionally unreadable
    # there; schema/taxonomy 1.0 entity records and fingerprint algorithms are unchanged.
    legacy_fields: dict[str, Any] = {
        name: (field.annotation, field.default)
        for name, field in PublicationContext.model_fields.items()
        if name not in {"publication_version", "evidence_paths"}
    }
    LegacyContext = create_model("LegacyContext", __base__=ContractModel, **legacy_fields)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        LegacyContext.model_validate(ctx.model_dump())
    old_writer = ctx.model_dump(exclude={"publication_version", "evidence_paths"})
    upgraded = PublicationContext.model_validate(old_writer)
    assert (
        R.INSUFFICIENT_EVIDENCE_PATHS in evaluate_publication(f.purchase_offer(), upgraded).reasons
    )
    LegacyDecision = create_model(
        "LegacyDecision",
        __base__=ContractModel,
        outcome=(str, ...),
        placement=(str, ...),
        reasons=(tuple[str, ...], ...),
    )
    decision = evaluate_publication(f.purchase_offer(), upgraded)
    assert decision.publication_version == "2.0"
    with pytest.raises(ValidationError, match="extra_forbidden"):
        LegacyDecision.model_validate(decision.model_dump())
    with pytest.raises(ValidationError):
        PublicationContext.model_validate({**ctx.model_dump(), "publication_version": "1.0"})
    with pytest.raises(ValidationError):
        PublicationDecision.model_validate({**decision.model_dump(), "publication_version": "1.0"})


def test_baseline_paths_resolve_two_fixture_artifacts_and_runs() -> None:
    ctx = f.context()
    artifacts = {a.artifact_id: a for a in ctx.known_artifacts}
    runs = {r.run_id: r for r in ctx.fetch_runs}
    assert len(artifacts) == len(runs) == 2
    for path in ctx.evidence_paths:
        artifact = artifacts[path.artifact_id]
        run = runs[path.capture_id]
        assert artifact.fetch_run_id == run.run_id
        assert artifact.artifact_id in run.artifact_ids
        assert path.content_sha256 == artifact.artifact_id
        assert path.observed_at == artifact.fetched_at
        assert path.provenance.artifact_id == artifact.artifact_id
    assert evaluate_publication(f.purchase_offer(), ctx).ranking_eligible
