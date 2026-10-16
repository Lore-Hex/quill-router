"""Shared stage names and complete check scope for every package front."""
from __future__ import annotations

from trusted_router.async_settle_shadow_v2_schema import require

# Each slash-separated table name is a separate requirement.
TRANSPORT = tuple('deployment_inventory authorization_transport_measurements maximum_header_hops '
    'shared_fixtures v2_wire a1_contract a2_contract frozen_pricing_crash_mutations '
    'a1_time_domain a1_accounting_dormancy health a1_report_obligations a1_completion_context durability '
    'a1_close_handoff a1_provenance_export_load p4a_fail_closed partition_wire candidate_backing '
    'original_receipt_locator money_echo demand budget_boot serialization bootstrap window live rare_cases '
    'a2_report_commitment a2_failure_counts a2_roots_preimages a2_verifier_purity a2_packaging_delegation_cost '
    'policy_rollback_status'.split())
BUDGET = ('fleet_load_budget', 'publisher_poll_freshness', 'lifecycle_cpu')
REQUIRED_STAGES = {'transport': TRANSPORT, 'fleet-budgets-pre': BUDGET, 'fleet-budgets': BUDGET,
                   'pre-flip': ('d2_d3_capacity_slo', 'policy_rollback_status', 'attestation', 'exposure_acceptance', 'live', 'rare_cases'),
                   'post-flip': ('policy_rollback_status', 'attestation', 'exposure_acceptance')}
STAGES = ('transport', 'fleet-budgets-pre', 'fleet-budgets', 'shadow-serving', 'pre-flip', 'post-flip')


# Names identify the positive/fault/mutation groups in sections 11 and 15.6.
# A release supplies originals for this scope; it cannot shorten this table.
CHECK_SCOPE = {
    'deployment_inventory': 'platform_originals process_launches idle_killed_boots config_boot_joins',
    'authorization_transport_measurements': 'actual_hops both_routes full_hash_only regional_timings',
    'maximum_header_hops': 'maximum_headers fragmentation actual_hops',
    'fleet_load_budget': 'native_trial configured_caps simultaneous_sweep publisher_export_shutdown',
    'publisher_poll_freshness': 'native_components delayed_iteration clock_boundaries',
    'lifecycle_cpu': 'baseline_trial authority_node sample_coverage strict_headroom',
    'shared_fixtures': 'router_bytes enclave_bytes combined_pin baseline_a1_a2',
    'v2_wire': 'schema_limits purpose_matrix fallback_matrix shared_bytes',
    'a1_contract': 'frozen_bytes delta_inventory baseline_differential',
    'a2_contract': 'frozen_bytes roots_preimages report_counts',
    'frozen_pricing_crash_mutations': 'full_money_oracle pricing_bytes binding_bytes crash_faults compiled_mutations',
    'a1_time_domain': 'native_ocr nanoseconds validity_availability delayed_lost_reply host_steps future_slots',
    'a1_accounting_dormancy': 'slot_product counter_trace_equal unknown_claim prestart_midnight native_deadline empty_policy_differential',
    'health': 'real_observer missing_stale_future unknown_age slot_races phase_boundaries money_calls_zero',
    'a1_report_obligations': 'issue_horizon fresh_terminal missing_forward_day replay_expiry conflicts issuer_exit',
    'a1_completion_context': 'all_completion_routes missing_original same_writer cross_day sequence_order preserved_fields',
    'durability': 'checkpoint_recovery sample_readback unknown_commit hwm_holes conflicts hard_kill midnight_fold',
    'a1_close_handoff': 'lost_original_reply lost_result_reply kills identity_conflicts one_attempt six_plus_one grace_spans',
    'a1_provenance_export_load': 'row_key_agreement basis_trace_queues upload_restart roots_reruns_cleanup churn_inputs',
    'p4a_fail_closed': 'real_authorize_settle_refund streams malformed_binding ineligible_context replay_missing_original header_absent unexpected_certificate',
    'partition_wire': 'frozen_variants purpose_backing_failures eligible_oversize fallback_changes receipt_hwm_faults',
    'candidate_backing': 'frozen_full_projection digest_only_rejected size_boundaries receipt_hwm_faults',
    'original_receipt_locator': 'frozen_locator_bytes wrong_coordinates missing_page event_digest receipt_hwm_faults',
    'money_echo': 'frozen_money_differential real_streams malformed_diagnostics retry_conflict no_double_booking deployment_marker',
    'demand': 'idle_authorize_excluded unknown_eligibility independent_e fraction_boundaries regional_seed',
    'budget_boot': 'sequential_overlap startup_peaks regional_peaks missing_boundaries n_one_n_two pre_post_cpu',
    'serialization': 'native_six_row_bytes result_row encoding_memory page_limits cross_day_reservations manifest_caps',
    'bootstrap': 'closed_warmup matching_predecessors no_seed_block original_seed_time prerequisite_boundaries',
    'window': 'real_writer_pipeline seven_day_b_zero eight_dates churn idle_horizons retries regional_seeds',
    'live': 'native_faults all_regions exact_releases actual_hops cpu_headroom shutdown',
    'rare_cases': 'boundary_cases compiled_mutations',
    'a2_report_commitment': 'canonical_outer fixed_fields null_zero original_seed_time',
    'a2_failure_counts': 'all_names exact_counts bounded_prefix day_counts permutations every_rule',
    'a2_roots_preimages': 'literal_vectors file_canonical_distinction missing_pages tails counts acyclic_roots',
    'a2_verifier_purity': 'actual_callables source_fixture_identity invocation_counts pure_imports',
    'a2_packaging_delegation_cost': 'combined_pin v1_differential retained_deltas worst_pages churn_reruns_cleanup',
    'policy_rollback_status': 'shadow_optout close_retention retained_losses',
    'd2_d3_capacity_slo': 'regional_drain load chosen_settings d2 d3',
    'attestation': 'released_image revision_configuration',
    'exposure_acceptance': 'selected_pilot reviewed_exposure',
}
MONEY_SCOPE = {
    'policy_rollback_status': 'truthful_status money_rollback handoff repair retained_losses',
    'partition_wire': 'live_mixed_excluded all_reasons immutable_witnesses',
    'candidate_backing': 'live_cross_boot original_projection faults',
    'original_receipt_locator': 'live_lookup issuer_kill concurrent_pages',
    'money_echo': 'live_certificate_emission old_new_combinations',
    'window': 'mixed_eligible_excluded',
}


def check_scope(name: str, stage: str) -> tuple[str, ...]:
    require(stage in ('transport', 'fleet-budgets-pre', 'fleet-budgets', 'shadow-serving', 'pre-flip', 'post-flip'),
            'artifact_gap')
    require(name in CHECK_SCOPE, 'artifact_gap')
    text = CHECK_SCOPE[name]
    if stage in ('pre-flip', 'post-flip') and name in MONEY_SCOPE:
        text = MONEY_SCOPE[name] if name == 'policy_rollback_status' else text + ' ' + MONEY_SCOPE[name]
    if stage == 'pre-flip' and name in ('live', 'rare_cases'):
        text += ' drain_handover'
    return tuple(sorted(text.split()))


STAGE_CHECKS = {stage: {name: check_scope(name, stage) for name in REQUIRED_STAGES.get(stage, ())}
                for stage in STAGES}
