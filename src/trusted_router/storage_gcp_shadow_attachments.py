"""Native service-identity transport for immutable v2 proof attachments."""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any
from urllib.parse import quote

from trusted_router.async_settle_shadow_v2_a1_health import HEALTH_BASIS_BATCH
from trusted_router.async_settle_shadow_v2_schema import require


class GCSAttachments:
    """One create-if-absent PUT or complete-key GET, with no request retry."""
    session_type: Any

    def __init__(self, bucket: str) -> None:
        self.bucket = quote(bucket, safe="")
        self.endpoint = "https://storage.googleapis.com/"
        self.authenticated = True

    def request(self, method: str, key: str, body: bytes | None, timeout: float, *,
                deadline: float | None = None, clock: Callable[[], float] | None = None) -> bytes | None:
        """One absolute, killable lifetime, including DNS/auth and response close.

        A Requests timeout is only an inactivity timeout. A disposable process
        also bounds operations (DNS, credential refresh, TLS, close) that cannot
        reliably be interrupted by closing a response socket from another thread.
        No live worker survives an unknown outcome or is reused for a retry.
        """
        clock = clock or time.monotonic
        deadline = min(clock() + timeout, deadline if deadline is not None else float("inf"))
        if timeout <= 0 or clock() >= deadline:
            raise TimeoutError("attachment_deadline")
        payload = json.dumps(dict(bucket=self.bucket, endpoint=getattr(self, "endpoint", "https://storage.googleapis.com/"),
            authenticated=getattr(self, "authenticated", True), method=method, key=key,
            body=base64.b64encode(body).decode() if body is not None else None, timeout=timeout)).encode()
        worker = subprocess.Popen(  # noqa: S603 - fixed module; object data is on stdin
            [sys.executable, "-c", "from trusted_router.storage_gcp_shadow_attachments import worker; worker()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            remaining = min(timeout, deadline - clock())
            if remaining <= 0:
                raise subprocess.TimeoutExpired("attachment", timeout)
            output, _ = worker.communicate(payload, timeout=remaining)
            if clock() >= deadline:
                raise TimeoutError("attachment_deadline")
            require(worker.returncode == 0, "attachment_response")
            result = json.loads(output)
            return base64.b64decode(result) if result is not None else None
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.communicate()  # Reap; no abandoned RPC or background retry.
            raise TimeoutError("attachment_deadline") from None
        except BaseException:
            if worker.poll() is None:
                worker.kill()
                worker.communicate()
            raise

    def _request(self, method: str, key: str, body: bytes | None, timeout: float) -> bytes | None:
        path = self.endpoint
        if method == "POST":
            path += "upload/storage/v1/b/"+self.bucket+"/o"
            params: dict[str, Any] = dict(uploadType="media", name=key, ifGenerationMatch=0)
        else:
            path += "storage/v1/b/"+self.bucket+"/o/"+quote(key, safe="")
            params = dict(alt="media")
        with self.session_type() as session, session.request(
                method, path, params=params, data=body, headers={"Content-Type": "application/json"},
                timeout=timeout, max_allowed_time=timeout, stream=True, allow_redirects=False) as response:
            if method == "GET" and response.status_code == 404:
                return None
            response.raise_for_status()
            require(response.status_code in (200, 201), "attachment_response")
            if method == "POST":
                return None
            content = bytearray()
            for chunk in response.iter_content(8192):
                content.extend(chunk)
                require(len(content) <= HEALTH_BASIS_BATCH.limit, "attachment_bytes")
            return bytes(content)

    def put(self, key: str, body: bytes, timeout: float, *, deadline: float | None = None,
            clock: Callable[[], float] | None = None) -> None:
        self.request("POST", key, body, timeout, deadline=deadline, clock=clock)

    def get(self, key: str, timeout: float, *, deadline: float | None = None,
            clock: Callable[[], float] | None = None) -> bytes | None:
        return self.request("GET", key, None, timeout, deadline=deadline, clock=clock)




def worker() -> None:
    """Private single-request process entry point; stdout is a bounded result."""
    value = json.loads(sys.stdin.buffer.read())
    transport = object.__new__(GCSAttachments)
    transport.bucket = value["bucket"]
    transport.endpoint = value["endpoint"]
    import requests
    with ExitStack() as stack:
        if value["authenticated"]:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession, Request
            class AuthSession(requests.Session):
                def request(self, *args: Any, **kwargs: Any) -> Any:
                    kwargs["allow_redirects"] = False
                    try:
                        response = super().request(*args, **kwargs)
                    except requests.RequestException as error:
                        # Do not let credential helpers classify a transport error
                        # as retryable. The encompassing worker deadline still owns it.
                        raise RuntimeError("attachment_auth") from error
                    if not 200 <= response.status_code < 300:
                        response.close()
                        raise RuntimeError("attachment_auth")
                    return response
            auth_session = stack.enter_context(AuthSession())
            auth_request = Request(session=auth_session)
            credentials, _ = google.auth.default(request=auth_request,
                scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
            transport.session_type = lambda: AuthorizedSession(credentials, auth_request=auth_request,
                max_refresh_attempts=0, refresh_timeout=value["timeout"])
        else:
            class OfflineSession(requests.Session):
                def request(self, *args: Any, **kwargs: Any) -> Any:
                    kwargs.pop("max_allowed_time", None)
                    return super().request(*args, **kwargs)
            transport.session_type = OfflineSession
        result = transport._request(value["method"], value["key"],
            base64.b64decode(value["body"]) if value["body"] is not None else None, value["timeout"])
    sys.stdout.write(json.dumps(base64.b64encode(result).decode() if result is not None else None))
