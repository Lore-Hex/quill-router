from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

import clickhouse.build_public_snapshots as snapshot_builder
from trusted_router import encrypted_ai
from trusted_router.catalog import MODELS, endpoints_for_model
from trusted_router.catalog_data import PRIVACY_TIER_CONFIDENTIAL
from trusted_router.catalog_privacy import endpoint_privacy_tier
from trusted_router.encrypted_ai import (
    aggregate_encrypted_ai,
    directory,
    directory_view,
    is_confidential_route,
)

GENERATED_AT = "2026-11-03T22:00:00.000Z"


def _route(confidential: bool) -> tuple[str, str]:
    for model_id in sorted(MODELS):
        for endpoint in endpoints_for_model(model_id):
            if (endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_CONFIDENTIAL) is confidential:
                return endpoint.provider, model_id
    raise AssertionError("catalog has no such route")


CONF = _route(True)
PLAIN = _route(False)


def _row(app: str, ref: str, route: tuple[str, str], *, t1: int = 0, t7: int = 0, t30: int = 0,
         categories: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "app": app, "ref": ref, "provider": route[0], "model": route[1], "categories": list(categories),
        "t1": t1, "r1": 1 if t1 else 0, "t7": t7, "r7": 2 if t7 else 0, "t30": t30, "r30": 3 if t30 else 0,
    }


def test_the_listing_data_is_well_formed() -> None:
    data = directory()
    assert len(data.listings) >= 30
    assert len({listing.slug for listing in data.listings}) == len(data.listings)
    for listing in data.listings:
        assert listing.url.startswith("https://"), listing.slug
        assert listing.verify_url is None or listing.verify_url.startswith("https://"), listing.slug
        assert listing.tier != "verified", "verified is earned on the exchange, never curated"
        assert len(listing.description) <= 160, listing.slug
        for match in listing.matches:
            # Bare words ("tab", "jo") would pin another app's traffic on a listing.
            assert "." in match or " " in match, (listing.slug, match)
    assert data.consumer_categories <= {slug for slug, _ in data.categories}


def test_confidential_routes_follow_the_catalog_privacy_tier() -> None:
    assert is_confidential_route(*CONF)
    assert not is_confidential_route(*PLAIN)
    # A retired model id counts only on a provider that is Confidential everywhere.
    assert is_confidential_route("tinfoil", "retired/model-no-longer-listed")
    assert not is_confidential_route(PLAIN[0], "retired/model-no-longer-listed")


def test_apps_rank_by_encrypted_tokens_per_window() -> None:
    rows = [
        _row("Acme", "acme.com", CONF, t1=10, t7=100, t30=1_000),
        _row("Acme Beta", "acme.com", CONF, t7=50, t30=50),  # same app: same referer domain
        _row("Acme", "acme.com", PLAIN, t7=150, t30=150),
        _row("Zed", "", CONF, t7=500, t30=500),
        _row("Plain Only", "plain.example", PLAIN, t7=9_999, t30=9_999),
        _row("attested-gateway", "", CONF, t1=7, t7=7, t30=7),  # gateway default: not an app
    ]
    snapshot = aggregate_encrypted_ai(rows, generated_at=GENERATED_AT)
    week = snapshot["windows"]["week"]
    assert [a["name"] for a in week["apps"]] == ["Zed", "Acme"]
    acme = week["apps"][1]
    assert acme["domain"] == "acme.com" and acme["encrypted_tokens"] == 150
    assert acme["encrypted_share"] == pytest.approx(150 / 300)
    assert [a["name"] for a in snapshot["windows"]["day"]["apps"]] == ["Acme"]
    # Exchange-wide encrypted tokens include untitled traffic.
    assert week["exchange_encrypted_tokens"] == 100 + 50 + 500 + 7
    assert snapshot["windows"]["month"]["apps"][0]["name"] == "Acme"


def test_hidden_apps_are_dropped_from_every_view(monkeypatch: pytest.MonkeyPatch) -> None:
    real = directory()
    monkeypatch.setattr(encrypted_ai, "directory", lambda: real.__class__(
        categories=real.categories, consumer_categories=real.consumer_categories,
        listings=real.listings, hidden=frozenset({"acme.com", "zed"}),
    ))
    snapshot = aggregate_encrypted_ai(
        [_row("Acme", "acme.com", CONF, t7=1, t30=1), _row("Zed", "", CONF, t7=1, t30=1)],
        generated_at=GENERATED_AT,
    )
    assert all(not w["apps"] for w in snapshot["windows"].values())


