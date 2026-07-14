"""Run a deterministic 2,000-request credit-shard lifecycle stress test.

This uses the concurrency-aware in-process Spanner fake, so it is a correctness
and contention-shape test rather than a production latency benchmark. It reports
authorize and settle separately to expose any remaining shared-row bottleneck.

Usage:
    PYTHONPATH=src:. uv run python scripts/stress_credit_shards.py
    PYTHONPATH=src:. uv run python scripts/stress_credit_shards.py --requests 2000 --concurrency 128
"""

from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_authorize import (
    AuthorizeOutcome,
    SettleOutcome,
    settle_atomic,
)
from trusted_router.storage_gcp_counters import (
    CREDIT_BALANCE_TABLE,
    KEY_LIMIT_COLUMNS,
    KEY_LIMIT_TABLE,
    key_limit_mirror_rows,
)
from trusted_router.storage_models import ApiKey, CreditAccount


@dataclass(frozen=True)
class PhaseResult:
    requests: int
    successes: int
    failures: int
    elapsed_seconds: float
    requests_per_second: float
    p50_ms: float
    p95_ms: float
    aborts: int
    error_types: dict[str, int]


@dataclass(frozen=True)
class StressResult:
    request_count: int
    concurrency: int
    shard_count: int
    estimate_micro: int
    authorize: PhaseResult
    settle: PhaseResult
    final_reserved_micro: int
    final_usage_micro: int
    final_total_credits_micro: int
    observed_key_shards: int
    final_key_usage_micro: int
    final_key_reserved_micro: int
    nonzero_key_shards: int
    max_key_shard_usage_micro: int
    invariant_clean: bool


def _percentile_ms(samples: list[float], percentile: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return ordered[index] * 1000.0


def _phase_result(
    *,
    requests: int,
    successes: int,
    elapsed: float,
    latencies: list[float],
    aborts: int,
    error_types: Counter[str],
) -> PhaseResult:
    return PhaseResult(
        requests=requests,
        successes=successes,
        failures=requests - successes,
        elapsed_seconds=elapsed,
        requests_per_second=requests / elapsed if elapsed > 0 else 0.0,
        p50_ms=statistics.median(latencies) * 1000.0 if latencies else 0.0,
        p95_ms=_percentile_ms(latencies, 0.95),
        aborts=aborts,
        error_types=dict(sorted(error_types.items())),
    )


def _seed(
    *,
    request_count: int,
    shard_count: int,
    estimate_micro: int,
) -> tuple[Any, Any, Any]:
    store, database, _ = make_fake_store()
    workspace_id = "stress-workspace"
    total_credits = request_count * estimate_micro
    base, remainder = divmod(total_credits, shard_count)
    shard_totals = [base] * shard_count
    shard_totals[0] += remainder
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(
            workspace_id=workspace_id,
            shard_count=shard_count,
        ),
    )
    table = database.typed.setdefault(CREDIT_BALANCE_TABLE, {})
    for shard, total in enumerate(shard_totals):
        table[(workspace_id, shard)] = {
            "workspace_id": workspace_id,
            "shard": shard,
            "total_credits": total,
            "total_usage": 0,
            "reserved": 0,
            "source_updated_at": None,
            "updated_at": None,
        }
    key = ApiKey(
        hash="stress-key",
        salt="stress-salt",
        secret_hash="stress-secret",  # noqa: S106 - placeholder stress fixture.
        lookup_hash="stress-lookup",
        name="stress-key",
        label="stress-key",
        workspace_id=workspace_id,
        creator_user_id=None,
        usage_shard_count=shard_count,
    )
    store._write_entity("api_key", key.hash, key)
    with database.batch() as batch:
        batch.insert_or_update(
            table=KEY_LIMIT_TABLE,
            columns=KEY_LIMIT_COLUMNS,
            values=key_limit_mirror_rows(
                key.hash,
                key,
                store._spanner.COMMIT_TIMESTAMP,
            ),
        )
    return store, database, key


