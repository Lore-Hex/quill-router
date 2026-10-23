"""Publishes the signed edge policy to the fast path, dark (E2 P2).

docs/design/fast-admission-e2-decide.md section 4 (the contract) and
docs/design/routing-snapshot-v1.md, "The edge policy". Imported only when
``edge_policy_publish_enabled`` is on; nothing else at runtime reads it.

The body (trusted_router/edge_policy.py) is the live control-plane facts the
edge gates on before it would answer an authorize itself: the accepted image
digests (the verified Stage D policy plus the break-glass override, the set
authorize checks a boot against), the Stage D settings, whether the settle
records are durable, the pilot's gate, the per-key in-flight limit, the
monitor key's lookup hash, the async-settle shadow cohort and the regions.
The manifest (routing_snapshot_signing.py, ``EDGE_POLICY_TYP``) is signed
with the routing snapshot's key under its own ``typ``.

A body is issued at the publisher's clock and signed at most once per
refresh interval: every poll until its ``refresh_at`` (+30 s) is served that
publication, and the first poll at or after it reads the facts afresh and
signs a new one. The edge polls at ``refresh_at`` and trusts a body until
its ``not_after`` (+120 s), so three missed polls leave the edge on a copy it
still trusts and a fourth leaves it on none (it then proxies everything to
Python). A changed fact therefore reaches a polling edge within 30 s.

Unlike the routing snapshot there is no worker process: every fact is this
process's own live state (its settings, its Stage D policy resolver, its
store), which is the point, and the body is a few hundred bytes, so building
and signing it costs well under a millisecond. Reading the facts kicks a due
Stage D policy refresh without waiting on it, as authorize does.

Nothing here logs or returns key material: the signing key is read from its
mounted file at construction and never leaves this process; responses carry
the body and the manifest, logs carry instants, sizes, digests and exception
classes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from trusted_router.edge_policy import (
    KIND,
    LIFETIME_SECONDS,
    REFRESH_SECONDS,
    EdgePolicyV1,
    canonical_edge_policy_bytes,
)
from trusted_router.regions import configured_regions
from trusted_router.routing_snapshot_signing import EdgePolicySigner
from trusted_router.services.fastpath_frontdoor import records_durable
from trusted_router.services.routing_snapshot_publisher import load_signer, plane_of
from trusted_router.stage_d_policy import accepted_image_digests
from trusted_router.synthetic.funding import monitor_lookup_hash

if TYPE_CHECKING:
    from fastapi import FastAPI

    from trusted_router.config import Settings
    from trusted_router.stage_d_policy import StageDPolicyResolver

log = logging.getLogger(__name__)

RESPONSE_KIND = "tr-edge-policy-publication"
# After a failure to read the facts or to sign, the edge is told to come back
# this soon; it keeps its last good copy until that copy's not_after.
ERROR_RETRY_SECONDS = 5


class PolicyUnavailable(Exception):
    """No policy can be served now; the caller answers 503."""

    def __init__(self, status: str, retry_at: int, now: int) -> None:
        super().__init__(status)
        self.status = status
        self.retry_at = retry_at
        self.retry_after = max(1, retry_at - now)


@dataclass(frozen=True)
class Publication:
    issued_at: int
    refresh_at: int
    not_after: int
    sha256: str
    body: bytes
    manifest: str
    response: bytes

    def current(self, now: int) -> bool:
        return self.issued_at <= now < self.refresh_at


def settings_facts(
    settings: Settings, *, accepted: frozenset[str], durable: bool,
) -> dict[str, Any]:
    """Every body field but the version, kind, plane and window, from these
    settings and the two live facts that are not settings."""
    return {
        "accepted_image_digests": sorted(accepted),
        "stage_d": {
            "eligibility_enabled": settings.stage_d_eligibility_enabled,
            "heartbeat_enabled": settings.stage_d_heartbeat_enabled,
            "require_heartbeat_declaration": settings.stage_d_require_heartbeat_declaration,
            "pilot_workspaces": sorted(settings.stage_d_pilot_workspaces),
        },
        "records_durable": durable,
        "fastpath_pilot_enabled": settings.fastpath_pilot_enabled,
        "fastpath_pilot_workspaces": sorted(settings.fastpath_pilot_workspace_ids),
        "fastpath_pilot_regions": sorted(settings.fastpath_pilot_region_ids),
        "gateway_authorize_max_in_flight_per_key": settings.gateway_authorize_max_in_flight_per_key,
        "monitor_lookup_hash": monitor_lookup_hash(settings),
        "async_settle_shadow_policy": settings.async_settle_shadow_policy,
        "async_settle_shadow_workspaces": sorted(settings.async_settle_shadow_workspace_ids),
        "configured_regions": configured_regions(settings),
        "primary_region": settings.primary_region,
        "multi_region_enabled": settings.multi_region_enabled,
    }


def build_policy(plane: BaseModel, issued_at: int, facts: dict[str, Any]) -> EdgePolicyV1:
    """The body for a window: validated, so a fact outside the schema fails here."""
    return EdgePolicyV1.model_validate({
        "v": 1,
        "kind": KIND,
        "plane": plane.model_dump(mode="json"),
        "issued_at": issued_at,
        "refresh_at": issued_at + REFRESH_SECONDS,
        "not_after": issued_at + LIFETIME_SECONDS,
        **facts,
    })


class LiveFacts:
    """This process's facts, read afresh at every call."""

    def __init__(
        self, settings: Settings, *,
        resolver: Callable[[], StageDPolicyResolver | None],
        typed_store: Callable[[], Any],
    ) -> None:
        self._settings, self._resolver, self._typed_store = settings, resolver, typed_store

    def __call__(self) -> dict[str, Any]:
        settings = self._settings
        return settings_facts(
            settings,
            accepted=accepted_image_digests(self._resolver(), settings),
            durable=records_durable(settings.settle_outbox_enabled, self._typed_store()),
        )


