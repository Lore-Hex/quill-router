"""A3 stage policy. Release approvals supply evidence, never applicability."""
from __future__ import annotations

from trusted_router.async_settle_shadow_v2_schema import require

TRANSPORT = tuple('''deployment_inventory authorization_transport_measurements maximum_header_hops
shared_fixtures frozen_pricing_crash_mutations policy_rollback_status rare_cases v2_wire a1_contract a2_contract
p4a_fail_closed a1_time_domain a1_accounting_dormancy a1_report_obligations a1_completion_context
 a1_close_handoff a1_provenance_export_load health partition_wire candidate_backing original_receipt_locator
money_echo demand budget_boot durability serialization bootstrap window live a2_report_commitment
 a2_failure_counts a2_roots_preimages a2_verifier_purity a2_packaging_delegation_cost'''.split())
BUDGET = ('fleet_load_budget', 'publisher_poll_freshness', 'lifecycle_cpu')
MONEY = ('d2_d3_capacity_slo', 'policy_rollback_status', 'live', 'rare_cases', 'attestation', 'exposure_acceptance')
REQUIRED = {
    'transport': TRANSPORT,
    'fleet-budgets-pre': BUDGET,
    'fleet-budgets': BUDGET,
    'shadow-serving': (),
    'pre-flip': MONEY,
    'post-flip': ('policy_rollback_status', 'attestation', 'exposure_acceptance'),
}
PREDECESSORS = {'fleet-budgets': ('fleet-budgets-pre',),
    'shadow-serving': ('transport', 'fleet-budgets'), 'pre-flip': ('shadow-serving',),
    'post-flip': ('pre-flip',)}
ALL_REQUIRED = tuple(sorted({name for names in REQUIRED.values() for name in names}))
REJECTED_TOKENS = ('expired', 'wrong_purpose', 'foreign_identity', 'malformed', 'invalid_signature')


def closure(stage: str) -> frozenset[str]:
    require(stage in REQUIRED, 'evidence_schema_gap')
    return frozenset((stage,)).union(*(closure(prior) for prior in PREDECESSORS.get(stage, ())))


def required_checks(name: str, stage: str) -> tuple[str, ...]:
    # Stage labels distinguish money/hand-off evidence from the earlier shadow
    # proof under the same retained criterion. Exact release checks may add.
    scope = 'money' if stage in ('pre-flip', 'post-flip') else 'shadow'
    checks = [scope + '_' + name]
    if name in ('p4a_fail_closed', 'money_echo'):
        checks.extend('verify_terminal_' + case + '_C' for case in REJECTED_TOKENS)
    return tuple(checks)


def validate_policy(required: tuple[str, ...], stages: dict[str, tuple[str, ...]], criteria: dict[str, tuple[str, ...]]) -> None:
    require(set(ALL_REQUIRED) <= set(required), 'evidence_schema_gap')
    require(all(set(names) <= set(stages.get(stage, ())) for stage, names in REQUIRED.items()), 'evidence_schema_gap')
    require(all(set(required_checks(name, stage)) <= set(criteria.get(stage + '.' + name, ()))
        for stage, names in REQUIRED.items() for name in names), 'evidence_schema_gap')
