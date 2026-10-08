"""Content-free, bounded shadow evidence and cumulative dimensional counters."""
from __future__ import annotations

import contextvars
import datetime as dt
import itertools
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, NamedTuple

from trusted_router.async_settle_shadow_binding import FIXTURE_SHA256, LIFETIME
from trusted_router.async_settle_shadow_compare import Comparison, Context
from trusted_router.detached_jws import canonical
from trusted_router.services.async_settle_shadow_admission import unknown
from trusted_router.storage_operational_analytics import analytics_surrogate

SAMPLE = "async_settle_shadow_sample"
COUNTER = "async_settle_shadow_counter"
CONTROL = "async_settle_shadow_control"
KINDS = frozenset({SAMPLE, COUNTER, CONTROL})
ADAPTERS = ("openai", "anthropic", "other", "unknown")
ROUTES = ("chat.completions", "responses", "other", "unknown")
DIMENSIONS = tuple(itertools.product(ADAPTERS, ROUTES, (False, True, None)))
COUNT_FIELDS = "authorize_attempts authorize_fresh authorize_replay header_absent requested_eligible snapshot_sent settle_attempts refund_attempts envelope_present observed_attempts observed_eligible observed_ineligible observed_unknown evaluable exact explained mismatch requires_review unevaluable".split()
PHASE_FIELDS = "envelope_present observed_attempts observed_eligible observed_ineligible observed_unknown evaluable exact explained mismatch requires_review unevaluable comparison_attempts comparison_dropped samples_inserted duplicate_samples conflicting_samples booking_pending booking_unknown".split()
HIST_BOUNDS = (100, 500, 1000, 2000, 5000, 10000, 50000, 200000)
REASONS = frozenset("""untyped non_credits settlement_authority unsupported_route service_tier app_markup custom_markup receipt_fee request_fee custom_model user_model tool_cost search_cost image_cost video_cost partner liberty native_batch fusion polyphemus private_tier_basis unsupported_adapter unknown_parameters replay header_absent snapshot_unavailable usage_missing usage_estimated malformed_usage arithmetic_overflow booking_pending booking_unknown rebuild_unavailable snapshot_reconstruction_failed catalog_change go_failure configuration_conflict missing_envelope snapshot_size winner_polarity legacy_oracle_unavailable counter_overflow header_duplicate header_size base64 json_encoding json_duplicate json_shape integer proof_signature proof_expired hash identity raw_usage rate_limit queue_full daily_cap store_unavailable evidence_size worker_error""".split())
COHORT_EXCLUSIONS = frozenset("""untyped non_credits settlement_authority unsupported_route service_tier app_markup custom_markup receipt_fee request_fee custom_model user_model tool_cost search_cost image_cost video_cost partner liberty native_batch fusion polyphemus private_tier_basis unsupported_adapter unknown_parameters replay header_absent""".split())
CLASSIFICATIONS = frozenset({"hash", "identity", "normalization", "evaluator_disagreement", "requires_review", "unevaluable", "exact", "explained-by-catalog-change"})
SAMPLE_FIELDS = frozenset("""v policy_version authorization_id authorization_day observed_at_us authorize_at_us booking_observed_at_us workspace_fingerprint adapter route_type streamed model_id endpoint_id snapshot_hash payload_hash rebuilt_snapshot_hash raw_usage python_usage go_usage legacy_usage frozen_micro python_micro go_micro booked_micro rebuilt_micro legacy_frozen_micro python_minus_go booked_minus_frozen rebuilt_minus_frozen booked_minus_rebuilt classification reason_codes eligibility booking admission timing deployment provenance""".split())
OBJECT_FIELDS = {
    "eligibility": "requested observed exclusion",
    "booking": "attempted_kind outcome source price_source",
    "admission": "prediction reason tier pending_micro cap_micro workspace_age_us health_age_us health_p95_us",
    "timing": "authorize_shadow_us handoff_prepare_us router_settle_us booking_confirm_us comparator_us evidence_write_us",
    "deployment": "region instance router_revision go_revision python_evaluator go_evaluator",
    "provenance": "binding_verified raw_matches_body rebuild_matches_booking_view snapshot_transport s0_reconstruction legacy_oracle fixture_sha256",
}


def day_at(seconds: float) -> str:
    return dt.datetime.fromtimestamp(seconds, dt.UTC).date().isoformat()


