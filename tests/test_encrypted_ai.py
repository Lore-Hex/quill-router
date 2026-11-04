from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest

import clickhouse.build_public_snapshots as snapshot_builder
from trusted_router import encrypted_ai
from trusted_router.catalog import MODELS, endpoints_for_model
from trusted_router.catalog_data import PRIVACY_TIER_CONFIDENTIAL
from trusted_router.catalog_privacy import endpoint_privacy_tier
from trusted_router.config import Settings
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
        # Traffic naming the company by its name or URL domain is held out.
        assert listing.name.casefold() in listing.matches, listing.slug
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


def _with_listing_data(monkeypatch: pytest.MonkeyPatch, **changes: Any) -> None:
    import dataclasses

    real = directory()
    monkeypatch.setattr(encrypted_ai, "directory", lambda: dataclasses.replace(real, **changes))


@pytest.mark.parametrize(("title", "ref"), [
    ("Maple AI", "evil.example"),  # a curated company's name, any domain
    ("x", "muse.ai"),  # a curated company's domain, any name
    ("Fambot", "fambot.com"),  # the company itself, before it confirmed
    ("Meta Agent", "app.muse.ai"),  # a subdomain of a curated company's domain
    ("x", "www.chat.trymaple.ai"),
    ("x", "muse.ai."),  # a fully qualified host is the same host
    ("x", "app.muse.ai."),
    ("app.muse.ai", ""),  # a dotted title naming a listed domain
])
def test_traffic_naming_a_curated_company_does_not_rank_unconfirmed(title: str, ref: str) -> None:
    snapshot = aggregate_encrypted_ai([_row(title, ref, CONF, t1=9, t7=9, t30=9)], generated_at=GENERATED_AT)
    view = directory_view(snapshot)
    assert all(not w["apps"] for w in view["windows"])
    # Curated cards never move: Maple stays self-attested, Muse and Fambot on the watchlist.
    assert {"maple-ai"} <= {listing.slug for listing in view["curated"]["self_attested"]}
    assert {"muse", "fambot"} <= {listing.slug for listing in view["curated"]["watchlist"]}


def test_a_confirmed_company_ranks_under_its_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses

    real = directory()
    _with_listing_data(monkeypatch, listings=tuple(
        dataclasses.replace(listing, confirmed=True) if listing.slug == "fambot" else listing
        for listing in real.listings
    ))
    snapshot = aggregate_encrypted_ai([_row("Fambot", "fambot.com", CONF, t7=5, t30=5)], generated_at=GENERATED_AT)
    week = next(w for w in directory_view(snapshot)["windows"] if w["name"] == "week")
    assert week["apps"][0]["listing"].slug == "fambot" and week["consumer_count"] == 1
    assert week["apps"][0]["category"] == "family"
    # Verified now: its watchlist card ("Not verified") goes.
    assert "fambot" not in {listing.slug for listing in directory_view(snapshot)["curated"]["watchlist"]}
    # Its name from another domain still does not rank, with or without its listing.
    spoof = aggregate_encrypted_ai([_row("Fambot", "evil.example", CONF, t7=5, t30=5)], generated_at=GENERATED_AT)
    assert all(not w["apps"] for w in directory_view(spoof)["windows"])
    # Its own domain carrying another listed company's name does not rank either.
    other = aggregate_encrypted_ai([_row("Maple AI", "fambot.com", CONF, t7=5, t30=5)], generated_at=GENERATED_AT)
    assert all(not w["apps"] for w in directory_view(other)["windows"])


def test_a_lookalike_domain_is_not_a_listed_one() -> None:
    snapshot = aggregate_encrypted_ai([_row("x", "notmuse.ai", CONF, t7=1, t30=1)], generated_at=GENERATED_AT)
    week = next(w for w in directory_view(snapshot)["windows"] if w["name"] == "week")
    assert [a["domain"] for a in week["apps"]] == ["notmuse.ai"]


def test_hidden_entries_compare_like_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_listing_data(monkeypatch, hidden=frozenset({encrypted_ai._domain("WWW.Acme.com")}))
    snapshot = aggregate_encrypted_ai([_row("Acme", "www.acme.com", CONF, t7=1, t30=1)], generated_at=GENERATED_AT)
    assert not snapshot["windows"]["week"]["apps"]


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
    assert "kilocode.ai" in html
    # The page ranks by encrypted tokens but never prints a token count.
    assert "12,345" not in html and "12345" not in html


def test_the_shipped_directory_hides_robot_robot_and_human() -> None:
    rows = [
        _row("Robot, Robot, and Human", "robotrobotandhuman.com", CONF, t7=9_000, t30=9_000),
        _row("Robot, Robot, and Human", "", CONF, t7=9_000, t30=9_000),
        _row("Kilo Code", "kilocode.ai", CONF, t7=10, t30=10),
    ]
    week = aggregate_encrypted_ai(rows, generated_at=GENERATED_AT)["windows"]["week"]
    assert [a["domain"] for a in week["apps"]] == ["kilocode.ai"]


