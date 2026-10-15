"""Explicit read-only HTTP adapters for the dormant collector ports.

No SDK imports, discovery, redirects, or retries. Original native responses are
retained separately from the records wrapper consumed by Pass.
"""
from __future__ import annotations

import json
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

import httpx

from trusted_router.async_settle_shadow_v2_activation_plan import Plan, Source
from trusted_router.async_settle_shadow_v2_collect_ports import Files, Page, Query


@dataclass(frozen=True)
class Response:
    raw: bytes
    status: int
    headers: dict[str, str]
    elapsed_us: int


class ReadTransport(Protocol):
    def read(self, method: str, url: str, *, params: dict[str, Any],
             body: dict[str, Any] | None, seconds: int, max_bytes: int) -> Response: ...


@contextmanager
def deadline(seconds: int) -> Iterator[None]:
    """POSIX main-thread total deadline, including headers, body and cleanup."""
    if threading.current_thread() is not threading.main_thread():
        raise ValueError('native reads require the command main thread')
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise ValueError('existing process timer')
    previous = signal.getsignal(signal.SIGALRM)
    def expired(_number: int, _frame: Any) -> None:
        raise TimeoutError('native total call deadline')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    except httpx.HTTPError:
        raise OSError('native HTTP read failed') from None
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class HTTPReadTransport:
    """Only GET and Logging entries:list (a read expressed as POST)."""

    def __init__(self, headers: Callable[[str], dict[str, str]]) -> None:
        self.headers = headers

    def read(self, method: str, url: str, *, params: dict[str, Any],
             body: dict[str, Any] | None, seconds: int, max_bytes: int) -> Response:
        if method != 'GET' and not (method == 'POST' and url == 'https://logging.googleapis.com/v2/entries:list'):
            raise ValueError('read method required')
        if not 0 < seconds <= 10 or not 0 < max_bytes <= 262144:
            raise ValueError('native call bounds')
        with deadline(seconds):
            start = time.monotonic()
            headers = self.headers(url)
            remaining = seconds - (time.monotonic() - start)
            if remaining <= 0:
                raise TimeoutError('call deadline')
            with httpx.Client(transport=httpx.HTTPTransport(retries=0), follow_redirects=False,
                              timeout=remaining, trust_env=False) as client:
                with client.stream(method, url, params=params, json=body, headers=headers) as response:
                    parts = bytearray()
                    for chunk in response.iter_bytes(chunk_size=8192):
                        if len(parts) + len(chunk) > max_bytes:
                            raise ValueError('native response byte limit')
                        parts.extend(chunk)
                        if time.monotonic() - start > seconds:
                            raise TimeoutError('call deadline')
                    result = Response(bytes(parts), response.status_code, dict(response.headers),
                                      int((time.monotonic() - start) * 1000000))
            if time.monotonic() - start > seconds:
                raise TimeoutError('call deadline')
            return result


@dataclass
class ReadBudget:
    calls: int = 0
    records: int = 0


class BoundedFiles(Files):
    def __init__(self, root: Any, max_bytes: int) -> None:
        super().__init__(root)
        self.remaining = max_bytes

    def put(self, path: str, raw: bytes, schema: Any = None) -> dict[str, Any]:
        if len(raw) > self.remaining:
            raise ValueError('native output byte budget')
        self.remaining -= len(raw)
        return super().put(path, raw, schema)


