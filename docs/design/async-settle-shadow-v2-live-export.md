# Shadow v2 live export — round 1 source map

Scope: offline round 1 at e5bf9c22. No activation, production reads, writer changes,
or evaluator changes. Read alongside §7–9 of async-settle-shadow-v2.md and the
171-field P5 provenance audit. **A schema is not a producer.** This inventory
separates (a) deployed-code writers, (b) reviewed operator inputs, and (c) missing
collectors/packagers. “Match” below means the actual serialization boundary uses
the loader's identical schema, not merely matching JSON field names.

## Transport boundary

`gate_loader.load` lines 25–119 / `gate_loading.transport_children` lines 223–281
accept a UTF-8 JSON object, at most 10000×262144 bytes. Exact allowed members:
`v` (optional, not checked), `policy_version:"shadow-v2"`,
`contract_revision:"a1"`, `rows`, `attachments`, `proof`, `ledgers`,
`transport`, `fleet`, `pre`, `serving`, `completed_at_us`, `days`, `preimages`,
`window`. No diagnostic extension member is accepted.

* `rows` and `attachments`: arrays (≤10000 each) of exact
  `{kind:string,id:string,raw:string}`. `raw.encode("utf-8")` is the source file;
  preserve whitespace, LF and conflicts. Their combined count with ledgers,
  preimages and proof is ≤10000. Row identity is `(kind,id)`.
* `proof`: nullable original A1 PROOF_MANIFEST string; absent/undecodable proof
  makes the renderer return UnavailableGateOutput rather than manufacture hashes.
* `ledgers`: ordered array ≤4096 original A1 PREREQUISITE_LEDGER strings.
* `transport|fleet|pre|serving`: nullable original A1 GATE_OUTPUT strings. Their
  complete original reports, DETAIL pages, INPUT_LEAFs and dependency graph must
  also appear under preimages. `transport` stage needs no predecessor;
  fleet requires pre; serving/pre-flip require transport, pre and fleet.
  Serving is never a seed clock authority (`gate_prior.py:34–38,41–233`).
* `days`: sorted unique valid YYYY-MM-DD strings, 1–32; `completed_at_us`:
  nonnegative int64; `window`: optional `{start_us:u,end_us:u}`, start < end.
  These select scope, never assert coverage or a trusted prior completion.
* `preimages`: ≤10000 exact `{name:string,raw:string}` entries. Names follow
  FILE_REF relative ASCII path rules. Each original file ≤262144 bytes. Repeated
  identical files coalesce; distinct bytes at one path fail closed. Manifest
  preimage names may be prefixed by `YYYY-MM-DD/`; proof names may not.

## Real row and object producers (a)

`async_settle_shadow_journal.Row.build` lines 65–83 is the common writer
serialization boundary; `gate_bundle.decode_bundle` lines 94–129 calls the same
A1 validate_row on original bytes. No exporter projection is needed for the
following stored schemas. Every field, including inherited retained fields,
is listed in the schema inventory below. Their scalar/nested checks are those
of the cited frozen schema, not exporter-owned reinterpretations.

| Transport identity | Actual writer and source | Byte agreement / limitations |
|---|---|---|
| rows sample_v2, authorization-day/id | services/async_settle_shadow_cohort_runtime.py:550–609 Runtime.finish; journal SAMPLE_KIND; ObservationStore.terminal | SAMPLE retained fields plus v=2, policy_version, proof_kind, original_terminal_receipt, health_source, health_slot, prediction_basis match; raw JWS deliberately absent |
| rows certificate_v2, authorization-day/id | No first-window P4a certificate producer (P4b deferred) | schema exists, no class-a producer in the compared-only path |
| rows receipts_v2, day/boot/first-seq | journal Journal.freeze, finalize, complete_event; ObservationStore.flush | PAGE and every EVENT serialized by frozen P1; padding, predecessor, HWM and compact identity match |
| rows counter_v2, day/boot | services/async_settle_shadow_health.py:555–573,708–718 Service.ensure_day/counter_row | A1 COUNTER exactly; inherited fixed arrays and publisher operations validated before storage |
| rows closure_v2, day/boot | same:720–730 Service.closure_row | A1 CLOSURE exactly, authorization_obligations=receipt_chain; no acknowledgement invented |
| rows close_result_v2, day/boot | same:1070–1097 Service._flush; ObservationStore.close_result | A1 CLOSE_RESULT exactly; created only after original final reply, separate immutable insert |
| rows control_v2, day/cap-v2 | journal.reserve_cap; ObservationStore.allocate | A1 CAP exactly; this is NOT a day MANIFEST |
| rows control_v2, retention-v2 | ObservationStore retention/fence APIs | Not in a day range; policy watermark, not daily coverage |
| rows control_v2, day/manifest-v2 | No service emits a complete MANIFEST | class c packaging source, not an existing writer |
| rows health_v2, local/fleet | storage_gcp_shadow_observation.py:192 publish; health_state.summarize | Same A1 HEALTH; latest mutable value, outside day range, historical basis is in attachments |
| rows health_claim_v2, local/publish | ObservationStore.claim; health_state.claim_allowed | Same A1 HEALTH_CLAIM; latest mutable claim, outside day range |
| attachments health_basis, health-basis-batches/day/boot/20-digit-sequence | async_settle_shadow_attachments.py:39–93 Lane.append; Port.capture_prediction and Service.upload | HEALTH_BASIS_BATCH exact; bases hash sorted/deduplicated; chain preserved; immutable GCS generation not exposed by writer get |
| attachments publisher_trace, publisher-traces/day/boot/first-slot | health service publish_cycle/publisher_reply/missed_slot; Lane.append | PUBLISHER_TRACE_PAGE exact; slots integer, first-slot in key unpadded; includes failures/missed outcomes |
| attachments handoff_accounting | No durable writer; A1 handoff_accounting pure derivation exists | class c report packaging; conservative bounds, never a manufactured reply |
| attachments authorization_obligation | No durable writer; A1 reconciliation pure functions exist | class c report joins through original receipt horizon |
| attachments coverage | No durable writer; A1 COVERAGE_PAGE schema/root exists | class c exporter packaging over INDEPENDENT expected set, not found-object listing |
| attachments shutdown_entry / shutdown_measurement | No durable writer | class c measured shutdown collector; loader reconstructs aliases and requires exact supplied aliases |

## Operator approval and independent collection (b/c)

All A2 objects below have exact v=2, policy_version=shadow-v2,
contract_revision=a2, ≤262144 bytes unless the schema specifies a smaller bound.
FILE_REF is `{path,sha256,bytes}` over exact bytes; APPROVAL is
`{subject_sha256,reviewer,reviewed_at_us,decision:"approved",record:FILE_REF}`.
The REVIEW_RECORD must reproduce those approval fields. The trusted release plan
must independently pin its record. A bundle's own assertion is not signoff.
Design §9 steps 2–5 assigns actual measurement approvals to release/operator
review (P6); it does not name an individual signer or introduce a new signature
protocol. Do not invent one.

