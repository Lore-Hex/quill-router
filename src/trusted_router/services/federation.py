"""Lazy API-key federation for standalone regional planes.

A standalone plane (aws.trustedrouter.com) owns its own database, credits
and TLS identity. What it does NOT own is the user directory: keys are
issued on the home plane. Federation closes that gap without giving up
independence.

The shape, and why each piece is load-bearing:

  * IDENTITY federates, CREDITS DO NOT. An identity is an assertion that
    can be copied safely. Credits are a quantity with a conservation law —
    copying them mints money. A federated key therefore arrives with its
    configuration and ZERO local balance; spending on this plane requires
    an explicit transfer.

  * LAZY, not synced. The first request for an unknown key asks the home
    plane once; every request after that is a local database read. A
    background sync would need a directory-wide feed and would be one more
    thing to silently stop.

  * STALE-WHILE-REVALIDATE. Past the soft TTL a refresh is scheduled but
    the cached record still serves. Blocking inference on a refresh would
    make this plane's availability a function of the home plane's, which
    is precisely what the separation exists to prevent.

  * A CIRCUIT BREAKER, so a home-plane outage cannot exhaust this plane's
    worker pool. Without it a US outage takes EU down with it — the exact
    coupling being removed.

  * UNKNOWN key during a home outage is 503, never 401. A 401 tells a
    paying customer their key is bad, which costs them a rotation and a
    support ticket over what is our outage.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# A federated record is trusted for this long before a refresh is
# scheduled; it keeps SERVING while that refresh happens.
SOFT_TTL_SECONDS = 15 * 60
# Past this, the record is refused. A full day of home-plane outage is
# survivable; a week of serving a key that may have been revoked is not.
HARD_TTL_SECONDS = 24 * 60 * 60
# A key the home plane says it does not know is remembered as unknown for
# this long, so a leaked or rotated key in a retry loop cannot turn into
# sustained cross-border QPS.
NEGATIVE_TTL_SECONDS = 60
# Consecutive failures before the breaker opens, and how long it stays open.
BREAKER_THRESHOLD = 5
BREAKER_COOLDOWN_SECONDS = 30
# Deliberately tight: this budget is spent inline on a customer request.
CONNECT_TIMEOUT_SECONDS = 1.0
TOTAL_TIMEOUT_SECONDS = 2.5


class FederationUnavailable(RuntimeError):
    """The home plane could not be reached and no usable cache exists.

    Callers must translate this to 503 + Retry-After — never 401.
    """


@dataclass
class _InFlight:
    """One in-progress resolve, shared by every caller waiting on it."""

    done: threading.Event = field(default_factory=threading.Event)
    record: dict[str, Any] | None = None
    error: Exception | None = None


@dataclass
class _Breaker:
    failures: int = 0
    opened_at: float = 0.0

    def is_open(self, now: float) -> bool:
        if self.failures < BREAKER_THRESHOLD:
            return False
        if now - self.opened_at >= BREAKER_COOLDOWN_SECONDS:
            # Half-open: allow one probe through by resetting the count.
            self.failures = 0
            return False
        return True

    def record_failure(self, now: float) -> None:
        self.failures += 1
        if self.failures >= BREAKER_THRESHOLD:
            self.opened_at = now

    def record_success(self) -> None:
        self.failures = 0


class FederationClient:
    """Resolves unknown keys from the home plane, once, with a breaker.

    Single-flight is keyed on the lookup hash: a burst of concurrent
    requests for the same cold key produces ONE home-plane call, not N.
    Without it, a popular key arriving on a cold plane would fan out into
    a thundering herd against the very plane we are trying not to depend
    on.
    """

    def __init__(
        self,
        *,
        home_base_url: str,
        peer_token: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._home = home_base_url.rstrip("/")
        self._peer_token = peer_token
        self._client = client
        self._lock = threading.Lock()
        self._inflight: dict[str, _InFlight] = {}
        self._negative: dict[str, float] = {}
        self._breaker = _Breaker()

    def resolve(self, lookup_hash: str) -> dict[str, Any] | None:
        """Fetch a key's federated record, or None if the home plane
        genuinely does not know it.

        Raises FederationUnavailable when the home plane is unreachable.
        """
        now = time.monotonic()

        negative_until = self._negative.get(lookup_hash)
        if negative_until is not None and now < negative_until:
            return None

        if self._breaker.is_open(now):
            raise FederationUnavailable("home plane circuit breaker is open")

        # Single-flight: the first caller fetches and PUBLISHES its result;
        # the rest wait and read it. Followers must not re-fetch — that was
        # the bug this shape exists to prevent, and it would turn a burst on
        # one cold key into N calls against the plane we are trying not to
        # depend on.
        with self._lock:
            inflight = self._inflight.get(lookup_hash)
            leader = inflight is None
            if leader:
                inflight = _InFlight()
                self._inflight[lookup_hash] = inflight

        assert inflight is not None
        if not leader:
            # Bounded wait: a hung leader must not pin every follower.
            if not inflight.done.wait(timeout=TOTAL_TIMEOUT_SECONDS):
                raise FederationUnavailable("timed out waiting on an in-flight resolve")
            if inflight.error is not None:
                raise FederationUnavailable(str(inflight.error))
            return inflight.record

        try:
            record = self._resolve_uncached(lookup_hash)
            if record is None:
                self._negative[lookup_hash] = time.monotonic() + NEGATIVE_TTL_SECONDS
            self._breaker.record_success()
            inflight.record = record
            return record
        except FederationUnavailable as exc:
            self._breaker.record_failure(time.monotonic())
            inflight.error = exc
            raise
        finally:
            with self._lock:
                self._inflight.pop(lookup_hash, None)
            inflight.done.set()

    def _resolve_uncached(self, lookup_hash: str) -> dict[str, Any] | None:
        url = f"{self._home}/v1/internal/federation/resolve-key"
        timeout = httpx.Timeout(TOTAL_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
        client = self._client
        try:
            if client is None:
                with httpx.Client(timeout=timeout) as owned:
                    response = owned.post(
                        url,
                        json={"api_key_lookup_hash": lookup_hash},
                        headers={"x-trustedrouter-federation-token": self._peer_token},
                    )
            else:
                response = client.post(
                    url,
                    json={"api_key_lookup_hash": lookup_hash},
                    headers={"x-trustedrouter-federation-token": self._peer_token},
                )
        except httpx.HTTPError as exc:
            raise FederationUnavailable(f"home plane unreachable: {exc}") from exc

        if response.status_code == 404:
            return None
        if response.status_code >= 500:
            # Including a home plane that is up but broken: treat as
            # unavailable so the breaker sees it, rather than as "no such key".
            raise FederationUnavailable(f"home plane returned {response.status_code}")
        if response.status_code != 200:
            # 401/403 here means OUR peer token is wrong. That is a
            # misconfiguration on this plane, not a verdict about the key,
            # so it must not be cached as a negative.
            raise FederationUnavailable(f"home plane rejected the peer token ({response.status_code})")
        try:
            payload = response.json()
        except ValueError as exc:
            raise FederationUnavailable("home plane returned a non-JSON body") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        return data if isinstance(data, dict) else None
