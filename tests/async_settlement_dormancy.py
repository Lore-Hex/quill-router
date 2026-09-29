"""Canonical parent capture; run as a module to reproduce the byte comparison."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from tests.fakes.spanner import _ParamTypes
from tests.test_stage_d_heartbeat import NOW, _heartbeat, _seed, _seed_reaper_counters
from trusted_router.storage_gcp_authorize import reap_expired_reservations_result


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _stable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stable(v) for v in value]
    return value


def capture(*, costs: bool = False, obligation_present: bool = True) -> str:
    cases = []
    for cohort in (False, True):
        for started in (False, True):
            for snapshot in (False, True):
                db, _ = _seed(cohort=cohort)
                if not obligation_present:
                    db.missing_tables.add("tr_async_settlement_obligation")
                _seed_reaper_counters(db)
                if cohort and started:
                    _heartbeat(db)
                reads = db.transaction_execute_sql_calls
                statements = db.transaction_execute_update_calls
                with patch('trusted_router.storage_gcp_authorize.uuid',
                           SimpleNamespace(uuid4=lambda: 'fixed-generation')):
                    result = reap_expired_reservations_result(db, _ParamTypes,
                        now=NOW+timedelta(seconds=301), snapshot_booking_enabled=snapshot)
                if costs:
                    cases.append(dict(cohort=cohort, started=started, snapshot=snapshot,
                        transaction_reads=db.transaction_execute_sql_calls-reads,
                        transaction_statements=db.transaction_execute_update_calls-statements,
                        snapshot_reads=db.snapshot_execute_sql_calls))
                else:
                    cases.append(dict(cohort=cohort, started=started, snapshot=snapshot,
                        result=asdict(result), reservations=db.reservations,
                        authorizations=db.gateway_authorizations, typed=_stable(db.typed),
                        generations=db.generation_records))
    return json.dumps(cases, sort_keys=True, default=str, separators=(',', ':'))+'\n'


def capture_legacy(*, costs: bool = False, obligation_present: bool = True) -> str:
    cases = []
    for snapshot in (False, True):
        db, _ = _seed(cohort=False)
        if not obligation_present:
            db.missing_tables.add("tr_async_settlement_obligation")
        db.gateway_authorizations.clear()  # Legacy authorization entry point.
        _seed_reaper_counters(db)
        reads = db.transaction_execute_sql_calls
        statements = db.transaction_execute_update_calls
        with patch('trusted_router.storage_gcp_authorize.utcnow', return_value=NOW):
            result = reap_expired_reservations_result(db, _ParamTypes,
                now=NOW+timedelta(seconds=301), snapshot_booking_enabled=snapshot)
        if costs:
            cases.append(dict(snapshot=snapshot,
                transaction_reads=db.transaction_execute_sql_calls-reads,
                transaction_statements=db.transaction_execute_update_calls-statements,
                snapshot_reads=db.snapshot_execute_sql_calls))
        else:
            cases.append(dict(snapshot=snapshot, result=asdict(result),
                reservations=db.reservations, authorizations=db.gateway_authorizations,
                typed=_stable(db.typed), generations=db.generation_records))
    return json.dumps(cases, sort_keys=True, default=str, separators=(',', ':'))+'\n'


if __name__ == '__main__':
    print(capture(), end='')


def capture_settlement(*, obligation_present: bool = False) -> str:
    """Capture standalone settle, both finalize modes and standalone outbox done.

    Reuse the behavioral scenarios, retaining every durable field. Freeze all
    input clocks (including the authorization factory), never output data.
    """
    from tests import test_async_settlement_obligations as scenarios

    original = scenarios._seed
    cases = []
    for outbox in (False, True):
        for mode in ('settle', 'typed', 'speculative', 'outbox'):
            if mode == 'outbox' and not outbox:
                continue
            databases = []
            def seed(*args: Any, captured: list[Any] = databases, **kwargs: Any) -> Any:
                db, auth = original(*args, **kwargs)
                captured.append(db)
                return db, auth
            with (
                patch('trusted_router.storage_models.utcnow', return_value=NOW),
                patch.object(scenarios, '_seed', seed),
                patch('trusted_router.storage_gcp_authorize.utcnow', return_value=NOW),
                patch('trusted_router.storage_gcp_settle_outbox._iso_now', return_value=NOW.isoformat()),
            ):
                scenarios.test_pre_migration_settlement(outbox, mode, obligation_present)
            db = databases[0]
            cases.append(dict(mode=mode, outbox=outbox, reservations=db.reservations,
                authorizations=db.gateway_authorizations, typed=_stable(db.typed),
                entities=_stable({k: asdict(v) for k, v in db.rows.items()}),
                outbox_rows=_stable(db.settle_outbox)))
    return json.dumps(cases, sort_keys=True, default=str, separators=(',', ':'))+'\n'
