"""Production stages built by package producers over local fake source ports."""
from __future__ import annotations

import json

from scripts.async_settle import shadow_v2_window as command
from tests.shadow_v2_pack_round5_captures import captured_day
from tests.test_async_settle_shadow_v2_pack import review
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles, check_plan, input_plan
from trusted_router.detached_jws import canonical


def production_chain(root):
    source = PackFiles(root)
    previous = []
    earlier = None
    for index, stage in enumerate(('transport', 'fleet-budgets-pre', 'fleet-budgets')):
        day = f'2026-10-{4 + index:02d}'
        inputs = root / 'captures' / stage / 'capture'
        plan = captured_day(inputs.parent, day,
            checkpoint=stage, budget_prefix='earlier/' if stage == 'fleet-budgets-pre' else '')
        files = PackFiles(inputs)
        if stage == 'fleet-budgets':
            assert earlier is not None
            old_files, approved = earlier
            files.capture(old_files, list(old_files.refs.values()))
            budget = json.loads(files.read(plan['captures']['Budget']))
            for field in ('post_scope', 'trial_record'):
                value = json.loads(files.read(budget[field]))
                value['scope'] = 'production_warmup'
                budget[field] = files.obj('production-input/' + field, value)
            budget['trial_review'] = review(files, budget['trial_record'], 'production-input/review', at_us=budget['post']['interval']['end_us'] + 1)
            budget['approved_pre'] = approved
            plan['captures']['Budget'] = files.obj('production-input/Budget', budget)
            plan['files'] = list({r['path']: r for r in [*plan['files'], *files.refs.values()]}.values())
        output = root / 'graphs' / stage
        result = command.collect_day(inputs, output, plan)
        (output / 'stage/collected.json').write_bytes(canonical(result))
        graph = PackFiles(output)
        if stage == 'fleet-budgets-pre':
            # Keep the exact pre envelope and all budget originals at their
            # initial paths. Later production inputs use a separate namespace.
            from trusted_router.async_settle_shadow_v2_pack_stage import references
            envelope = json.loads((output / 'proof.pre_budget_sha256').read_bytes())
            from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
            all_refs = check_plan(graph, result['inputs'])
            lookup = PayloadGraph({r['path']: graph.read(r) for r in all_refs})
            pending = [*references(envelope), *lookup.detail(envelope['retained']['dependencies'], 'files')]
            members = {}
            while pending:
                ref = pending.pop()
                if ref['path'] in members:
                    continue
                members[ref['path']] = ref
                raw = graph.read(ref)
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                children = list(references(value))
                if isinstance(value, dict) and value.get('format') == 'shadow-v2-live-export-a3-n':
                    prefix = ref['path'].rsplit('/', 1)[0] + '/'
                    children = [{**r, 'path': prefix + r['path']} for r in children]
                pending.extend(children)
            for ref in check_plan(graph, result['inputs']):
                if ref['path'].startswith('earlier/'):
                    members[ref['path']] = ref
                if ref['path'].startswith('loading/retained-output/'):
                    record = json.loads(graph.read(ref))
                    if record['output']['path'].startswith('earlier/'):
                        members[ref['path']] = ref
            for ref in members.values():
                graph.refs[ref['path']] = ref
            # Retained dependency pages enumerate all the plan originals.
            approved_ref = graph.put('proof.pre_budget_sha256', (output / 'proof.pre_budget_sha256').read_bytes())
            approved_review = review(graph, approved_ref, 'earlier/review-envelope', at_us=envelope['v2_load']['interval']['end_us'] + 1)
            registered = input_plan(graph, list(graph.refs.values()), days=[day], predecessors=[], prefix='earlier/inputs')
            input_ref = graph.refs['earlier/inputs/plan.json']
            budget = json.loads((inputs / plan['captures']['Budget']['path']).read_bytes())
            approved = dict(envelope=approved_ref, review=approved_review, inputs=registered,
                inputs_ref=input_ref, inputs_review=review(graph, input_ref, 'earlier/review-inputs', at_us=envelope['v2_load']['interval']['end_us'] + 2),
                allowances=budget['pre_allowances'], configuration=budget['configuration'])
            earlier = PackFiles(graph.root, refs=dict(graph.refs)), approved
        checked = result['gate']
        gate = graph.obj('stage/evaluated.json', checked)
        import hashlib
        raw = (output / 'stage/transport.json').read_bytes()
        transport = dict(parts=[graph.put(f'graph-transport/{i:05d}', raw[at:at+262144])
            for i, at in enumerate(range(0, len(raw), 262144))], bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        release = graph.put('loading/release-plan.json', (output / 'loading/release-plan.json').read_bytes())
        # Re-evaluate with the same independently named release used by copies.
        checked = command.evaluate_stage(output / 'stage/transport.json', output, stage, release['sha256'])
        gate = graph.obj('stage/reviewed-evaluation.json', checked)
        from trusted_router.async_settle_shadow_v2_pack_stage import references
        for ref in [*check_plan(graph, result['inputs']), *references(result['inputs'])]:
            graph.refs[ref['path']] = ref
        registered = input_plan(graph, list(graph.refs.values()), days=[day], predecessors=[], prefix='graph-inputs')
        proof = json.loads((output / 'proof_manifest').read_bytes())
        manifest = json.loads((output / day / 'manifest-v2.json').read_bytes())
        descriptor = source.obj('descriptors/' + stage, dict(format='shadow-v2-production-predecessor-1',
            stage=stage, source_root='graphs/' + stage, inputs=registered, release_plan=release,
            release_sha256=release['sha256'], transport=transport, result=gate,
            predecessors=list(previous), manifest_complete=manifest['complete'], proof_complete=proof['complete']))
        previous.append(descriptor)
    return previous


def link_production_day(root, plan, prior_root):
    """Use the original reviewed pre graph in a later day's budget producer."""
    files = PackFiles(root)
    prior = json.loads((prior_root / 'captures/fleet-budgets/capture/production-input/Budget').read_bytes())
    approved = prior['approved_pre']
    previous_files = PackFiles(prior_root / 'captures/fleet-budgets/capture')
    from trusted_router.async_settle_shadow_v2_pack_stage import references
    for ref in [*check_plan(previous_files, approved['inputs']), *references(approved)]:
        raw = previous_files.read(ref)
        target = root / ref['path']
        assert not target.exists() or target.read_bytes() == raw, ref['path']
        files.put(ref['path'], raw)
    budget = json.loads(files.read(plan['captures']['Budget']))
    for field in ('post_scope', 'trial_record'):
        value = json.loads(files.read(budget[field]))
        value['scope'] = 'production_warmup'
        budget[field] = files.obj('production-input/' + field, value)
    budget['trial_review'] = review(files, budget['trial_record'], 'production-input/review', at_us=budget['post']['interval']['end_us'] + 1)
    budget['approved_pre'] = approved
    plan['captures']['Budget'] = files.obj('production-input/Budget', budget)
    plan['files'] = list({r['path']: r for r in [*plan['files'], *files.refs.values()]}.values())
    return plan
