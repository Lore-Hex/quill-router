from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from defusedxml import ElementTree as ET
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts import update_model_changes as recorder
from trusted_router import model_changes as history
from trusted_router.routes import catalog as catalog_routes
from trusted_router.routes import model_changes as routes

AT = "2026-10-04T00:00:00Z"
FUTURE = "2026-10-09T16:00:00Z"
MODEL = "example/model"


def state(*, confidential=False, tier=1, endpoints=None, schedules=None):
    return {MODEL: {"confidential": confidential, "privacy_tier": tier,
                    "endpoints": endpoints or {}, "pricing_schedules": schedules or {}}}


def event(kind, *, at=AT, model=MODEL, before=None, after=None, recorded_at=AT):
    return recorder.record(history.change(model, kind, before, after, at), recorded_at)


@pytest.mark.parametrize(("before", "after", "kinds"), [
    ({}, state(), ["model_added"]),
    (state(), {}, ["model_removed"]),
    (state(), state(endpoints={"route-a": {"provider": "a"}}), ["endpoint_added"]),
    (state(endpoints={"route-a": {"provider": "a"}}), state(), ["endpoint_removed"]),
    (state(endpoints={"route-a": {"privacy_tier": 1}}),
     state(endpoints={"route-a": {"privacy_tier": 2}}), ["endpoint_changed"]),
    (state(), state(confidential=True), ["confidential_route_gained"]),
    (state(confidential=True), state(), ["confidential_route_lost"]),
    (state(), state(tier=2), ["privacy_tier_changed"]),
    (state(), state(schedules={"provider": {"effective_at": FUTURE}}), ["pricing_schedule_changed"]),
    (state(), state(), []),
])
def test_diff_each_type_and_replay(before, after, kinds):
    changes = history.diff_catalogs(before, after, AT)
    assert [row["type"] for row in changes] == kinds
    assert all(row["effective_at"] == AT and row["model"] == MODEL for row in changes)
    assert history.apply_changes(before, changes) == after
    assert changes == history.diff_catalogs(before, after, AT)
    if kinds == ["endpoint_removed"]:
        assert changes[0]["endpoint"] == "route-a"
        assert changes[0]["before"] == {"provider": "a"}
        assert changes[0]["after"] is None


def test_projection_uses_capability_not_privacy_tier_and_ignores_noise():
    row = {"id": MODEL, "pricing": {"prompt": "1"}, "trustedrouter": {
        "privacy_tier": 999, "capabilities": {"confidential": False},
        "endpoints": [{"id": "a", "provider": "test", "privacy_tier": 999,
                       "capabilities": {"confidential": False}}],
    }}
    before = history.route_state([row])
    assert before[MODEL]["confidential"] is False
    assert before[MODEL]["endpoints"]["a"]["confidential"] is False
    row["pricing"]["prompt"] = "2"
    row["trustedrouter"]["privacy_tier_label"] = "new label"
    assert history.diff_catalogs(before, history.route_state([row]), AT) == []
    row["trustedrouter"]["capabilities"]["confidential"] = True
    assert [e["type"] for e in history.diff_catalogs(before, history.route_state([row]), AT)] == ["confidential_route_gained"]
    row["trustedrouter"]["internal_only"] = True
    assert history.route_state([row]) == {}


def test_alias_absence_is_not_a_confidential_loss():
    assert history.diff_catalogs(state(confidential=None), state(), AT) == []


def test_scheduled_projection_uses_effective_date_and_records_absent_retirement(monkeypatch):
    def raw(confidential):
        return {"rows": [{"id": MODEL, "trustedrouter": {
            "privacy_tier": 1, "capabilities": {"confidential": confidential}, "endpoints": [],
            "pricing_schedules": {} if confidential else {"test": {"rate": 2}},
        }}], "cutovers": [FUTURE], "retirements": [
            {"provider": "retiring", "models": ["example/absent"], "effective_at": FUTURE},
        ]}

    def snapshot(at, **kwargs):
        if at == FUTURE:
            assert kwargs["freshness_at"] == AT
        return raw(at == AT)

    monkeypatch.setattr(recorder, "read_snapshot", snapshot)
    models, planned = recorder.projection(AT)
    assert models[MODEL]["confidential"] is True
    assert {e["type"] for e in planned} == {
        "confidential_route_lost", "pricing_schedule_changed", "retirement_scheduled",
    }
    assert all(e["effective_at"] == FUTURE and e["source"] == "scheduled" for e in planned)
    notice = next(e for e in planned if e["type"] == "retirement_scheduled")
    assert notice["model"] == "example/absent"
    assert notice["after"] == {"provider": "retiring"}


