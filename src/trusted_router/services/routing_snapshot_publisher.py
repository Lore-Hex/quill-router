"""Publishes the signed routing snapshot to the fast path, dark (E2, "Publishing").

docs/design/routing-snapshot-v1.md, "Publishing". Imported only when
``routing_snapshot_publish_enabled`` is on; nothing else at runtime reads it.

A window starts on the hour, or at the latest retirement or catalog deadline
of the hour that has passed (``routing_snapshot.publication_window``): the
instants at which a window compiled earlier in the hour ends. Every instance of
a revision, cold or warm, derives the same start for the same instant, so it
produces identical bytes and an identical manifest (Ed25519 signatures are
deterministic). A publication is served until the next hour or its
``not_after``, whichever comes first, and the next poll after that compiles the
next window. A window whose compile the catalog refuses is not compiled again;
the next window start retries. Any other failure is retried after a few
seconds. An instance imported before a retirement cannot compile a window
starting after it (the compiler refuses, so two instances never publish two
documents for one window); it answers 503 until it is replaced, and the
windows it published before end at that retirement. Publishing stays off until
the catalog is rebuilt as of the window's start (the design doc, "Not yet").

Compiling takes 1.5 to 4.5 s of CPU on development machines (4.3 s measured in
review). On the combined service it holds the GIL against authorize for that
long, once a window per polled instance: measure it there before enabling. It
runs lazily, single-flight under a lock, in the threadpool worker of the first
poll that finds no current snapshot; never on the event loop, never on
authorize. Polls that arrive meanwhile wait for it and are then served the same
cached bytes.

Nothing here logs or returns key material: the signing key is read from its
mounted file once, at construction; responses carry the body and the manifest,
logs carry instants, sizes and digests.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trusted_router.detached_jws import TrustedKey, b64encode
from trusted_router.routing_snapshot import (
    EPOCH_SECONDS,
    CatalogInputs,
    Plane,
    RoutingSnapshotV1,
    SnapshotRefused,
    SnapshotSettings,
    canonical_snapshot_bytes,
    compile_routing_snapshot,
    publication_window,
    snapshot_settings_from,
)
from trusted_router.routing_snapshot_signing import PURPOSE, RoutingSnapshotSigner

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


class RoutingSnapshotPublisher:
    """Compiles, signs and caches the snapshot for the current window."""

    def __init__(
        self,
        *,
        signer: RoutingSnapshotSigner,
        plane: Plane,
        source_revision: str,
        settings: SnapshotSettings,
        inputs: Callable[[], CatalogInputs],
        clock: Callable[[], float] = time.time,
        horizon: int = HORIZON_SECONDS,
    ) -> None:
        self._signer = signer
        self._plane = plane
        self._source_revision = source_revision
        self._settings = settings
        self._inputs = inputs
        self._clock = clock
        self._horizon = horizon
        self._lock = threading.Lock()
        self._cached: Publication | None = None
        # A refused window's end: no load or compile before the next window
        # start (the window start is constant until then).
        self._refused_until = 0
        # After an unexpected failure, no compile before this instant.
        self._error_until = 0
        self.compiles = 0

    @property
    def trusted_keys(self) -> Sequence[TrustedKey]:
        return (self._signer.trusted,)

    def current(self) -> Publication:
        """The current window's publication; raises SnapshotUnavailable."""
        cached = self._cached
        if cached is not None and cached.current(int(self._clock())):
            return cached
        with self._lock:
            now = int(self._clock())
            cached = self._cached
            if cached is not None and cached.current(now):
                return cached
            if now < self._refused_until:
                raise SnapshotUnavailable("snapshot-refused", self._refused_until, now)
            if now < self._error_until:
                raise SnapshotUnavailable("snapshot-error", self._error_until, now)
            # Until the inputs name the window, a refusal holds for the hour.
            hour = now - now % EPOCH_SECONDS
            next_start = hour + EPOCH_SECONDS
            try:
                inputs = self._inputs()
                start, next_start = publication_window(now, inputs)
                snapshot = self._compile(start, inputs)
                if not now < snapshot.not_after:
                    raise SnapshotRefused("the window ended before it was published")
                publication = self._sign(snapshot, next_start)
            except SnapshotRefused as exc:
                # The catalog refuses this window for good: an event between
                # its start and the process's clocks or import, or (from the
                # inputs) a catalog shape the compiler does not know, both
                # facts of this process that a later attempt would meet again.
                # The next window start retries.
                self._cached, self._refused_until = None, next_start
                # The reason names instants, providers or fields, never secrets.
                log.warning("routing snapshot compile refused: %s", exc)
                raise SnapshotUnavailable("snapshot-refused", next_start, now) from None
            except Exception as exc:
                # Anything else may be transient: retry after a short backoff,
                # logged by its class alone.
                self._cached = None
                self._error_until = now + ERROR_BACKOFF_SECONDS
                log.warning("routing snapshot compile failed: %s", type(exc).__name__)
                raise SnapshotUnavailable("snapshot-error", self._error_until, now) from None
            self._cached = publication
            return publication

    def _compile(self, at: int, inputs: CatalogInputs) -> RoutingSnapshotV1:
        self.compiles += 1
        started = time.monotonic()
        snapshot = compile_routing_snapshot(
            at=at, horizon=self._horizon, settings=self._settings,
            source_revision=self._source_revision, plane=self._plane, inputs=inputs,
        )
        log.info(
            "routing snapshot compiled: compiled_at=%d not_after=%d elapsed_ms=%d",
            snapshot.compiled_at, snapshot.not_after, int((time.monotonic() - started) * 1000),
        )
        return snapshot

    def _sign(self, snapshot: RoutingSnapshotV1, next_start: int) -> Publication:
        body = canonical_snapshot_bytes(snapshot)
        manifest = self._signer.sign(body)
        refresh_at = min(next_start, snapshot.not_after)
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
            compiled_at=snapshot.compiled_at, not_after=snapshot.not_after,
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
    """The publisher for this process; raises ValueError when the key is unusable."""
    return RoutingSnapshotPublisher(
        signer=load_signer(settings),
        plane=plane_of(settings),
        source_revision=settings.source_revision or "unversioned",
        settings=snapshot_settings_from(settings),
        inputs=inputs or (lambda: CatalogInputs.live(settings)),
        clock=clock,
    )


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
