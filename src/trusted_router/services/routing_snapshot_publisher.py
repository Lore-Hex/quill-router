"""Publishes the signed routing snapshot to the fast path, dark (E2, "Publishing").

docs/design/routing-snapshot-v1.md, "Publishing". Imported only when
``routing_snapshot_publish_enabled`` is on; nothing else at runtime reads it.

A window starts on the hour, or at the latest retirement or expiring-manifest
deadline of the hour that has passed (``routing_snapshot.publication_window_as_of``,
over events read from the image, not from this process's registry). A
publication is served until the next window start or its ``not_after``,
whichever comes first, and the next poll after that compiles the next window.

The live catalog is compiled as of the window's start, in a worker process
(``routing_snapshot_worker``) whose clocks read that instant before it imports
the catalog. This process's registry was built when it was imported and has
dropped what was retired then; the worker's is what a process imported at the
window's start builds. So every instance, cold or warm, publishes identical
bytes and an identical manifest (Ed25519 signatures are deterministic), across
retirements too, and the serving process's catalog is never touched.

A window whose compile the catalog refuses is not compiled again; the next
window start retries. Any other failure (the worker's exit, a timeout, an
answer that does not verify) is retried after a backoff that starts at 5 s and
doubles with each failure in the window, up to its end. A poll that finds a
compile in flight answers 503 ``compiling`` at once rather than wait on the
lock in a threadpool thread authorize shares.

The worker also signs, with the mounted key file; this process checks the
manifest under its own pinned key and that it binds the bytes received. The
worker takes about 3 s of CPU (its import, the compile and the signature) and
150 to 300 MB of memory while it runs; this process about 0.03 s. It runs
lazily, single-flight under a lock, in the threadpool worker of the first poll
that finds no current snapshot, which waits on the worker process: the CPU is
not this process's, so the compile holds no GIL that authorize waits on, but
it competes with authorize for the instance's vCPU, and its memory sits
beside the serving process's within the instance's limit: measure both
before enabling.

Nothing here logs or returns key material: the signing key is read from its
mounted file, here at construction and in the worker per window, and never
leaves either process; responses carry the body and the manifest, logs carry
instants, sizes and digests.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trusted_router.detached_jws import TrustedKey, b64encode
from trusted_router.routing_snapshot import (
    EPOCH_SECONDS,
    CatalogInputs,
    Plane,
    SnapshotRefused,
    SnapshotSettings,
    canonical_snapshot_bytes,
    compile_routing_snapshot,
    publication_window,
    publication_window_as_of,
    snapshot_settings_from,
)
from trusted_router.routing_snapshot_signing import (
    PURPOSE,
    RoutingSnapshotSigner,
    verify_manifest_digest,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from trusted_router.config import Settings

log = logging.getLogger(__name__)

# The control plane signs; the fast path's edge is the one consumer. Fixed
# here, not settings: Go pins the same two values beside the public key.
ISSUER = "https://trustedrouter.com"
AUDIENCE = "tr-fastpath-edge"
# Six hours of last-good copy for the edge, recompiled every hour.
HORIZON_SECONDS = 6 * EPOCH_SECONDS
RESPONSE_KIND = "tr-routing-snapshot-publication"
# After a failure other than the catalog's refusal, which may be transient.
ERROR_BACKOFF_SECONDS = 5
# A poll that finds a compile in flight is told to come back this soon,
# rather than wait on the lock in a threadpool thread authorize shares.
COMPILING_RETRY_SECONDS = 2
WORKER_MODULE = "trusted_router.routing_snapshot_worker"
# A compile is a few seconds; a worker past this is killed and retried.
WORKER_TIMEOUT_SECONDS = 120.0


class WorkerFailed(RuntimeError):
    """The worker exited non-zero; ``error`` is its exception's class, or ``unknown``."""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


_ERROR_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")


def _failure_name(exc: BaseException) -> str:
    """The class to log for a failure: the worker's own, or this exception's."""
    return f"worker:{exc.error}" if isinstance(exc, WorkerFailed) else type(exc).__name__


class SnapshotUnavailable(Exception):
    """No snapshot is current; the caller answers 503 (Go keeps its last good copy until its not_after)."""

    def __init__(self, status: str, retry_at: int, now: int) -> None:
        super().__init__(status)
        self.status = status
        self.retry_at = retry_at
        self.retry_after = max(1, retry_at - now)


