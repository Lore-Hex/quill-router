"""Schema v3: component-keyed leases in the shared release journal.

Design: docs/design/component-leases-v3.md. Each rule below has a test, and
tests/cloud_rollout_v3_mutations.py removes each rule in turn and requires
the matching test to fail.
"""
from __future__ import annotations

import copy
import datetime as dt
import email.message
import hashlib
import importlib.util
import io
import json
import ssl
import sys
import threading
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.deploy import cloud_rollout as rollout

API_SERVICE_ANSWERS = rollout.api_service_answers  # kept before the autouse stub
BUILD_OPENER = urllib.request.build_opener  # kept before any test replaces it

QR = "https://github.com/Lore-Hex/quill-router/actions/runs/{}"
QCP = "https://github.com/Lore-Hex/quill-cloud-proxy/actions/runs/{}"
# The coordinator quill-cloud-proxy runs today: tools/cloud-rollout-source.json
# pins commit 11ae458f0f833be38170a4f7044bee6d89f9c63d with this digest.
PINNED_V2_SHA256 = "0f7b668b1f359b4f49c8f468f01f6e20b4949dc01d432b8295858b985c4cf032"


class MemoryStore:
    def __init__(self):
        self.record = None
        self.generation = 0
        self.lock = threading.Lock()
        self.writes = 0

    def read(self):
        with self.lock:
            return copy.deepcopy(self.record), self.generation

    def write(self, record, generation):
        with self.lock:
            if generation != self.generation:
                raise rollout.Conflict()
            self.record = copy.deepcopy(record)
            self.generation += 1
            self.writes += 1


def make_system(version):
    store = MemoryStore()
    health = {c: "a" * 40 for c in rollout.CLOUDS}
    component_health = {(c, k): True for c in rollout.CLOUDS for k in rollout.COMPONENTS}
    probed = []
    clock = [10_000]

    def probe(cloud):
        probed.append(("cloud", cloud))
        if health[cloud] is None:
            raise rollout.Refused("unhealthy")
        return health[cloud]

    def component_probe(cloud, component):
        probed.append((cloud, component))
        if not component_health[(cloud, component)]:
            raise rollout.Refused(f"{cloud}/{component} unhealthy")

    evidence = {}  # cloud -> release SHAs its evidence reads; default follows health

    def releases(cloud):
        if cloud in evidence:
            return list(evidence[cloud])
        return [] if health[cloud] is None else [health[cloud]]

    coordinator = rollout.Coordinator(store, probe, lambda: clock[0], component_probe, releases)
    if version == rollout.V3:
        assert coordinator.upgrade() is True
    return SimpleNamespace(store=store, health=health, component_health=component_health,
                           probed=probed, clock=clock, coordinator=coordinator, evidence=evidence)


@pytest.fixture
def v3():
    return make_system(rollout.V3)


@pytest.fixture
def v2():
    return make_system(rollout.VERSION)


def hold(system, cloud, component, run=1):
    owner = (QCP if component == "gateway" else QR).format(run)
    return system.coordinator.acquire(cloud, owner, component, 3600)


def enqueue(system, cloud, component, run=1, wait=3600):
    owner = (QCP if component == "gateway" else QR).format(run)
    return system.coordinator.enqueue(cloud, owner, component, wait)


def admit(system, ticket):
    return system.coordinator.try_admit(ticket, 3600)


# Same component, same cloud --------------------------------------------------

@pytest.mark.parametrize("cloud", rollout.CLOUDS)
@pytest.mark.parametrize("component", rollout.COMPONENTS)
def test_v3_same_component_on_one_cloud_stays_exclusive(v3, cloud, component):
    hold(v3, cloud, component)
    with pytest.raises(rollout.Busy, match=f"{cloud}/{component} already reserved"):
        hold(v3, cloud, component, run=2)


# Control plane and gateway on gcp ---------------------------------------------

@pytest.mark.parametrize("first,second", [("control-plane", "gateway"), ("gateway", "control-plane")])
def test_v3_gcp_control_plane_and_gateway_hold_leases_at_once(v3, first, second):
    a = hold(v3, "gcp", first)
    b = hold(v3, "gcp", second, run=2)
    assert set(v3.store.record["leases"]) == {f"gcp/{first}", f"gcp/{second}"}
    assert v3.coordinator.check("gcp", a["operation_id"])["component"] == first
    assert v3.coordinator.check("gcp", b["operation_id"])["component"] == second


@pytest.mark.parametrize("held,requested", [
    ("public-surface", "control-plane"), ("public-surface", "gateway"),
    ("control-plane", "public-surface"), ("gateway", "public-surface"),
])
def test_v3_public_surface_is_exclusive_with_both_components(v3, held, requested):
    hold(v3, "gcp", held)
    with pytest.raises(rollout.Busy, match=f"gcp/{held} already reserved"):
        hold(v3, "gcp", requested, run=2)


@pytest.mark.parametrize("cloud", ["aws", "azure"])
@pytest.mark.parametrize("held,requested", [("control-plane", "gateway"), ("gateway", "control-plane")])
def test_v3_clouds_without_attributable_gates_stay_one_component_at_a_time(v3, cloud, held, requested):
    assert cloud not in rollout.CONCURRENT_CLOUDS
    hold(v3, cloud, held)
    with pytest.raises(rollout.Busy, match="already reserved"):
        hold(v3, cloud, requested, run=2)


def test_v3_a_blocked_lease_stops_every_other_component_on_its_cloud(v3):
    lease = hold(v3, "gcp", "control-plane")
    v3.coordinator.release("gcp", lease["operation_id"], False)
    with pytest.raises(rollout.Busy, match="gcp/control-plane is blocked"):
        hold(v3, "gcp", "gateway", run=2)
    # Other clouds are unaffected beyond the cap.
    hold(v3, "aws", "gateway", run=3)


def test_v3_blocked_lease_is_kept_and_needs_recovery(v3):
    lease = hold(v3, "gcp", "gateway")
    v3.coordinator.release("gcp", lease["operation_id"], False)
    assert v3.store.record["leases"]["gcp/gateway"]["state"] == "blocked"
    with pytest.raises(rollout.Refused, match="recovery"):
        v3.coordinator.release("gcp", lease["operation_id"], True)
    with pytest.raises(rollout.Refused, match="blocked or expired"):
        v3.coordinator.check("gcp", lease["operation_id"])
    v3.coordinator.recover("gcp", lease["operation_id"], lambda _: True)
    assert v3.store.record["leases"] == {}


# The two-cloud cap counts clouds -----------------------------------------------

def test_v3_cap_counts_distinct_clouds_not_leases(v3):
    hold(v3, "gcp", "control-plane")
    hold(v3, "aws", "control-plane", run=2)
    hold(v3, "gcp", "gateway", run=3)  # gcp already counted
    assert len(v3.store.record["leases"]) == 3
    with pytest.raises(rollout.Busy, match="third is protected"):
        hold(v3, "azure", "control-plane", run=4)
    with pytest.raises(rollout.Busy, match="third is protected"):
        hold(v3, "azure", "gateway", run=5)
    assert v3.store.record["holdback"]["cloud"] == "azure"


def test_v3_holdback_is_a_cloud_with_no_lease_and_is_probed_whole(v3):
    hold(v3, "gcp", "control-plane")
    hold(v3, "gcp", "gateway", run=2)
    holdback = v3.store.record["holdback"]["cloud"]
    assert holdback not in rollout.leased_clouds(v3.store.record)
    assert holdback != "gcp"
    assert ("cloud", holdback) in v3.probed  # both services, not one component


def test_v3_a_leased_cloud_never_becomes_the_holdback(v3):
    hold(v3, "gcp", "control-plane")
    assert v3.store.record["holdback"]["cloud"] == "aws"
    v3.health["azure"] = None  # leasing aws leaves only azure to protect
    with pytest.raises(rollout.Refused, match="no unchanged healthy cloud"):
        hold(v3, "aws", "gateway", run=2)
    assert set(v3.store.record["leases"]) == {"gcp/control-plane"}


@pytest.mark.parametrize("version", [rollout.VERSION, rollout.V3])
def test_leasing_a_changed_holdback_is_refused(version):
    """Follow-up to #2568 review: v2 had the same gap."""
    system = make_system(version)
    hold(system, "gcp", "control-plane")
    assert system.store.record["holdback"]["cloud"] == "aws"
    system.health["aws"] = "b" * 40  # changed outside any lease
    before = (copy.deepcopy(system.store.record), system.store.generation)
    with pytest.raises(rollout.Refused, match="refusing to lease over it"):
        hold(system, "aws", "gateway", run=2)
    assert (system.store.record, system.store.generation) == before


