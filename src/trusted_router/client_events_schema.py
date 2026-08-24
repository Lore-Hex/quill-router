"""Content-free wire schema for client-observed reliability beacons."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trusted_router.client_context import (
    CLIENT_ARCHES,
    CLIENT_LANGS,
    CLIENT_OSES,
    CLIENT_SDKS,
)
from trusted_router.client_reliability import (
    ENDPOINTS,
    ERROR_CLASSES,
    FINAL_OUTCOMES,
    HOSTS,
    LATENCY_BUCKETS,
    OUTCOMES,
    TIMEOUT_PHASES,
)


def _closed_pattern(values: tuple[str, ...]) -> str:
    return rf"^(?:{'|'.join(re.escape(value) for value in values)})$"


TR_CLIENT_SDKS = tuple(value for value in CLIENT_SDKS if value.startswith("tr-"))

ClientSdkName = Annotated[
    str,
    Field(pattern=_closed_pattern(TR_CLIENT_SDKS), max_length=max(map(len, TR_CLIENT_SDKS))),
]
ClientLang = Annotated[
    str,
    Field(pattern=_closed_pattern(CLIENT_LANGS), max_length=max(map(len, CLIENT_LANGS))),
]
ClientOs = Annotated[
    str,
    Field(pattern=_closed_pattern(CLIENT_OSES), max_length=max(map(len, CLIENT_OSES))),
]
ClientArch = Annotated[
    str,
    Field(pattern=_closed_pattern(CLIENT_ARCHES), max_length=max(map(len, CLIENT_ARCHES))),
]
Host = Annotated[
    str,
    Field(pattern=_closed_pattern(HOSTS), max_length=max(map(len, HOSTS))),
]
Endpoint = Annotated[
    str,
    Field(pattern=_closed_pattern(ENDPOINTS), max_length=max(map(len, ENDPOINTS))),
]
Outcome = Annotated[
    str,
    Field(pattern=_closed_pattern(OUTCOMES), max_length=max(map(len, OUTCOMES))),
]
FinalOutcome = Annotated[
    str,
    Field(pattern=_closed_pattern(FINAL_OUTCOMES), max_length=max(map(len, FINAL_OUTCOMES))),
]
ErrorClass = Annotated[
    str,
    Field(pattern=_closed_pattern(ERROR_CLASSES), max_length=max(map(len, ERROR_CLASSES))),
]
TimeoutPhase = Annotated[
    str,
    Field(pattern=_closed_pattern(TIMEOUT_PHASES), max_length=max(map(len, TIMEOUT_PHASES))),
]
LatencyBucket = Annotated[
    str,
    Field(pattern=_closed_pattern(LATENCY_BUCKETS), max_length=max(map(len, LATENCY_BUCKETS))),
]

Semver = Annotated[
    str,
    Field(
        pattern=r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}([-+][0-9A-Za-z.]{0,20})?$",
        max_length=32,
    ),
]
Runtime = Annotated[
    str,
    Field(pattern=r"^[a-z]{1,10}/[0-9A-Za-z.+-]{1,24}$", max_length=35),
]
ModelId = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9._:/~@-]{1,128}$", max_length=128),
]
RequestId = Annotated[
    str,
    Field(pattern=r"^rlog_[0-9a-f]{32}$", max_length=37),
]
BatchId = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{32}$", max_length=32),
]
InstanceId = Annotated[
    str,
    Field(pattern=r"^[0-9a-f]{16}$", max_length=16),
]
HistogramCount = Annotated[int, Field(ge=0, le=10_000_000)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClientSDK(_Strict):
    name: ClientSdkName
    version: Semver
    lang: ClientLang
    runtime: Runtime
    os: ClientOs
    arch: ClientArch


class ClientAttempt(_Strict):
    index: int = Field(ge=0, le=99)
    host: Host
    outcome: Outcome
    http_status: int | None = Field(default=None, ge=100, le=599)
    error_class: ErrorClass | None = None
    error_source: Literal["router", "provider", "unknown"] | None = None
    should_retry: Literal["true", "false", "absent"] = "absent"
    retry_after_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    elapsed_ms: int = Field(ge=0, le=3_600_000)
    ttfb_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    request_id: RequestId | None = None
    moved: bool


class ClientRequestEvent(_Strict):
    age_ms: int = Field(ge=0, le=86_400_000)
    plane: Literal["inference", "control"]
    endpoint: Endpoint
    method: Literal["GET", "POST"]
    streaming: bool
    provider_pinned: bool
    model: ModelId | None = None
    attempts: list[ClientAttempt] = Field(min_length=1, max_length=16)
    final_outcome: FinalOutcome
    final_http_status: int | None = Field(default=None, ge=100, le=599)
    total_ms: int = Field(ge=0, le=3_600_000)
    ttft_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    failover_used: bool
    timeout_phase: TimeoutPhase
    configured_timeout_ms: int | None = Field(default=None, ge=1, le=3_600_000)
    sample_rate: float = Field(gt=0, le=1)
    sample_reason: Literal["failure", "retried", "slow", "random"]


class ClientMinuteCounter(_Strict):
    window_start_age_ms: int = Field(ge=0, le=86_400_000)
    level: Literal["attempt", "request"]
    endpoint: Endpoint
    streaming: bool
    host: Host
    outcome: Outcome
    error_class: ErrorClass | None = None
    http_status_class: Literal["none", "2xx", "4xx", "429", "5xx"]
    timeout_phase: TimeoutPhase
    timeout_floor_met: bool
    provider_pinned: bool
    requests: int = Field(ge=1, le=10_000_000)
    attempts: int = Field(ge=0, le=10_000_000)
    failover_used: int = Field(ge=0, le=10_000_000)
    first_attempt_success: int = Field(ge=0, le=10_000_000)
    total_ms_hist: dict[LatencyBucket, HistogramCount] = Field(max_length=12)
    first_event_ms_hist: dict[LatencyBucket, HistogramCount] = Field(max_length=12)


class ClientEventsBatch(_Strict):
    schema_version: Literal[1]
    batch_id: BatchId
    instance_id: InstanceId
    seq: int = Field(ge=0, le=2_147_483_647)
    sent_at_ms: int | None = Field(default=None, ge=0, le=4_102_444_800_000)
    sdk: ClientSDK
    synthetic: bool = False
    dropped_since_last: int = Field(default=0, ge=0, le=1_000_000_000)
    events: list[ClientRequestEvent] = Field(default_factory=list, max_length=100)
    counters: list[ClientMinuteCounter] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def contains_observations(self) -> ClientEventsBatch:
        if not self.events and not self.counters:
            raise ValueError("at least one of events or counters must be non-empty")
        return self
