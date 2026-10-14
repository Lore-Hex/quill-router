"""Independent review probes; no production changes."""

import asyncio
import hashlib
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from tests.test_async_settle_shadow import NOW, context
from tests.test_async_settle_shadow_cohort import additions, config
from tests.test_async_settle_shadow_cohort_runtime import installed, request
from tests.test_async_settle_shadow_startup_harness import descriptor, fake_harness, manifest
from tests.test_async_settle_ticket import endpoint
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.services import async_settle_shadow_startup_harness as module


@pytest.mark.parametrize("state", ["ready", "held", "absent", "crashing"])
def test_noncohort_full_outputs_and_no_callbacks(monkeypatch, state):
    shell, svc, _ = installed()
    ctx = context()
    auth = replace(ctx.authorization, workspace_id="outside")
    body = GatewayAuthorizeRequest(
        api_key_hash="key-v1",
        model=auth.model_id,
        route_type="chat.completions",
        stream=False,
        requested_parameters=[],
    )
    calls = []
    shell.startup_admission = module.Admission(state == "ready")
    if state != "absent":

        def forbidden(*args):
            calls.append(1)
            raise RuntimeError("no callback for outside cohort")

        shell.startup_attempt_start = shell.startup_attempt_result = forbidden
    monkeypatch.setattr("time.time_ns", lambda: NOW * 10**9)
    try:
        value = additions()
        before = json.dumps(value, sort_keys=True)
        shell.authorize_http(
            request([("X-TR-Settlement-Shadow-Policy", "shadow-v2")]),
            body,
            auth,
            value,
            [endpoint()],
            typed=True,
            federated=False,
            replay=False,
        )
        shell.join()
        assert json.dumps(value, sort_keys=True) == before and not calls
        assert not svc.journal.pages and not svc.port.lost.is_set()
    finally:
        shell.close()
        shell.worker.join(2)
        svc.publisher_executor.shutdown(wait=True)
        svc.evidence_executor.shutdown(wait=True)


async def test_opted_between_ready_emission_and_ack(monkeypatch):
    shell, svc, _ = installed()
    h, lines, _ = fake_harness()
    shell.startup_admission = h.admission
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold(key, raw):
        entered.set()
        await release.wait()
        return module.Acknowledgement(key, "1", hashlib.sha256(raw).hexdigest(), len(raw))

    h.ports.retain_ready = hold
    task = asyncio.create_task(h.run(descriptor(), manifest()))
    ctx = context()
    body = GatewayAuthorizeRequest(
        api_key_hash="key-v1",
        model=ctx.authorization.model_id,
        route_type="chat.completions",
        stream=False,
        requested_parameters=[],
    )
    monkeypatch.setattr("time.time_ns", lambda: NOW * 10**9)
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert any(json.loads(x)["kind"] == "a3_traffic_ready" for x in lines)
        assert not h.admission.ready
        value = additions()
        before = dict(value)
        shell.authorize_http(
            request([("X-TR-Settlement-Shadow-Policy", "shadow-v2")]),
            body,
            ctx.authorization,
            value,
            [endpoint()],
            typed=True,
            federated=False,
            replay=False,
        )
        shell.join()
        assert value == before
        release.set()
        await task
        assert h.admission.ready
    finally:
        release.set()
        await task
        shell.close()
        shell.worker.join(2)
        svc.publisher_executor.shutdown(wait=True)
        svc.evidence_executor.shutdown(wait=True)