@pytest.mark.parametrize("version", [rollout.VERSION, rollout.V3])
def test_a_changed_holdback_with_failing_services_is_still_refused(version):
    """Review of #2573: a down gateway must not hide a readable release change."""
    system = make_system(version)
    hold(system, "gcp", "control-plane")
    system.health["aws"] = None  # probe_cloud fails: the gateway is down
    system.evidence["aws"] = ["b" * 40]  # but the release evidence reads, and changed
    before = (copy.deepcopy(system.store.record), system.store.generation)
    with pytest.raises(rollout.Refused, match="refusing to lease over it"):
        hold(system, "aws", "gateway", run=2)
    assert (system.store.record, system.store.generation) == before


@pytest.mark.parametrize("version", [rollout.VERSION, rollout.V3])
def test_a_down_holdback_may_still_be_leased_to_repair_it(version):
    system = make_system(version)
    hold(system, "gcp", "control-plane")
    system.health["aws"] = None
    lease = hold(system, "aws", "gateway", run=2)
    assert lease["cloud"] == "aws"
    assert system.store.record["holdback"]["cloud"] == "azure"


def test_v3_leasing_the_holdback_moves_protection_to_the_free_cloud(v3):
    hold(v3, "gcp", "control-plane")
    hold(v3, "aws", "gateway", run=2)
    assert v3.store.record["holdback"]["cloud"] == "azure"


@pytest.mark.parametrize("change", ["changed", "unhealthy"])
def test_v3_a_sibling_grant_never_launders_a_changed_holdback(v3, change):
    """Review of #2568: a second gcp grant must not move protection elsewhere."""
    cp = hold(v3, "gcp", "control-plane")
    assert v3.store.record["holdback"]["cloud"] == "aws"
    v3.health["aws"] = "b" * 40 if change == "changed" else None  # outside any lease
    assert v3.health["azure"] is not None  # a healthy cloud that could stand in
    with pytest.raises(rollout.Refused):
        v3.coordinator.check("gcp", cp["operation_id"])
    before = (copy.deepcopy(v3.store.record), v3.store.generation)
    with pytest.raises(rollout.Refused):
        hold(v3, "gcp", "gateway", run=2)
    assert (v3.store.record, v3.store.generation) == before
    with pytest.raises(rollout.Refused):
        v3.coordinator.check("gcp", cp["operation_id"])


def test_v2_grant_with_a_changed_holdback_still_refuses(v2):
    hold(v2, "gcp", "control-plane")
    v2.health["aws"] = "b" * 40
    with pytest.raises(rollout.Refused):
        hold(v2, "azure", "gateway", run=2)


def test_v3_second_component_rechecks_the_unchanged_holdback(v3):
    hold(v3, "gcp", "control-plane")
    holdback = v3.store.record["holdback"]["cloud"]
    v3.health[holdback] = "b" * 40  # changed outside any lease
    for cloud in set(rollout.CLOUDS) - {"gcp", holdback}:
        v3.health[cloud] = None  # and no other cloud can stand in
    before = v3.store.generation
    with pytest.raises(rollout.Refused, match="protected cloud changed"):
        hold(v3, "gcp", "gateway", run=2)
    assert v3.store.generation == before


# Release and recover probe only their own component (E5) ----------------------

@pytest.mark.parametrize("releasing,broken", [("control-plane", "gateway"), ("gateway", "control-plane")])
def test_v3_release_probes_only_the_releasing_component(v3, releasing, broken):
    lease = hold(v3, "gcp", releasing)
    other = hold(v3, "gcp", broken, run=2)
    v3.component_health[("gcp", broken)] = False
    v3.health["gcp"] = None  # probe_cloud would refuse
    v3.coordinator.release("gcp", lease["operation_id"], True)
    assert f"gcp/{releasing}" not in v3.store.record["leases"]
    assert f"gcp/{broken}" in v3.store.record["leases"]
    assert ("gcp", releasing) in v3.probed and ("gcp", broken) not in v3.probed
    assert other["operation_id"] == v3.store.record["leases"][f"gcp/{broken}"]["operation_id"]


@pytest.mark.parametrize("component", ["control-plane", "gateway"])
def test_v3_release_refused_while_its_own_component_is_unhealthy(v3, component):
    lease = hold(v3, "gcp", component)
    v3.component_health[("gcp", component)] = False
    with pytest.raises(rollout.Refused, match="unhealthy"):
        v3.coordinator.release("gcp", lease["operation_id"], True)
    assert f"gcp/{component}" in v3.store.record["leases"]


def test_v3_recover_probes_only_the_recovered_component(v3):
    lease = hold(v3, "gcp", "gateway")
    hold(v3, "gcp", "control-plane", run=2)
    v3.coordinator.release("gcp", lease["operation_id"], False)
    v3.component_health[("gcp", "control-plane")] = False
    v3.coordinator.recover("gcp", lease["operation_id"], lambda _: True)
    assert set(v3.store.record["leases"]) == {"gcp/control-plane"}
    with pytest.raises(rollout.Refused, match="still be mutating"):
        other = v3.store.record["leases"]["gcp/control-plane"]
        v3.coordinator.recover("gcp", other["operation_id"], lambda _: False)


PLANE_JSON = {"release": "c" * 40, "api_base_url": "https://api.trustedrouter.com/v1"}


@pytest.fixture(autouse=True)
def no_live_api(monkeypatch):
    """Tests never reach a real plane; a test that needs the API probe sets it."""
    def refuse(plane):
        raise AssertionError(f"unexpected live API probe of {plane}")
    monkeypatch.setattr(rollout, "api_service_answers", refuse)


@pytest.mark.parametrize("component,expected", [
    ("gateway", ["https://api.trustedrouter.com/health"]),
    ("control-plane", ["api https://trustedrouter.com",
                       "https://trustedrouter.com/trust/control-plane.json",
                       "https://trustedrouter.com/health",
                       "https://trustedrouter.com/trust/control-plane.json"]),
    ("public-surface", ["https://trustedrouter.com/trust/control-plane.json",
                        "https://trustedrouter.com/health",
                        "https://trustedrouter.com/trust/control-plane.json"]),
])
def test_probe_component_reads_only_that_components_service(monkeypatch, component, expected):
    fetched = []

    def fetch(url):
        fetched.append(url)
        return PLANE_JSON if url.endswith("control-plane.json") else {"status": "ok"}

    monkeypatch.setattr(rollout, "fetch", fetch)
    monkeypatch.setattr(rollout, "api_service_answers", lambda plane: fetched.append(f"api {plane}"))
    rollout.probe_component("gcp", component)
    assert fetched == expected


@pytest.mark.parametrize("component,broken", [
    ("gateway", "https://api.trustedrouter.com/health"),
    ("control-plane", "https://trustedrouter.com/health"),
])
def test_probe_component_refuses_its_own_unhealthy_service(monkeypatch, component, broken):
    def fetch(url):
        if url == broken:
            return {"status": "degraded"}
        return PLANE_JSON if url.endswith("control-plane.json") else {"status": "ok"}

    monkeypatch.setattr(rollout, "fetch", fetch)
    monkeypatch.setattr(rollout, "api_service_answers", lambda plane: None)
    with pytest.raises(rollout.Refused, match=f"gcp/{component} is not verified healthy"):
        rollout.probe_component("gcp", component)


def test_probe_component_refuses_when_the_api_service_does_not_answer(monkeypatch):
    def down(plane):
        raise rollout.Refused("control-plane API did not answer")

    monkeypatch.setattr(rollout, "api_service_answers", down)
    monkeypatch.setattr(rollout, "fetch", lambda url: PLANE_JSON if url.endswith(".json") else {"status": "ok"})
    with pytest.raises(rollout.Refused, match="gcp/control-plane is not verified healthy"):
        rollout.probe_component("gcp", "control-plane")
    rollout.probe_component("gcp", "public-surface")  # the public service needs no API answer


class FakeOpener:
    def __init__(self, outcomes):
        self.outcomes, self.urls = list(outcomes), []

    def open(self, request, timeout):
        self.urls.append(request.full_url)
        code, body, *extra = self.outcomes.pop(0)  # (status, body), then optionally headers
        if code == 200:
            return io.BytesIO(body)
        headers = email.message.Message()
        for name, value in (extra[0] if extra else {}).items():
            headers[name] = value
        raise urllib.error.HTTPError(request.full_url, code, "x", headers, io.BytesIO(body))


ROUTER_401 = json.dumps({"error": {"code": 401, "message": "Invalid internal service token",
                                   "type": "unauthorized", "source": "router"}}).encode()