def test_ci_command_fails_on_drift_then_passes_after_update(tmp_path, monkeypatch, capsys):
    state_path, history_path = tmp_path / "state.json", tmp_path / "history.jsonl"
    current = state()
    monkeypatch.setattr(recorder, "projection", lambda at: (copy.deepcopy(current), []))
    args = ["update_model_changes.py", "--state", str(state_path), "--history", str(history_path), "--at", AT]
    monkeypatch.setattr("sys.argv", [*args, "--check"])
    assert recorder.main() == 1
    assert recorder.COMMAND in capsys.readouterr().err
    monkeypatch.setattr("sys.argv", args)
    assert recorder.main() == 0
    current[MODEL]["endpoints"]["new-route"] = {"provider": "new"}
    monkeypatch.setattr("sys.argv", [*args, "--check"])
    assert recorder.main() == 1
    assert "Catalog change history is stale" in capsys.readouterr().err
    monkeypatch.setattr("sys.argv", args)
    assert recorder.main() == 0
    recorded = [json.loads(line) for line in history_path.read_text().splitlines()]
    assert len(recorded) == 1
    assert recorded[0]["type"] == "endpoint_added"
    assert recorded[0]["after"] == {"provider": "new"}
    monkeypatch.setattr("sys.argv", [*args, "--check"])
    assert recorder.main() == 0
    history_path.write_text("")
    assert recorder.main() == 1  # cannot silently lose the history and keep its baseline


def test_check_pins_observation_clock_and_detects_future_drift(tmp_path, monkeypatch):
    state_path, history_path = tmp_path / "s", tmp_path / "h"
    calls = []
    planned = []

    def projection(at):
        calls.append(at)
        return state(), copy.deepcopy(planned)

    monkeypatch.setattr(recorder, "projection", projection)
    assert recorder.update(state_path, history_path, AT)
    assert recorder.update(state_path, history_path, FUTURE, check=True)
    assert calls == [AT, AT]
    planned.append(event("retirement_scheduled", at=FUTURE))
    assert not recorder.update(state_path, history_path, FUTURE, check=True)
    assert recorder.update(state_path, history_path, AT)
    assert recorder.update(state_path, history_path, FUTURE, check=True)


