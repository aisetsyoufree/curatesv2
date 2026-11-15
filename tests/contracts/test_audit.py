from datetime import timedelta

import pytest
from pydantic import ValidationError

from cu_rates.contracts import (
    Correction,
    PublishedSnapshot,
    ReviewDecision,
    ReviewSubject,
    ReviewVerdict,
    SnapshotEntry,
    WithdrawalEntry,
    WithdrawalScope,
    offer_fingerprints,
    snapshot_manifest_sha256,
)

from .factories import SV, T0, product_review, provenance, purchase_offer, review, withdrawal

HEX_A, HEX_B = "a" * 64, "b" * 64


def _assign(obj: object, name: str, value: object) -> None:
    setattr(obj, name, value)


def test_audit_records_are_immutable_and_utc() -> None:
    decision = review(purchase_offer())
    with pytest.raises(ValidationError):
        _assign(decision, "verdict", ReviewVerdict.REJECTED)
    naive = decision.model_dump() | {"decided_at": T0.replace(tzinfo=None)}
    with pytest.raises(ValidationError):
        ReviewDecision.model_validate(naive)
    offset = decision.model_dump(mode="json") | {"decided_at": "2026-11-14T07:00:00-05:00"}
    assert ReviewDecision.model_validate(offset).decided_at.utcoffset() == timedelta(0)


def test_review_needs_identifiers_actor_reason_and_extra_fields_are_forbidden() -> None:
    payload = review(purchase_offer()).model_dump(mode="json")
    for missing in ("review_id", "actor", "rationale", "decided_at", "subject_id"):
        with pytest.raises(ValidationError):
            ReviewDecision.model_validate({k: v for k, v in payload.items() if k != missing})
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ReviewDecision.model_validate({**payload, "auto_approved": True})
    with pytest.raises(ValidationError):
        ReviewDecision.model_validate({**payload, "actor": "  "})


def test_offer_review_is_bound_to_content_fingerprints() -> None:
    offer = purchase_offer()
    decision = review(offer)
    assert decision.reviewed_fingerprints == offer_fingerprints(offer)
    assert decision.approved
    payload = decision.model_dump(mode="json") | {"reviewed_fingerprints": None}
    with pytest.raises(ValidationError, match="must record the fingerprints"):
        ReviewDecision.model_validate(payload)
    assert decision.reviewed_fingerprints is not None
    with pytest.raises(ValidationError, match="only offer reviews"):
        ReviewDecision.model_validate(
            product_review().model_dump(mode="json")
            | {"reviewed_fingerprints": decision.reviewed_fingerprints.model_dump()}
        )
    assert product_review().subject is ReviewSubject.PRODUCT


def test_review_can_carry_evidence() -> None:
    with_evidence = review(purchase_offer()).model_copy(update={"evidence": (provenance(),)})
    assert ReviewDecision.model_validate_json(with_evidence.model_dump_json()) == with_evidence


def test_withdrawal_entry_scope_target_actor_and_lift_rules() -> None:
    entry = withdrawal(WithdrawalScope.PRODUCT, "prod-30yr-fixed")
    assert (entry.scope, entry.target_id, entry.actor) == (
        WithdrawalScope.PRODUCT,
        "prod-30yr-fixed",
        "Sid",
    )
    assert entry.in_force_at(T0)
    assert not entry.in_force_at(T0 - timedelta(seconds=1))
    with pytest.raises(ValidationError, match="set together"):
        withdrawal(WithdrawalScope.OFFER, "o", lifted_at=T0 + timedelta(days=1))
    with pytest.raises(ValidationError, match="lifted after"):
        withdrawal(WithdrawalScope.OFFER, "o", lifted_at=T0, lifted_by="Sid")
    with pytest.raises(ValidationError):
        WithdrawalEntry.model_validate(entry.model_dump() | {"scope": "everything"})
    with pytest.raises(ValidationError):
        withdrawal(WithdrawalScope.OFFER, "Not An Identifier!")


def test_correction_retains_previous_and_corrected_values() -> None:
    correction = Correction(
        schema_version=SV,
        correction_id="corr-1",
        offer_id="offer-001",
        field_path="note_rate",
        previous_value="6.125",
        corrected_value="6.250",
        actor="Sid",
        corrected_at=T0,
        reason="Typo in the source table.",
        evidence=(provenance(),),
    )
    assert correction.previous_value != correction.corrected_value
    with pytest.raises(ValidationError):
        _assign(correction, "reason", "changed")
    with pytest.raises(ValidationError):
        Correction.model_validate(correction.model_dump() | {"field_path": "Bad Path"})
    with pytest.raises(ValidationError):
        Correction.model_validate(correction.model_dump() | {"reason": ""})


def _snapshot(**overrides: object) -> PublishedSnapshot:
    entries = (
        SnapshotEntry(offer_id="offer-b", structural_sha256=HEX_A, value_sha256=HEX_B),
        SnapshotEntry(offer_id="offer-a", structural_sha256=HEX_B, value_sha256=HEX_A),
    )
    ordered = tuple(sorted(entries, key=lambda e: e.offer_id))
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "snapshot_id": "snap-2",
        "published_at": T0,
        "published_by": "Sid",
        "entries": entries,
        "manifest_sha256": snapshot_manifest_sha256(ordered),
        "supersedes": "snap-1",
        "review_ids": ("review-001",),
    }
    payload.update(overrides)
    return PublishedSnapshot.model_validate(payload)


def test_published_snapshot_is_sorted_manifest_checked_and_immutable() -> None:
    snapshot = _snapshot()
    assert [e.offer_id for e in snapshot.entries] == ["offer-a", "offer-b"]
    with pytest.raises(ValidationError):
        _assign(snapshot, "published_by", "x")
    with pytest.raises(ValidationError, match="manifest_sha256 does not match"):
        _snapshot(manifest_sha256=HEX_A)


def test_published_snapshot_rejects_empty_duplicate_and_self_supersession() -> None:
    entry = SnapshotEntry(offer_id="offer-a", structural_sha256=HEX_A, value_sha256=HEX_B)
    with pytest.raises(ValidationError):
        _snapshot(entries=())
    with pytest.raises(ValidationError, match="duplicate offer"):
        _snapshot(entries=(entry, entry), manifest_sha256=snapshot_manifest_sha256((entry, entry)))
    with pytest.raises(ValidationError, match="cannot supersede itself"):
        _snapshot(supersedes="snap-2")