def retired_day(day: str, observed_at: dt.datetime) -> bool:
    """All evidence keys retain the current UTC day and the preceding 30 days."""
    parsed = dt.date.fromisoformat(day)
    if parsed.isoformat() != day:
        raise ValueError("shadow day")
    return parsed < observed_at.date() - dt.timedelta(days=30)


def dimensions(adapter: str | None, route: str | None, streamed: bool | None) -> tuple[str, str, bool | None]:
    return (adapter if adapter in ADAPTERS else "other" if adapter else "unknown",
            route if route in ROUTES else "other" if route else "unknown", streamed)


class RetryIdentity(NamedTuple):
    kind: str
    region: str
    authorization_day: str
    adapter: str
    route_type: str
    streamed: bool | None


def retry_identity(row: dict[str, Any]) -> RetryIdentity:
    """Verified retry bucket; the adapter separately enforces the durable key."""
    return RetryIdentity(row["booking"]["attempted_kind"], row["deployment"]["region"],
                         row["authorization_day"], row["adapter"], row["route_type"], row["streamed"])


def original_can_back_retry(original: RetryIdentity, observed_at_us: int,
                            retry: RetryIdentity, started_at_us: int, flushed_at_us: int) -> bool:
    """Necessary compatibility when exact retry hashes/expiry are unavailable.

    A verified original's receipt is no earlier than binding issuance. Its
    receipt plus LIFETIME is therefore a conservative upper bound, not proof
    of validity. The comparator still enforces the signed, exclusive expiry.
    Counter windows use inclusive bounds; adapter observations use a point.
    """
    return (original == retry
            and started_at_us - LIFETIME * 1000000 <= observed_at_us <= flushed_at_us)


def retry_classification(original: dict[str, Any], retry: dict[str, Any]) -> str:
    """Classify two validated observations at the same durable sample key.

    Only a verified terminal hash identifies a reproducible payload. Diagnostic
    classifications may change on replay without changing that payload. Missing
    hashes cannot prove equality, even when both observations are unevaluable.
    """
    if original["booking"]["attempted_kind"] != retry["booking"]["attempted_kind"]:
        return "winner_polarity"
    if (original["payload_hash"] is None or retry["payload_hash"] is None
            or not original["provenance"]["binding_verified"]
            or not retry["provenance"]["binding_verified"]
            or retry_identity(original) != retry_identity(retry)
            or original["payload_hash"] != retry["payload_hash"]):
        return "conflict"
    if not original_can_back_retry(retry_identity(original), original["observed_at_us"],
                                   retry_identity(retry), retry["observed_at_us"], retry["observed_at_us"]):
        return "proof_expired"
    return "duplicate"


def sample(ctx: Context, comparison: Comparison, *, observed_us: int, router_us: int,
           comparator_us: int, booking_us: int | None, instance: str,
           revision: str, admission: dict[str, Any] | None = None) -> dict[str, Any]:
    auth = ctx.authorization
    created = dt.datetime.fromisoformat(auth.created_at.replace("Z", "+00:00"))
    fields = asdict(comparison)
    body = {key: fields[key] for key in (
        "snapshot_hash", "payload_hash", "rebuilt_snapshot_hash", "raw_usage", "python_usage",
        "go_usage", "legacy_usage", "python_micro", "go_micro", "booked_micro", "rebuilt_micro",
        "legacy_frozen_micro", "python_minus_go", "booked_minus_frozen", "rebuilt_minus_frozen",
        "booked_minus_rebuilt", "classification")}
    adapter, route, streamed = dimensions(comparison.adapter or auth.provider, ctx.body.route_type, comparison.verified_streamed)
    body.update(v=1, policy_version="shadow-v1", authorization_id=auth.id,
                authorization_day=created.date().isoformat(), observed_at_us=observed_us,
                authorize_at_us=int(created.timestamp() * 1e6),
                booking_observed_at_us=None if booking_us is None else observed_us + booking_us,
                workspace_fingerprint=analytics_surrogate("workspace", auth.workspace_id),
                adapter=adapter, route_type=route, streamed=streamed,
                model_id=auth.model_id if len(auth.model_id) <= 128 else None,
                endpoint_id=ctx.selected_endpoint if (ctx.selected_endpoint and len(ctx.selected_endpoint) <= 128
                    and ctx.selected_endpoint in (*auth.candidate_endpoint_ids, auth.endpoint_id)) else None,
                frozen_micro=comparison.python_micro, reason_codes=sorted(comparison.reasons),
                eligibility=dict(requested=comparison.binding_verified, observed=comparison.observed_eligible,
                                 exclusion=next(iter(sorted(comparison.reasons)), None) if comparison.observed_eligible is False else None),
                booking=dict(attempted_kind=ctx.attempted_kind, outcome=ctx.booking.outcome,
                             source="finalized_authorization" if ctx.booking.confirmed else "none",
                             price_source=ctx.price_source),
                admission=admission or unknown(),
                timing=dict(authorize_shadow_us=None, handoff_prepare_us=comparison.handoff_prepare_us,
                            router_settle_us=router_us, booking_confirm_us=booking_us,
                            comparator_us=comparator_us, evidence_write_us=None),
                deployment=dict(region=ctx.region, instance=instance, router_revision=revision,
                                go_revision=comparison.go_revision, python_evaluator="billing-v1", go_evaluator="billing-v1"),
                provenance=dict(binding_verified=comparison.binding_verified, raw_matches_body=comparison.raw_matches_body,
                                rebuild_matches_booking_view=ctx.rebuild_matches_booking_view,
                                snapshot_transport=comparison.snapshot_transport, s0_reconstruction=comparison.s0_reconstruction,
                                legacy_oracle="stage_d_candidate_v1" if comparison.legacy_frozen_micro is not None else "unavailable",
                                fixture_sha256=FIXTURE_SHA256))
    validate_sample(body, body["authorization_day"] + "/" + auth.id)
    return body


