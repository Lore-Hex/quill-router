"""Select PR D pins from timing exports and measured concurrency sweep trials.

No defaults are production recommendations. Sweep rows must include the same
traffic/window as the capacity export; operator-supplied RPC room is explicit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from trusted_router.config import Settings

if __package__:
    from .drain_capacity import parse_lines, summarize
else:
    from drain_capacity import parse_lines, summarize


def select(rows: list[dict[str, Any]], trials: list[dict[str, Any]], *, start: float,
           end: float, bucket_seconds: float, margin: float, backlog: int,
           target_seconds: float, rpc_room_seconds: float) -> dict[str, Any]:
    if not math.isfinite(rpc_room_seconds) or rpc_room_seconds <= 0:
        raise ValueError("explicit positive claim/resolution RPC room required")
    capacity = summarize(rows, start=start, end=end, bucket_seconds=bucket_seconds,
                         margin=margin, backlog=backlog, target_seconds=target_seconds)
    peak = capacity['peak_arrival_rate']
    if peak is None:
        raise ValueError("arrival observations required")
    accepted = []
    rejected = []
    for trial in trials:
        try:
            numbers = ('poll_seconds', 'batch', 'concurrency', 'lease_seconds', 'pass_seconds',
                       'service_rate', 'claim_seconds', 'resolution_seconds', 'work_seconds',
                       'booking_p95_seconds', 'completion_max_seconds', 'crash_reclaim_seconds',
                       'burst_clear_seconds')
            if any(type(trial[n]) not in (int, float) or not math.isfinite(trial[n]) or trial[n] < 0 for n in numbers):
                raise ValueError("invalid measured trial")
            pins = dict(settle_outbox_fast_drain_enabled=True,
                settle_outbox_poll_interval_seconds=trial['poll_seconds'],
                settle_outbox_claim_batch=trial['batch'],
                settle_outbox_worker_concurrency=trial['concurrency'],
                settle_outbox_lease_seconds=trial['lease_seconds'],
                settle_outbox_pass_budget_seconds=trial['pass_seconds'])
            Settings(environment='test', **pins)
            required = max(peak * (1 + margin), peak + backlog / target_seconds)
            room = max(rpc_room_seconds, trial['claim_seconds'] + trial['resolution_seconds'])
            if (trial['service_rate'] <= required
                    or trial['work_seconds'] + room > trial['pass_seconds']
                    or trial['pass_seconds'] + room >= trial['lease_seconds']
                    or trial['booking_p95_seconds'] > 5 or trial['completion_max_seconds'] > 60
                    or trial['crash_reclaim_seconds'] > 60
                    or trial['burst_clear_seconds'] > target_seconds
                    or any(trial[k] != 0 for k in ('lease_losses', 'fence_misses'))
                    or trial['contention_acceptable'] is not True
                    or trial['health_freshness_pass'] is not True
                    or trial['repair_included'] is not True):
                raise ValueError("capacity, RPC room, contention, health or recovery gate")
            accepted.append((trial, pins, room))
        except (KeyError, ValueError) as exc:
            rejected.append({'trial': trial.get('id'), 'reason': str(exc)})
    if not accepted:
        return dict(status='BLOCKED', capacity=capacity, rejected=rejected)
    trial, pins, room = min(accepted, key=lambda item: (
        item[0]['concurrency'], item[0]['batch'], item[0]['poll_seconds'], item[0]['id']))
    return dict(status='PASS', capacity=capacity, trial=trial, rpc_room_seconds=room,
                recovery_margin=margin, burst_backlog=backlog, target_seconds=target_seconds,
                pins=[f"TR_{k.upper()}={str(v).lower()}" for k, v in pins.items()], rejected=rejected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--trials', type=Path, required=True)
    for name in ('start', 'end', 'bucket-seconds', 'margin', 'target-seconds', 'rpc-room-seconds'):
        parser.add_argument('--' + name, type=float, required=True)
    parser.add_argument('--backlog', type=int, required=True)
    args = parser.parse_args()
    result = select(parse_lines(args.input.read_text()), json.loads(args.trials.read_text()),
        **{key: value for key, value in vars(args).items() if key not in {'input', 'trials'}})
    result['exports'] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in (args.input, args.trials)}
    print(json.dumps(result, indent=2, allow_nan=False))
    raise SystemExit(0 if result['status'] == 'PASS' else 1)


if __name__ == '__main__':
    main()