| Member/path | Real source / responsibility | Exact decoding and dependencies |
|---|---|---|
| proof | c: release evidence packager | A1 PROOF_MANIFEST; roots over all named source bytes, rosters, config and dependency files; no current live producer |
| ledgers | c: per-boot readiness collector, b: reviewed criterion artifacts | A1 PREREQUISITE_LEDGER; six ARTIFACTs transport/size/regional_load/boot_transport/observer_freshness/marker_keys; each `{sha256,completed_at_us}` resolves to PROOF_RESULT and exactly one PREREQUISITE_CONTEXT matching role/boot/revision/region/boot_at_us/criterion/completion (`gate_provenance.py:19`) |
| day/manifest.independent_inventory_sha256 | c: independent serving/deployment collector, b: approved deployment | INDEPENDENT_INVENTORY; serving is retained v1 roster array, deployment object has serving array; equal coverage and pins; include killed/retired boots, not discovered evidence writers |
| proof.shutdown_inventory_sha256 | c: measured shutdown collector, b: reviewed measurement source | SHUTDOWN_INVENTORY; writers DETAIL, measurement FILE_REF list, per-writer SHUTDOWN_MEASUREMENT and independently pinned sources/measurement/* parsed as SHUTDOWN_SOURCE; original_close and close_result exact row references |
| proof.cohort_seed_policy_sha256 | b: reviewed single-workspace P6 config, c: envelope packager | COHORT_SEED_POLICY configuration must exactly be `{workspace_count:1,eligible_only:true,supported_candidates:1,fallbacks:false,routes:["chat.completions","responses"]}`; workspace fingerprints count 1, each_traffic_region |
| proof.pre_budget_sha256 / proof.post_budget_sha256 | c: isolated measurement collector; b: P6 approved budgets before opt-in, actual post-opt-in approval | BUDGET_ENVELOPE, matching pre hash, retained payload/output/dependencies, V2_LOAD, limits, segments/operations DETAIL; no inferred CPU/timing/headroom values |
| loading/budget-measurements.json | c: RPC/metadata/export/cleanup/shutdown collector | BUDGET_MEASUREMENTS exact ALLOWANCES rational, TOTALS integer, component_us; must be post retained dependency; loader derives rates from original counters/traces |
| proof.external_proofs_sha256 | b: exact-revision reviewed CI/money oracle/fault/rollout artifacts, c: packaging | EXTERNAL_PROOFS → requirements/PROOF_RESULT/checks/source FILE_REF DETAIL; criteria exactly approved in release plan; all EXTERNAL_REQUIRED names expected by gate_views.external_view |
| proof.resolution_dag_sha256 | c: retained history collector; b: reviewed mismatch fixes/rollback | RESOLUTION_DAG → original retained collect_rollbacks payload/dependencies/output capture, reset-history DETAIL/support FILE_REFs; original rows/proof/manifests/serving spans retained |
| loading/retained-output/<payload-sha>.json | c: retained callable capture | RETAINED_OUTPUT `{entry_point,payload_sha256,output:FILE_REF}`. Re-execute original tool and require exact output bytes and counted calls. Budget uses explicit output instead |
| loading/signatures.json | c: secure transient capture at ingress/authorize response; b: approved public descriptor files | array ≤10000 exact `{object_id,verifier,token,now,expected,attempt,server_attempt,claims}`; verifier binding/certificate; token≤32768; maps exact sample/certificate-event population. Writers deliberately do not retain tokens (design §7). Purity/privacy ambiguity requires review |
| manifest.config_sha256 / manifest.fixture_sha256 | b: reviewed original configuration/fixture file | exact UTF-8 original file F, indexed by these preimage names, optionally day-prefixed |
| proof evaluator/binding/wire fixture hashes | b: immutable reviewed checkout release | fixed billing_v1.json, shadow_v1.json, shadow_v2/manifest.json via trusted tool; sole exemption from exported FILE_REF size/inventory; do not export regenerated fixtures |
| loading/release-plan.json + independent CLI digest | b: release review trust root | exact object `{artifacts:{path:sha},criteria:{criterion:[check]},verifiers:{entry:[source-sha,fixture-sha,keyset-sha-or-null]},required:[name],required_stages?:{stage:[name]}}`; original plan bytes also in preimages; trusted digest outside transport |
| verifier source/fixture/keysets and tool dependencies | b: independently approved release files | VerifierApproval imported callable identity, source/fixture/keyset original bytes and pins; real binding/certificate/retained/roster adapters; never bundle-provided code |
| prior transport/fleet/pre/serving | c: earlier real evaluator output and full input inventory | gate_prior resolves exact report, all detail pages and leaves, re-loads/re-evaluates same stage at original completion, exact rendered byte equality |

The retained serving roster items are `{instance,role,revision,region,intervals,
pins}` (retained parser permits ancillary fields). Each interval is
`{day,started_at_us,flushed_at_us}`. Router pins are
TR_ASYNC_SETTLE_ENABLED=false, TR_ASYNC_SETTLE_PROTECTION=false,
TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED=false; enclave pins are
TR_ASYNC_SETTLE_NEGOTIATE=off and TR_ASYNC_SETTLE_SHADOW=on.
`gate_payloads.inventory:275–320` checks every lifetime and actual region.

## Exact schema field inventory

The lists below include inherited fields; nested validation and byte/type bounds
are owned by the named frozen schemas in src/trusted_router. This provides an
explicit field-by-field join: all class-a records listed above use the very same
schema on write and load, including every listed field. No renaming, padding,
version translation or reserialization is authorized. Semantic integration
failures are listed separately, because schema agreement does not establish G1–G11.

* `COUNTER`: `drops, started_at_us, counter_overflow, authorize_shadow_hist, sequence, flushed_at_us, dimension_overflow, instance, booking_unknown, admission_observer, comparison_attempts, first_evidence_at_us, terminal_counts, last_mismatch_at_us, booking_pending, evidence_write_hist, counts, rejections, duplicate_samples, comparison_dropped, samples_inserted, exclusions, first_gap_at_us, region, v, conflicting_samples, closed, router_revision, policy_version, day, boot, authorize_attempts, authorize_fresh, authorize_replay, settle_attempts, refund_attempts, A, B, C, proof_compared, proof_certificate, proof_observed, requested_eligible, requested_excluded, requested_unknown, checkpoint_attempts, checkpoint_failures, checkpoint_recoveries, unknown_commits, unreconciled_commits, irrecoverable_losses, eligible_demand, event_seq, durable_event_hwm, checkpoint_seq, receipt_count, receipt_chain_sha256, loss_count, reset_count, diagnostics, buckets, publisher, contract_revision`.
* `CLOSURE`: `v, policy_version, day, boot, router_revision, region, reason, interval_end_us, final_event_seq, final_checkpoint_seq, durable_event_hwm, receipt_chain_sha256, counter_sha256, retained_work, pending_comparisons, unresolved_commits, loss_count, observer_fold_through_us, previous_day_closure_sha256, contract_revision, authorization_obligations`.
* `CLOSE_RESULT`: `v, policy_version, contract_revision, day, boot, router_revision, closure_sha256, final_checkpoint_seq, attempt_started_at_us, observed_at_us, elapsed_us, physical_commit_ack_at_us, flush_remaining_us, outcome, shutdown_remaining_us, handoff_attempt_limit`.
* `CAP`: `v, policy_version, day, terminal_reserved, event_reserved, contract_revision`.
* `RETENTION`: `v, policy_version, retired_through, kinds, contract_revision`.
* `MANIFEST`: `v, policy_version, day, config_sha256, fixture_sha256, router_writers, enclave_writers, revisions, counter_root, closure_root, receipts_root, prerequisite_ledger_root, independent_inventory_sha256, first_evidence_at_us, proof_manifest_sha256, disabled_flag_intervals, complete, gaps, contract_revision, publisher_trace_root, health_basis_root, close_result_root`.
* `PROOF_MANIFEST`: `v, policy_version, source_shas, evaluator_fixture_sha256, binding_fixture_sha256, wire_fixture_sha256, ledger_root, shutdown_inventory_sha256, cohort_seed_policy_sha256, pre_budget_sha256, post_budget_sha256, publisher_slot_trace_sha256, external_proofs_sha256, resolution_dag_sha256, rosters_by_day, instance_regions_by_day, attachments, explicit_keys, complete, contract_revision, publisher_trace_root, close_result_root, authorization_obligation_root, health_basis_root, handoff_accounting_root`.
* `PREREQUISITE_LEDGER`: `v, policy_version, role, boot, revision, region, policy_sha256, fixture_sha256, config_sha256, boot_at_us, collection_interval, traffic_ready_at_us, transport, size, regional_load, boot_transport, observer_freshness, marker_keys, previous_ledger_sha256, contract_revision`.
* `GATE_OUTPUT`: `v, policy_version, gate, verdict, completed_at_us, proof_manifest_sha256, prerequisite_ledger_root, gaps, clock_start_us, elapsed_seconds, coverage_sha256, contract_revision`.
* `SAMPLE`: `endpoint_id, authorize_at_us, booked_micro, go_micro, raw_usage, provenance, booking_observed_at_us, eligibility, rebuilt_snapshot_hash, booking, model_id, legacy_frozen_micro, authorization_day, go_usage, authorization_id, admission, python_usage, reason_codes, workspace_fingerprint, adapter, rebuilt_minus_frozen, observed_at_us, rebuilt_micro, booked_minus_rebuilt, legacy_usage, route_type, booked_minus_frozen, deployment, streamed, frozen_micro, snapshot_hash, v, payload_hash, python_minus_go, classification, python_micro, timing, policy_version, proof_kind, original_terminal_receipt, health_source, health_slot, prediction_basis`.
* `CERTIFICATE_RECORD`: `v, policy_version, claims, certificate_sha256, issuer_keyring_sha256, verified_at_us, verification_result, authorization_day`.
* `PAGE`: `v, policy_version, boot, day, first_seq, last_seq, previous_page_sha256, events`.
* `EVENT`: `union: AUTHORIZE_EVENT / TERMINAL_EVENT / COMPLETION_EVENT`.
* `shutdown_entry`: `day, boot, termination_applies, reason, interval_end_us, grace_us, original_close, close_result, measurement`.
* `shutdown_measurement`: `v, policy_version, contract_revision, day, boot, grace_us, final_pass_us, close_us, result_us, export_wait_us, preparation_us, outcome, source`.
* `health_basis`: `v, policy_version, contract_revision, boot, day, sequence, previous_sha256, bases`.
* `publisher_trace`: `v, policy_version, contract_revision, day, boot, first_slot, last_slot, previous_sha256, entries`.
* `handoff_accounting`: `v, policy_version, contract_revision, day, boot, expected_handoffs, attempts_lower, attempts_upper, landed, reply_status, failures_lower, failures_upper, unknown_reply_upper, result_sha256`.
* `authorization_obligation`: `v, policy_version, contract_revision, day, boot, page_seq, previous_sha256, entries`.
* `coverage`: `v, policy_version, contract_revision, day, stream, page_seq, previous_sha256, entries`.
* `manifest.independent_inventory_sha256`: `v, policy_version, contract_revision, day, serving, deployment, approval, complete`.
* `proof.shutdown_inventory_sha256`: `v, policy_version, contract_revision, writers, measurements, approval, complete`.
* `proof.cohort_seed_policy_sha256`: `v, policy_version, contract_revision, workspace_fingerprints, regional_seed_policy, required_regions, primary_region, maximum_publishers, configuration, approval`.
* `proof.pre_budget_sha256`: `v, policy_version, contract_revision, stage, retained, output, pre_envelope_sha256, approval, v2_load`.
* `proof.post_budget_sha256`: `v, policy_version, contract_revision, stage, retained, output, pre_envelope_sha256, approval, v2_load`.
* `proof.external_proofs_sha256`: `v, policy_version, contract_revision, requirements, pilot`.
* `proof.resolution_dag_sha256`: `v, policy_version, contract_revision, retained, history, approvals`.
* `FILE_REF`: `path, sha256, bytes`.
* `APPROVAL`: `subject_sha256, reviewer, reviewed_at_us, decision, record`.
* `REVIEW_RECORD`: `v, policy_version, contract_revision, subject_sha256, reviewer, reviewed_at_us, decision`.
* `SHUTDOWN_ENTRY`: `day, boot, termination_applies, reason, interval_end_us, grace_us, original_close, close_result, measurement`.
* `SHUTDOWN_MEASUREMENT`: `v, policy_version, contract_revision, day, boot, grace_us, final_pass_us, close_us, result_us, export_wait_us, preparation_us, outcome, source`.
* `DELEGATED_PAYLOAD`: `v, policy_version, contract_revision, entry_point, payload, dependencies, source`.
* `PROOF_RESULT`: `v, policy_version, contract_revision, criterion, router_revision, enclave_revision, completed_at_us, producer, checks, sources`.
* `LOAD_SEGMENT`: `day, boot, region, start_us, end_us, role, contender, workspace_count, peaks`.
* `INPUT_LEAF`: `v, policy_version, contract_revision, keys, complete`.
* `BUDGET_MEASUREMENTS`: `publisher_reads, publisher_writes, publisher_unresolved_rows_per_second, publisher_control_rows_per_second, publisher_transactions_per_second, publisher_rpc_per_second, metadata_bytes_per_second, surge, export_operations, export_bytes, close_transactions, close_rows, export_passes, export_root_writes, export_write_bytes, cleanup_spanner_keys, cleanup_sink_keys, cleanup_seconds, component_us`.
* `SHUTDOWN_SOURCE`: `v, policy_version, contract_revision, day, boot, grace_us, final_pass_us, close_us, result_us, export_wait_us, preparation_us, outcome`.
* `PREREQUISITE_CONTEXT`: `role, boot, revision, region, boot_at_us, criterion, completed_at_us`.
* `RETAINED_OUTPUT`: `entry_point, payload_sha256, output`.

## 171-field typed-input crosswalk

These are derived views, not alternate serialized transport fields. Each row
keeps the exact P5 consumption contract and assigns its upstream owner above.

| Typed field | Production source class | Original source and validation |
|---|---|---|
| `VerifierApproval.entry_point` | b: reviewed tool/release | Trusted checkout source, frozen fixture, ordered public key descriptor bytes and independent release pins; gate_verifiers.check_ports; A2 VERIFIER_USE; FILE_REF; gate_release.release_checks |
| `VerifierApproval.function` | b: reviewed tool/release | Trusted checkout source, frozen fixture, ordered public key descriptor bytes and independent release pins; gate_verifiers.check_ports; A2 VERIFIER_USE; FILE_REF; gate_release.release_checks |
| `VerifierApproval.source` | b: reviewed tool/release | Trusted checkout source, frozen fixture, ordered public key descriptor bytes and independent release pins; gate_verifiers.check_ports; A2 VERIFIER_USE; FILE_REF; gate_release.release_checks |
| `VerifierApproval.fixture` | b: reviewed tool/release | Trusted checkout source, frozen fixture, ordered public key descriptor bytes and independent release pins; gate_verifiers.check_ports; A2 VERIFIER_USE; FILE_REF; gate_release.release_checks |
| `VerifierApproval.keyset` | b: reviewed tool/release | Trusted checkout source, frozen fixture, ordered public key descriptor bytes and independent release pins; gate_verifiers.check_ports; A2 VERIFIER_USE; FILE_REF; gate_release.release_checks |
| `VerifierApproval.keys` | b: reviewed tool/release | Trusted checkout source, frozen fixture, ordered public key descriptor bytes and independent release pins; gate_verifiers.check_ports; A2 VERIFIER_USE; FILE_REF; gate_release.release_checks |
| `VerifierApproval.source_pin` | b: reviewed tool/release | Trusted checkout source, frozen fixture, ordered public key descriptor bytes and independent release pins; gate_verifiers.check_ports; A2 VERIFIER_USE; FILE_REF; gate_release.release_checks |
| `VerifierApproval.fixture_pin` | b: reviewed tool/release | Trusted checkout source, frozen fixture, ordered public key descriptor bytes and independent release pins; gate_verifiers.check_ports; A2 VERIFIER_USE; FILE_REF; gate_release.release_checks |
| `Ports.verify_certificate` | b: reviewed tool/release | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence; gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.verify_original_receipt` | b: reviewed tool/release | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence; gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.verify_binding` | b: reviewed tool/release | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence; gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.comparator_positive` | b: reviewed tool/release | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence; gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.positive_sample` | b: reviewed tool/release | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence; gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.known_exclusion` | b: reviewed tool/release | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence; gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.fleet_budgets_pre` | b: reviewed tool/release | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence; gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.collect_rollbacks` | b: reviewed tool/release | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence; gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.report` | b: reviewed tool/release | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence; gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.post_flip` | b: reviewed tool/release | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence; gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.approvals` | b: reviewed tool/release | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence; gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.approved_files` | b: reviewed tool/release | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence; gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.read_release_file` | b: reviewed tool/release | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence; gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.roster_coverage` | b: reviewed tool/release | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence; gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Key.kind` | a: original store bytes + exporter metadata | Original transport row kind/id; validated together with original body bytes; a1.validate_row; ATTACHMENTS schemas; audit_expected |
| `Key.id` | a: original store bytes + exporter metadata | Original transport row kind/id; validated together with original body bytes; a1.validate_row; ATTACHMENTS schemas; audit_expected |
| `Row.key` | a: original store bytes + exporter metadata | Original UTF-8 transport row or attachment bytes, unchanged; a1.validate_row; ATTACHMENTS schemas; A2 validate_preimages |
| `Row.raw` | a: original store bytes + exporter metadata | Original UTF-8 transport row or attachment bytes, unchanged; a1.validate_row; ATTACHMENTS schemas; A2 validate_preimages |
| `Boot.boot` | c: independent collector/packager | INDEPENDENT_INVENTORY serving/deployment FILE_REF bytes; contender from LOAD_SEGMENT, termination from SHUTDOWN_ENTRY; INDEPENDENT_INVENTORY; PayloadGraph.inventory; roster_coverage; LOAD_SEGMENT; SHUTDOWN_ENTRY; validate_preimages |
| `Boot.role` | c: independent collector/packager | INDEPENDENT_INVENTORY serving/deployment FILE_REF bytes; contender from LOAD_SEGMENT, termination from SHUTDOWN_ENTRY; INDEPENDENT_INVENTORY; PayloadGraph.inventory; roster_coverage; LOAD_SEGMENT; SHUTDOWN_ENTRY; validate_preimages |
| `Boot.revision` | c: independent collector/packager | INDEPENDENT_INVENTORY serving/deployment FILE_REF bytes; contender from LOAD_SEGMENT, termination from SHUTDOWN_ENTRY; INDEPENDENT_INVENTORY; PayloadGraph.inventory; roster_coverage; LOAD_SEGMENT; SHUTDOWN_ENTRY; validate_preimages |
| `Boot.region` | c: independent collector/packager | INDEPENDENT_INVENTORY serving/deployment FILE_REF bytes; contender from LOAD_SEGMENT, termination from SHUTDOWN_ENTRY; INDEPENDENT_INVENTORY; PayloadGraph.inventory; roster_coverage; LOAD_SEGMENT; SHUTDOWN_ENTRY; validate_preimages |
| `Boot.start_us` | c: independent collector/packager | INDEPENDENT_INVENTORY serving/deployment FILE_REF bytes; contender from LOAD_SEGMENT, termination from SHUTDOWN_ENTRY; INDEPENDENT_INVENTORY; PayloadGraph.inventory; roster_coverage; LOAD_SEGMENT; SHUTDOWN_ENTRY; validate_preimages |
| `Boot.end_us` | c: independent collector/packager | INDEPENDENT_INVENTORY serving/deployment FILE_REF bytes; contender from LOAD_SEGMENT, termination from SHUTDOWN_ENTRY; INDEPENDENT_INVENTORY; PayloadGraph.inventory; roster_coverage; LOAD_SEGMENT; SHUTDOWN_ENTRY; validate_preimages |
| `Boot.contender` | c: independent collector/packager | INDEPENDENT_INVENTORY serving/deployment FILE_REF bytes; contender from LOAD_SEGMENT, termination from SHUTDOWN_ENTRY; INDEPENDENT_INVENTORY; PayloadGraph.inventory; roster_coverage; LOAD_SEGMENT; SHUTDOWN_ENTRY; validate_preimages |
| `Boot.termination_applies` | c: independent collector/packager | INDEPENDENT_INVENTORY serving/deployment FILE_REF bytes; contender from LOAD_SEGMENT, termination from SHUTDOWN_ENTRY; INDEPENDENT_INVENTORY; PayloadGraph.inventory; roster_coverage; LOAD_SEGMENT; SHUTDOWN_ENTRY; validate_preimages |
| `Inventory.boots` | c: independent collector/packager | All approved dated independent serving/deployment files, merged contiguous lifetimes; PayloadGraph.inventory; gate_roster; validate_preimages |
| `Cohort.workspace_count` | c: independent collector/packager | proof.cohort_seed_policy_sha256 configuration FILE_REF and policy original bytes; COHORT_SEED_POLICY; PayloadGraph.cohort; validate_preimages; release_checks |
| `Cohort.regional_seeds` | c: independent collector/packager | proof.cohort_seed_policy_sha256 configuration FILE_REF and policy original bytes; COHORT_SEED_POLICY; PayloadGraph.cohort; validate_preimages; release_checks |
| `Cohort.primary_region` | c: independent collector/packager | proof.cohort_seed_policy_sha256 configuration FILE_REF and policy original bytes; COHORT_SEED_POLICY; PayloadGraph.cohort; validate_preimages; release_checks |
| `Rate.boot` | c: independent collector/packager | Original COUNTER observer/publisher counts and approved post LOAD_SEGMENT intervals; a1.validate_row; LOAD_SEGMENT; validate_publisher_traces; gate_views.budget_view exact count/duration equality |
| `Rate.start_us` | c: independent collector/packager | Original COUNTER observer/publisher counts and approved post LOAD_SEGMENT intervals; a1.validate_row; LOAD_SEGMENT; validate_publisher_traces; gate_views.budget_view exact count/duration equality |
| `Rate.end_us` | c: independent collector/packager | Original COUNTER observer/publisher counts and approved post LOAD_SEGMENT intervals; a1.validate_row; LOAD_SEGMENT; validate_publisher_traces; gate_views.budget_view exact count/duration equality |
| `Rate.workspace_reads` | c: independent collector/packager | Original COUNTER observer/publisher counts and approved post LOAD_SEGMENT intervals; a1.validate_row; LOAD_SEGMENT; validate_publisher_traces; gate_views.budget_view exact count/duration equality |
| `Rate.health_reads` | c: independent collector/packager | Original COUNTER observer/publisher counts and approved post LOAD_SEGMENT intervals; a1.validate_row; LOAD_SEGMENT; validate_publisher_traces; gate_views.budget_view exact count/duration equality |
| `Rate.publisher_reads` | c: independent collector/packager | Original COUNTER observer/publisher counts and approved post LOAD_SEGMENT intervals; a1.validate_row; LOAD_SEGMENT; validate_publisher_traces; gate_views.budget_view exact count/duration equality |
| `Rate.publisher_writes` | c: independent collector/packager | Original COUNTER observer/publisher counts and approved post LOAD_SEGMENT intervals; a1.validate_row; LOAD_SEGMENT; validate_publisher_traces; gate_views.budget_view exact count/duration equality |
| `Rate.burst_reads` | c: independent collector/packager | Explicit zero in the original-byte budget adapter; startup operations are independently supplied by PEAKS; gate_views.budget_view; gate_load.measured_peaks and exact approved component limits |
| `Rate.burst_pending` | c: independent collector/packager | Original COUNTER observer/publisher counts and approved post LOAD_SEGMENT intervals; a1.validate_row; LOAD_SEGMENT; validate_publisher_traces; gate_views.budget_view exact count/duration equality |
| `Budget.pre_input` | c: independent collector/packager | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes; BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.regional_caps` | c: independent collector/packager | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes; BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.fleet_cap` | c: independent collector/packager | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes; BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.surge` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.publisher_cap` | c: independent collector/packager | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes; BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.regional_reads` | c: independent collector/packager | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes; BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.regional_pending` | c: independent collector/packager | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes; BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.fleet_reads` | c: independent collector/packager | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes; BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.publisher_reads` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.publisher_writes` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.cpu_peak` | c: independent collector/packager | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes; BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.cpu_increment` | c: independent collector/packager | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes; BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.component_seconds` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.rates` | c: independent collector/packager | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes; BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.pre_completed_us` | c: independent collector/packager | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes; BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.post_completed_us` | c: independent collector/packager | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes; BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.pre_digest` | c: independent collector/packager | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes; BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.post_pre_digest` | c: independent collector/packager | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes; BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.post_workspace_count` | c: independent collector/packager | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes; BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.post_regional_caps` | c: independent collector/packager | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes; BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.export_operations` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.export_bytes` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.close_transactions` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.close_rows` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.close_elapsed_us` | c: independent collector/packager | Single verified Shutdown.measurements population; SHUTDOWN_MEASUREMENT; PayloadGraph.materialize_one; gate_views.budget_view |
| `Budget.handoff_elapsed_us` | c: independent collector/packager | Single verified Shutdown.measurements population; SHUTDOWN_MEASUREMENT; PayloadGraph.materialize_one; gate_views.budget_view |
| `Budget.grace_us` | c: independent collector/packager | Single verified Shutdown.measurements population; SHUTDOWN_MEASUREMENT; PayloadGraph.materialize_one; gate_views.budget_view |
| `Budget.publisher_unresolved_rows_per_second` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.publisher_control_rows_per_second` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.publisher_transactions_per_second` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.publisher_rpc_per_second` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.metadata_bytes_per_second` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.export_passes` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.export_root_writes` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.export_write_bytes` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.cleanup_spanner_keys` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.cleanup_sink_keys` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.cleanup_seconds` | c: independent collector/packager | Approved loading/budget-measurements.json original dependency; component_us converted to seconds; BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.load_segments` | c: independent collector/packager | Pre/post v2_load segments DETAIL originals and approved limits FILE_REF originals; BUDGET_ENVELOPE; LOAD_SEGMENT; LIMITS/PEAKS; validate_detail; gate_views.budget_view; compare_peak_limits |
| `Budget.projected_load_segments` | c: independent collector/packager | Pre/post v2_load segments DETAIL originals and approved limits FILE_REF originals; BUDGET_ENVELOPE; LOAD_SEGMENT; LIMITS/PEAKS; validate_detail; gate_views.budget_view; compare_peak_limits |
| `Budget.peak_limits` | c: independent collector/packager | Pre/post v2_load segments DETAIL originals and approved limits FILE_REF originals; BUDGET_ENVELOPE; LOAD_SEGMENT; LIMITS/PEAKS; validate_detail; gate_views.budget_view; compare_peak_limits |
| `ExternalProof.name` | c: independent collector/packager | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory; EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.router_revision` | c: independent collector/packager | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory; EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.enclave_revision` | c: independent collector/packager | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory; EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.raw` | c: independent collector/packager | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory; EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.sha256` | c: independent collector/packager | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory; EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.passed` | c: independent collector/packager | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory; EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.stage` | c: independent collector/packager | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory; EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `External.proofs` | c: independent collector/packager | EXTERNAL_PROOFS original bytes, approved requirements and recomputed retained pilot output; EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.required` | c: independent collector/packager | EXTERNAL_PROOFS original bytes, approved requirements and recomputed retained pilot output; EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.drain_selected` | c: independent collector/packager | EXTERNAL_PROOFS original bytes, approved requirements and recomputed retained pilot output; EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.signer_checked` | c: independent collector/packager | EXTERNAL_PROOFS original bytes, approved requirements and recomputed retained pilot output; EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.attestation_checked` | c: independent collector/packager | EXTERNAL_PROOFS original bytes, approved requirements and recomputed retained pilot output; EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.exposure_accepted` | c: independent collector/packager | EXTERNAL_PROOFS original bytes, approved requirements and recomputed retained pilot output; EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.post_input` | c: independent collector/packager | EXTERNAL_PROOFS original bytes, approved requirements and recomputed retained pilot output; EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.required_stages` | c: independent collector/packager | EXTERNAL_PROOFS original bytes, approved requirements and recomputed retained pilot output; EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `ResetSupport.rows` | c: independent collector/packager | RESOLUTION_DAG retained original payload, dependency files, recomputed report/rollback results and history support FILE_REFs; RESOLUTION_DAG; RESET_HISTORY_ENTRY; gate_views.reset_view; gate_retained.delegate; validate_preimages |
| `ResetSupport.proof` | c: independent collector/packager | RESOLUTION_DAG retained original payload, dependency files, recomputed report/rollback results and history support FILE_REFs; RESOLUTION_DAG; RESET_HISTORY_ENTRY; gate_views.reset_view; gate_retained.delegate; validate_preimages |
| `ResetSupport.resets` | c: independent collector/packager | RESOLUTION_DAG retained original payload, dependency files, recomputed report/rollback results and history support FILE_REFs; RESOLUTION_DAG; RESET_HISTORY_ENTRY; gate_views.reset_view; gate_retained.delegate; validate_preimages |
| `ResetSupport.support_files` | c: independent collector/packager | RESOLUTION_DAG retained original payload, dependency files, recomputed report/rollback results and history support FILE_REFs; RESOLUTION_DAG; RESET_HISTORY_ENTRY; gate_views.reset_view; gate_retained.delegate; validate_preimages |
| `Signature.object_id` | c: independent collector/packager | Approved loading/signatures.json originals, referenced receipt bytes and independently pinned public keys; gate_views.signature_views; binding.verify_binding; certificate.verify_certificate; verify_original_receipt |
| `Signature.token` | c: independent collector/packager | Approved loading/signatures.json originals, referenced receipt bytes and independently pinned public keys; gate_views.signature_views; binding.verify_binding; certificate.verify_certificate; verify_original_receipt |
| `Signature.keys` | c: independent collector/packager | Approved loading/signatures.json originals, referenced receipt bytes and independently pinned public keys; gate_views.signature_views; binding.verify_binding; certificate.verify_certificate; verify_original_receipt |
| `Signature.now` | c: independent collector/packager | Retained signature metadata; verification time comes from the original event, not this redundant value; gate_views.signature_views; gate_accounting event-time receipt verification |
| `Signature.expected` | c: independent collector/packager | Approved loading/signatures.json originals, referenced receipt bytes and independently pinned public keys; gate_views.signature_views; binding.verify_binding; certificate.verify_certificate; verify_original_receipt |
| `Signature.attempt` | c: independent collector/packager | Approved loading/signatures.json originals, referenced receipt bytes and independently pinned public keys; gate_views.signature_views; binding.verify_binding; certificate.verify_certificate; verify_original_receipt |
| `Signature.server_attempt` | c: independent collector/packager | Approved loading/signatures.json originals, referenced receipt bytes and independently pinned public keys; gate_views.signature_views; binding.verify_binding; certificate.verify_certificate; verify_original_receipt |
| `Signature.claims` | c: independent collector/packager | Approved loading/signatures.json originals, referenced receipt bytes and independently pinned public keys; gate_views.signature_views; binding.verify_binding; certificate.verify_certificate; verify_original_receipt |
| `Preimage.name` | c: independent collector/packager | Original exported path and UTF-8 bytes; conflicting candidates remain separate; FILE_REF; gate_loading.retained_json; validate_preimages; PayloadGraph.file/object/detail |
| `Preimage.raw` | c: independent collector/packager | Original exported path and UTF-8 bytes; conflicting candidates remain separate; FILE_REF; gate_loading.retained_json; validate_preimages; PayloadGraph.file/object/detail |
| `Shutdown.entries` | c: independent collector/packager | Committed SHUTDOWN_INVENTORY writer detail -> measurement FILE_REF -> approved source original bytes; aliases must match exactly; SHUTDOWN_INVENTORY; SHUTDOWN_ENTRY; SHUTDOWN_MEASUREMENT; source parser; PayloadGraph.materialize_one; decode_bundle alias identity/byte checks |
| `Shutdown.measurements` | c: independent collector/packager | Committed SHUTDOWN_INVENTORY writer detail -> measurement FILE_REF -> approved source original bytes; aliases must match exactly; SHUTDOWN_INVENTORY; SHUTDOWN_ENTRY; SHUTDOWN_MEASUREMENT; source parser; PayloadGraph.materialize_one; decode_bundle alias identity/byte checks |
| `Shutdown.aliases` | c: independent collector/packager | Committed SHUTDOWN_INVENTORY writer detail -> measurement FILE_REF -> approved source original bytes; aliases must match exactly; SHUTDOWN_INVENTORY; SHUTDOWN_ENTRY; SHUTDOWN_MEASUREMENT; source parser; PayloadGraph.materialize_one; decode_bundle alias identity/byte checks |
| `Bundle.rows` | a/b/c: transport join; see member map | Original graph decoded by gate_loader.load; see corresponding type rows in this table; PayloadGraph.materialize; gate_views; validate_original_bytes; validate_preimages |
| `Bundle.proof` | a/b/c: transport join; see member map | Original PROOF_MANIFEST and each PREREQUISITE_LEDGER byte string; PROOF_MANIFEST; PREREQUISITE_LEDGER; ledger_preimages; validate_preimages |
| `Bundle.ledgers` | a/b/c: transport join; see member map | Original PROOF_MANIFEST and each PREREQUISITE_LEDGER byte string; PROOF_MANIFEST; PREREQUISITE_LEDGER; ledger_preimages; validate_preimages |
| `Bundle.attachments` | a/b/c: transport join; see member map | Original graph decoded by gate_loader.load; see corresponding type rows in this table; PayloadGraph.materialize; gate_views; validate_original_bytes; validate_preimages |
| `Bundle.inventory` | a/b/c: transport join; see member map | Original graph decoded by gate_loader.load; see corresponding type rows in this table; PayloadGraph.materialize; gate_views; validate_original_bytes; validate_preimages |
| `Bundle.cohort` | a/b/c: transport join; see member map | Original graph decoded by gate_loader.load; see corresponding type rows in this table; PayloadGraph.materialize; gate_views; validate_original_bytes; validate_preimages |
| `Bundle.budget` | a/b/c: transport join; see member map | Original graph decoded by gate_loader.load; see corresponding type rows in this table; PayloadGraph.materialize; gate_views; validate_original_bytes; validate_preimages |
| `Bundle.external` | a/b/c: transport join; see member map | Original graph decoded by gate_loader.load; see corresponding type rows in this table; PayloadGraph.materialize; gate_views; validate_original_bytes; validate_preimages |
| `Bundle.reset_support` | a/b/c: transport join; see member map | Original graph decoded by gate_loader.load; see corresponding type rows in this table; PayloadGraph.materialize; gate_views; validate_original_bytes; validate_preimages |
| `Bundle.signatures` | a/b/c: transport join; see member map | Original graph decoded by gate_loader.load; see corresponding type rows in this table; PayloadGraph.materialize; gate_views; validate_original_bytes; validate_preimages |
| `Bundle.transport` | a/b/c: transport join; see member map | Original envelope plus report, streams, leaves and original graph from finite input inventory; gate_prior._resolve; validate_gate_output; validate_preimages; release_checks; ledger_preimages; exact current binding |
| `Bundle.fleet` | a/b/c: transport join; see member map | Original envelope plus report, streams, leaves and original graph from finite input inventory; gate_prior._resolve; validate_gate_output; validate_preimages; release_checks; ledger_preimages; exact current binding |
| `Bundle.pre` | a/b/c: transport join; see member map | Original envelope plus report, streams, leaves and original graph from finite input inventory; gate_prior._resolve; validate_gate_output; validate_preimages; release_checks; ledger_preimages; exact current binding |
| `Bundle.serving` | a/b/c: transport join; see member map | Optional original envelope, inventoried but never a §8 predecessor or clock authority; a1.GATE_OUTPUT parsing; deliberately excluded from REQUIRED and clock |
| `Bundle.completed_at_us` | a/b/c: transport join; see member map | Requested transport metadata. Invalid fields produce loading issues; scope recovered together from independent inventory, proof rosters or surviving day-manifest lifetimes. Window cannot assert evidence; integer; day; INTERVAL; gate_report.diagnostic_scope/window_for; gates.clock (seed and elapsed are separate from requested observation scope). Completion fallback is a report bound, not a trusted gate timestamp |
| `Bundle.days` | a/b/c: transport join; see member map | Requested transport metadata. Invalid fields produce loading issues; scope recovered together from independent inventory, proof rosters or surviving day-manifest lifetimes. Window cannot assert evidence; integer; day; INTERVAL; gate_report.diagnostic_scope/window_for; gates.clock (seed and elapsed are separate from requested observation scope). Completion fallback is a report bound, not a trusted gate timestamp |
| `Bundle.undefined_preimages` | a/b/c: transport join; see member map | Loader-generated diagnostics and materialized original DETAIL streams; diagnostics are not evidence and are never proof preimages; LoadingIssue; GAP_NAME; validate_detail; validate_original_bytes |
| `Bundle.preimages` | a/b/c: transport join; see member map | Original named proof/manifest objects and FILE_REF bytes; OBJECT_PREIMAGES schemas; FILE_REF; PayloadGraph; validate_preimages |
| `Bundle.a2_payloads` | a/b/c: transport join; see member map | Original named proof/manifest objects and FILE_REF bytes; OBJECT_PREIMAGES schemas; FILE_REF; PayloadGraph; validate_preimages |
| `Bundle.shutdown` | a/b/c: transport join; see member map | Only the loader committed SHUTDOWN_INVENTORY -> measurement -> source population; PayloadGraph.materialize_one; PROOF_MANIFEST shutdown digest binding in load; validate_preimages; decode_bundle alias equality |
| `Bundle.window` | a/b/c: transport join; see member map | Requested transport metadata. Invalid fields produce loading issues; scope recovered together from independent inventory, proof rosters or surviving day-manifest lifetimes. Window cannot assert evidence; integer; day; INTERVAL; gate_report.diagnostic_scope/window_for; gates.clock (seed and elapsed are separate from requested observation scope). Completion fallback is a report bound, not a trusted gate timestamp |
| `Bundle.a2_streams` | a/b/c: transport join; see member map | Loader-generated diagnostics and materialized original DETAIL streams; diagnostics are not evidence and are never proof preimages; LoadingIssue; GAP_NAME; validate_detail; validate_original_bytes |
| `RetainedResult.output` | c: independent collector/packager | Actual retained callable execution on original payload/dependencies through finite file port, with counted calls; gate_retained.delegate; RETAINED_OUTPUT; capture/output byte equality; check_calls |
| `RetainedResult.opened` | c: independent collector/packager | Actual retained callable execution on original payload/dependencies through finite file port, with counted calls; gate_retained.delegate; RETAINED_OUTPUT; capture/output byte equality; check_calls |
| `RetainedResult.calls` | c: independent collector/packager | Actual retained callable execution on original payload/dependencies through finite file port, with counted calls; gate_retained.delegate; RETAINED_OUTPUT; capture/output byte equality; check_calls |
| `RetainedResult.components` | c: independent collector/packager | Actual retained callable execution on original payload/dependencies through finite file port, with counted calls; gate_retained.delegate; RETAINED_OUTPUT; capture/output byte equality; check_calls |
| `ReleasePlan.path` | b: reviewed tool/release | Independent CLI release-plan bytes, pinned by explicit external digest; read_release_plan; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.raw` | b: reviewed tool/release | Independent CLI release-plan bytes, pinned by explicit external digest; read_release_plan; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.artifacts` | b: reviewed tool/release | Independent CLI release-plan bytes, pinned by explicit external digest; read_release_plan; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.criteria` | b: reviewed tool/release | Independent CLI release-plan bytes, pinned by explicit external digest; read_release_plan; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.verifiers` | b: reviewed tool/release | Independent CLI release-plan bytes, pinned by explicit external digest; read_release_plan; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.required` | b: reviewed tool/release | Independent CLI release-plan bytes, pinned by explicit external digest; read_release_plan; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.required_stages` | b: reviewed tool/release | Independent CLI release-plan bytes, pinned by explicit external digest; read_release_plan; FILE_REF; digest; gate_release.release_checks |
| `LoadingPorts.verify_certificate` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.verify_original_receipt` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.verify_binding` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.comparator_positive` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.positive_sample` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.known_exclusion` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.fleet_budgets_pre` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.collect_rollbacks` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.report` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.post_flip` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.approvals` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.approved_files` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.read_release_file` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.roster_coverage` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.retained` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.gate_roster` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.release` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.measurements` | b: reviewed tool/release | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port; gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |

## Nested wire shapes and scalar bounds

Generated by introspecting the **existing schemas**, not generating evidence.
All objects have exact fields; byte limits apply conjunctively. `u` is exact
nonnegative int64. Source-qualified unchecked/retained validators below delegate
to their original function and invariant; they are not permission to accept
arbitrary bytes. EVENT is the phase-discriminated union of the listed P1 events.

### a1.COUNTER (≤65536 bytes)

| Field | Exact type / nested schema |
|---|---|
| `counter_overflow` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `region` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `booking_unknown` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `terminal_counts` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `comparison_attempts` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `drops` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `samples_inserted` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `counts` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `dimension_overflow` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `authorize_shadow_hist` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `started_at_us` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `instance` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `v` | `literal(2,)` |
| `flushed_at_us` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `first_evidence_at_us` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `comparison_dropped` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `exclusions` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `conflicting_samples` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `booking_pending` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `last_mismatch_at_us` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `rejections` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `closed` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `evidence_write_hist` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `first_gap_at_us` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `sequence` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `policy_version` | `literal('shadow-v2',)` |
| `duplicate_samples` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `router_revision` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `admission_observer` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `authorize_attempts` | `u (exact nonnegative int64)` |
| `authorize_fresh` | `u (exact nonnegative int64)` |
| `authorize_replay` | `u (exact nonnegative int64)` |
| `settle_attempts` | `u (exact nonnegative int64)` |
| `refund_attempts` | `u (exact nonnegative int64)` |
| `A` | `u (exact nonnegative int64)` |
| `B` | `u (exact nonnegative int64)` |
| `C` | `u (exact nonnegative int64)` |
| `proof_compared` | `u (exact nonnegative int64)` |
| `proof_certificate` | `u (exact nonnegative int64)` |
| `proof_observed` | `u (exact nonnegative int64)` |
| `requested_eligible` | `u (exact nonnegative int64)` |
| `requested_excluded` | `u (exact nonnegative int64)` |
| `requested_unknown` | `u (exact nonnegative int64)` |
| `checkpoint_attempts` | `u (exact nonnegative int64)` |
| `checkpoint_failures` | `u (exact nonnegative int64)` |
| `checkpoint_recoveries` | `u (exact nonnegative int64)` |
| `unknown_commits` | `u (exact nonnegative int64)` |
| `unreconciled_commits` | `u (exact nonnegative int64)` |
| `irrecoverable_losses` | `u (exact nonnegative int64)` |
| `eligible_demand` | `null  or  u (exact nonnegative int64)` |
| `event_seq` | `u (exact nonnegative int64)` |
| `durable_event_hwm` | `u (exact nonnegative int64)` |
| `checkpoint_seq` | `u (exact nonnegative int64)` |
| `receipt_count` | `u (exact nonnegative int64)` |
| `receipt_chain_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `loss_count` | `u (exact nonnegative int64)` |
| `reset_count` | `u (exact nonnegative int64)` |
| `diagnostics` | `map[≤128] ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/ → u (exact nonnegative int64)` |
| `buckets` | `array[0..144, unique=False] of p1.BUCKET` |
| `publisher` | `health.PUBLISHER_OPERATIONS` |
| `contract_revision` | `literal('a1',)` |

Additional invariant: `extend.<locals>.validate [async_settle_shadow_v2_a1_deltas.py:83]`.

### a1.CLOSURE (≤4096 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `router_revision` | `ASCII[1..40] /[0-9a-f]{40}/` |
| `region` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `reason` | `literal('utc_boundary', 'shutdown', 'opt_out')` |
| `interval_end_us` | `u (exact nonnegative int64)` |
| `final_event_seq` | `u (exact nonnegative int64)` |
| `final_checkpoint_seq` | `u (exact nonnegative int64)` |
| `durable_event_hwm` | `u (exact nonnegative int64)` |
| `receipt_chain_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `counter_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `retained_work` | `literal(0,)` |
| `pending_comparisons` | `literal(0,)` |
| `unresolved_commits` | `literal(0,)` |
| `loss_count` | `u (exact nonnegative int64)` |
| `observer_fold_through_us` | `u (exact nonnegative int64)` |
| `previous_day_closure_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `contract_revision` | `literal('a1',)` |
| `authorization_obligations` | `literal('receipt_chain',)` |

Additional invariant: `extend.<locals>.validate [async_settle_shadow_v2_a1_deltas.py:83]`.

### a1.MANIFEST (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `config_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `fixture_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `router_writers` | `map[≤4096] ASCII[1..64] /[A-Za-z0-9_-]+/ → inline_113` |
| `enclave_writers` | `map[≤4096] ASCII[1..64] /[A-Za-z0-9_-]+/ → inline_113` |
| `revisions` | `inline_114` |
| `counter_root` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `closure_root` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `receipts_root` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `prerequisite_ledger_root` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `independent_inventory_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `first_evidence_at_us` | `null  or  u (exact nonnegative int64)` |
| `proof_manifest_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `disabled_flag_intervals` | `array[0..128, unique=False] of p1.FLAG_INTERVAL` |
| `complete` | `exact boolean` |
| `gaps` | `array[0..128, unique=False] of p1.GAP_INTERVAL` |
| `contract_revision` | `literal('a1',)` |
| `close_result_root` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `publisher_trace_root` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `health_basis_root` | `ASCII[1..64] /[0-9a-f]{64}/` |

Additional invariant: `extend.<locals>.validate [async_settle_shadow_v2_a1_deltas.py:83]`.

### a1.PROOF_MANIFEST (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `source_shas` | `inline_115` |
| `evaluator_fixture_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `binding_fixture_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `wire_fixture_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `ledger_root` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `shutdown_inventory_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `cohort_seed_policy_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `pre_budget_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `post_budget_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `publisher_slot_trace_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `external_proofs_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `resolution_dag_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `rosters_by_day` | `map[≤32] day [async_settle_shadow_v2_schema.py:95] → inline_116` |
| `instance_regions_by_day` | `map[≤32] day [async_settle_shadow_v2_schema.py:95] → map[≤4096] ASCII[1..64] /[A-Za-z0-9_-]+/ → ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `attachments` | `array[0..10000, unique=True] of inline_117` |
| `explicit_keys` | `array[0..10000, unique=True] of ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `complete` | `exact boolean` |
| `contract_revision` | `literal('a1',)` |
| `health_basis_root` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `authorization_obligation_root` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `close_result_root` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `publisher_trace_root` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `handoff_accounting_root` | `ASCII[1..64] /[0-9a-f]{64}/` |

Additional invariant: `extend.<locals>.validate [async_settle_shadow_v2_a1_deltas.py:83]`.

### a1.PREREQUISITE_LEDGER (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `role` | `literal('router', 'enclave')` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `revision` | `ASCII[1..40] /[0-9a-f]{40}/` |
| `region` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `policy_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `fixture_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `config_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `boot_at_us` | `u (exact nonnegative int64)` |
| `collection_interval` | `p1.INTERVAL` |
| `traffic_ready_at_us` | `u (exact nonnegative int64)` |
| `transport` | `p1.ARTIFACT` |
| `size` | `p1.ARTIFACT` |
| `regional_load` | `p1.ARTIFACT` |
| `boot_transport` | `p1.ARTIFACT` |
| `observer_freshness` | `p1.ARTIFACT` |
| `marker_keys` | `p1.ARTIFACT` |
| `previous_ledger_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `contract_revision` | `literal('a1',)` |

Additional invariant: `extend.<locals>.validate [async_settle_shadow_v2_a1_deltas.py:83]`.

### a2.GATE_OUTPUT (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `gate` | `literal('transport', 'fleet-budgets-pre', 'fleet-budgets', 'shadow-serving', 'pre-flip', 'post-flip')` |
| `verdict` | `literal('PASS', 'BLOCKED')` |
| `completed_at_us` | `u (exact nonnegative int64)` |
| `proof_manifest_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `prerequisite_ledger_root` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `gaps` | `array[0..128, unique=False] of ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `clock_start_us` | `null  or  u (exact nonnegative int64)` |
| `elapsed_seconds` | `u (exact nonnegative int64)` |
| `coverage_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `contract_revision` | `literal('a1',)` |

Additional invariant: `extend.<locals>.validate [async_settle_shadow_v2_a1_deltas.py:83]`.

### a1.CAP (≤4096 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `terminal_reserved` | `u (exact nonnegative int64)` |
| `event_reserved` | `u (exact nonnegative int64)` |
| `contract_revision` | `literal('a1',)` |

Additional invariant: `extend.<locals>.validate [async_settle_shadow_v2_a1_deltas.py:83]`.

### a1.RETENTION (≤4096 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `retired_through` | `day [async_settle_shadow_v2_schema.py:95]` |
| `kinds` | `array[9..9, unique=True] of literal('async_settle_shadow_sample_v2', 'async_settle_shadow_certificate_v2', 'async_settle_shadow_receipts_v2', 'async_settle_shadow_counter_v2', 'async_settle_shadow_closure_v2', 'async_settle_shadow_control_v2', 'async_settle_shadow_health_v2', 'async_settle_shadow_health_control_v2', 'async_settle_shadow_close_result_v2')` |
| `contract_revision` | `literal('a1',)` |

Additional invariant: `extend.<locals>.validate [async_settle_shadow_v2_a1_deltas.py:83]`.

### a1.CLOSE_RESULT (≤4096 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a1',)` |
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `router_revision` | `ASCII[1..40] /[0-9a-f]{40}/` |
| `closure_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `final_checkpoint_seq` | `u (exact nonnegative int64)` |
| `attempt_started_at_us` | `u (exact nonnegative int64)` |
| `observed_at_us` | `u (exact nonnegative int64)` |
| `elapsed_us` | `u (exact nonnegative int64)` |
| `physical_commit_ack_at_us` | `u (exact nonnegative int64)` |
| `flush_remaining_us` | `u (exact nonnegative int64)` |
| `outcome` | `literal('acknowledged',)` |
| `shutdown_remaining_us` | `null  or  u (exact nonnegative int64)` |
| `handoff_attempt_limit` | `literal(1,)` |

Additional invariant: `_close_result [async_settle_shadow_v2_a1_evidence.py:56]`.

### a1.HANDOFF_ACCOUNTING (≤1024 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a1',)` |
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `expected_handoffs` | `literal(1,)` |
| `attempts_lower` | `u (exact nonnegative int64)` |
| `attempts_upper` | `literal(1,)` |
| `landed` | `exact boolean` |
| `reply_status` | `literal('not_proven',)` |
| `failures_lower` | `literal(0,)` |
| `failures_upper` | `literal(1,)` |
| `unknown_reply_upper` | `literal(1,)` |
| `result_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |

Additional invariant: `_handoff [async_settle_shadow_v2_a1_evidence.py:106]`.

### a1.COVERAGE_PAGE (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a1',)` |
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `stream` | `literal('close_result', 'publisher_trace', 'handoff_accounting', 'health_basis', 'authorization_obligation')` |
| `page_seq` | `sequence [async_settle_shadow_v2_schema.py:107]` |
| `previous_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `entries` | `array[0..1000, unique=False] of a2.ROOT_ENTRY` |

Additional invariant: `_coverage [async_settle_shadow_v2_a1_evidence.py:128]`.

### health.HEALTH (≤4096 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `authority` | `literal('local',)` |
| `source` | `literal('shadow_observation',)` |
| `publisher_boot_id` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `publisher_region` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `router_revision` | `ASCII[1..40] /[0-9a-f]{40}/` |
| `complete` | `exact boolean` |
| `slot` | `u (exact nonnegative int64)` |
| `sample_count` | `u (exact nonnegative int64)` |
| `backlog_count` | `u (exact nonnegative int64)` |
| `frozen_micro` | `u (exact nonnegative int64)` |
| `p50_age_us` | `u (exact nonnegative int64)` |
| `p95_age_us` | `u (exact nonnegative int64)` |
| `oldest_unresolved_age_us` | `u (exact nonnegative int64)` |
| `dead_count` | `u (exact nonnegative int64)` |
| `contract_revision` | `literal('a1',)` |
| `observed_at_ns` | `u (exact nonnegative int64)` |
| `publisher_heartbeat_ns` | `u (exact nonnegative int64)` |
| `backlog_age_at_seconds` | `binary64 [async_settle_shadow_v2_a1_numbers.py:30]` |

Additional invariant: `extend.<locals>.validate [async_settle_shadow_v2_a1_deltas.py:83]`.

### health.HEALTH_CLAIM (≤1024 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `authority` | `literal('local',)` |
| `slot` | `u (exact nonnegative int64)` |
| `owner_boot_id` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `contract_revision` | `literal('a1',)` |
| `claim_at_ns` | `u (exact nonnegative int64)` |

Additional invariant: `extend.<locals>.validate [async_settle_shadow_v2_a1_deltas.py:83]`.

### health.HEALTH_BASIS_BATCH (≤65536 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a1',)` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `sequence` | `sequence [async_settle_shadow_v2_schema.py:107]` |
| `previous_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `bases` | `array[1..10, unique=False] of health.HEALTH_BASIS` |

Additional invariant: `_basis_batch [async_settle_shadow_v2_a1_health.py:81]`.

### health.PUBLISHER_TRACE_PAGE (≤65536 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a1',)` |
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `first_slot` | `u (exact nonnegative int64)` |
| `last_slot` | `u (exact nonnegative int64)` |
| `previous_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `entries` | `array[1..60, unique=False] of health.PUBLISHER_SLOT_TRACE` |

Additional invariant: `_trace_page [async_settle_shadow_v2_a1_health.py:139]`.

### p1.SAMPLE (≤8192 bytes)

| Field | Exact type / nested schema |
|---|---|
| `raw_usage` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `timing` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `python_usage` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `booked_micro` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `legacy_frozen_micro` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `frozen_micro` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `booked_minus_frozen` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `booked_minus_rebuilt` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `authorization_id` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `rebuilt_micro` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `snapshot_hash` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `authorize_at_us` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `go_micro` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `v` | `literal(2,)` |
| `workspace_fingerprint` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `rebuilt_minus_frozen` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `payload_hash` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `model_id` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `booking_observed_at_us` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `python_micro` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `endpoint_id` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `reason_codes` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `legacy_usage` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `go_usage` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `adapter` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `admission` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `observed_at_us` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `classification` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `streamed` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `provenance` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `booking` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `authorization_day` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `python_minus_go` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `route_type` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `rebuilt_snapshot_hash` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `policy_version` | `literal('shadow-v2',)` |
| `eligibility` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `deployment` | `unchecked [async_settle_shadow_v2_evidence.py:299]` |
| `proof_kind` | `literal('compared', 'observed_binding')` |
| `original_terminal_receipt` | `p1.RECEIPT_REFERENCE` |
| `health_source` | `literal('shadow_observation',)` |
| `health_slot` | `null  or  u (exact nonnegative int64)` |
| `prediction_basis` | `literal('counterfactual_backlog_v2',)` |

Additional invariant: `_sample [async_settle_shadow_v2_evidence.py:328]`.

### p1.CERTIFICATE_RECORD (≤4096 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `claims` | `p1.CERTIFICATE_PROJECTION` |
| `certificate_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `issuer_keyring_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `verified_at_us` | `u (exact nonnegative int64)` |
| `verification_result` | `literal('verified',)` |
| `authorization_day` | `day [async_settle_shadow_v2_schema.py:95]` |

Additional invariant: `<lambda> [async_settle_shadow_v2_evidence.py:212]`.

### p1.PAGE (≤65536 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `first_seq` | `sequence [async_settle_shadow_v2_schema.py:107]` |
| `last_seq` | `sequence [async_settle_shadow_v2_schema.py:107]` |
| `previous_page_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `events` | `array[1..128, unique=False] of EventSchema [union P1 events]` |

Additional invariant: `_page [async_settle_shadow_v2_evidence.py:168]`.

### p1.AUTHORIZE_EVENT (≤2048 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `seq` | `sequence [async_settle_shadow_v2_schema.py:107]` |
| `event_at_us` | `u (exact nonnegative int64)` |
| `receipt_at_us` | `u (exact nonnegative int64)` |
| `phase` | `literal('authorize', 'replay', 'settle', 'refund', 'completion')` |
| `authorization_day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `authorization_id` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `identity` | `p1.AUTHORIZATION_IDENTITY` |
| `router_revision` | `ASCII[1..40] /[0-9a-f]{40}/` |
| `dimensions` | `p1.DIMENSIONS` |
| `decision` | `p1.DECISION_REFERENCE` |
| `facts` | `null  or  p1.REQUESTED_FACTS` |
| `candidates` | `null  or  array[1..128, unique=False] of inline_118` |
| `receipt_locator` | `null  or  a2.COORDINATES` |
| `partition` | `literal('A', 'B', 'C')` |
| `eligibility` | `literal('eligible', 'excluded', 'unknown')` |
| `diagnostic` | `null  or  inline_119` |

Additional invariant: `<lambda> [async_settle_shadow_v2_evidence.py:140]`.

### p1.TERMINAL_EVENT (≤2048 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `seq` | `sequence [async_settle_shadow_v2_schema.py:107]` |
| `event_at_us` | `u (exact nonnegative int64)` |
| `receipt_at_us` | `u (exact nonnegative int64)` |
| `phase` | `literal('authorize', 'replay', 'settle', 'refund', 'completion')` |
| `authorization_day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `authorization_id` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `identity` | `p1.AUTHORIZATION_IDENTITY` |
| `router_revision` | `ASCII[1..40] /[0-9a-f]{40}/` |
| `dimensions` | `p1.DIMENSIONS` |
| `decision` | `p1.DECISION_REFERENCE` |
| `facts` | `null  or  p1.REQUESTED_FACTS` |
| `candidates` | `null  or  array[1..128, unique=False] of inline_118` |
| `receipt_locator` | `null  or  a2.COORDINATES` |
| `original_receipt` | `null  or  joins.LOCATOR` |
| `certificate_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `payload_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `proof_kind` | `literal('none', 'compared', 'certificate', 'observed_binding')` |
| `partition` | `literal('A', 'B', 'C')` |
| `eligibility` | `literal('eligible', 'excluded', 'unknown')` |
| `comparison` | `null  or  literal('exact', 'explained-by-catalog-change', 'mismatch', 'unevaluable')` |
| `booking` | `p1.BOOKING` |
| `prediction` | `null  or  p1.PREDICTION` |
| `retry_original` | `null  or  p1.RECEIPT_REFERENCE` |
| `completion_of` | `null  or  sequence [async_settle_shadow_v2_schema.py:107]` |
| `diagnostic` | `null  or  inline_119` |

Additional invariant: `_event [async_settle_shadow_v2_evidence.py:85]`.

### a2.INPUT_LEAF (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a2',)` |
| `keys` | `array[0..10000, unique=False] of a2.FILE_REF` |
| `complete` | `exact boolean` |

Additional invariant: `<lambda> [async_settle_shadow_v2_a2_evidence.py:477]`.

### a2.INPUT_INVENTORY_ENTRY (≤2048 bytes)

| Field | Exact type / nested schema |
|---|---|
| `bundle` | `a2.FILE_REF` |
| `first_key` | `ASCII[1..512]` |
| `last_key` | `ASCII[1..512]` |
| `objects` | `u (exact nonnegative int64)` |

Additional invariant: `<lambda> [async_settle_shadow_v2_a2_evidence.py:480]`.

### a2.INDEPENDENT_INVENTORY (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a2',)` |
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `serving` | `a2.FILE_REF` |
| `deployment` | `a2.FILE_REF` |
| `approval` | `a2.APPROVAL` |
| `complete` | `exact boolean` |

### a2.SHUTDOWN_INVENTORY (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a2',)` |
| `writers` | `a2.DETAIL` |
| `measurements` | `a2.FILE_REF` |
| `approval` | `a2.APPROVAL` |
| `complete` | `exact boolean` |

### a2.SHUTDOWN_ENTRY (≤2048 bytes)

| Field | Exact type / nested schema |
|---|---|
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `termination_applies` | `exact boolean` |
| `reason` | `literal('utc_boundary', 'shutdown', 'opt_out', 'hard_kill')` |
| `interval_end_us` | `u (exact nonnegative int64)` |
| `grace_us` | `null  or  u (exact nonnegative int64)` |
| `original_close` | `null  or  a2.FILE_REF` |
| `close_result` | `null  or  a2.FILE_REF` |
| `measurement` | `a2.FILE_REF` |

### a2.SHUTDOWN_MEASUREMENT (≤2048 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a2',)` |
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `grace_us` | `u (exact nonnegative int64)` |
| `final_pass_us` | `u (exact nonnegative int64)` |
| `close_us` | `u (exact nonnegative int64)` |
| `result_us` | `u (exact nonnegative int64)` |
| `export_wait_us` | `u (exact nonnegative int64)` |
| `preparation_us` | `u (exact nonnegative int64)` |
| `outcome` | `literal('graceful', 'hard_kill')` |
| `source` | `a2.FILE_REF` |

### a2.COHORT_SEED_POLICY (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a2',)` |
| `workspace_fingerprints` | `array[1..32, unique=False] of ASCII[1..64] /[0-9a-f]{64}/` |
| `regional_seed_policy` | `literal('each_traffic_region', 'fleet_only')` |
| `required_regions` | `array[0..4096, unique=False] of ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `primary_region` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `maximum_publishers` | `u (exact nonnegative int64)` |
| `configuration` | `a2.FILE_REF` |
| `approval` | `a2.APPROVAL` |

Additional invariant: `_cohort [async_settle_shadow_v2_a2_evidence.py:486]`.

### a2.BUDGET_ENVELOPE (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a2',)` |
| `stage` | `literal('pre', 'post')` |
| `retained` | `a2.DELEGATED_PAYLOAD` |
| `output` | `a2.FILE_REF` |
| `pre_envelope_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `approval` | `a2.APPROVAL` |
| `v2_load` | `a2.LOAD_EVIDENCE` |

Additional invariant: `<lambda> [async_settle_shadow_v2_a2_evidence.py:505]`.

### a2.EXTERNAL_PROOFS (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a2',)` |
| `requirements` | `array[0..64, unique=False] of a2.EXTERNAL_REQUIREMENT` |
| `pilot` | `null  or  a2.DELEGATED_PAYLOAD` |

Additional invariant: `<lambda> [async_settle_shadow_v2_a2_evidence.py:510]`.

### a2.RESOLUTION_DAG (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a2',)` |
| `retained` | `a2.DELEGATED_PAYLOAD` |
| `history` | `a2.DETAIL` |
| `approvals` | `array[0..128, unique=False] of a2.APPROVAL` |

Additional invariant: `<lambda> [async_settle_shadow_v2_a2_evidence.py:515]`.

### a2.PROOF_RESULT (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a2',)` |
| `criterion` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `router_revision` | `ASCII[1..40] /[0-9a-f]{40}/` |
| `enclave_revision` | `ASCII[1..40] /[0-9a-f]{40}/` |
| `completed_at_us` | `u (exact nonnegative int64)` |
| `producer` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `checks` | `array[0..128, unique=False] of inline_120` |
| `sources` | `a2.DETAIL` |

### a2.LOAD_SEGMENT (≤8192 bytes)

| Field | Exact type / nested schema |
|---|---|
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `region` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `start_us` | `u (exact nonnegative int64)` |
| `end_us` | `u (exact nonnegative int64)` |
| `role` | `literal('router', 'enclave')` |
| `contender` | `exact boolean` |
| `workspace_count` | `u≤32` |
| `peaks` | `a2.PEAKS` |

Additional invariant: `<lambda> [async_settle_shadow_v2_a2_evidence.py:499]`.

### a2.EXPORT_OPERATION (≤2048 bytes)

| Field | Exact type / nested schema |
|---|---|
| `object` | `a2.FILE_REF` |
| `pass` | `u (exact nonnegative int64)` |
| `at_us` | `u (exact nonnegative int64)` |
| `operation` | `literal('get', 'put', 'delete')` |
| `native_rpcs` | `u (exact nonnegative int64)` |
| `bytes` | `u (exact nonnegative int64)` |
| `elapsed_us` | `u (exact nonnegative int64)` |

### a2.REVIEW_RECORD (≤1024 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a2',)` |
| `subject_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `reviewer` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `reviewed_at_us` | `u (exact nonnegative int64)` |
| `decision` | `literal('approved', 'rejected')` |

### inputs.BUDGET_MEASUREMENTS (≤8192 bytes)

| Field | Exact type / nested schema |
|---|---|
| `publisher_reads` | `a2.RATIONAL` |
| `publisher_writes` | `a2.RATIONAL` |
| `publisher_unresolved_rows_per_second` | `a2.RATIONAL` |
| `publisher_control_rows_per_second` | `a2.RATIONAL` |
| `publisher_transactions_per_second` | `a2.RATIONAL` |
| `publisher_rpc_per_second` | `a2.RATIONAL` |
| `metadata_bytes_per_second` | `a2.RATIONAL` |
| `surge` | `u (exact nonnegative int64)` |
| `export_operations` | `u (exact nonnegative int64)` |
| `export_bytes` | `u (exact nonnegative int64)` |
| `close_transactions` | `u (exact nonnegative int64)` |
| `close_rows` | `u (exact nonnegative int64)` |
| `export_passes` | `u (exact nonnegative int64)` |
| `export_root_writes` | `u (exact nonnegative int64)` |
| `export_write_bytes` | `u (exact nonnegative int64)` |
| `cleanup_spanner_keys` | `u (exact nonnegative int64)` |
| `cleanup_sink_keys` | `u (exact nonnegative int64)` |
| `cleanup_seconds` | `u (exact nonnegative int64)` |
| `component_us` | `inline_121` |

### inputs.SHUTDOWN_SOURCE (≤2048 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a2',)` |
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `grace_us` | `u (exact nonnegative int64)` |
| `final_pass_us` | `u (exact nonnegative int64)` |
| `close_us` | `u (exact nonnegative int64)` |
| `result_us` | `u (exact nonnegative int64)` |
| `export_wait_us` | `u (exact nonnegative int64)` |
| `preparation_us` | `u (exact nonnegative int64)` |
| `outcome` | `literal('graceful', 'hard_kill')` |

### inputs.PREREQUISITE_CONTEXT (≤2048 bytes)

| Field | Exact type / nested schema |
|---|---|
| `role` | `literal('router', 'enclave')` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `revision` | `ASCII[1..40] /[0-9a-f]{40}/` |
| `region` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `boot_at_us` | `u (exact nonnegative int64)` |
| `criterion` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `completed_at_us` | `u (exact nonnegative int64)` |

### inputs.RETAINED_OUTPUT (≤2048 bytes)

| Field | Exact type / nested schema |
|---|---|
| `entry_point` | `literal('pilot_enablement.fleet_budgets_pre', 'pilot_enablement.fleet_budgets', 'pilot_enablement.shadow_serving', 'pilot_enablement.pre_flip', 'pilot_enablement.post_flip', 'pilot_enablement.counter_coverage_report', 'pilot_enablement.roster_coverage', 'shadow_report.collect_rollbacks')` |
| `payload_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `output` | `a2.FILE_REF` |

### loading.LIMITS (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `regions` | `array[0..4096, unique=False] of inline_122` |
| `fleet` | `a2.PEAKS` |

Additional invariant: `<lambda> [async_settle_shadow_v2_gate_loading.py:23]`.

### joins.AUTHORIZATION_OBLIGATION_PAGE (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a1',)` |
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `page_seq` | `sequence [async_settle_shadow_v2_schema.py:107]` |
| `previous_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `entries` | `array[0..1000, unique=False] of joins.AUTHORIZATION_OBLIGATION` |

Additional invariant: `_obligation_page [async_settle_shadow_v2_a1_reconciliation.py:88]`.

### joins.OBLIGATION_PROJECTION (≤1024 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a1',)` |
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `closure_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `open_obligation_count` | `u (exact nonnegative int64)` |
| `open_obligations_root` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `reconciliation_root` | `ASCII[1..64] /[0-9a-f]{64}/` |

### p1.BUCKET (≤2048 bytes)

| Field | Exact type / nested schema |
|---|---|
| `phase` | `literal('authorize', 'settle', 'refund')` |
| `dimensions` | `p1.DIMENSIONS` |
| `authorize_attempts` | `u (exact nonnegative int64)` |
| `authorize_fresh` | `u (exact nonnegative int64)` |
| `authorize_replay` | `u (exact nonnegative int64)` |
| `settle_attempts` | `u (exact nonnegative int64)` |
| `refund_attempts` | `u (exact nonnegative int64)` |
| `A` | `u (exact nonnegative int64)` |
| `B` | `u (exact nonnegative int64)` |
| `C` | `u (exact nonnegative int64)` |
| `proof_compared` | `u (exact nonnegative int64)` |
| `proof_certificate` | `u (exact nonnegative int64)` |
| `proof_observed` | `u (exact nonnegative int64)` |
| `requested_eligible` | `u (exact nonnegative int64)` |
| `requested_excluded` | `u (exact nonnegative int64)` |
| `requested_unknown` | `u (exact nonnegative int64)` |
| `eligible_demand` | `null  or  u (exact nonnegative int64)` |

Additional invariant: `_bucket [async_settle_shadow_v2_evidence.py:250]`.

### health.PUBLISHER_OPERATIONS (≤2048 bytes)

| Field | Exact type / nested schema |
|---|---|
| `claim_attempts` | `u (exact nonnegative int64)` |
| `claims` | `u (exact nonnegative int64)` |
| `scans` | `u (exact nonnegative int64)` |
| `publications` | `u (exact nonnegative int64)` |
| `failures` | `u (exact nonnegative int64)` |
| `missed_slots` | `u (exact nonnegative int64)` |
| `cycle_us` | `p1.HISTOGRAM` |
| `rpc_us` | `p1.HISTOGRAM` |
| `late_cycles` | `u (exact nonnegative int64)` |
| `scheduled_slots` | `u (exact nonnegative int64)` |
| `unknown_outcomes` | `u (exact nonnegative int64)` |
| `lost_claims` | `u (exact nonnegative int64)` |

Additional invariant: `extend.<locals>.validate [async_settle_shadow_v2_a1_deltas.py:83]`.

### inline_113 (≤256 bytes)

| Field | Exact type / nested schema |
|---|---|
| `revision` | `ASCII[1..40] /[0-9a-f]{40}/` |
| `region` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `lifetime` | `p1.INTERVAL` |

### inline_114 (≤8192 bytes)

| Field | Exact type / nested schema |
|---|---|
| `router` | `array[0..64, unique=True] of ASCII[1..40] /[0-9a-f]{40}/` |
| `enclave` | `array[0..64, unique=True] of ASCII[1..40] /[0-9a-f]{40}/` |

### p1.FLAG_INTERVAL (≤256 bytes)

| Field | Exact type / nested schema |
|---|---|
| `interval` | `p1.INTERVAL` |
| `admission` | `literal(False,)` |
| `protection` | `literal(False,)` |
| `negotiation` | `literal(False,)` |
| `fast_drain` | `literal(False,)` |

### p1.GAP_INTERVAL (≤256 bytes)

| Field | Exact type / nested schema |
|---|---|
| `interval` | `p1.INTERVAL` |
| `reason` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |

### inline_115 (≤128 bytes)

| Field | Exact type / nested schema |
|---|---|
| `router` | `ASCII[1..40] /[0-9a-f]{40}/` |
| `enclave` | `ASCII[1..40] /[0-9a-f]{40}/` |

### inline_116 (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `router` | `map[≤4096] ASCII[1..64] /[A-Za-z0-9_-]+/ → inline_113` |
| `enclave` | `map[≤4096] ASCII[1..64] /[A-Za-z0-9_-]+/ → inline_113` |

### inline_117 (≤128 bytes)

| Field | Exact type / nested schema |
|---|---|
| `sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `bytes` | `u (exact nonnegative int64)` |

Additional invariant: `<lambda> [async_settle_shadow_v2_evidence.py:371]`.

### p1.INTERVAL (≤128 bytes)

| Field | Exact type / nested schema |
|---|---|
| `start_us` | `u (exact nonnegative int64)` |
| `end_us` | `u (exact nonnegative int64)` |

Additional invariant: `<lambda> [async_settle_shadow_v2_evidence.py:340]`.

### p1.ARTIFACT (≤256 bytes)

| Field | Exact type / nested schema |
|---|---|
| `sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `completed_at_us` | `u (exact nonnegative int64)` |

### a2.ROOT_ENTRY (≤1024 bytes)

| Field | Exact type / nested schema |
|---|---|
| `key` | `ASCII[0..512]` |
| `sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `bytes` | `u (exact nonnegative int64)` |
| `state` | `literal('present', 'missing', 'conflict')` |

Additional invariant: `_coverage_entry [async_settle_shadow_v2_a1_evidence.py:117]`.

### health.HEALTH_BASIS (≤6144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a1',)` |
| `reader_boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `source_state` | `literal('installed', 'unusable', 'empty', 'read_failed')` |
| `body` | `null  or  health.HEALTH` |
| `committed_at_ns` | `null  or  u (exact nonnegative int64)` |
| `read_at_ns` | `null  or  u (exact nonnegative int64)` |
| `read_elapsed_ns` | `u (exact nonnegative int64)` |
| `since_reply_ns` | `u (exact nonnegative int64)` |

Additional invariant: `_basis [async_settle_shadow_v2_a1_health.py:66]`.

### health.PUBLISHER_SLOT_TRACE (≤1024 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a1',)` |
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `slot` | `u (exact nonnegative int64)` |
| `claim_start_us` | `null  or  u (exact nonnegative int64)` |
| `claim_at_ns` | `null  or  u (exact nonnegative int64)` |
| `observed_at_ns` | `null  or  u (exact nonnegative int64)` |
| `publisher_heartbeat_ns` | `null  or  u (exact nonnegative int64)` |
| `ack_at_us` | `null  or  u (exact nonnegative int64)` |
| `cycle_us` | `null  or  u (exact nonnegative int64)` |
| `body_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `outcome` | `literal('missed', 'lost_claim', 'published', 'failed')` |
| `unknown_outcome` | `exact boolean` |
| `late_cycle` | `exact boolean` |
| `rpc_count` | `u (exact nonnegative int64)` |
| `rpc_elapsed_us` | `u (exact nonnegative int64)` |

Additional invariant: `_trace [async_settle_shadow_v2_a1_health.py:101]`.

### p1.RECEIPT_REFERENCE (≤128 bytes)

| Field | Exact type / nested schema |
|---|---|
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `event_seq` | `sequence [async_settle_shadow_v2_schema.py:107]` |

### p1.CERTIFICATE_PROJECTION (≤4096 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `authorization_id` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `generation_id` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `billing_authority` | `literal('local', 'spend_lease', 'regional_lease', 'federated', 'deferred_home')` |
| `journal_region` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `epoch` | `u (exact nonnegative int64)` |
| `settle_origin` | `literal('typed', 'legacy')` |
| `workspace_id_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `key_id_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `invocation_nonce_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `reservation_id_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `iss` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `aud` | `literal('router-shadow-exclusion',)` |
| `iat` | `u (exact nonnegative int64)` |
| `exp` | `u (exact nonnegative int64)` |
| `requested_route` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `requested_streamed` | `exact boolean` |
| `reason` | `literal('app_markup', 'custom_markup', 'custom_model', 'fusion', 'image_cost', 'liberty', 'native_batch', 'non_credits', 'partner', 'polyphemus', 'private_tier_basis', 'receipt_fee', 'request_fee', 'search_cost', 'service_tier', 'settlement_authority', 'tool_cost', 'unknown_parameters', 'unsupported_adapter', 'unsupported_route', 'untyped', 'user_model', 'video_cost')` |
| `facts_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `candidate_set_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `router_revision` | `ASCII[1..40] /[0-9a-f]{40}/` |
| `original_receipt` | `joins.LOCATOR` |
| `async_eligible` | `literal(False,)` |

Additional invariant: `validate_identity [async_settle_shadow_v2_contract.py:81]`.

### p1.AUTHORIZATION_IDENTITY (≤1024 bytes)

| Field | Exact type / nested schema |
|---|---|
| `authorization_id` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `generation_id` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `billing_authority` | `literal('local', 'spend_lease', 'regional_lease', 'federated', 'deferred_home')` |
| `journal_region` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `epoch` | `u (exact nonnegative int64)` |
| `settle_origin` | `literal('typed', 'legacy')` |
| `workspace_id_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `key_id_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `invocation_nonce_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `reservation_id_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |

Additional invariant: `validate_identity [async_settle_shadow_v2_contract.py:81]`.

### p1.DIMENSIONS (≤256 bytes)

| Field | Exact type / nested schema |
|---|---|
| `adapter` | `null  or  literal('openai', 'anthropic', 'other')` |
| `route_type` | `null  or  literal('chat.completions', 'responses', 'other')` |
| `streamed` | `null  or  exact boolean` |

### p1.DECISION_REFERENCE (≤256 bytes)

| Field | Exact type / nested schema |
|---|---|
| `reason` | `null  or  literal('app_markup', 'custom_markup', 'custom_model', 'fusion', 'image_cost', 'liberty', 'native_batch', 'non_credits', 'partner', 'polyphemus', 'private_tier_basis', 'receipt_fee', 'request_fee', 'search_cost', 'service_tier', 'settlement_authority', 'tool_cost', 'unknown_parameters', 'unsupported_adapter', 'unsupported_route', 'untyped', 'user_model', 'video_cost')` |
| `facts_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `candidate_set_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |

### p1.REQUESTED_FACTS (≤2048 bytes)

| Field | Exact type / nested schema |
|---|---|
| `settle_origin` | `literal('typed', 'legacy')` |
| `usage_class` | `literal('Credits', 'BYOK', 'Invoice')` |
| `billing_authority` | `literal('local', 'spend_lease', 'regional_lease', 'federated', 'deferred_home')` |
| `route` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `streamed` | `exact boolean` |
| `service_tier` | `null  or  literal('default', 'priority', 'auto', 'flex')` |
| `app_markup` | `u (exact nonnegative int64)` |
| `custom_markup` | `u (exact nonnegative int64)` |
| `receipt_requested` | `exact boolean` |
| `receipt_fee` | `u (exact nonnegative int64)` |
| `commercial_program` | `literal('ordinary', 'partner')` |
| `features` | `inline_123` |
| `unknown_features` | `array[0..0, unique=False] of ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |

### inline_118 (≤2048 bytes)

| Field | Exact type / nested schema |
|---|---|
| `endpoint_id` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `adapter` | `literal('openai', 'anthropic', 'other')` |
| `features` | `array[0..12, unique=True] of literal('custom_model', 'user_model', 'native_batch', 'fusion', 'polyphemus', 'liberty', 'tool_cost', 'search_cost', 'image_cost', 'video_cost', 'request_fee', 'private_tier_basis')` |

### a2.COORDINATES (≤164 bytes)

| Field | Exact type / nested schema |
|---|---|
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `first_seq` | `sequence [async_settle_shadow_v2_schema.py:107]` |
| `event_seq` | `sequence [async_settle_shadow_v2_schema.py:107]` |

Additional invariant: `<lambda> [async_settle_shadow_v2_schema.py:263]`.

### inline_119 (≤256 bytes)

| Field | Exact type / nested schema |
|---|---|
| `reason` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `required_bytes` | `u (exact nonnegative int64)` |
| `candidate_count` | `u (exact nonnegative int64)` |

### joins.LOCATOR (≤246 bytes)

| Field | Exact type / nested schema |
|---|---|
| `day` | `day [async_settle_shadow_v2_schema.py:95]` |
| `boot` | `ASCII[1..64] /[A-Za-z0-9_-]+/` |
| `first_seq` | `sequence [async_settle_shadow_v2_schema.py:107]` |
| `event_seq` | `sequence [async_settle_shadow_v2_schema.py:107]` |
| `event_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |

Additional invariant: `<lambda> [async_settle_shadow_v2_schema.py:265]`.

### p1.BOOKING (≤512 bytes)

| Field | Exact type / nested schema |
|---|---|
| `outcome` | `literal('settled', 'refunded', 'pending', 'unknown', 'not_attempted')` |
| `reference` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |
| `winner` | `null  or  literal('settle', 'refund')` |

### p1.PREDICTION (≤512 bytes)

| Field | Exact type / nested schema |
|---|---|
| `prediction` | `literal('yes', 'no', 'unknown')` |
| `reason` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `health_source` | `literal('shadow_observation',)` |
| `health_slot` | `null  or  u (exact nonnegative int64)` |
| `prediction_basis` | `literal('counterfactual_backlog_v2',)` |
| `basis_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |

### a2.FILE_REF (≤768 bytes)

| Field | Exact type / nested schema |
|---|---|
| `path` | `_path [async_settle_shadow_v2_a2_evidence.py:469]` |
| `sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `bytes` | `u≤262144` |

### a2.APPROVAL (≤1024 bytes)

| Field | Exact type / nested schema |
|---|---|
| `subject_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `reviewer` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `reviewed_at_us` | `u (exact nonnegative int64)` |
| `decision` | `literal('approved',)` |
| `record` | `a2.FILE_REF` |

### a2.DETAIL (≤512 bytes)

| Field | Exact type / nested schema |
|---|---|
| `root` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `count` | `u (exact nonnegative int64)` |
| `pages` | `u (exact nonnegative int64)` |
| `tail_sha256` | `null  or  ASCII[1..64] /[0-9a-f]{64}/` |

Additional invariant: `<lambda> [async_settle_shadow_v2_a2_evidence.py:409]`.

### a2.DELEGATED_PAYLOAD (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a2',)` |
| `entry_point` | `literal('pilot_enablement.fleet_budgets_pre', 'pilot_enablement.fleet_budgets', 'pilot_enablement.shadow_serving', 'pilot_enablement.pre_flip', 'pilot_enablement.post_flip', 'pilot_enablement.counter_coverage_report', 'pilot_enablement.roster_coverage', 'shadow_report.collect_rollbacks')` |
| `payload` | `a2.FILE_REF` |
| `dependencies` | `a2.DETAIL` |
| `source` | `a2.FILE_REF` |

### a2.LOAD_EVIDENCE (≤262144 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a2',)` |
| `interval` | `p1.INTERVAL` |
| `segments` | `a2.DETAIL` |
| `operations` | `a2.DETAIL` |
| `limits` | `a2.FILE_REF` |
| `approval` | `a2.APPROVAL` |

### a2.EXTERNAL_REQUIREMENT (≤2048 bytes)

| Field | Exact type / nested schema |
|---|---|
| `name` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `stage` | `literal('transport', 'fleet-budgets-pre', 'fleet-budgets', 'shadow-serving', 'pre-flip', 'post-flip')` |
| `router_revision` | `ASCII[1..40] /[0-9a-f]{40}/` |
| `enclave_revision` | `ASCII[1..40] /[0-9a-f]{40}/` |
| `evidence` | `a2.FILE_REF` |
| `approval` | `a2.APPROVAL` |

### inline_120 (≤1024 bytes)

| Field | Exact type / nested schema |
|---|---|
| `name` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `result` | `literal('pass', 'fail')` |
| `evidence` | `a2.FILE_REF` |

### a2.PEAKS (≤4096 bytes)

| Field | Exact type / nested schema |
|---|---|
| `simultaneous_routers` | `null  or  u (exact nonnegative int64)` |
| `simultaneous_publishers` | `null  or  u (exact nonnegative int64)` |
| `configured_routers` | `null  or  u (exact nonnegative int64)` |
| `configured_publishers` | `null  or  u (exact nonnegative int64)` |
| `startup_operations_1s` | `null  or  u (exact nonnegative int64)` |
| `close_operations_1s` | `null  or  u (exact nonnegative int64)` |
| `export_operations_1s` | `null  or  u (exact nonnegative int64)` |
| `reads_per_second` | `null  or  a2.RATIONAL` |
| `pending_rows_per_second` | `null  or  a2.RATIONAL` |
| `trust_rows_per_second` | `null  or  a2.RATIONAL` |
| `health_rows_per_second` | `null  or  a2.RATIONAL` |
| `claim_operations_per_second` | `null  or  a2.RATIONAL` |
| `scan_operations_per_second` | `null  or  a2.RATIONAL` |
| `publication_operations_per_second` | `null  or  a2.RATIONAL` |
| `export_operations_per_second` | `null  or  a2.RATIONAL` |
| `export_bytes_per_second` | `null  or  a2.RATIONAL` |
| `configured_reads_per_second` | `null  or  a2.RATIONAL` |
| `configured_pending_rows_per_second` | `null  or  a2.RATIONAL` |
| `cpu_percent` | `null  or  a2.RATIONAL` |

### a2.RATIONAL (≤128 bytes)

| Field | Exact type / nested schema |
|---|---|
| `numerator` | `u (exact nonnegative int64)` |
| `denominator` | `u (exact nonnegative int64)` |

Additional invariant: `<lambda> [async_settle_shadow_v2_a2_evidence.py:387]`.

### inline_121 (≤2048 bytes)

| Field | Exact type / nested schema |
|---|---|
| `publisher_period` | `u (exact nonnegative int64)` |
| `publisher_jitter` | `u (exact nonnegative int64)` |
| `publication` | `u (exact nonnegative int64)` |
| `poll_period` | `u (exact nonnegative int64)` |
| `poll_jitter` | `u (exact nonnegative int64)` |
| `install` | `u (exact nonnegative int64)` |
| `workspace_period` | `u (exact nonnegative int64)` |
| `workspace_jitter` | `u (exact nonnegative int64)` |
| `workspace_install` | `u (exact nonnegative int64)` |

### inline_122 (≤8192 bytes)

| Field | Exact type / nested schema |
|---|---|
| `region` | `ASCII[1..128] /[A-Za-z0-9_./:@+\-]+/` |
| `approved` | `a2.PEAKS` |

### joins.AUTHORIZATION_OBLIGATION (≤2048 bytes)

| Field | Exact type / nested schema |
|---|---|
| `v` | `literal(2,)` |
| `policy_version` | `literal('shadow-v2',)` |
| `contract_revision` | `literal('a1',)` |
| `original_receipt` | `joins.LOCATOR` |
| `identity_sha256` | `ASCII[1..64] /[0-9a-f]{64}/` |
| `horizon_at_us` | `u (exact nonnegative int64)` |
| `status` | `literal('open', 'terminal_reconciled', 'blocked')` |
| `terminal_reference` | `null  or  joins.LOCATOR` |
| `gap` | `null  or  literal('authorization_link_gap', 'authorization_horizon_gap', 'authorization_terminal_conflict')` |

Additional invariant: `_obligation [async_settle_shadow_v2_a1_reconciliation.py:68]`.

### p1.HISTOGRAM (≤512 bytes)

| Field | Exact type / nested schema |
|---|---|
| `bounds_us` | `array[8..8, unique=False] of u (exact nonnegative int64)` |
| `counts` | `array[9..9, unique=False] of u (exact nonnegative int64)` |

Additional invariant: `<lambda> [async_settle_shadow_v2_evidence.py:262]`.

### inline_123 (≤1024 bytes)

| Field | Exact type / nested schema |
|---|---|
| `custom_model` | `exact boolean` |
| `user_model` | `exact boolean` |
| `native_batch` | `exact boolean` |
| `fusion` | `exact boolean` |
| `polyphemus` | `exact boolean` |
| `liberty` | `exact boolean` |
| `tool_cost` | `exact boolean` |
| `search_cost` | `exact boolean` |
| `image_cost` | `exact boolean` |
| `video_cost` | `exact boolean` |
| `request_fee` | `exact boolean` |
| `private_tier_basis` | `exact boolean` |

## Export execution and remaining work

The test module is `tests/test_async_settle_shadow_v2_live_export.py`. It uses
P3's real ObservationStore with FakeSpannerDatabase and P4a's real Runtime;
no gate evidence generator is imported. Three authorize operations and
settle/refund/settle produce three A/eligible/exact receipts and three samples.
Three scheduled publisher slots all publish; predictions are all yes. The real
allocator writes cap-v2. Real checkpoint/final-close/result writes and both
immutable attachment lanes finish. Original row/object bytes compare exactly
with storage before P5 reads them; every row also passes the unchanged public
row validator. No observed writer-schema defect is asserted from this history.

Test-only collector/operator stand-ins supply a known single-boot independent
roster, configuration, incomplete manifest/proof, approved checkout/public
verifier descriptors, response-captured bindings, independent logical shutdown
measurement and original close references/aliases, conservative handoff bounds,
and the REAL retained collect_rollbacks/report output on an empty prior history.
The external-proof collection is explicitly empty. These are not production
approvals. BudgetMeasurements, BootReadiness and ExpectedCoverageAssembly remain
named seams: this test has no enclave fleet, CPU monitor, pre-opt-in trial,
readiness-criterion CI or forward-horizon inventory to honestly certify. Missing
budgets and readiness are left missing rather than assigned invented passing
measurements or acknowledgements. The incomplete proof uses distinct explicitly
uncollected commitments. G1 rejects them.

`transport` requires only G1 (`async_settle_shadow_v2_a2_contract.py:38`). The rendered envelope is BLOCKED with
exact gaps `artifact_gap`, `evidence_schema_gap`. Assertions also fix every
loader diagnostic and the multiplicity of each original-byte validation detail;
shrinking the gaps later requires intentionally updating these assertions.

Five primary loader diagnostics remain:

| Gap / path / detail | Classification |
|---|---|
| artifact_gap / proof.pre_budget_sha256 / required payload absent | iii: BudgetMeasurements collector |
| artifact_gap / proof.post_budget_sha256 / required payload absent | iii: BudgetMeasurements collector |
| evidence_schema_gap / proof.pre_budget_sha256 / 'proof.pre_budget_sha256' | ii: absent dependency is caught KeyError and misclassified; underlying missing producer iii |
| artifact_gap / proof.external_proofs_sha256 / artifact_gap | iii: empty honest collection cannot establish required CI/trials |
| evidence_schema_gap / proof.resolution_dag_sha256 / evidence_schema_gap | ii: reset_view parses cap-v2 as MANIFEST |

Additional validation details are incomplete proof (iv, deliberately honest),
missing budget preimages (iii), missing expected coverage pages/close coverage
(iii, ExpectedCoverageAssembly), missing prerequisite ledger (iii,
BootReadiness), unverified manifest/proof roots (iii + iv), and unexpected
publisher traces (iii: contender is derived from absent load segments, not from
trace presence). There is no transport-stage claim about passing G2–G11.
No real Go process, HTTP hop, physical Spanner RPC, production CPU or fleet
measurement was exercised; those are class iv limitations. The compared wire
input uses the actual billing evaluator in a test-owned enclave stand-in.

Two explicit diagnostic experiments preserve rather than hide defects:
MissingManifestCapRecovery removes only the test collector manifest; the cap
then makes loader recovery erase the valid proof and render unavailable.
DiagnosticCapIsolation removes only the cap in a separate in-memory diagnostic
copy and proves the original retained reset payload loads successfully. The
exporter itself never filters cap rows, and its output includes every cap byte.

### Export interface and completeness limits

`async_settle_shadow_v2_export.export_day` copies originals through one port with
`rows(kind,day)` and `objects(exact_key)`. The dormant NativePort binds the
bounded row reader and exact object reader through NativeIO.
MemoryPort.from_fake preserves row.body
UTF-8 bytes and labels its metadata **fake_version**, not a server timestamp.
The fake object sink supplies immutable fake generation numbers. The supplied
finite object key plan is explicit; prefix iteration is constrained to that plan,
and an exact planned key selects only that key (not a similarly prefixed sibling).
Rows use bounded ordered day pages (≤200, declared 200 ms LOW/no retry), with an
empty EOF page after exact multiples. A failed source is a gap and later sources
are still attempted. Page tails already fetched at a cap are fully charged.
Before completion, each page is compared with its returned originals: count,
first/last keys, strict cursor, kind, snapshot and successful status. Iterator
cleanup precedes that comparison, including when the cap is reached exactly.

The JSON transport contains original strings and bounded supplemental
`export/{entries,reads,plan,gaps}/*.json` preimages. Each accounting page commits
its predecessor, and export/accounting.json carries exact file refs, counts,
key/body byte totals and source-operation counts. Original sidecars preserve
non-UTF8 bytes that cannot enter P5's string transport. Conflicts are retained;
no latest-write selection or reserialization occurs. Supplemental accounting
proves the declared **captured snapshot ranges**, not independent writer/boot
completeness, not native RPC performance and not the frozen A1 expected coverage
roots. ExportAccountingBinding and ExpectedCoverageAssembly are explicit seams;
G1/G7 do not yet consume this supplemental accounting protocol. The CLI exits 1
on export gaps; callers must not treat a partial transport as complete by
bypassing that status.

Run offline with:

```sh
.venv/bin/python -m scripts.async_settle.shadow_v2_live_export --live \
  --capture capture.json --inputs operator-inputs.json --day 2026-10-12 \
  --completed-at-us 1791763205100000 --bundle transport.json
```

Capture has exact original UTF-8 strings: rows `{kind,key,raw,commit}`,
objects `{key,raw,generation}`, and independently planned `object_keys`. Inputs
are original loader proof/preimage/ledger/prior strings and optional original
collector attachments. Capture mode constructs no credentials. Native mode
requires explicit project, instance, database, bucket, finite object plan and
read-only confirmation before the storage adapter constructs clients. It requests
the supported Spanner `spanner.data` scope and GCS `devstorage.read_only` scope.
The data scope alone does not make reads exclusive; use a read-only Spanner
identity. There is no Spanner read-only OAuth scope or network discovery.
Outputs are exclusive-create, with originals under
`transport.json.sources/originals/`; reruns require a new output path and count
as another pass. No release/deployment integration is added.

MemorySnapshotMaterialization is explicit for fake/capture mode, which
materializes the finite snapshot and assembled transport in RAM. Native mode
spools bounded pages and original bytes to files, reads one fixed snapshot and
records actual calls, received bytes, metadata and failures. NativeIO separates
the pure reader from storage_gcp_shadow_v2_export, which owns SDK clients,
credentials, parameters, snapshots and streams. The library performs no evidence
JSON parse/re-serialization; it serializes only its own transport/accounting.

## MISMATCHES

1. **Loader assumption / design ambiguity: signatures.** Runtime.finish stores
   content-free samples (`services/async_settle_shadow_cohort_runtime.py:584–589`),
   while `gate_views.py:236–274` requires raw tokens for every sample through
   loading/signatures.json. Design §7 says samples have no JWS. No writer emits
   this companion. Smallest routing decision: approve a separate ephemeral
   secure capture source and retention policy, or change the verifier boundary
   to consume the evidence the design actually retains. Export cannot recover a token.
2. **Native row interface.** NativeV2RangeRead implements the registered v2
   primary-key range and exact-key statements through NativeIO, with one fixed
   snapshot, ≤200 rows, 200 ms LOW/no retry. Every delivered key and body is
   retained even when metadata, order, size or deadline checks fail. Pages are
   reconciled with returned originals after iterator cleanup on every exit.
3. **Native object interface.** NativeGenerationRead implements finite exact-key
   reads and retains generation/metageneration through the adapter's bounded
   attachment transport. A returned generation is pinned for subsequent reads;
   it never uses LIST as absence proof.
4. **Scope ambiguity.** Health local/fleet and claim local/publish are mutable
   singleton keys (`v2_evidence.py:397`), not day keys. Daily history must use
   bases/traces. A day-range exporter cannot claim it exported historical health
   singleton versions. Exact current snapshots, if separately supplied, are diagnostics.
5. **Exporter diagnostics ingestion seam.** `gate_loading.py:223–281` accepts no
   export-gap/completeness member, and arbitrary preimages are not semantic
   export coverage. The tool returns nonzero on export gaps even if a
   caller bypasses it and independently evaluates the partial transport. Routing
   fix: bind an approved finite export accounting dependency in G1/G7.

6. **Confirmed loader defect: control kind is not synonymous with MANIFEST.**
   ObservationStore.allocate (`storage_gcp_shadow_observation.py:260`) writes a
   valid cap-v2 in the same kind. `gate_expected.py:95,104` indexes every control
   as a manifest; without the collector manifest this raises router_writers.
   `gate_loader.py:183,185–227` retries the same dependency exception and finally
   discards independently valid proof bytes. Smallest fix: select exact manifest
   keys/schema before building expected populations, and preserve validated
   proof/siblings when downstream adaptation fails. Add this real-cap witness to
   the evaluator owner's tests. No writer change is warranted.
7. **Confirmed loader defect: reset adaptation also assumes control=manifest.**
   `gate_views.py:209–210` runs a1.MANIFEST.parse on cap-v2. The real retained
   callable output is accepted after removing only the cap in a diagnostic copy.
   Smallest fix: same exact manifest selection, leaving cap inventoried.
8. **Loader diagnostic defect.** `gate_loader.py:153–168` catches missing
   dependency KeyErrors and `gate_loading.error_gap:45–47` calls them schema
   failures. A missing pre budget produces both artifact_gap and a quoted-key
   evidence_schema_gap. Smallest fix: classify absence by dependency/path before
   calling the view; do not alter unknown-schema rejection.
9. **D2 timestamp labels (settled).** The row metadata reader with D2 timestamp
   labels records HEALTH_KIND updated_at as store_commit_timestamp and every
   other kind as writer_updated_at. Fixed-snapshot read metadata is separate;
   neither is an acknowledgement. Nanoseconds are retained without float
   conversion. Fake commit version remains explicitly a test version.

## MISSING PRODUCERS

* Independent boot/lifetime/region/pin/attestation collector, including retired,
  killed and non-writing instances; never derive independence from found rows.
* Daily MANIFEST and global PROOF_MANIFEST packager with all F/R/C roots.
* Per-boot prerequisite ledger and six exact-context approved criterion artifacts.
* Pre/post isolated and actual load measurements, segments, CPU, component/RPC
  costs, limits and retained payload/output/dependency packaging.
* Shutdown measurement sources, reviewed SHUTDOWN_INVENTORY and exact aliases.
* Complete expected-key coverage, handoff accounting and forward-horizon
  authorization-obligation projection exports (pure contract helpers exist).
* Reviewed external proof wrappers, stage requirement matrix and release plan.
* Retained v1 original payloads, original outputs/captures and reset/fix history.
* Secure transient signature companion (pending the ambiguity above); P4b
  certificate records are deferred beyond first cohort.
This package includes dormant native row and exact object readers; native mode requires explicit resources and confirmation. Emulator execution and accounting consumption remain separate acceptance work.