@pytest.mark.parametrize("failure", ["missing_manifest", "malformed_descriptor", "crashing_check"])
async def test_listener_real_begin_failure(monkeypatch, failure):
    app = FastAPI()
    module.install(app, config(source_revision="a" * 40))
    h = app.state.async_settle_shadow_startup
    h.emitter.write = lambda raw: None

    @app.get("/live")
    async def live():
        return {"live": True}

    monkeypatch.setattr(module, "read_fd", descriptor)
    monkeypatch.setattr(module, "load_manifest", lambda path: manifest())
    if failure == "missing_manifest":
        monkeypatch.setattr(
            module, "load_manifest", lambda path: (_ for _ in ()).throw(FileNotFoundError())
        )
    elif failure == "malformed_descriptor":
        monkeypatch.setattr(module, "read_fd", lambda: (_ for _ in ()).throw(ValueError()))
    else:

        async def crash(*args):
            h.review_crash_called = True
            raise RuntimeError()

        monkeypatch.setattr(module, "dependency_operations", lambda *args, **kwargs: crash)
    module.listener_started(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await asyncio.wait_for(client.get("/live"), 0.3)).json() == {"live": True}
    await asyncio.wait_for(h.task, 3)
    assert not h.admission.ready
    if failure == "crashing_check":
        assert h.review_crash_called


async def test_stalled_original_read_is_bounded(monkeypatch):
    h, _, _ = fake_harness()
    entered, release = threading.Event(), threading.Event()
    original = module.read_original
    calls = []

    def blocked(path):
        calls.append(path)
        entered.set()
        release.wait(4)
        return original(path)

    monkeypatch.setattr(module, "read_original", blocked)
    monkeypatch.setattr(module, "ORIGINAL_READ_SECONDS", 0.05)
    task = asyncio.create_task(h.run(descriptor(), manifest()))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        await asyncio.wait_for(asyncio.shield(task), 0.5)
        assert not h.admission.ready and not h.ready_raw
        assert len(calls) == 1
        assert any(name.startswith("ReleaseArtifactOriginals:") for name in h.missing)
        other, _, _ = fake_harness()
        await asyncio.wait_for(other.run(descriptor(), manifest()), 0.5)
        assert len(calls) == 1 and not other.admission.ready
    finally:
        release.set()
        for _ in range(200):
            if module._STARTUP_WORKER.acquire(blocking=False):
                module._STARTUP_WORKER.release()
                break
            await asyncio.sleep(0.005)
        await task


async def test_resolver_lock_blocks_listener_task(monkeypatch):
    from trusted_router.stage_d_policy import StageDPolicyResolver

    settings = config(source_revision="a" * 40)
    resolver = StageDPolicyResolver(settings, SimpleNamespace())
    app = FastAPI()
    app.state.stage_d_policy_resolver = resolver
    module.install(app, settings)
    h = app.state.async_settle_shadow_startup
    h.emitter.write = lambda raw: None

    @app.get("/live")
    async def live():
        return {"live": True}

    monkeypatch.setattr(module, "read_fd", descriptor)
    monkeypatch.setattr(module, "load_manifest", lambda path: manifest())
    monkeypatch.setattr(module, "REGISTRY_SECONDS", 0.05)
    ready, release = threading.Event(), threading.Event()

    def locker():
        with resolver._state_lock:
            ready.set()
            release.wait(2)

    worker = threading.Thread(target=locker)
    worker.start()
    assert ready.wait(1)

    async def http_probe():
        before = time.monotonic()
        await asyncio.sleep(0.02)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            response = await client.get("/live")
        assert response.json() == {"live": True}
        return time.monotonic() - before

    task = asyncio.create_task(http_probe())
    module.listener_started(app)
    try:
        assert await task < 0.2
        await asyncio.wait_for(h.task, 0.5)
        assert "DependencyRegistry" in h.missing and not h.admission.ready
    finally:
        release.set()
        worker.join(1)
        for _ in range(200):
            if module._STARTUP_WORKER.acquire(blocking=False):
                module._STARTUP_WORKER.release()
                break
            await asyncio.sleep(0.005)
        await h.task