# What the control plane's own source limiter answers once its bucket is spent
# (_rate_limit_request in src/trusted_router/middleware.py), byte for byte.
# test_the_api_probe_passes_both_answers_the_application_itself_sends checks
# that the application still sends exactly this.
RATE_LIMITED = b'{"error":{"code":429,"message":"Rate limit exceeded","type":"rate_limited","source":"router"}}'
# Its headers as an azure replica sends them (240 a window), halfway through
# the 60 s window. That test gets the same with its own limit of 2.
LIMITER_HEADERS = {"Content-Type": "application/json", "Retry-After": "30", "X-RateLimit-Limit": "240",
                   "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "2026-08-19T12:01:00Z"}


def real_api_probe(monkeypatch, outcomes):
    opener = FakeOpener(outcomes)
    monkeypatch.setattr(rollout.urllib.request, "build_opener", lambda *a: opener)
    monkeypatch.setattr(rollout.time, "sleep", lambda s: None)
    return opener


@pytest.mark.parametrize("outcomes,ok", [
    ([(401, ROUTER_401)], True),
    ([(503, b""), (401, ROUTER_401)], True),
    ([(401, b'{"error": {"code": 401, "source": "edge"}}')], False),
    ([(401, b"<html>")], False),
    ([(404, b"{}")], False),
    ([(200, b"{}")], False),
    ([(503, b""), (503, b""), (503, b"")], False),
])
def test_api_service_answers_only_with_its_own_401(monkeypatch, outcomes, ok):
    opener = real_api_probe(monkeypatch, outcomes)
    probe = API_SERVICE_ANSWERS
    if ok:
        probe("https://trustedrouter.com")
    else:
        with pytest.raises(rollout.Refused):
            probe("https://trustedrouter.com")
    assert all(u.startswith("https://trustedrouter.com/v1/internal/synthetic/health?rollout_check=")
               for u in opener.urls)


def limiter_error(**fields):
    """The limiter's error object with `fields` changed (None drops one), compact like the app's."""
    error = {"code": 429, "message": "Rate limit exceeded", "type": "rate_limited", "source": "router"}
    error = {k: v for k, v in {**error, **fields}.items() if v is not None}
    return json.dumps({"error": error}, separators=(",", ":")).encode()


NOT_THE_LIMITER = {
    # What a proxy or load balancer in front of the application answers.
    "plain text 429": b"Rate exceeded.",
    "html 429": b"<html><head><title>429 Too Many Requests</title></head><body>nginx</body></html>",
    "empty 429": b"",
    "json 429 naming no error object": b'{"error":"rate_limited"}',
    # The limiter's object with one field off.
    "no source": limiter_error(source=None),
    "another source": limiter_error(source="edge"),
    "provider source": limiter_error(source="provider"),
    "another type": limiter_error(type="provider_rate_limited"),
    "another code": limiter_error(code=503),
    "a float code": limiter_error(code=429.0),
    "no message": limiter_error(message=None),
    # Another 429 of the application's own, which this route answers only past auth.
    "another route's 429": limiter_error(message="Synthetic operation is already in progress"),
    "an extra field": limiter_error(retry_after=30),
    # The same object in other bytes: nothing between the coordinator and the
    # application rewrites a body, so a rewritten one did not come from it.
    "reformatted": json.dumps(json.loads(RATE_LIMITED)).encode(),
    "trailing newline": RATE_LIMITED + b"\n",
    "wrapped": b'{"data":' + RATE_LIMITED + b"}",
}


@pytest.mark.parametrize("outcomes,ok", [
    # The limiter's own 429 passes at once, with or without its headers: no
    # retry spends more of a bucket that refills only when its window ends.
    pytest.param([(429, RATE_LIMITED)], True, id="the limiter"),
    pytest.param([(429, RATE_LIMITED, LIMITER_HEADERS)], True, id="the limiter, with its headers"),
    pytest.param([(503, b""), (429, RATE_LIMITED)], True, id="a 503, then the limiter"),
    pytest.param([(429, b"Rate exceeded."), (429, RATE_LIMITED)], True, id="a proxy 429, then the limiter"),
    # Any other 429 is retried like a 503, and refused on the third.
    *(pytest.param([(429, body)] * 3, False, id=name) for name, body in NOT_THE_LIMITER.items()),
    # The limiter's body at any other status is not the limiter.
    pytest.param([(401, RATE_LIMITED)], False, id="the limiter's body at 401"),
    pytest.param([(503, RATE_LIMITED)] * 3, False, id="the limiter's body at 503"),
    pytest.param([(403, RATE_LIMITED)], False, id="the limiter's body at 403"),
    pytest.param([(200, RATE_LIMITED)], False, id="the limiter's body at 200"),
])
def test_api_service_answers_with_its_own_limiters_429_and_no_other(monkeypatch, outcomes, ok):
    """The #2577 review: with the shared unauthenticated bucket spent, the
    probe got the limiter's 429 three times and refused, so `release` and
    `recover` of a control-plane lease failed on traffic from anyone."""
    opener = real_api_probe(monkeypatch, outcomes)
    if ok:
        API_SERVICE_ANSWERS("https://aws.trustedrouter.com")
        assert opener.outcomes == []  # and asked nothing after the answer it passed
    else:
        with pytest.raises(rollout.Refused, match="control-plane API answered"):
            API_SERVICE_ANSWERS("https://aws.trustedrouter.com")
        assert opener.outcomes == []
    assert all(u.startswith("https://aws.trustedrouter.com/v1/internal/synthetic/health?rollout_check=")
               for u in opener.urls)


def test_the_api_probe_passes_both_answers_the_application_itself_sends(monkeypatch):
    """Pins the application's side. The 401 and the 429 below are what the
    real application answers the probe's own request, not fixtures: first its
    internal auth, then, once any unauthenticated internal request (here one
    to a route that does not exist) has spent the shared bucket, its source
    limiter, which runs before internal auth. Each passes the real probe."""
    from fastapi.testclient import TestClient

    from trusted_router import storage_rate_limits
    from trusted_router.config import Settings
    from trusted_router.main import create_app

    # Mid-window, so the bucket cannot roll over between the requests.
    monkeypatch.setattr(storage_rate_limits, "utcnow", lambda: dt.datetime(2026, 8, 19, 12, 0, 30, tzinfo=dt.UTC))
    settings = Settings(environment="test", rate_limit_enabled=True, rate_limit_ip_per_window=2,
                        rate_limit_client_ip_mode="untrusted", observer_internal_token="o" * 40)
    client = TestClient(create_app(settings, init_observability=False))
    probe = {"Accept": "application/json", "Cache-Control": "no-cache",
             "User-Agent": "TrustedRouter-Release-Coordinator/2"}
    query = {"rollout_check": "0" * 32}

    refused = client.get(rollout.API_PROBE_PATH, params=query, headers=probe)
    assert client.get("/v1/internal/no-such-route", params=query, headers=probe).status_code == 404
    limited = client.get(rollout.API_PROBE_PATH, params=query, headers=probe)

    assert refused.status_code == 401
    assert json.loads(refused.content) == json.loads(ROUTER_401)
    assert limited.status_code == 429
    assert limited.content == RATE_LIMITED == rollout.LIMITER_429
    assert {name: limited.headers[name] for name in LIMITER_HEADERS} == {
        **LIMITER_HEADERS, "X-RateLimit-Limit": "2"}
    for answer in (refused, limited):
        opener = real_api_probe(monkeypatch, [(answer.status_code, answer.content, dict(answer.headers))])
        API_SERVICE_ANSWERS("https://azure.trustedrouter.com")
        assert opener.outcomes == []


def test_probe_component_requires_stable_release_evidence(monkeypatch):
    answers = iter([PLANE_JSON, {"status": "ok"}, {**PLANE_JSON, "release": "d" * 40}])
    monkeypatch.setattr(rollout, "fetch", lambda url: next(answers))
    monkeypatch.setattr(rollout, "api_service_answers", lambda plane: None)
    with pytest.raises(rollout.Refused, match="not verified healthy"):
        rollout.probe_component("gcp", "control-plane")


def fake_planes(monkeypatch, *, aws_release="a" * 40, aws_gateway_ok=True, aws_evidence_ok=True):
    """The real probe_cloud and observed_releases against a fake fetch."""
    def fetch(url):
        for cloud, (plane, gateway) in rollout.PLANES.items():
            if url == f"{plane}/trust/control-plane.json":
                if cloud == "aws" and not aws_evidence_ok:
                    raise OSError("unreachable")
                return {"release": aws_release if cloud == "aws" else "a" * 40, "api_base_url": f"{gateway}/v1"}
            if url == f"{plane}/health":
                return {"status": "ok"}
            if url == f"{gateway}/health":
                return {"status": "ok" if cloud != "aws" or aws_gateway_ok else "down"}
        raise AssertionError(url)

    monkeypatch.setattr(rollout, "fetch", fetch)


@pytest.mark.parametrize("version", [rollout.VERSION, rollout.V3])
def test_default_coordinator_reads_the_evidence_past_failing_services(monkeypatch, version):
    """The repro from the #2573 review, through the real default probes."""
    fake_planes(monkeypatch)
    store = MemoryStore()
    coordinator = rollout.Coordinator(store, None, lambda: 10_000, lambda c, k: None)
    if version == rollout.V3:
        coordinator.upgrade()
    cp = coordinator.acquire("gcp", QR.format(1), "control-plane", 3600)
    assert store.record["holdback"]["cloud"] == "aws"
    fake_planes(monkeypatch, aws_release="b" * 40, aws_gateway_ok=False)
    with pytest.raises(rollout.Refused, match="refusing to lease over it"):
        coordinator.acquire("aws", QR.format(3), "control-plane", 3600)
    with pytest.raises(rollout.Refused):
        coordinator.check("gcp", cp["operation_id"])


@pytest.mark.parametrize("version", [rollout.VERSION, rollout.V3])
def test_default_coordinator_leases_a_holdback_whose_evidence_is_unreadable(monkeypatch, version):
    fake_planes(monkeypatch)
    store = MemoryStore()
    coordinator = rollout.Coordinator(store, None, lambda: 10_000, lambda c, k: None)
    if version == rollout.V3:
        coordinator.upgrade()
    coordinator.acquire("gcp", QR.format(1), "control-plane", 3600)
    fake_planes(monkeypatch, aws_gateway_ok=False, aws_evidence_ok=False)
    lease = coordinator.acquire("aws", QR.format(3), "control-plane", 3600)
    assert lease["cloud"] == "aws"
    assert store.record["holdback"]["cloud"] == "azure"


@pytest.mark.parametrize("answers,expected", [
    ([{"release": "c" * 40}, {"release": "c" * 40}], ["c" * 40, "c" * 40]),
    ([{"release": "c" * 40}, {"release": "d" * 40}], ["c" * 40, "d" * 40]),
    # No document came back: the read adds nothing.
    ([OSError("down"), {"release": "d" * 40}], ["d" * 40]),
    ([OSError("down"), rollout.Redirected("redirect")], []),
    ([ssl.SSLCertVerificationError("bad certificate"), OSError("down")], []),  # also a ValueError
    # An answer without a valid release is evidence, and never the protected release.
    ([{"release": "not-a-sha"}, {}], ["", ""]),
    ([{"release": "c" * 40}, {"release": "local"}], ["c" * 40, ""]),
    ([{"release": None}, {"release": "C" * 40}], ["", ""]),
    ([{"release": ["c" * 40]}, {"release": 1234567}], ["", ""]),
    ([OSError("down"), ValueError("bad json")], [""]),
    ([rollout.Refused("invalid cloud health response"),
      rollout.Refused("oversized cloud health response")], ["", ""]),
])
def test_observed_releases_reads_the_evidence_twice_without_health(monkeypatch, answers, expected):
    calls = []

    def fetch(url):
        calls.append(url)
        answer = answers[len(calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(rollout, "fetch", fetch)
    assert rollout.observed_releases("aws") == expected
    assert calls == ["https://aws.trustedrouter.com/trust/control-plane.json"] * 2


class CannedHTTPS(urllib.request.HTTPSHandler):
    """Canned answers under the real opener: urllib's own error and redirect
    handling, NoRedirect and fetch all run as they do against a live cloud."""

    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def https_open(self, request):
        outcome = self.answer(request.full_url.split("?")[0])
        if isinstance(outcome, Exception):
            raise outcome
        # (status, body), then optionally a redirect's Location or a dict of headers.
        status, body, *extra = outcome
        headers = email.message.Message()
        for name, value in (extra[0] if extra and isinstance(extra[0], dict)
                            else {"Location": extra[0]} if extra else {}).items():
            headers[name] = value
        response = urllib.response.addinfourl(io.BytesIO(body), headers, request.full_url, status)
        response.msg = "canned"
        return response


def evidence_document(cloud, **fields):
    document = {"release": "a" * 40, "api_base_url": f"{rollout.PLANES[cloud][1]}/v1", **fields}
    return json.dumps(document).encode()


def canned_planes(monkeypatch, aws_evidence=None, answers=None):
    """Every plane healthy at release a; `aws_evidence` is what aws's evidence
    URL answers (a list answers one request after another), and `answers`
    replaces what any other URL answers.

    The API service answers its own 401 too, so the real probe_component runs
    over the same canned HTTPS as probe_cloud and observed_releases.
    """
    served = []

    def answer(url):
        served.append(url)
        if answers and url in answers:
            return answers[url]
        for cloud, (plane, gateway) in rollout.PLANES.items():
            if url == f"{plane}/trust/control-plane.json":
                if cloud == "aws" and aws_evidence is not None:
                    return aws_evidence.pop(0) if isinstance(aws_evidence, list) else aws_evidence
                return 200, evidence_document(cloud)
            if url in (f"{plane}/health", f"{gateway}/health"):
                return 200, b'{"status": "ok"}'
            if url == f"{plane}{rollout.API_PROBE_PATH}":
                return 401, ROUTER_401
        raise AssertionError(url)

    monkeypatch.setattr(rollout.urllib.request, "build_opener",
                        lambda *handlers: BUILD_OPENER(CannedHTTPS(answer), *handlers))
    monkeypatch.setattr(rollout.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(rollout, "api_service_answers", API_SERVICE_ANSWERS)  # canned, so never live
    return served


def default_coordinator_protecting_aws(monkeypatch, version):
    """Every default probe (probe_cloud, probe_component, observed_releases)
    over canned HTTPS, with gcp leased and aws the holdback."""
    canned_planes(monkeypatch)
    store = MemoryStore()
    coordinator = rollout.Coordinator(store, None, lambda: 10_000)
    if version == rollout.V3:
        coordinator.upgrade()
    lease = coordinator.acquire("gcp", QR.format(1), "control-plane", 3600)
    assert store.record["holdback"] == {"cloud": "aws", "release": "a" * 40, "checked_at": 10_000}
    return store, coordinator, lease


AWS_EVIDENCE = "https://aws.trustedrouter.com/trust/control-plane.json"
# A 2xx answer at the evidence URL that is not the protected release's document.
ANSWERED_WITHOUT_ITS_RELEASE = {
    "release local": (200, evidence_document("aws", release="local")),  # TR_RELEASE dropped
    "release eu": (200, evidence_document("aws", release="eu")),  # an old revision
    "release azure": (200, evidence_document("aws", release="azure")),
    "release null": (200, evidence_document("aws", release=None)),
    "release missing": (200, b'{"api_base_url": "https://api-aws.trustedrouter.com/v1"}'),
    "release uppercase": (200, evidence_document("aws", release="A" * 40)),
    "release too short": (200, evidence_document("aws", release="a" * 6)),
    "release a number": (200, evidence_document("aws", release=1234567)),
    "release a list": (200, evidence_document("aws", release=["a" * 40])),
    "empty object": (200, b"{}"),
    "body a list": (200, b'["' + b"a" * 40 + b'"]'),
    "body a string": (200, b'"' + b"a" * 40 + b'"'),
    "body null": (200, b"null"),
    "body html": (200, b"<!doctype html><title>ok</title>"),
    "body empty": (200, b""),
    "body not utf-8": (200, b"\xff\xfe{}"),
    "body truncated json": (200, evidence_document("aws")[:-1]),
    # The protected release itself, padded past the size limit.
    "body oversize": (200, evidence_document("aws") + b" " * rollout.MAX_BYTES),
    "204 no content": (204, b""),
}
# The control plane's own source limiter (RATE_LIMITED above). Retry-After is
# what is left of its 60 s window: usually more than the 5 s fetch will wait
# for, so fetch gives up at once, and now and then less, so fetch retries.
LIMITED = (429, RATE_LIMITED, {"Retry-After": "37", "X-RateLimit-Limit": "240", "X-RateLimit-Remaining": "0",
                               "X-RateLimit-Reset": "2026-11-13T18:01:00Z"})
# No document was served: the cloud is down, and may be leased to repair it.
# Three kinds, and tests/cloud_rollout_v3_mutations.py turns each kind alone
# into an answer: only that kind's cases below can catch it.
TRANSPORT_FAILURES = {
    "unreachable": urllib.error.URLError("connection refused"),
    "timeout": TimeoutError("timed out"),
    "bad certificate": urllib.error.URLError(ssl.SSLCertVerificationError("certificate has expired")),
}
HTTP_ERROR_STATUSES = {
    "500": (500, b"internal error"),
    "503 on every attempt": (503, b""),
    "404": (404, b'{"detail": "Not Found"}'),
    "404 with another release": (404, evidence_document("aws", release="b" * 40)),
    "403": (403, b"forbidden"),
    "401": (401, b'{"error": {"code": 401, "message": "Unauthorized", "type": "unauthorized"}}'),
    "401 with another release": (401, evidence_document("aws", release="b" * 40)),
    # fetch retries a 429 that names no wait, or a wait of at most 5 s, and
    # gives up at once on a longer one. Either way no document was served.
    "429 on every attempt": (429, RATE_LIMITED),
    "429 asking for 1 s on every attempt": (429, RATE_LIMITED, {"Retry-After": "1"}),
    "429 asking for 37 s": LIMITED,
    "429 with another release": (429, evidence_document("aws", release="b" * 40), {"Retry-After": "37"}),
    # A Retry-After int() cannot convert gives up at once like any long one.
    # Before the #2577 review's fix it raised ValueError, which read as an
    # answer without a release and refused the grant.
    "429 asking for 4301 digits of seconds": (429, RATE_LIMITED, {"Retry-After": "9" * 4301}),
    "429 asking for 1 s in 4301 digits": (429, RATE_LIMITED, {"Retry-After": "0" * 4300 + "1"}),
    "429 asking for 2 s in a superscript digit": (429, RATE_LIMITED, {"Retry-After": "\u00b2"}),
    "503 asking for 3 s in a superscript digit": (503, b"", {"Retry-After": "\u00b3"}),
}
REDIRECTS = {
    "302 redirect": (302, b"", "https://trustedrouter.com/trust/control-plane.json"),
    "307 redirect": (307, b"", "https://trustedrouter.com/trust/control-plane.json"),
    "301 to a login page": (301, b"", "https://example.com/login"),
}
SERVED_NO_DOCUMENT = {**TRANSPORT_FAILURES, **HTTP_ERROR_STATUSES, **REDIRECTS}
# How many requests one evidence read makes for each answer: fetch tries three
# times after a transport failure or a retryable status, and once otherwise.
ATTEMPTS_PER_READ = {name: 1 for name in SERVED_NO_DOCUMENT} | {
    name: 3 for name in (*TRANSPORT_FAILURES, "503 on every attempt", "429 on every attempt",
                         "429 asking for 1 s on every attempt")}


@pytest.mark.parametrize("version", [rollout.VERSION, rollout.V3])
@pytest.mark.parametrize("answer", ANSWERED_WITHOUT_ITS_RELEASE.values(), ids=ANSWERED_WITHOUT_ITS_RELEASE)
def test_default_coordinator_refuses_a_holdback_answering_without_its_release(monkeypatch, version, answer):
    """Round 4 of the #2573 review: with its plane and gateway healthy, a
    holdback that answered 200 without a valid release was leased over."""
    store, coordinator, lease = default_coordinator_protecting_aws(monkeypatch, version)
    served = canned_planes(monkeypatch, aws_evidence=answer)
    before = (copy.deepcopy(store.record), store.generation)
    with pytest.raises(rollout.Refused, match="refusing to lease over it"):
        coordinator.acquire("aws", QR.format(3), "control-plane", 3600)
    assert (store.record, store.generation) == before
    assert served == [AWS_EVIDENCE] * 2  # decided on the evidence alone, read twice
    # Not a deadlock, provided the lease that relies on aws can itself be
    # released: the refusal lasts only while that lease exists. The release
    # runs the real release probe (gcp's own services, never aws), and once
    # the lease is gone aws can be leased and repaired. What happens when
    # that lease is blocked instead is in the frozen-journal tests below.
    del served[:]
    coordinator.release("gcp", lease["operation_id"], True)
    assert served == (GCP_CONTROL_PLANE_PROBE if version == rollout.V3 else GCP_CLOUD_PROBE)
    assert store.record["holdback"] is None
    repair = coordinator.acquire("aws", QR.format(3), "control-plane", 3600)
    assert repair["cloud"] == "aws"
    assert store.record["holdback"]["cloud"] in {"gcp", "azure"}


@pytest.mark.parametrize("version", [rollout.VERSION, rollout.V3])
@pytest.mark.parametrize("name", SERVED_NO_DOCUMENT)
def test_default_coordinator_leases_a_holdback_that_served_no_document(monkeypatch, version, name):
    store, coordinator, _ = default_coordinator_protecting_aws(monkeypatch, version)
    served = canned_planes(monkeypatch, aws_evidence=SERVED_NO_DOCUMENT[name])
    lease = coordinator.acquire("aws", QR.format(3), "control-plane", 3600)
    assert lease["cloud"] == "aws"
    assert store.record["holdback"]["cloud"] == "azure"
    # aws was asked for its evidence twice and for nothing else: it is being
    # leased, so its health is not what admits it.
    assert [url for url in served if "aws" in url] == [AWS_EVIDENCE] * (2 * ATTEMPTS_PER_READ[name])


# What a release or recovery of the gcp control-plane lease reads: under v3 the
# component's own services (rule 9), under v2 the whole cloud. Never the holdback.
GCP_CONTROL_PLANE_PROBE = ["https://trustedrouter.com/v1/internal/synthetic/health",
                           "https://trustedrouter.com/trust/control-plane.json",
                           "https://trustedrouter.com/health",
                           "https://trustedrouter.com/trust/control-plane.json"]
GCP_CLOUD_PROBE = ["https://trustedrouter.com/trust/control-plane.json",
                   "https://trustedrouter.com/health",
                   "https://api.trustedrouter.com/health",
                   "https://trustedrouter.com/trust/control-plane.json"]
# aws deployed outside any lease: healthy, with a valid release that is not the protected one.
REDEPLOYED = (200, evidence_document("aws", release="b" * 40))


def frozen_by_a_changed_holdback(monkeypatch, version):
    """Rule 7's refusal as production reaches it, through the default probes.

    gcp is leased and aws is its holdback when aws changes outside any lease.
    The gcp rollout's next assert reads aws and fails, and its deploy records
    the failure (deploy_mutex_finish), which leaves the gcp lease blocked.
    """
    store, coordinator, lease = default_coordinator_protecting_aws(monkeypatch, version)
    canned_planes(monkeypatch, aws_evidence=REDEPLOYED)
    with pytest.raises(rollout.Refused, match="protected cloud release changed"):
        coordinator.check("gcp", lease["operation_id"])
    coordinator.release("gcp", lease["operation_id"], False)
    assert [held["state"] for held in store.record["leases"].values()] == ["blocked"]
    assert store.record["holdback"]["cloud"] == "aws"
    return store, coordinator, lease


def admissions(coordinator):
    """Which clouds a new rollout is admitted on now, tried on a copy of the journal."""
    admitted, record, generation = set(), copy.deepcopy(coordinator.store.record), coordinator.store.generation
    for cloud in rollout.CLOUDS:
        for component in ("control-plane", "gateway"):
            try:
                coordinator.acquire(cloud, (QCP if component == "gateway" else QR).format(9), component, 3600)
                admitted.add(cloud)
            except rollout.Refused:
                pass
            coordinator.store.record, coordinator.store.generation = copy.deepcopy(record), generation
    return admitted


@pytest.mark.parametrize("version", [rollout.VERSION, rollout.V3])
def test_a_changed_holdback_blocks_the_lease_relying_on_it_and_then_nothing_is_admitted(monkeypatch, version):
    store, coordinator, lease = frozen_by_a_changed_holdback(monkeypatch, version)
    frozen = (copy.deepcopy(store.record), store.generation)
    with pytest.raises(rollout.Busy):  # rule 5: gcp is blocked
        coordinator.acquire("gcp", QCP.format(3), "gateway", 3600)
    with pytest.raises(rollout.Refused, match="refusing to lease over it"):  # rule 7: aws changed
        coordinator.acquire("aws", QR.format(3), "control-plane", 3600)
    # azure: every grant needs the holdback, aws, healthy and unchanged.
    with pytest.raises(rollout.Refused, match="protected cloud changed outside the rollout guard"):
        coordinator.acquire("azure", QR.format(3), "control-plane", 3600)
    # The blocked lease is not freed by a release, nor recovered while its owner may be running.
    with pytest.raises(rollout.Refused, match="requires verified recovery"):
        coordinator.release("gcp", lease["operation_id"], True)
    with pytest.raises(rollout.Refused, match="owner may still be mutating production"):
        coordinator.recover("gcp", lease["operation_id"], lambda held: False)
    assert (store.record, store.generation) == frozen
    assert admissions(coordinator) == set()


@pytest.mark.parametrize("version", [rollout.VERSION, rollout.V3])
def test_exit_one_recovering_the_blocked_lease_lets_the_changed_holdback_be_leased(monkeypatch, version):
    store, coordinator, lease = frozen_by_a_changed_holdback(monkeypatch, version)
    served = canned_planes(monkeypatch, aws_evidence=REDEPLOYED)
    coordinator.recover("gcp", lease["operation_id"], lambda held: True)
    # Recovery read what the lease changed and nothing of aws.
    assert served == (GCP_CONTROL_PLANE_PROBE if version == rollout.V3 else GCP_CLOUD_PROBE)
    assert store.record["leases"] == {} and store.record["holdback"] is None
    assert admissions(coordinator) == set(rollout.CLOUDS)
    repair = coordinator.acquire("aws", QR.format(3), "control-plane", 3600)
    assert repair["cloud"] == "aws"
    assert store.record["holdback"]["cloud"] == "gcp"


GCP_UNHEALTHY = {"https://trustedrouter.com/health": (200, b'{"status": "down"}')}


@pytest.mark.parametrize("version", [rollout.VERSION, rollout.V3])
def test_exit_one_is_closed_while_the_blocked_cloud_fails_its_own_probe(monkeypatch, version):
    """The precondition of "not a deadlock": the relying lease must be
    recoverable. With gcp failing too, the coordinator admits nothing until
    an operator repairs gcp or restores aws outside it."""
    store, coordinator, lease = frozen_by_a_changed_holdback(monkeypatch, version)
    canned_planes(monkeypatch, aws_evidence=REDEPLOYED, answers=GCP_UNHEALTHY)
    frozen = (copy.deepcopy(store.record), store.generation)
    with pytest.raises(rollout.Refused, match="not verified healthy|not a verified healthy holdback"):
        coordinator.recover("gcp", lease["operation_id"], lambda held: True)
    assert (store.record, store.generation) == frozen
    assert admissions(coordinator) == set()
    # The protected release put back on aws by hand reopens aws and azure. gcp stays blocked.
    canned_planes(monkeypatch, answers=GCP_UNHEALTHY)
    assert admissions(coordinator) == {"aws", "azure"}
    with pytest.raises(rollout.Refused, match="not verified healthy|not a verified healthy holdback"):
        coordinator.recover("gcp", lease["operation_id"], lambda held: True)
    # Repaired outside the coordinator, gcp recovers and the journal empties.
    canned_planes(monkeypatch, aws_evidence=REDEPLOYED)
    coordinator.recover("gcp", lease["operation_id"], lambda held: True)
    assert store.record["leases"] == {} and store.record["holdback"] is None


@pytest.mark.parametrize("version", [rollout.VERSION, rollout.V3])
@pytest.mark.parametrize("aws_evidence,admitted", [
    # aws serves the protected release again: aws and azure reopen.
    pytest.param(None, {"aws", "azure"}, id="the protected release again"),
    # aws serves no document: only aws, to repair it. azure still needs a healthy holdback.
    pytest.param((500, b"internal error"), {"aws"}, id="no document, 500"),
    pytest.param(LIMITED, {"aws"}, id="no document, 429"),
    # Still changed: nothing.
    pytest.param(REDEPLOYED, set(), id="still changed"),
])
def test_exit_two_the_holdbacks_evidence_stops_differing(monkeypatch, version, aws_evidence, admitted):
    store, coordinator, lease = frozen_by_a_changed_holdback(monkeypatch, version)
    canned_planes(monkeypatch, aws_evidence=aws_evidence)
    assert admissions(coordinator) == admitted
    # gcp stays blocked either way, until it is recovered.
    assert [held["state"] for held in store.record["leases"].values()] == ["blocked"]
    if "aws" in admitted:
        coordinator.acquire("aws", QR.format(3), "control-plane", 3600)
        assert store.record["holdback"]["cloud"] == "azure"


UNCHANGED = (200, evidence_document("aws"))
LOCAL = (200, evidence_document("aws", release="local"))
DOWN = (500, b"internal error")


@pytest.mark.parametrize("version", [rollout.VERSION, rollout.V3])
@pytest.mark.parametrize("first,second,leasable", [
    (UNCHANGED, UNCHANGED, True),
    (UNCHANGED, DOWN, True),
    (DOWN, UNCHANGED, True),
    (UNCHANGED, LOCAL, False),
    (LOCAL, UNCHANGED, False),
    (DOWN, LOCAL, False),
    (LOCAL, DOWN, False),
    (UNCHANGED, (200, b"<html>"), False),
    ((200, b"[]"), DOWN, False),
    # A rate-limited read is no document. It neither refuses on its own nor
    # hides a change the other read shows.
    (LIMITED, LIMITED, True),
    (LIMITED, UNCHANGED, True),
    (UNCHANGED, LIMITED, True),
    (LIMITED, LOCAL, False),
    (LOCAL, LIMITED, False),
])
def test_default_coordinator_refuses_on_either_of_the_two_reads(monkeypatch, version, first, second, leasable):
    store, coordinator, _ = default_coordinator_protecting_aws(monkeypatch, version)
    # After the two evidence reads, aws is not asked again: it is being leased.
    served = canned_planes(monkeypatch, aws_evidence=[first, second])
    if leasable:
        assert coordinator.acquire("aws", QR.format(3), "control-plane", 3600)["cloud"] == "aws"
        assert store.record["holdback"]["cloud"] == "azure"
    else:
        with pytest.raises(rollout.Refused, match="refusing to lease over it"):
            coordinator.acquire("aws", QR.format(3), "control-plane", 3600)
    assert served[:2] == [AWS_EVIDENCE] * 2


@pytest.mark.parametrize("retry_after,attempts", [
    pytest.param("5", 3, id="5"),
    pytest.param("0" * (rollout.RETRY_AFTER_DIGITS - 1) + "5", 3, id="5 in the most digits converted"),
    pytest.param("0" * rollout.RETRY_AFTER_DIGITS + "5", 1, id="5 in one digit more"),
    pytest.param("6", 1, id="6"),
    # Past Python's 4300 digit limit, where int() raises ValueError.
    pytest.param("9" * 4301, 1, id="4301 nines"),
    pytest.param("0" * 4300 + "1", 1, id="1 in 4301 digits"),
    # 0xB2, 0xB3 and 0xB9 decode to superscript digits: str.isdigit() passes
    # them and int() raises ValueError.
    pytest.param("\u00b2", 1, id="superscript 2"),
    pytest.param("\u00b3", 1, id="superscript 3"),
    pytest.param("1\u00b9", 1, id="1 and superscript 1"),
])
def test_fetch_converts_only_a_short_ascii_retry_after(monkeypatch, retry_after, attempts):
    """However the header reads, a retryable status leaves fetch as its own
    HTTPError (no document was served), never as a ValueError (an answer)."""
    served = canned_planes(monkeypatch, aws_evidence=(429, RATE_LIMITED, {"Retry-After": retry_after}))
    with pytest.raises(urllib.error.HTTPError) as caught:
        rollout.fetch(AWS_EVIDENCE)
    assert caught.value.code == 429
    assert served == [AWS_EVIDENCE] * attempts


@pytest.mark.parametrize("answer,raised", [
    ((302, b"", "https://trustedrouter.com/health"), rollout.Redirected),
    ((200, b"[]"), rollout.Refused),
    ((200, b" " * (rollout.MAX_BYTES + 1)), rollout.Refused),
    ((200, b"<html>"), ValueError),
    ((404, b"{}"), urllib.error.HTTPError),
])
def test_fetch_tells_a_redirect_from_an_answer_that_is_not_a_document(monkeypatch, answer, raised):
    canned_planes(monkeypatch, aws_evidence=answer)
    with pytest.raises(raised) as caught:
        rollout.fetch(AWS_EVIDENCE)
    assert isinstance(caught.value, rollout.Redirected) is (raised is rollout.Redirected)
    with pytest.raises(rollout.Refused, match="aws is not a verified healthy holdback"):
        rollout.probe_cloud("aws")  # every one of them still fails the whole-cloud probe


def test_v2_release_still_probes_the_whole_cloud(v2):
    lease = hold(v2, "gcp", "control-plane")
    v2.health["gcp"] = None
    with pytest.raises(rollout.Refused):
        v2.coordinator.release("gcp", lease["operation_id"], True)
    assert ("gcp", "control-plane") not in v2.probed


def test_v3_holdback_cleared_only_with_the_last_lease(v3):
    a = hold(v3, "gcp", "control-plane")
    b = hold(v3, "gcp", "gateway", run=2)
    v3.coordinator.release("gcp", a["operation_id"], True)
    assert v3.store.record["holdback"] is not None
    v3.coordinator.release("gcp", b["operation_id"], True)
    assert v3.store.record["holdback"] is None


# A lease check depends on its own lease and the holdback ----------------------

class Interleaving(MemoryStore):
    """Run `between` once, after a check's first read and before its second."""

    def __init__(self, inner, between):
        self.inner, self.between, self.reads = inner, between, 0

    def read(self):
        self.reads += 1
        if self.reads == 2 and self.between:
            between, self.between = self.between, None
            between()
        return self.inner.read()

    def write(self, record, generation):
        self.inner.write(record, generation)


def test_v3_check_survives_the_other_component_coming_and_going(v3):
    lease = hold(v3, "gcp", "gateway")
    store = Interleaving(v3.store, lambda: hold(v3, "gcp", "control-plane", run=2))
    checker = rollout.Coordinator(store, v3.coordinator.probe, lambda: v3.clock[0],
                                  v3.coordinator.component_probe)
    assert checker.check("gcp", lease["operation_id"])["component"] == "gateway"
    assert store.reads == 2  # decided on the first pass, not by retrying


def test_v3_check_refuses_when_its_own_lease_changes_meanwhile(v3):
    lease = hold(v3, "gcp", "gateway")

    def block():
        v3.coordinator.release("gcp", lease["operation_id"], False)

    store = Interleaving(v3.store, block)
    checker = rollout.Coordinator(store, v3.coordinator.probe, lambda: v3.clock[0],
                                  v3.coordinator.component_probe)
    with pytest.raises(rollout.Refused, match="blocked or expired"):
        checker.check("gcp", lease["operation_id"])


def test_v3_assert_with_a_fence_from_another_cloud_or_component_is_refused(v3):
    lease = hold(v3, "gcp", "gateway")
    with pytest.raises(rollout.Refused, match="not owned"):
        v3.coordinator.check("aws", lease["operation_id"])
    with pytest.raises(rollout.Refused, match="not owned"):
        v3.coordinator.check("gcp", "f" * 32)


# Lane epochs: what a gateway gate reads ----------------------------------------

def test_v3_every_grant_moves_its_lane_epoch_and_lanes_reports_it(v3):
    lanes = v3.coordinator.lanes("gcp")
    assert lanes["schema_version"] == 3
    assert all(not lane["held"] and lane["epoch"] == 0 for lane in lanes["lanes"].values())
    a = hold(v3, "gcp", "control-plane")
    lanes = v3.coordinator.lanes("gcp")["lanes"]
    assert lanes["control-plane"] == {"held": True, "state": "active", "epoch": 1}
    assert lanes["gateway"] == {"held": False, "state": None, "epoch": 0}
    v3.coordinator.release("gcp", a["operation_id"], True)
    released = v3.coordinator.lanes("gcp")["lanes"]["control-plane"]
    assert released == {"held": False, "state": None, "epoch": 1}  # a lease existed in between
    hold(v3, "gcp", "control-plane", run=2)
    assert v3.coordinator.lanes("gcp")["lanes"]["control-plane"]["epoch"] == 2
    hold(v3, "aws", "gateway", run=3)
    assert v3.coordinator.lanes("gcp")["lanes"]["gateway"]["epoch"] == 0
    assert v3.coordinator.lanes("aws")["lanes"]["gateway"]["epoch"] == 1


def test_v3_lanes_reports_a_blocked_lane(v3):
    lease = hold(v3, "gcp", "control-plane")
    v3.coordinator.release("gcp", lease["operation_id"], False)
    assert v3.coordinator.lanes("gcp")["lanes"]["control-plane"]["state"] == "blocked"


def test_v2_lanes_reports_the_one_lease_without_epochs(v2):
    hold(v2, "gcp", "gateway")
    lanes = v2.coordinator.lanes("gcp")
    assert lanes["schema_version"] == 2
    assert lanes["lanes"]["gateway"] == {"held": True, "state": "active", "epoch": None}
    assert lanes["lanes"]["control-plane"] == {"held": False, "state": None, "epoch": None}


def test_v2_lease_without_a_known_component_holds_every_lane(v2):
    hold(v2, "gcp", "gateway")
    record = copy.deepcopy(v2.store.record)
    del record["leases"]["gcp"]["component"]
    v2.store.record = record
    assert all(lane["held"] for lane in v2.coordinator.lanes("gcp")["lanes"].values())


def test_v2_lease_without_a_component_still_blocks_its_cloud(v2):
    hold(v2, "gcp", "gateway")
    record = copy.deepcopy(v2.store.record)
    del record["leases"]["gcp"]["component"]
    v2.store.record = record
    with pytest.raises(rollout.Busy, match="gcp already reserved"):
        hold(v2, "gcp", "control-plane", run=2)


def test_lanes_cli_reads_without_retention_check(v3, monkeypatch, capsys):
    hold(v3, "gcp", "gateway")
    monkeypatch.setattr(rollout, "GCSStore", lambda: v3.store)
    monkeypatch.setattr(sys, "argv", ["cloud_rollout.py", "lanes", "--cloud", "gcp"])
    assert rollout.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["lanes"]["gateway"]["held"] is True


# FIFO per lane, plus the cap across clouds --------------------------------------

def test_v3_an_earlier_conflicting_waiter_keeps_its_place(v3):
    lease = hold(v3, "gcp", "gateway")
    earlier = enqueue(v3, "gcp", "gateway", run=2)
    v3.coordinator.release("gcp", lease["operation_id"], True)  # free, not yet admitted
    for component in ("gateway", "public-surface"):
        with pytest.raises(rollout.Busy, match="earlier waiter"):
            hold(v3, "gcp", component, run=3)
    assert [e["ticket"] for e in v3.store.record["queue"]] == [earlier]
    assert admit(v3, earlier)["operation_id"] == earlier


def test_v3_an_earlier_waiter_for_the_other_lane_does_not_block(v3):
    hold(v3, "gcp", "gateway")
    earlier = enqueue(v3, "gcp", "gateway", run=2)  # waits for the gateway lane
    lease = hold(v3, "gcp", "control-plane", run=3)
    assert lease["component"] == "control-plane"
    with pytest.raises(rollout.Busy):
        admit(v3, earlier)


def test_v3_queued_waiters_in_both_lanes_are_admitted_together(v3):
    gw = hold(v3, "gcp", "gateway")
    cp = hold(v3, "gcp", "control-plane", run=2)
    next_gw = enqueue(v3, "gcp", "gateway", run=3)
    next_cp = enqueue(v3, "gcp", "control-plane", run=4)
    v3.coordinator.release("gcp", cp["operation_id"], True)
    with pytest.raises(rollout.Busy, match="gcp/gateway already reserved"):
        admit(v3, next_gw)
    assert admit(v3, next_cp)["operation_id"] == next_cp  # not stuck behind the gateway waiter
    v3.coordinator.release("gcp", gw["operation_id"], True)
    assert admit(v3, next_gw)["operation_id"] == next_gw


def test_v3_public_surface_waiter_is_not_starved_by_later_lanes(v3):
    gw = hold(v3, "gcp", "gateway")
    surface = enqueue(v3, "gcp", "public-surface", run=2)
    with pytest.raises(rollout.Busy, match="earlier waiter"):
        hold(v3, "gcp", "control-plane", run=3)
    v3.coordinator.release("gcp", gw["operation_id"], True)
    with pytest.raises(rollout.Busy, match="earlier waiter"):
        hold(v3, "gcp", "gateway", run=4)
    assert admit(v3, surface)["component"] == "public-surface"


def test_v3_cap_fifo_earlier_waiters_for_other_new_clouds_take_slots_first(v3):
    hold(v3, "gcp", "control-plane")
    enqueue(v3, "aws", "control-plane", run=2)
    with pytest.raises(rollout.Busy, match="take the free slot"):
        hold(v3, "azure", "gateway", run=3)
    # Joining a cloud that already holds a lease needs no new slot.
    assert hold(v3, "gcp", "gateway", run=4)["cloud"] == "gcp"


def test_v3_cap_fifo_a_same_cloud_waiter_shares_the_slot(v3):
    hold(v3, "aws", "control-plane")
    blocker = hold(v3, "gcp", "public-surface", run=2)
    gw = enqueue(v3, "gcp", "gateway", run=3)  # waits for public-surface
    v3.coordinator.release("gcp", blocker["operation_id"], True)
    # gcp is free again; the earlier gcp/gateway waiter does not take a second slot.
    assert hold(v3, "gcp", "control-plane", run=4)["component"] == "control-plane"
    assert admit(v3, gw)["component"] == "gateway"


# Format and cut-over -----------------------------------------------------------

def test_a_fresh_journal_starts_as_v2(v2):
    hold(v2, "gcp", "gateway")
    assert v2.store.record["schema_version"] == 2
    assert "epochs" not in v2.store.record


def test_v2_journal_keeps_one_lease_per_cloud_under_the_new_code(v2):
    hold(v2, "gcp", "gateway")
    with pytest.raises(rollout.Busy, match="gcp already reserved"):
        hold(v2, "gcp", "control-plane", run=2)
    assert set(v2.store.record["leases"]) == {"gcp"}


@pytest.mark.parametrize("busy", ["lease", "waiter"])
def test_upgrade_needs_an_empty_journal(v2, busy):
    if busy == "lease":
        hold(v2, "gcp", "gateway")
    else:
        hold(v2, "gcp", "gateway")
        enqueue(v2, "gcp", "control-plane", run=2)
        v2.coordinator.release("gcp", v2.store.record["leases"]["gcp"]["operation_id"], True)
    with pytest.raises(rollout.Busy, match="empty journal"):
        v2.coordinator.upgrade()
    assert v2.store.record["schema_version"] == 2


def test_upgrade_and_downgrade_are_idempotent_and_exact(v2):
    assert v2.coordinator.upgrade() is True
    journal_id = v2.store.record.pop("journal_id")
    assert rollout.TICKET.fullmatch(journal_id)
    assert v2.store.record == {"schema_version": 3, "leases": {}, "holdback": None, "epochs": {}}
    v2.store.record["journal_id"] = journal_id
    writes = v2.store.writes
    assert v2.coordinator.upgrade() is False
    assert v2.store.writes == writes
    assert v2.coordinator.downgrade() is True
    assert v2.store.record == {"schema_version": 2, "leases": {}, "holdback": None}
    assert v2.coordinator.downgrade() is False


def test_epochs_survive_a_downgrade_and_every_upgrade_gets_a_new_journal_id(v3):
    """No ABA for a gate window: lanes never repeats an earlier snapshot."""
    lease = hold(v3, "gcp", "control-plane")
    v3.coordinator.release("gcp", lease["operation_id"], True)
    first = v3.coordinator.lanes("gcp")
    assert first["lanes"]["control-plane"]["epoch"] == 1
    assert v3.coordinator.downgrade() is True
    assert v3.store.record["carried_epochs"] == {"gcp/control-plane": 1}
    hold(v3, "gcp", "gateway")  # a v2 lease; v2 keeps the carried field
    v3.coordinator.release("gcp", v3.store.record["leases"]["gcp"]["operation_id"], True)
    assert v3.coordinator.upgrade() is True
    second = v3.coordinator.lanes("gcp")
    assert second["lanes"]["control-plane"]["epoch"] == 1
    assert second["journal_id"] != first["journal_id"]
    hold(v3, "gcp", "control-plane", run=2)
    assert v3.coordinator.lanes("gcp")["lanes"]["control-plane"]["epoch"] == 2


def test_lanes_retries_a_conflicting_read(v3):
    reads = [0]
    inner = v3.store.read

    def flaky():
        reads[0] += 1
        if reads[0] < 3:
            raise rollout.Conflict()
        return inner()

    v3.store.read = flaky
    assert v3.coordinator.lanes("gcp")["schema_version"] == 3
    assert reads[0] == 3


@pytest.mark.parametrize("busy", ["lease", "waiter"])
def test_downgrade_needs_an_empty_journal(v3, busy):
    lease = hold(v3, "gcp", "gateway")
    if busy == "waiter":
        enqueue(v3, "gcp", "gateway", run=2)
        v3.coordinator.release("gcp", lease["operation_id"], True)
    with pytest.raises(rollout.Busy, match="empty journal"):
        v3.coordinator.downgrade()
    assert v3.store.record["schema_version"] == 3


def load_pinned_v2(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "cloud_rollout_pinned_v2.py.txt"
    source = fixture.read_bytes()
    assert hashlib.sha256(source).hexdigest() == PINNED_V2_SHA256
    path = tmp_path / "pinned_v2_cloud_rollout.py"
    path.write_bytes(source)
    spec = importlib.util.spec_from_file_location("pinned_v2_cloud_rollout", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("holding", [False, True])
def test_pinned_v2_client_refuses_a_v3_journal_before_any_write(v3, tmp_path, holding):
    """E6: the pinned client reads v3 as an unknown lock and never writes it."""
    old = load_pinned_v2(tmp_path)
    if holding:
        hold(v3, "gcp", "control-plane")
        enqueue(v3, "gcp", "control-plane", run=2)
    before = (copy.deepcopy(v3.store.record), v3.store.generation)
    legacy = old.Coordinator(v3.store, v3.coordinator.probe, lambda: v3.clock[0])
    fence = (v3.store.record["leases"]["gcp/control-plane"]["operation_id"] if holding else "a" * 32)
    attempts = [
        lambda: legacy.acquire("gcp", QCP.format(9), "gateway", 3600),
        lambda: legacy.enqueue("gcp", QCP.format(9), "gateway", 600),
        lambda: legacy.try_admit("b" * 32, 3600),
        lambda: legacy.check("gcp", fence),
        lambda: legacy.release("gcp", fence, True),
        lambda: legacy.release("gcp", fence, False),
        lambda: legacy.recover("gcp", fence, lambda _: True),
        lambda: legacy.dequeue("b" * 32),
        lambda: old.acquire_in_order(legacy, "gcp", QCP.format(9), "gateway", 3600, 600,
                                     stopped=lambda _: False, sleep=lambda _: None),
    ]
    for attempt in attempts:
        with pytest.raises(old.Refused, match="legacy or unknown"):
            attempt()
    with pytest.raises(old.Refused):
        legacy.prune(lambda _: True)
    assert (v3.store.record, v3.store.generation) == before


def test_new_client_reads_a_journal_the_pinned_v2_client_wrote(v2, tmp_path):
    old = load_pinned_v2(tmp_path)
    legacy = old.Coordinator(v2.store, v2.coordinator.probe, lambda: v2.clock[0])
    lease = legacy.acquire("gcp", QCP.format(9), "gateway", 3600)
    with pytest.raises(rollout.Busy, match="gcp already reserved"):
        hold(v2, "gcp", "control-plane", run=2)
    assert v2.coordinator.lanes("gcp")["lanes"]["gateway"]["held"] is True
    with pytest.raises(rollout.Busy, match="empty journal"):
        v2.coordinator.upgrade()
    legacy.release("gcp", lease["operation_id"], True)
    assert v2.coordinator.upgrade() is True


# Validation of v3 journals ------------------------------------------------------

def v3_lease(cloud, component, fence):
    return {"cloud": cloud, "component": component, "operation_id": fence, "owner": "x",
            "state": "active", "created_at": 1, "expires_at": 2, "host": "h", "pid": 0}


def v3_record(*leases, holdback="azure", epochs=None, journal_id="e" * 32):
    record = {"schema_version": 3, "holdback": None,
              "epochs": {f"{c}/{k}": 1 for c, k, _ in leases} if epochs is None else epochs,
              "journal_id": journal_id,
              "leases": {f"{c}/{k}": v3_lease(c, k, f) for c, k, f in leases}}
    if leases:
        record["holdback"] = {"cloud": holdback, "release": "a" * 40, "checked_at": 1}
    return record


@pytest.mark.parametrize("record,match", [
    (v3_record(("gcp", "gateway", "a" * 32), ("gcp", "public-surface", "b" * 32)), "conflicting"),
    (v3_record(("aws", "gateway", "a" * 32), ("aws", "control-plane", "b" * 32), holdback="gcp"), "conflicting"),
    (v3_record(("gcp", "gateway", "a" * 32), ("gcp", "control-plane", "a" * 32)), "repeated fence"),
    (v3_record(("gcp", "gateway", "a" * 32), ("aws", "gateway", "b" * 32), ("azure", "gateway", "c" * 32),
               holdback="gcp"), "leases"),
    (v3_record(("gcp", "gateway", "a" * 32), holdback="gcp"), "protected cloud"),
    (v3_record(("gcp", "gateway", "a" * 32), epochs={"gcp/gateway": -1}), "epochs"),
    (v3_record(epochs={"gcp/gateway": "1"}), "epochs"),
    (v3_record(("gcp", "gateway", "a" * 32), epochs={}), "epochs"),
    (v3_record(("gcp", "gateway", "a" * 32), epochs={"gcp/gateway": 0}), "epochs"),
    (v3_record(epochs={"gcp/router": 1}), "epochs"),
    (v3_record(epochs={"gcp": 1}), "epochs"),
    (v3_record(journal_id=None), "journal id"),
    (v3_record(journal_id="x"), "journal id"),
])
def test_v3_journal_validation_refuses_states_no_client_writes(record, match):
    with pytest.raises(rollout.Refused, match=match):
        rollout.state_record(record)


@pytest.mark.parametrize("mutate", [
    lambda r: r["leases"].__setitem__("gcp", r["leases"].pop("gcp/gateway")),
    lambda r: r["leases"].__setitem__("gcp/router", r["leases"].pop("gcp/gateway")),
    lambda r: r["leases"]["gcp/gateway"].__setitem__("component", "control-plane"),
    lambda r: r["leases"]["gcp/gateway"].__setitem__("cloud", "aws"),
])
def test_v3_lease_key_must_match_its_cloud_and_component(mutate):
    record = v3_record(("gcp", "gateway", "a" * 32))
    mutate(record)
    with pytest.raises(rollout.Refused, match="invalid release lease"):
        rollout.state_record(record)


@pytest.mark.parametrize("version", [1, 4, "3", 3.0, None])
def test_unknown_journal_versions_stay_busy(version):
    with pytest.raises(rollout.Busy, match="legacy or unknown"):
        rollout.state_record({"schema_version": version, "leases": {}, "holdback": None})


# The interleavings table (docs/design/component-leases-v3.md) -------------------

@pytest.mark.parametrize("cp,gw,allowed", [
    ("control-plane", "gateway", True),      # canary/ramp/migrate while the gateway rolls
    ("public-surface", "gateway", False),    # a standalone public-surface change and PONG gates
    ("control-plane", "public-surface", False),
])
def test_interleavings_table_admission(v3, cp, gw, allowed):
    hold(v3, "gcp", cp)
    if allowed:
        assert hold(v3, "gcp", gw, run=2)["component"] == gw
    else:
        with pytest.raises(rollout.Busy):
            hold(v3, "gcp", gw, run=2)


def test_interleavings_table_release_while_the_other_lane_rolls(v3):
    # "verify-cloud-complete, release | gateway rolling": the control plane's
    # release must not read the gateway's state, and the gateway keeps its lease.
    cp = hold(v3, "gcp", "control-plane")
    gw = hold(v3, "gcp", "gateway", run=2)
    v3.component_health[("gcp", "gateway")] = False
    v3.coordinator.release("gcp", cp["operation_id"], True)
    assert v3.coordinator.check("gcp", gw["operation_id"])["component"] == "gateway"
