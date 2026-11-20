"""T052: credit-union profile and coverage pages, read only from the verified public snapshot."""

from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import pytest
from flask import Flask
from flask.testing import FlaskClient

from cu_rates.contracts.enums import (
    CanonicalScenarioId,
    ConditionKind,
    MatchState,
    WithdrawalScope,
)
from cu_rates.contracts.offers import OfferCondition
from cu_rates.contracts.publication import (
    DEGRADE_AFTER,
    WITHDRAW_AFTER,
    Placement,
    evaluate_publication,
)
from cu_rates.snapshot import Denylist, SnapshotReader
from cu_rates.snapshot._models import PublicOffer, SnapshotInfo, SnapshotView
from cu_rates.snapshot.builder import _public_projection
from cu_rates.web import create_app, profiles
from cu_rates.web.comparison import (
    DETAIL_ONLY_REASON,
    classify_and_format_offer,
    format_institution_detail_offer,
)
from tests.contracts import factories as make
from tests.test_web import _make_candidate

NOW = make.EVALUATED_AT
HOSTILE = '<script>alert("x")</script> & "Co" <b>'


def _offer(
    offer_id: str,
    name: str,
    charter: int,
    *,
    apr: str | None = "6.250",
    note_rate: str | None = "6.125",
    conditions: tuple[OfferCondition, ...] = (),
    placement: Placement | None = None,
) -> PublicOffer:
    item = _make_candidate(
        offer_id,
        "Placeholder CU",
        note_rate_str=note_rate,
        apr_str=apr,
        conditions=conditions,
        now=NOW,
    )
    decided = evaluate_publication(item.offer, item.context).placement
    projected = _public_projection(item, placement or decided, False)
    return projected.model_copy(update={"institution_name": name, "institution_charter": charter})


def _app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    offers: list[PublicOffer],
    *,
    now: datetime = NOW,
) -> tuple[Flask, Denylist]:
    """Mock the sealed reader in memory (as test_web does); the app code is unchanged."""
    root = tmp_path / "snap"
    root.mkdir()
    denylist = Denylist(tmp_path / "independent" / "withdrawals.json")
    denylist.initialize()
    info = SnapshotInfo(
        publication_version="2.0",
        schema_version=make.SV,
        snapshot_id="profiles-fixture",
        published_at=NOW,
        published_by="mock-reader",
        manifest_sha256="0" * 64,
        supersedes=None,
    )

    def read(self: SnapshotReader, *, at: datetime | None = None) -> SnapshotView:
        moment = now if at is None else at
        active = denylist.active_targets(moment)
        visible = tuple(
            o.model_copy(update={"placement": Placement.BROWSE_ONLY})
            if moment - o.observed_at > DEGRADE_AFTER and o.placement is Placement.RANKED
            else o
            for o in offers
            if moment < o.valid_until
            and moment - o.observed_at <= WITHDRAW_AFTER
            and ("offer", o.offer_id) not in active
        )
        return SnapshotView(info=info, offers=visible)

    monkeypatch.setattr(SnapshotReader, "read", read)
    app = create_app(
        {
            "TESTING": True,
            "SNAPSHOT_ROOT": str(root),
            "DENYLIST_PATH": str(denylist.path),
            "AUTOMATIC_DENYLIST_PATH": str(denylist.path),
            "SNAPSHOT_CLOCK": now,
        }
    )
    return app, denylist


def _noindex(client: FlaskClient, path: str) -> bool:
    resp = client.get(path)
    return "noindex" in resp.headers.get("X-Robots-Tag", "") and (
        '<meta name="robots" content="noindex, follow">' in resp.get_data(as_text=True)
    )


def _sitemap(client: FlaskClient) -> list[str]:
    ns = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ElementTree.fromstring(client.get("/sitemap.xml").get_data())
    return [e.text or "" for e in root.findall(".//ns:loc", ns)]


def test_no_snapshot_means_no_profiles(client_without_snapshot: FlaskClient) -> None:
    client = client_without_snapshot
    assert client.get("/credit-unions/12345").status_code == 404
    listing = client.get("/credit-unions")
    assert listing.status_code == 200
    assert _noindex(client, "/credit-unions")
    assert "No Verified Credit Union Profiles Yet" in listing.get_data(as_text=True)
    assert not any("/credit-unions" in u for u in _sitemap(client))


