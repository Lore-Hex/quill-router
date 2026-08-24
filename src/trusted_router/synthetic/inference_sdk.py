"""The monitor's TrustedRouter Python SDK session for its inference probes.

The pong probes (``openai_sdk_pong``, ``responses_pong``) are the monitor's
only real model calls, and they used to go out as raw ``httpx`` POSTs. That
left the SDK's client-telemetry path -- the per-attempt ``x-tr-client`` header
the enclave forwards into settle context, and the beacon the SDK posts to
``/v1/client-events`` -- with no traffic from our own monitor: not one
``client_source='tr'`` row had ever settled in production, and the canary
batch the monitor posts (``probes.client_telemetry_canary_probe``) is
hand-built, so it proves ingest liveness and nothing about the SDK. Sending
the probes through the official SDK makes every pass exercise that path end
to end.

The session is configured to preserve what the probe measures:

* exactly one attempt per probe: ``max_retries=0``. ``regional_failover`` is
  off as well, although a non-default base URL has a single candidate and
  could not move anyway;
* the monitor's own ``httpx.AsyncClient`` is injected, so a probe keeps its
  configured timeout and its TLS context (attested targets trust the
  TEE-minted certificate through the attestation probe, not through a CA);
* ``regional_affinity=False``: the target IS the region; the SDK must never
  race the regional health endpoints to pick one for us;
* telemetry explicitly on, at a 1.0 sample rate. The monitor wants every
  event, and the SDK's environment opt-outs (``TRUSTEDROUTER_TELEMETRY``,
  ``DO_NOT_TRACK``) must not silence it; an explicit ``telemetry=True`` wins
  over both.

The request bodies are byte-for-byte what the raw-httpx probes sent: the
gateway's Responses validator rejects unknown top-level keys with HTTP 501.
Synthetic marking in the body uses ``metadata.trustedrouter_synthetic``.
The beacon is sent with the monitor key, which ``/v1/client-events`` identifies
server-side as synthetic regardless of the batch's client-supplied bit. No
client honesty is required on either channel.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from trustedrouter import AsyncTrustedRouter, TrustedRouterError

log = logging.getLogger(__name__)

DEFAULT_CONTROL_PLANE_BASE_URL = "https://trustedrouter.com"
# Every synthetic event is wanted. The SDK's default (0.01) keeps one success
# in a hundred, which would leave the monitor's own beacons nearly empty.
MONITOR_TELEMETRY_SAMPLE_RATE = 1.0
# How long ``aclose()`` joins the reporter's final flush (one bounded POST).
SDK_CLOSE_TIMEOUT_SECONDS = 2.0


def build_inference_sdk(
    api_base_url: str,
    *,
    api_key: str,
    http_client: httpx.AsyncClient,
    control_plane_base_url: str,
) -> AsyncTrustedRouter:
    """One SDK session for one probe target, on the monitor's own HTTP client."""
    return AsyncTrustedRouter(
        api_key,
        base_url=api_base_url,
        control_base_url=f"{control_plane_base_url.rstrip('/')}/v1",
        client=http_client,
        max_retries=0,
        regional_failover=False,
        regional_affinity=False,
        telemetry=True,
        telemetry_sample_rate=MONITOR_TELEMETRY_SAMPLE_RATE,
    )


async def close_inference_sdk(sdk: AsyncTrustedRouter) -> None:
    """Flush the session's telemetry reporter without stalling the event loop.

    ``aclose()`` joins the reporter's final flush for up to
    :data:`SDK_CLOSE_TIMEOUT_SECONDS`. Other probes (ledger, rotation) may
    still be timing requests on this loop, and a two-second stall would land
    in their latencies, so the join runs in a worker thread. The session owns
    no loop-bound resource (the HTTP client is the monitor's), which is what
    makes running ``aclose()`` on a private loop in that thread safe.
    Telemetry never fails a pass: an error here is logged and swallowed, and
    the SDK's own atexit hook is the backstop for a reporter left unclosed.
    """
    try:
        await asyncio.to_thread(asyncio.run, sdk.aclose())
    except Exception:  # noqa: BLE001 - telemetry must never fail a probe pass
        log.warning("synthetic.sdk_close_failed", exc_info=True)


@dataclass(frozen=True)
class SdkFailure:
    """The monitor's reading of an SDK exception, in the pre-SDK vocabulary."""

    error_type: str
    http_status: int | None
    # Whether a response reached the probe, i.e. whether a time-to-first-byte
    # exists for the sample. False for transport failures.
    response_received: bool


def classify_sdk_failure(exc: BaseException) -> SdkFailure:
    """Map an SDK exception back onto the raw-httpx probes' taxonomy.

    Those probes had exactly three outcomes: a parsed response (``up``, or
    ``pong_mismatch`` carrying the real HTTP status -- they never raised on a
    status), an ``httpx.HTTPError`` (``error_type`` is its class name, no
    status), or an exception escaping the pass. The SDK folds the first two
    into typed errors, and this undoes the fold:

    * a transport failure arrives as ``InternalError(503)`` raised ``from``
      the httpx exception. The cause chain, not the status, tells it apart
      from a real 503, and the httpx class name is kept;
    * any other ``TrustedRouterError`` is a response the probe could not
      accept: ``pong_mismatch`` with the status the SDK saw. A 2xx whose JSON
      is not an object lands here too, status intact;
    * a successful body that is not JSON surfaces as the bare ``ValueError``;
      this path is only reached after ``response.is_success``, and these two
      gateway endpoints' success contract is 200, so the old status is kept;
    * anything else is an SDK-internal failure the old probes could not have
      had. It becomes a down sample named after the exception rather than a
      crashed pass, so the anomaly shows on the page instead of as a gap.
    """
    transport = _transport_cause(exc)
    if transport is not None:
        return SdkFailure(type(transport).__name__, None, False)
    if isinstance(exc, TrustedRouterError):
        return SdkFailure("pong_mismatch", exc.status_code, True)
    if isinstance(exc, ValueError):
        return SdkFailure("pong_mismatch", 200, True)
    return SdkFailure(type(exc).__name__, None, False)


def _transport_cause(exc: BaseException) -> httpx.HTTPError | None:
    """The httpx error behind ``exc``, following explicit causes only.

    ``__context__`` is deliberately not followed: a status error raised inside
    the SDK's JSON handling has the decode error as implicit context, and the
    response status, not a transport failure, is the truth there.
    """
    current: BaseException | None = exc
    for _ in range(6):
        if current is None:
            return None
        if isinstance(current, httpx.HTTPError):
            return current
        current = current.__cause__
    return None
