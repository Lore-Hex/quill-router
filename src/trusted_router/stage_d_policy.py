"""Signed runtime resolver for the GCP Stage D accepted-image policy."""

from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urljoin

import httpcore
import httpx

from trusted_router.config import Settings

logger = logging.getLogger(__name__)

_POLICY_PATH = "gcp/stage-d-accepted.json"
_POLICY_KEYS = frozenset(
    {"schema", "plane", "sequence", "kind", "issued_at", "image_digests"}
)
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_RFC3339_UTC_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z"
)
_MAX_DOCUMENT_BYTES = 64 * 1024
_MAX_BUNDLE_BYTES = 2 * 1024 * 1024
# Each release URL's two fetches end within this: every connect, TLS
# handshake, read and write gets the time left before the URL's deadline
# (_DeadlineBackend), so a host that drips header bytes or empty deflate
# blocks times out and the next URL still runs in the same refresh. Name
# resolution, and a connect's tries of a host's further addresses, are
# bounded only by the refresh's join below.
_FETCH_TIMEOUT_SECONDS = 5.0
# A refresh's network part (the fetches and the Sigstore check, which runs a
# TUF refresh, URL by URL in one short-lived thread) fails once that thread
# has run this long: room for a hung primary, a fallback and the checks.
REFRESH_TIMEOUT_SECONDS = 3 * _FETCH_TIMEOUT_SECONDS
# Each of the durable watermark's Spanner calls (storage_gcp_stage_d_policy)
# has this budget for its RPCs and retries. A refresh makes at most three
# under _refresh_lock: the floor read before the network part, the advance
# and the highest-sequence read after it.
WATERMARK_TIMEOUT_SECONDS = _FETCH_TIMEOUT_SECONDS
# So one refresh holds _refresh_lock at most this long. Not counted: the
# Spanner client creating or rotating its multiplexed session (CreateSession
# with its own 30 s timeout and retries, at startup and once a week), which
# can add tens of seconds to a watermark call in that window. Bounded, not
# indefinite.
REFRESH_BOUND_SECONDS = REFRESH_TIMEOUT_SECONDS + 3 * WATERMARK_TIMEOUT_SECONDS
_MAX_FUTURE_SKEW = timedelta(minutes=5)


# The deadline of the release URL the current thread is fetching from.
_fetch_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "stage_d_policy_fetch_deadline", default=None,
)


def _within_deadline(timeout: float | None, expired: type[Exception]) -> float | None:
    """An operation's timeout cut to the fetch deadline; ``expired`` once it passed."""
    deadline = _fetch_deadline.get()
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise expired("Stage D policy fetch deadline passed")
    return remaining if timeout is None else min(timeout, remaining)