async def test_failed_original_adopted_once_with_fail():
    from pathlib import Path

    h, lines, _ = fake_harness()
    m = manifest()
    name = m["selections"]["transport"]["ci"]
    declared = m["artifacts"][name]
    declared["checks"]["actual_hops"] = "fail"
    declared["conclusion"] = "fail"
    Path(declared["path"]).write_bytes(
        module.canonical({k: v for k, v in declared.items() if k != "path"})
    )
    loaded = module.ReleaseManifest(module.canonical(m))
    loaded.test_directory = m.test_directory
    m = loaded
    await h.run(descriptor(), m)
    assert "transport" in h.adopted
    assert h.adopted["transport"]["checks"]["actual_hops"] == "fail"
    assert not h.admission.ready and not h.ready_raw
    assert sum(json.loads(raw).get("criterion") == "transport" for raw in lines) == 1
    before = len(lines)
    await h.adopt("transport")
    assert len(lines) == before
    assert not h.admission.ready and not h.ready_raw
    assert h.adopted["transport"]["checks"]["actual_hops"] == "fail"


@pytest.mark.parametrize("request_value", ["request-coordinate", "b" * 64])
async def test_attempt_identities_equal_journal(monkeypatch, request_value):
    shell, svc, _ = installed()
    h, lines, _ = fake_harness()
    h.boot = svc.journal.boot
    module.bind_service(h, svc, shell)
    shell.startup_admission = module.Admission(True)
    ctx = context()
    body = GatewayAuthorizeRequest(
        api_key_hash=request_value,
        model=ctx.authorization.model_id,
        route_type="chat.completions",
        stream=False,
        requested_parameters=[],
    )
    monkeypatch.setattr("time.time_ns", lambda: NOW * 10**9)
    try:
        shell.authorize_http(
            request(
                [("X-TR-Settlement-Shadow-Policy", "shadow-v2"), ("incoming-record", request_value)]
            ),
            body,
            ctx.authorization,
            additions(),
            [endpoint()],
            typed=True,
            federated=False,
            replay=False,
        )
        shell.join()
        pages = svc.journal.freeze(next(iter(svc.journal.days)))
        svc.flush(svc.store.clock() + 2)
        emitted = [json.loads(x) for x in lines]
        assert len(emitted) == 1
        row = emitted[0]
        assert row["boot"] == row["receipt"]["boot"] == svc.journal.boot
        page = pages[0]
        event = page.value()["events"][0]
        assert row["at_us"] == event["event_at_us"]
        assert row["receipt"]["day"] == page.value()["day"]
        assert row["receipt"]["sequence"] == int(event["seq"])
        assert row["receipt"]["original"] == module.coordinate("receipt.original", page.body)
    finally:
        shell.close()
        shell.worker.join(2)
        svc.publisher_executor.shutdown(wait=True)
        svc.evidence_executor.shutdown(wait=True)


async def test_close_interval_matches_retained_result():
    from tests.test_async_settle_shadow_observation import service as make_service

    svc = make_service()
    ticks = [100.0]
    def advancing_clock():
        ticks[0] += .000007
        return ticks[0]
    svc.store.clock = svc.observer.clock = advancing_clock
    h, lines, _ = fake_harness()
    h.boot = svc.journal.boot
    h.monotonic_at_boot_us = round(advancing_clock() * 1e6)
    h.correlate(descriptor())
    module.bind_service(h, svc)
    try:
        await svc.close()
        span = next(
            json.loads(raw) for raw in lines if json.loads(raw)["kind"] == "a3_shutdown_span"
        )
        result = next(
            json.loads(raw)
            for (kind, _, _), raw in h.originals.buffers.items()
            if kind == "close_result"
        )
        from trusted_router.async_settle_shadow_v2_collect_shutdown import durations

        measured = durations(span)["close_us"]
        assert measured == result["elapsed_us"], (measured, result["elapsed_us"])
    finally:
        svc.publisher_executor.shutdown(wait=True)
        svc.evidence_executor.shutdown(wait=True)