@dataclass(frozen=True)
class Publication:
    compiled_at: int
    not_after: int
    refresh_at: int
    sha256: str
    body: bytes
    manifest: str
    response: bytes

    def current(self, now: int) -> bool:
        return self.compiled_at <= now < self.refresh_at


def _pem_public(path: str) -> str | None:
    try:
        private = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    except Exception:
        return None
    if not isinstance(private, Ed25519PrivateKey):
        return None
    return b64encode(private.public_key().public_bytes_raw())


def load_signer(settings: Settings) -> RoutingSnapshotSigner:
    """The routing key from its mounted file, refused unless it is the pinned key.

    Raises ValueError with a reason that names no key material.
    """
    path = settings.routing_snapshot_private_key_file
    if not path or not settings.routing_snapshot_kid or not settings.routing_snapshot_public_key:
        raise ValueError("routing snapshot key is not configured")
    others = (
        (settings.async_settle_ticket_private_key_file, settings.async_settle_ticket_kid),
        (settings.speculation_shadow_private_key_file, settings.speculation_shadow_kid),
    )
    if any(other_path == path for other_path, _ in others):
        raise ValueError("separate key file required")
    try:
        private = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    except Exception:
        raise ValueError("routing snapshot key file is unreadable") from None
    if not isinstance(private, Ed25519PrivateKey):
        raise ValueError("routing snapshot key must be Ed25519")
    public = b64encode(private.public_key().public_bytes_raw())
    if public != settings.routing_snapshot_public_key:
        raise ValueError("mounted routing snapshot key is not the pinned public key")
    # The other purposes' keys, so the signer refuses a shared kid or key.
    reserved = [
        TrustedKey(kid, "other", (_pem_public(other_path) if other_path else None) or "")
        for other_path, kid in others
        if kid or other_path
    ]
    trusted = TrustedKey(settings.routing_snapshot_kid, PURPOSE, public, ISSUER, AUDIENCE)
    return RoutingSnapshotSigner(private, trusted, reserved=reserved)


@dataclass(frozen=True)
class Signed:
    """A window's canonical body and its manifest."""

    body: bytes
    manifest: str
    compiled_at: int
    not_after: int


@dataclass(frozen=True)
class WindowPlan:
    """The window for an instant, and how to compile and sign it."""

    start: int
    next_start: int
    compile: Callable[[RoutingSnapshotSigner], Signed]


class SnapshotSource(Protocol):
    def plan(self, now: int) -> WindowPlan: ...


@dataclass(frozen=True)
class SnapshotIdentity:
    """What every compile of this process's snapshots names."""

    snapshot_settings: SnapshotSettings
    plane: Plane
    source_revision: str
    horizon: int = HORIZON_SECONDS


class InProcessSource:
    """Compiles and signs a catalog this process holds: a fixture's (tests), never the live one.

    The live registry depends on when this process imported it, so a live
    catalog goes through AsOfSource.
    """

    def __init__(self, inputs: Callable[[], CatalogInputs], identity: SnapshotIdentity) -> None:
        self._inputs, self._identity = inputs, identity

    def plan(self, now: int) -> WindowPlan:
        inputs = self._inputs()
        start, next_start = publication_window(now, inputs)
        identity = self._identity

        def compile_and_sign(signer: RoutingSnapshotSigner) -> Signed:
            snapshot = compile_routing_snapshot(
                at=start, horizon=identity.horizon, settings=identity.snapshot_settings,
                source_revision=identity.source_revision, plane=identity.plane, inputs=inputs,
            )
            body = canonical_snapshot_bytes(snapshot)
            return Signed(body, signer.sign(body), snapshot.compiled_at, snapshot.not_after)

        return WindowPlan(start, next_start, compile_and_sign)


