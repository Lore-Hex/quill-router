# Stage C admission and P1/P2 evidence

Admission remains disabled: `TR_SPEND_LEASE_ADMISSION_ACCEPT=false` and
`TR_SPEND_LEASE_ADMISSION_WORKSPACE_IDS=` in rollout configuration. The latter
is a comma-separated independent cohort, default empty (none), required at
both mint and reserve. Stage B's pilot issuance gate remains in force.

Boot-auth and receipt verification share one resolved signed-policy digest set
per authorize request, unioned with the explicit emergency override. Unavailable
or expired policy evidence supplies no accepted digests. Admission also requires
an invocation nonce and streaming chat/responses with Stage D eligibility and
frozen billing prices. The additional closed wire reasons are `not_streaming`
(non-streaming local admission is outside this rollout), `cap_not_enforceable`,
and `invocation_nonce_required`. Existing rejection reasons retain their meaning.
Committed reserve replays return their stored nonce and Stage D pricing/cap.

Apply `clickhouse/016_spend_lease_parity.sql` to the replicated cluster before
rolling out the new writers; `017_spend_lease_parity_single_node.sql` is its
single-node counterpart. Then apply `018_spend_lease_identity.sql` (replicated)
or `019_spend_lease_identity_single_node.sql` (single node) before either new
writer. These add `echo_lease_id`; existing `lease_id` now records the router
identity. All historical `lease_id` values were NULL, so no historical identity
is reinterpreted or backfilled. The operational analytics deployment workflow includes
the replicated migration before updating ingestion. Old payloads remain readable:
new columns are nullable and historical absence is not evidence of success.
No historical events are backfilled as successful parity or binding.

Stable columns in `tr.spend_lease_shadow`:

| Column | Meaning |
| --- | --- |
| `lease_id` | Router lease minted/retained by Stage A, or returned on the committed authorization by binding. Provisional binding candidates are not recorded. NULL means no router lease observed, never proof that the enclave had none. |
| `echo_lease_id` | Enclave claim, independently nullable. Compare with `lease_id` to detect disagreement; never used as a fallback for the router identity. |
| `binding_outcome` | Existing router binding outcome (e.g. `mint_bound`, `reuse_bound`, `ordinary`, `ledger_unavailable`), now retained by both sinks. `mint_bound`/`reuse_bound` report committed bindings; `replay` identifies a stored authorization. Stage A issuance without binding can have a router ID and NULL outcome. NULL means no observed binding outcome. |
| `enclave_estimate_micro` | Enclave echo's estimate, unchanged. |
| `server_estimate_micro` | Current routing/pricing estimate, unchanged for ordinary authorize; excludes replacement with the admission estimate. |
| `frozen_server_estimate_micro` | Router recomputation over the echoed lease's immutable candidate catalog. NULL if no comparable snapshot/request is available. |
| `catalog_version` | Enclave-echoed catalog identity, unchanged. |
| `comparison_catalog_version` | Identity of the retained snapshot the router examined; must equal `catalog_version` for comparison. It hashes all frozen candidate fields. |
| `divergence` | `estimate_low`, `estimate_high`, or `estimate_equal` compares enclave against **frozen** router estimate; `not_comparable` means missing or mismatched evidence; `echo_invalid` means absent echo or unverified boot. Old `none` rows do not establish equality. |
| `applicability_drift` | `none` means current and frozen catalogs match; `catalog_changed` reports changed candidate order/membership, prices or applicability/dispatch fields; `request_inapplicable` means the request has no applicable frozen candidate; `echo_catalog_mismatch`, `snapshot_unavailable`, and `observation_failed` identify missing/unusable evidence. NULL means observation was not attempted. Drift never rejects authorization. |
| `server_verdict` | Actual authorization verdict, independent of estimator equality. Combine with `would_admit` to find admission disagreement. |

The observation reads the retained grant by the resolved key ID and verified
boot ID before mint/reuse can replace it. A different retained lease is missing
evidence, never a comparison against a new grant. Stage A/B tokens work with
admission off, including tokens that retain their catalog only inside the signed
token. Observation errors are best-effort telemetry failures, never money-path
failures. Use nonzero representative comparable coverage and zero low/high rows
for P2; report drift and missing coverage separately. P1 additionally needs
continuous binding configuration and reconciler coverage, not just event counts.

One-row identity and binding evidence (supply a fixed interval and workspace):

```sql
SELECT event_id, lease_id, echo_lease_id, binding_outcome, server_verdict,
       ifNull(lease_id != echo_lease_id, false) AS identity_disagreement
FROM tr.spend_lease_shadow FINAL
WHERE workspace_id = {workspace:String}
  AND created_at >= parseDateTime64BestEffort({from:String})
  AND created_at < parseDateTime64BestEffort({to:String});
```

`identity_disagreement` compares two present IDs; a missing ID remains visible
in its own column and does not establish agreement.

Example P2 evidence query (supply a fixed seven-day UTC interval and workspace):

```sql
SELECT comparison_catalog_version, applicability_drift, divergence,
       count() AS events,
       countIf(binding_outcome IN ('mint_bound', 'reuse_bound')) AS bound_events,
       countIf(boot_verified = 1
           AND catalog_version = comparison_catalog_version
           AND isNotNull(frozen_server_estimate_micro)
           AND isNotNull(enclave_estimate_micro)) AS comparable_events
FROM tr.spend_lease_shadow FINAL
WHERE workspace_id = {workspace:String}
  AND created_at >= parseDateTime64BestEffort({from:String})
  AND created_at < parseDateTime64BestEffort({to:String})
GROUP BY comparison_catalog_version, applicability_drift, divergence;
```

The production-shaped wire fixtures live in `tests/fixtures/stage_c`. Regenerate
with `uv run python -m scripts.fixtures.regenerate_stage_c`. The public deterministic
Ed25519 seed is unchanged. Request lookup hashes differ from resolved key IDs;
lease and receipt claims use the latter. Requests include stream, invocation
nonce, supported provider preferences/aliases, tags and attribution. The accepted
response includes Stage D prices/cap. `wire_manifest.json` hashes every fixture
and `wire_manifest.ed25519` signs that manifest with the same public test seed;
tests also verify each JWS, boot-auth signature, and normalized routing hash.
The enclave PR must reproduce these exact wire bytes. The seed is test-only.