class NativeClient:
    """One explicit source binding; no EOF-to-delivery-boundary conversion."""

    def __init__(self, plan: Plan, source: Source, transport: ReadTransport, files: Files,
                 *, complete_through_us: int | None = None, budget: ReadBudget | None = None) -> None:
        self.plan, self.source, self.transport, self.files = plan, source, transport, files
        self.complete_through_us = complete_through_us
        self.budget = budget if budget is not None else ReadBudget()
        self._page_size: int | None = None
        self._total: int | None = None
        self.calls = 0
        self.records = 0
        self.originals: list[dict[str, Any]] = []
        self._tokens: set[str | None] = set()

    def read(self, query: Query, token: str | None) -> Page:
        limits, source = self.plan.limits, self.source
        if (query.source, query.resource, query.day) != (source.id, source.resource, source.day):
            raise ValueError('source binding differs')
        if not 0 < query.page_size <= limits.records_per_page or not 0 < query.deadline_seconds <= limits.call_seconds:
            raise ValueError('native query bounds')
        requested = query.page_size if source.kind in ('monitoring', 'lifecycle', 'boot_ledger', 'github_jobs', 'github_artifacts') else 1
        if self.budget.calls >= limits.calls_per_pass or self.budget.records + requested > limits.records_per_pass or token in self._tokens:
            raise ValueError('native pass bounds or repeated page')
        if self._page_size is not None and self._page_size != query.page_size:
            raise ValueError('native page size changed')
        self._page_size = query.page_size
        self._tokens.add(token)
        self.calls += 1  # Failed attempts consume the same finite pass.
        self.budget.calls += 1
        method, params, body = self.request(query, token)
        reply = self.transport.read(method, source.url, params=params, body=body,
                                    seconds=query.deadline_seconds, max_bytes=limits.bytes_per_page)
        prefix = f'native/{source.id}/{self.calls:05d}'
        ref = self.files.put(prefix + '.json', reply.raw)
        self.originals.append(dict(page_token=token or '', original=ref))
        self.files.obj(prefix + '-receipt.json', dict(original=ref, method=method,
            url=source.url, params=params, body=body, status=reply.status,
            elapsed_us=reply.elapsed_us, requested_token=token))
        if reply.status != 200 or reply.elapsed_us > query.deadline_seconds * 1000000:
            raise ValueError('native response status or deadline')
        if len(reply.raw) > limits.bytes_per_page:
            raise ValueError('native response byte limit')
        value = json.loads(reply.raw)
        rows, next_token = self.records_from(value, reply.headers, token)
        if len(rows) > query.page_size or self.budget.records + len(rows) > limits.records_per_pass:
            raise ValueError('native record limit')
        self.records += len(rows)
        self.budget.records += len(rows)
        wrapped = json.dumps(dict(records=rows), separators=(',', ':')).encode()
        if len(wrapped) > 262144:
            raise ValueError('collector wrapper byte limit')
        return Page(wrapped, next_token, next_token is None, reply.elapsed_us,
                    self.complete_through_us)

    def request(self, query: Query, token: str | None) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
        source = self.source
        host = urlsplit(source.url).netloc
        if source.kind == 'monitoring':
            if source.parser != 'monitoring-v3' or source.url != f'https://monitoring.googleapis.com/v3/{source.resource}/timeSeries':
                raise ValueError('Monitoring binding differs')
            if not source.filter or '*' in source.filter or 'resource.labels.' not in source.filter or 'metric.type=' not in source.filter:
                raise ValueError('explicit metric and resource required')
            return 'GET', dict(filter=source.filter, **{'interval.startTime': source.interval.start,
                'interval.endTime': source.interval.end}, view='FULL', pageSize=query.page_size,
                pageToken=token or ''), None
        if source.kind in ('lifecycle', 'boot_ledger'):
            if source.parser != 'logging-v2' or source.url != 'https://logging.googleapis.com/v2/entries:list':
                raise ValueError('Logging binding differs')
            if not source.filter or '*' in source.filter or 'resource.labels.' not in source.filter or 'logName=' not in source.filter:
                raise ValueError('explicit lifecycle source required')
            bounded = f'({source.filter}) AND timestamp>="{source.interval.start}" AND timestamp<"{source.interval.end}"'
            return 'POST', {}, dict(resourceNames=[source.resource], filter=bounded,
                orderBy='timestamp asc', pageSize=query.page_size, pageToken=token or '')
        if source.kind.startswith('github_'):
            release = getattr(self.plan.releases, source.release_role)
            prefix = f'https://api.github.com/repos/{release.repository}/actions/'
            if source.parser != 'github-v3' or not source.url.startswith(prefix):
                raise ValueError('GitHub repository binding differs')
            import re
            suffix = source.url.removeprefix(prefix)
            patterns = dict(github_run=r'runs/[1-9][0-9]*', github_jobs=r'runs/[1-9][0-9]*/jobs',
                            github_artifacts=r'runs/[1-9][0-9]*/artifacts', github_job=r'jobs/[1-9][0-9]*', github_artifact=r'artifacts/[1-9][0-9]*')
            if re.fullmatch(patterns[source.kind], suffix) is None:
                raise ValueError('explicit GitHub run required')
            return 'GET', dict(per_page=query.page_size, page=int(token or '1')), None
        if source.kind in ('trust', 'registry', 'health', 'attestation'):
            regional_host = ('api.trustedrouter.com' if source.resource == self.plan.schedule.primary
                             else f'api-{source.resource}.quillrouter.com')
            allowed = (source.kind == 'registry' and host == 'artifactregistry.googleapis.com' and '/dockerImages/' in source.url
                       or source.kind == 'trust' and source.url == 'https://trustedrouter.com/trust/gcp-release.json'
                       or source.kind == 'health' and source.url == 'https://trustedrouter.com/health'
                       or source.kind == 'attestation' and source.resource in self.plan.enclave_regions
                       and source.url == f'https://{regional_host}/attestation')
            if source.parser != 'released-json-v1' or token is not None or not allowed:
                raise ValueError('released original binding differs')
            return 'GET', {}, None
        raise ValueError('source parser unavailable')

    def records_from(self, value: Any, headers: dict[str, str], token: str | None) -> tuple[list[dict[str, Any]], str | None]:
        source = self.source
        if not isinstance(value, dict) or value.get('executionErrors') or value.get('unreachable'):
            raise ValueError('native result incomplete')
        if source.kind == 'monitoring':
            # Monitoring pageSize counts points, not just series.
            series = value.get('timeSeries', [])
            rows = [dict(metric=s['metric'], resource=s['resource'], point=p)
                    for s in series for p in s.get('points', [])]
            return rows, value.get('nextPageToken') or None
        if source.kind in ('lifecycle', 'boot_ledger'):
            return value.get('entries', []), value.get('nextPageToken') or None
        if source.kind.startswith('github_'):
            sha = getattr(self.plan.releases, source.release_role).sha
            rows = ([value] if source.kind in ('github_run', 'github_job', 'github_artifact') else
                    value['jobs' if source.kind == 'github_jobs' else 'artifacts'])
            requested = int(source.url.rsplit('/', 1)[-1]) if source.kind in ('github_run', 'github_job', 'github_artifact') else int(source.url.split('/runs/')[1].split('/')[0])
            for row in rows:
                observed_id = (row['id'] if source.kind in ('github_run', 'github_job', 'github_artifact')
                               else row['run_id'] if source.kind == 'github_jobs' else row['workflow_run']['id'])
                if observed_id != requested:
                    raise ValueError('GitHub selected record differs')
                revision = row.get('head_sha') if source.kind not in ('github_artifacts', 'github_artifact') else row['workflow_run']['head_sha']
                if revision != sha:
                    raise ValueError('released SHA differs')
                if source.kind not in ('github_artifacts', 'github_artifact') and (row['status'] != 'completed' or row['conclusion'] != 'success' or not (row.get('completed_at') or row.get('updated_at'))):
                    raise ValueError('released run or job incomplete')
                if source.kind in ('github_artifacts', 'github_artifact') and row.get('expired') is not False:
                    raise ValueError('released artifact expired')
            link = headers.get('link', '')
            next_token = str(int(token or '1') + 1) if 'rel="next"' in link else None
            if source.kind in ('github_jobs', 'github_artifacts'):
                total = value['total_count']
                if type(total) is not int or total < 0 or total > self.plan.limits.records_per_pass:
                    raise ValueError('GitHub total record limit')
                if self._total is not None and self._total != total:
                    raise ValueError('GitHub population changed')
                self._total = total
                consumed = self.records + len(rows)
                if consumed > total or bool(next_token) != (consumed < total):
                    raise ValueError('GitHub continuation missing or extra')
                if next_token:
                    import re
                    links = re.findall(r'<([^>]+)>; rel="next"', link)
                    if len(links) != 1:
                        raise ValueError('GitHub continuation shape')
                    parsed = urlsplit(links[0])
                    params = parse_qs(parsed.query)
                    if (parsed.scheme + '://' + parsed.netloc + parsed.path != source.url
                            or params.get('page') != [next_token]
                            or params.get('per_page') != [str(self._page_size)]):
                        raise ValueError('GitHub continuation differs')
            return rows, next_token
        release = getattr(self.plan.releases, source.release_role)
        if source.kind == 'trust' and value.get('image_digest') != release.image.split('@')[1]:
            raise ValueError('published image differs')
        if source.kind == 'registry' and value.get('uri') != release.image:
            raise ValueError('registry image differs')
        if source.kind == 'health' and value != {'status': 'ok'}:
            raise ValueError('router health differs')
        return [value], None