def collector(h, record, m):
    config = module.canonical(m["config"])

    def read(ref):
        if ref.get("path") == "config":
            return config
        return h.originals.buffers[(ref["kind"], ref["sha256"], ref["bytes"])]

    from trusted_router.async_settle_shadow_v2_collect_readiness import local_measurements

    return local_measurements(
        SimpleNamespace(files=SimpleNamespace(read=read)),
        dict(
            boot=h.boot,
            boot_at_us=h.boot_at_us,
            role="router",
            region=m["region"],
            revision=m["revision"],
            pins=m["effective_pins"],
        ),
        record["criterion"],
        record,
        dict(
            config=dict(path="config", sha256=m["config_sha256"]),
            policy=dict(sha256=m["policy_sha256"]),
            fixture=dict(sha256=m["fixture_sha256"]),
        ),
    )


async def test_transport_observed_time_collector_join():
    h, _, _ = fake_harness()
    m = manifest()
    h.correlate(descriptor())
    h.manifest = m
    h.now = lambda: time.time_ns() // 1000
    h.monotonic = lambda: time.monotonic_ns() // 1000
    for aid, decl in m["artifacts"].items():
        m.artifact_originals[aid] = module.read_original(decl["path"])

    async def dependency(*args):
        await asyncio.sleep(0.02)
        return True

    h.ports.dependency = dependency
    await h.adopt("boot_transport")
    record = h.adopted["boot_transport"]
    collector(h, record, m)


