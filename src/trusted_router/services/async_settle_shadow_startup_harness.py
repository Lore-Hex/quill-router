"""One boot's startup ledger; missing producers keep opted admission withheld."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode

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

    def correlate(self, descriptor: Descriptor) -> bool:
        if self.descriptor is not None:
            return False
        self.descriptor = descriptor
        self.correlated = self.emitter.emit(dict(kind="a3_startup_correlation", **descriptor.value,
            boot=self.boot, process_start_us=min(PROCESS_START_US, self.boot_at_us),
            recorded_at_us=self.now())) is not None
        return self.correlated

    def artifact_inputs(self, criterion: str) -> tuple[dict[str, str], dict[str, str]]:
        assert self.manifest is not None
        manifest = self.manifest
        selections = manifest["selections"].get(criterion, {})
        closed(selections, set(SOURCES[criterion]))
        checks = dict.fromkeys(CHECKS[criterion], "pass")
        for source, artifact_id in selections.items():
            identifier(artifact_id)
            artifact = manifest["artifacts"][artifact_id]
            closed(artifact, "id criterion source revision region config_sha256 started_at_us completed_at_us checks")
            require((artifact["id"], artifact["criterion"], artifact["source"]) == (artifact_id, criterion, source))
            for name in ("revision", "region", "config_sha256"):
                require(artifact[name] == manifest[name])
            integer(artifact["started_at_us"])
            integer(artifact["completed_at_us"], artifact["started_at_us"]+1, self.now())
            if source != "ci":
                require(artifact["completed_at_us"] - artifact["started_at_us"] <= 3_600_000_000)
            closed(artifact["checks"], set(CHECKS[criterion]))
            for name, result in artifact["checks"].items():
                require(type(result) is str and result in {"pass", "fail"})
                if result == "fail":
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
            if self.emitter.emit(record) is not None:
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
            for criterion in CHECKS:
                await self.adopt(criterion)
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
            require(ack.key == key and type(ack.generation) is str and ack.generation.isascii()
                    and ack.generation.isdigit() and 0 < int(ack.generation) <= (1 << 63)-1)
            require(ack.sha256 == hashlib.sha256(self.ready_raw).hexdigest() and ack.bytes == len(self.ready_raw))
            self.acknowledgement = ack
            self.admission.ready = True
        except Exception:
            self.missing.add("PreIngressRetention" if self.ready_raw else "startup")

    def retained_page(self, row: Any, results: dict[int, str] | None = None) -> None:
        """Called on a retained PAGE, after the native writer's acknowledgement."""
        try:
            page = row.value()  # Existing PAGE parser and locator joins.
            raw = row.body
            require(page["boot"] == self.boot)
            ref = coordinate("receipt.original", raw)
            for event in page["events"]:
                if event["phase"] != "authorize" or event["receipt_locator"] is None:
                    continue
                sequence = int(event["seq"])
                integer(sequence, 1)
                if results is None or sequence not in results:
                    self.missing.add("NativeAttemptResults")
                    continue
                result = results[sequence]
                self.emitter.emit(dict(kind="a3_opted_attempt", boot=self.boot,
                    at_us=event["event_at_us"], result=result,
                    receipt=dict(day=page["day"], boot=self.boot, sequence=sequence, original=ref)))
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


async def retain_ready(bucket: str, key: str, raw: bytes) -> Acknowledgement:
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

    validate(json.loads(raw))
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
    if service is None:
        # JournalBootAllocation: do not invent a replacement journal identity.
        app.state.async_settle_shadow_startup_missing = "JournalBootAllocation"
        return
    admission = Admission()
    if runtime is not None:
        runtime.startup_admission = admission
    harness = Harness(service.journal.boot, admission)
    app.state.async_settle_shadow_startup = harness
    original_acknowledged = service.acknowledged

    def acknowledged(day: str, pages: list[Any], closure: Any) -> None:
        original_acknowledged(day, pages, closure)
        for page in pages:
            harness.retained_page(page)
    service.acknowledged = acknowledged

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
            # ReleasedDependencyOperations and ObserverInstallOriginals remain
            # missing until the owning adapters provide their measured originals.
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
