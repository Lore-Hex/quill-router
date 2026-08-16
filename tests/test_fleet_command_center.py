"""Tests for the fleet command-center substrate (see synthetic/fleet.py).

Covers the two halves plus the alert plumbing they rely on:
- peer policing: peer_monitor samples up/down for fresh, stale, broken,
  and unreachable peers, and the run_synthetic_once gating;
- fleet view: /fleet.json + /fleet merge, heartbeat liveness rows;
- ops_alert: money alarms become fingerprinted Sentry issues and can
  never break their caller.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.storage import STORE
from trusted_router.storage_models import iso_now, utcnow
from trusted_router.synthetic import fleet
from trusted_router.synthetic.alerts import ops_alert
from trusted_router.synthetic.fleet import (
    fleet_peer_probes,
    fleet_peers,
    fleet_snapshot,
    record_heartbeat,
)


@pytest.fixture(autouse=True)
def _reset_heartbeat_marks() -> None:
    fleet.reset_for_tests()


def _settings(**kwargs: object) -> Settings:
    return Settings(environment="test", **kwargs)  # type: ignore[arg-type]


def _peer_transport(responses: dict[str, object]) -> httpx.MockTransport:
    """Map host -> response spec: dict payload => 200 status.json body,
    int => that HTTP status, Exception => raised as a connect error."""

    def handler(request: httpx.Request) -> httpx.Response:
        spec = responses.get(request.url.host)
        if isinstance(spec, Exception):
            raise httpx.ConnectError("boom", request=request)
        if isinstance(spec, int):
            return httpx.Response(spec)
        return httpx.Response(200, json={"data": spec})

    return httpx.MockTransport(handler)


def test_remediator_background_loop_can_be_disabled_for_request_cpu() -> None:
    app = create_app(
        _settings(
            remediator_mode="observe",
            remediator_in_process_enabled=False,
            sentry_dsn=None,
        ),
        init_observability=False,
    )

    assert "_start_remediator_loop" not in {handler.__name__ for handler in app.router.on_startup}


def test_fleet_peers_parsing_skips_junk() -> None:
    settings = _settings(
        synthetic_fleet_peers="gcp=https://trustedrouter.com/, junk, =nope,aws=https://aws.trustedrouter.com"
    )
    assert fleet_peers(settings) == [
        ("gcp", "https://trustedrouter.com"),
        ("aws", "https://aws.trustedrouter.com"),
    ]


def test_peer_probes_classify_fresh_stale_broken_unreachable() -> None:
    settings = _settings(
        synthetic_fleet_peers=(
            "fresh=https://fresh.example,stale=https://stale.example,"
            "broken=https://broken.example,gone=https://gone.example"
        )
    )
    transport = _peer_transport(
        {
            "fresh.example": {"monitor_freshness": {"is_stale": False}, "overall_status": "up"},
            "stale.example": {"monitor_freshness": {"is_stale": True}, "overall_status": "up"},
            "broken.example": 500,
            "gone.example": httpx.ConnectError("x", request=None),  # type: ignore[arg-type]
        }
    )

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(transport=transport) as client:
            samples = await fleet_peer_probes(settings, monitor_region="test-1", client=client)
        return {s.target: s for s in samples}

    by_target = asyncio.run(run())
    assert by_target["fresh"].status == "up"  # type: ignore[union-attr]
    assert by_target["stale"].status == "down"  # type: ignore[union-attr]
    assert by_target["stale"].error_type == "peer_monitor_stale"  # type: ignore[union-attr]
    assert by_target["broken"].status == "down"  # type: ignore[union-attr]
    assert by_target["broken"].error_type == "peer_bad_status"  # type: ignore[union-attr]
    assert by_target["gone"].status == "down"  # type: ignore[union-attr]
    assert str(by_target["gone"].error_type).startswith("peer_unreachable")  # type: ignore[union-attr]
    for sample in by_target.values():
        assert sample.probe_type == "peer_monitor"  # type: ignore[union-attr]


def test_peer_probe_bad_payload_is_down() -> None:
    settings = _settings(synthetic_fleet_peers="odd=https://odd.example")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    async def run() -> list[object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return list(await fleet_peer_probes(settings, monitor_region="test-1", client=client))

    (sample,) = asyncio.run(run())
    assert sample.status == "down"  # type: ignore[union-attr]
    assert sample.error_type == "peer_bad_payload"  # type: ignore[union-attr]


def test_run_synthetic_once_gates_peer_probes_off_in_test_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.synthetic import probes as probes_module

    called: list[str] = []

    async def spy(*args: object, **kwargs: object) -> list[object]:
        called.append("fleet")
        return []

    monkeypatch.setattr(fleet, "fleet_peer_probes", spy)
    monkeypatch.setattr(probes_module, "configured_targets", lambda settings: [])

    from trusted_router.synthetic.probes import run_synthetic_once

    # environment="test": the peers stay configured but the branch is off.
    asyncio.run(run_synthetic_once(_settings()))
    assert called == []

    # any non-test environment with peers configured: the branch runs.
    canary = Settings(environment="canary")
    assert canary.synthetic_fleet_peers  # default-on config
    asyncio.run(run_synthetic_once(canary))
    assert called == ["fleet"]


def test_record_heartbeat_and_liveness_rows() -> None:
    settings = _settings()
    record_heartbeat("scheduler:test-loop", settings=settings)
    stored = STORE.synthetic_probe_samples(
        target="scheduler:test-loop",
        probe_type="heartbeat",
        limit=1,
    )[0]
    bucket = int(stored.id.rsplit("_", 1)[1])
    created_at = dt.datetime.fromisoformat(stored.created_at.replace("Z", "+00:00"))
    assert created_at == dt.datetime.fromtimestamp(
        bucket * fleet.HEARTBEAT_BUCKET_SECONDS,
        tz=dt.UTC,
    )

    snapshot = asyncio.run(fleet_snapshot(settings))
    beats = {row["name"]: row for row in snapshot["heartbeats"]}
    assert "scheduler:test-loop" in beats
    assert beats["scheduler:test-loop"]["stale"] is False
    assert beats["scheduler:test-loop"]["age_seconds"] is not None

    # An old heartbeat renders as stale — the fleet page's dead-loop signal.
    old = (utcnow() - dt.timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    from trusted_router.storage_models import SyntheticProbeSample

    STORE.record_synthetic_probe_sample(
        SyntheticProbeSample(
            id="syn_hb_dead_loop_test",
            probe_type="heartbeat",
            target="scheduler:dead-loop",
            target_url="",
            monitor_region="test-1",
            status="up",
            created_at=old,
        )
    )
    fleet.register_heartbeat_target("scheduler:dead-loop")
    snapshot = asyncio.run(fleet_snapshot(settings))
    beats = {row["name"]: row for row in snapshot["heartbeats"]}
    assert beats["scheduler:dead-loop"]["stale"] is True


def test_heartbeat_read_your_write_masks_stale_analytics_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rollout must not page between the durable write and CH ingestion."""
    from trusted_router.storage_models import SyntheticProbeSample

    settings = _settings()
    target = "scheduler:rollout-race"
    record_heartbeat(target, settings=settings)
    stale = SyntheticProbeSample(
        id="syn_hb_rollout_stale",
        probe_type="heartbeat",
        target=target,
        target_url="",
        monitor_region="test-1",
        status="up",
        created_at=(utcnow() - dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    )
    reads: list[dict[str, object]] = []

    def stale_analytics(self: object, **kwargs: object) -> list[SyntheticProbeSample]:
        reads.append(kwargs)
        return [stale] if kwargs.get("target") == target else []

    monkeypatch.setattr(
        type(STORE.target),
        "synthetic_probe_samples",
        stale_analytics,
    )

    beats = {row["name"]: row for row in fleet._heartbeat_rows()}

    assert beats[target]["stale"] is False
    assert beats[target]["last_beat_at"] != stale.created_at
    assert reads
    assert all(read["target"] and read["limit"] == 1 for read in reads)
    assert all(read["probe_type"] == "heartbeat" for read in reads)


def test_concurrent_heartbeat_calls_write_one_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_type = type(STORE.target)
    original = store_type.record_synthetic_probe_sample

    def slow_record(self: object, sample: object) -> None:
        time.sleep(0.01)
        original(self, sample)  # type: ignore[arg-type]

    monkeypatch.setattr(store_type, "record_synthetic_probe_sample", slow_record)
    settings = _settings()
    target = "job:concurrent-heartbeat"
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _: record_heartbeat(target, settings=settings), range(24)))

    samples = STORE.synthetic_probe_samples(
        target=target,
        probe_type="heartbeat",
        limit=100,
    )
    assert len(samples) == 1