def test_removed_listings_stay_removed() -> None:
    slugs = {listing.slug for listing in encrypted_ai.directory().listings}
    assert not slugs & {"apple-private-cloud-compute", "confident-security", "anonrouter"}


def _run_builder(monkeypatch: pytest.MonkeyPatch, *, age: float | None,
                 rows: Any = None) -> list[set[str]]:
    inserted: list[set[str]] = []
    monkeypatch.setenv("CH_PASSWORD", "test")
    monkeypatch.setattr(snapshot_builder, "_utcnow", lambda: dt.datetime(2026, 11, 3, 22, 7, tzinfo=dt.UTC))
    fakes: dict[str, Any] = {
        "_status_inputs": ([], []), "_evidence_samples": [], "_samples": [], "_video_samples": [],
        "_client_reliability_rows": [], "_client_reliability_signals": {},
    }
    for name, value in fakes.items():
        monkeypatch.setattr(snapshot_builder, name, lambda *_a, _v=value, **_k: _v)
    monkeypatch.setattr(snapshot_builder, "_encrypted_ai_age_seconds", lambda _p, *, now: age)

    def encrypted_rows(_password: str) -> Any:
        if isinstance(rows, Exception):
            raise rows
        return rows if rows is not None else [_row("Acme", "acme.com", CONF, t7=1, t30=1)]

    monkeypatch.setattr(snapshot_builder, "_encrypted_ai_rows", encrypted_rows)

    def fake_query(_password: str, sql: str, *, input_bytes: bytes | None = None, **_k: Any) -> str:
        assert sql.startswith("INSERT INTO public_analytics_snapshots")
        inserted.append({json.loads(line)["name"] for line in (input_bytes or b"").decode().splitlines()})
        return ""

    monkeypatch.setattr(snapshot_builder, "_query", fake_query)
    assert snapshot_builder.main() == 0
    return inserted


@pytest.mark.parametrize(("age", "built"), [(None, True), (14 * 60, True), (13 * 60, False), (-5, True)])
def test_the_builder_rebuilds_the_ranking_when_it_is_old(
    monkeypatch: pytest.MonkeyPatch, age: float | None, built: bool
) -> None:
    inserted = _run_builder(monkeypatch, age=age)
    assert "apps" in inserted[0] and "encrypted_ai" not in inserted[0]
    assert (inserted[1:] == [{"encrypted_ai"}]) is built


@pytest.mark.parametrize("failure", [RuntimeError("query"), ValueError("schema"), json.JSONDecodeError("x", "y", 0)])
def test_a_failed_ranking_build_never_stops_the_core_snapshots(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    inserted = _run_builder(monkeypatch, age=None, rows=failure)
    assert len(inserted) == 1 and "leaderboard" in inserted[0]


def test_hostile_app_titles_are_escaped(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router.routes import public as public_routes

    snapshot = aggregate_encrypted_ai(
        [_row('<script>alert(1)</script>"', "evil.example", CONF, t7=1, t30=1)], generated_at=GENERATED_AT
    )
    monkeypatch.setattr(public_routes, "_encrypted_ai_snapshot", lambda _settings: snapshot)
    html = client.get("/encrypted-ai").text
    assert "<script>alert(1)" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_the_page_reader_backs_off_and_never_serves_a_stale_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from trusted_router.routes import public as public_routes

    clock = [1_000.0]
    reads: list[str] = []
    fresh = {"generated_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"), "windows": {}}
    stale = {"generated_at": "2020-01-01T00:00:00Z", "windows": {}}
    answers: list[Any] = [fresh, RuntimeError("down"), stale]

    def read(name: str) -> Any:
        reads.append(name)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(public_routes, "_ENCRYPTED_AI_CACHE", None)
    monkeypatch.setattr(public_routes.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(public_routes, "_precomputed_public_analytics_snapshot", read)
    settings = cast(Settings, SimpleNamespace(environment="production"))
    assert public_routes._encrypted_ai_snapshot(settings) == fresh
    assert public_routes._encrypted_ai_snapshot(settings) == fresh and len(reads) == 1  # cached
    clock[0] += 60
    assert public_routes._encrypted_ai_snapshot(settings) == fresh  # read failed: last good, still fresh
    assert public_routes._encrypted_ai_snapshot(settings) == fresh and len(reads) == 2  # no retry storm
    monkeypatch.setattr(public_routes, "_ENCRYPTED_AI_CACHE", (clock[0], stale))
    assert public_routes._encrypted_ai_snapshot(settings) is None  # too old to call "this week"


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
    assert "SETTINGS max_execution_time = " in sql


def test_the_listing_loader_normalizes_hidden_entries(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    raw = json.loads(encrypted_ai.LISTINGS_PATH.read_text(encoding="utf-8"))
    raw["hidden"] = ["WWW.Acme.COM", " Zed "]
    path = tmp_path / "listings.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr(encrypted_ai, "LISTINGS_PATH", path)
    encrypted_ai.directory.cache_clear()
    try:
        assert directory().hidden == frozenset({"acme.com", "zed"})
    finally:
        encrypted_ai.directory.cache_clear()