def test_the_view_says_so_when_no_consumer_agent_verified() -> None:
    snapshot = aggregate_encrypted_ai(
        [_row("Kilo Code", "kilocode.ai", CONF, t7=10, t30=10, categories=("coding",))],
        generated_at=GENERATED_AT,
    )
    view = directory_view(snapshot)
    week = next(w for w in view["windows"] if w["name"] == "week")
    assert [a["name"] for a in week["apps"]] == ["Kilo Code"]
    assert week["consumer_count"] == 0 and week["apps"][0]["category"] == "other"


def test_a_listed_app_on_the_exchange_moves_up_to_verified() -> None:
    snapshot = aggregate_encrypted_ai([_row("Fambot", "fambot.com", CONF, t7=5, t30=5)], generated_at=GENERATED_AT)
    view = directory_view(snapshot)
    week = next(w for w in view["windows"] if w["name"] == "week")
    assert week["apps"][0]["listing"].slug == "fambot" and week["consumer_count"] == 1
    assert "fambot" not in {listing.slug for listing in view["curated"]["watchlist"]}


def test_the_page_renders_every_tier_and_never_says_not_encrypted(client: Any) -> None:
    response = client.get("/encrypted-ai")
    assert response.status_code == 200
    html = response.text
    for text in ("Verified encrypted", "Encrypted, self-attested", "On-device", "Watchlist",
                 "Not verified", "Claim this listing", "X-NYTE-Title", "Tinfoil Private AI"):
        assert text in html, text
    assert "not encrypted" not in html.lower().replace("not that they are unencrypted", "")


def test_the_page_headline_with_a_snapshot(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router.routes import public as public_routes

    snapshot = aggregate_encrypted_ai(
        [_row("Kilo Code", "kilocode.ai", CONF, t7=12_345, t30=12_345)], generated_at=GENERATED_AT
    )
    monkeypatch.setattr(public_routes, "_encrypted_ai_snapshot", lambda _settings: snapshot)
    html = client.get("/encrypted-ai").text
    assert "No consumer agent has verified yet." in html
    assert "kilocode.ai" in html and "12,345" in html


@pytest.mark.parametrize(("minute", "built"), [(0, True), (15, True), (7, False)])
def test_the_builder_publishes_the_ranking_quarter_hourly(
    monkeypatch: pytest.MonkeyPatch, minute: int, built: bool
) -> None:
    inserted: list[bytes] = []
    monkeypatch.setenv("CH_PASSWORD", "test")
    monkeypatch.setattr(snapshot_builder, "_utcnow", lambda: dt.datetime(2026, 11, 3, 22, minute, tzinfo=dt.UTC))
    fakes: dict[str, Any] = {
        "_status_inputs": ([], []), "_evidence_samples": [], "_samples": [], "_video_samples": [],
        "_client_reliability_rows": [], "_client_reliability_signals": {},
    }
    for name, value in fakes.items():
        monkeypatch.setattr(snapshot_builder, name, lambda *_a, _v=value, **_k: _v)
    monkeypatch.setattr(snapshot_builder, "_encrypted_ai_rows",
                        lambda _p: [_row("Acme", "acme.com", CONF, t7=1, t30=1)])

    def fake_query(_password: str, sql: str, *, input_bytes: bytes | None = None, **_k: Any) -> str:
        assert sql.startswith("INSERT INTO public_analytics_snapshots")
        inserted.append(input_bytes or b"")
        return ""

    monkeypatch.setattr(snapshot_builder, "_query", fake_query)
    assert snapshot_builder.main() == 0
    names = {json.loads(line)["name"] for line in inserted[0].decode().splitlines()}
    assert ("encrypted_ai" in names) is built
    assert "apps" in names


def test_the_builder_query_reads_names_and_counts_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_query(_password: str, sql: str, **_k: Any) -> str:
        seen.append(sql)
        return json.dumps(_row("Acme", "acme.com", CONF, t30=1)) + "\n"

    monkeypatch.setattr(snapshot_builder, "_query", fake_query)
    rows = snapshot_builder._encrypted_ai_rows("pw")
    assert rows[0]["app"] == "Acme"
    sql = seen[0]
    for forbidden in ("workspace_id", "key_id", "user", "session_id", "tenant_id"):
        assert forbidden not in sql, forbidden
    assert "INTERVAL 30 DAY" in sql and "synthetic = 0" in sql and "LIMIT" in sql
