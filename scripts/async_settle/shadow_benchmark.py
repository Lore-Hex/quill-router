"""Local CPU evidence, including complete hash-only reconstruction, not a rollout proof."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import platform
import time
from dataclasses import replace
from pathlib import Path

from tests.test_async_settle_shadow import FIXTURE, context, signer, wire
from trusted_router import billing_snapshot as b
from trusted_router.async_settle_shadow_compare import Booking, compare
from trusted_router.async_settle_shadow_evidence import sample
from trusted_router.async_settle_shadow_projection import clear_caches, project
from trusted_router.async_settle_shadow_wire import _inline_snapshot
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
from trusted_router.detached_jws import b64encode, canonical


def cases():
    at = context().authorization.created_at
    endpoints = tuple(e for e in MODEL_ENDPOINTS.values() if e.provider in {'openai','anthropic'}
        and e.usage_type == 'Credits' and e.model_id.startswith(e.provider+'/') and MODELS[e.model_id].supports_chat)
    named = ('openai/gpt-6-astra','openai/gpt-5.6-sol','openai/gpt-6.1-sol','openai/gpt-5.5')
    four = tuple(next(e for e in endpoints if e.model_id == model) for model in named)
    for group in ((four[0],), four, endpoints):
        snapshot = project(group, at)
        raw = b.RawUsage.model_validate(FIXTURE['raw_usage'])
        selected = next(e for e in group if e.id == snapshot.candidates[0].endpoint_id)
        observed = b.Eligibility()
        evaluated = b.evaluate(snapshot, selected.id, raw, observed)
        value = copy.deepcopy(FIXTURE)
        value['billing_snapshot'] = snapshot.model_dump(mode='json')
        value['terminal'].update(snapshot_hash=b.canonical_hash(snapshot), selected_endpoint=selected.id,
                                 usage=evaluated.usage.model_dump(), charge_micro=evaluated.charge_micro)
        claims = json.loads(__import__('base64').urlsafe_b64decode(value['billing_shadow_binding'].split('.')[1]+'=='))
        claims['snapshot_hash'] = b.canonical_hash(snapshot)
        value['billing_shadow_binding'] = signer().sign(claims, 1791244801)
        value['payload_hash'] = hashlib.sha256(canonical(value['terminal'])).hexdigest()
        sizes = dict(snapshot=len(b.canonical_bytes(snapshot)), decoded=len(canonical(value)), encoded=len(b64encode(canonical(value))))
        if sizes['snapshot'] > 6144 or sizes['decoded'] > 8192 or sizes['encoded'] > 12288:
            del value['billing_snapshot']
        sizes.update(transmitted_decoded=len(canonical(value)), transmitted_encoded=len(b64encode(canonical(value))))
        ctx = context()
        ctx.authorization.candidate_endpoint_ids = [e.id for e in group]
        ctx.authorization.endpoint_id = selected.id
        ctx.authorization.model_id, ctx.authorization.provider = selected.model_id, selected.provider
        ctx.body.selected_endpoint = selected.id
        ctx = replace(ctx, selected_endpoint=selected.id, booking=Booking(evaluated.charge_micro,'settled',True),
                      rebuild=lambda group=group: project(group, at))
        yield len(group), value, ctx, sizes


def benchmark(iterations=250):
    records = []
    for count, value, ctx, sizes in cases():
        headers = wire(value)
        samples = []
        keys = [signer().trusted]
        cold = []
        for i in range(iterations):
            if i < 5:
                clear_caches()
                _inline_snapshot.cache_clear()
            started = time.thread_time_ns()
            result = compare(headers, ctx, keys)
            row = sample(ctx, result, observed_us=1791244801000000, router_us=1, comparator_us=1,
                         booking_us=1, instance="00000000-0000-0000-0000-000000000001", revision="a"*40)
            canonical(row)
            elapsed = (time.thread_time_ns()-started)/1000
            assert result.classification == 'exact', result
            (cold if i < 5 else samples).append(elapsed)
        warmed = sorted(samples)
        records.append(dict(candidates=count, mode='full' if 'billing_snapshot' in value else 'hash_only',
            sizes=sizes,
            warmed_cpu_us={name:warmed[math.ceil(len(warmed)*q)-1] for name,q in [('p50',.5),('p99',.99)]},
            cold_max_us=max(cold), iterations=iterations))
        assert records[-1]['warmed_cpu_us']['p99'] <= 5000, 'shadow comparator exceeds 5 ms CPU budget'
    return dict(platform=platform.platform(), python=platform.python_version(), measurements=records)


if __name__ == '__main__':
    result = benchmark()
    target = Path('/tmp/f2b-shadow-cpu.json')  # noqa: S108 - disposable benchmark artifact
    target.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
