"""Offline source adapters over original Monitoring pages and CI archives."""
from __future__ import annotations

import hashlib
import re
import zipfile
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_budget import fraction, rational
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_schema import require


def timestamp_ns(value: str) -> int:
    match = re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z', value)
    require(match is not None, 'artifact_gap')
    assert match is not None
    second = datetime.strptime(match[1], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=UTC)
    delta = second - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 10**9 + int((match[2] or '').ljust(9, '0'))


def monitoring_series(files: Files, pages: list[dict[str, Any]], *, metric: str,
                      resource: dict[str, Any], metric_labels: dict[str, str],
                      start_ns: int, end_ns: int, interval_ns: int) -> dict[str, Any]:
    """Parse timeSeries.list originals; keep each point's original boundaries.

    Each page descriptor retains its requested page token and original FILE_REF.
    The caller supplies the exact expected series identity and sampling interval.
    """
    require(bool(pages) and 0 < interval_ns and start_ns < end_ns, 'artifact_gap')
    require(len(pages) <= 10000, 'artifact_gap')
    token = ''
    seen_tokens: set[str] = set()
    samples: list[dict[str, Any]] = []
    for index, page in enumerate(pages):
        require(page['page_token'] == token and token not in seen_tokens, 'artifact_gap')
        seen_tokens.add(token)
        value = retained_json(files.read(page['original']))
        require(not value.get('executionErrors') and not value.get('unreachable'), 'artifact_gap')
        series = value.get('timeSeries', [])
        require(type(series) is list, 'artifact_gap')
        for item in series:
            require(item['metric'] == dict(type=metric, labels=metric_labels)
                    and item['resource'] == resource, 'artifact_gap')
            require(item['metricKind'] == 'GAUGE' and item['valueType'] == 'DOUBLE', 'artifact_gap')
            require(bool(item['points']), 'artifact_gap')
            for point in item['points']:
                bounds = point['interval']
                right = timestamp_ns(bounds['endTime'])
                # GAUGE responses may omit startTime; their sample interval is
                # supplied by the reviewed query, never guessed from neighbours.
                left = timestamp_ns(bounds['startTime']) if 'startTime' in bounds else right - interval_ns
                if left == right:
                    left = right - interval_ns
                require(right - left == interval_ns and start_ns <= left < right <= end_ns, 'artifact_gap')
                require(set(point['value']) == {'doubleValue'}, 'artifact_gap')
                number = Fraction(str(point['value']['doubleValue']))
                require(0 <= number <= 1, 'artifact_gap')
                samples.append(dict(start_ns=left, end_ns=right, percent=rational(number * 100),
                                    original=page['original'], point=point))
        token = value.get('nextPageToken', '')
        require(type(token) is str and bool(token) == (index < len(pages) - 1), 'artifact_gap')
    samples.sort(key=lambda item: (item['start_ns'], item['end_ns']))
    cursor = start_ns
    for sample in samples:
        require(sample['start_ns'] == cursor, 'artifact_gap')
        cursor = sample['end_ns']
    require(bool(samples) and cursor == end_ns, 'artifact_gap')
    return dict(metric=metric, resource=resource, metric_labels=metric_labels,
                interval_ns=interval_ns, samples=samples, originals=pages)


def compare_cpu(baseline: dict[str, Any], trial: dict[str, Any],
                approved_increment: dict[str, int]) -> dict[str, Any]:
    require(all(baseline[k] == trial[k] for k in ('metric', 'resource', 'metric_labels', 'interval_ns')),
            'artifact_gap')
    require(bool(baseline['samples']) and len(baseline['samples']) == len(trial['samples']), 'artifact_gap')
    base_peak = max(fraction(s['percent']) for s in baseline['samples'])
    trial_peak = max(fraction(s['percent']) for s in trial['samples'])
    # Compare like sample positions as well as peaks; a moving baseline must
    # not hide a larger increase in a non-peak trial sample.
    increase = max(Fraction(0), *(fraction(t['percent']) - fraction(b['percent'])
        for b, t in zip(baseline['samples'], trial['samples'], strict=True)))
    allowance = fraction(approved_increment)
    require(0 < allowance and increase <= allowance and base_peak + allowance < 45,
            'fleet_headroom_gap')
    return dict(baseline_peak=rational(base_peak), trial_peak=rational(trial_peak),
                measured_increase=rational(increase), approved_increment=approved_increment)