def validate_sample(body: dict[str, Any], identity: str) -> None:
    import re

    if set(body) != SAMPLE_FIELDS or len(canonical(body)) > 8192:
        raise ValueError("evidence_size")
    if (type(body["v"]) is not int or body["v"] != 1 or body["policy_version"] != "shadow-v1"
            or body["classification"] not in CLASSIFICATIONS
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", body["authorization_id"])
            or identity != body["authorization_day"] + "/" + body["authorization_id"]):
        raise ValueError("invalid sample")
    dt.date.fromisoformat(body["authorization_day"])
    for key, fields in OBJECT_FIELDS.items():
        if not isinstance(body[key], dict) or set(body[key]) != set(fields.split()):
            raise ValueError("invalid sample object")
    if (body["adapter"], body["route_type"], body["streamed"]) not in DIMENSIONS:
        raise ValueError("invalid dimensions")
    reasons = body["reason_codes"]
    if not isinstance(reasons, list) or len(reasons) > 8 or sorted(set(reasons)) != reasons or not set(reasons) <= REASONS:
        raise ValueError("invalid reasons")
    for name in ("snapshot_hash", "payload_hash", "rebuilt_snapshot_hash", "workspace_fingerprint"):
        if body[name] is not None and (not isinstance(body[name], str) or not re.fullmatch(r"[0-9a-f]{64}", body[name])):
            raise ValueError("invalid hash")
    for name, value in body.items():
        if name.endswith(("_micro", "_us")) or "_minus_" in name:
            if value is not None and (type(value) is not int or abs(value) > (1 << 63) - 1 or value < 0 and "_minus_" not in name):
                raise ValueError("invalid integer")
    for name, expected in (("raw_usage", "input_tokens output_tokens cache_read_tokens cache_creation_tokens reasoning_tokens"),
                           *((key, "uncached_input_tokens total_prompt_tokens output_tokens cache_read_tokens cache_creation_tokens reasoning_tokens") for key in ("python_usage", "go_usage", "legacy_usage"))):
        value = body[name]
        if value is not None and (not isinstance(value, dict) or set(value) != set(expected.split()) or
                                  any(type(n) is not int or not 0 <= n < 1 << 63 for n in value.values())):
            raise ValueError("invalid usage")
    if body["timing"]["evidence_write_us"] is not None:
        raise ValueError("self timing")
    if body["provenance"]["fixture_sha256"] != FIXTURE_SHA256:
        raise ValueError("fixture")
    for name in ("model_id", "endpoint_id"):
        if body[name] is not None and (not isinstance(body[name], str) or re.fullmatch(r"[A-Za-z0-9_./:@+\-]{1,128}", body[name]) is None):
            raise ValueError("invalid catalog identity")
    for name in ("observed_at_us", "authorize_at_us"):
        if body[name] is None:
            raise ValueError("missing time")
    if dt.datetime.fromtimestamp(body["authorize_at_us"]/1e6, dt.UTC).date().isoformat() != body["authorization_day"]:
        raise ValueError("authorize day")
    if type(body["streamed"]) not in (bool, type(None)):
        raise ValueError("stream type")
    eligibility, booking, admission = body["eligibility"], body["booking"], body["admission"]
    if (type(eligibility["requested"]) is not bool or type(eligibility["observed"]) not in (bool, type(None))
            or eligibility["exclusion"] is not None and eligibility["exclusion"] not in REASONS):
        raise ValueError("eligibility")
    if (booking["attempted_kind"] not in {"settle", "refund"} or booking["outcome"] not in {"settled", "refunded", "pending", "unknown"}
            or booking["source"] not in {"finalized_authorization", "none"}
            or booking["price_source"] not in {"catalog_at_authorize_time", "stage_d_document", "unknown"}):
        raise ValueError("booking")
    if (admission["prediction"] not in {"yes", "no", "unknown"} or admission["reason"] not in {
            "eligible", "ineligible_tier", "cap_exceeded", "drain_unhealthy", "cache_missing", "cache_stale", "cache_busy", "invalid_data"}):
        raise ValueError("admission")
    for value in [*(v for k, v in admission.items() if k not in {"prediction", "reason"}), *body["timing"].values()]:
        if value is not None and (type(value) is not int or not 0 <= value < 1 << 63):
            raise ValueError("nested integer")
    provenance = body["provenance"]
    if any(type(provenance[k]) is not bool for k in ("binding_verified", "raw_matches_body", "rebuild_matches_booking_view")):
        raise ValueError("provenance boolean")
    if (provenance["snapshot_transport"] not in {"full", "hash_only", "unknown"}
            or provenance["s0_reconstruction"] not in {"not_needed", "verified", "failed", "not_attempted"}
            or provenance["legacy_oracle"] not in {"stage_d_candidate_v1", "unavailable"}):
        raise ValueError("provenance enum")
    deployment = body["deployment"]
    for key, value in deployment.items():
        if key == "go_revision" and value is None:
            continue
        if not isinstance(value, str) or not 1 <= len(value) <= 128 or not value.isascii():
            raise ValueError("deployment")
        if key.endswith("revision") and re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ValueError("deployment revision")
        if key.endswith("evaluator") and value != "billing-v1":
            raise ValueError("evaluator identity")
    if body["classification"] in {"exact", "explained-by-catalog-change"}:
        if (provenance["binding_verified"] is not True or provenance["raw_matches_body"] is not True
                or any(body[k] is None for k in ("python_micro", "go_micro", "booked_micro", "legacy_frozen_micro"))
                or not body["python_micro"] == body["go_micro"] == body["legacy_frozen_micro"]
                or body["python_usage"] != body["go_usage"] or body["python_usage"] != body["legacy_usage"]
                or (booking["attempted_kind"], booking["outcome"]) not in {("settle", "settled"), ("refund", "refunded")}):
            raise ValueError("contradictory clean sample")
        if body["classification"] == "exact" and body["booked_micro"] != body["python_micro"]:
            raise ValueError("contradictory equality")
        if body["classification"] == "explained-by-catalog-change" and (
                body["booked_micro"] != body["rebuilt_micro"] or body["snapshot_hash"] == body["rebuilt_snapshot_hash"]
                or body["booked_micro"] == body["python_micro"] or booking["attempted_kind"] != "settle"
                or provenance["rebuild_matches_booking_view"] is not True):
            raise ValueError("contradictory explanation")
    if "winner_polarity" in body["reason_codes"] and (body["booked_minus_frozen"] is not None or body["booked_minus_rebuilt"] is not None):
        raise ValueError("winner delta")
    for delta, left, right in (("python_minus_go", "python_micro", "go_micro"),
                               ("booked_minus_frozen", "booked_micro", "frozen_micro"),
                               ("rebuilt_minus_frozen", "rebuilt_micro", "frozen_micro"),
                               ("booked_minus_rebuilt", "booked_micro", "rebuilt_micro")):
        if body[delta] is not None and (body[left] is None or body[right] is None or body[delta] != body[left] - body[right]):
            raise ValueError("invalid delta")


