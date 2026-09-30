# Workspace authorize read-collapse proof (retired)

This document recorded the mutation-testing proof for the regional record
transaction's combined `tr_credit_balance` read (shard completeness, pause
detection and lease trust state in one query). That transaction, the standalone
workspace lease-eligibility reads (`trust_eligibility.read_lease_trust` and
`read_workspace_lease_trust`), and the tests the proof named
(`tests/test_authorize_workspace_reads.py`, `test_trust_gate_cost.py`,
`test_trust_eligibility_pr2.py`, `test_regional_quota_spanner.py`) were removed
with the regional-quota and spend-lease pilots in 2026-09.

What survives: the typed authorize transaction still reads the workspace's
billing-pause state on its selected shard inside its own transaction
(`trust_eligibility.billing_paused_tx`, armed by
`TR_SPEND_LEASE_TRUST_ELIGIBILITY_ENABLED`, whose name predates the removal).

The full proof, its mutation table and its validation record are in git
history: `git log -- docs/authorize-workspace-read-proof.md`.