def archive_members(files: PackFiles, archive: Path, *, archive_sha256: str,
                    members: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    """Select exact immutable member names, comparing both archive and member bytes."""
    a2.digest(archive_sha256)
    require(bool(members) and len(members) <= 10000, 'artifact_gap')
    require(len({m['name'] for m in members}) == len(members), 'artifact_gap')
    digest = hashlib.sha256()
    chunks: list[dict[str, Any]] = []
    # Preserve the archive as bounded original pieces, without extracting paths.
    with archive.open('rb') as source:
        while raw := source.read(262144):
            digest.update(raw)
            chunks.append(files.put(f'{prefix}/archive/{len(chunks):05d}', raw))
    require(digest.hexdigest() == archive_sha256, 'artifact_gap')
    result: list[dict[str, Any]] = []
    with zipfile.ZipFile(archive) as downloaded:
        names = downloaded.namelist()
        for member in members:
            require(names.count(member['name']) == 1, 'artifact_gap')
            info = downloaded.getinfo(member['name'])
            require(not info.is_dir() and info.file_size == member['bytes'] <= 262144, 'artifact_gap')
            with downloaded.open(info) as stream:
                raw = stream.read(262145)
            require(len(raw) == member['bytes'] and hashlib.sha256(raw).hexdigest() == member['sha256'],
                    'artifact_gap')
            ref = files.put(f'{prefix}/members/{len(result):05d}', raw)
            result.append(ref)
    files.obj(prefix + '/selection.json', dict(archive_sha256=archive_sha256,
        archive=chunks, members=[dict(**m, selected=r) for m, r in zip(members, result, strict=True)]))
    return result


def publisher_projection(*, fleet_cap: int, surge: int, publishers: int) -> dict[str, Any]:
    require(type(fleet_cap) is type(surge) is type(publishers) is int
            and fleet_cap > 0 and surge >= 0 and publishers > 0, 'artifact_gap')
    k = Fraction(publishers)
    return {key: rational(value) for key, value in dict(
        selects=k / 2 + Fraction(3, 2), unresolved_rows=Fraction(10001, 2),
        control_reads=k / 2 + 1, mutations=Fraction(1), transactions=k / 2 + Fraction(1, 2),
        metadata_bytes=Fraction(16 * (fleet_cap + surge) + 4 * publishers),
        observer_reads=Fraction(5, 4) * (fleet_cap + surge),
        pending_rows=Fraction(1001, 4) * (fleet_cap + surge)).items()}


def derived_peaks(files: Files, derivations: dict[str, Any]) -> dict[str, Any]:
    """Every PEAKS field names a measured or configured original and its field."""
    require(set(derivations) == set(a2.PEAKS.fields), 'artifact_gap')
    result = {}
    for name, item in derivations.items():
        require(item['kind'] in ('measured', 'configured'), 'artifact_gap')
        value = retained_json(files.read(item['original']))
        require(bool(item['field']), 'artifact_gap')
        for field in item['field']:
            value = value[field]
        require(value is not None, 'artifact_gap')
        result[name] = value
    a2.PEAKS(result)
    return result


def equal_allowances(files: Files, pre: dict[str, Any], post: dict[str, Any]) -> None:
    from trusted_router.async_settle_shadow_v2_gate_inputs import BUDGET_MEASUREMENTS
    before = BUDGET_MEASUREMENTS.parse(files.read(pre))
    after = BUDGET_MEASUREMENTS.parse(files.read(post))
    require({k: v for k, v in before.items() if k != 'component_us'} ==
            {k: v for k, v in after.items() if k != 'component_us'}, 'artifact_gap')