class Counters:
    def __init__(self, region: str, revision: str, *, clock: Any = time.time) -> None:
        self.clock = clock
        self.lock = threading.RLock()
        self.instance = str(uuid.uuid4())
        self.region, self.revision = region, revision
        self.days: dict[str, dict[str, Any]] = {}
        self.active: dict[str, int] = {}
        # CPython deque append/popleft are atomic. Bounded immutable messages
        # never acquire the worker lock; overflow permanently disqualifies coverage.
        self.mailbox: deque[tuple[int, str, float, str]] = deque(maxlen=128)
        self.mailbox_sequence = itertools.count()
        self.mailbox_expected = 0
        self.mailbox_overflow = False
        self.retired_through = ""
        self.phase: contextvars.ContextVar[str] = contextvars.ContextVar("shadow_phase", default="settle")
        self.observation_day: contextvars.ContextVar[str | None] = contextvars.ContextVar("shadow_counter_day", default=None)

    def defer(self, operation: str, observed: float, phase: str = "worker") -> None:
        if len(self.mailbox) == self.mailbox.maxlen:
            self.mailbox_overflow = True
        self.mailbox.append((next(self.mailbox_sequence), operation, observed, phase))

    @contextmanager
    def request_access(self, observed: float, phase: str = "worker") -> Iterator[bool]:
        acquired = self.lock.acquire(blocking=False)
        if not acquired:
            self.defer("drop", observed, phase)
        try:
            yield acquired
        finally:
            if acquired:
                self.lock.release()

    def release_request(self, observed: float) -> None:
        if not self.lock.acquire(blocking=False):
            self.defer("release", observed)
            return
        try:
            self.release(observed)
        finally:
            self.lock.release()

    def _drain_mailbox(self) -> None:
        # Called with the worker lock. Never chase concurrent producers forever.
        for _ in range(len(self.mailbox)):
            sequence, operation, observed, phase = self.mailbox.popleft()
            # Detect eviction even if concurrent producers race the length
            # check. Reordered producers conservatively mark the same gap.
            if sequence != self.mailbox_expected:
                self.mailbox_overflow = True
            self.mailbox_expected = sequence + 1
            if operation == "release":
                self.release(observed)
            else:
                token = self.observation_day.set(day_at(observed))
                try:
                    self.reason(dimensions(None, None, None), phase, "queue_full")
                except ValueError:
                    self.observation_day.set(None)
                    self.reason(dimensions(None, None, None), phase, "queue_full")
                finally:
                    self.observation_day.reset(token)
        if self.mailbox_overflow:
            self._day()["counter_overflow"] = True
            self._day()["first_gap_at_us"] = int(self.clock() * 1e6)

    @contextmanager
    def day(self, observed: float, phase: str = "settle") -> Iterator[None]:
        self.retain(observed)
        phase_token = self.phase.set(phase)
        token = self.observation_day.set(day_at(observed))
        try:
            yield
        finally:
            self.observation_day.reset(token)
            self.phase.reset(phase_token)
            self.release(observed)

    def retain(self, observed: float) -> None:
        """Keep a receipt day open through money work and queued observation."""
        with self.lock:
            token = self.observation_day.set(day_at(observed))
            try:
                body = self._day()
                body["started_at_us"] = min(body["started_at_us"], int(observed * 1e6))
                day = day_at(observed)
                self.active[day] = self.active.get(day, 0) + 1
            finally:
                self.observation_day.reset(token)

    def release(self, observed: float) -> None:
        with self.lock:
            day = day_at(observed)
            remaining = self.active.get(day, 0) - 1
            if remaining > 0:
                self.active[day] = remaining
            else:
                self.active.pop(day, None)


    def _day(self) -> dict[str, Any]:
        now = int(self.clock() * 1e6)
        day = self.observation_day.get() or day_at(self.clock())
        if (day <= self.retired_through and day not in self.days) or self.days.get(day, {}).get("closed"):
            # Never reopen a durable closed writer for a late/abandoned task.
            token = self.observation_day.set(None)
            try:
                if day < day_at(self.clock()):
                    self._day()["first_gap_at_us"] = now
            finally:
                self.observation_day.reset(token)
            raise ValueError("closed shadow day")
        if day not in self.days:
            self.days[day] = dict(v=1, instance=self.instance, region=self.region,
                router_revision=self.revision, policy_version="shadow-v1", started_at_us=now,
                flushed_at_us=now, sequence=0, closed=False,
                counts=[dict(adapter=a, route_type=r, streamed=s, **dict.fromkeys(COUNT_FIELDS, 0)) for a, r, s in DIMENSIONS],
                terminal_counts=[], exclusions=[], rejections=[], drops=[], dimension_overflow=0, counter_overflow=False,
                comparison_attempts=0, comparison_dropped=0, samples_inserted=0, duplicate_samples=0, conflicting_samples=0,
                booking_pending=0, booking_unknown=0, first_evidence_at_us=None,
                last_mismatch_at_us=None, first_gap_at_us=None, authorize_shadow_hist=[0]*9,
                evidence_write_hist=[0]*9,
                admission_observer=dict.fromkeys("workspace_reads health_reads read_failures missed_ticks prediction_yes prediction_no prediction_unknown".split(), 0))
        if len(self.days) > 3:
            # Retain only the observation lifetime. Unflushed evicted writers
            # remain unclosed durably; the current day also retains a gap.
            oldest = min(key for key in self.days if key != day)
            self.days.pop(oldest)
            self.active.pop(oldest, None)
            self.retired_through = max(self.retired_through, oldest)
            self.days[day]["first_gap_at_us"] = now
        return self.days[day]

    def terminal_bucket(self, day: dict[str, Any], dims: tuple[str, str, bool | None]) -> dict[str, Any]:
        identity = dict(phase=self.phase.get(), adapter=dims[0], route_type=dims[1], streamed=dims[2])
        for row in day["terminal_counts"]:
            if all(row[k] == v for k, v in identity.items()):
                return row
        row = dict(**identity, **dict.fromkeys(PHASE_FIELDS, 0))
        day["terminal_counts"].append(row)
        return row

    def increment(self, dims: tuple[str, str, bool | None], field: str, count: int = 1) -> None:
        with self.lock:
            day = self._day()
            if field in PHASE_FIELDS:
                self.add(day, self.terminal_bucket(day, dims), field, count)
            if field in COUNT_FIELDS:
                self.add(day, day["counts"][DIMENSIONS.index(dims)], field, count)
            else:
                self.add(day, day, field, count)

    def add(self, day: dict[str, Any], target: Any, key: Any, count: int = 1) -> None:
        value = target[key] + count
        if not 0 <= value < 1 << 63:
            day["counter_overflow"] = True
            day["first_gap_at_us"] = day["first_gap_at_us"] or int(self.clock()*1e6)
            value = max(0, min(value, (1 << 63)-1))
        target[key] = value

    def reason(self, dims: tuple[str, str, bool | None], phase: str, reason: str, group: str = "drops") -> None:
        if reason not in REASONS or group not in {"drops", "exclusions", "rejections"}:
            raise ValueError("unknown reason")
        with self.lock:
            day = self._day()
            if group != "exclusions" or reason not in COHORT_EXCLUSIONS:
                day["first_gap_at_us"] = day["first_gap_at_us"] or int(self.clock() * 1e6)
            a, r, s = dims
            identity = dict(phase=phase, adapter=a, route_type=r, streamed=s, reason=reason)
            for row in day[group]:
                if all(row[k] == v for k, v in identity.items()):
                    self.add(day, row, "count")
                    return
            if sum(len(day[key]) for key in ("drops", "exclusions", "rejections")) >= 128:
                self.add(day, day, "dimension_overflow")
                day["counter_overflow"] = True
                day["first_gap_at_us"] = day["first_gap_at_us"] or int(self.clock()*1e6)
            else:
                day[group].append({**identity, "count": 1})

    def histogram(self, field: str, microseconds: int) -> None:
        with self.lock:
            day = self._day()
            self.add(day, day[field], sum(microseconds > edge for edge in HIST_BOUNDS))

    def outcome(self, dims: tuple[str, str, bool | None], value: Comparison) -> None:
        with self.lock:
            day = self._day()
            bucket = day["counts"][DIMENSIONS.index(dims)]
            terminal = self.terminal_bucket(day, dims)
            if value.observed_eligible is not None:
                self.add(day, terminal, "observed_unknown", -1)
                self.add(day, terminal, "observed_eligible" if value.observed_eligible else "observed_ineligible")
                self.add(day, bucket, "observed_unknown", -1)
                self.add(day, bucket, "observed_eligible" if value.observed_eligible else "observed_ineligible")
            clean = value.classification in {"exact", "explained-by-catalog-change"}
            self.add(day, terminal, "evaluable", int(clean))
            self.add(day, bucket, "evaluable", int(clean))
            category = {"explained-by-catalog-change": "explained", "hash": "mismatch", "identity": "mismatch",
                        "normalization": "mismatch", "evaluator_disagreement": "mismatch"}.get(value.classification, value.classification)
            self.add(day, terminal, category)
            self.add(day, bucket, category)
            now = int(self.clock()*1e6)
            if category == "mismatch":
                day["last_mismatch_at_us"] = now
            excluded = value.classification == "unevaluable" and value.observed_eligible is False and bool(value.reasons) and value.reasons <= COHORT_EXCLUSIONS
            if not clean and not excluded:
                day["first_gap_at_us"] = day["first_gap_at_us"] or now

    def snapshot(self, closed: bool = False, *, retiring_only: bool = False) -> list[tuple[str, dict[str, Any]]]:
        import copy
        with self.lock:
            self._drain_mailbox()
            result = []
            for day, body in sorted(self.days.items()):
                if retiring_only and (day >= day_at(self.clock()) or self.active.get(day)):
                    continue
                self.add(body, body, "sequence")
                body["flushed_at_us"] = int(self.clock() * 1e6)
                body["closed"] = body["closed"] or ((closed or day < day_at(self.clock())) and not self.active.get(day))
                # Detach the bounded schema using only builtin shallow copies.
                # Serialization/deep-copy below own no shared mutable state and
                # hold no lock needed by request capture.
                detached = dict(body)
                for key in ("counts", "terminal_counts", "exclusions", "rejections", "drops"):
                    detached[key] = [dict(row) for row in body[key]]
                for key in ("authorize_shadow_hist", "evidence_write_hist"):
                    detached[key] = list(body[key])
                detached["admission_observer"] = dict(body["admission_observer"])
                result.append((day + "/" + self.instance, detached))
        private = [(identity, copy.deepcopy(body)) for identity, body in result]
        for _, body in private:
            if len(canonical(body)) > 65536:
                raise ValueError("counter_overflow")
        return private

    def acknowledge(self, identity: str, body: dict[str, Any]) -> None:
        """Retire only a successfully persisted final cumulative snapshot."""
        with self.lock:
            day = identity.split("/")[0]
            current = self.days.get(day)
            if body["closed"] and current is not None and current["sequence"] == body["sequence"]:
                self.days.pop(day)
                self.retired_through = max(self.retired_through, day)


