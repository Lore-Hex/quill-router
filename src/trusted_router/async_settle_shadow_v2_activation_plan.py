"""Closed first-window instructions, separate from the frozen evidence plan."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, model_validator

ATTACHMENT_BUCKET = 'tr-async-settle-shadow-attachments-quill-cloud-proxy'
WORKSPACE = '162e441a-5d13-414f-92bd-7c6cd805faa3'
Name = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:/@=-]{0,255}$')]
SHA = Annotated[str, Field(pattern=r'^[0-9a-f]{40}$')]
Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Relative = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_./-]{0,255}$')]
Positive = Annotated[int, Field(gt=0, le=10000)]


class Closed(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @model_validator(mode='before')
    @classmethod
    def integer_literals(cls, value: Any) -> Any:
        # Literal equality otherwise accepts True as 1 and 0.0 as 0.
        if isinstance(value, dict):
            for name, field in cls.model_fields.items():
                choices = get_args(field.annotation)
                if (get_origin(field.annotation) is Literal and choices
                        and all(type(choice) is int for choice in choices)
                        and name in value and type(value[name]) is not int):
                    raise ValueError('integer literal required')
        return value


class File(Closed):
    path: Relative
    sha256: Digest


class Release(Closed):
    repository: Annotated[str, Field(pattern=r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')]
    sha: SHA
    image: Annotated[str, Field(pattern=r'^[a-z0-9.-]+/[A-Za-z0-9_./-]+@sha256:[0-9a-f]{64}$')]
    pin_file: Relative


class Releases(Closed):
    router: Release
    enclave: Release


class Interval(Closed):
    start: str
    end: str

    @model_validator(mode='after')
    def ordered(self) -> Interval:
        if not self.start.endswith('Z') or not self.end.endswith('Z') or stamp(self.start) >= stamp(self.end):
            raise ValueError('UTC interval required')
        return self


class Region(Closed):
    region: Name
    service: Name
    cap: Positive
    surge: Annotated[int, Field(ge=0, le=10000)]
    publishers: Positive


class Launch(Closed):
    id: Name
    region: Name
    role: Literal['router', 'enclave']
    start: str
    end: str
    end_reason: Literal['shutdown', 'hard_kill', 'opt_out']


class Schedule(Closed):
    workspace_count: Literal[1]
    primary: Literal['us-central1']
    regions: Annotated[list[Region], Field(min_length=1, max_length=32)]
    fleet_cap: Positive
    surge: Annotated[int, Field(ge=0, le=10000)]
    k: Positive
    launches: Annotated[list[Launch], Field(min_length=1, max_length=10000)]
    churn_limit: Positive
    trial: Interval
    warmup: Interval
    window: Interval
    horizon_end: str
    retention_days: Annotated[int, Field(ge=30, le=3650)]


class Limits(Closed):
    records_per_page: Annotated[int, Field(gt=0, le=100)]
    records_per_pass: Positive
    calls_per_pass: Positive
    call_seconds: Annotated[int, Field(gt=0, le=10)]
    retries: Literal[0]
    bytes_per_page: Annotated[int, Field(gt=0, le=262144)]
    total_passes: Positive
    output_bytes: Annotated[int, Field(gt=0, le=10**12)]
    trial_seconds: Annotated[int, Field(gt=0, le=3600)]
    cpu_increment_percent: Annotated[int, Field(gt=0, lt=45)]
    blocked_day_target_ppm: Annotated[int, Field(ge=0, le=1000000)]
    blocked_day_measurement: File


class PlannedPass(Closed):
    id: Name
    day: str
    purpose: Literal['trial', 'warmup', 'daily', 'horizon', 'rerun']
    max_records: Positive
    max_calls: Positive
    out: Relative


class Workflow(Closed):
    role: Literal['router', 'enclave']
    gate: Literal['ci', 'attestation', 'rollout', 'health']
    workflow: Name
    run_id: Annotated[int, Field(ge=0)]
    jobs: Annotated[list[Name], Field(min_length=1, max_length=100)]


class Job(Closed):
    name: Name
    region: Name
    scheduler: Name


class Probe(Closed):
    project: Name
    service_account: Annotated[str, Field(pattern=r'^[0-9]+-compute@developer\.gserviceaccount\.com$')]
    jobs: Annotated[list[Job], Field(min_length=1, max_length=8)]
    warmup_minutes: Literal[1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30]
    window_minutes: Literal[1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30]
    repetitions: Annotated[int, Field(ge=1, le=8)]
    max_invocations: Positive
    max_requests: Annotated[int, Field(gt=0, le=10000000)]
    max_spend_microdollars: Annotated[int, Field(gt=0, le=1000000000)]
    secret_reference: Annotated[str, Field(pattern=r'^projects/[a-z0-9-]+/secrets/[a-z0-9-]+$')]


class Source(Closed):
    id: Name
    kind: Literal['monitoring', 'lifecycle', 'boot_ledger', 'github_run', 'github_jobs', 'github_artifacts', 'github_job', 'github_artifact', 'trust', 'registry', 'health', 'attestation']
    resource: Name
    day: str
    parser: Literal['monitoring-v3', 'logging-v2', 'github-v3', 'released-json-v1']
    url: Annotated[str, Field(pattern=r'^https://[A-Za-z0-9./_:@?=&%-]+$')]
    filter: Annotated[str, Field(max_length=4096)]
    interval: Interval
    release_role: Literal['router', 'enclave']
    completion_record: File | None

    @model_validator(mode='after')
    def bounded_day(self) -> Source:
        start = datetime.strptime(self.day, '%Y-%m-%d').replace(tzinfo=UTC).timestamp()
        if not start <= stamp(self.interval.start) < stamp(self.interval.end) <= start + 86400:
            raise ValueError('source interval outside selected UTC day')
        if '?' in self.url or '@' in self.url.split('/')[2]:
            raise ValueError('source URL must not carry request values')
        return self


class Archive(Closed):
    role: Literal['router', 'enclave']
    run_id: Annotated[int, Field(gt=0)]
    artifact_id: Annotated[int, Field(gt=0)]
    sha256: Digest
    max_bytes: Annotated[int, Field(gt=0, le=1000000000)]
    out: Relative


class Artifacts(Closed):
    root: Relative
    release: Relative
    probe: Relative
    trial: Relative
    native: Relative
    bundle_plan: File


class Trial(Closed):
    resource: Name
    project: Name
    approval: File
    harness: Literal['P6NativeQualification']
    measurement_recipe: File


class Plan(Closed):
    version: Literal['p6-first-window-v1']
    review: Literal['REVIEW', 'approved']
    reviewer: Name
    releases: Releases
    workspace: Literal['162e441a-5d13-414f-92bd-7c6cd805faa3']
    attachment_bucket: Literal['tr-async-settle-shadow-attachments-quill-cloud-proxy']
    enclave_regions: Annotated[list[Name], Field(min_length=4, max_length=32)]
    schedule: Schedule
    limits: Limits
    passes: Annotated[list[PlannedPass], Field(min_length=1, max_length=10000)]
    workflows: Annotated[list[Workflow], Field(min_length=4, max_length=32)]
    probe: Probe
    sources: Annotated[list[Source], Field(min_length=1, max_length=10000)]
    archives: Annotated[list[Archive], Field(max_length=100)]
    checkpoint: Literal['fleet-budgets-pre', 'transport', 'fleet-budgets', 'shadow-serving']
    artifacts: Artifacts
    trial: Trial

    @model_validator(mode='after')
    def finite(self) -> Plan:
        s = self.schedule
        if s.fleet_cap != sum(r.cap for r in s.regions) or s.surge != sum(r.surge for r in s.regions):
            raise ValueError('configured fleet totals differ')
        primary = next((r for r in s.regions if r.region == s.primary), None)
        if primary is None or s.k != primary.cap + primary.surge or s.k != primary.publishers:
            raise ValueError('primary contender count differs')
        if len(set(self.enclave_regions)) != len(self.enclave_regions) or not {'us-central1', 'us-east4', 'europe-west4', 'us-west1'} <= set(self.enclave_regions):
            raise ValueError('complete enclave region inventory required')
        if any(r.publishers > r.cap + r.surge for r in s.regions):
            raise ValueError('contenders exceed configured capacity')
        if stamp(s.trial.end) - stamp(s.trial.start) > self.limits.trial_seconds:
            raise ValueError('trial exceeds approved duration')
        if not stamp(s.trial.end) <= stamp(s.warmup.start) < stamp(s.warmup.end) < stamp(s.window.start):
            raise ValueError('stage order differs')
        if stamp(s.window.end) - stamp(s.window.start) < 604800 or stamp(s.horizon_end) < stamp(s.window.end) + 172800:
            raise ValueError('window or forward horizon too short')
        if len(self.passes) > self.limits.total_passes or len(s.launches) > s.churn_limit:
            raise ValueError('finite pass or launch budget exceeded')
        for items, field in ((self.passes, 'id'), (self.sources, 'id'), (s.launches, 'id'), (s.regions, 'region')):
            if len({getattr(item, field) for item in items}) != len(items):
                raise ValueError('duplicate plan coordinate')
        for p in self.passes:
            datetime.strptime(p.day, '%Y-%m-%d')
            if p.max_records > self.limits.records_per_pass or p.max_calls > self.limits.calls_per_pass:
                raise ValueError('pass exceeds approved limits')
        for launch in s.launches:
            if launch.region not in ({r.region for r in s.regions} if launch.role == 'router' else set(self.enclave_regions)):
                raise ValueError('launch outside regions')
            if not stamp(s.trial.start) <= stamp(launch.start) < stamp(launch.end) <= stamp(s.horizon_end):
                raise ValueError('launch outside schedule')
        for region in s.regions:
            events = [(stamp(b.start), 1) for b in s.launches if b.role == 'router' and b.region == region.region]
            events += [(stamp(b.end), -1) for b in s.launches if b.role == 'router' and b.region == region.region]
            active = 0
            for _, delta in sorted(events):
                active += delta
                if active > region.cap + region.surge:
                    raise ValueError('launch overlap exceeds capacity')
        required = {('router', 'ci'), ('enclave', 'ci'), ('enclave', 'attestation'), ('router', 'rollout'), ('enclave', 'rollout'), ('router', 'health'), ('enclave', 'health')}
        if not required <= {(w.role, w.gate) for w in self.workflows}:
            raise ValueError('required workflow gate missing')
        import math
        expected_runs = len(self.probe.jobs) * sum(math.ceil((stamp(interval.end) - stamp(interval.start)) / (minutes * 60))
            for interval, minutes in ((s.warmup, self.probe.warmup_minutes), (s.window, self.probe.window_minutes)))
        if expected_runs > self.probe.max_invocations:
            raise ValueError('probe invocation budget')
        # All free path fields remain beneath the explicit workspace root.
        def paths(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ('path', 'pin_file', 'root', 'out', 'release', 'probe', 'trial', 'native') and isinstance(item, str):
                        if '..' in Path(item).parts or Path(item).is_absolute():
                            raise ValueError('path outside workspace')
                    paths(item)
            elif isinstance(value, list):
                for item in value:
                    paths(item)
        paths(self.model_dump())
        return self


def stamp(value: str) -> float:
    if not value.endswith('Z'):
        raise ValueError('UTC timestamp required')
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC).timestamp()


def load(path: Path, *, approved_sha256: str | None = None) -> Plan:
    raw = path.read_bytes()
    if len(raw) > 262144:
        raise ValueError('plan byte limit')
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        if len(dict(items)) != len(items):
            raise ValueError('duplicate plan key')
        return dict(items)
    plan = Plan.model_validate(json.loads(raw, object_pairs_hook=pairs))
    if approved_sha256 is not None:
        if hashlib.sha256(raw).hexdigest() != approved_sha256 or plan.review != 'approved' or plan.reviewer == 'REVIEW':
            raise ValueError('independent plan review required')
        for release in (plan.releases.router, plan.releases.enclave):
            if release.sha == '0' * 40 or '0' * 64 in release.image or 'REVIEW' in release.repository:
                raise ValueError('reviewed release SHA and image required')
        def reviewed(value: Any) -> None:
            if isinstance(value, str) and ('REVIEW' in value or '0' * 40 in value or value.startswith('000000000000-')):
                raise ValueError('unfilled REVIEW entry')
            if isinstance(value, dict):
                for item in value.values():
                    reviewed(item)
            elif isinstance(value, list):
                for item in value:
                    reviewed(item)
        reviewed(plan.model_dump())
        if any(w.run_id == 0 or 'REVIEW' in w.workflow for w in plan.workflows):
            raise ValueError('reviewed workflow runs required')
    return plan


def contained_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('file outside plan workspace')
    return path


def read_file(root: Path, ref: File) -> bytes:
    raw = contained_path(root, ref.path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != ref.sha256:
        raise ValueError('original digest differs')
    return raw