class EdgePolicyPublisher:
    """Builds, signs and caches the edge policy for the current window."""

    def __init__(
        self, *, signer: EdgePolicySigner, plane: BaseModel,
        facts: Callable[[], dict[str, Any]], clock: Callable[[], float] = time.time,
    ) -> None:
        self._signer, self._plane, self._facts, self._clock = signer, plane, facts, clock
        self._lock = threading.Lock()
        self._cached: Publication | None = None
        self.signatures = 0

    def current(self) -> Publication:
        """The current window's publication; raises PolicyUnavailable."""
        with self._lock:
            now = int(self._clock())
            cached = self._cached
            if cached is not None and cached.current(now):
                return cached
            try:
                publication = self._publish(now, self._facts())
            except Exception as exc:
                # Logged by class alone: a message could carry a setting's value.
                log.warning("edge policy publication failed: %s", type(exc).__name__)
                raise PolicyUnavailable("policy-error", now + ERROR_RETRY_SECONDS, now) from None
            self._cached = publication
            return publication

    def _publish(self, issued_at: int, facts: dict[str, Any]) -> Publication:
        policy = build_policy(self._plane, issued_at, facts)
        body = canonical_edge_policy_bytes(policy)
        manifest = self._signer.sign(body)
        self.signatures += 1
        sha256 = hashlib.sha256(body).hexdigest()
        response = json.dumps(
            {
                "v": 1,
                "kind": RESPONSE_KIND,
                "manifest": manifest,
                "body": base64.b64encode(body).decode("ascii"),
            },
            sort_keys=True, separators=(",", ":"),
        ).encode("ascii")
        log.info("edge policy published: sha256=%s size=%d issued_at=%d",
                 sha256, len(body), issued_at)
        return Publication(
            issued_at=issued_at, refresh_at=policy.refresh_at, not_after=policy.not_after,
            sha256=sha256, body=body, manifest=manifest, response=response,
        )


def _typed_store() -> Any:
    from trusted_router.storage import STORE, typed_billing_store

    return typed_billing_store(STORE)


def build_publisher(
    settings: Settings, *,
    resolver: Callable[[], StageDPolicyResolver | None] = lambda: None,
    typed_store: Callable[[], Any] = _typed_store,
    facts: Callable[[], dict[str, Any]] | None = None,
    clock: Callable[[], float] = time.time,
) -> EdgePolicyPublisher:
    """The publisher for this process; raises ValueError when the routing key is unusable.

    ``facts`` replaces the live facts (tests).
    """
    signer = EdgePolicySigner(load_signer(settings))
    return EdgePolicyPublisher(
        signer=signer, plane=plane_of(settings),
        facts=facts or LiveFacts(settings, resolver=resolver, typed_store=typed_store),
        clock=clock,
    )


def install(app: FastAPI, settings: Settings) -> None:
    """Put the publisher, or why there is none, on ``app.state``.

    The Stage D policy resolver is read from ``app.state`` at each poll: it is
    installed after this runs.
    """
    state = app.state
    try:
        state.edge_policy_publisher = build_publisher(
            settings, resolver=lambda: getattr(state, "stage_d_policy_resolver", None),
        )
        state.edge_policy_status = "serving"
    except ValueError as exc:
        state.edge_policy_publisher = None
        state.edge_policy_status = "signer-unavailable"
        # load_signer's reasons name no key material.
        log.warning("edge policy publisher disabled: %s", exc)