def test_update_idempotence_cutover_cancellation_and_reinstatement(tmp_path, monkeypatch):
    state_path, history_path = tmp_path / "s", tmp_path / "h"
    planned = history.diff_catalogs(state(confidential=True), state(), FUTURE, source="scheduled")
    monkeypatch.setattr(recorder, "projection", lambda at: (state(confidential=True), planned))
    recorder.update(state_path, history_path, AT)
    initial = history_path.read_bytes()
    recorder.update(state_path, history_path, AT)
    assert history_path.read_bytes() == initial
    monkeypatch.setattr(recorder, "projection", lambda at: (state(confidential=True), []))
    recorder.update(state_path, history_path, "2026-10-05T00:00:00Z")
    rows = [json.loads(line) for line in history_path.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[1]["type"] == "schedule_cancelled"
    assert rows[1]["before"]["id"] == rows[0]["id"]
    assert [r["type"] for r in history.active_history(rows)] == ["schedule_cancelled"]
    monkeypatch.setattr(recorder, "projection", lambda at: (state(confidential=True), planned))
    recorder.update(state_path, history_path, "2026-10-06T00:00:00Z")
    rows = [json.loads(line) for line in history_path.read_text().splitlines()]
    assert len(rows) == 3 and rows[0]["id"] != rows[2]["id"]
    monkeypatch.setattr(recorder, "projection", lambda at: (state(), []))
    recorder.update(state_path, history_path, FUTURE)
    assert len(history_path.read_text().splitlines()) == 3  # no duplicate at cutover
    with pytest.raises(ValueError, match="before the recorded baseline"):
        recorder.update(state_path, history_path, AT)


@pytest.fixture
def feed(monkeypatch):
    rows = [
        event("confidential_route_lost", before=True, after=False),
        event("endpoint_removed", model="example/other&model", before={"provider": "a"}),
        event("retirement_scheduled", at=FUTURE, after={"provider": "test"}),
        event("pricing_schedule_changed", after={"rate": 2}),
        event("privacy_tier_changed", recorded_at="2026-10-05T00:00:00Z", before=2, after=1),
    ]
    monkeypatch.setattr(routes, "load_history", lambda: tuple(reversed(rows)))
    monkeypatch.setattr(routes, "_utc_now", lambda: datetime(2026, 10, 5, tzinfo=UTC))
    app = FastAPI()
    from fastapi import APIRouter
    router = APIRouter()
    routes.register_model_change_routes(router)
    app.include_router(router, prefix="/v1")
    return TestClient(app), rows


def test_endpoint_filters_order_dates_and_no_auth(feed):
    client, rows = feed
    response = client.get("/v1/models/changes")
    assert response.status_code == 200
    expected = sorted((e for e in rows if e["type"] != "pricing_schedule_changed"), key=lambda e: (e["recorded_at"], e["id"]))
    assert response.json() == {"data": expected}
    response = client.get("/v1/models/changes", params={"model": MODEL, "since": AT, "upcoming": "true"})
    assert response.status_code == 200
    assert [e["type"] for e in response.json()["data"]] == ["retirement_scheduled"]
    assert len(client.get("/v1/models/changes?upcoming=false").json()["data"]) == 3
    assert len(client.get("/v1/models/changes?include_prices=true").json()["data"]) == 5
    response = client.get("/v1/models/changes?type=pricing_schedule_changed&type=confidential_route_lost")
    assert {e["type"] for e in response.json()["data"]} == {"pricing_schedule_changed", "confidential_route_lost"}
    response = client.get("/v1/models/changes", params={"since": "2026-10-04T00:00:00.001Z"})
    assert [e["type"] for e in response.json()["data"]] == ["privacy_tier_changed"]
    assert client.get("/v1/models/changes?model=unknown").json() == {"data": []}


@pytest.mark.parametrize("query", ["since=not-a-date", "since=2026-10-04T00:00:00", "type=made_up", "upcoming=maybe"])
def test_endpoint_rejects_invalid_filters(feed, query):
    client, _ = feed
    response = client.get(f"/v1/models/changes?{query}")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][0] == "query"


def test_atom_is_valid_xml_with_exactly_one_entry_per_json_change(feed):
    client, _ = feed
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for query in ("", "?model=unknown", "?include_prices=true", "?type=retirement_scheduled"):
        response = client.get(f"/v1/models/changes.atom{query}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/atom+xml")
        root = ET.fromstring(response.content)
        assert root.tag == "{http://www.w3.org/2005/Atom}feed"
        entries = root.findall("a:entry", ns)
        rows = client.get(f"/v1/models/changes{query}").json()["data"]
        assert len(entries) == len(rows)
        assert [json.loads(e.findtext("a:content", namespaces=ns)) for e in entries] == rows
        assert len({e.findtext("a:id", namespaces=ns) for e in entries}) == len(rows)
        assert all(e.findtext("a:updated", namespaces=ns) for e in entries)


def test_models_last_change_excludes_future_cancelled_and_price_events(monkeypatch, client):
    cancelled = event("endpoint_removed", at="2026-10-03T00:00:00Z", model="deepseek/deepseek-v4-flash")
    rows = [
        event("confidential_route_lost", at="2026-09-16T00:00:00Z", model=cancelled["model"]),
        event("pricing_schedule_changed", model=cancelled["model"]),
        event("endpoint_removed", at=FUTURE, model=cancelled["model"]),
        cancelled,
        event("schedule_cancelled", model=cancelled["model"], before={"id": cancelled["id"]}),
    ]
    monkeypatch.setattr(history, "load_history", lambda: tuple(rows))
    monkeypatch.setattr(catalog_routes, "_utc_now", lambda: datetime(2026, 10, 4, tzinfo=UTC))
    catalog_routes._public_catalog_payload.cache_clear()
    try:
        response = client.get("/v1/models")
        assert response.status_code == 200
        model = next(r for r in response.json()["data"] if r["id"] == cancelled["model"])
        assert model["trustedrouter"]["last_route_change_at"] == "2026-09-16T00:00:00Z"
        assert history.last_route_changes(FUTURE)[cancelled["model"]] == FUTURE
    finally:
        catalog_routes._public_catalog_payload.cache_clear()


