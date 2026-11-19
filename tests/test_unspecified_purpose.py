"""T040c: unstated mortgage purpose is an explicit browse-only contract boundary."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from cu_rates.adapters.registry import normalize_row
from cu_rates.adapters.static_html import Field, SourceConfiguration, extract
from cu_rates.contracts import (
    CONTRACT_VERSION,
    CanonicalScenarioId,
    Placement,
    ProductFamily,
    ReasonCode,
)
from cu_rates.contracts.evidence import RawArtifact
from cu_rates.contracts.offers import (
    NormalizedOffer,
    RefinanceMortgageDetails,
    UnspecifiedMortgageDetails,
)
from cu_rates.contracts.publication import evaluate_publication
from cu_rates.contracts.sources import RateSource
from cu_rates.pipeline.extract import layout_sha256
from tests.contracts.factories import context, purchase_offer
from tests.test_static_html_adapter import GIT, inputs


def unspecified_offer() -> NormalizedOffer:
    return purchase_offer(
        schema_version="1.1",
        details={
            "kind": "unspecified_mortgage",
            "term_months": 360,
            "rate_type": "fixed",
            "lock_days": 30,
        },
        scenario_id=None,
        scenario_matches=(),
    )


def test_contract_accepts_and_roundtrips_unspecified_without_guessing() -> None:
    offer = unspecified_offer()
    assert CONTRACT_VERSION == "1.1"
    assert offer.family is ProductFamily.MORTGAGE and offer.purpose == "unspecified"
    assert NormalizedOffer.model_validate_json(offer.model_dump_json()) == offer
    with pytest.raises(ValidationError):
        RefinanceMortgageDetails.model_validate(
            dict(term_months=360, rate_type="fixed", purpose="unspecified")
        )
    with pytest.raises(ValidationError):
        NormalizedOffer.model_validate({**offer.model_dump(), "schema_version": "1.0"})


@pytest.mark.parametrize("scenario", list(CanonicalScenarioId))
def test_unspecified_cannot_claim_any_canonical_scenario(scenario: str) -> None:
    offer = unspecified_offer()
    with pytest.raises(ValidationError, match="family/purpose"):
        NormalizedOffer.model_validate({**offer.model_dump(), "scenario_id": scenario})


def test_gate_publishes_only_browse_and_preserves_corroboration() -> None:
    offer = unspecified_offer()
    ctx = context(offer)
    decision = evaluate_publication(offer, ctx)
    assert decision.publishable_now, decision.reasons
    assert decision.placement is Placement.BROWSE_ONLY
    assert ReasonCode.SCENARIO_UNSTATED in decision.reasons
    assert not decision.ranking_eligible
    broken = ctx.model_copy(update={"evidence_paths": ctx.evidence_paths[:1]})
    assert not evaluate_publication(offer, broken).publishable_now
    # A forged canonical ID must not evade the placement guard through model_copy.
    forged = offer.model_copy(
        update={
            "scenario_id": purchase_offer().scenario_id,
            "scenario_matches": purchase_offer().scenario_matches,
        }
    )
    assert not evaluate_publication(forged, context(forged)).ranking_eligible


def absent_inputs(
    flag: bool = True, extra: bytes = b""
) -> tuple[SourceConfiguration, RateSource, bytes, RawArtifact]:
    config, source, body, _, _ = inputs()
    body = body.replace(b"<td>purchase</td>", b"<td></td>").replace(b"</body>", extra + b"</body>")
    config_data = config.model_dump()
    row = config_data["rows"][0]
    row["comparison_target"] = None
    row["allow_unspecified_purpose"] = flag
    row["fields"] = [
        f
        for f in row["fields"]
        if f["path"] != "details.purpose" and not f["path"].startswith("scenario.")
    ]
    config_data["layout_sha256"] = layout_sha256(body)
    config = SourceConfiguration.model_validate(config_data)
    _, _, _, artifact, _ = inputs(body=body)
    return config, source, body, artifact


def test_adapter_requires_explicit_absence_flag_and_keeps_source_text() -> None:
    config, source, body, artifact = absent_inputs()
    result = extract(config, source, body, artifact, extraction_run_id="run", git_sha=GIT)
    assert result.confidence == "high", result.diagnostics
    offer = normalize_row(result.rows[0], config, artifact)
    assert offer.purpose == "unspecified" and offer.scenario_id is None
    assert all(c.raw_value == c.provenance.source_text for c in result.rows[0].candidates)
    config, source, body, artifact = absent_inputs(False)
    refused = extract(config, source, body, artifact, extraction_run_id="run", git_sha=GIT)
    assert refused.confidence == "low"


@pytest.mark.parametrize(
    "text",
    [
        b"Purchase",
        b"Rate and term refinance",
        b"Cash-out refinancing",
        b"Not available for purchase",
        b"home purchase and refinance",
    ],
)
def test_adapter_refuses_unspecified_if_page_states_any_purpose(text: bytes) -> None:
    config, source, body, artifact = absent_inputs(extra=b"<p>" + text + b"</p>")
    result = extract(config, source, body, artifact, extraction_run_id="run", git_sha=GIT)
    assert result.confidence == "low"
    assert any("purpose" in d for d in result.diagnostics)


def scoped_absent_inputs(
    extra: bytes = b"",
) -> tuple[SourceConfiguration, RateSource, bytes, RawArtifact]:
    real, _, body, _, _ = inputs("real")
    body += extra
    baseline, source, _, artifact, _ = inputs(body=body)
    fields = []
    for field in real.rows[0].fields:
        data = field.model_dump()
        data["selector"] = field.selector.replace("#loan-rates", "#mortgage-rates")
        data["label"]["selector"] = field.label.selector.replace("#loan-rates", "#mortgage-rates")
        if field.group_header:
            data["label"]["text"] = "First mortgage rates"
        if field.path == "details.lien_position":
            data["path"] = "details.rate_type"
        fields.append(Field.model_validate(data))
    row = baseline.rows[0].model_dump()
    row.update(
        comparison_target=None,
        browse_only=True,
        allow_unspecified_purpose=True,
        fields=[f.model_dump() for f in fields],
    )
    config = SourceConfiguration.model_validate(
        baseline.model_dump()
        | dict(sections=("mortgage-rates", "rates-heading", "rates-note"), rows=(row,))
    )
    config = config.model_copy(update={"layout_sha256": config.layout_of(body)})
    return config, source, body, artifact


def test_unspecified_composes_with_sections_group_headers_and_prose_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from cu_rates.adapters import registry
    from cu_rates.adapters.static_html import configuration_parser

    config, source, body, artifact = scoped_absent_inputs()
    result = extract(config, source, body, artifact, extraction_run_id="run", git_sha=GIT)
    assert result.confidence == "medium" and not result.diagnostics
    offer = normalize_row(result.rows[0], config, artifact)
    assert offer.purpose == "unspecified" and offer.scenario_id is None
    assert not offer.scenario_matches
    assert isinstance(offer.details, UnspecifiedMortgageDetails)
    assert offer.points is None and offer.details.lock_days is None
    assert offer.effective_at == datetime(2026, 11, 18, 5, tzinfo=UTC)
    assert all(c.proposed_path != "details.purpose" for c in result.rows[0].candidates)
    assert all(c.raw_value == c.provenance.source_text for c in result.rows[0].candidates)
    (tmp_path / "table.rates.json").write_text(config.model_dump_json())
    monkeypatch.setattr(registry, "CONFIGURATIONS", tmp_path)
    approval = inputs()[4].model_copy(
        update={
            "adapter": configuration_parser(config),
            "approved_layout_sha256": config.layout_sha256,
        }
    )
    verified = registry.verify_offer_against_stored_bytes(source, approval, offer, body, artifact)
    assert verified.confidence == "medium" and verified.comparable


@pytest.mark.parametrize(
    "extra",
    [
        b"<nav>Purchase</nav>",
        b"<footer>Not available for refinancing</footer>",
        b"<p>Mortgage</p><p>Cash-out</p>",
        b"<h3>pur<span>chase</span></h3>",
        b"<p>home&#32;buying</p>",
        b"<p hidden>refinance</p>",
    ],
)
def test_unspecified_scoped_rows_still_scan_the_whole_page(extra: bytes) -> None:
    config, source, body, artifact = scoped_absent_inputs(extra)
    assert config.layout_sha256 == scoped_absent_inputs()[0].layout_sha256
    result = extract(config, source, body, artifact, extraction_run_id="run", git_sha=GIT)
    assert result.confidence == "low"
    assert any("loan purpose is stated or ambiguous" in d for d in result.diagnostics)


@pytest.mark.parametrize("extra", [b"<script>purchase</script>", b"<template>refinance</template>"])
def test_inert_markup_is_not_a_purpose_statement(extra: bytes) -> None:
    config, source, body, artifact = scoped_absent_inputs(extra)
    result = extract(config, source, body, artifact, extraction_run_id="run", git_sha=GIT)
    assert result.confidence == "medium", result.diagnostics


@pytest.mark.parametrize("extra", [b"<template>purchase", b"</template>", b"<template/>purchase"])
def test_uncertain_inert_markup_cannot_hide_a_page_purpose(extra: bytes) -> None:
    config, source, body, artifact = scoped_absent_inputs(extra)
    result = extract(config, source, body, artifact, extraction_run_id="run", git_sha=GIT)
    assert result.confidence == "low"
    assert any("cannot verify absent loan purpose" in d for d in result.diagnostics)


@pytest.mark.parametrize("mutation", ["rate_type", "footnote", "hidden", "date", "row"])
def test_unspecified_retains_v2_field_guards(mutation: str) -> None:
    config, source, body, _ = scoped_absent_inputs()
    before, after = {
        "rate_type": (b">Fixed</td>", b">Not a rate type</td>"),
        "footnote": (b">6.111%</td>", b">6.111<sup>5</sup>%</td>"),
        "hidden": (b">6.111%</td>", b"><span hidden>6.111%</span></td>"),
        "date": (b"November 18, 2026.", b"November 18, 2026. Not current."),
        "row": (b">30 Year<sup>4</sup>", b">30 Year<sup>4</sup>"),
    }[mutation]
    body = body.replace(before, after)
    if mutation == "row":
        row = config.rows[0]
        fields = tuple(
            f.model_copy(
                update={
                    "label": row.fields[2].label.model_copy(
                        update={
                            "selector": "#mortgage-rates tbody tr:nth-child(1) > td:nth-child(1)",
                            "text": "First mortgage",
                        }
                    )
                }
            )
            if f.path == "note_rate"
            else f
            for f in row.fields
        )
        config = config.model_copy(update={"rows": (row.model_copy(update={"fields": fields}),)})
    config = config.model_copy(update={"layout_sha256": config.layout_of(body)})
    artifact = inputs(body=body)[3]
    result = extract(config, source, body, artifact, extraction_run_id="run", git_sha=GIT)
    assert result.confidence == "low" and result.diagnostics


def test_unspecified_browse_row_cannot_declare_scenario_fields() -> None:
    config, _, _, _ = scoped_absent_inputs()
    data = config.model_dump()
    field = config.rows[0].fields[2].model_dump() | dict(path="scenario.credit_score")
    data["rows"][0]["fields"] += (field,)
    with pytest.raises(ValidationError, match="browse-only"):
        SourceConfiguration.model_validate(data)


def test_parser_identity_advances_v2_and_binds_the_new_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import json

    from cu_rates.adapters import registry
    from cu_rates.adapters.static_html import PARSER, configuration_parser

    config, source, _, _, approval = inputs()
    assert PARSER.version == "1.2.0"
    # Golden v2 digest excluded this new reviewed row option. Pin both intentionally:
    # even its default false participates in canonical serialization and reapproval.
    legacy = config.model_dump(mode="json")
    for row in legacy["rows"]:
        row.pop("allow_unspecified_purpose")
    legacy_digest = hashlib.sha256(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert legacy_digest == "d95a4e5d45ae2e564d30716a80c3937fdcd21bcea389cb50ebe95741f7f82981"
    assert config.sha256 == "6e298d33552064b62c5e31001d977a586325bc0caa506b9ec4f91c81845c14bf"
    (tmp_path / "table.rates.json").write_text(config.model_dump_json())
    monkeypatch.setattr(registry, "CONFIGURATIONS", tmp_path)
    assert registry.approved_adapter(source, approval).parser == configuration_parser(config)
    for version in (f"1.1.0+cfg.{legacy_digest}", f"1.1.0+cfg.{config.sha256}"):
        stale = approval.model_copy(
            update={"adapter": PARSER.model_copy(update={"version": version})}
        )
        with pytest.raises(ValueError, match="unknown adapter"):
            registry.approved_adapter(source, stale)
    absent, _, _, _ = absent_inputs()
    flag_off = absent.model_copy(
        update={"rows": (absent.rows[0].model_copy(update={"allow_unspecified_purpose": False}),)}
    )
    assert configuration_parser(absent) != configuration_parser(flag_off)
    (tmp_path / "table.rates.json").write_text(absent.model_dump_json())
    stale = approval.model_copy(update={"adapter": configuration_parser(flag_off)})
    with pytest.raises(ValueError, match="digest is not approved"):
        registry.approved_adapter(source, stale)


@pytest.mark.parametrize("scoped", [False, True])
def test_stored_page_to_snapshot_and_web_is_browse_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scoped: bool
) -> None:
    from datetime import timedelta

    from cu_rates.adapters import registry
    from cu_rates.adapters.static_html import configuration_parser
    from cu_rates.artifact_store import ArtifactStore
    from cu_rates.contracts import base
    from cu_rates.db import open_operational_database, upgrade_database
    from cu_rates.pipeline.trusted import load_publication_context, publish
    from cu_rates.snapshot import Denylist, SnapshotError, SnapshotReader
    from cu_rates.snapshot.builder import SnapshotPublisher
    from cu_rates.web import create_app
    from tests import test_static_html_adapter as adapter_tests

    config, source, body, artifact = scoped_absent_inputs() if scoped else absent_inputs()
    _, _, _, _, approval = inputs()
    approval = approval.model_copy(
        update={
            "adapter": configuration_parser(config),
            "approved_layout_sha256": config.layout_sha256,
        }
    )
    configs = tmp_path / "configurations"
    configs.mkdir()
    (configs / "table.rates.json").write_text(config.model_dump_json())
    monkeypatch.setattr(registry, "CONFIGURATIONS", configs)
    monkeypatch.setattr(
        adapter_tests, "inputs", lambda name="table": (config, source, body, artifact, approval)
    )
    database = tmp_path / "worker.sqlite"
    upgrade_database(database)
    store = ArtifactStore(tmp_path / "evidence")
    engine = open_operational_database(database)
    root = tmp_path / "snapshot"
    now = adapter_tests.NOW
    try:
        with engine.begin() as conn:
            source, profile = adapter_tests.prepare(conn, store)
            adapter_tests.capture(
                conn,
                store,
                source,
                profile,
                now - timedelta(hours=4),
                "first",
                body + b"\n",
                expected_confidence="medium" if scoped else "high",
            )
            adapter_tests.capture(
                conn,
                store,
                source,
                profile,
                now,
                "second",
                body,
                expected_confidence="medium" if scoped else "high",
            )
            loaded = load_publication_context(conn, "table.purchase", now, store=store)
            decision = evaluate_publication(loaded.offer, loaded.context)
            assert decision.publishable_now and decision.placement is Placement.BROWSE_ONLY, (
                decision.reasons
            )
            manifest = publish(
                conn, loaded.offer.offer_id, store=store, evaluated_at=now, snapshot_id="unstated"
            )
            assert manifest is not None and manifest.schema_version == "1.1"
            publisher = SnapshotPublisher(root, allow_fixture=True)
            from cu_rates.snapshot.builder import SnapshotCandidate, _admit

            with pytest.raises(SnapshotError, match=r"requires snapshot schema 1\.1"):
                _admit(
                    SnapshotCandidate(offer=loaded.offer, context=loaded.context, candidates=()),
                    manifest.model_copy(update={"schema_version": "1.0"}),
                )
            publisher.build(manifest, conn=conn, store=store)
        denylist = Denylist(tmp_path / "withdrawals.json")
        denylist.initialize()
        reader = SnapshotReader(root, tmp_path / "withdrawals.json")
        view = reader.read(at=now)
        assert len(view.offers) == 1
        public = view.offers[0]
        assert public.purpose == "unspecified" and public.placement is Placement.BROWSE_ONLY
        assert public.scenario_id is None and not public.assumptions
        # Two captures for each published value. The scoped source states no points,
        # so it has four paths (rate/APR) rather than six (rate/APR/points).
        evidence = public.require_protocol2().publication_evidence
        assert len(evidence) == (4 if scoped else 6)
        assert {p.path for p in evidence} == (
            {"note_rate", "apr"} if scoped else {"note_rate", "apr", "points"}
        )
        assert {p.capture_id for p in evidence} == {"first", "second"}
        with pytest.raises(ValueError, match="browse/detail"):
            public.model_copy(update={"placement": Placement.RANKED}).require_protocol2()
        # Same upper-minor validator used by a 1.0 reader, exercised at the sealed DB boundary.
        with monkeypatch.context() as legacy:
            legacy.setattr(base, "CONTRACT_MINOR", 0)
            with pytest.raises(SnapshotError):
                reader.read(at=now)
        assert reader.read(at=now).offers == view.offers
        app = create_app()
        app.config.update(
            TESTING=True,
            SNAPSHOT_ROOT=root,
            DENYLIST_PATH=tmp_path / "withdrawals.json",
            SNAPSHOT_CLOCK=now,
        )
        client = app.test_client()
        for query in (
            "",
            "?scenario=purchase_30y_fixed&sort=apr",
            "?scenario=rate_term_refi_30y_fixed",
        ):
            response = client.get("/compare" + query)
            assert response.status_code == 200
            assert b"Synthetic Cedar CU" not in response.data
        detail = client.get("/credit-unions/8000401")
        assert detail.status_code == 200
        assert b"loan purpose not stated by the source" in detail.data
        # Re-seal a falsely labeled 1.0 snapshot: even an internally consistent file
        # digest must not permit the new shape under an old contract declaration.
        import json
        import sqlite3
        from contextlib import closing

        from cu_rates.snapshot._database import database_digest
        from cu_rates.snapshot._models import Version

        version_dir = root / "versions" / "unstated"
        public_db = version_dir / "published.sqlite"
        public_db.chmod(0o600)
        with closing(sqlite3.connect(public_db)) as raw, raw:
            payload = json.loads(raw.execute("SELECT payload FROM snapshot_info").fetchone()[0])
            payload["schema_version"] = "1.0"
            raw.execute("UPDATE snapshot_info SET payload = ?", (json.dumps(payload),))
        public_db.chmod(0o444)
        replacement = Version(
            version=1, snapshot_id="unstated", database_sha256=database_digest(public_db)
        )
        (root / "current.json").write_text(replacement.model_dump_json())
        (version_dir / "version.json").chmod(0o644)
        (version_dir / "version.json").write_text(replacement.model_dump_json())
        with pytest.raises(SnapshotError, match=r"requires snapshot schema 1\.1"):
            reader.read(at=now)
    finally:
        engine.dispose()


def test_public_model_refuses_forged_ranked_unspecified_even_with_canonical_proofs() -> None:
    from cu_rates.snapshot import PublicOffer
    from cu_rates.snapshot.builder import SnapshotCandidate, _public_projection

    ranked = purchase_offer()
    public = _public_projection(
        SnapshotCandidate(offer=ranked, context=context(ranked), candidates=()),
        Placement.RANKED,
        False,
    )
    payload = public.model_dump()
    payload.update(
        purpose="unspecified", details={**public.details, "kind": "unspecified_mortgage"}
    )
    with pytest.raises(ValueError, match="browse/detail"):
        PublicOffer.model_validate(payload)
    # Also enforce the rule at read/rollback admission if validation was bypassed.
    forged = public.model_copy(
        update={"purpose": unspecified_offer().purpose, "details": payload["details"]}
    )
    with pytest.raises(ValueError, match="browse/detail"):
        forged.require_protocol2()


def test_gate_without_a_scenario_still_rejects_incomplete_arm() -> None:
    offer = purchase_offer(
        schema_version="1.1",
        details={
            "kind": "unspecified_mortgage",
            "term_months": 360,
            "rate_type": "adjustable",
            "arm": {},
        },
        scenario_id=None,
        scenario_matches=(),
    )
    decision = evaluate_publication(offer, context(offer))
    assert not decision.publishable_now
    assert ReasonCode.ARM_TERMS_INCOMPLETE in decision.reasons


def test_normalizer_flag_never_overrides_stated_purpose() -> None:
    from cu_rates.normalize import NormalizationError, OfferContext, normalize_offer

    config, source, body, artifact, _ = inputs()
    row = extract(config, source, body, artifact, extraction_run_id="run", git_sha=GIT).rows[0]
    with pytest.raises(NormalizationError, match="absent source purpose"):
        normalize_offer(
            row.candidates,
            OfferContext(
                offer_id="test",
                product_id="test",
                source_id=source.source_id,
                institution_charter=config.institution_charter,
                observed_at=artifact.fetched_at,
                allow_unspecified_purpose=True,
            ),
        )


def test_no_scenario_exception_is_specific_and_cannot_borrow_canonical_statements() -> None:
    ordinary = purchase_offer(scenario_id=None, scenario_matches=())
    assert not evaluate_publication(ordinary, context(ordinary)).publishable_now
    offer = unspecified_offer()
    ctx = context(offer)
    baseline = context(purchase_offer())
    paths = tuple(
        p.model_copy(update={"scenario_statements": baseline.evidence_paths[0].scenario_statements})
        for p in ctx.evidence_paths
    )
    assert not evaluate_publication(
        offer, ctx.model_copy(update={"evidence_paths": paths})
    ).publishable_now


@pytest.mark.parametrize("field", ["purpose", "scenario"])
def test_config_cannot_combine_absence_flag_with_a_stated_purpose_or_target(field: str) -> None:
    config, _, _, _ = absent_inputs()
    data = config.model_dump()
    if field == "scenario":
        data["rows"][0]["comparison_target"] = "purchase_30y_fixed"
    else:
        data["rows"][0]["fields"] += (inputs()[0].model_dump()["rows"][0]["fields"][1],)
    with pytest.raises(ValidationError, match="no purpose field or comparison target"):
        SourceConfiguration.model_validate(data)


def test_normalizer_never_infers_absence_without_authorization() -> None:
    from cu_rates.normalize import NormalizationError, OfferContext, normalize_offer

    config, source, body, artifact = absent_inputs()
    row = extract(config, source, body, artifact, extraction_run_id="run", git_sha=GIT).rows[0]
    with pytest.raises(NormalizationError, match="require a stated loan purpose"):
        normalize_offer(
            row.candidates,
            OfferContext(
                offer_id="test",
                product_id="test",
                source_id=source.source_id,
                institution_charter=config.institution_charter,
                observed_at=artifact.fetched_at,
            ),
        )


def test_canonical_scenario_definition_requires_a_stated_purpose() -> None:
    from cu_rates.contracts.scenarios import CANONICAL_SCENARIOS, ComparisonScenario

    scenario = CANONICAL_SCENARIOS[CanonicalScenarioId.PURCHASE_30Y_FIXED]
    with pytest.raises(ValidationError, match="purpose"):
        ComparisonScenario.model_validate({**scenario.model_dump(), "purpose": "unspecified"})


def test_public_details_kind_cannot_be_relabelled_as_purchase() -> None:
    from cu_rates.snapshot import PublicOffer
    from cu_rates.snapshot.builder import SnapshotCandidate, _public_projection

    ranked = purchase_offer()
    public = _public_projection(
        SnapshotCandidate(offer=ranked, context=context(ranked), candidates=()),
        Placement.RANKED,
        False,
    )
    payload = public.model_dump()
    payload["details"]["kind"] = "unspecified_mortgage"
    with pytest.raises(ValidationError, match="browse/detail"):
        PublicOffer.model_validate(payload)
