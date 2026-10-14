"""Round five: process ownership, worker setup and native producer handoff."""
from __future__ import annotations

import ast
import asyncio
import inspect
import re
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from tests.test_async_settle_shadow_cohort import config
from tests.test_async_settle_shadow_startup_harness import descriptor, fake_harness
from trusted_router.services import async_settle_shadow_startup_harness as h


def child(code):
    result = subprocess.run([sys.executable, "-c", code], close_fds=True,  # noqa: S603 - fixed local child
        capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("resource", ["socket", "pipe", "file"])
@pytest.mark.parametrize("harness", [False, True])
def test_package_import_leaves_unowned_input_alone(resource, harness):
    child(f'''
import os, socket, tempfile
if {resource!r} == 'socket':
 owner, peer = socket.socketpair()
 fd = owner.fileno()
elif {resource!r} == 'pipe':
 fd, writer = os.pipe()
 os.write(writer, b'ordinary-file-data'); os.close(writer)
else:
 path = tempfile.mktemp()
 with open(path, 'wb') as output: output.write(b'ordinary-file-data')
 fd = os.open(path, os.O_RDONLY); os.unlink(path)
assert fd == 3
before = set(os.listdir('/dev/fd'))
import trusted_router
assert set(os.listdir('/dev/fd')) == before
if {harness!r}:
 from trusted_router.services.async_settle_shadow_launch_descriptor import read_fd
 from trusted_router.services.async_settle_shadow_startup_harness import Harness, Admission
 startup = Harness('boot', Admission())
 assert read_fd() is None
if {resource!r} == 'socket':
 owner.close(); peer.settimeout(.1)
 assert peer.recv(1) == b''
 peer.close()
else:
 if {resource!r} == 'file': assert os.lseek(fd, 0, os.SEEK_CUR) == 0
 assert os.read(fd, 100) == b'ordinary-file-data'
 os.close(fd)
''')


def test_package_import_does_not_keep_pipe_reader():
    child('''
import os
reader, writer = os.pipe()
assert reader == 3
import trusted_router
os.close(reader)
try: os.write(writer, b'x')
except BrokenPipeError: pass
else: raise AssertionError('package kept the reader open')
os.close(writer)
''')


@pytest.mark.parametrize("valid", [False, True])
def test_owned_descriptor_closes_after_read(valid, tmp_path):
    source = tmp_path / "descriptor"
    source.write_bytes(descriptor().original if valid else b"unfinished")
    child(f'''
import os
fd = os.open({str(source)!r}, os.O_RDONLY)
assert fd == 3
from trusted_router import launch_descriptor_state as state
state.claim()
from trusted_router.services.async_settle_shadow_launch_descriptor import read_fd
try:
 value = read_fd()
 assert {valid!r} and value.mode == 'fd3'
except ValueError:
 assert not {valid!r}
assert state.INHERITED_FD is None
try: os.fstat(3)
except OSError: pass
else: raise AssertionError('owned input was not closed')
assert read_fd() is None
''')


def test_serving_exit_closes_unread_descriptor(tmp_path):
    source = tmp_path / "descriptor"
    source.write_bytes(descriptor().original)
    child(f'''
import os
assert os.open({str(source)!r}, os.O_RDONLY) == 3
from trusted_router import launch_descriptor_state as state
state.claim()
from trusted_router import serve
serve.build_config = lambda: None
class Server:
 def __init__(self, *a, **k): pass
 def run(self): raise RuntimeError('startup did not finish')
serve.DrainingServer = Server
try: serve.main()
except RuntimeError: pass
assert state.INHERITED_FD is None
try: os.fstat(3)
except OSError: pass
else: raise AssertionError('serving exit kept the input')
''')


@pytest.mark.parametrize("setup", ["context", "client", "target"])
async def test_client_setup_keeps_listener_available(monkeypatch, setup):
    settings = config(source_revision="a" * 40)
    resource = h.regional_attestation_resource(settings)
    target = dict(operation="transport", resource=resource, region=settings.effective_serving_region)
    operation = h.dependency_operations(None, {"config": {"dependencies": {"router": [target]}}}, settings=settings)
    app = FastAPI()
    startup, _, _ = fake_harness()
    entered, release = threading.Event(), threading.Event()
    worker_ids = []
    def stalled(*args, **kwargs):
        worker_ids.append(threading.get_ident())
        entered.set()
        release.wait(.4)
        raise ValueError("unfinished setup")
    async def begin():
        with pytest.raises(ValueError):
            await operation(target, 1_000_000)
    app.state.async_settle_shadow_startup = startup
    app.state.async_settle_shadow_startup_begin = begin
    @app.get("/live")
    async def live():
        return {"live": True}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        if setup == "context":
            monkeypatch.setattr(ssl, "create_default_context", stalled)
        elif setup == "client":
            monkeypatch.setattr(httpx, "AsyncClient", stalled)
        else:
            monkeypatch.setattr(h, "regional_attestation_resource", stalled)
        try:
            started = time.monotonic()
            h.listener_started(app)
            while not entered.is_set():
                await asyncio.sleep(.005)
            assert (await client.get("/live")).json() == {"live": True}
            assert time.monotonic()-started < .25
            assert worker_ids == [worker_ids[0]] and worker_ids[0] != threading.get_ident()
        finally:
            release.set()
            await startup.task


async def test_whole_setup_deadline_and_single_worker(monkeypatch):
    settings = config()
    target = dict(operation="transport", resource=h.regional_attestation_resource(settings),
        region=settings.effective_serving_region)
    operation = h.dependency_operations(None, {"config": {"dependencies": {"router": [target]}}}, settings=settings)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def stalled(*args):
        entered.set()
        try:
            release.wait(2)
            raise ValueError("unfinished target")
        finally:
            finished.set()
    monkeypatch.setattr(h, "regional_attestation_resource", stalled)
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await operation(target, 50_000)
        assert entered.is_set() and time.monotonic()-started < .25
        with pytest.raises(ValueError):
            await h.startup_call(lambda: True, .1)
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        # The worker releases its slot after the function returns.
        for _ in range(100):
            if h._STARTUP_WORKER.acquire(blocking=False):
                h._STARTUP_WORKER.release()
                break
            await asyncio.sleep(.005)


def test_authorize_uses_owned_input_only():
    from trusted_router.services import async_settle_shadow_cohort as cohort
    from trusted_router.services.async_settle_shadow_cohort_runtime import Runtime
    source = inspect.getsource(cohort.authorize)
    assert not hasattr(cohort, "NativeAttempt") and not hasattr(cohort, "NativeAttempts")
    assert "startup_" not in source and "report(" not in source and ".retain(" not in source
    assert "NativeAttempt" not in source and "native_attempt" not in source
    tree = ast.parse(source)
    assert not any(isinstance(n, ast.AugAssign) for n in ast.walk(tree))
    runtime = inspect.getsource(Runtime.authorize_http)
    assert runtime.count("self.startup_admission.ready") == 1
    assert ".append(" not in runtime and "collect_native" not in runtime
    assert "self.defer_input(inputs, capable)" in runtime


def test_all_deployed_router_regions_use_released_resource():
    from trusted_router.config import Settings
    source = Path("scripts/deploy/_lib.sh").read_text()
    regions = re.search(r'TR_CONTROL_PLANE_REGIONS="\$\{TR_CONTROL_PLANE_REGIONS:-([^}]+)', source).group(1).split(",")
    assert regions == ["us-central1", "us-east4", "europe-west4", "southamerica-east1"]
    for region in regions:
        dependency_region = "us-central1" if region == "southamerica-east1" else region
        released = h.regional_attestation_resource(Settings(serving_region=dependency_region))
        assert h.regional_attestation_resource(Settings(serving_region=region), released) == released
        with pytest.raises(ValueError):
            h.regional_attestation_resource(Settings(serving_region=region), "https://unlisted.example/attestation")
    with pytest.raises(ValueError):
        h.regional_attestation_resource(Settings(serving_region="southamerica-east1"))


async def test_sao_paulo_runs_released_get(monkeypatch):
    from tests.test_async_settle_shadow_startup_harness_runtime import RegionalEnclave, image_body
    from trusted_router.synthetic import probes
    from trusted_router.synthetic.probes import SyntheticTarget
    settings = config(serving_region="southamerica-east1", spend_lease_accepted_gcp_image_digests="sha256:"+"a"*64)
    with RegionalEnclave(monkeypatch, image_body()) as enclave:
        monkeypatch.setattr(probes, "configured_targets", lambda settings: [
            SyntheticTarget("us-central1", enclave.resource.removesuffix("/attestation")+"/v1")])
        target = dict(operation="transport", resource=enclave.resource, region="southamerica-east1")
        operation = h.dependency_operations(None, {"config": {"dependencies": {"router": [target]}}}, settings=settings)
        assert await operation(target, 1_000_000)
        assert enclave.requests == [("GET", "/attestation")]
        with pytest.raises(ValueError):
            await operation(target, 1_000_000)


async def test_object_reply_comes_from_existing_adapter(monkeypatch):
    from trusted_router.storage_gcp_shadow_attachments import GCSAttachments
    calls = []
    def request(self, method, key, raw, timeout):
        calls.append((method, key, raw, timeout))
        return h.canonical(dict(name=key, size=len(raw), generation="42"))
    monkeypatch.setattr(GCSAttachments, "request", request)
    ack = await h.retain_object("bucket", "boot/original", b"original")
    assert ack.generation == "42" and ack.bytes == 8
    assert calls == [("POST", "boot/original", b"original", 9.)]


def test_missing_owned_descriptor_does_not_become_instance_input():
    child('''
import os
reader, writer = os.pipe()
assert reader == 3
from trusted_router import launch_descriptor_state as state
state.claim()
from trusted_router.services.async_settle_shadow_launch_descriptor import read_fd
os.close(reader)
try: read_fd()
except ValueError: pass
else: raise AssertionError('missing owned input became absent input')
assert state.INHERITED_FD is None
os.close(writer)
''')


@pytest.mark.parametrize("headers, capable", [([], False),
    ([("X-TR-Settlement-Shadow-Policy", "shadow-v2")], True),
    ([("X-TR-Settlement-Shadow-Policy", "request-coordinate")], False)])
def test_deferred_input_carries_capability_bit(monkeypatch, headers, capable):
    from tests.test_async_settle_shadow import NOW, context
    from tests.test_async_settle_shadow_cohort import additions
    from tests.test_async_settle_shadow_cohort_runtime import installed, request
    from tests.test_async_settle_ticket import endpoint
    from trusted_router.schemas import GatewayAuthorizeRequest
    shell, svc, _ = installed()
    startup, _, _ = fake_harness()
    startup.boot = svc.journal.boot
    h.bind_service(startup, svc, shell)
    dispatch = shell.dispatch
    seen = []
    def native_handoff(action, *args):
        if action == shell.input_work:
            inputs, bit, size = args
            assert type(bit) is bool and bit is capable
            assert inputs.reservation is not None and inputs.event is not None
            assert not hasattr(inputs, "native_attempt")
            seen.append(bit)
        return dispatch(action, *args)
    monkeypatch.setattr(shell, "dispatch", native_handoff)
    monkeypatch.setattr("time.time_ns", lambda: NOW*10**9)
    ctx = context()
    body = GatewayAuthorizeRequest(api_key_hash="request-coordinate", model=ctx.authorization.model_id,
        route_type="chat.completions", stream=False, requested_parameters=[])
    try:
        shell.authorize_http(request(headers), body, ctx.authorization, additions(), [endpoint()],
            typed=True, federated=False, replay=False)
        shell.join()
        assert seen == [capable]
        assert startup.attempt_count == int(capable)
    finally:
        shell.close()
        shell.worker.join(2)
        svc.publisher_executor.shutdown(wait=True)
        svc.evidence_executor.shutdown(wait=True)