def test_committed_backfill_has_september_confidential_loss_and_upcoming_retirements():
    rows = [json.loads(line) for line in history.HISTORY_PATH.read_text().splitlines()]
    losses = [e for e in rows if e["model"] == "deepseek/deepseek-v4-flash" and e["type"] == "confidential_route_lost"]
    assert len(losses) >= 1
    assert losses[0]["effective_at"].startswith("2026-09-16")
    assert losses[0]["before"] is True and losses[0]["after"] is False
    assert losses[0]["commit"]
    assert any(e["type"] == "retirement_scheduled" and e["effective_at"] == "2026-10-16T00:00:00Z" for e in rows)
    assert len({e["id"] for e in rows}) == len(rows)


def test_public_documentation_links_feed():
    root = Path(__file__).resolve().parents[1] / "src/trusted_router/templates/public"
    assert "/v1/models/changes.atom" in (root / "models.html").read_text()
    docs = (root / "docs.html").read_text()
    assert 'id="model-changes"' in docs
    assert "last_route_change_at" in docs


def test_snapshot_includes_retirements_without_a_named_date_constant(monkeypatch):
    from types import SimpleNamespace

    from scripts.model_change_snapshot import snapshot
    from trusted_router import catalog, catalog_data, provider_lifecycle

    # snapshot runs in a disposable process in production. Register these
    # assignments with monkeypatch here so its clock replacements are restored.
    monkeypatch.setattr(provider_lifecycle, "_utc_now", provider_lifecycle._utc_now)
    monkeypatch.setattr(catalog_data, "_utc_now", catalog_data._utc_now)
    monkeypatch.setattr(catalog, "MODELS", {})
    retirement = SimpleNamespace(provider="example", model_ids={MODEL},
                                 effective_at=datetime(2026, 11, 11, tzinfo=UTC))
    monkeypatch.setattr(provider_lifecycle, "_RETIREMENTS", (retirement,))
    at = datetime(2026, 10, 4, tzinfo=UTC)
    result = snapshot(at, at)
    assert "2026-11-11T00:00:00Z" in result["cutovers"]
    assert result["retirements"] == [{"provider": "example", "models": [MODEL],
                                       "effective_at": "2026-11-11T00:00:00Z"}]
    assert result["rows"] == []


def test_real_control_plane_feed_is_public_and_openapi_documents_it(client):
    response = client.get("/v1/models/changes", params={
        "model": "deepseek/deepseek-v4-flash", "type": "confidential_route_lost",
    })
    assert response.status_code == 200
    rows = response.json()["data"]
    assert len(rows) == 1
    assert rows[0]["effective_at"] == "2026-09-16T00:59:32Z"
    assert rows[0]["before"] is True and rows[0]["after"] is False
    schema = client.app.openapi()
    for path, media_type in (("/v1/models/changes", "application/json"),
                             ("/v1/models/changes.atom", "application/atom+xml")):
        operation = schema["paths"][path]["get"]
        assert not operation.get("security")
        assert operation["servers"] == [{"url": "https://trustedrouter.com"}]
        assert media_type in operation["responses"]["200"]["content"]


def test_same_second_reinstatement_has_a_new_active_id(tmp_path, monkeypatch):
    planned = [history.change(MODEL, "retirement_scheduled", None,
                              {"provider": "a"}, FUTURE, source="scheduled")]
    monkeypatch.setattr(recorder, "projection", lambda at: (state(), list(planned)))
    state_path, history_path = tmp_path / "s", tmp_path / "h"
    recorder.update(state_path, history_path, AT)
    original = planned.pop()
    recorder.update(state_path, history_path, AT)
    planned.append(original)
    recorder.update(state_path, history_path, AT)
    rows = [json.loads(line) for line in history_path.read_text().splitlines()]
    assert len(rows) == len({e["id"] for e in rows}) == 3
    active = history.active_history(rows)
    assert sorted(e["type"] for e in active) == ["retirement_scheduled", "schedule_cancelled"]
    assert len({e["recorded_at"] for e in rows}) == 1
