"""Small GCS generation-guarded leases for one-shot worker admission.

This module intentionally uses only the standard library. Workers import it
before application configuration, Sentry, Spanner, or Bigtable so duplicate
Cloud Run Job executions can leave without opening expensive clients.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

_METADATA_CREDENTIAL_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)
_STORAGE_API = "https://storage.googleapis.com/storage/v1"
_STORAGE_UPLOAD_API = "https://storage.googleapis.com/upload/storage/v1"


@dataclass(frozen=True)
class GCSLeaseConfig:
    bucket: str
    object_name: str
    lease_seconds: float
    min_interval_seconds: float
    failure_cooldown_seconds: float
    request_timeout_seconds: float = 10.0

    def validate(self) -> None:
        if not self.bucket:
            raise ValueError("GCS lease bucket must not be empty")
        if not self.object_name:
            raise ValueError("GCS lease object name must not be empty")
        if self.lease_seconds <= 0:
            raise ValueError("GCS lease duration must be positive")
        if self.min_interval_seconds < 0:
            raise ValueError("GCS lease minimum interval must not be negative")
        if self.failure_cooldown_seconds < 0:
            raise ValueError("GCS lease failure cooldown must not be negative")
        if self.request_timeout_seconds <= 0:
            raise ValueError("GCS lease request timeout must be positive")


@dataclass(frozen=True)
class GCSLease:
    owner: str
    generation: int
    acquired_at: float


class GCSGenerationLease:
    """Acquire and finish a lease using GCS object generation preconditions."""

    def __init__(self, config: GCSLeaseConfig) -> None:
        config.validate()
        self._config = config

    def acquire(
        self,
        *,
        now: float | None = None,
        owner: str | None = None,
    ) -> GCSLease | None:
        acquired_at = time.time() if now is None else now
        lease_owner = owner or os.environ.get("CLOUD_RUN_EXECUTION") or (
            f"manual-{os.getpid()}-{uuid.uuid4().hex}"
        )
        token = self._access_token()
        for _attempt in range(3):
            current = self._read(token)
            if current is not None:
                current_payload, current_generation = current
                expires_at = current_payload["expires_at"]
                if not isinstance(expires_at, (int, float)):
                    raise RuntimeError("GCS lease payload has no numeric expiry")
                if float(expires_at) > acquired_at:
                    return None
                if not self._delete(token, current_generation):
                    continue

            new_payload: dict[str, object] = {
                "owner": lease_owner,
                "state": "running",
                "acquired_at": acquired_at,
                "expires_at": acquired_at + self._config.lease_seconds,
            }
            new_generation = self._write(token, new_payload, if_generation_match=0)
            if new_generation is not None:
                return GCSLease(
                    owner=lease_owner,
                    generation=new_generation,
                    acquired_at=acquired_at,
                )
        return None

    def finish(
        self,
        lease: GCSLease,
        *,
        succeeded: bool,
        now: float | None = None,
    ) -> float:
        completed_at = time.time() if now is None else now
        expires_at = (
            max(
                completed_at,
                lease.acquired_at + self._config.min_interval_seconds,
            )
            if succeeded
            else completed_at + self._config.failure_cooldown_seconds
        )
        payload: dict[str, object] = {
            "owner": lease.owner,
            "state": "cooldown" if succeeded else "failed",
            "acquired_at": lease.acquired_at,
            "completed_at": completed_at,
            "expires_at": expires_at,
        }
        token = self._access_token()
        generation = self._write(
            token,
            payload,
            if_generation_match=lease.generation,
        )
        if generation is None:
            raise RuntimeError("GCS lease ownership changed before completion")
        return max(0.0, expires_at - completed_at)

    def _access_token(self) -> str:
        request = urllib.request.Request(
            _METADATA_CREDENTIAL_URL,
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(  # noqa: S310 - URL is the fixed metadata host.
            request,
            timeout=self._config.request_timeout_seconds,
        ) as response:
            payload = json.loads(response.read())
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("metadata server returned no GCS access token")
        return token

    def _request(
        self,
        url: str,
        *,
        token: str,
        method: str = "GET",
        body: bytes | None = None,
    ) -> tuple[int, bytes]:
        headers = {"Authorization": f"Bearer {token}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if not url.startswith((_STORAGE_API, _STORAGE_UPLOAD_API)):
            raise ValueError("GCS lease requests must target the Google Storage API")
        request = urllib.request.Request(  # noqa: S310 - host is allowlisted above.
            url,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - host is allowlisted above.
                request,
                timeout=self._config.request_timeout_seconds,
            ) as response:
                return int(response.status), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _object_url(
        self,
        *,
        media: bool = False,
        generation: int | None = None,
    ) -> str:
        bucket = urllib.parse.quote(self._config.bucket, safe="")
        name = urllib.parse.quote(self._config.object_name, safe="")
        query: dict[str, str] = {}
        if media:
            query["alt"] = "media"
        if generation is not None:
            query["generation"] = str(generation)
        suffix = "?" + urllib.parse.urlencode(query) if query else ""
        return f"{_STORAGE_API}/b/{bucket}/o/{name}{suffix}"

    def _read(self, token: str) -> tuple[dict[str, object], int] | None:
        status, raw_metadata = self._request(self._object_url(), token=token)
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"GCS lease metadata read failed with HTTP {status}")
        metadata = json.loads(raw_metadata)
        generation = int(metadata["generation"])
        status, raw_payload = self._request(
            self._object_url(media=True, generation=generation),
            token=token,
        )
        if status in {404, 412}:
            return None
        if status != 200:
            raise RuntimeError(f"GCS lease payload read failed with HTTP {status}")
        payload = json.loads(raw_payload)
        if not isinstance(payload, dict):
            raise RuntimeError("GCS lease payload must be a JSON object")
        owner = payload.get("owner")
        expires_at = payload.get("expires_at")
        if not isinstance(owner, str) or not owner:
            raise RuntimeError("GCS lease payload has no owner")
        if not isinstance(expires_at, (int, float)):
            raise RuntimeError("GCS lease payload has no numeric expiry")
        return payload, generation

    def _write(
        self,
        token: str,
        payload: dict[str, object],
        *,
        if_generation_match: int,
    ) -> int | None:
        bucket = urllib.parse.quote(self._config.bucket, safe="")
        query = urllib.parse.urlencode(
            {
                "uploadType": "media",
                "name": self._config.object_name,
                "ifGenerationMatch": str(if_generation_match),
            }
        )
        status, raw = self._request(
            f"{_STORAGE_UPLOAD_API}/b/{bucket}/o?{query}",
            token=token,
            method="POST",
            body=json.dumps(payload, sort_keys=True).encode(),
        )
        if status == 412:
            return None
        if status not in {200, 201}:
            raise RuntimeError(f"GCS lease write failed with HTTP {status}")
        metadata = json.loads(raw)
        return int(metadata["generation"])

    def _delete(self, token: str, generation: int) -> bool:
        separator = "&" if "?" in self._object_url() else "?"
        url = self._object_url() + separator + urllib.parse.urlencode(
            {"ifGenerationMatch": str(generation)}
        )
        status, _raw = self._request(url, token=token, method="DELETE")
        if status in {404, 412}:
            return False
        if status not in {200, 204}:
            raise RuntimeError(f"GCS lease delete failed with HTTP {status}")
        return True
