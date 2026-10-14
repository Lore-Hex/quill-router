"""Source-joined budget assembly over captured trial and writer originals."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a1_health as health
from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_budget import nominal
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_schema import require


def operation_join(files: Files, *, counter_ref: dict[str, Any], trace_refs: list[dict[str, Any]],
                   operation_refs: list[dict[str, Any]], contender: bool,
                   start_us: int, end_us: int, role_intervals: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    counter = a1.COUNTER.parse(files.read(counter_ref))
    require((counter['started_at_us'], counter['flushed_at_us']) == (start_us, end_us)
            and counter['closed'], 'artifact_gap')
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from trusted_router.async_settle_shadow_v2_pack_records import (
        RecordMap,
        RecordSpool,
        RecordView,
    )
    scratch = TemporaryDirectory(prefix='publisher-traces-')
    trace_records = RecordSpool(Path(scratch.name) / 'records')
    traces = RecordView(lambda: iter(trace_records))
    trace_index = RecordMap(Path(scratch.name) / 'index')
    previous = None
    for ref in trace_refs:
        page = health.PUBLISHER_TRACE_PAGE.parse(files.read(ref))
        require(page['previous_sha256'] == previous, 'publisher_trace_gap')
        from trusted_router.async_settle_shadow_v2_schema import sha256
        previous = sha256(page)
        for trace in page['entries']:
            require(trace['slot'] not in trace_index, 'publisher_trace_gap')
            trace_records.append(trace)
            trace_index[trace['slot']] = trace
    intervals = role_intervals if role_intervals is not None else [dict(
        start_us=start_us, end_us=end_us, contender=contender)]
    expected_slots = [slot for interval in intervals if interval['contender']
        for slot in range((interval['start_us'] + 1999999) // 2000000, (interval['end_us'] - 1) // 2000000 + 1)]
    require(len(expected_slots) <= 43200 and [t['slot'] for t in traces] == expected_slots, 'publisher_trace_gap')
    publisher = counter['publisher']
    health.validate_publisher_final(publisher)
    role_totals = dict(scheduled_slots=len(traces), missed_slots=0, claim_attempts=0, lost_claims=0,
        publications=0, failures=0, unknown_outcomes=0, late_cycles=0)
    cycle_counts = [0] * len(publisher['cycle_us']['counts'])
    for trace in traces:
        health.PUBLISHER_SLOT_TRACE(trace)
        require((trace['day'], trace['boot']) == (counter['day'], counter['boot']), 'publisher_trace_gap')
        key = dict(missed='missed_slots', lost_claim='lost_claims', published='publications', failed='failures')[trace['outcome']]
        role_totals[key] += 1
        role_totals['claim_attempts'] += trace['outcome'] != 'missed'
        role_totals['unknown_outcomes'] += trace['unknown_outcome']
        role_totals['late_cycles'] += trace['late_cycle']
        if trace['cycle_us'] is not None:
            cycle_counts[sum(trace['cycle_us'] > edge for edge in publisher['cycle_us']['bounds_us'])] += 1
    require(all(publisher[k] == value for k, value in role_totals.items()) and
        publisher['cycle_us']['counts'] == cycle_counts and
        sum(publisher['rpc_us']['counts']) == sum(t['rpc_count'] for t in traces), 'publisher_accounting_gap')
    require(sum(t['observed_at_ns'] is not None for t in traces) <= publisher['scans'] <=
        publisher['claims'] <= publisher['claim_attempts'] - publisher['lost_claims'], 'publisher_accounting_gap')
    require(bool(operation_refs), 'artifact_gap')
    totals: Counter[str] = Counter()
    slots: Counter[int] = Counter()
    ids = set()
    for ref in operation_refs:
        original = retained_json(files.read(ref))
        require(original['complete'] is True and original['boot'] == counter['boot']
                and original['day'] == counter['day'], 'artifact_gap')
        require(original['start_us'] == start_us and original['end_us'] == end_us, 'artifact_gap')
        for operation in original['operations']:
            require(operation['id'] not in ids and start_us <= operation['at_us'] < end_us, 'artifact_gap')
            ids.add(operation['id'])
            require(operation['operation'] in ('workspace_reads', 'health_reads', 'claims', 'scans',
                'publications', 'startup', 'close', 'export', 'begin', 'commit', 'rollback', 'stream', 'metadata', 'mutation', 'cleanup'), 'artifact_gap')
            for field in ('logical_count', 'native_rpcs', 'rows', 'bytes', 'elapsed_us'):
                require(type(operation[field]) is int and operation[field] >= 0, 'artifact_gap')
            totals[operation['operation']] += operation['logical_count']
            totals['native_rpcs'] += operation['native_rpcs']
            totals['native_bytes'] += operation['bytes']
            if operation['operation'] in ('claims', 'scans', 'publications'):
                trace = trace_index.get(operation['slot'])
                require(trace is not None, 'PublisherOperationInterval')
                assert trace is not None
                left = trace['claim_start_us']
                right = max(trace['ack_at_us'] or 0, (left or 0) + (trace['cycle_us'] or 0))
                require(left is not None and left <= operation['at_us'] <= right and
                        operation['at_us'] + operation['elapsed_us'] <= right + 1,
                        'PublisherOperationInterval')
                intervals = role_intervals if role_intervals is not None else [dict(
                    start_us=start_us, end_us=end_us, contender=contender)]
                require(any(i['contender'] and i['start_us'] <= operation['slot'] * 2000000
                    and i['start_us'] <= left <= operation['at_us'] < i['end_us']
                    and right < i['end_us'] for i in intervals), 'PublisherOperationInterval')
            if operation['slot'] is not None:
                require(operation['slot'] in trace_index, 'publisher_accounting_gap')
                slots[operation['slot']] += operation['native_rpcs']
    require(all(slots[t['slot']] == t['rpc_count'] for t in traces), 'publisher_accounting_gap')
    require(all(totals[k] == counter['publisher'][k] for k in ('claims', 'scans', 'publications')),
            'publisher_accounting_gap')
    obs = counter['admission_observer']
    require(all(totals[k] == obs[k] for k in ('workspace_reads', 'health_reads')), 'observer_budget_gap')
    return dict(totals=dict(totals), nominal=nominal(1, obs['workspace_reads'], obs['health_reads'], end_us - start_us))


@dataclass(frozen=True)
class BudgetSourceAssembly:
    pre: dict[str, Any]
    post: dict[str, Any]
    measurements: dict[str, Any]
    sources: tuple[dict[str, Any], ...]

    @classmethod
    def build(cls, files: PackFiles, plan: dict[str, Any], *, checkpoint: str) -> BudgetSourceAssembly:
        from trusted_router.async_settle_shadow_v2_pack_captured_budget import CapturedBudget
        result = CapturedBudget.build(files, plan, checkpoint=checkpoint)
        return cls(result.pre, result.post, result.measurements, result.sources)
