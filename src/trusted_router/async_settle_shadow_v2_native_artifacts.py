"""Bounded original CI archives; existing C2 member parsing stays unchanged."""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from trusted_router.async_settle_shadow_v2_activation_plan import Archive, Plan
from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_native_reads import ReadBudget, ReadTransport, deadline


class ArchivePort(Protocol):
    def download(self, url: str, target: Path, *, seconds: int, max_bytes: int) -> dict[str, Any]: ...


class HTTPArchivePort:
    def __init__(self, headers: Callable[[str], dict[str, str]]) -> None:
        self.headers = headers

    def download(self, url: str, target: Path, *, seconds: int, max_bytes: int) -> dict[str, Any]:
        if not 0 < seconds <= 10 or max_bytes <= 0 or urlsplit(url).hostname != 'api.github.com':
            raise ValueError('archive bounds or origin')
        size = 0
        digest = hashlib.sha256()
        start = time.monotonic()
        with deadline(seconds), httpx.Client(transport=httpx.HTTPTransport(retries=0),
                follow_redirects=False, timeout=seconds, trust_env=False) as client:
            # The one GitHub redirect is a second charged call, never a retry.
            with client.stream('GET', url, headers=self.headers(url)) as first:
                if first.status_code != 302:
                    raise ValueError('archive redirect missing')
                location = first.headers['location']
                parsed = urlsplit(location)
                if parsed.scheme != 'https' or parsed.username or parsed.password:
                    raise ValueError('archive redirect shape')
            # This temporary URL is never retained, nor given repository headers.
            with client.stream('GET', location) as response, target.open('xb') as output:
                if response.status_code != 200:
                    raise ValueError('archive unavailable')
                for chunk in response.iter_bytes(chunk_size=65536):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError('archive byte budget')
                    output.write(chunk)
                    digest.update(chunk)
        return dict(bytes=size, sha256=digest.hexdigest(), calls=2,
                    elapsed_us=int((time.monotonic() - start) * 1000000))


def capture_archive(plan: Plan, spec: Archive, files: Files, reader: ReadTransport,
                    download: ArchivePort, budget: ReadBudget) -> dict[str, Any]:
    release = getattr(plan.releases, spec.role)
    url = f'https://api.github.com/repos/{release.repository}/actions/artifacts/{spec.artifact_id}'
    if budget.calls + 3 > plan.limits.calls_per_pass or budget.records >= plan.limits.records_per_pass:
        raise ValueError('archive pass budget')
    budget.calls += 1
    reply = reader.read('GET', url, params={}, body=None, seconds=plan.limits.call_seconds,
                        max_bytes=plan.limits.bytes_per_page)
    metadata = files.put(f'archives/{spec.artifact_id}-metadata.json', reply.raw)
    if reply.status != 200 or reply.elapsed_us > plan.limits.call_seconds * 1000000:
        raise ValueError('archive metadata unavailable')
    value = json.loads(reply.raw)
    if (value['id'] != spec.artifact_id or value['workflow_run']['id'] != spec.run_id
            or value['workflow_run']['head_sha'] != release.sha or value['expired'] is not False
            or value['digest'] != 'sha256:' + spec.sha256 or value['size_in_bytes'] > spec.max_bytes):
        raise ValueError('archive release or original differs')
    budget.records += 1
    budget.calls += 2
    target = files.root / spec.out
    target.parent.mkdir(parents=True, exist_ok=True)
    result = download.download(url + '/zip', target, seconds=plan.limits.call_seconds,
                               max_bytes=spec.max_bytes)
    if (result['sha256'] != spec.sha256 or result['bytes'] != value['size_in_bytes']
            or result['elapsed_us'] > plan.limits.call_seconds * 1000000 or result['calls'] != 2):
        raise ValueError('archive download differs')
    return files.obj(f'archives/{spec.artifact_id}-receipt.json', dict(**result,
        path=spec.out, metadata=metadata, release_sha=release.sha, run_id=spec.run_id))
