"""Native service-identity transport for immutable v2 proof attachments."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from trusted_router.async_settle_shadow_v2_a1_health import HEALTH_BASIS_BATCH
from trusted_router.async_settle_shadow_v2_schema import require


class GCSAttachments:
    """One create-if-absent PUT or complete-key GET, with no request retry."""
    def __init__(self, bucket: str) -> None:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
        self.credentials = credentials
        self.session_type = AuthorizedSession
        self.bucket = quote(bucket, safe="")

    def request(self, method: str, key: str, body: bytes | None, timeout: float) -> bytes | None:
        path = "https://storage.googleapis.com/"
        if method == "POST":
            path += "upload/storage/v1/b/"+self.bucket+"/o"
            params: dict[str, Any] = dict(uploadType="media", name=key, ifGenerationMatch=0)
        else:
            path += "storage/v1/b/"+self.bucket+"/o/"+quote(key, safe="")
            params = dict(alt="media")
        with self.session_type(self.credentials, max_refresh_attempts=0, refresh_timeout=timeout) as session, session.request(
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

    def put(self, key: str, body: bytes, timeout: float) -> None:
        self.request("POST", key, body, timeout)

    def get(self, key: str, timeout: float) -> bytes | None:
        return self.request("GET", key, None, timeout)