class _DeadlineStream(httpcore.NetworkStream):
    def __init__(self, stream: httpcore.NetworkStream) -> None:
        self._stream = stream

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._stream.read(max_bytes, _within_deadline(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._stream.write(buffer, _within_deadline(timeout, httpcore.WriteTimeout))

    def close(self) -> None:
        self._stream.close()

    def start_tls(
        self, ssl_context: Any, server_hostname: str | None = None, timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        return _DeadlineStream(self._stream.start_tls(
            ssl_context, server_hostname, _within_deadline(timeout, httpcore.ConnectTimeout)))

    def get_extra_info(self, info: str) -> Any:
        return self._stream.get_extra_info(info)


class _DeadlineBackend(httpcore.NetworkBackend):
    """httpcore's sync backend with every operation cut to ``_fetch_deadline``."""

    def __init__(self) -> None:
        self._backend = httpcore.SyncBackend()

    def connect_tcp(
        self, host: str, port: int, timeout: float | None = None,
        local_address: str | None = None, socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        return _DeadlineStream(self._backend.connect_tcp(
            host, port, _within_deadline(timeout, httpcore.ConnectTimeout),
            local_address, socket_options))

    def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        return _DeadlineStream(self._backend.connect_unix_socket(
            path, _within_deadline(timeout, httpcore.ConnectTimeout), socket_options))

    def sleep(self, seconds: float) -> None:
        self._backend.sleep(seconds)


class DeadlineTransport(httpx.HTTPTransport):
    """httpx's transport on a pool whose network backend honours the fetch
    deadline (httpx 0.28's HTTPTransport takes no network_backend)."""

    def __init__(self) -> None:
        ssl_context = httpx.create_ssl_context()
        super().__init__(verify=ssl_context)
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl_context, network_backend=_DeadlineBackend(),
        )


def _deadline_client(**kwargs: Any) -> httpx.Client:
    return httpx.Client(transport=DeadlineTransport(), **kwargs)


@dataclass(eq=False)
class _NetworkRun:
    """One refresh's network part: its thread and, once it returns, its outcome."""

    thread: threading.Thread | None = None
    outcome: list[StageDPolicy | Exception] = field(default_factory=list)
    abandoned: bool = False
    skip_logged: bool = False


class BundleVerifier(Protocol):
    """Narrow verification seam; tests never need Sigstore network state."""

    def verify(
        self,
        document: bytes,
        bundle: bytes,
        *,
        certificate_identity: str,
        oidc_issuer: str,
    ) -> None: ...


class PolicyWatermark(Protocol):
    """Durable monotonic sequence gate shared by every serving replica."""

    def advance(self, *, plane: str, sequence: int, updated_at: datetime) -> bool: ...

    def highest_sequence(self, *, plane: str) -> int | None: ...


class SigstoreBundleVerifier:
    """Verify a cosign ``sign-blob --bundle`` bundle with pinned identity."""

    def verify(
        self,
        document: bytes,
        bundle: bytes,
        *,
        certificate_identity: str,
        oidc_issuer: str,
    ) -> None:
        from sigstore.models import Bundle
        from sigstore.verify import Verifier
        from sigstore.verify.policy import Identity

        parsed_bundle = Bundle.from_json(bundle.decode("utf-8"))
        Verifier.production().verify_artifact(
            input_=document,
            bundle=parsed_bundle,
            policy=Identity(identity=certificate_identity, issuer=oidc_issuer),
        )


class StorePolicyWatermark:
    """Adapter around the optional native-Spanner watermark capability."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def advance(self, *, plane: str, sequence: int, updated_at: datetime) -> bool:
        advance = getattr(self._store, "advance_stage_d_policy_watermark", None)
        if not callable(advance):
            return False
        return bool(advance(plane=plane, sequence=sequence, updated_at=updated_at))

    def highest_sequence(self, *, plane: str) -> int | None:
        read = getattr(self._store, "get_stage_d_policy_watermark", None)
        if not callable(read):
            return None
        value = read(plane=plane)
        return int(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class StageDPolicy:
    schema: str
    plane: str
    sequence: int
    kind: str
    issued_at: datetime
    image_digests: frozenset[str]


def parse_stage_d_policy(document: bytes, *, now: datetime | None = None) -> StageDPolicy:
    """Strictly validate the signed document without normalizing its bytes."""

    def object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Stage D policy contains a duplicate key")
            value[key] = item
        return value

    try:
        payload = json.loads(
            document.decode("utf-8"),
            object_pairs_hook=object_without_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Stage D policy is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Stage D policy must be an object")
    if set(payload) != _POLICY_KEYS:
        raise ValueError("Stage D policy keys do not match the schema")
    if payload["schema"] != "tr.stage-d-accepted/1":
        raise ValueError("unexpected Stage D policy schema")
    if payload["plane"] != "gcp":
        raise ValueError("unexpected Stage D policy plane")
    sequence = payload["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise ValueError("Stage D policy sequence must be a positive integer")
    kind = payload["kind"]
    if kind not in {"transitional", "final"}:
        raise ValueError("invalid Stage D policy kind")
    issued_at_raw = payload["issued_at"]
    if (
        not isinstance(issued_at_raw, str)
        or _RFC3339_UTC_RE.fullmatch(issued_at_raw) is None
    ):
        raise ValueError("Stage D policy issued_at must be RFC3339 UTC")
    try:
        issued_at = datetime.fromisoformat(issued_at_raw.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError("Stage D policy issued_at must be RFC3339 UTC") from exc
    if issued_at.tzinfo is None or issued_at.utcoffset() != timedelta(0):
        raise ValueError("Stage D policy issued_at must be RFC3339 UTC")
    issued_at = issued_at.astimezone(UTC)
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if issued_at > current + _MAX_FUTURE_SKEW:
        raise ValueError("Stage D policy issued_at is in the future")
    digests = payload["image_digests"]
    if not isinstance(digests, list) or not digests:
        raise ValueError("Stage D policy image_digests must be a non-empty array")
    if any(not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None for digest in digests):
        raise ValueError("Stage D policy contains an invalid image digest")
    if digests != sorted(digests) or len(digests) != len(set(digests)):
        raise ValueError("Stage D policy image_digests must be sorted and unique")
    return StageDPolicy(
        schema=str(payload["schema"]),
        plane=str(payload["plane"]),
        sequence=sequence,
        kind=str(kind),
        issued_at=issued_at,
        image_digests=frozenset(digests),
    )


class StageDPolicyResolver:
    """Refresh a signed policy in the background and retain only verified state."""

    def __init__(
        self,
        settings: Settings,
        watermark: PolicyWatermark,
        *,
        verifier: BundleVerifier | None = None,
        release_urls: Sequence[str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
        client_factory: Callable[..., httpx.Client] = _deadline_client,
    ) -> None:
        self._settings = settings
        self._watermark = watermark
        self._verifier = verifier or SigstoreBundleVerifier()
        self._release_urls_override = tuple(release_urls) if release_urls is not None else None
        self._monotonic = monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._client_factory = client_factory
        self._state_lock = threading.Lock()
        self._worker_lock = threading.Lock()
        self._worker: threading.Thread | None = None
        # Serializes refresh_now() callers, kick's worker among them, so two
        # refreshes never overlap and an older verified policy cannot be
        # assigned after a newer one. Separate from _worker_lock, which
        # kick() takes on the request path and must never wait on a fetch.
        self._refresh_lock = threading.Lock()
        # The network part of the latest refresh. One that outlived its
        # refresh is left to finish alone and no other starts meanwhile, so
        # a hang the fetch deadlines do not cover (name resolution, the TUF
        # refresh) costs one thread, not one per refresh.
        self._network_lock = threading.Lock()
        self._network: _NetworkRun | None = None
        self._next_refresh_at = 0.0
        self._policy: StageDPolicy | None = None

    def _release_urls(self) -> tuple[str, ...]:
        if self._release_urls_override is not None:
            candidates = self._release_urls_override
        else:
            candidates = (
                self._settings.trust_gcp_release_url,
                *self._settings.trust_gcp_release_fallback_url_list,
            )
        return tuple(dict.fromkeys(url.strip() for url in candidates if url.strip()))

    def release_configured(self) -> bool:
        """Whether there is any release to refresh from; without one the
        accepted set is the break-glass override alone."""
        return bool(self._release_urls())

    def accepted_image_digests(self) -> frozenset[str]:
        with self._state_lock:
            return self._policy.image_digests if self._policy is not None else frozenset()

    def current_policy(self) -> StageDPolicy | None:
        with self._state_lock:
            return self._policy

    def kick(self) -> None:
        """Start one due refresh without making the request wait for it."""

        if not self._release_urls() or self._monotonic() < self._next_refresh_at:
            return
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._next_refresh_at = (
                self._monotonic() + self._settings.stage_d_policy_refresh_seconds
            )
            self._worker = threading.Thread(
                target=self.refresh_now,
                name="stage-d-policy-refresh",
                daemon=True,
            )
            self._worker.start()

    def refresh_now(self) -> bool:
        """``refresh()``, serialized with every other ``refresh_now()``: kick's
        background worker and a caller that needs the set current now (the
        fast path's edge policy publisher). A caller waits for an in-flight
        refresh to finish, then fetches again: at most two refreshes'
        ``REFRESH_BOUND_SECONDS``."""

        with self._refresh_lock:
            return self.refresh()

    def refresh(self) -> bool:
        """Fetch, verify, validate, and watermark one policy publication.

        Not serialized: call ``refresh_now()`` where another refresh may run.
        Returns within ``REFRESH_BOUND_SECONDS`` (Spanner session creation
        aside): the watermark is read as a floor, then the network part
        (``_verified_policy``, which changes no state) runs in a daemon
        thread joined for ``REFRESH_TIMEOUT_SECONDS``; only after it returns
        a verified policy at or above the floor does this thread advance the
        watermark and assign it. A thread that outlives its refresh is
        abandoned, and its outcome with it, so it can never change the
        verified state.
        """

        if not self._release_urls():
            return False
        try:
            floor = self._watermark.highest_sequence(plane="gcp") or 0
            run = self._start_network(floor)
            if run is None:
                return False
            assert run.thread is not None
            run.thread.join(REFRESH_TIMEOUT_SECONDS)
            with self._network_lock:
                if not run.outcome:
                    run.abandoned = run.thread.is_alive()
                    raise (TimeoutError if run.abandoned else RuntimeError)(
                        "Stage D policy fetch and verification returned nothing")
                policy = run.outcome[0]
            if isinstance(policy, Exception):
                raise policy
            advanced = self._watermark.advance(
                plane=policy.plane,
                sequence=policy.sequence,
                updated_at=self._wall_clock(),
            )
            if (
                not advanced
                and self._watermark.highest_sequence(plane=policy.plane)
                != policy.sequence
            ):
                raise ValueError("Stage D policy sequence is below the watermark")
        except Exception as exc:  # noqa: BLE001 - verified state survives every failure
            logger.warning(
                "stage_d.policy_refresh_failed error_class=%s",
                type(exc).__name__,
            )
            return False
        with self._state_lock:
            self._policy = policy
        logger.info(
            "stage_d.policy_accepted plane=%s sequence=%s kind=%s digests=%s",
            policy.plane,
            policy.sequence,
            policy.kind,
            len(policy.image_digests),
        )
        return True

    def _start_network(self, floor: int) -> _NetworkRun | None:
        """Start this refresh's network part, or None while an abandoned one
        still runs (logged once for it, and once more when it ends)."""

        with self._network_lock:
            previous = self._network
            if previous is not None and previous.thread is not None and previous.thread.is_alive():
                if not previous.skip_logged:
                    previous.skip_logged = True
                    logger.warning(
                        "stage_d.policy_refresh_failed error_class=%s", "PreviousFetchRunning",
                    )
                return None
            run = _NetworkRun()

            def network() -> None:
                result = self._verified_policy(floor)
                with self._network_lock:
                    run.outcome.append(result)
                    if run.abandoned:
                        logger.info("stage_d.policy_abandoned_fetch_ended error_class=%s",
                                    type(result).__name__)

            run.thread = threading.Thread(target=network, name="stage-d-policy-fetch", daemon=True)
            self._network = run
            run.thread.start()
            return run

    def _verified_policy(self, floor: int) -> StageDPolicy | Exception:
        """The first release's policy that fetches, verifies, validates and
        is at or above ``floor``, or the last failure. Release URL by URL,
        each with its own fetch deadline. Network only: touches no resolver
        state."""

        last_error: Exception = ValueError("no Stage D release URL")
        for release_url in self._release_urls():
            try:
                policy_url = _policy_url(release_url)
                deadline = _fetch_deadline.set(time.monotonic() + _FETCH_TIMEOUT_SECONDS)
                try:
                    document = self._fetch(policy_url, _MAX_DOCUMENT_BYTES)
                    bundle = self._fetch(policy_url + ".bundle", _MAX_BUNDLE_BYTES)
                finally:
                    _fetch_deadline.reset(deadline)
                self._verifier.verify(
                    document,
                    bundle,
                    certificate_identity=self._settings.stage_d_policy_cert_identity,
                    oidc_issuer=self._settings.stage_d_policy_oidc_issuer,
                )
                policy = parse_stage_d_policy(document, now=self._wall_clock())
                if policy.sequence < floor:
                    raise ValueError("Stage D policy sequence is below the watermark")
                return policy
            except Exception as exc:  # noqa: BLE001 - reported by refresh()
                last_error = exc
        return last_error

    def _fetch(self, url: str, byte_limit: int) -> bytes:
        parsed = httpx.URL(url)
        if parsed.scheme != "https" or not parsed.host:
            raise ValueError("Stage D policy URL must be absolute HTTPS")
        with self._client_factory(
            timeout=_FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,
        ) as client:
            with client.stream("GET", parsed, headers={"accept": "application/json"}) as response:
                response.raise_for_status()
                length = response.headers.get("content-length")
                if length is not None and int(length) > byte_limit:
                    raise ValueError("Stage D policy artifact exceeds its size limit")
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > byte_limit:
                        raise ValueError("Stage D policy artifact exceeds its size limit")
                    content.extend(chunk)
        return bytes(content)


def _policy_url(release_url: str) -> str:
    parsed = httpx.URL(release_url)
    if parsed.scheme != "https" or not parsed.host:
        raise ValueError("trust release URL must be absolute HTTPS")
    return urljoin(str(parsed), _POLICY_PATH)