@pytest.fixture
def client_without_snapshot() -> FlaskClient:
    return create_app({"TESTING": True}).test_client()


def test_profile_with_verified_offer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = _app(tmp_path, monkeypatch, [_offer("o-1", "Alpha Credit Union", 111)])
    client = app.test_client()
    resp = client.get("/credit-unions/111")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "X-Robots-Tag" not in resp.headers
    assert 'name="robots"' not in html
    assert "Alpha Credit Union" in html
    assert "<dd>111</dd>" in html
    assert '<link rel="canonical" href="https://cu-rates.org/credit-unions/111">' in html
    # coverage: mortgage verified, the other families unknown (never "no product")
    assert html.count("<td>Verified</td>") == 1
    assert html.count("<td>Unknown</td>") == 2
    assert "No product" not in html
    assert "6.250%" in html  # APR from the verified offer, with its observation time
    assert "Nov 15, 2026" in html and "Observed" in html
    assert 'href="/corrections"' in html
    assert 'href="/methodology"' in html
    assert "Nothing unverified is presented as current fact" in html
    assert "Not available in the verified public snapshot" in html  # city
    assert "https://cu-rates.org/credit-unions/111" in _sitemap(client)
    assert "https://cu-rates.org/credit-unions" in _sitemap(client)
    listing = client.get("/credit-unions").get_data(as_text=True)
    assert 'href="/credit-unions/111"' in listing


def _condition(condition_id: str, kind: ConditionKind, text: str) -> OfferCondition:
    """Synthetic condition (no real institution text); it conflicts with the scenario."""
    return OfferCondition(
        condition_id=condition_id,
        kind=kind,
        description=text,
        provenance=make.provenance(text),
        scenario_state=MatchState.CONFLICTS,
    )


def _profile_items(html: str) -> list[str]:
    coverage = html.split("Coverage by product")[1].split("How to read coverage")[0]
    return [item.split("</li>")[0] for item in coverage.split("<li>")[1:]]