def validate_manifest(body: dict[str, Any], identity: str) -> None:
    import re
    fields = set("v day instance_boot_ids router_revisions go_revisions configuration_sha256 admission_disabled_from_us admission_disabled_until_us first_evidence_at_us completeness gap_intervals proof_manifest_sha256".split())
    if set(body) != fields or len(canonical(body)) > 262144 or type(body["v"]) is not int or body["v"] != 1:
        raise ValueError("manifest schema")
    day = dt.date.fromisoformat(body["day"])
    if identity != day.isoformat()+"/manifest-v1":
        raise ValueError("manifest identity")
    roster = body["instance_boot_ids"]
    if not isinstance(roster, list) or len(roster) > 4096 or sorted(set(roster)) != roster:
        raise ValueError("manifest roster")
    for boot in roster:
        if str(uuid.UUID(boot)) != boot:
            raise ValueError("manifest boot")
    for key in ("router_revisions", "go_revisions"):
        values = body[key]
        if not isinstance(values, list) or len(values) > 64 or sorted(set(values)) != values or any(re.fullmatch(r"[0-9a-f]{40}", value) is None for value in values):
            raise ValueError("manifest revisions")
    for key in ("configuration_sha256", "proof_manifest_sha256"):
        if body[key] is None and key == "proof_manifest_sha256":
            continue
        if not isinstance(body[key], str) or re.fullmatch(r"[0-9a-f]{64}", body[key]) is None:
            raise ValueError("manifest hash")
    for key in ("admission_disabled_from_us", "admission_disabled_until_us", "first_evidence_at_us"):
        if body[key] is not None and (type(body[key]) is not int or not 0 <= body[key] < 1 << 63):
            raise ValueError("manifest timestamp")
    if body["completeness"] not in {"complete", "unknown", "gap"} or len(body["gap_intervals"]) > 128:
        raise ValueError("manifest completeness")
    for interval in body["gap_intervals"]:
        if not isinstance(interval, list) or len(interval) != 2 or any(type(n) is not int or not 0 <= n < 1 << 63 for n in interval) or interval[0] >= interval[1]:
            raise ValueError("manifest gap")