def run_stress(
    *,
    request_count: int = 2_000,
    concurrency: int = 128,
    shard_count: int = 16,
    estimate_micro: int = 300_000,
) -> StressResult:
    if request_count < 1 or concurrency < 1 or shard_count < 1 or estimate_micro < 1:
        raise ValueError("stress arguments must be positive")
    store, database, key = _seed(
        request_count=request_count,
        shard_count=shard_count,
        estimate_micro=estimate_micro,
    )
    workspace_id = "stress-workspace"
    store._credit_shard_count(workspace_id)  # warm the allow-stale config cache.
    authorizations: list[Any] = []
    authorization_lock = threading.Lock()
    authorize_latencies: list[float] = []
    authorize_errors: Counter[str] = Counter()
    authorize_start_aborts = database.aborts

    def authorize(index: int) -> bool:
        started = time.perf_counter()
        outcome = ""
        authorization = None
        for attempt in range(8):
            try:
                outcome, authorization = store.authorize_gateway_typed(
                    workspace_id=workspace_id,
                    key_hash=key.hash,
                    estimate=estimate_micro,
                    has_credit_candidate=True,
                    reservation_usage_type="Credits",
                    model_id="stress-model",
                    provider="stress-provider",
                    requested_model_id=None,
                    candidate_model_ids=["stress-model"],
                    region="test",
                    endpoint_id="stress-endpoint",
                    candidate_endpoint_ids=["stress-endpoint"],
                    idempotency_key=f"stress-{index}",
                    idempotency_fingerprint="stress-body-v1",
                    key_usage_shards=key.usage_shard_count,
                )
            except Exception as exc:  # noqa: BLE001 - stress harness classifies failures.
                elapsed = time.perf_counter() - started
                with authorization_lock:
                    authorize_latencies.append(elapsed)
                    authorize_errors[type(exc).__name__] += 1
                return False
            if outcome != AuthorizeOutcome.INSUFFICIENT_CREDITS or attempt == 7:
                break
            time.sleep(0.002)
        elapsed = time.perf_counter() - started
        with authorization_lock:
            authorize_latencies.append(elapsed)
            if authorization is not None:
                authorizations.append(authorization)
            if outcome != AuthorizeOutcome.ACCEPTED:
                authorize_errors[str(outcome)] += 1
        return outcome == AuthorizeOutcome.ACCEPTED and authorization is not None

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        authorize_successes = sum(
            1
            for future in as_completed(
                executor.submit(authorize, index) for index in range(request_count)
            )
            if future.result()
        )
    authorize_elapsed = time.perf_counter() - started
    authorize_result = _phase_result(
        requests=request_count,
        successes=authorize_successes,
        elapsed=authorize_elapsed,
        latencies=authorize_latencies,
        aborts=database.aborts - authorize_start_aborts,
        error_types=authorize_errors,
    )

    settle_latencies: list[float] = []
    settle_errors: Counter[str] = Counter()
    settle_lock = threading.Lock()
    settle_start_aborts = database.aborts

    def settle(authorization: Any) -> bool:
        started_at = time.perf_counter()
        try:
            result = settle_atomic(
                store._database,
                store._param_types,
                reservation_id=authorization.credit_reservation_id,
                actual_micro=estimate_micro,
                settled_usage_type="Credits",
                success=True,
            )
        except Exception as exc:  # noqa: BLE001 - stress harness classifies failures.
            elapsed = time.perf_counter() - started_at
            with settle_lock:
                settle_latencies.append(elapsed)
                settle_errors[type(exc).__name__] += 1
            return False
        elapsed = time.perf_counter() - started_at
        with settle_lock:
            settle_latencies.append(elapsed)
            if result["outcome"] != SettleOutcome.SETTLED:
                settle_errors[str(result["outcome"])] += 1
        return result["outcome"] == SettleOutcome.SETTLED

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        settle_successes = sum(
            1
            for future in as_completed(
                executor.submit(settle, authorization)
                for authorization in authorizations
            )
            if future.result()
        )
    settle_elapsed = time.perf_counter() - started
    settle_result = _phase_result(
        requests=len(authorizations),
        successes=settle_successes,
        elapsed=settle_elapsed,
        latencies=settle_latencies,
        aborts=database.aborts - settle_start_aborts,
        error_types=settle_errors,
    )

    rows = list(database.typed[CREDIT_BALANCE_TABLE].values())
    final_reserved = sum(int(row["reserved"]) for row in rows)
    final_usage = sum(int(row["total_usage"]) for row in rows)
    final_total = sum(int(row["total_credits"]) for row in rows)
    key_rows = [
        row
        for (row_key_hash, _shard), row in database.typed[KEY_LIMIT_TABLE].items()
        if row_key_hash == key.hash
    ]
    final_key_usage = sum(int(row["usage"]) for row in key_rows)
    final_key_reserved = sum(int(row["reserved"]) for row in key_rows)
    invariant_clean = (
        authorize_successes == request_count
        and settle_successes == request_count
        and final_reserved == 0
        and final_usage == request_count * estimate_micro
        and final_total == request_count * estimate_micro
        and len(key_rows) == shard_count
        and final_key_usage == request_count * estimate_micro
        and final_key_reserved == 0
        and all(
            int(row["total_usage"]) + int(row["reserved"])
            <= int(row["total_credits"])
            for row in rows
        )
    )
    return StressResult(
        request_count=request_count,
        concurrency=concurrency,
        shard_count=shard_count,
        estimate_micro=estimate_micro,
        authorize=authorize_result,
        settle=settle_result,
        final_reserved_micro=final_reserved,
        final_usage_micro=final_usage,
        final_total_credits_micro=final_total,
        observed_key_shards=len(key_rows),
        final_key_usage_micro=final_key_usage,
        final_key_reserved_micro=final_key_reserved,
        nonzero_key_shards=sum(1 for row in key_rows if int(row["usage"]) > 0),
        max_key_shard_usage_micro=max(
            (int(row["usage"]) for row in key_rows),
            default=0,
        ),
        invariant_clean=invariant_clean,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=2_000)
    parser.add_argument("--concurrency", type=int, default=128)
    parser.add_argument("--shards", type=int, default=16)
    parser.add_argument("--estimate-micro", type=int, default=300_000)
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = run_stress(
        request_count=args.requests,
        concurrency=args.concurrency,
        shard_count=args.shards,
        estimate_micro=args.estimate_micro,
    )
    print(json.dumps(asdict(result), indent=2, sort_keys=True))
    return 0 if result.invariant_clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
