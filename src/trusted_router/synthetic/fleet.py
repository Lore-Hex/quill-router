"""Fleet view + peer policing: the command-center substrate.

Every deployment publishes an honest self-portrait at /status.json, but until
this module nobody assembled the fleet: an operator had to open four status
pages to know whether TrustedRouter was healthy, and a cloud whose monitor
silently died told the truth ("Monitor Data Stale") to nobody in particular.

Two jobs, deliberately in one module because they share the peer list:

* ``fleet_peer_probes`` — the WATCHER-WATCHING half. Each synthetic pass
  fetches every peer's /status.json (the deployment's own public URL
  included — proving our own public serving path end to end) and records a
  ``peer_monitor`` sample per peer. Those samples ride the existing pipeline:
  three consecutive failures fire the streak alert, and the fleet page shows
  the freshness. The dead-man's switch is cross-cloud peer pressure — no new
  scheduler, no external service, no single point of failure.

* ``fleet_snapshot`` — the SEEING half, served at /fleet.json and /fleet.
  A compact merge of every peer's banner, components, and monitor freshness,
  plus this deployment's scheduler heartbeats. It deliberately extracts a
  small summary instead of embedding full payloads: the fleet view is for
  "where do I look next", not a second copy of every status page.

``peer_monitor`` samples map to no public component (see components.py), so
peer trouble never repaints THIS deployment's banner — policing the watchers
must not let a peer outage masquerade as local unavailability.

Heartbeats are ordinary synthetic samples too (``probe_type="heartbeat"``,
target = job name): recording them through the existing store means zero new
storage surface and the same retention, and the fleet page derives "this
scheduler is alive" from sample age alone.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
import time
import uuid
from typing import Any

import httpx

from trusted_router.config import Settings
from trusted_router.storage_models import SyntheticProbeSample, utcnow

logger = logging.getLogger(__name__)

PEER_MONITOR_PROBE = "peer_monitor"
HEARTBEAT_PROBE = "heartbeat"
# One fetch per peer per synthetic pass (1-3 min cadence). Keep the timeout
# well under the pass budget so a black-holed peer cannot stall the pass.
PEER_FETCH_TIMEOUT_SECONDS = 10.0
# A heartbeat older than this is a dead scheduler as far as the fleet page is
# concerned. Two synthetic cadences plus scheduling allowance, mirroring
# SILENT_PROBE_TTL_SECONDS in status.py.
HEARTBEAT_STALE_SECONDS = 11 * 60

# Heartbeat reads must use the target-leading analytics indexes. A broad
# ``probe_type=heartbeat`` read scans the entire synthetic table in ClickHouse
# because ``target`` is the first sort-key column. Keep every product heartbeat
# name here; ``record_heartbeat`` also registers local extension names.
BUILTIN_HEARTBEAT_TARGETS = frozenset(
    {
        "job:settle-outbox-drain",
        "scheduler:home-settlement",
        "scheduler:remediator",
        "scheduler:synthetic",
    }
)


def parse_fleet_peers(raw: str | None) -> list[tuple[str, str]]:
    """Parse a ``synthetic_fleet_peers`` STRING ("name=base_url,...") into pairs.

    Split out from :func:`fleet_peers` so that
    :mod:`trusted_router.operational_analytics_fleet` can read the same setting
    as a deployment source without building a ``Settings``: the peer list is a
    cloud-name-keyed table of deployments, so it binds drain-freshness coverage
    the same way the region tables do, and it must not do so through a second,
    subtly different parser.
    """
    peers: list[tuple[str, str]] = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, _, base_url = entry.partition("=")
        name = name.strip()
        base_url = base_url.strip().rstrip("/")
        if name and base_url:
            peers.append((name, base_url))
    return peers


def fleet_peers(settings: Settings) -> list[tuple[str, str]]:
    """Parse ``synthetic_fleet_peers`` ("name=base_url,...") into pairs."""
    return parse_fleet_peers(settings.synthetic_fleet_peers)


def _peer_sample(
    *,
    name: str,
    url: str,
    monitor_region: str,
    status: str,
    latency_ms: int | None,
    error_type: str | None,
    http_status: int | None,
) -> SyntheticProbeSample:
    return SyntheticProbeSample(
        id=f"syn_peer_{name}_{uuid.uuid4().hex[:12]}",
        probe_type=PEER_MONITOR_PROBE,
        target=name,
        target_url=url,
        monitor_region=monitor_region,
        status=status,
        latency_milliseconds=latency_ms,
        http_status=http_status,
        error_type=error_type,
    )


async def _probe_peer(
    client: httpx.AsyncClient,
    name: str,
    base_url: str,
    *,
    monitor_region: str,
) -> SyntheticProbeSample:
    """One peer_monitor sample: up iff the peer serves a parseable status.json
    whose OWN monitor is fresh. A reachable page with a stale monitor is DOWN
    here — "serving but blind" is exactly the state this probe exists to catch.
    """
    url = f"{base_url}/status.json"
    started = time.perf_counter()
    try:
        # Per-request timeout: the production caller hands us the shared
        # monitor client (20s budget); a black-holed peer must not eat the
        # whole pass, so the peer budget is enforced here, not on the client.
        response = await client.get(url, timeout=PEER_FETCH_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        return _peer_sample(
            name=name,
            url=url,
            monitor_region=monitor_region,
            status="down",
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_type=f"peer_unreachable:{exc.__class__.__name__}",
            http_status=None,
        )
    latency_ms = int((time.perf_counter() - started) * 1000)
    if response.status_code != 200:
        return _peer_sample(
            name=name,
            url=url,
            monitor_region=monitor_region,
            status="down",
            latency_ms=latency_ms,
            error_type="peer_bad_status",
            http_status=response.status_code,
        )
    try:
        data = response.json()["data"]
        freshness = data.get("monitor_freshness") or {}
        is_stale = bool(freshness.get("is_stale"))
    except Exception:  # noqa: BLE001 - malformed peer payload is a DOWN, not a crash
        return _peer_sample(
            name=name,
            url=url,
            monitor_region=monitor_region,
            status="down",
            latency_ms=latency_ms,
            error_type="peer_bad_payload",
            http_status=response.status_code,
        )
    if is_stale:
        return _peer_sample(
            name=name,
            url=url,
            monitor_region=monitor_region,
            status="down",
            latency_ms=latency_ms,
            error_type="peer_monitor_stale",
            http_status=response.status_code,
        )
    return _peer_sample(
        name=name,
        url=url,
        monitor_region=monitor_region,
        status="up",
        latency_ms=latency_ms,
        error_type=None,
        http_status=response.status_code,
    )


async def fleet_peer_probes(
    settings: Settings,
    *,
    monitor_region: str,
    client: httpx.AsyncClient | None = None,
) -> list[SyntheticProbeSample]:
    peers = fleet_peers(settings)
    if not peers:
        return []
    owns_client = client is None
    active = client or httpx.AsyncClient(timeout=httpx.Timeout(PEER_FETCH_TIMEOUT_SECONDS))
    try:
        return list(
            await asyncio.gather(
                *(
                    _probe_peer(active, name, base_url, monitor_region=monitor_region)
                    for name, base_url in peers
                )
            )
        )
    finally:
        if owns_client:
            await active.aclose()


# Heartbeats are bucketed to one row per (job, 5 minutes): liveness needs
# "recent beat exists", not a row per iteration — per-iteration rows from a
# 30s loop would both bloat stores with no synthetic-row deletion (Postgres)
# and let one chatty job crowd older jobs out of bounded newest-N queries,
# making a dead scheduler VANISH from /fleet instead of rendering stale.
HEARTBEAT_BUCKET_SECONDS = 5 * 60
# (job name -> last bucket recorded by THIS process); purely a write saver,
# the deterministic id is what keeps concurrent replicas to one row.
_HEARTBEAT_MARKS: dict[str, int] = {}
_REGISTERED_HEARTBEAT_TARGETS: set[str] = set()
_HEARTBEAT_LOCK = threading.RLock()


def register_heartbeat_target(name: str) -> None:
    """Register a heartbeat name for indexed fleet reads.

    Product heartbeat names belong in ``BUILTIN_HEARTBEAT_TARGETS``. This
    registry preserves the helper's usefulness for local jobs and tests
    without falling back to a full-table ``probe_type`` scan.
    """
    if name:
        with _HEARTBEAT_LOCK:
            _REGISTERED_HEARTBEAT_TARGETS.add(name)


def _heartbeat_bucket_start(bucket: int) -> str:
    return (
        dt.datetime.fromtimestamp(
            bucket * HEARTBEAT_BUCKET_SECONDS,
            tz=dt.UTC,
        )
        .isoformat()
        .replace("+00:00", "Z")
    )


def record_heartbeat(name: str, *, settings: Settings) -> None:
    """Record one liveness sample for a background job. Never raises: a
    heartbeat that can kill its own loop would be worse than no heartbeat."""
    try:
        bucket = int(time.time() // HEARTBEAT_BUCKET_SECONDS)
        with _HEARTBEAT_LOCK:
            _REGISTERED_HEARTBEAT_TARGETS.add(name)
            if _HEARTBEAT_MARKS.get(name) == bucket:
                return
            from trusted_router.storage import STORE

            bucket_start = _heartbeat_bucket_start(bucket)
            STORE.record_synthetic_probe_sample(
                SyntheticProbeSample(
                    id=f"syn_hb_{name.replace(':', '_')}_{bucket}",
                    probe_type=HEARTBEAT_PROBE,
                    target=name,
                    target_url="",
                    monitor_region=(settings.synthetic_monitor_region or settings.primary_region),
                    status="up",
                    created_at=bucket_start,
                )
            )
            # Publish the read-your-write mark only after the store accepted
            # the sample. The lock also collapses concurrent gateway calls
            # into one heartbeat row for this five-minute bucket.
            _HEARTBEAT_MARKS[name] = bucket
    except Exception:  # noqa: BLE001 - liveness reporting must not break the job
        logger.exception("heartbeat record failed for %s", name)


def reset_for_tests() -> None:
    with _HEARTBEAT_LOCK:
        _HEARTBEAT_MARKS.clear()
        _REGISTERED_HEARTBEAT_TARGETS.clear()


def _age_seconds(created_at: str) -> float | None:
    import datetime as dt

    try:
        created = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (utcnow() - created).total_seconds())


def _heartbeat_rows() -> list[dict[str, Any]]:
    from trusted_router.storage import STORE

    # The durable analytics pipeline is intentionally asynchronous. Merge the
    # successful process-local write marks so a newly rolled instance cannot
    # page on an old ClickHouse snapshot immediately after publishing its own
    # heartbeat. Durable samples remain authoritative after process restart.
    latest: dict[str, str] = {}
    with _HEARTBEAT_LOCK:
        targets = BUILTIN_HEARTBEAT_TARGETS | _REGISTERED_HEARTBEAT_TARGETS
        local_marks = dict(_HEARTBEAT_MARKS)
    for name in sorted(targets):
        samples = STORE.synthetic_probe_samples(
            target=name,
            probe_type=HEARTBEAT_PROBE,
            limit=1,
        )
        if samples:
            latest[name] = samples[0].created_at
    for name, bucket in local_marks.items():
        local_created_at = _heartbeat_bucket_start(bucket)
        if local_created_at > latest.get(name, ""):
            latest[name] = local_created_at

    rows = []
    for name, created_at in sorted(latest.items()):
        age = _age_seconds(created_at)
        rows.append(
            {
                "name": name,
                "last_beat_at": created_at,
                "age_seconds": round(age) if age is not None else None,
                "stale": age is None or age > HEARTBEAT_STALE_SECONDS,
            }
        )
    return rows


def _peer_summary(name: str, base_url: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {
            "name": name,
            "url": f"{base_url}/status.json",
            "reachable": False,
            "overall_status": "unreachable",
            "headline": "Unreachable",
            "components": {},
            "monitor_stale": None,
            "generated_at": None,
        }
    # A peer payload is REMOTE input: one deployment shipping a malformed
    # status.json must degrade to one weird row on everyone's fleet page,
    # never a 500 of the fleet page itself.
    try:
        freshness = payload.get("monitor_freshness")
        summary = payload.get("summary")
        components = payload.get("components")
        return {
            "name": name,
            "url": f"{base_url}/status.json",
            "reachable": True,
            "overall_status": str(payload.get("overall_status") or "unknown"),
            "headline": str((summary.get("headline") if isinstance(summary, dict) else None) or ""),
            "components": {
                str(row.get("id")): str(row.get("status"))
                for row in (components if isinstance(components, list) else [])
                if isinstance(row, dict)
            },
            "monitor_stale": bool(freshness.get("is_stale"))
            if isinstance(freshness, dict)
            else None,
            "generated_at": payload.get("generated_at"),
        }
    except Exception:  # noqa: BLE001 - malformed peer data renders, not raises
        logger.exception("unparseable peer status payload from %s", name)
        return {
            "name": name,
            "url": f"{base_url}/status.json",
            "reachable": True,
            "overall_status": "unknown",
            "headline": "Unparseable status payload",
            "components": {},
            "monitor_stale": None,
            "generated_at": None,
        }


_FLEET_SEVERITY = {
    "up": 0,
    "degraded": 1,
    "routing_degraded": 1,
    "trust_degraded": 2,
    "down": 3,
    "unreachable": 3,
    "unknown": 1,
}


def _fleet_overall(summaries: list[dict[str, Any]]) -> str:
    worst = "up"
    for row in summaries:
        status = str(row["overall_status"])
        if row.get("monitor_stale"):
            status = "degraded" if _FLEET_SEVERITY.get(status, 0) < 1 else status
        if _FLEET_SEVERITY.get(status, 1) > _FLEET_SEVERITY.get(worst, 0):
            worst = status
    return worst


async def fleet_snapshot(
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """The merged fleet view served at /fleet.json.

    In the test environment peers are only fetched through an explicitly
    injected client (tests pass a MockTransport-backed one); without it the
    peer list is treated as empty so no test ever touches the network.
    """
    peers = fleet_peers(settings)
    if settings.environment == "test" and client is None:
        peers = []
    payloads: dict[str, dict[str, Any] | None] = {}
    if peers:
        owns_client = client is None
        active = client or httpx.AsyncClient(timeout=httpx.Timeout(PEER_FETCH_TIMEOUT_SECONDS))

        async def fetch(name: str, base_url: str) -> None:
            try:
                response = await active.get(
                    f"{base_url}/status.json", timeout=PEER_FETCH_TIMEOUT_SECONDS
                )
                data = response.json()["data"] if response.status_code == 200 else None
                payloads[name] = data if isinstance(data, dict) else None
            except Exception:  # noqa: BLE001 - a down peer is data, not an error
                payloads[name] = None

        try:
            await asyncio.gather(*(fetch(name, url) for name, url in peers))
        finally:
            if owns_client:
                await active.aclose()
    summaries = [_peer_summary(name, url, payloads.get(name)) for name, url in peers]
    from trusted_router.storage_models import iso_now

    return {
        "generated_at": iso_now(),
        "fleet_overall_status": _fleet_overall(summaries) if summaries else "unknown",
        "deployments": summaries,
        # Blocking storage reads — off the event loop, same rule as every
        # other sync store call on a request path (head-of-line blocking).
        "heartbeats": await asyncio.to_thread(_heartbeat_rows),
        "remediator": {
            "mode": settings.remediator_mode,
            "decisions": await asyncio.to_thread(_recent_decisions_safe),
        },
    }


def _recent_decisions_safe() -> list[dict[str, Any]]:
    # The fleet page must render even if the remediator surface breaks.
    try:
        from trusted_router.synthetic.remediator import recent_decisions

        return recent_decisions()
    except Exception:  # noqa: BLE001 - fleet view over remediator introspection
        logger.exception("remediator decision read failed")
        return []
