"""PR C safety mutations in a disposable copy; never modify the working tree."""
# ruff: noqa: S108 - explicit disposable evidence paths
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLER = 'src/trusted_router/services/async_settle_handler.py'
STORAGE = 'src/trusted_router/storage_gcp_async_settle.py'
TEST = 'tests/test_async_settle_handler.py::'
MUTATIONS = [
    ('oracle-native-cost-plus-one', [('src/trusted_router/routes/internal/gateway.py', [
        ('        return cost_microdollars\n    _require_native_batch_route_binding',
         '        return cost_microdollars + 1\n    _require_native_batch_route_binding'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('oracle-ordinary-partner-free', [('src/trusted_router/partner_billing.py', [
        ('    return None\n', '    return PartnerBillingMode.INTERNAL\n'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('persist-wrong-model', [(HANDLER, [
        ('model_id=candidate.model_id,', 'model_id="wrong/model",'),
    ])], 'tests/test_async_settle_proof.py::test_four_path_billing_state[component_half_up]'),
    ('error-envelope-message', [(HANDLER, [
        ('Invalid async settlement snapshot', 'Changed async settlement snapshot'),
    ])], 'tests/test_async_settle_proof.py::test_error_envelopes_real_http[invalid_snapshot]'),
    ('gate-recovery-dispatch-on-admission', [('src/trusted_router/routes/settlements.py', [
        ('if not settings.async_settle_protection or modes not in',
         'if not settings.async_settle_admission_enabled or modes not in'),
    ])], 'test_snapshot_dispatch_after_admission_rollback[sync-fresh-rollback]'),
    ('remove-atomic-check', [(STORAGE, [
        ('    statements.append(async_reservation_admission_statement(pt, row))', ''),
        ('[*intent_insert_counts(statements), (1,)]', 'intent_insert_counts(statements)'),
        ('if len(actual) == len(statements) and actual[-1] == 0:', 'if False:'),
    ])], 'test_admission_miss_rolls_back'),
    ('accept-zero-admission', [(STORAGE, [
        ('[*intent_insert_counts(statements), (1,)]', '[*intent_insert_counts(statements), (0, 1)]'),
        ('if len(actual) == len(statements) and actual[-1] == 0:', 'if False:'),
    ])], 'test_admission_miss_rolls_back'),
    ('refresh-conflicting-payload', [(HANDLER, [
        ('    if existing.async_version != 1 or existing.payload_hash != row.payload_hash:\n'
         '        raise api_error(409, "Settlement intent already exists with a different payload", "conflict")',
         '    if existing.payload_hash != row.payload_hash:\n'
         '        outbox.enqueue(row)\n'
         '        existing = outbox.get(row.authorization_id, row.intent_kind)'),
    ]), ('src/trusted_router/storage_gcp_settle_outbox.py', [
        ('("AND async_version IS NULL " if self._async_fence else "")', '""'),
    ])], 'test_duplicate_conflict_expired_and_immutable'),
    ('drop-hash-comparisons', [(HANDLER, [
        ('if billing.canonical_hash(value.snapshot) != claims.snapshot_hash:', 'if False:'),
    ]), ('src/trusted_router/billing_snapshot.py', [
        ('if envelope.snapshot_hash != canonical_hash(snapshot):', 'if False:'),
    ])], 'test_handler_matrix[hash-400-None]'),
    ('skip-admission-recheck', [(HANDLER, [
        ('if runtime.admission is None or not runtime.admission.eligible(',
         'if False and (runtime.admission is None or not runtime.admission.eligible('),
        ('claims.workspace_id, settings.async_settle_pilot_cap_micro):',
         'claims.workspace_id, settings.async_settle_pilot_cap_micro)):'),
    ])], 'test_handler_matrix[health-200-drain_unhealthy]'),
    ('credit-release-in-enqueue', [(STORAGE, [
        ('    opened: list[Any] = []',
         '    from trusted_router.storage_gcp_counter_dml import release_credit_no_debt_statement\n'
         '    statements.insert(1, release_credit_no_debt_statement(pt, row.workspace_id, 1, 0, shard=0))\n'
         '    counts.insert(1, (1,))\n    opened: list[Any] = []'),
    ])], 'test_enqueue_batch_no_money'),
    ('silently-extend-handoff', [(HANDLER, [
        ('started + (2 if synchronous else 0.5)', 'started + 2'),
    ])], 'test_budget_includes_admission_and_retries'),
    ('skip-status-owner-check', [('src/trusted_router/routes/settlements.py', [
        ('if row is None or row.async_version != 1 or row.workspace_id != principal.workspace.id:',
         'if row is None or row.async_version != 1:'),
        ('if (auth is None or auth.workspace_id != principal.workspace.id\n'
         '                or principal.api_key is None or auth.key_hash != principal.api_key.hash):',
         'if auth is None:'),
    ])], 'test_drain_and_status_ownership[settle]'),
    ('refresh-fence', [('src/trusted_router/storage_gcp_settle_outbox.py', [
        ('("AND async_version IS NULL " if self._async_fence else "")', '""'),
    ])], 'test_duplicate_conflict_expired_and_immutable'),
    ('lookup-audience', [('src/trusted_router/async_settle_ticket.py', [
        ('or parsed.aud != key.aud or parsed.aud != "router-settlement"', ''),
    ])], 'test_lookup_ticket_rejects_other_audience'),
    ('status-key', [('src/trusted_router/routes/settlements.py', [
        ('or principal.api_key is None or auth.key_hash != principal.api_key.hash',
         'or principal.api_key is None'),
    ])], 'test_drain_and_status_ownership[settle]'),
    ('fence-when-protection-off', [('src/trusted_router/storage_gcp_counter_dml.py', [
        ('if outbox_available and async_fence:', 'if outbox_available:'),
    ])], 'test_claim_sql_protection_pin[False]'),
    ('drop-fence-when-protection-on', [('src/trusted_router/storage_gcp_counter_dml.py', [
        ('if outbox_available and async_fence:', 'if False:'),
    ])], 'test_claim_sql_protection_pin[True]'),
    # Additional evidence: the frozen oracle itself kills ungated helper work.
    ('oracle-ungated-claim', [('src/trusted_router/storage_gcp_counter_dml.py', [
        ('if outbox_available and async_fence:', 'if outbox_available:'),
    ])], 'tests/test_async_settle_oracle.py::test_frozen_main_effects_and_operation_trace[False-True-ordinary]'),
    ('oracle-ungated-reconciliation', [('src/trusted_router/routes/internal/gateway.py', [
        ('if settings.async_settle_protection and getattr(STORE, "_database", None) is not None:',
         'if getattr(STORE, "_database", None) is not None:'),
    ])], 'tests/test_async_settle_oracle.py::test_frozen_main_effects_and_operation_trace[False-False-unresolved]'),
    ('oracle-ungated-refresh', [('src/trusted_router/storage_gcp_settle_outbox.py', [
        ('("AND async_version IS NULL " if self._async_fence else "")', '"AND async_version IS NULL "'),
    ])], 'tests/test_async_settle_oracle.py::test_frozen_main_effects_and_operation_trace[True-True-refresh]'),

    ('amount-comparison-removal', [(HANDLER, [
        ('if amount != value.terminal.charge_micro:', 'if False:'),
    ]), ('src/trusted_router/billing_snapshot.py', [
        ('if envelope.charge_micro != expected or envelope.usage != evaluated.usage:',
         'if envelope.usage != evaluated.usage:'),
    ])], 'test_handler_matrix[charge-409-None]'),
    ('late-confirmation-acceptance', [(HANDLER, [
        ('if result is not None and time.monotonic() < deadline:', 'if result is not None:'),
    ])], 'test_commit_handoff_boundary[0.501]'),
    ('dead-as-failed-status', [(HANDLER, [
        ('row.status == "release_approved"', 'row.status in {"release_approved", "dead"}'),
    ])], 'test_mark_park_never_rewrites_async_metadata'),
    ('protection-follows-admission', [(path, [
        ('"async_settle_protection"', '"async_settle_enabled"'),
    ]) for path in ('src/trusted_router/storage_gcp.py',
                    'src/trusted_router/services/settle_outbox_drain.py')] + [
        ('src/trusted_router/routes/internal/gateway.py', [
            ('settings.async_settle_protection', 'settings.async_settle_enabled'),
        ]),
    ], 'test_legacy_retry_preserves_accepted_amount[settle-False-False]'),
    ('second-cleanup-budget', [('src/trusted_router/storage_gcp_io.py', [
        ('if getattr(transaction, "_tr_async_cleanup_attempted", False):', 'if False:'),
    ])], 'test_late_batch_cleanup_chain_has_one_budget[True]'),
    ('insert-uniqueness-fake', [('tests/fakes/spanner.py', [
        ('raise FakeAlreadyExists(str(pk))  # duplicate PK', 'pass  # duplicate PK mutant'),
    ])], 'tests/test_async_settle_proof_faults.py::test_insert_uniqueness_and_preserve_existing'),
    ('preserve-existing', [('src/trusted_router/storage_gcp_settle_outbox.py', [
        ('if preserve_existing:', 'if False:'),
    ])], 'tests/test_async_settle_proof_faults.py::test_insert_uniqueness_and_preserve_existing'),
    ('claim-not-exists', [('src/trusted_router/storage_gcp_counter_dml.py', [
        (' AND NOT EXISTS (SELECT 1 FROM tr_settle_outbox a ', ' AND EXISTS (SELECT 1 FROM tr_settle_outbox a '),
    ])], 'tests/test_async_settle_proof_faults.py::test_fake_rejects_dropped_predicate[claim_not_exists]'),
    ('atomic-settled-predicate', [('src/trusted_router/storage_gcp_async_settle.py', [
        ('authorization_id=@aid AND settled=false', 'authorization_id=@aid'),
    ])], 'tests/test_async_settle_proof_faults.py::test_fake_rejects_dropped_predicate[atomic_settled]'),
    ('admission-sentinel', [('src/trusted_router/storage_gcp_async_admission.py', [
        ('LIMIT 1001', 'LIMIT 1000'),
    ])], 'tests/test_async_settle_proof_faults.py::test_fake_rejects_dropped_predicate[sentinel]'),
    ('reaper-guard', [('src/trusted_router/storage_gcp_settle_outbox.py', [
        ('GUARD_STATUSES = ("pending", "dead")', 'GUARD_STATUSES = ("dead",)'),
    ])], 'test_enqueue_wins_reaper_and_legacy_fence'),

    ('oracle-generation-amount-plus-one', [('src/trusted_router/storage_models.py', [
        ('total_cost_microdollars=actual_cost_microdollars,',
         'total_cost_microdollars=actual_cost_microdollars + 1,'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('oracle-finalization-input-plus-123', [('src/trusted_router/storage_models.py', [
        ('max(0, int(generation.tokens_prompt)) if generation is not None else 0',
         'max(0, int(generation.tokens_prompt)) + 123 if generation is not None else 0'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('handler-zero-output-usage', [(HANDLER, [
        ('actual_output_tokens=usage.output_tokens,', 'actual_output_tokens=0,'),
    ])], 'tests/test_async_settle_proof.py::test_four_path_billing_state[component_half_up]'),

]


# Round-4 independent seed-4 mutations, plus the two money-changing guard
# escapes and reverse import witness. Exact edits retained for reproducibility.
MUTATIONS += [
    ('review-generation-json-spacing', [('src/trusted_router/storage_gcp_generation_records.py', [('separators=(",", ":")', 'separators=(", ", ": ")')])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-generation-retention-null', [('src/trusted_router/storage_gcp_generation_records.py', [('"terminal_at": terminal_at,', '"terminal_at": None,')])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-wire-dollar-conversion', [('src/trusted_router/money.py', [('return float(microdollars) / MICRODOLLARS_PER_DOLLAR', 'return float(microdollars + 1) / MICRODOLLARS_PER_DOLLAR')])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-reservation-amount', [('src/trusted_router/storage_gcp_counter_dml.py', [('"actual": int(actual_micro),', '"actual": int(actual_micro) + 1,')])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-authorization-region', [('src/trusted_router/storage_models.py', [('self.finalized_region = generation.region if generation is not None else self.region', 'self.finalized_region = "wrong-region"')])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-config-lease', [('src/trusted_router/config.py', [('settle_outbox_lease_seconds: int = Field(default=300,', 'settle_outbox_lease_seconds: int = Field(default=299,')])], 'tests/test_async_settle_proof_faults.py::test_local_ttl_pins'),
    ('review-money-rounding', [('src/trusted_router/money.py', [('(raw + TOKENS_PER_MILLION // 2) // TOKENS_PER_MILLION', 'raw // TOKENS_PER_MILLION')])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-generation-retention-type', [('src/trusted_router/storage_gcp_generation_records.py', [('"terminal_at": param_types.TIMESTAMP,', '"terminal_at": param_types.STRING,')])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-config-health', [('src/trusted_router/config.py', [('settle_outbox_health_publish_interval_seconds: float = Field(default=2,', 'settle_outbox_health_publish_interval_seconds: float = Field(default=3,')])], 'tests/test_async_settle_proof_faults.py::test_local_ttl_pins'),
    ('review-generation-id', [('src/trusted_router/storage_models.py', [("f'trustedrouter:{authorization_id}'", "f'wrong:{authorization_id}'")])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-preexisting-worker-cost-bridge', [('src/trusted_router/routes/internal/gateway.py', [('        return cost_microdollars\n    _require_native_batch_route_binding', '        return cost_microdollars + 1\n    _require_native_batch_route_binding')]), ('tests/test_async_settle_proof_oracle.py', [('def test_frozen_main_provenance():', "\n@pytest.fixture(autouse=True)\ndef review_existing_worker_bridge(monkeypatch):\n    from concurrent.futures import ThreadPoolExecutor\n    from trusted_router.routes.internal import gateway as live_gateway\n    with ThreadPoolExecutor(max_workers=1) as pool:\n        pool.submit(lambda: None).result()\n        live_cost = live_gateway._native_batch_cost_or_error\n        def callback(*args, **kwargs):\n            return pool.submit(live_cost, *args, **kwargs).result()\n        monkeypatch.setattr(module('routes.internal.gateway'), '_native_batch_cost_or_error', callback)\n        yield\n\ndef test_frozen_main_provenance():")])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-warmed-cache-cost-bridge', [('src/trusted_router/routes/internal/gateway.py', [('        return cost_microdollars\n    _require_native_batch_route_binding', '        return cost_microdollars + 1\n    _require_native_batch_route_binding')]), ('tests/test_async_settle_proof_oracle.py', [('def test_frozen_main_provenance():', '\n@pytest.fixture(autouse=True)\ndef review_cached_bridge(monkeypatch):\n    from functools import partial, lru_cache\n    from trusted_router.routes.internal import gateway as live_gateway\n    live_cost = lru_cache(maxsize=1)(live_gateway._native_batch_cost_or_error)\n    assert live_cost(2, route_type="chat.completions", provider="openai", idempotency_key=None,\n                     native_batch_eligible=False, selected_usage_type=live_gateway.UsageType.CREDITS) == 3\n    monkeypatch.setattr(module(\'routes.internal.gateway\'), \'_native_batch_cost_or_error\', partial(live_cost))\n    yield\n\ndef test_frozen_main_provenance():')])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-production-imports-snapshot', [('src/trusted_router/storage_errors.py', [('from __future__ import annotations', 'from __future__ import annotations\nfrom tests.fakes.frozen_package import module as _review_snapshot_module')])], 'tests/test_async_settle_proof_oracle.py::test_production_import_fence'),
]


# Round 7: deleting one GC edge kind must make its dormant witness go red.
# Mutation-only type dispatch deliberately demonstrates what the real walker
# must never do. These rows alter tests/fakes only, never snapshot bytes.
_REFERENCE_MUTATIONS = [
    ('partial', "type(value).__name__ == 'partial'", 'test_guard_reviewer_references[partial_cache]'),
    ('namespace', "type(value).__name__ == 'SimpleNamespace'", 'test_guard_reviewer_references[simple_namespace]'),
    ('function-default', "isinstance(value, FunctionType)", 'test_guard_reviewer_references[external_default]'),
    ('function-globals', "isinstance(value, FunctionType)", 'test_guard_reviewer_references[copied_globals]'),
    ('shared-io', "type(value).__name__ == 'partial'", 'test_guard_reviewer_references[shared_fake_io]'),
    ('bound-cache-call', "type(value).__name__ == 'method-wrapper'", 'test_guard_reviewer_references[cached_bound_call]'),
    ('dataclass-factory', "isinstance(value, type)", 'test_guard_reviewer_references[dataclass_factory]'),
    ('validator', "isinstance(value, type)", 'test_guard_reviewer_references[pydantic_validator]'),
    ('closure-cell', "type(value).__name__ == 'cell'", 'test_guard_reviewer_references[captured_callback]'),
    ('private-slot', "type(value).__name__ == 'Holder'", 'test_guard_reviewer_separate_warmed_cache[private_slot]'),
    ('mapping-proxy', "type(value).__name__ == 'mappingproxy'", 'test_guard_reviewer_separate_warmed_cache[mapping_proxy]'),
    ('nested-mapping-slot', "type(value).__name__ == 'mappingproxy'", 'test_guard_gc_composed_witness[nested_mapping_slot]'),
    ('code-constants', "isinstance(value, CodeType)", 'test_guard_gc_composed_witness[code_constants]'),
    ('frozen-closure', "type(value).__name__ == 'cell'", 'test_guard_gc_composed_witness[frozen_closure]'),
    ('class-descriptor', "type(value).__name__ == 'property'", 'test_guard_gc_composed_witness[class_descriptor]'),
    ('dataclass-frozenset-tuple', "type(value).__name__ == 'frozenset'", 'test_guard_gc_composed_witness[dataclass_frozenset_tuple]'),
]
MUTATIONS += [
    ('reference-stop-' + name, [('tests/fakes/frozen_package.py', [
        ('        yield value\n', '        yield value\n        if ' + condition + ':\n            continue\n'),
    ])], 'tests/test_async_settle_proof_oracle.py::' + witness)
    for name, condition, witness in _REFERENCE_MUTATIONS
]
MUTATIONS += [
    ('reference-omit-datetime-tzinfo', [('tests/fakes/frozen_package.py', [
        ('pending.append(_Datetime.tzinfo.__get__(value))', 'pass'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_atomic_tzinfo_cache[datetime]'),
    ('reference-omit-time-tzinfo', [('tests/fakes/frozen_package.py', [
        ('pending.append(_Time.tzinfo.__get__(value))', 'pass'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_atomic_tzinfo_cache[time]'),
    ('reference-omit-typing-registry-caches', [('tests/fakes/frozen_package.py', [
        ('    caches.update(shared_runtime_caches)', ''),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_clears_registry_only_typing_cache'),
    ('reference-ignore-explicit-cache-root', [('tests/fakes/frozen_package.py', [
        (' and identity not in explicit', ''),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_explicit_typing_cache_root_is_inspected'),
    ('reference-purge-external-cache-before-audit', [('tests/fakes/frozen_package.py', [
        ('identity in shared_runtime_caches and identity not in explicit',
         "identity in shared_runtime_caches or _owner(cache) == 'review_external'"),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_reviewer_references[external_cache_result]'),
    ('reference-omit-code-supplement', [('tests/fakes/frozen_package.py', [
        ('pending.extend(field.__get__(value) for field in _CODE_MEMBERS)', 'pass'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_reference_walk_atomic_code_constants'),
    ('reference-omit-object-bound', [('tests/fakes/frozen_package.py', [
        ('assert len(visited) < max_objects', 'assert True'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_reference_walk_bound_and_cycle'),
    ('reference-omit-code-provenance', [('tests/fakes/frozen_package.py', [
        ('else value if value_type is CodeType else None)', 'else None)'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_live_code_object_argument'),
    ('profile-omit-existing-worker-check', [('tests/fakes/frozen_package.py', [
        ('assert not foreign,', 'assert True,'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_preexisting_worker_cost_bridge'),
]
MUTATIONS += [
    ('profile-omit-' + name, [('tests/fakes/frozen_package.py', [
        ('        if live:\n', '        if False:\n'),
    ])], 'tests/test_async_settle_proof_oracle.py::' + witness)
    for name, witness in [
        ('dynamic-import', 'test_guard_dynamic_import'),
        ('thread', 'test_guard_has_no_omitted_function_or_module_exemption[worker]'),
        ('raw-thread', 'test_guard_raw_thread_finishes_before_exit[_thread.start_new_thread]'),
    ]
]


MUTATIONS += [
    ('reference-omit-atomic-' + kind, [('tests/fakes/frozen_package.py', [(edge, 'pass')])],
     'tests/test_async_settle_proof_oracle.py::test_guard_atomic_metadata_cache[' + kind + ']')
    for kind, edge in [
        *[(name, 'pending.extend(field.__get__(value) for field in _CODE_MEMBERS)')
          for name in ('co_filename', 'co_name', 'co_qualname', 'co_linetable', 'co_exceptiontable')],
        ('timezone_offset', 'pending.append(_Timezone.utcoffset(value, None))'),
        ('timezone_name', 'pending.append(_Timezone.tzname(value, None))'),
    ]
]
MUTATIONS += [
    ('reference-overridden-filename-comparison', [('tests/fakes/frozen_package.py', [
        ("str.__contains__(filename, '/src/trusted_router/')\n            and not str.startswith(filename, str(ROOT) + '/')",
         "'/src/trusted_router/' in filename and not filename.startswith(str(ROOT) + '/')"),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_live_code_filename_subclass'),
]

MUTATIONS += [
    ('reference-dynamic-provenance-property', [('tests/fakes/frozen_package.py', [
        ("    value_type = type(value)\n    if issubclass(value_type, ModuleType):",
         "    owner = (value.__name__ if isinstance(value, ModuleType) else getattr(value, '__module__', type(value).__module__))\n    return owner if isinstance(owner, str) else getattr(owner, '__name__', '')\n    value_type = type(value)\n    if issubclass(value_type, ModuleType):"),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_provenance_property_cannot_remove_nested_cache'),
    ('reference-dynamic-walk-class-property', [('tests/fakes/frozen_package.py', [
        ('        if value_type is CodeType:', '        if isinstance(value, CodeType):'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_does_not_execute_metadata_properties[__class__]'),
    ('reference-dynamic-cache-class-property', [('tests/fakes/frozen_package.py', [
        ('type(value) is functools._lru_cache_wrapper', 'isinstance(value, functools._lru_cache_wrapper)'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_does_not_execute_metadata_properties[__class__]'),
    ('reference-dynamic-audit-class-property', [('tests/fakes/frozen_package.py', [
        ('value.__code__ if value_type is FunctionType', 'value.__code__ if isinstance(value, FunctionType)'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_does_not_execute_metadata_properties[__class__]'),
    ('reference-dynamic-namespace-comparisons', [('tests/fakes/frozen_package.py', [
        ("str.__eq__(name, namespace) is True or str.startswith(name, namespace + '.')",
         "name == namespace or name.startswith(namespace + '.')"),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_does_not_execute_namespace_comparisons'),
]


MUTATIONS += [
    ('reference-overridden-cache-clear', [('tests/fakes/frozen_package.py', [
        ('functools._lru_cache_wrapper.cache_clear(cache)', 'cache.cache_clear()'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_clears_native_cache_despite_shadowed_method'),
]


MUTATIONS += [
    ('reference-dynamic-metadata-dictionary-get', [('tests/fakes/frozen_package.py', [
        ("_metadata(dict.items(vars(value)), '__module__')", "vars(value).get('__module__', '')"),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_cache_metadata_cannot_remove_cached_live_result[dictionary_get]'),
    ('reference-dynamic-metadata-key-equality', [('tests/fakes/frozen_package.py', [
        ('str.__eq__(key, name) is True', 'key == name'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_cache_metadata_cannot_remove_cached_live_result[key_equality]'),
]


# Round 8: freeze the reviewer's ten seeded live-side projection mutations
# (seed 156710), then require all three native frame ownership paths.
MUTATIONS += [
    ('review-r7-activity_payload-total_cost_microdollars', [('src/trusted_router/storage_operational_analytics.py', [
        ('"total_cost_microdollars": generation.total_cost_microdollars', '"total_cost_microdollars": ((generation.total_cost_microdollars or 0) + 7)'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-r7-enqueue_statement-payload', [('src/trusted_router/storage_gcp_analytics_outbox.py', [
        ('"payload": json_body(sample)', '"payload": (json_body(sample) + " ")'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-r7-activity_payload-key_id', [('src/trusted_router/storage_operational_analytics.py', [
        ('"key_id": analytics_surrogate("api-key", generation.key_hash)', '"key_id": (analytics_surrogate("api-key", generation.key_hash) + "-r7")'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-r7-generation_insert_statement-workspace_id', [('src/trusted_router/storage_gcp_generation_records.py', [
        ('"workspace_id": generation.workspace_id', '"workspace_id": (generation.workspace_id + "-r7")'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-r7-activity_payload-generation_id', [('src/trusted_router/storage_operational_analytics.py', [
        ('"generation_id": generation.id', '"generation_id": (generation.id + "-r7")'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-r7-generation_insert_statement-key_hash', [('src/trusted_router/storage_gcp_generation_records.py', [
        ('"key_hash": generation.key_hash', '"key_hash": (generation.key_hash + "-r7")'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-r7-activity_payload-request_id', [('src/trusted_router/storage_operational_analytics.py', [
        ('"request_id": generation.request_id', '"request_id": (generation.request_id + "-r7")'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-r7-generation_insert_statement-generation_id', [('src/trusted_router/storage_gcp_generation_records.py', [
        ('"generation_id": generation.id', '"generation_id": (generation.id + "-r7")'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-r7-activity_payload-tenant_id', [('src/trusted_router/storage_operational_analytics.py', [
        ('"tenant_id": analytics_surrogate("workspace", generation.workspace_id)', '"tenant_id": (analytics_surrogate("workspace", generation.workspace_id) + "-r7")'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
    ('review-r7-activity_payload-workspace_id', [('src/trusted_router/storage_operational_analytics.py', [
        ('"workspace_id": generation.workspace_id', '"workspace_id": (generation.workspace_id + "-r7")'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_frozen_main_complete_entry[inline-no_header_off-settle-component_half_up]'),
]
MUTATIONS += [
    ('reference-stop-' + name, [('tests/fakes/frozen_package.py', [
        ('        yield value\n', '        yield value\n        if ' + condition + ':\n            continue\n'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_reference_walk_native_frames[' + kind + ']')
    for name, condition, kind in [
        ('frames', 'type(value) is FrameType', 'frame'),
        ('tracebacks', 'type(value) is TracebackType', 'traceback'),
        ('generator-frames', 'type(value) is GeneratorType', 'generator'),
    ]
]


# Round 9: preserve frame GC edges independently of the native proxy supplement,
# and prove proxy keys even when the interpreter omits the redundant GC edge.
MUTATIONS += [
    ('reference-continue-past-frame-gc', [('tests/fakes/frozen_package.py', [
        ('        pending.extend(gc.get_referents(value))',
         '        if value_type is FrameType:\n            continue\n'
         '        pending.extend(gc.get_referents(value))'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_frame_mapping_edges[exec_mapping]'),
    ('reference-proxy-values-only', [('tests/fakes/frozen_package.py', [
        ('pending.extend((key, held))', 'pending.append(held)'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_frame_mapping_edges[proxy_locals_key]'),
]


# Round 10: weak targets must be native, and opaque proxies must fail closed.
MUTATIONS += [
    ('reference-skip-weakref-targets', [('tests/fakes/frozen_package.py', [
        ('target = weakref.ReferenceType.__call__(value)', 'target = None'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_weak_reference_targets[ref]'),
    ('reference-dispatch-overridden-weakref-call', [('tests/fakes/frozen_package.py', [
        ('target = weakref.ReferenceType.__call__(value)', 'target = value()'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_weak_reference_targets[overridden_call]'),
    ('reference-accept-weak-proxy', [('tests/fakes/frozen_package.py', [
        ('if value_type is weakref.ProxyType or value_type is weakref.CallableProxyType:',
         'if False:'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_weak_reference_targets[callable_proxy]'),
]


# Round 11: a prebound partial can start an unprofiled raw worker on CPython.
MUTATIONS += [
    ('skip-prebound-starter-rejection', [('tests/fakes/frozen_package.py', [
        ('raise AssertionError(_PREBOUND_STARTER_REASON)', 'pass'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_rejects_reachable_prebound_starter[partial_func]'),
    ('partial-starter-not-unwrapped', [('tests/fakes/frozen_package.py', [
        ('        yield value\n',
         '        yield value\n        if type(value) is functools.partial:\n            continue\n'),
    ])], 'tests/test_async_settle_proof_oracle.py::test_guard_rejects_reachable_prebound_starter[partial_func]'),
]


def main() -> None:
    results = []
    evidence = Path(os.environ.get('ASYNC_SETTLE_MUTATION_OUTPUT_DIR', '/tmp'))
    evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='pr-c-mutations-') as directory:
        target = Path(directory)
        for folder in ('src', 'tests', 'scripts'):
            shutil.copytree(ROOT/folder, target/folder,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache'))
        shutil.copy2(ROOT/'pyproject.toml', target/'pyproject.toml')
        for name, files, selection in MUTATIONS:
            selected_test = selection if '::' in selection else TEST + selection
            originals = {}
            for relative, edits in files:
                path = target/relative
                text = originals[path] = path.read_text()
                for before, after in edits:
                    assert before in text, (name, before)
                    text = text.replace(before, after)
                path.write_text(text)
            try:
                result = subprocess.run(  # noqa: S603 - fixed executable and test selection
                    [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                     '--disable-warnings', '--tb=short', '-x', selected_test],
                    cwd=target, capture_output=True, text=True, timeout=300,
                    env={**os.environ, 'PYTHONPATH': str(target/'src'), 'PYTHONDONTWRITEBYTECODE': '1'},
                )
                log = evidence / f'pr-c-mutation-{name}.log'
                log.write_text(result.stdout + result.stderr)
                killed = result.returncode == 1 and 'FAILED ' + selected_test.split('[')[0] in result.stdout
                record = dict(mutation=name, killed=killed, exit_code=result.returncode, log=str(log))
                results.append(record)
                print(json.dumps(record), flush=True)
            finally:
                for path, text in originals.items():
                    path.write_text(text)
    (evidence / 'pr-c-mutations.json').write_text(json.dumps(results, indent=2)+'\n')
    assert all(row['killed'] for row in results), results


if __name__ == '__main__':
    main()