def test_profile_shows_each_offers_own_conditions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#103 (a): the profile renders the same validated conditions the comparison shows.

    Synthetic fixture only: two separate public offers for one charter, one with an
    autopay and an advertised as-low-as condition, one with none recorded.
    """
    conditioned = _offer(
        "o-terms",
        "Synthetic Conditions CU",
        888,
        note_rate="5.875",
        apr="5.990",
        conditions=(
            _condition("c-autopay", ConditionKind.AUTOPAY, "synthetic autopay"),
            _condition("c-ala", ConditionKind.AS_LOW_AS, "synthetic as low as"),
        ),
    )
    plain = _offer("o-plain", "Synthetic Conditions CU", 888).model_copy(update={"assumptions": ()})
    app, _ = _app(tmp_path, monkeypatch, [conditioned, plain])
    resp = app.test_client().get("/credit-unions/888")
    assert resp.status_code == 200
    items = _profile_items(resp.get_data(as_text=True))
    assert len(items) == 2  # separate public offers stay separate rows (sorted by offer id)
    plain_item, terms_item = items
    assert "6.125%" in plain_item and "5.875%" not in plain_item
    assert "5.875%" in terms_item and "5.990%" in terms_item

    # The conditioned offer discloses its own conditions, in the comparison's wording.
    assert "Conditions: " in terms_item
    assert "Autopay discount condition" in terms_item
    assert "As-low-as qualification condition" in terms_item
    assert "none recorded" not in terms_item
    # Its separation reason is still shown independently of the conditions disclosure.
    assert "Not comparable:" in terms_item

    # Empty conditions are unknown, never "no conditions": no leak from the other offer.
    assert "Autopay" not in plain_item and "As-low-as" not in plain_item
    assert (
        "Conditions: none recorded in the verified snapshot; this does not mean none apply."
        in plain_item
    )
    for overclaim in ("None published", "No conditions", "no conditions apply"):
        assert overclaim not in plain_item
    # Effective date and review status are deliberately not rendered here (deferred to #160).
    for deferred in ("Reviewed by human", "Automated check", "Effective"):
        assert deferred not in terms_item and deferred not in plain_item


def test_profile_without_verified_offers_is_noindex_and_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A detail-only placement that nonetheless carries an APR contradicts the gate (D-05 only
    # routes missing-APR offers there), so neither projection shows it: the charter is known
    # but nothing is displayed. (A genuine note-rate-only offer is covered by the D-05 tests.)
    offer = _offer("o-2", "Beta CU", 222, placement=Placement.INSTITUTION_DETAIL_ONLY)
    app, _ = _app(tmp_path, monkeypatch, [offer])
    client = app.test_client()
    resp = client.get("/credit-unions/222")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert _noindex(client, "/credit-unions/222")
    assert html.count("<td>Unknown</td>") == 3
    assert "<td>Verified</td>" not in html
    assert "%" not in html.split("Coverage by product")[1].split("How to read coverage")[0]
    assert "no fresh verified offers" in html
    assert _noindex(client, "/credit-unions")
    urls = _sitemap(client)
    assert not any("/credit-unions" in u for u in urls)


def test_stale_state_when_only_old_offers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    late = make.T0 + DEGRADE_AFTER + timedelta(hours=2)
    offer = _offer("o-3", "Gamma CU", 333)
    offer = offer.model_copy(update={"valid_until": make.T0 + timedelta(days=10)})
    app, _ = _app(tmp_path, monkeypatch, [offer], now=late)
    client = app.test_client()
    html = client.get("/credit-unions/333").get_data(as_text=True)
    assert "<td>Stale</td>" in html
    assert "<td>Verified</td>" not in html
    assert _noindex(client, "/credit-unions/333")  # stale is not verified content
    assert not any("/credit-unions" in u for u in _sitemap(client))


def test_withdrawn_offer_disappears(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    offers = [_offer("o-keep", "Delta CU", 444), _offer("o-gone", "Echo CU", 555)]
    app, denylist = _app(tmp_path, monkeypatch, offers)
    client = app.test_client()
    assert client.get("/credit-unions/555").status_code == 200
    denylist.withdraw(make.withdrawal(WithdrawalScope.OFFER, "o-gone", withdrawn_at=make.T0))
    assert client.get("/credit-unions/555").status_code == 404
    assert "Echo CU" not in client.get("/credit-unions").get_data(as_text=True)
    assert "https://cu-rates.org/credit-unions/555" not in _sitemap(client)
    assert client.get("/credit-unions/444").status_code == 200


def test_unknown_or_malformed_charter_404(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = _app(tmp_path, monkeypatch, [_offer("o-1", "Alpha CU", 111)])
    client = app.test_client()
    for path in (
        "/credit-unions/999",
        "/credit-unions/abc",
        "/credit-unions/-1",
        "/credit-unions/111/x",
    ):
        resp = client.get(path)
        assert resp.status_code == 404
        assert "noindex" in resp.headers["X-Robots-Tag"]


def test_names_are_escaped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = _app(tmp_path, monkeypatch, [_offer("o-x", HOSTILE, 666)])
    client = app.test_client()
    for path in ("/credit-unions/666", "/credit-unions"):
        html = client.get(path).get_data(as_text=True)
        assert HOSTILE not in html
        assert "<script>alert" not in html
        assert "&lt;script&gt;alert(" in html
    # JSON-LD is a script block: the name must not be able to close it
    profile = client.get("/credit-unions/666").get_data(as_text=True)
    ld = profile.split('<script type="application/ld+json">')[1].split("</script>")[0]
    assert "</" not in ld and "<script" not in ld


def test_conflicting_names_for_one_charter_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    offers = [_offer("o-a", "Name One", 777), _offer("o-b", "Name Two", 777)]
    app, _ = _app(tmp_path, monkeypatch, offers)
    assert app.test_client().get("/credit-unions/777").status_code == 404


def test_no_path_or_query_leakage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = _app(tmp_path, monkeypatch, [_offer("o-1", "Alpha CU", 111)])
    client = app.test_client()
    html = client.get("/credit-unions/111?utm=SECRETQ&next=//evil.example").get_data(as_text=True)
    assert "SECRETQ" not in html and "evil.example" not in html
    assert '<link rel="canonical" href="https://cu-rates.org/credit-unions/111">' in html
    for local in (str(tmp_path), "withdrawals.json", "/snap", "data/snapshots", "profiles-fixture"):
        assert local not in html
    for page in ("/credit-unions", "/credit-unions/111"):
        body = client.get(page).get_data(as_text=True)
        assert str(tmp_path) not in body and "profiles-fixture" not in body


# --- #133 A9 / D-05: a verified note rate without APR is institution-detail-only ----------
# Synthetic fixtures only. "5.555" is a marker value used by no other fixture.

DETAIL_NAME = "Synthetic Detail Only CU"
DETAIL_RATE = "5.555"
DETAIL_STATE = "<td>Institution detail only (APR not published)</td>"
COMPARE_PATHS = (
    "/compare",
    "/compare?sort=apr",
    "/compare?sort=apr_desc",
    "/compare?sort=rate",
    "/compare?sort=freshness",
    f"/compare?scenario={CanonicalScenarioId.PURCHASE_30Y_FIXED.value}&sort=apr",
    f"/compare?scenario={CanonicalScenarioId.PURCHASE_30Y_FIXED.value}&sort=rate",
)


def _detail(offer_id: str = "o-detail", charter: int = 901) -> PublicOffer:
    offer = _offer(offer_id, DETAIL_NAME, charter, apr=None, note_rate=DETAIL_RATE)
    # The gate itself routed it (not forced by the fixture).
    assert offer.placement is Placement.INSTITUTION_DETAIL_ONLY
    assert "apr" not in {r.path for r in offer.rates}
    return offer


def test_detail_only_rate_shown_on_own_profile_with_explicit_missing_apr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ranked = _offer("o-ranked", "Synthetic Ranked CU", 902)
    app, _ = _app(tmp_path, monkeypatch, [_detail(), ranked])
    client = app.test_client()

    resp = client.get("/credit-unions/901")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    items = _profile_items(html)
    assert len(items) == 1
    (item,) = items
    assert f"{DETAIL_RATE}%" in item
    assert "APR not published" in item
    assert "APR APR" not in html and "APR Not published" not in item
    assert f"Not comparable: {DETAIL_ONLY_REASON}." in item
    assert "Observed " in item and 'class="source-link"' in item
    assert DETAIL_STATE in html
    assert "<td>Verified</td>" not in html and html.count("<td>Unknown</td>") == 2
    # The status line no longer claims that nothing is shown, but stays non-comparable.
    assert "nothing is presented as a current rate" not in html
    assert "never ranked or compared" in html
    # Detail-only alone is not verified content: noindex and absent from the sitemap.
    assert _noindex(client, "/credit-unions/901")
    urls = _sitemap(client)
    assert "https://cu-rates.org/credit-unions/901" not in urls
    assert "https://cu-rates.org/credit-unions/902" in urls  # the eligible one is unaffected

    # Never on another institution's profile.
    other = client.get("/credit-unions/902").get_data(as_text=True)
    assert DETAIL_RATE not in other and DETAIL_NAME not in other
    assert "APR not published" not in other


def test_detail_only_rate_absent_from_compare_ranking_and_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    detail = _detail()
    ranked = _offer("o-ranked", "Synthetic Ranked CU", 902)
    app, _ = _app(tmp_path, monkeypatch, [detail, ranked])
    client = app.test_client()
    for path in COMPARE_PATHS:
        resp = client.get(path)
        body = resp.get_data(as_text=True)
        assert resp.status_code == 200, path
        assert DETAIL_NAME not in body and DETAIL_RATE not in body, path
        assert "APR not published" not in body, path
    assert "Synthetic Ranked CU" in client.get("/compare").get_data(as_text=True)
    home = client.get("/").get_data(as_text=True)
    assert DETAIL_NAME not in home and DETAIL_RATE not in home
    # The comparison projection is unchanged: detail-only never enters it.
    assert classify_and_format_offer(detail, NOW) is None


def test_detail_only_alone_opens_no_compare_or_sitemap_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = _app(tmp_path, monkeypatch, [_detail()])
    client = app.test_client()
    compare = client.get("/compare")
    assert "noindex" in compare.headers.get("X-Robots-Tag", "")
    assert DETAIL_RATE not in compare.get_data(as_text=True)
    urls = _sitemap(client)
    assert "https://cu-rates.org/compare" not in urls
    assert not any("/credit-unions" in u for u in urls)
    assert _noindex(client, "/credit-unions")
    assert _noindex(client, "/credit-unions/901")
    assert DETAIL_RATE in client.get("/credit-unions/901").get_data(as_text=True)


def test_detail_only_profile_indexable_only_through_another_eligible_offer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    same_charter_ranked = _offer("o-ranked", DETAIL_NAME, 901)
    app, _ = _app(tmp_path, monkeypatch, [_detail(), same_charter_ranked])
    client = app.test_client()
    resp = client.get("/credit-unions/901")
    html = resp.get_data(as_text=True)
    # Indexable because of the APR-bearing eligible offer, not the detail-only one.
    assert "X-Robots-Tag" not in resp.headers and 'name="robots"' not in html
    assert "https://cu-rates.org/credit-unions/901" in _sitemap(client)
    assert "<td>Verified</td>" in html and DETAIL_STATE not in html
    detail_item, ranked_item = _profile_items(html)  # sorted by offer id
    assert DETAIL_RATE in detail_item and "APR not published" in detail_item
    assert "APR 6.250%" in ranked_item and "APR not published" not in ranked_item
    # Still not on compare even though its institution is.
    compare = client.get("/compare").get_data(as_text=True)
    assert DETAIL_NAME in compare and DETAIL_RATE not in compare


def test_detail_only_formatter_fails_closed() -> None:
    detail = _detail()
    formatted = format_institution_detail_offer(detail, NOW)
    assert formatted is not None
    assert formatted.detail_only and not formatted.is_comparable
    assert formatted.apr_display == "APR not published" and formatted.sort_apr is None
    # Expired (valid_until) and past the 96-hour withdrawal are never resurrected, even
    # when the reader is bypassed.
    assert format_institution_detail_offer(detail, detail.valid_until) is None
    long_lived = detail.model_copy(update={"valid_until": detail.observed_at + timedelta(days=30)})
    past_withdraw = detail.observed_at + WITHDRAW_AFTER + timedelta(seconds=1)
    assert format_institution_detail_offer(long_lived, past_withdraw) is None
    degraded = format_institution_detail_offer(
        long_lived, detail.observed_at + DEGRADE_AFTER + timedelta(hours=1)
    )
    assert degraded is not None and "Degraded freshness" in degraded.separation_reason
    # Only the detail-only placement, and only without APR: no double rendering of
    # comparison rows and no "APR not published" label on a row that has an APR.
    assert format_institution_detail_offer(_offer("o-r", "X CU", 1), NOW) is None
    with_apr = _offer("o-a", "X CU", 1, placement=Placement.INSTITUTION_DETAIL_ONLY)
    assert format_institution_detail_offer(with_apr, NOW) is None


def test_detail_only_expired_offer_not_resurrected_in_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web-layer expiry holds even if the reader returned an expired detail-only row."""
    detail = _detail()
    keep = _offer("o-keep", DETAIL_NAME, 901).model_copy(
        update={"valid_until": detail.observed_at + timedelta(days=30)}
    )
    late = detail.valid_until
    monkeypatch.setattr(profiles, "read_snapshot_offers", lambda at=None: (None, (detail, keep)))
    _, directory = profiles.build_directory(late)
    shown = [o for row in directory[901].coverage for o in row.offers]
    assert [o.offer_id for o in shown] == ["o-keep"]
    assert not directory[901].has_detail_only


def test_withdrawn_detail_only_offer_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    offers = [_detail(), _detail("o-detail-2", 903), _offer("o-keep", DETAIL_NAME, 903)]
    app, denylist = _app(tmp_path, monkeypatch, offers)
    client = app.test_client()
    assert DETAIL_RATE in client.get("/credit-unions/903").get_data(as_text=True)
    for n, target in enumerate(("o-detail", "o-detail-2")):
        denylist.withdraw(
            make.withdrawal(
                WithdrawalScope.OFFER, target, withdrawal_id=f"wd-d{n}", withdrawn_at=make.T0
            )
        )
    assert client.get("/credit-unions/901").status_code == 404  # its only offer is gone
    html = client.get("/credit-unions/903").get_data(as_text=True)
    assert DETAIL_RATE not in html and "APR not published" not in html
    assert "6.250%" in html