async def test_contender_elapsed_collector_join():
    from tests.test_async_settle_shadow_observation import NOW_US
    from tests.test_async_settle_shadow_observation import service as make_service

    svc = make_service()
    svc.store.wall = lambda: NOW_US / 1e6
    clock = [100.0]
    svc.store.clock = svc.observer.clock = lambda: clock[0]
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    h, _, _ = fake_harness()
    h.boot = svc.journal.boot
    h.boot_at_us = NOW_US - 1
    h.now = lambda: NOW_US + 1000
    h.clock_basis = dict(
        clock="utc_ns",
        boot=h.boot,
        boot_at_us=h.boot_at_us,
        original=h.originals.retain(
            "clock_basis.original",
            module.canonical(dict(clock="utc_ns", boot=h.boot, boot_at_us=h.boot_at_us)),
        ),
    )
    m = manifest()
    h.manifest = m
    module.bind_service(h, svc)
    native = svc.store.publish

    def delayed(*args):
        value = native(*args)
        clock[0] += 0.001
        return value

    svc.store.publish = delayed
    try:
        svc.publish_cycle(NOW_US // 2000000, NOW_US)
        svc.observer.install_health(svc.store.clock(), svc.store.read_shadow_health())
        fields = await h.local_fields("observer_freshness", {})
        record = dict(
            criterion="observer_freshness",
            completed_at_us=h.now(),
            clock_basis=h.clock_basis,
            **{k: v for k, v in fields.items() if k != "clock_basis"},
        )
        print(
            "publication_elapsed_us",
            record["publication_elapsed_us"],
            "original_delta_us",
            (record["published_at_ns"] - record["claim_at_ns"]) // 1000,
        )
        collector(h, record, m)
    finally:
        svc.publisher_executor.shutdown(wait=True)
        svc.evidence_executor.shutdown(wait=True)


@pytest.mark.parametrize(
    "ids", [[f"key-{i}" for i in range(16)], [f"key-{i}" for i in range(17)], ["key-0", "key-0"]]
)
async def test_public_descriptor_count_collector_join(ids):
    h, _, _ = fake_harness()
    m = manifest()
    m["config"]["descriptor_ids"] = ids
    m["config_sha256"] = hashlib.sha256(module.canonical(m["config"])).hexdigest()
    m["public_descriptors"]["config_sha256"] = m["config_sha256"]
    m["public_descriptors"]["descriptor_ids"] = ids
    m = module.ReleaseManifest(module.canonical(m))
    h.manifest = m
    valid = len(ids) <= 16 and len(ids) == len(set(ids))
    if valid:
        fields = await h.local_fields("marker_keys", {})
        collector(
            h,
            dict(
                criterion="marker_keys",
                completed_at_us=h.now(),
                config_sha256=m["config_sha256"],
                **fields,
            ),
            m,
        )
    else:
        with pytest.raises(ValueError):
            await h.local_fields("marker_keys", {})
        with pytest.raises(ValueError):
            collector(h, dict(criterion="marker_keys"), m)


@pytest.mark.parametrize("supplied", [False, True])
def test_inherited_descriptor_state_before_event_loop(supplied):
    import subprocess
    import sys

    code = """import os
try:
 os.fstat(3)
except OSError:
 pass
else:
 raise AssertionError('fresh child must start without descriptor 3')
if SUPPLIED:
 import tempfile
 source=tempfile.TemporaryFile()
 source.write(b'not-a-descriptor');source.seek(0)
 os.dup2(source.fileno(),3)
import trusted_router
from trusted_router import launch_descriptor_state
launch_descriptor_state.claim()
import asyncio
from types import SimpleNamespace
from trusted_router.services import async_settle_shadow_startup_harness as module
from trusted_router.services import async_settle_shadow_launch_descriptor as launch
from tests.test_async_settle_shadow_startup_harness import manifest
from tests.test_async_settle_shadow_cohort import config
async def main():
 app=SimpleNamespace(state=SimpleNamespace())
 module.install(app,config(source_revision='a'*40))
 h=app.state.async_settle_shadow_startup
 h.emitter.write=lambda raw:None
 module.load_manifest=lambda path:manifest()
 module.instance_descriptor=lambda mapping:launch.instance_descriptor(mapping,environ={'K_REVISION':'revision-one'},instance_reader=lambda:'123')
 module.listener_started(app)
 await asyncio.wait_for(h.task,3)
 if SUPPLIED:
  assert h.descriptor is None and 'ReleaseStartupInputs' in h.missing
 else:
  assert h.descriptor is not None and h.descriptor.mode=='instance_as_launch', h.missing
 assert not h.admission.ready
asyncio.run(main())
""".replace("SUPPLIED", repr(supplied))
    result = subprocess.run(  # noqa: S603 - fixed local child
        [sys.executable, "-c", code], close_fds=True, capture_output=True, text=True, timeout=45
    )  # noqa: S603 - fixed local child
    assert result.returncode == 0, result.stderr


def test_admission_recorders_run_only_on_worker(monkeypatch):
    shell, svc, _ = installed()
    h, _, _ = fake_harness()
    h.boot = svc.journal.boot
    module.bind_service(h, svc, shell)
    shell.startup_admission = module.Admission(True)
    calls = []

    def started():
        calls.append(("start", threading.get_ident()))

    def result(*args):
        calls.append(("result", threading.get_ident()))

    shell.startup_attempt_start = started
    shell.startup_attempt_result = result
    ctx = context()
    body = GatewayAuthorizeRequest(
        api_key_hash="request-id",
        model=ctx.authorization.model_id,
        route_type="chat.completions",
        stream=False,
        requested_parameters=[],
    )
    monkeypatch.setattr("time.time_ns", lambda: NOW * 10**9)
    try:
        shell.authorize_http(
            request([("X-TR-Settlement-Shadow-Policy", "shadow-v2")]),
            body,
            ctx.authorization,
            additions(),
            [endpoint()],
            typed=True,
            federated=False,
            replay=False,
        )
        shell.join()
        assert calls == [("start", shell.worker.ident), ("result", shell.worker.ident)]
    finally:
        shell.close()
        shell.worker.join(2)
        svc.publisher_executor.shutdown(wait=True)
        svc.evidence_executor.shutdown(wait=True)

@pytest.mark.parametrize("request_value", ["request-coordinate", "e" * 64])
async def test_all_record_producer_joins_with_request_values(monkeypatch, request_value):
    from tests.test_async_settle_shadow_observation_a1 import MemoryAttachments
    shell, svc, db = installed()
    svc.bases.transport = svc.traces.transport = MemoryAttachments()
    h, lines, _ = fake_harness()
    h.boot = svc.journal.boot
    h.boot_at_us = (NOW - 2) * 1_000_000
    tick = [NOW * 1_000_000]
    def now():
        tick[0] += 1000
        return tick[0]
    h.now = h.monotonic = now
    h.clock_basis = dict(clock="utc_ns", boot=h.boot, boot_at_us=h.boot_at_us,
        original=h.originals.retain("clock_basis.original", module.canonical(
            dict(clock="utc_ns", boot=h.boot, boot_at_us=h.boot_at_us))))
    module.bind_service(h, svc, shell)
    shell.startup_admission = h.admission
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    svc.store.wall = lambda: NOW - 1
    db.server_ns = (NOW - 1) * 10**9
    monkeypatch.setattr("time.time_ns", lambda: NOW * 10**9)
    ctx = context()
    body = GatewayAuthorizeRequest(api_key_hash=request_value, model=ctx.authorization.model_id,
        route_type="chat.completions", stream=False, requested_parameters=[])
    headers = [("X-TR-Settlement-Shadow-Policy", "shadow-v2"), ("incoming-record", request_value)]
    d, m = descriptor(), manifest()
    try:
        shell.authorize_http(request(headers), body, ctx.authorization, additions(), [endpoint()],
            typed=True, federated=False, replay=False)
        shell.join()
        svc.publish_cycle((NOW - 1) * 1_000_000 // 2_000_000, (NOW - 1) * 1_000_000)
        svc.observer.install_health(svc.store.clock(), svc.store.read_shadow_health())
        assert svc.startup_observation is not None
        h.manifest = m
        await h.local_fields("observer_freshness", {})
        await h.run(d, m)
        assert h.admission.ready, h.missing
        svc.store.wall = lambda: NOW + 1
        db.server_ns = (NOW + 1) * 10**9
        pages = svc.journal.freeze(next(iter(svc.journal.days)))
        svc.flush(svc.store.clock() + 2)
        rows = [json.loads(raw) for raw in lines]
        assert {r["kind"] for r in rows} == {"a3_startup_correlation", "a3_boot_check",
            "a3_traffic_ready", "a3_opted_attempt"}
        for row in rows:
            assert row["boot"] == svc.journal.boot
            for name in ("instance", "launch", "revision", "region", "role"):
                if name in row:
                    assert row[name] == d.value[name]
            if row["kind"] == "a3_boot_check":
                assert row["config_sha256"] == m["config_sha256"]
                assert row["inputs"] == m["selections"][row["criterion"]]
                for key in ("health_basis", "claim", "publication", "public_descriptors"):
                    if key in row:
                        ref = row[key]
                        raw = h.originals.buffers[(ref["kind"], ref["sha256"], ref["bytes"])]
                        owned = {"health_basis": svc.startup_observation["health_basis_raw"],
                            "claim": svc.store.startup_claim_raw,
                            "publication": svc.store.startup_publication_raw,
                            "public_descriptors": m.public_original}[key]
                        assert raw == owned
                        assert ref == module.coordinate(key, owned)
                if "clock_basis" in row:
                    assert row["clock_basis"] == h.clock_basis
                    ref = row["clock_basis"]["original"]
                    raw = h.originals.buffers[(ref["kind"], ref["sha256"], ref["bytes"])]
                    assert json.loads(raw) == dict(clock="utc_ns", boot=svc.journal.boot, boot_at_us=h.boot_at_us)
                if "dependencies" in row:
                    for dep, target in zip(row["dependencies"], m["config"]["dependencies"]["router"], strict=True):
                        assert dep["boot"] == svc.journal.boot
                        assert dep["config_sha256"] == m["config_sha256"]
                        assert all(dep[k] == v for k, v in target.items())
                if row["criterion"] == "marker_keys":
                    assert row["effective_pins"] == m["effective_pins"]
                    assert row["authority_epoch"] == m["public_descriptors"]["authority_epoch"]
                    assert row["source_revision"] == m["revision"]
                    assert row["policy_sha256"] == m["policy_sha256"]
                    assert row["fixture_sha256"] == m["fixture_sha256"]
            elif row["kind"] == "a3_opted_attempt":
                page = pages[0]
                event = page.value()["events"][0]
                assert row["receipt"] == dict(day=page.value()["day"], boot=svc.journal.boot,
                    sequence=int(event["seq"]), original=module.coordinate("receipt.original", page.body))
                assert row["at_us"] == event["event_at_us"]
            elif row["kind"] == "a3_traffic_ready":
                assert h.ready_raw == module.canonical(row)
                assert h.acknowledgement.sha256 == hashlib.sha256(h.ready_raw).hexdigest()
    finally:
        shell.close()
        shell.worker.join(2)
        svc.publisher_executor.shutdown(wait=True)
        svc.evidence_executor.shutdown(wait=True)

async def test_declaration_mismatch_is_missing_input():
    h, lines, _ = fake_harness()
    m = manifest()
    m['artifacts'][m['selections']['transport']['ci']]['checks']['actual_hops'] = 'fail'
    loaded = module.ReleaseManifest(module.canonical(m))
    loaded.test_directory = m.test_directory
    m = loaded
    await h.run(descriptor(), m)
    assert 'transport' in h.missing and 'transport' not in h.adopted
    assert not h.admission.ready and h.ready_raw is None
    assert not any(json.loads(raw).get('criterion') == 'transport' for raw in lines)

@pytest.mark.parametrize("request_value", ["request-coordinate", "e" * 64])
async def test_close_producer_joins_after_request_inputs(monkeypatch, request_value):
    shell, svc, _ = installed()
    h, lines, _ = fake_harness()
    h.boot = svc.journal.boot
    module.bind_service(h, svc, shell)
    d = descriptor()
    h.correlate(d)
    ctx = context()
    auth = replace(ctx.authorization, workspace_id="outside")
    body = GatewayAuthorizeRequest(api_key_hash=request_value, model=auth.model_id,
        route_type="chat.completions", stream=False, requested_parameters=[])
    monkeypatch.setattr("time.time_ns", lambda: NOW * 10**9)
    try:
        shell.authorize_http(request([("X-TR-Settlement-Shadow-Policy", request_value)]), body,
            auth, additions(), [endpoint()], typed=True, federated=False, replay=False)
        shell.join()
        await svc.close()
        span = next(json.loads(raw) for raw in lines if json.loads(raw)["kind"] == "a3_shutdown_span")
        assert all(span[name] == value for name, value in d.value.items() if name != "role")
        assert span["boot"] == span["clock_origin"]["boot"] == svc.journal.boot
        assert span["clock_origin"]["monotonic_at_boot_us"] == h.monotonic_at_boot_us
        originals = {kind: json.loads(raw) for (kind, _, _), raw in h.originals.buffers.items()
            if kind in {"closure", "close_result"}}
        for original in originals.values():
            assert original["boot"] == svc.journal.boot
            assert original["day"] == span["day"]
            assert original["router_revision"] == span["revision"]
        assert originals["close_result"] == svc.last_closure_ack
        assert hashlib.sha256(module.canonical(originals["closure"])).hexdigest() == svc.last_closure_ack["closure_sha256"]
        assert originals["closure"]["region"] == span["region"]
        assert originals["closure"]["interval_end_us"] == span["interval_end_us"]
        from trusted_router.async_settle_shadow_v2_collect_shutdown import durations
        assert durations(span)["close_us"] == originals["close_result"]["elapsed_us"]
    finally:
        shell.close()
        shell.worker.join(2)
        svc.publisher_executor.shutdown(wait=True)
        svc.evidence_executor.shutdown(wait=True)
