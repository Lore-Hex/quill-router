"""One boot's startup ledger; missing producers keep opted admission withheld."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode
from uuid import uuid4

from trusted_router.services.async_settle_shadow_launch_descriptor import (
    Descriptor,
    instance_descriptor,
    read_fd,
)
from trusted_router.services.async_settle_shadow_startup_records import (
    CHECKS,
    SOURCES,
    Emitter,
    canonical,
    closed,
    coordinate,
    digest,
    identifier,
    integer,
    require,
    revision,
    validate,
)

MANIFEST_ENV = "TR_ASYNC_SETTLE_SHADOW_STARTUP_MANIFEST"
PROCESS_START_US = time.time_ns() // 1000
MANIFEST_BYTES = 262144


@dataclass
class Admission:
    ready: bool = False


@dataclass(frozen=True)
class Acknowledgement:
    key: str
    generation: str
    sha256: str
    bytes: int


@dataclass
class Originals:
    """Finite boot-local originals, indexed by coordinates made from these bytes."""
    buffers: dict[tuple[str, str, int], bytes] = field(default_factory=dict)
    total: int = 0

    def retain(self, name: str, raw: bytes) -> dict[str, Any]:
        ref = coordinate(name, raw)
        key = (ref["kind"], ref["sha256"], ref["bytes"])
        if key not in self.buffers:
            require(self.total + len(raw) <= 1048576)
            self.buffers[key] = raw
            self.total += len(raw)
        return ref


class ReleaseManifest(dict[str, Any]):
    """Keep the loaded buffer and the exact public-descriptor object within it."""
    def __init__(self, raw: bytes) -> None:
        require(type(raw) is bytes and len(raw) <= MANIFEST_BYTES)
        value = json.loads(raw)
        require(type(value) is dict and canonical(value) == raw)
        super().__init__(value)
        self.original = raw
        public = canonical(value["public_descriptors"])
        marker = b'"public_descriptors":'
        offset = raw.index(marker+public) + len(marker)
        self.public_original = raw[offset:offset+len(public)]
        self.artifact_originals: dict[str, bytes] = {}


def load_manifest(path: str) -> dict[str, Any]:
    # Read one regular reviewed file, with a fixed cap. No directory discovery.
    require(bool(path) and Path(path).is_file())
    with open(path, "rb") as source:
        raw = source.read(MANIFEST_BYTES+1)
    require(len(raw) <= MANIFEST_BYTES)
    value = json.loads(raw)
    require(canonical(value) == raw)
    closed(value, "launch revision region config_sha256 policy_sha256 fixture_sha256 config effective_pins public_descriptors artifacts selections ready_key_prefix")
    revision(value["revision"])
    identifier(value["region"])
    for name in ("config_sha256", "policy_sha256", "fixture_sha256"):
        digest(value[name])
    require(hashlib.sha256(canonical(value["config"])).hexdigest() == value["config_sha256"])
    identifier(value["ready_key_prefix"])
    require(type(value["artifacts"]) is dict and len(value["artifacts"]) <= 32)
    require(type(value["selections"]) is dict and not set(value["selections"]) - set(CHECKS))
    return ReleaseManifest(raw)


@dataclass
class Ports:
    # These producers accept released coordinates only. No HTTP request reaches them.
    dependency: Callable[[dict[str, Any], int], Awaitable[bool]] | None = None
    observer: Callable[[], Awaitable[dict[str, Any]]] | None = None
    retain_ready: Callable[[str, bytes], Awaitable[Acknowledgement | None]] | None = None
    validate_observer: Callable[[dict[str, Any]], None] | None = None
    retain_original: Callable[[str, bytes], Awaitable[Acknowledgement | None]] | None = None


class Harness:
    def __init__(self, boot: str, admission: Admission, *, emitter: Emitter | None = None,
                 ports: Ports | None = None, now: Callable[[], int] | None = None,
                 monotonic: Callable[[], int] | None = None) -> None:
        identifier(boot)
        self.boot, self.admission = boot, admission
        self.emitter, self.ports = emitter or Emitter(), ports or Ports()
        self.now = now or (lambda: time.time_ns()//1000)
        self.monotonic = monotonic or (lambda: time.monotonic_ns()//1000)
        self.boot_at_us, self.monotonic_at_boot_us = self.now(), self.monotonic()
        self.originals = Originals()
        self.files: dict[str, bytes] = {}
        self.file_acknowledgements: dict[str, Acknowledgement] = {}
        raw = canonical(dict(clock="utc_ns", boot=boot, boot_at_us=self.boot_at_us))
        self.clock_basis = dict(clock="utc_ns", boot=boot, boot_at_us=self.boot_at_us,
                                original=self.originals.retain("clock_basis.original", raw))
        self.adopted: dict[str, dict[str, Any]] = {}
        self.finished: set[str] = set()
        self.missing: set[str] = set()
        self.correlated = False
        self.descriptor: Descriptor | None = None
        self.manifest: dict[str, Any] | None = None
        self.ready_raw: bytes | None = None
        self.acknowledgement: Acknowledgement | None = None
        self.task: asyncio.Task[None] | None = None
        self.started = False
        self.shutdown_days: set[str] = set()
        self.attempts: dict[tuple[str, int], tuple[int, str]] = {}
        self.attempt_pages: dict[tuple[str, int], tuple[int, dict[str, Any]]] = {}
        self.attempt_emitted: set[tuple[str, int]] = set()
        self.attempt_emitted_order: deque[tuple[str, int]] = deque()
        self.attempt_count = self.attempt_projected = self.attempt_overflow = 0
        self.attempt_lock = threading.Lock()

    def correlate(self, descriptor: Descriptor) -> bool:
        if self.descriptor is not None:
            return False
        self.descriptor = descriptor
        raw = self.emitter.emit(dict(kind="a3_startup_correlation", **descriptor.value,
            boot=self.boot, process_start_us=min(PROCESS_START_US, self.boot_at_us),
            recorded_at_us=self.now()))
        self.correlated = raw is not None
        if raw is not None:
            self.files["correlation.json"] = raw
        self.files["descriptor.json"] = descriptor.original
        self.files["mode.json"] = canonical(dict(mode=descriptor.mode))
        if descriptor.source_coordinate is not None:
            self.files["source.json"] = descriptor.source_coordinate
        return self.correlated

    def artifact_inputs(self, criterion: str) -> tuple[dict[str, str], dict[str, str]]:
        assert self.manifest is not None
        manifest = self.manifest
        selections = manifest["selections"].get(criterion, {})
        closed(selections, set(SOURCES[criterion]))
        checks = dict.fromkeys(CHECKS[criterion], "pass")
        for source, artifact_id in selections.items():
            identifier(artifact_id)
            declared = manifest["artifacts"][artifact_id]
            require(isinstance(manifest, ReleaseManifest))
            assert isinstance(manifest, ReleaseManifest)
            raw = manifest.artifact_originals[artifact_id]
            artifact = json.loads(raw)
            closed(artifact, "id criterion source router_revision enclave_revision region config_sha256 started_at_us completed_at_us checks conclusion")
            require(artifact["router_revision"] == manifest["revision"])
            require(artifact["enclave_revision"] == manifest["config"]["enclave_revision"])
            revision(artifact["enclave_revision"])
            require(artifact["conclusion"] in {"pass", "fail"})
            # Compare declared joins, but derive all times and outcomes from the original.
            require(all(artifact[k] == v for k, v in declared.items() if k != "path"))
            require((artifact["id"], artifact["criterion"], artifact["source"]) == (artifact_id, criterion, source))
            for name in ("region", "config_sha256"):
                require(artifact[name] == manifest[name])
            integer(artifact["started_at_us"])
            integer(artifact["completed_at_us"], artifact["started_at_us"]+1, self.now())
            if source != "ci":
                require(artifact["completed_at_us"] - artifact["started_at_us"] <= 3_600_000_000)
            closed(artifact["checks"], set(CHECKS[criterion]))
            for name, result in artifact["checks"].items():
                require(type(result) is str and result in {"pass", "fail"})
                if result == "fail" or artifact["conclusion"] == "fail":
                    checks[name] = "fail"
        return selections, checks

    async def local_fields(self, criterion: str, checks: dict[str, str]) -> dict[str, Any]:
        assert self.manifest is not None
        m = self.manifest
        if criterion == "boot_transport":
            require(self.ports.dependency is not None)
            targets = m["config"]["dependencies"]["router"]
            require(type(targets) is list and 2 <= len(targets) <= 16)
            seen = set()
            for target in targets:
                closed(target, "operation resource region")
                require(target["operation"] in {"transport", "read"} and target["region"] == m["region"])
                identifier(target["resource"])
                key = canonical(target)
                require(key not in seen)
                seen.add(key)
            require({t["operation"] for t in targets} == {"transport", "read"})
            observations = []
            for target in targets:
                started = self.monotonic()
                assert self.ports.dependency is not None
                async with asyncio.timeout(10):
                    matched = await self.ports.dependency(dict(target), 10_000_000)
                elapsed = self.monotonic()-started
                # Incomplete or unmatched observations cannot fit a completed row.
                require(matched is True and 0 <= elapsed <= 10_000_000)
                observations.append(dict(**target, boot=self.boot, config_sha256=m["config_sha256"],
                    attempts=1, marker_matches=True, elapsed_us=elapsed, deadline_us=10_000_000,
                    observed_at_us=self.now()))
            return dict(dependencies=observations, clock_basis=self.clock_basis)
        if criterion == "observer_freshness":
            require(self.ports.observer is not None)
            assert self.ports.observer is not None
            async with asyncio.timeout(1):
                observed = await self.ports.observer()
            # ObserverInstallOriginals supplies the exact retained producer buffers.
            expected = "health_basis_raw installed_at_ns contender"
            if observed.get("contender") is True:
                expected += " claim_raw publication_raw publication_elapsed_us claim_at_ns published_at_ns"
            closed(observed, expected)
            require(self.ports.validate_observer is not None)
            assert self.ports.validate_observer is not None
            self.ports.validate_observer(observed)
            basis = json.loads(observed["health_basis_raw"])
            require(basis["reader_boot"] == self.boot and basis["source_state"] == "installed")
            read_at, installed = basis["read_at_ns"], observed["installed_at_ns"]
            integer(read_at, self.boot_at_us*1000)
            integer(installed, read_at+basis["read_elapsed_ns"], self.now()*1000)
            require((installed-read_at) % 1000 == 0)
            elapsed = (installed-read_at)//1000
            integer(elapsed, 0, 250_000)
            fields = dict(health_basis=self.originals.retain("health_basis", observed["health_basis_raw"]),
                install_elapsed_us=elapsed, contender=observed["contender"], installed_at_ns=installed,
                clock_basis=self.clock_basis)
            if observed["contender"]:
                claim, publication = (json.loads(observed[k+"_raw"]) for k in ("claim", "publication"))
                require(claim["owner_boot_id"] == self.boot == publication["publisher_boot_id"])
                require(claim["slot"] == publication["slot"] and publication == basis["body"])
                require(claim["claim_at_ns"] == observed["claim_at_ns"] and publication["observed_at_ns"] == observed["published_at_ns"])
                integer(observed["claim_at_ns"], self.boot_at_us*1000, installed)
                integer(observed["published_at_ns"], observed["claim_at_ns"], installed)
                integer(observed["publication_elapsed_us"], 0, 500_000)
                for name in ("claim", "publication"):
                    fields[name] = self.originals.retain(name, observed[name+"_raw"])
                for name in ("publication_elapsed_us", "claim_at_ns", "published_at_ns"):
                    fields[name] = observed[name]
            return fields
        if criterion == "marker_keys":
            require(isinstance(m, ReleaseManifest))
            assert isinstance(m, ReleaseManifest)
            public = m["public_descriptors"]
            closed(public, "authority_epoch config_sha256 source_revision descriptor_ids")
            require(public["config_sha256"] == m["config_sha256"] and public["source_revision"] == m["revision"])
            require(public["descriptor_ids"] == m["config"]["descriptor_ids"])
            require(type(public["descriptor_ids"]) is list and 1 <= len(public["descriptor_ids"]) <= 32)
            for item in public["descriptor_ids"]:
                identifier(item)
            return dict(effective_pins=m["effective_pins"], source_revision=m["revision"],
                authority_epoch=public["authority_epoch"],
                public_descriptors=self.originals.retain("public_descriptors", m.public_original),
                policy_sha256=m["policy_sha256"], fixture_sha256=m["fixture_sha256"])
        return {}

    async def adopt(self, criterion: str) -> None:
        if criterion in self.finished:
            return
        self.finished.add(criterion)
        assert self.manifest is not None and self.descriptor is not None
        try:
            inputs, checks = self.artifact_inputs(criterion)
            fields = await self.local_fields(criterion, checks)
            record = dict(kind="a3_boot_check", boot=self.boot, role="router",
                revision=self.descriptor.value["revision"], region=self.descriptor.value["region"],
                boot_at_us=self.boot_at_us, criterion=criterion, completed_at_us=self.now(),
                checks=checks, config_sha256=self.manifest["config_sha256"], inputs=inputs, **fields)
            raw = self.emitter.emit(record)
            if raw is not None:
                self.files["adoptions/"+criterion+".json"] = raw
                self.adopted[criterion] = record
            else:
                self.missing.add(criterion)
        except Exception:
            self.missing.add(criterion)

    async def run(self, descriptor: Descriptor, manifest: dict[str, Any]) -> None:
        if self.started:
            return
        self.started = True
        try:
            if self.descriptor is None:
                self.correlate(descriptor)
            require(self.correlated and self.descriptor == descriptor)
            self.manifest = manifest
            require(descriptor.value["revision"] == manifest["revision"] and descriptor.value["region"] == manifest["region"])
            require(isinstance(manifest, ReleaseManifest))
            assert isinstance(manifest, ReleaseManifest)
            self.files["manifest.json"] = manifest.original
            for artifact_id, declared in manifest["artifacts"].items():
                try:
                    raw = await asyncio.to_thread(read_original, declared["path"])
                    require(sum(map(len, self.files.values()))+len(raw) <= 1048576)
                    manifest.artifact_originals[artifact_id] = raw
                    self.files["artifacts/"+hashlib.sha256(raw).hexdigest()+".json"] = raw
                except Exception:
                    self.missing.add("ReleaseArtifactOriginals:"+artifact_id)
            for criterion in CHECKS:
                await self.adopt(criterion)
            await self.retain_startup()
            if set(self.adopted) != set(CHECKS) or any(
                    result != "pass" for r in self.adopted.values() for result in r["checks"].values()):
                return
            at_us = self.now()
            # Do not invent a timestamp when the sampled clock has not advanced.
            require(at_us > max(r["completed_at_us"] for r in self.adopted.values()))
            self.ready_raw = self.emitter.emit(dict(kind="a3_traffic_ready", boot=self.boot, at_us=at_us))
            require(self.ready_raw is not None and self.ports.retain_ready is not None)
            assert self.ready_raw is not None and self.ports.retain_ready is not None
            key = manifest["ready_key_prefix"]+"/"+self.boot+".json"
            async with asyncio.timeout(10):
                ack = await self.ports.retain_ready(key, self.ready_raw)
            require(isinstance(ack, Acknowledgement))
            assert ack is not None
            check_acknowledgement(ack, key, self.ready_raw)
            self.acknowledgement = ack
            self.admission.ready = True
        except Exception:
            self.missing.add("PreIngressRetention" if self.ready_raw else "startup")

    async def retain_startup(self) -> None:
        assert self.manifest is not None
        require(self.ports.retain_original is not None)
        assert self.ports.retain_original is not None
        for (kind, sha, _), raw in self.originals.buffers.items():
            self.files["originals/"+kind+"/"+sha+".json"] = raw
        for suffix, raw in self.files.items():
            key = self.manifest["ready_key_prefix"]+"/"+self.boot+"/"+suffix
            async with asyncio.timeout(10):
                ack = await self.ports.retain_original(key, raw)
            require(isinstance(ack, Acknowledgement))
            assert ack is not None
            check_acknowledgement(ack, key, raw)
            self.file_acknowledgements[key] = ack
        # The index lets capture resolve coordinates and the generation replies.
        index = canonical({k: dict(generation=v.generation, sha256=v.sha256, bytes=v.bytes)
                           for k, v in self.file_acknowledgements.items()})
        key = self.manifest["ready_key_prefix"]+"/"+self.boot+"/index.json"
        async with asyncio.timeout(10):
            ack = await self.ports.retain_original(key, index)
        require(isinstance(ack, Acknowledgement))
        assert ack is not None
        check_acknowledgement(ack, key, index)
        self.file_acknowledgements[key] = ack

    @property
    def missing_attempts(self) -> int:
        return self.attempt_count - self.attempt_projected

    def attempt_start(self) -> None:
        self.attempt_count += 1

    def attempt_result(self, reservation: Any, result: str) -> None:
        if not self.attempt_lock.acquire(blocking=False):
            self.attempt_overflow += 1
            return
        try:
            self._attempt_result(reservation, result)
        finally:
            self.attempt_lock.release()

    def _attempt_result(self, reservation: Any, result: str) -> None:
        key = (reservation.day, reservation.event_seq)
        if len(self.attempts) >= 8192:
            self.attempt_overflow += 1
            return
        require(reservation.boot == self.boot)
        require(result in {"accepted", "failed", "rejected", "timeout"})
        self.attempts[key] = (reservation.observed_us, result)
        self.project_attempt(key)

    def project_attempt(self, key: tuple[str, int]) -> None:
        if key in self.attempt_emitted or key not in self.attempts or key not in self.attempt_pages:
            return
        at_us, result = self.attempts[key]
        page_at, ref = self.attempt_pages[key]
        require(page_at == at_us)
        raw = self.emitter.emit(dict(kind="a3_opted_attempt", boot=self.boot, at_us=at_us,
            result=result, receipt=dict(day=key[0], boot=self.boot, sequence=key[1], original=ref)))
        if raw is not None:
            self.attempt_projected += 1
            if len(self.attempt_emitted_order) == 8192:
                self.attempt_emitted.discard(self.attempt_emitted_order.popleft())
            self.attempt_emitted_order.append(key)
            self.attempt_emitted.add(key)
            del self.attempts[key]
            del self.attempt_pages[key]

    def retained_page(self, row: Any, results: dict[int, str] | None = None) -> None:
        """Observe exact PAGE bytes only after the P3 writer acknowledgement."""
        if not self.attempt_lock.acquire(blocking=False):
            self.attempt_overflow += 1
            self.missing.add("NativeAttemptPage")
            return
        try:
            self._retained_page(row, results)
        finally:
            self.attempt_lock.release()

    def _retained_page(self, row: Any, results: dict[int, str] | None = None) -> None:
        try:
            page = row.value()
            require(page["boot"] == self.boot)
            ref = coordinate("receipt.original", row.body)
            for event in page["events"]:
                if event["phase"] != "authorize" or event["receipt_locator"] is None:
                    continue
                sequence = int(event["seq"])
                integer(sequence, 1)
                key = (page["day"], sequence)
                if key in self.attempt_emitted:
                    continue
                if len(self.attempt_pages) >= 8192:
                    self.attempt_overflow += 1
                    continue
                self.attempt_pages[key] = (event["event_at_us"], ref)
                if results is not None and sequence in results:
                    self.attempt_count += 1
                    self.attempts[key] = (event["event_at_us"], results[sequence])
                if key not in self.attempts:
                    self.missing.add("NativeAttemptResults")
                self.project_attempt(key)
        except Exception:
            self.missing.add("NativeAttemptPage")

    def shutdown_span(self, *, day: str, interval_end_us: int, spans: dict[str, list[int]],
                      complete: bool, outcome: str = "graceful") -> bool:
        if day in self.shutdown_days or self.descriptor is None:
            return False
        self.shutdown_days.add(day)
        if set(spans) != {"preparation", "export_wait", "final_pass", "close", "result"}:
            self.missing.add("P3ShutdownEndpoints")
            return False
        d = self.descriptor.value
        return self.emitter.emit(dict(kind="a3_shutdown_span", boot=self.boot,
            **{k: d[k] for k in ("instance", "launch", "revision", "region")}, day=day,
            interval_end_us=interval_end_us, complete=complete, clock="monotonic_us",
            clock_origin=dict(boot=self.boot, monotonic_at_boot_us=self.monotonic_at_boot_us),
            spans=spans, outcome=outcome)) is not None


def read_original(path: str) -> bytes:
    require(type(path) is str and Path(path).is_file())
    with open(path, "rb") as source:
        raw = source.read(65537)
    require(0 < len(raw) <= 65536)
    return raw


def check_acknowledgement(ack: Acknowledgement, key: str, raw: bytes) -> None:
    require(ack.key == key and type(ack.generation) is str and ack.generation.isascii()
            and ack.generation.isdigit() and 0 < int(ack.generation) <= (1 << 63)-1)
    require(ack.sha256 == hashlib.sha256(raw).hexdigest() and ack.bytes == len(raw))


async def retain_ready(bucket: str, key: str, raw: bytes) -> Acknowledgement:
    validate(json.loads(raw))
    return await retain_object(bucket, key, raw)


async def retain_object(bucket: str, key: str, raw: bytes) -> Acknowledgement:
    """Reuse the existing bounded object worker, retaining its generation reply."""
    from trusted_router.storage_gcp_shadow_attachments import AuthRequest, GCSAttachments, Lifetime

    class ReadyObjects(GCSAttachments):
        def _request(self, method: str, key: str, body: bytes | None, lifetime: Lifetime) -> bytes:
            import google.auth

            headers = {"Content-Type": "application/json"}
            url = self.endpoint+"upload/storage/v1/b/"+self.bucket+"/o?"+urlencode(
                dict(uploadType="media", name=key, ifGenerationMatch=0))
            auth = AuthRequest(lifetime)
            credentials, _ = google.auth.default(request=cast(Any, auth),
                scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
            lifetime.remaining()
            credentials.before_request(auth, "POST", url, headers)
            lifetime.remaining()
            status, _, response = lifetime.exchange("POST", url, body, headers)
            lifetime.remaining()
            require(status in {200, 201})
            return response

    require(type(raw) is bytes and 0 < len(raw) <= MANIFEST_BYTES)
    transport = ReadyObjects(bucket)
    response = await asyncio.to_thread(transport.request, "POST", key, raw, 9.)
    require(response is not None)
    metadata = json.loads(response or b"")
    require(metadata["name"] == key and int(metadata["size"]) == len(raw))
    return Acknowledgement(key, metadata["generation"], hashlib.sha256(raw).hexdigest(), len(raw))


def install(app: Any, settings: Any, runtime: Any = None) -> None:
    """Install only the latch here. The serving listener starts the task later."""
    if getattr(app.state, "async_settle_shadow_startup", None) is not None:
        return
    service = getattr(app.state, "async_settle_shadow_observation", None)
    admission = Admission()
    if runtime is not None:
        runtime.startup_admission = admission
    harness = Harness(service.journal.boot if service is not None else str(uuid4()), admission)
    app.state.async_settle_shadow_startup = harness
    if service is not None:
        bind_service(harness, service, runtime)

    async def begin() -> None:
        try:
            async with asyncio.timeout(2):
                descriptor = await asyncio.to_thread(read_fd)
                if descriptor is not None:
                    require(harness.correlate(descriptor))
                manifest = await asyncio.to_thread(load_manifest, os.environ.get(MANIFEST_ENV, ""))
                if descriptor is None:
                    descriptor = await asyncio.to_thread(instance_descriptor, manifest["launch"])
                    require(harness.correlate(descriptor))
            # Actual deployed settings, including the dormant OFF values.
            pins = dict(TR_ASYNC_SETTLE_ENABLED=str(settings.async_settle_enabled).lower(),
                TR_ASYNC_SETTLE_PROTECTION=str(settings.async_settle_protection).lower(),
                TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED=str(settings.settle_outbox_fast_drain_enabled).lower())
            require(pins == manifest["effective_pins"] and settings.source_revision == manifest["revision"])
            if settings.async_settle_shadow_attachment_bucket:
                async def retain(key: str, raw: bytes) -> Acknowledgement:
                    return await retain_ready(settings.async_settle_shadow_attachment_bucket, key, raw)
                harness.ports.retain_ready = retain
                async def retain_other(key: str, raw: bytes) -> Acknowledgement:
                    return await retain_object(settings.async_settle_shadow_attachment_bucket, key, raw)
                harness.ports.retain_original = retain_other
            if service is not None:
                harness.ports.dependency = dependency_operations(service, manifest)
            await harness.run(descriptor, manifest)
        except Exception:
            harness.missing.add("ReleaseStartupInputs")
    app.state.async_settle_shadow_startup_begin = begin


def listener_started(app: Any) -> None:
    for _ in range(8):
        if hasattr(app, "state"):
            break
        app = getattr(app, "app", None)
    if app is None or not hasattr(app, "state"):
        return
    harness = getattr(app.state, "async_settle_shadow_startup", None)
    begin = getattr(app.state, "async_settle_shadow_startup_begin", None)
    if harness is not None and begin is not None and harness.task is None:
        harness.task = asyncio.create_task(begin(), name="shadow-startup-records")


def bind_service(harness: Harness, service: Any, runtime: Any = None) -> None:
    from trusted_router.async_settle_shadow_health_state import validate_startup_observation

    async def observer() -> dict[str, Any]:
        # Warm work remains owned by P3. This task only waits for its retained reply.
        for _ in range(90):
            if service.startup_observation is not None:
                return dict(service.startup_observation)
            await asyncio.sleep(.01)
        raise ValueError("ObserverInstallOriginals")

    def closed_interval(*, closure: Any, result: Any, **fields: Any) -> None:
        harness.originals.retain("originals.original_close", closure.body)
        if result is not None:
            harness.originals.retain("originals.close_result", result.body)
        harness.shutdown_span(**fields)

    service.startup_close = closed_interval
    harness.ports.observer = observer
    harness.ports.validate_observer = validate_startup_observation
    original_acknowledged = service.acknowledged

    def acknowledged(day: str, pages: list[Any], closure: Any) -> None:
        original_acknowledged(day, pages, closure)
        for page in pages:
            harness.retained_page(page)
    service.acknowledged = acknowledged
    if runtime is not None:
        runtime.startup_attempt_result = harness.attempt_result
        runtime.startup_attempt_start = harness.attempt_start


def dependency_operations(service: Any, manifest: dict[str, Any]) -> Callable[[dict[str, Any], int], Awaitable[bool]]:
    """Bind an exact-key read to the installed observation client, once per boot."""
    from trusted_router.async_settle_shadow_journal import HEALTH_KIND
    from trusted_router.storage_gcp_shadow_observation import Budget

    targets = tuple(canonical(t) for t in manifest["config"]["dependencies"]["router"])
    used: set[bytes] = set()

    async def operation(target: dict[str, Any], deadline_us: int) -> bool:
        key = canonical(target)
        require(key in targets and key not in used and 0 < deadline_us <= 10_000_000)
        used.add(key)
        require(target["region"] == service.settings.effective_serving_region)
        if target["operation"] == "read":
            # The resource is the configured P3 database, never a request value.
            require(target["resource"] == service.settings.spanner_database_id)
            budget = Budget(service.store.clock()+deadline_us/1_000_000, service.store.clock)
            await asyncio.to_thread(service.store.read, HEALTH_KIND, "local/fleet", budget)
            budget.remaining(deadline_us/1_000_000)
            return True
        raise ValueError("ReleasedDependencyOperations.transport")
    return operation