def test_fleet_snapshot_merges_peers_and_ranks_overall() -> None:
    settings = _settings(synthetic_fleet_peers="good=https://good.example,bad=https://bad.example")
    transport = _peer_transport(
        {
            "good.example": {
                "overall_status": "up",
                "summary": {"headline": "All Systems Operational"},
                "components": [{"id": "model_inference", "status": "up"}],
                "monitor_freshness": {"is_stale": False},
                "generated_at": iso_now(),
            },
            "bad.example": httpx.ConnectError("x", request=None),  # type: ignore[arg-type]
        }
    )

    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(transport=transport) as client:
            return await fleet_snapshot(settings, client=client)

    snapshot = asyncio.run(run())
    rows = {row["name"]: row for row in snapshot["deployments"]}  # type: ignore[union-attr,index]
    assert rows["good"]["reachable"] is True
    assert rows["good"]["overall_status"] == "up"
    assert rows["good"]["components"] == {"model_inference": "up"}
    assert rows["bad"]["reachable"] is False
    assert rows["bad"]["overall_status"] == "unreachable"
    # One unreachable deployment pulls the fleet banner all the way down:
    # a cloud you cannot see is a cloud you cannot vouch for.
    assert snapshot["fleet_overall_status"] == "unreachable"


def test_fleet_routes_serve_json_and_html(client: TestClient) -> None:
    record_heartbeat("scheduler:route-test", settings=_settings())
    response = client.get("/fleet.json")
    assert response.status_code == 200
    data = response.json()["data"]
    assert "fleet_overall_status" in data
    assert isinstance(data["deployments"], list)
    names = {row["name"] for row in data["heartbeats"]}
    assert "scheduler:route-test" in names

    page = client.get("/fleet")
    assert page.status_code == 200
    assert "TrustedRouter Fleet" in page.text
    assert "scheduler:route-test" in page.text
    # Ops page: keep it out of search indexes.
    assert 'name="robots" content="noindex"' in page.text