class AsOfSource:
    """Compiles and signs the live catalog as of the window's start, in routing_snapshot_worker.

    The worker is a process of its own whose clocks read the window's start
    before it imports the catalog, so every instance, whenever it was imported,
    publishes what a process imported at that instant compiles. It also signs,
    with the mounted key file (``RoutingSnapshotSigner.sign`` verifies what it
    signs in full), so the compile's and the signature's CPU are not this
    process's and hold no lock that authorize waits on. This process checks the
    manifest under its own pinned key, plane and audience, and that it binds
    the bytes it received (``verify_manifest_digest``), without parsing them.
    The worker inherits this process's environment (the catalog's gateway holds
    read the region from it). Its stderr is discarded: a failure is logged by
    its exit status.
    """

    def __init__(self, identity: SnapshotIdentity, *, auto_model_order: str, key_file: str,
                 timeout: float = WORKER_TIMEOUT_SECONDS,
                 runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
                 ) -> None:
        self._identity, self._auto_model_order = identity, auto_model_order
        self._key_file, self._timeout, self._runner = key_file, timeout, runner

    def plan(self, now: int) -> WindowPlan:
        start, next_start = publication_window_as_of(now)
        return WindowPlan(start, next_start, lambda signer: self._compile_as_of(start, signer))

    def _compile_as_of(self, at: int, signer: RoutingSnapshotSigner) -> Signed:
        identity, trusted = self._identity, signer.trusted
        request = json.dumps({
            "at": at, "horizon": identity.horizon,
            "settings": identity.snapshot_settings.model_dump(mode="json"),
            "source_revision": identity.source_revision,
            "plane": identity.plane.model_dump(mode="json"),
            "auto_model_order": self._auto_model_order,
            "key": {"file": self._key_file, "kid": trusted.kid, "public_key": trusted.public_key_b64url,
                    "iss": trusted.iss, "aud": trusted.aud},
        }).encode("ascii")
        # -P: the working directory is not on the worker's import path.
        result = self._runner(  # noqa: S603 - this interpreter and a fixed module, no shell
            [sys.executable, "-P", "-m", WORKER_MODULE], input=request, capture_output=True,
            timeout=self._timeout, check=False,
        )
        answer = _last_json_object(result.stdout)
        if result.returncode != 0:
            error = answer.get("error") if answer is not None else None
            raise WorkerFailed(
                error if isinstance(error, str) and _ERROR_CLASS.fullmatch(error) else "unknown",
            )
        if answer is None:
            raise RuntimeError("as-of compile answered no object")
        if "refused" in answer:
            raise SnapshotRefused(str(answer["refused"]))
        body = base64.b64decode(answer["body"], validate=True)
        claims = verify_manifest_digest(
            answer["manifest"], [trusted], body,
            expected_plane=identity.plane, expected_aud=trusted.aud,
        )
        if claims.compiled_at != at or claims.source_revision != identity.source_revision:
            raise RuntimeError("as-of compile answered another snapshot")
        return Signed(body, answer["manifest"], claims.compiled_at, claims.not_after)


def _last_json_object(stdout: bytes) -> dict[str, Any] | None:
    lines = stdout.strip().splitlines()
    try:
        answer = json.loads(lines[-1]) if lines else None
    except ValueError:
        return None
    return answer if isinstance(answer, dict) else None


