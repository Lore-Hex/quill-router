"""Native service-identity transport for immutable v2 proof attachments."""
from __future__ import annotations

import http.client
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote, urlencode, urlsplit

from trusted_router.async_settle_shadow_v2_a1_health import HEALTH_BASIS_BATCH
from trusted_router.async_settle_shadow_v2_schema import require


class Lifetime:
    """One admission deadline, shared by credentials and every HTTP operation.

    Cancellation shuts down the socket before close (close alone need not wake a
    blocked read). A late DNS/credential return is checked before any send. The
    transport admits no further operation until the bounded single worker exits.
    """
    def __init__(self, deadline: float, clock: Callable[[], float]) -> None:
        self.deadline, self.clock = deadline, clock
        self.cancelled = False
        self.connection: http.client.HTTPConnection | None = None
        self.response: http.client.HTTPResponse | None = None

    def remaining(self) -> float:
        remaining = self.deadline - self.clock()
        if self.cancelled or remaining <= 0:
            raise TimeoutError("attachment_deadline")
        return remaining

    def abort(self) -> None:
        self.cancelled = True
        connection = self.connection
        sock = connection.sock if connection is not None else None
        # HTTP/1.0 / Connection: close moves the socket to the response reader.
        response = self.response
        if sock is None and response is not None:
            try:
                sock = cast(Any, response.fp.raw)._sock if response.fp is not None else None
            except (AttributeError, ValueError):
                pass  # The worker has concurrently closed the response.
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def exchange(self, method: str, url: str, body: bytes | None,
                 headers: Mapping[str, str]) -> tuple[int, Mapping[str, str], bytes]:
        parsed = urlsplit(url)
        require(parsed.scheme in ("http", "https") and bool(parsed.hostname), "attachment_url")
        cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = cls(str(parsed.hostname), parsed.port, timeout=self.remaining())
        self.connection = connection
        response = None
        try:
            connection.connect()
            # DNS/connect can finish after cancellation; never send in that case.
            assert connection.sock is not None
            connection.sock.settimeout(self.remaining())
            connection.request(method, parsed.path + ("?"+parsed.query if parsed.query else ""),
                               body=body, headers=dict(headers))
            connection.sock.settimeout(self.remaining())
            response = connection.getresponse()
            self.response = response
            content = bytearray()
            while True:
                remaining = self.remaining()
                sock = connection.sock
                if sock is None and response.fp is not None:
                    sock = cast(Any, response.fp.raw)._sock
                if sock is not None:
                    sock.settimeout(remaining)
                # The extra byte detects overflow without ever reading an unbounded body.
                chunk = response.read1(min(8192, HEALTH_BASIS_BATCH.limit + 1 - len(content)))
                self.remaining()
                if not chunk:
                    break
                content.extend(chunk)
                require(len(content) <= HEALTH_BASIS_BATCH.limit, "attachment_bytes")
            return response.status, {key.lower(): value for key, value in response.getheaders()}, bytes(content)
        finally:
            if response is not None:
                response.close()
            connection.close()
            self.response = None
            self.connection = None
            self.remaining()  # A late cleanup is never acknowledged.


@dataclass
class AuthResponse:
    status: int
    headers: Mapping[str, str]
    data: bytes


class AuthRequest:
    """No redirects/retries, including credential-library retry classification."""
    def __init__(self, lifetime: Lifetime) -> None:
        self.lifetime = lifetime

    def __call__(self, url: str, method: str = "GET", body: bytes | None = None,
                 headers: Mapping[str, str] | None = None, **kwargs: Any) -> AuthResponse:
        try:
            status, response_headers, data = self.lifetime.exchange(method, url, body, headers or {})
            if not 200 <= status < 300:
                raise RuntimeError("attachment_auth")
            return AuthResponse(status, response_headers, data)
        except Exception as error:
            # google-auth retries TransportError and selected HTTP statuses. Do
            # not classify any failure as retryable or return those statuses.
            raise RuntimeError("attachment_auth") from error


class GCSAttachments:
    """One create-if-absent PUT or complete-key GET; one in-process worker.

    The caller stops at its absolute deadline. Socket shutdown interrupts body
    reads. Non-cancellable platform DNS/credential work remains quarantined in
    the single daemon worker; another operation is refused until it exits.
    """
    def __init__(self, bucket: str) -> None:
        self.bucket = quote(bucket, safe="")
        self.endpoint = "https://storage.googleapis.com/"
        self.authenticated = True
        self.active = threading.Lock()

    def _request(self, method: str, key: str, body: bytes | None, lifetime: Lifetime) -> bytes | None:
        lifetime.remaining()
        headers = {"Content-Type": "application/json"}
        if method == "POST":
            path = self.endpoint + "upload/storage/v1/b/"+self.bucket+"/o"
            params: dict[str, Any] = dict(uploadType="media", name=key, ifGenerationMatch=0)
        else:
            path = self.endpoint + "storage/v1/b/"+self.bucket+"/o/"+quote(key, safe="")
            params = dict(alt="media")
        url = path + "?" + urlencode(params)
        if self.authenticated:
            import google.auth
            auth = AuthRequest(lifetime)
            credentials, _ = google.auth.default(request=cast(Any, auth),
                scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
            lifetime.remaining()
            credentials.before_request(auth, method, url, headers)
            lifetime.remaining()
        status, _, data = lifetime.exchange(method, url, body, headers)
        lifetime.remaining()
        if method == "GET" and status == 404:
            return None
        require(status in (200, 201), "attachment_response")
        return data if method == "GET" else None

    def request(self, method: str, key: str, body: bytes | None, timeout: float, *,
                deadline: float | None = None, clock: Callable[[], float] | None = None) -> bytes | None:
        clock = clock or time.monotonic
        lifetime = Lifetime(min(clock()+timeout, deadline if deadline is not None else float("inf")), clock)
        lifetime.remaining()
        require(self.active.acquire(blocking=False), "attachment_busy")
        done = threading.Event()
        result: list[bytes | None] = []
        errors: list[BaseException] = []
        def run() -> None:
            try:
                result.append(self._request(method, key, body, lifetime))
            except BaseException as error:
                errors.append(error)
            finally:
                self.active.release()
                done.set()
        worker = threading.Thread(target=run, name="shadow-v2-attachment", daemon=True)
        try:
            worker.start()
        except BaseException:
            self.active.release()
            raise
        try:
            if not done.wait(lifetime.remaining()):
                raise TimeoutError("attachment_deadline")
            lifetime.remaining()
            if errors:
                raise errors[0]
            return result[0]
        finally:
            lifetime.abort()

    def put(self, key: str, body: bytes, timeout: float, *, deadline: float | None = None,
            clock: Callable[[], float] | None = None) -> None:
        self.request("POST", key, body, timeout, deadline=deadline, clock=clock)

    def get(self, key: str, timeout: float, *, deadline: float | None = None,
            clock: Callable[[], float] | None = None) -> bytes | None:
        return self.request("GET", key, None, timeout, deadline=deadline, clock=clock)
