"""Reviewed operator stop/clear for exactly one fleet shadow control row."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from trusted_router.async_settle_shadow_journal import CONTROL_KIND, Row
from trusted_router.storage_gcp_shadow_observation import Budget, ObservationStore


def execute(store: ObservationStore, *, action: str, actor: str, reason: str,
            incident: str = '') -> dict[str, Any]:
    if action not in {'stop', 'clear'} or not reason.strip() or not actor.strip():
        raise ValueError('action, actor and reason required')
    if any(not value.isascii() or len(value) > limit for value, limit in ((actor, 256), (reason, 512))):
        raise ValueError('actor/reason must be bounded ASCII text')
    stamp = int(store.wall()*1_000_000)
    row = None
    if action == 'stop':
        row = Row.build(CONTROL_KIND, 'stop-v2', dict(v=2, policy_version='shadow-v2', actor=actor,
            stopped_at_us=stamp, reason=reason, incident=incident, source='operator',
            failure_count=0, window_seconds=0))
    result = store.runtime_stop(row, Budget(store.clock()+2, store.clock))
    return dict(kind=CONTROL_KIND, id='stop-v2', row=result.value() if result else None,
                action=action, actor=actor, reason=reason, at_us=stamp)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--clear" in argv:
        argv[argv.index("--clear")] = "clear"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', required=True)
    parser.add_argument('--instance', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--actor', required=True, help='Reviewed operator identity (no credentials)')
    sub = parser.add_subparsers(dest='action', required=True)
    stop = sub.add_parser('stop')
    stop.add_argument('--reason', required=True)
    stop.add_argument('--incident', required=True)
    clear = sub.add_parser('clear')
    clear.add_argument('--reason', required=True)
    args = parser.parse_args(argv)
    from google.cloud.spanner_v1 import Client
    store = ObservationStore(Client(project=args.project).instance(args.instance).database(args.database))
    result = execute(store, action=args.action,
                     actor=args.actor, reason=args.reason, incident=getattr(args, 'incident', ''))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