class RoutingSnapshotPublisher:
    """Compiles, signs and caches the snapshot for the current window."""

    def __init__(
        self,
        *,
        signer: RoutingSnapshotSigner,
        source: SnapshotSource,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._signer = signer
        self._source = source
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: Publication | None = None
        # A refused window's end: no load or compile before the next window
        # start (the window start is constant until then).
        self._refused_until = 0
        # After an unexpected failure, no compile before this instant; the
        # backoff doubles with each failure in one window, up to its end.
        self._error_until = 0
        self._failures: tuple[int, int] = (0, 0)  # (window start or hour, count)
        self.compiles = 0

    @property
    def trusted_keys(self) -> Sequence[TrustedKey]:
        return (self._signer.trusted,)

    def current(self) -> Publication:
        """The current window's publication; raises SnapshotUnavailable."""
        cached = self._cached
        if cached is not None and cached.current(int(self._clock())):
            return cached
        if not self._lock.acquire(blocking=False):
            # A compile is in flight: come back shortly, without holding a
            # threadpool thread that authorize shares for its duration.
            now = int(self._clock())
            raise SnapshotUnavailable("compiling", now + COMPILING_RETRY_SECONDS, now)
        try:
            return self._current_locked()
        finally:
            self._lock.release()

    def _current_locked(self) -> Publication:
        now = int(self._clock())
        cached = self._cached
        if cached is not None and cached.current(now):
            return cached
        if now < self._refused_until:
            raise SnapshotUnavailable("snapshot-refused", self._refused_until, now)
        if now < self._error_until:
            raise SnapshotUnavailable("snapshot-error", self._error_until, now)
        # Until the source names the window, a refusal holds for the hour.
        hour = now - now % EPOCH_SECONDS
        start, next_start = hour, hour + EPOCH_SECONDS
        try:
            plan = self._source.plan(now)
            start, next_start = plan.start, plan.next_start
            signed = self._compile(plan)
            if not now < signed.not_after:
                raise SnapshotRefused("the window ended before it was published")
            publication = self._publication(signed, next_start)
        except SnapshotRefused as exc:
            # The catalog refuses this window for good: an event between
            # its start and the compiling process's clocks or import, or a
            # catalog shape the compiler does not know, facts of this
            # image that a later attempt would meet again. The next window
            # start retries.
            self._cached, self._refused_until = None, next_start
            # The reason names instants, providers or fields, never secrets.
            log.warning("routing snapshot compile refused: %s", exc)
            raise SnapshotUnavailable("snapshot-refused", next_start, now) from None
        except Exception as exc:
            # Anything else may be transient: retry after a backoff that
            # doubles with each failure in this window, up to its end (a
            # deterministic failure is then tried once a window). Logged by
            # its class alone, the worker's own for a worker failure.
            self._cached = None
            window, count = self._failures
            count = count + 1 if window == start else 1
            self._failures = (start, count)
            backoff = ERROR_BACKOFF_SECONDS << min(count - 1, 16)
            self._error_until = max(now + 1, min(now + backoff, next_start))
            log.warning("routing snapshot compile failed: %s", _failure_name(exc))
            raise SnapshotUnavailable("snapshot-error", self._error_until, now) from None
        self._cached, self._failures = publication, (0, 0)
        return publication

    def _compile(self, plan: WindowPlan) -> Signed:
        self.compiles += 1
        started = time.monotonic()
        signed = plan.compile(self._signer)
        log.info(
            "routing snapshot compiled: compiled_at=%d not_after=%d elapsed_ms=%d",
            signed.compiled_at, signed.not_after, int((time.monotonic() - started) * 1000),
        )
        return signed

    def _publication(self, signed: Signed, next_start: int) -> Publication:
        body, manifest = signed.body, signed.manifest
        refresh_at = min(next_start, signed.not_after)
        sha256 = hashlib.sha256(body).hexdigest()
        response = json.dumps(
            {
                "v": 1,
                "kind": RESPONSE_KIND,
                "manifest": manifest,
                "body": base64.b64encode(body).decode("ascii"),
                "refresh_at": refresh_at,
            },
            sort_keys=True, separators=(",", ":"),
        ).encode("ascii")
        log.info("routing snapshot published: sha256=%s size=%d refresh_at=%d",
                 sha256, len(body), refresh_at)
        return Publication(
            compiled_at=signed.compiled_at, not_after=signed.not_after,
            refresh_at=refresh_at, sha256=sha256, body=body, manifest=manifest,
            response=response,
        )


def plane_of(settings: Settings) -> Plane:
    # GCP only: the other clouds stay on Python until E5's decision.
    return Plane(cloud="gcp", region=settings.effective_serving_region)


def build_publisher(
    settings: Settings,
    *,
    inputs: Callable[[], CatalogInputs] | None = None,
    clock: Callable[[], float] = time.time,
) -> RoutingSnapshotPublisher:
    """The publisher for this process; raises ValueError when the key is unusable.

    ``inputs`` replaces the live catalog with a fixture's, compiled in process.
    """
    identity = SnapshotIdentity(
        snapshot_settings=snapshot_settings_from(settings), plane=plane_of(settings),
        source_revision=settings.source_revision or "unversioned",
    )
    source: SnapshotSource = (
        InProcessSource(inputs, identity) if inputs is not None
        else AsOfSource(identity, auto_model_order=settings.auto_model_order,
                        key_file=settings.routing_snapshot_private_key_file)
    )
    return RoutingSnapshotPublisher(signer=load_signer(settings), source=source, clock=clock)


def install(app: FastAPI, settings: Settings) -> None:
    """Put the publisher, or why there is none, on ``app.state``."""
    state = app.state
    try:
        state.routing_snapshot_publisher = build_publisher(settings)
        state.routing_snapshot_status = "serving"
    except ValueError as exc:
        state.routing_snapshot_publisher = None
        state.routing_snapshot_status = "signer-unavailable"
        # load_signer's reasons name no key material.
        log.warning("routing snapshot publisher disabled: %s", exc)
