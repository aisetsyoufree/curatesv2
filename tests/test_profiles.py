"""T052: credit-union profile and coverage pages, read only from the verified public snapshot."""

from datetime import datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import pytest
from flask import Flask
from flask.testing import FlaskClient

from cu_rates.contracts.enums import WithdrawalScope
from cu_rates.contracts.publication import (
    DEGRADE_AFTER,
    WITHDRAW_AFTER,
    Placement,
    evaluate_publication,
)
from cu_rates.snapshot import Denylist, SnapshotReader
from cu_rates.snapshot._models import PublicOffer, SnapshotInfo, SnapshotView
from cu_rates.snapshot.builder import _public_projection
from cu_rates.web import create_app
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
) -> PublicOffer:
    item = _make_candidate(offer_id, "Placeholder CU", apr_str=apr, now=NOW)
    projected = _public_projection(
        item, evaluate_publication(item.offer, item.context).placement, False
    )
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


def test_profile_without_verified_offers_is_noindex_and_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No APR: display-ineligible under D-05, so the charter is known but nothing is verified.
    app, _ = _app(tmp_path, monkeypatch, [_offer("o-2", "Beta CU", 222, apr=None)])
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
