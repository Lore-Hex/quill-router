"""A2 loading seam. Original row bytes survive; undefined payloads fail closed."""
from __future__ import annotations

from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_contract as a2
from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle, Key, Ports, Preimage, Row
from trusted_router.async_settle_shadow_v2_gate_loading import (
    LoadingIssue,
    error_gap,
    transport_children,
    validate_original_bytes,
)
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
from trusted_router.async_settle_shadow_v2_gate_plan import PlanInput
from trusted_router.async_settle_shadow_v2_schema import day, day_at, integer, require, sha256

# Missing bindings are reported at the particular dependency, never as a
# blanket stand-in for implementing the retained adapters.
UNDEFINED_PREIMAGES: tuple[str, ...] = ()
MAX_BUNDLE_BYTES = 10000 * 262144
ADAPTATION_ERRORS = (ValueError, KeyError, TypeError, OSError, AttributeError, IndexError, OverflowError, RecursionError)


def load(raw: bytes | PlanInput, ports: Ports | None = None, *, gate: str = 'pre-flip', _recover: bool = True) -> Bundle:
    """Decode original row and A2 payload bytes, retaining fail-closed adaptation."""
    value: Any = None
    try:
        # This is the finite transport containing many original files, not one
        # FILE_REF payload. Each child keeps its own contract byte bound.
        value, transport_issues = raw.decode() if isinstance(raw, PlanInput) else transport_children(raw, MAX_BUNDLE_BYTES)
        require(type(value) is dict and value.get('policy_version') == 'shadow-v2'
                and value.get('contract_revision') == 'a1', 'policy_version_gap')
        require(set(value) <= {'v', 'policy_version', 'contract_revision', 'rows', 'attachments', 'proof', 'ledgers',
                              'transport', 'fleet', 'pre', 'serving', 'completed_at_us', 'days', 'preimages', 'window', 'producer_registrations'}, 'artifact_gap')
        def rows(name: str) -> tuple[Row, ...]:
            values = value.get(name, [])
            require(type(values) is list, 'artifact_gap')
            found = []
            for row in values:
                require(type(row) is dict and set(row) == {'kind', 'id', 'raw'}
                        and all(type(row[k]) is str for k in row), 'artifact_gap')
                found.append(Row(Key(row['kind'], row['id']), row['raw'].encode('utf-8')))
            return tuple(found)
        def original(item: Any) -> bytes | None:
            require(item is None or type(item) is str, 'artifact_gap')
            return item.encode('utf-8') if item is not None else None
        ledgers = value.get('ledgers', [])
        require(type(ledgers) is list and len(ledgers) <= 4096 and all(type(x) is str for x in ledgers), 'artifact_gap')
        days = value.get('days', [])
        metadata_issues = [issue.path.removeprefix('transport/') for issue in transport_issues
                           if issue.path in ('transport/days', 'transport/window', 'transport/completed_at_us')]
        if 'completed_at_us' not in value:
            metadata_issues.append('completed_at_us')
        try:
            require(type(days) is list and 0 < len(days) <= 32 and days == sorted(set(days)), 'artifact_gap')
            for selected in days:
                day(selected)
        except (ValueError, TypeError):
            metadata_issues.append('days')
            days = []
        window = value.get('window')
        try:
            if window is not None:
                from trusted_router import async_settle_shadow_v2_evidence as p1
                p1.INTERVAL(window)
                require(window['start_us'] < window['end_us'])
        except (ValueError, TypeError, KeyError):
            metadata_issues.append('window')
            window = None
        completed = value.get('completed_at_us', 0)
        try:
            integer(completed)
        except ValueError:
            metadata_issues.append('completed_at_us')
            completed = 0
        files = value.get('preimages', [])
        require(type(files) is list, 'artifact_gap')
        preimages = []
        payloads = {}
        issues: list[LoadingIssue] = list(transport_issues)
        unique: dict[str, bytes] = {}
        conflicts: set[str] = set()
        from trusted_router.async_settle_shadow_v2_gate_preimages import object_preimage_schemas
        schemas = object_preimage_schemas(days)
        for source in files:
            require(type(source) is dict and set(source) == {'name', 'raw'}
                    and type(source['name']) is str and type(source['raw']) is str, 'evidence_schema_gap')
            name, source_raw = source['name'], source['raw'].encode('utf-8')
            a2.e.FILE_REF.fields['path'](name)
            if name in unique and unique[name] == source_raw:
                continue
            preimages.append(Preimage(name, source_raw))
            if name in unique:
                conflicts.add(name)
                issues.append(LoadingIssue('artifact_gap', name, 'conflicting file candidates'))
                continue
            unique[name] = source_raw
            if len(source_raw) > 262144:
                issues.append(LoadingIssue('artifact_gap', name, 'exported FILE_REF byte bound'))
                conflicts.add(name)
                continue
            if name in schemas:
                try:
                    payloads[name] = a2._object_bytes(source_raw, schemas[name])
                except ValueError as exc:
                    issues.append(LoadingIssue(error_gap(exc), name, error_gap(exc)))
        for name in conflicts:
            payloads.pop(name, None)
        decoded_rows, decoded_attachments = rows('rows'), rows('attachments')
        registrations = value.get('producer_registrations', [])
        try:
            require(type(registrations) is list, 'evidence_schema_gap')
            for registration in registrations:
                a2.e.FILE_REF(registration)
        except (ValueError, TypeError, KeyError):
            registrations = []
            issues.append(LoadingIssue('evidence_schema_gap', 'transport/producer_registrations', 'evidence_schema_gap'))
        graph = PayloadGraph({k: v for k, v in unique.items() if k not in conflicts},
                             None if 'completed_at_us' in metadata_issues else completed, ports=ports, stage=gate,
                             ledgers=tuple(item.encode('utf-8') for item in ledgers),
                             producer_registrations=tuple(registrations) if 'producer_registrations' in value else None)
        for qualified in a2.OBJECT_PREIMAGES:
            if qualified.startswith('proof.') and qualified not in unique:
                issues.append(LoadingIssue('artifact_gap', qualified, 'required payload absent'))
        undefined: tuple[str, ...] = UNDEFINED_PREIMAGES
        inventory = None
        cohort = None
        try:
            graph.materialize_gate(payloads)
        except ADAPTATION_ERRORS as exc:
            graph.reject("loading/materialize", exc)
        try:
            from trusted_router.async_settle_shadow_v2_gate_native_accounting import consume
            consume(graph, payloads, decoded_rows, decoded_attachments)
        except ADAPTATION_ERRORS as exc:
            graph.reject('loading/export-accounting', exc)
        # Materialization verifies source closure; the original proof must also
        # commit this population before either Budget or reporting can read it.
        try:
            proof_raw = original(value.get('proof'))
            require(proof_raw is not None, 'artifact_gap')
            assert proof_raw is not None
            proof_value = a1.PROOF_MANIFEST.parse(proof_raw)
            if 'proof.shutdown_inventory_sha256' not in payloads:
                raise ValueError('artifact_gap')
            require(sha256(payloads['proof.shutdown_inventory_sha256'])
                    == proof_value['shutdown_inventory_sha256'], 'artifact_gap')
        except (ValueError, KeyError) as exc:
            graph.shutdown = None
            graph.reject('proof.shutdown_inventory_sha256', exc)
        try:
            inventory = graph.inventory(payloads, ports)
        except ADAPTATION_ERRORS as exc:
            graph.reject('manifest.independent_inventory_sha256', exc)
        if inventory is not None:
            if not days:
                days = sorted({day_at(at) for boot in inventory.boots
                    for at in range(boot.start_us // 86400000000 * 86400000000,
                                    boot.end_us or completed, 86400000000)})
            if 'completed_at_us' in metadata_issues:
                completed = max((boot.end_us or boot.start_us for boot in inventory.boots), default=0)
                graph.completed_at_us = completed
        issues.extend(LoadingIssue('artifact_gap', 'transport/' + field, 'invalid transport metadata')
                      for field in metadata_issues)
        try:
            cohort = graph.cohort(payloads)
            if inventory is not None:
                from trusted_router.async_settle_shadow_v2_gate_controls import traffic_regions
                traffic_regions(decoded_rows, inventory, payloads['proof.cohort_seed_policy_sha256'])
        except ADAPTATION_ERRORS as exc:
            graph.reject('proof.cohort_seed_policy_sha256', exc)
        from trusted_router.async_settle_shadow_v2_a2_contract import REQUIRED_RULES
        from trusted_router.async_settle_shadow_v2_gate_release import release_checks
        from trusted_router.async_settle_shadow_v2_gate_views import (
            budget_view,
            external_view,
            reset_view,
            signature_views,
        )
        selected = REQUIRED_RULES[gate]
        views: dict[str, Any] = {}
        for path, dependencies, build in (
            ('loading/release-plan.json', (), lambda: release_checks(graph, payloads)),
            ('proof.pre_budget_sha256', ('proof.pre_budget_sha256', 'proof.post_budget_sha256', 'proof.cohort_seed_policy_sha256'), lambda: budget_view(graph, payloads, inventory, decoded_rows, decoded_attachments) if inventory else None),
            ('proof.external_proofs_sha256', ('proof.external_proofs_sha256',), lambda: external_view(graph, payloads['proof.external_proofs_sha256'])),
            ('proof.resolution_dag_sha256', ('proof.resolution_dag_sha256',), lambda: reset_view(graph, payloads['proof.resolution_dag_sha256'], inventory, decoded_rows)),
            ('loading/signatures.json', (), lambda: signature_views(graph, decoded_rows, payloads)),
        ):
            if path == 'proof.pre_budget_sha256' and 'G7' not in selected:
                continue
            if path == 'proof.resolution_dag_sha256' and 'G9' not in selected:
                continue
            if any(dependency not in payloads for dependency in dependencies):
                continue
            try:
                view = build()
                views[path] = view
            except ADAPTATION_ERRORS as exc:
                graph.reject(path, exc)
        # A dependency has one primary diagnostic, even when several views use it.
        diagnosed = {issue.path for issue in issues}
        for issue in graph.issues:
            if issue.path not in diagnosed:
                issues.append(issue)
                diagnosed.add(issue.path)
        graph.streams['loading_issues'] = [issue.as_dict() for issue in issues]
        bundle = Bundle(rows=decoded_rows, attachments=decoded_attachments, proof=original(value.get('proof')),
                      ledgers=tuple(x.encode('utf-8') for x in ledgers), days=tuple(days),
                      transport=original(value.get('transport')), fleet=original(value.get('fleet')),
                      pre=original(value.get('pre')), serving=original(value.get('serving')),
                      completed_at_us=completed, undefined_preimages=undefined,
                      preimages=tuple(preimages), a2_payloads=payloads, window=window,
                      producer_registrations=graph.producer_registrations,
                      a2_streams=graph.streams, inventory=inventory, cohort=cohort, shutdown=graph.shutdown,
                      budget=views.get('proof.pre_budget_sha256'), external=views.get('proof.external_proofs_sha256'),
                      reset_support=views.get('proof.resolution_dag_sha256'),
                      signatures=views.get('loading/signatures.json', ()))
        try:
            graph.streams['loading_validation'] = validate_original_bytes(bundle, ports, gate=gate)
        except ADAPTATION_ERRORS:
            # Adaptation cannot turn supplied proof bytes into an unavailable proof.
            graph.streams['loading_issues'].append(LoadingIssue(
                'evidence_schema_gap', 'loading/validation', 'original view construction failed').as_dict())
        return bundle
    except (ValueError, UnicodeError, TypeError, KeyError, RecursionError, OverflowError) as exc:
        # A malformed sibling cannot erase a present original proof commitment.
        # Recover only independently decodable transport children, with one
        # bounded retry and a permanent G1 failure for the damaged container.
        if _recover and type(value) is dict:
            from dataclasses import replace

            from trusted_router.detached_jws import canonical
            repaired: dict[str, Any] = dict(policy_version='shadow-v2', contract_revision='a1')
            remaining = 9999
            for field in ('preimages', 'rows', 'attachments', 'ledgers'):
                found = []
                candidates = value.get(field)
                for item in candidates if type(candidates) is list else []:
                    if not remaining:
                        break
                    if field == 'ledgers':
                        valid = type(item) is str
                    else:
                        fields = {'name', 'raw'} if field == 'preimages' else {'kind', 'id', 'raw'}
                        valid = type(item) is dict and set(item) == fields and all(type(v) is str for v in item.values())
                        if valid and field == 'preimages':
                            try:
                                a2.e.FILE_REF.fields['path'](item['name'])
                            except ValueError:
                                valid = False
                    if valid:
                        found.append(item)
                        remaining -= 1
                repaired[field] = found
            for field in ('proof', 'transport', 'pre', 'fleet', 'serving'):
                repaired[field] = value.get(field) if type(value.get(field)) is str else None
            for field in ('days', 'window', 'completed_at_us'):
                if field in value:
                    repaired[field] = value[field]
            recovered = load(canonical(repaired), ports, gate=gate, _recover=False)
            diagnostics = [*recovered.a2_streams.get('loading_issues', []),
                *(issue.as_dict() for issue in transport_issues),
                LoadingIssue('policy_version_gap' if str(exc) == 'policy_version_gap' else error_gap(exc),
                    'transport/container', error_gap(exc)).as_dict()]
            return replace(recovered, undefined_preimages=(*recovered.undefined_preimages, 'invalid_or_cross_version_bundle'),
                a2_streams={**recovered.a2_streams, 'loading_issues': diagnostics})
        return Bundle(undefined_preimages=('invalid_or_cross_version_bundle', *UNDEFINED_PREIMAGES))
