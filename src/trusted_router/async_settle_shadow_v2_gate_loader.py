"""A2 loading seam. Original row bytes survive; undefined payloads fail closed."""
from __future__ import annotations

from typing import Any

from trusted_router import async_settle_shadow_v2_a2_contract as a2
from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle, Key, Ports, Preimage, Row
from trusted_router.async_settle_shadow_v2_gate_loading import (
    LoadingIssue,
    error_gap,
    retained_json,
    validate_original_bytes,
)
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
from trusted_router.async_settle_shadow_v2_schema import integer, require

# Missing bindings are reported at the particular dependency, never as a
# blanket stand-in for implementing the retained adapters.
UNDEFINED_PREIMAGES: tuple[str, ...] = ()
MAX_BUNDLE_BYTES = 10000 * 262144


def load(raw: bytes, ports: Ports | None = None) -> Bundle:
    """Decode original row and A2 payload bytes, retaining fail-closed adaptation."""
    try:
        # This is the finite transport containing many original files, not one
        # FILE_REF payload. Each child keeps its own contract byte bound.
        value = retained_json(raw, limit=MAX_BUNDLE_BYTES, allow_float=False, max_depth=16)
        require(type(value) is dict and value.get('policy_version') == 'shadow-v2'
                and value.get('contract_revision') == 'a1', 'policy_version_gap')
        require(set(value) <= {'v', 'policy_version', 'contract_revision', 'rows', 'attachments', 'proof', 'ledgers',
                              'transport', 'fleet', 'pre', 'serving', 'completed_at_us', 'days', 'preimages', 'window'}, 'artifact_gap')
        def rows(name: str) -> tuple[Row, ...]:
            values = value.get(name, [])
            require(type(values) is list and len(values) <= 10000, 'artifact_gap')
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
        require(type(days) is list and len(days) <= 32 and all(type(x) is str for x in days), 'artifact_gap')
        completed = value.get('completed_at_us', 0)
        integer(completed)
        files = value.get('preimages', [])
        require(type(files) is list and len(files) <= 10000, 'artifact_gap')
        preimages = []
        payloads = {}
        issues: list[LoadingIssue] = []
        unique: dict[str, bytes] = {}
        conflicts: set[str] = set()
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
            qualified = name.split('/', 1)[-1] if '/manifest.' in name else name
            if qualified in a2.OBJECT_PREIMAGES:
                try:
                    payloads[name] = a2._object_bytes(source_raw, a2.OBJECT_PREIMAGES[qualified])
                except ValueError as exc:
                    issues.append(LoadingIssue(error_gap(exc), name, str(exc)))
        for name in conflicts:
            payloads.pop(name, None)
        decoded_rows, decoded_attachments = rows('rows'), rows('attachments')
        require(len(decoded_rows) + len(decoded_attachments) + len(ledgers) + len(preimages) + 1 <= 10000, 'artifact_gap')
        graph = PayloadGraph({k: v for k, v in unique.items() if k not in conflicts}, completed, ports=ports)
        for qualified in a2.OBJECT_PREIMAGES:
            if qualified.startswith('proof.') and qualified not in unique:
                issues.append(LoadingIssue('artifact_gap', qualified, 'required payload absent'))
        undefined: tuple[str, ...] = UNDEFINED_PREIMAGES
        inventory = None
        cohort = None
        graph.materialize(payloads)
        try:
            inventory = graph.inventory(payloads, ports)
        except ValueError as exc:
            graph.reject('manifest.independent_inventory_sha256', exc)
        except (KeyError, TypeError):
            graph.reject('manifest.independent_inventory_sha256', ValueError('evidence_schema_gap'))
        try:
            cohort = graph.cohort(payloads)
        except (ValueError, KeyError, TypeError) as exc:
            graph.reject('proof.cohort_seed_policy_sha256', exc)
        from trusted_router.async_settle_shadow_v2_gate_release import release_checks
        from trusted_router.async_settle_shadow_v2_gate_views import (
            budget_view,
            external_view,
            reset_view,
            signature_views,
        )
        views: dict[str, Any] = {}
        for path, build in (
            ('loading/release-plan.json', lambda: release_checks(graph, payloads)),
            ('proof.pre_budget_sha256', lambda: budget_view(graph, payloads, inventory, decoded_rows, decoded_attachments) if inventory else None),
            ('proof.external_proofs_sha256', lambda: external_view(graph, payloads['proof.external_proofs_sha256'])),
            ('proof.resolution_dag_sha256', lambda: reset_view(graph, payloads['proof.resolution_dag_sha256'], inventory, decoded_rows)),
            ('loading/signatures.json', lambda: signature_views(graph, decoded_rows)),
        ):
            try:
                view = build()
                views[path] = view
            except (ValueError, KeyError, TypeError, OSError, AttributeError) as exc:
                graph.reject(path, exc)
        issues.extend(graph.issues)
        graph.streams['loading_issues'] = [issue.as_dict() for issue in issues]
        bundle = Bundle(rows=decoded_rows, attachments=decoded_attachments, proof=original(value.get('proof')),
                      ledgers=tuple(x.encode('utf-8') for x in ledgers), days=tuple(days),
                      transport=original(value.get('transport')), fleet=original(value.get('fleet')),
                      pre=original(value.get('pre')), serving=original(value.get('serving')),
                      completed_at_us=completed, undefined_preimages=undefined,
                      preimages=tuple(preimages), a2_payloads=payloads, window=value.get('window'),
                      a2_streams=graph.streams, inventory=inventory, cohort=cohort,
                      budget=views.get('proof.pre_budget_sha256'), external=views.get('proof.external_proofs_sha256'),
                      reset_support=views.get('proof.resolution_dag_sha256'),
                      signatures=views.get('loading/signatures.json', ()))
        graph.streams['loading_validation'] = validate_original_bytes(bundle, ports)
        return bundle
    except (ValueError, UnicodeError):
        return Bundle(undefined_preimages=('invalid_or_cross_version_bundle', *UNDEFINED_PREIMAGES))