def test_ops_alert_fires_fingerprinted_sentry_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sentry_sdk

    events: list[str] = []
    fingerprints: list[list[str]] = []

    class _Scope:
        def __enter__(self) -> _Scope:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def __setattr__(self, name: str, value: object) -> None:
            if name == "fingerprint":
                fingerprints.append(list(value))  # type: ignore[arg-type]
            object.__setattr__(self, name, value)

        def set_tag(self, key: str, value: str) -> None:
            return None

    monkeypatch.setattr(sentry_sdk, "new_scope", lambda: _Scope())
    monkeypatch.setattr(
        sentry_sdk,
        "capture_message",
        lambda message, level=None: events.append(message),
    )

    assert (
        ops_alert(
            "ALERT settle outbox lost charge authorization_id=auth_x actual_cost_micro=5",
            fingerprint=["settle-outbox", "lost-charge"],
            tags={"authorization_id": "auth_x"},
        )
        is True
    )
    assert events and "lost charge" in events[0]
    assert fingerprints == [["ops-alert", "settle-outbox", "lost-charge"]]


def test_ops_alert_survives_sentry_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import sentry_sdk

    def explode() -> object:
        raise RuntimeError("sentry down")

    monkeypatch.setattr(sentry_sdk, "new_scope", explode)
    assert ops_alert("ALERT test message", fingerprint=["test"], tags=None) is False


def test_peer_samples_stay_out_of_components_and_slo() -> None:
    from trusted_router.storage_models import SyntheticProbeSample
    from trusted_router.synthetic.components import (
        sample_component_ids,
        sample_slo_class_ids,
    )

    for probe_type in ("peer_monitor", "heartbeat"):
        sample = SyntheticProbeSample(
            id=f"syn_{probe_type}_scope",
            probe_type=probe_type,
            target="aws",
            target_url="https://aws.trustedrouter.com/status.json",
            monitor_region="test-1",
            status="down",
        )
        # Policing the watchers must never repaint THIS deployment's banner.
        assert sample_component_ids(sample) == []
        assert sample_slo_class_ids(sample) == []


def test_heartbeats_never_keep_the_freshness_clock_alive() -> None:
    # The masking failure: the probe fleet dies but a background loop keeps
    # heartbeating. monitor_freshness must report STALE — a heartbeat is not
    # the probe fleet reporting.
    from trusted_router.storage_models import SyntheticProbeSample
    from trusted_router.synthetic.status import status_snapshot

    now = utcnow()
    old = (now - dt.timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    fresh = (now - dt.timedelta(seconds=20)).isoformat().replace("+00:00", "Z")
    samples = [
        SyntheticProbeSample(
            id="syn_tls_old",
            probe_type="tls_health",
            target="canonical",
            target_url="https://api.trustedrouter.com/v1",
            monitor_region="test-1",
            status="up",
            created_at=old,
        ),
        SyntheticProbeSample(
            id="syn_hb_fresh",
            probe_type="heartbeat",
            target="scheduler:home-settlement",
            target_url="",
            monitor_region="test-1",
            status="up",
            created_at=fresh,
        ),
        SyntheticProbeSample(
            id="syn_peer_fresh",
            probe_type="peer_monitor",
            target="aws",
            target_url="https://aws.trustedrouter.com/status.json",
            monitor_region="test-1",
            status="up",
            created_at=fresh,
        ),
    ]

    snapshot = status_snapshot(samples, now=now, settings=_settings())

    assert snapshot["monitor_freshness"]["is_stale"] is True
    assert snapshot["monitor_freshness"]["latest_sample_at"] == old


def test_fleet_probe_constants_stay_in_the_ops_set() -> None:
    # OPS_PROBE_TYPES (components.py) is what keeps liveness samples out of
    # the freshness clock; fleet.py's constants must never drift out of it.
    from trusted_router.synthetic.components import OPS_PROBE_TYPES

    assert {fleet.PEER_MONITOR_PROBE, fleet.HEARTBEAT_PROBE} <= OPS_PROBE_TYPES


def test_fleet_json_shape_is_stable() -> None:
    # The fleet feed is a machine surface (future remediation loops read it):
    # lock the top-level keys the same way status.json's shape is locked.
    settings = _settings()
    snapshot = asyncio.run(fleet_snapshot(settings))
    assert set(snapshot) == {
        "generated_at",
        "fleet_overall_status",
        "deployments",
        "remediator",
        "heartbeats",
    }
    json.dumps(snapshot)  # must be JSON-serializable as-is
