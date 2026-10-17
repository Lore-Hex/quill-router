# P5 input provenance and consumption audit

This table is checked against every dataclass field in `gate_inputs.py`, including inherited fields. It describes the production original-byte loader boundary, not permission to construct arbitrary trusted typed views in a caller. Ports and release pins are explicit trust roots. Schema validation alone is not semantic approval; the listed P5 checks bind the original bytes.

All package-owned original-byte JSON decoding passes through `gate_loading.decode_original`: strict standard decoding (duplicate pairs and nonfinite numbers rejected, original byte limit) must agree with the local decoder on values, key order and number types for a clean result. Local issues preserve independently framed siblings. An otherwise clean disagreement adds the fixed root subject `transport/decoder-disagreement` with `evidence_schema_gap`. The internal `DecoderStatus.DISAGREEMENT` marker is separate from member-path tuples and is rendered only by `transport_children`. An ordinary malformed member named `decoder-disagreement` keeps the same single `transport/extra` subject as any unknown member. Default regressions check the exact byte limit and one byte over it at all four loading entry points, with neither decoder called for oversized bytes. The decoder-call inventory test enumerates local scanner calls and the unchanged retained v1 tool exceptions; frozen contract schema parsers retain their own boundary.

Prior gates resolve through their original report, complete DETAIL streams, input leaves and original preimages. `gate_prior._resolve` calls the frozen output/preimage validators, verifies the ledger and release plan, binds revisions/configuration/cohort and pre-budget identity, reconstructs the prior transport through the same loader (including approval-time materialization), evaluates the same stage at the committed completion, and requires exact rendered report-byte equality before supplying that completion. Content-keyed acceptance caching retains original immutable bytes and rechecks current bindings and checkout pins.

Section 8's graph is: transport and pre-enable have no prior gate requirement; fleet requires the matching pre-enable gate; shadow-serving and pre-flip require transport, fleet and matching pre-enable. Fleet's pre-enable predecessor is recursively verified. Every graph verifies its full boot-readiness ledger and ledger artifact provenance under §5.3. No prior shadow-serving gate is required, and its completion never enters the seed maximum. Post-flip delegates its retained pilot prerequisites/outcomes; it adds no recursive gate edge.

| Field | Original source or explicit trust boundary | Validator/binding |
|---|---|---|
| `VerifierApproval.entry_point` | Trusted checkout source, frozen fixture, ordered public key descriptor bytes and independent release pins | gate_verifiers.check_ports; A2 VERIFIER_USE; FILE_REF; gate_release.release_checks |
| `VerifierApproval.function` | Trusted checkout source, frozen fixture, ordered public key descriptor bytes and independent release pins | gate_verifiers.check_ports; A2 VERIFIER_USE; FILE_REF; gate_release.release_checks |
| `VerifierApproval.source` | Trusted checkout source, frozen fixture, ordered public key descriptor bytes and independent release pins | gate_verifiers.check_ports; A2 VERIFIER_USE; FILE_REF; gate_release.release_checks |
| `VerifierApproval.fixture` | Trusted checkout source, frozen fixture, ordered public key descriptor bytes and independent release pins | gate_verifiers.check_ports; A2 VERIFIER_USE; FILE_REF; gate_release.release_checks |
| `VerifierApproval.keyset` | Trusted checkout source, frozen fixture, ordered public key descriptor bytes and independent release pins | gate_verifiers.check_ports; A2 VERIFIER_USE; FILE_REF; gate_release.release_checks |
| `VerifierApproval.keys` | Trusted checkout source, frozen fixture, ordered public key descriptor bytes and independent release pins | gate_verifiers.check_ports; A2 VERIFIER_USE; FILE_REF; gate_release.release_checks |
| `VerifierApproval.source_pin` | Trusted checkout source, frozen fixture, ordered public key descriptor bytes and independent release pins | gate_verifiers.check_ports; A2 VERIFIER_USE; FILE_REF; gate_release.release_checks |
| `VerifierApproval.fixture_pin` | Trusted checkout source, frozen fixture, ordered public key descriptor bytes and independent release pins | gate_verifiers.check_ports; A2 VERIFIER_USE; FILE_REF; gate_release.release_checks |
| `Ports.verify_certificate` | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence | gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.verify_original_receipt` | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence | gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.verify_binding` | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence | gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.comparator_positive` | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence | gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.positive_sample` | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence | gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.known_exclusion` | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence | gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.fleet_budgets_pre` | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence | gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.collect_rollbacks` | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence | gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.report` | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence | gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.post_flip` | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence | gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.approvals` | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence | gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.approved_files` | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence | gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.read_release_file` | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence | gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Ports.roster_coverage` | Trusted tool wiring, independently pinned checkout files; executable callables are trust-root inputs, not exported evidence | gate_verifiers.check_ports/check_calls; gate_release.release_checks |
| `Key.kind` | Original transport row kind/id; validated together with original body bytes | a1.validate_row; ATTACHMENTS schemas; audit_expected |
| `Key.id` | Original transport row kind/id; validated together with original body bytes | a1.validate_row; ATTACHMENTS schemas; audit_expected |
| `Row.key` | Original UTF-8 transport row or attachment bytes, unchanged | a1.validate_row; ATTACHMENTS schemas; A2 validate_preimages |
| `Row.raw` | Original UTF-8 transport row or attachment bytes, unchanged | a1.validate_row; ATTACHMENTS schemas; A2 validate_preimages |
| `Boot.boot` | INDEPENDENT_INVENTORY serving/deployment FILE_REF bytes; contender from LOAD_SEGMENT, termination from SHUTDOWN_ENTRY | INDEPENDENT_INVENTORY; PayloadGraph.inventory; roster_coverage; LOAD_SEGMENT; SHUTDOWN_ENTRY; validate_preimages |
| `Boot.role` | INDEPENDENT_INVENTORY serving/deployment FILE_REF bytes; contender from LOAD_SEGMENT, termination from SHUTDOWN_ENTRY | INDEPENDENT_INVENTORY; PayloadGraph.inventory; roster_coverage; LOAD_SEGMENT; SHUTDOWN_ENTRY; validate_preimages |
| `Boot.revision` | INDEPENDENT_INVENTORY serving/deployment FILE_REF bytes; contender from LOAD_SEGMENT, termination from SHUTDOWN_ENTRY | INDEPENDENT_INVENTORY; PayloadGraph.inventory; roster_coverage; LOAD_SEGMENT; SHUTDOWN_ENTRY; validate_preimages |
| `Boot.region` | INDEPENDENT_INVENTORY serving/deployment FILE_REF bytes; contender from LOAD_SEGMENT, termination from SHUTDOWN_ENTRY | INDEPENDENT_INVENTORY; PayloadGraph.inventory; roster_coverage; LOAD_SEGMENT; SHUTDOWN_ENTRY; validate_preimages |
| `Boot.start_us` | INDEPENDENT_INVENTORY serving/deployment FILE_REF bytes; contender from LOAD_SEGMENT, termination from SHUTDOWN_ENTRY | INDEPENDENT_INVENTORY; PayloadGraph.inventory; roster_coverage; LOAD_SEGMENT; SHUTDOWN_ENTRY; validate_preimages |
| `Boot.end_us` | INDEPENDENT_INVENTORY serving/deployment FILE_REF bytes; contender from LOAD_SEGMENT, termination from SHUTDOWN_ENTRY | INDEPENDENT_INVENTORY; PayloadGraph.inventory; roster_coverage; LOAD_SEGMENT; SHUTDOWN_ENTRY; validate_preimages |
| `Boot.contender` | INDEPENDENT_INVENTORY serving/deployment FILE_REF bytes; contender from LOAD_SEGMENT, termination from SHUTDOWN_ENTRY | INDEPENDENT_INVENTORY; PayloadGraph.inventory; roster_coverage; LOAD_SEGMENT; SHUTDOWN_ENTRY; validate_preimages |
| `Boot.termination_applies` | INDEPENDENT_INVENTORY serving/deployment FILE_REF bytes; contender from LOAD_SEGMENT, termination from SHUTDOWN_ENTRY | INDEPENDENT_INVENTORY; PayloadGraph.inventory; roster_coverage; LOAD_SEGMENT; SHUTDOWN_ENTRY; validate_preimages |
| `Inventory.boots` | All approved dated independent serving/deployment files, merged contiguous lifetimes | PayloadGraph.inventory; gate_roster; validate_preimages |
| `Cohort.workspace_count` | proof.cohort_seed_policy_sha256 configuration FILE_REF and policy original bytes | COHORT_SEED_POLICY; PayloadGraph.cohort; validate_preimages; release_checks |
| `Cohort.regional_seeds` | proof.cohort_seed_policy_sha256 configuration FILE_REF and policy original bytes | COHORT_SEED_POLICY; PayloadGraph.cohort; validate_preimages; release_checks |
| `Cohort.primary_region` | proof.cohort_seed_policy_sha256 configuration FILE_REF and policy original bytes | COHORT_SEED_POLICY; PayloadGraph.cohort; validate_preimages; release_checks |
| `Rate.boot` | Original COUNTER observer/publisher counts and approved post LOAD_SEGMENT intervals | a1.validate_row; LOAD_SEGMENT; validate_publisher_traces; gate_views.budget_view exact count/duration equality |
| `Rate.start_us` | Original COUNTER observer/publisher counts and approved post LOAD_SEGMENT intervals | a1.validate_row; LOAD_SEGMENT; validate_publisher_traces; gate_views.budget_view exact count/duration equality |
| `Rate.end_us` | Original COUNTER observer/publisher counts and approved post LOAD_SEGMENT intervals | a1.validate_row; LOAD_SEGMENT; validate_publisher_traces; gate_views.budget_view exact count/duration equality |
| `Rate.workspace_reads` | Original COUNTER observer/publisher counts and approved post LOAD_SEGMENT intervals | a1.validate_row; LOAD_SEGMENT; validate_publisher_traces; gate_views.budget_view exact count/duration equality |
| `Rate.health_reads` | Original COUNTER observer/publisher counts and approved post LOAD_SEGMENT intervals | a1.validate_row; LOAD_SEGMENT; validate_publisher_traces; gate_views.budget_view exact count/duration equality |
| `Rate.publisher_reads` | Original COUNTER observer/publisher counts and approved post LOAD_SEGMENT intervals | a1.validate_row; LOAD_SEGMENT; validate_publisher_traces; gate_views.budget_view exact count/duration equality |
| `Rate.publisher_writes` | Original COUNTER observer/publisher counts and approved post LOAD_SEGMENT intervals | a1.validate_row; LOAD_SEGMENT; validate_publisher_traces; gate_views.budget_view exact count/duration equality |
| `Rate.burst_reads` | Explicit zero in the original-byte budget adapter; startup operations are independently supplied by PEAKS | gate_views.budget_view; gate_load.measured_peaks and exact approved component limits |
| `Rate.burst_pending` | Original COUNTER observer/publisher counts and approved post LOAD_SEGMENT intervals | a1.validate_row; LOAD_SEGMENT; validate_publisher_traces; gate_views.budget_view exact count/duration equality |
| `Budget.pre_input` | Original dormant production pre BUDGET_ENVELOPE payload/output and deployment/read-health/CPU; qualification-scope.json separately links the isolated trial post envelope | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.regional_caps` | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.fleet_cap` | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.surge` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.publisher_cap` | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.regional_reads` | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.regional_pending` | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.fleet_reads` | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.publisher_reads` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.publisher_writes` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.cpu_peak` | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.cpu_increment` | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.component_seconds` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.rates` | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.pre_completed_us` | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.post_completed_us` | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.pre_digest` | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.post_pre_digest` | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.post_workspace_count` | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.post_regional_caps` | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
| `Budget.export_operations` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.export_bytes` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.close_transactions` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.close_rows` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.close_elapsed_us` | Maximum close_us over the verified Shutdown.measurements population | SHUTDOWN_MEASUREMENT; PayloadGraph.materialize_one; gate_views.budget_view |
| `Budget.handoff_elapsed_us` | Maximum result_us over the verified Shutdown.measurements population | SHUTDOWN_MEASUREMENT; PayloadGraph.materialize_one; gate_views.budget_view |
| `Budget.grace_us` | Minimum reported grace for display; predicates use each measurement and its matching termination entry, never combined maxima | SHUTDOWN_MEASUREMENT; PayloadGraph.materialize_one; gate_views.budget_view |
| `Budget.publisher_unresolved_rows_per_second` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.publisher_control_rows_per_second` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.publisher_transactions_per_second` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.publisher_rpc_per_second` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.metadata_bytes_per_second` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.export_passes` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.export_root_writes` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.export_write_bytes` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.cleanup_spanner_keys` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.cleanup_sink_keys` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.cleanup_seconds` | Approved loading/budget-measurements.json original dependency; component_us converted to seconds | BUDGET_MEASUREMENTS; FILE_REF; gate_views.budget_view; release_checks |
| `Budget.load_segments` | Pre/post v2_load segments DETAIL originals and approved limits FILE_REF originals | BUDGET_ENVELOPE; LOAD_SEGMENT; LIMITS/PEAKS; validate_detail; gate_views.budget_view; compare_peak_limits |
| `Budget.projected_load_segments` | Production pre v2_load projected segments and limits; qualification-scope.json separates trial actuals without changing dormant pins | BUDGET_ENVELOPE; LOAD_SEGMENT; LIMITS/PEAKS; validate_detail; gate_views.budget_view; compare_peak_limits |
| `Budget.peak_limits` | Pre/post v2_load segments DETAIL originals and approved limits FILE_REF originals | BUDGET_ENVELOPE; LOAD_SEGMENT; LIMITS/PEAKS; validate_detail; gate_views.budget_view; compare_peak_limits |
| `ExternalProof.name` | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory | EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.router_revision` | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory | EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.enclave_revision` | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory | EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.raw` | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory | EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.sha256` | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory | EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.passed` | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory | EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.stage` | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory | EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `External.proofs` | Stage-applicable EXTERNAL_PROOFS originals; whole-policy gate_policy.REQUIRED and approved stage check scopes; retained pilot output at money stages | EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.required` | Stage-applicable EXTERNAL_PROOFS originals; whole-policy gate_policy.REQUIRED and approved stage check scopes; retained pilot output at money stages | EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.drain_selected` | Stage-applicable EXTERNAL_PROOFS originals; whole-policy gate_policy.REQUIRED and approved stage check scopes; retained pilot output at money stages | EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.signer_checked` | Stage-applicable EXTERNAL_PROOFS originals; whole-policy gate_policy.REQUIRED and approved stage check scopes; retained pilot output at money stages | EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.attestation_checked` | Stage-applicable EXTERNAL_PROOFS originals; whole-policy gate_policy.REQUIRED and approved stage check scopes; retained pilot output at money stages | EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.exposure_accepted` | Stage-applicable EXTERNAL_PROOFS originals; whole-policy gate_policy.REQUIRED and approved stage check scopes; retained pilot output at money stages | EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.post_input` | Stage-applicable EXTERNAL_PROOFS originals; whole-policy gate_policy.REQUIRED and approved stage check scopes; retained pilot output at money stages | EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.required_stages` | Stage-applicable EXTERNAL_PROOFS originals; whole-policy gate_policy.REQUIRED and approved stage check scopes; retained pilot output at money stages | EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `ResetSupport.rows` | RESOLUTION_DAG retained original payload, dependency files, recomputed report/rollback results and history support FILE_REFs | RESOLUTION_DAG; RESET_HISTORY_ENTRY; gate_views.reset_view; gate_retained.delegate; validate_preimages |
| `ResetSupport.proof` | RESOLUTION_DAG retained original payload, dependency files, recomputed report/rollback results and history support FILE_REFs | RESOLUTION_DAG; RESET_HISTORY_ENTRY; gate_views.reset_view; gate_retained.delegate; validate_preimages |
| `ResetSupport.resets` | RESOLUTION_DAG retained original payload, dependency files, recomputed report/rollback results and history support FILE_REFs | RESOLUTION_DAG; RESET_HISTORY_ENTRY; gate_views.reset_view; gate_retained.delegate; validate_preimages |
| `ResetSupport.support_files` | RESOLUTION_DAG retained original payload, dependency files, recomputed report/rollback results and history support FILE_REFs | RESOLUTION_DAG; RESET_HISTORY_ENTRY; gate_views.reset_view; gate_retained.delegate; validate_preimages |
| `Signature.object_id` | Optional certificate loading/signatures.json originals and ordered approved public keys; none required for compared samples/retries | gate_views.signature_views; certificate.verify_certificate; verify_original_receipt; D1 zero compared binding calls |
| `Signature.token` | Optional certificate loading/signatures.json originals and ordered approved public keys; none required for compared samples/retries | gate_views.signature_views; certificate.verify_certificate; verify_original_receipt; D1 zero compared binding calls |
| `Signature.keys` | Optional certificate loading/signatures.json originals and ordered approved public keys; none required for compared samples/retries | gate_views.signature_views; certificate.verify_certificate; verify_original_receipt; D1 zero compared binding calls |
| `Signature.now` | Optional certificate metadata; certificate verification time comes from the original event | gate_views.signature_views; gate_accounting event-time receipt verification |
| `Signature.expected` | Optional certificate loading/signatures.json originals and ordered approved public keys; none required for compared samples/retries | gate_views.signature_views; certificate.verify_certificate; verify_original_receipt; D1 zero compared binding calls |
| `Signature.attempt` | Optional certificate loading/signatures.json originals and ordered approved public keys; none required for compared samples/retries | gate_views.signature_views; certificate.verify_certificate; verify_original_receipt; D1 zero compared binding calls |
| `Signature.server_attempt` | Optional certificate loading/signatures.json originals and ordered approved public keys; none required for compared samples/retries | gate_views.signature_views; certificate.verify_certificate; verify_original_receipt; D1 zero compared binding calls |
| `Signature.claims` | Optional certificate loading/signatures.json originals and ordered approved public keys; none required for compared samples/retries | gate_views.signature_views; certificate.verify_certificate; verify_original_receipt; D1 zero compared binding calls |
| `Preimage.name` | Original exported path and UTF-8 bytes; conflicting candidates remain separate | FILE_REF; gate_loading.retained_json; validate_preimages; PayloadGraph.file/object/detail |
| `Preimage.raw` | Original exported path and UTF-8 bytes; conflicting candidates remain separate | FILE_REF; gate_loading.retained_json; validate_preimages; PayloadGraph.file/object/detail |
| `Shutdown.entries` | Committed SHUTDOWN_INVENTORY writer detail -> measurement FILE_REF -> approved source original bytes; aliases must match exactly | SHUTDOWN_INVENTORY; SHUTDOWN_ENTRY; SHUTDOWN_MEASUREMENT; source parser; PayloadGraph.materialize_one; decode_bundle alias identity/byte checks |
| `Shutdown.measurements` | Committed SHUTDOWN_INVENTORY writer detail -> measurement FILE_REF -> approved source original bytes; aliases must match exactly | SHUTDOWN_INVENTORY; SHUTDOWN_ENTRY; SHUTDOWN_MEASUREMENT; source parser; PayloadGraph.materialize_one; decode_bundle alias identity/byte checks |
| `Shutdown.aliases` | Committed SHUTDOWN_INVENTORY writer detail -> measurement FILE_REF -> approved source original bytes; aliases must match exactly | SHUTDOWN_INVENTORY; SHUTDOWN_ENTRY; SHUTDOWN_MEASUREMENT; source parser; PayloadGraph.materialize_one; decode_bundle alias identity/byte checks |
| `Bundle.rows` | Original graph decoded by gate_loader.load; see corresponding type rows in this table | gate_loading.transport_values/transport_children (local scalar and element rejection); PayloadGraph.materialize; gate_views; validate_original_bytes; validate_preimages |
| `Bundle.proof` | Original PROOF_MANIFEST and each PREREQUISITE_LEDGER byte string | PROOF_MANIFEST; PREREQUISITE_LEDGER; ledger_preimages; validate_preimages |
| `Bundle.ledgers` | Original PROOF_MANIFEST and each PREREQUISITE_LEDGER byte string | PROOF_MANIFEST; PREREQUISITE_LEDGER; ledger_preimages; validate_preimages |
| `Bundle.attachments` | Original graph decoded by gate_loader.load; see corresponding type rows in this table | gate_loading.transport_values/transport_children (local scalar and element rejection); PayloadGraph.materialize; gate_views; validate_original_bytes; validate_preimages |
| `Bundle.inventory` | Original graph decoded by gate_loader.load; see corresponding type rows in this table | PayloadGraph.materialize; gate_views; validate_original_bytes; validate_preimages |
| `Bundle.cohort` | Original graph decoded by gate_loader.load; see corresponding type rows in this table | PayloadGraph.materialize; gate_views; validate_original_bytes; validate_preimages |
| `Bundle.budget` | Original graph decoded by gate_loader.load; see corresponding type rows in this table | PayloadGraph.materialize; gate_views; validate_original_bytes; validate_preimages |
| `Bundle.external` | Original graph decoded by gate_loader.load; see corresponding type rows in this table | PayloadGraph.materialize; gate_views; validate_original_bytes; validate_preimages |
| `Bundle.reset_support` | Original graph decoded by gate_loader.load; see corresponding type rows in this table | PayloadGraph.materialize; gate_views; validate_original_bytes; validate_preimages |
| `Bundle.signatures` | Optional certificate material; no file or Signature required for compared samples/retries | gate_views.signature_views; gate_compared retained joins; D1 zero offline binding calls |
| `Bundle.transport` | Original envelope plus report, streams, leaves and original graph from finite input inventory | gate_prior._resolve; validate_gate_output; validate_preimages; release_checks; ledger_preimages; exact current binding |
| `Bundle.fleet` | Original envelope plus report, streams, leaves and original graph from finite input inventory | gate_prior._resolve; validate_gate_output; validate_preimages; release_checks; ledger_preimages; exact current binding |
| `Bundle.pre` | Original envelope plus report, streams, leaves and original graph from finite input inventory | gate_prior._resolve; validate_gate_output; validate_preimages; release_checks; ledger_preimages; exact current binding |
| `Bundle.serving` | Optional original envelope, inventoried but never a §8 predecessor or clock authority | a1.GATE_OUTPUT parsing; deliberately excluded from REQUIRED and clock |
| `Bundle.completed_at_us` | Requested transport metadata. Invalid fields produce loading issues; each interval validated for contract representability before selection; scope recovered together from independent inventory, proof rosters or surviving day-manifest lifetimes. Window cannot assert evidence | integer; day; INTERVAL; gate_report.diagnostic_scope/window_for; gates.clock (seed and elapsed are separate from requested observation scope). Completion fallback is a report bound, not a trusted gate timestamp |
| `Bundle.days` | Requested transport metadata. Invalid fields produce loading issues; each interval validated for contract representability before selection; scope recovered together from independent inventory, proof rosters or surviving day-manifest lifetimes. Window cannot assert evidence | integer; day; INTERVAL; gate_report.diagnostic_scope/window_for; gates.clock (seed and elapsed are separate from requested observation scope). Completion fallback is a report bound, not a trusted gate timestamp |
| `Bundle.undefined_preimages` | Loader-generated diagnostics and materialized original DETAIL streams; diagnostics are not evidence and are never proof preimages | LoadingIssue; GAP_NAME; validate_detail; validate_original_bytes |
| `Bundle.preimages` | Original named proof/manifest objects and FILE_REF bytes | gate_loading.transport_values/transport_children (local scalar and element rejection); OBJECT_PREIMAGES schemas; FILE_REF; PayloadGraph; validate_preimages |
| `Bundle.a2_payloads` | Original named proof/manifest objects and FILE_REF bytes | OBJECT_PREIMAGES schemas; FILE_REF; PayloadGraph; validate_preimages |
| `Bundle.shutdown` | Only the loader committed SHUTDOWN_INVENTORY -> measurement -> source population | PayloadGraph.materialize_one; PROOF_MANIFEST shutdown digest binding in load; validate_preimages; decode_bundle alias equality |
| `Bundle.window` | Requested transport metadata. Invalid fields produce loading issues; each interval validated for contract representability before selection; scope recovered together from independent inventory, proof rosters or surviving day-manifest lifetimes. Window cannot assert evidence | integer; day; INTERVAL; gate_report.diagnostic_scope/window_for; gates.clock (seed and elapsed are separate from requested observation scope). Completion fallback is a report bound, not a trusted gate timestamp |
| `Bundle.producer_registrations` | Transport producer_registrations FILE_REF declarations, or the R2 conventional registration after the complete role walk; retained in the structured prior report member | gate_loader.load; gate_dependencies.dependencies; select_registrations; accounting_prefixes; gate_report.build; GATE_REPORT; gate_prior._resolve |
| `Bundle.a2_streams` | Loader-generated diagnostics and materialized original DETAIL streams; diagnostics are not evidence and are never proof preimages | LoadingIssue; GAP_NAME; validate_detail; validate_original_bytes |
| `RetainedResult.output` | Actual retained callable execution on original payload/dependencies through finite file port, with counted calls | gate_retained.delegate; RETAINED_OUTPUT; capture/output byte equality; check_calls |
| `RetainedResult.opened` | Actual retained callable execution on original payload/dependencies through finite file port, with counted calls | gate_retained.delegate; RETAINED_OUTPUT; capture/output byte equality; check_calls |
| `RetainedResult.calls` | Actual retained callable execution on original payload/dependencies through finite file port, with counted calls | gate_retained.delegate; RETAINED_OUTPUT; capture/output byte equality; check_calls |
| `RetainedResult.components` | Actual retained callable execution on original payload/dependencies through finite file port, with counted calls | gate_retained.delegate; RETAINED_OUTPUT; capture/output byte equality; check_calls |
| `ReleasePlan.path` | Independent CLI release-plan bytes, pinned by explicit external digest | read_release_plan; gate_policy.validate_policy; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.raw` | Independent CLI release-plan bytes, pinned by explicit external digest | read_release_plan; gate_policy.validate_policy; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.artifacts` | Independent CLI release-plan bytes, pinned by explicit external digest | read_release_plan; gate_policy.validate_policy; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.criteria` | Independent CLI release-plan bytes, pinned by explicit external digest | read_release_plan; gate_policy.validate_policy; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.verifiers` | Independent CLI release-plan bytes, pinned by explicit external digest | read_release_plan; gate_policy.validate_policy; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.required` | Independent CLI release-plan bytes, pinned by explicit external digest | read_release_plan; gate_policy.validate_policy; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.required_stages` | Independent CLI release-plan bytes, pinned by explicit external digest | read_release_plan; gate_policy.validate_policy; FILE_REF; digest; gate_release.release_checks |
| `LoadingPorts.verify_certificate` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.verify_original_receipt` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.verify_binding` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.comparator_positive` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.positive_sample` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.known_exclusion` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.fleet_budgets_pre` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.collect_rollbacks` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.report` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.post_flip` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.approvals` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.approved_files` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.read_release_file` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.roster_coverage` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.retained` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.gate_roster` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.release` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |
| `LoadingPorts.measurements` | Trusted tool wiring and independently pinned release plan; source-specific parser is an explicit trusted port | gate_verifiers.check_ports; gate_release.release_checks; read_release_plan |

## Limits and allowances

| Parsed input | Consuming comparison |
|---|---|
| Every pre and post LIMITS.PEAKS component, fleet and region | `compare_peak_limits`, iterating `PEAKS.fields`; exact Fraction arithmetic; null makes G7 unavailable |
| Retained workspace count, regional caps and pre/post approval linkage | `budget_view`, `load_budgets`, retained `fleet_budgets_pre` |
| Regional reads/pending, fleet reads, configured surge, CPU peak/increment | `load_budgets` configured and simultaneous comparisons |
| Cohort maximum_publishers and primary_region | `load_budgets` publisher concurrency, role/location, coverage and read/write bounds |
| Every COMPONENTS component_us | `COMPONENT_BOUNDS` and composed 4.25/4.75-second bounds in `load_budgets` |
| ALLOWANCES publisher rows/transactions/metadata and reads/writes | `publisher_bounds`, measured read/write comparisons, metadata formula in `load_budgets` |
| Native publisher RPC allowance | `load_budgets` measured `rpc_peak` comparison |
| Export operation/byte/pass/root/write allowances | `load_budgets` export-plan comparisons |
| Cleanup key counts/seconds | `load_budgets` cleanup cost comparison |
| Close transactions/rows/final/handoff/grace | `load_budgets` separate transaction, two-second and combined grace comparisons |
| Observer failure/streak/degraded budgets and publisher cycle deadline | `observer_health`, `load_budgets` |
| Requested observation window / minimum elapsed | `window_for` derived evidence bound; `clock` continuity and 604800-second comparison |

This audit found the dropped PEAKS limits and the parallel shutdown report population; both now have explicit consumers. Existing legacy binary64 budget semantics remain delegated, rather than being silently reinterpreted.

## Rendering failure audit and remaining seam

The finite review input matrix and the malformed days/window cases are original-byte load/evaluate/render regressions. Scope recovery uses independently loaded inventory even if the manifest row is absent/conflicting. Invalid transport metadata is diagnosed before evaluation; it never provides a successful coverage assertion. Rendering still runs `validate_gate_output` last.

`unavailable_required_report_commitments` returns `UnavailableGateOutput`, never raises it. The conditions are `proof_manifest_absent_or_undecodable` (no exact proof bytes with a decodable ledger commitment) and `observation_scope_artifacts_absent_or_undecodable` (no decoded lifetime from independent inventory, proof roster or day manifest, and no valid selected-day scope). A missing proof takes precedence. Neither condition manufactures a digest or a report. Both tools exit 1 and print exactly one compact JSON diagnostic line, without creating output artifacts. Missing release/approval or other evidence with surviving proof/scope instead renders BLOCKED through the unchanged final validator. A malformed transport sibling is recovered once with a permanent G1 failure; it cannot erase surviving original proof bytes. Missing/invalid release or key files likewise remain BLOCKED evidence inputs; an unreadable bundle has no decodable proof and produces the unavailable diagnostic.

Diagnostic scope can survive failed approval from decoded proof/day lifetimes or the requested calendar days. These fallbacks never establish clock coverage: G10 still requires independent inventory and verified prerequisites. The frozen report still has non-null digests, at least one day and a nonempty window; no contract unavailable form was added. Internal invariant failures are not relabelled as missing artifacts.

Round 12 boundary invariants: `gate_loading.transport_values` frames every native
transport value iteratively (16 container levels); scalar failures and duplicate
keys retain scoped permanent G1 issues without consuming siblings. Original
strings are byte-preserving leaves. `diagnostic_scope` validates each calendar
or lifetime interval before selection. `gate_result.run` is the shared library
and tools boundary: unexpected load/evaluate/render failures yield a fixed
`internal-error` artifact subject and an unevaluable BLOCKED report when original
proof and scope survive, otherwise the applicable proof-first diagnostic.
The optional window constrains containment; it does not replace report scope.
No contract, fixture or pin changes accompany these internal seams.

## A3 role-resolution rules

R1. A file's role comes from its declaration. The transport's optional `producer_registrations` member (complete FILE_REFs, as in your round-12 draft) declares the producer registration(s); a registration's `NativeExport` FILE_REF declares a capture. Every producer of transports in this tree emits the member: the collection script, the bundle composition, and gate_prior's reconstruction of prior transports. The report carries `producer_registrations` as its own checked array of complete FILE_REFs, in the same shape as collection and composition. One original path keeps its original bytes; one transport member keeps its own report field. The member is never materialized at an original path, and prior reconstruction reads it only from the report field, never by decoding an original with a metadata-looking name. Newly emitted reports include the member, including an empty array; the earlier report shape without the member remains readable.
R2. Compatibility fallback, narrowly: when a transport has NO `producer_registrations` member, the registration is the original at the conventional path `stage/producer-captures.json` (or a path ending `/stage/producer-captures.json`) ONLY IF that original has no other declared role anywhere in the input: it is not a member of any prerequisite's or source's source list, no FILE_REF of another role points at it, and the dependency walk has not assigned it a parser. A file with any declared role keeps that role whatever its name. (That is why the r6/r7 support files, declared as byte-only sources, must never be promoted, and why a historical capture, whose registration is undeclared, keeps its scope.)
R3. A declared or fallback-selected registration and its captures undergo the full format check: not a dictionary, no list-valued `passes`, a pass missing a required key, an unreadable NativeExport target: each is `evidence_schema_gap` at that path, never a silent `continue` with an empty prefix (P1-16).
R4. No other role is ever derived from a path spelling. The `sources/measurement/` mapping in gate_release applies only to files declared as SHUTDOWN_MEASUREMENT sources; a file reached solely through an ordinary source list keeps `original_bytes` even under that prefix (P1-17; the mapping predates this package, you fix its application in the dependency walk, not the release inventory format).
R5. Byte-only originals are never parsed for content, in any code path, including scope discovery.

## P5 test data lifetime and oracle scheduling

Round 20: CI run 38838857421 stopped in `live_objects()` at `gc.collect()`,
called by `_p5_cache_counts` during setup of a worker's first P5 module after
about 260 unrelated tests, including Spanner SDK tests. The initial native-finalizer
explanation was incorrect; round 21 below records the heap growth that made the
collection slow. Live-object counts and their unchanged 35,000-object margins
were restricted to sessions where every selected item belongs to
`PACKAGE_MODULES`. At that point mixed sessions printed why that check was
skipped; module data release, restored lookup identities, and the unchanged
5,000-object reachable margin still ran. The reference walk uses
`gc.get_referents()` and does not call `gc.collect()`. The signal timeout marks
remain unchanged.
A separate covered step in the `spanner-emulator` job runs only the eight
package modules with `-m "not proof_oracle" -n 2`, in fresh workers, with a
five-minute limit. Its partial coverage has no floor; the full-suite combined
coverage gate still requires 70%.

Round 21: CI run 38844053421 stopped both shard-4 selections at 37 percent,
where P5 modules followed about 260 unrelated tests. The single-process covered
replay was killed with exit 137 at a P5 module boundary. `reachable_objects()`
walked from `trusted_router` module roots into the shared worker's application
heap; its pending list and visited dictionary retained the reached objects,
with edges appended before deduplication, growing memory before the visited-object
ceiling could stop the walk. That heap growth also explains round 20's slow
collection, rather than a native finalizer. Mixed sessions now skip both
reachable-object and live-object measurements with one line naming both;
they still check released data and restored lookup identities. Package-only
sessions, including the separate CI step, retain all four checks with the
same 5,000 reachable-object and 35,000 live-object margins. `_p5_cache_lifetime`
and `release_data_caches` only clear known caches and containers and inspect
cache sizes; neither walks the heap or requests collection in the mixed path.
Regression tests make both measurements, the frozen-package walk, and explicit
GC calls raise in mixed sessions while exercising setup and real data release.

The production input fields above are unchanged in round 16. The all-paths decoder
property retains all ten sibling-check lexemes and all twenty agreement-check
lexemes at the same native paths. Its test ids divide the original traversal
into sixteen disjoint ranges for each lexeme; the root remains omitted only
from the sibling check. Per-case timing lives in `.test_durations`. Default checks
collect the complete lexeme/range product, compare its path union with the
unsplit matrix, and require timing entries for every selected window oracle ID,
including loadgroup suffixes. The timing check does not generate entries.

Round 19 adds estimated `.test_durations` entries for every previously unrecorded
test ID marked `conformance-spanner-emulator`, `conformance-spanner-pg`, or
`conformance-postgres`, including loadgroup suffixes, in the union of collections
with `-m "not provider_health and not proof_oracle"` on the default clock and
`TR_LIFECYCLE_CLOCK_OVERRIDE=2027-02-05T00:00:00Z`. The weights are 25.0 seconds
for each Spanner backend and 6.0 seconds for Postgres. These conservative
estimates derive from the CI timelines of runs 38809305127 and 38820979824,
using tail seconds divided by pinned-group size; the latter run's emulator
tails imply about 14.7 seconds per ID (24.5 minutes / 100) and 16.9 seconds per
ID (20 minutes / 71). These weights estimate serial backend work for shard
assignment. Existing entries remain unchanged; replace these estimates with
CI-recorded durations once the shards upload them.

| Test data | Lifetime check |
|---|---|
| Parsed variants, last transport, typed ports, prior results and entry-digest inputs | `_p5_cache_lifetime` clears these at module teardown; the separate `_p5_cache_counts` checks the remaining objects and restored functions |
| Fixture byte tables and original prior chain | Shared only through test-module holders; included in the unchanged 35,000-object check. The seven-day family still performs a full release after its large graph |
| Boundary loaded graphs and evaluated results | `BoundaryCache.installed` clears both tables after restoring the original functions |
| Schema, validation, original-byte, root and inventory lookup tables | Existing module fixtures clear their tables and restore function identities before the shared lifetime check |

For the default selection, the shared check allows at most 35,000 additional live objects within a module,
and at most 35,000 above the count before the first package
module in each process, for reports and first-use imports. The one-process follow-on checks
exercise the frame-cache cases and the original format-checking routes after
all eight modules. The existing subprocess check remains selected by the
oracle job. A selection containing oracle pieces still performs a full data-cache
release and records counts, but does not apply the default selection’s 35,000-object
margin while long-family graphs and teardown reports remain live. Its CI time cap
is unchanged.
The baseline is taken after importing the six common function-fixture support
modules (fake database helpers and the shared shadow-clock modules), before any
P5 fixture data is built. This excludes permanent first-use support imports from
the data-lifetime measurement without increasing its fixed allowance.

The final default cache-lifetime case leaves 40,000 nested entries in the
transport helper cache and 12,000 distinct scalar strings in the prior-result
table. The release fixture clears both. A separate module-scoped
checker observes teardown even if the release fixture is removed. Round 16 adds a
default subprocess that runs this sentinel and its module teardown; scratch
fixture-removal verification is recorded in the round report. The checker also
counts the module-rooted reference walk before and after each package module,
with a fixed 5,000-object allowance, and checks restored lookup identities and
empty transient tables. The existing 35,000 live-object allowances remain.
The digest table and prior results are released in the default selection too;
shared original bytes remain only in test-module holders.

The window validation fixture imports every `async_settle_shadow_v2_gate*.py`
module by filename before snapshotting and replacing digest aliases. Its undo
therefore restores aliases regardless of which modules the selection initially
loaded. The fixture then checks every loaded gate module for its temporary
wrapper; the separate module checker still compares every digest alias with
the restored roots function. A fresh-process default check runs one window
case and both teardowns with no earlier package-test collection.

The generated boundary family and seven-day family use class-scoped data.
Their large loaded graphs are released when each family completes; the
seven-day family is collected last. The assertions and generated cases are
unchanged.

### A3 round 1 source boundaries

All control populations use `gate_controls.manifests`: exact day/manifest-v2
key plus the frozen MANIFEST schema. CAP and RETENTION retain their original
bytes in `Bundle.rows` and the input inventory and use their own frozen schemas.
Unknown control subtypes produce a row-scoped schema failure.

`Bundle.signatures` and every `Signature.*` field are optional certificate
material. Compared samples and retries have no signature-file dependency and
no offline binding invocation. Both approved callables and ordered public key
sets remain required. `gate_compared` joins retained original sample/terminal
coordinates, deployment, authorization, payload/snapshot commitments,
classification and booking; independent accounting keeps every retry attempt
and its own prediction. Actual offline compared binding calls are zero.

`External.required`, `External.required_stages`, `ReleasePlan.required` and
`ReleasePlan.required_stages` are constrained by `gate_policy.REQUIRED`, derived
from sections 8, 11 and 15.6. Release criteria can add checks; stage-specific
shadow/money check scopes cannot substitute for each other. Loading receives the
requested gate, including recursive predecessor loading. Future requirement
objects may be absent; their names remain in the whole-policy release inventory.
The frozen original envelope slots, including linked pre/post budgets, remain
mandatory. View construction is limited to applicable rules.

For `Budget.pre_input`, `Budget.projected_load_segments`, `Budget.load_segments`
and `Bundle.inventory`, pre-enable separates the unchanged retained dormant
production deployment/read-health/CPU projection from the closed isolated
qualification trial's enabled writers and post envelope. The independently
approved supplemental `loading/qualification-scope.json` identifies distinct
production/trial resources, the exact production deployment FILE_REF, the trial
INDEPENDENT_INVENTORY FILE_REFs and both canonical envelope digests. It is
inventoried original evidence, not a frozen contract extension or seed. Missing
or invalid scope records keep the pre-enable seam closed. The unchanged
`validate_preimages` still validates both envelopes and their link.

`Cohort.regional_seeds` uses the reviewed COHORT_SEED_POLICY.required_regions
subset plus reconciled terminal regions. `Inventory.boots`, load segments and
capacity sweeps retain every enabled writer, including idle regions. An unknown
terminal writer or an unreviewed booking region blocks.

`Bundle.a2_streams.loading_issues` reports one primary diagnostic per dependency
path for the six required proof roots: absent originals are artifact failures,
present malformed schema bytes are schema failures. Nested FILE_REF originals now use the declared format parser before byte/hash
comparison, as described in Round 3 below. Diagnostics retain original paths and
do not expose exception text. Section 15.4 multi-bundle loading and declared
export-accounting consumption are implemented; full-day validation capacity
remains the named open seam below.

## Round 2 file checks and shutdown accounting

The nested dependency inventory records original paths and expected parsers before FILE_REF byte-count and digest checks. Missing bytes produce artifact_gap; present malformed bytes produce evidence_schema_gap. Diagnostics are deduplicated by original path. Page chains are resolved through their decoded digest or the approved original inventory, and prerequisite source files and retained-output descriptors are included. No optional future file becomes required merely because it is listed in the release plan.

Downstream view errors retain the original proof and sibling bytes. Each view uses the fixed diagnostic label at its original subject. Outer recovery lists a named failure for every required rule it marks unevaluable and validates its report with the unchanged output contract.

Shutdown final_pass_us and preparation_us + export_wait_us + final_pass_us remain in Shutdown.measurements. Each measurement checks close_us and result_us separately against 2,000,000 us, their sum against final_pass_us, final_pass_us against 4,000,000 us, and the total against min(the matching writer's remaining grace, 9,000,000 us). Existing original close-result checks and transaction/row counts remain applicable. Aggregate maxima never borrow another writer's grace.

The transport boundary oracle follows the applicable descriptor closure, including prerequisite files and mandatory retained-output descriptors. Unused original reviews and sources may be absent or relocated; present declared bytes retain their byte/digest checks. Round 3 below supersedes the round-2 export-accounting and combined transport-count limitations.

## Round 3 file formats, original deadlines and input plans

| Input | Original source | Consumer |
|---|---|---|
| Serving and deployment rosters | Independently declared FILE_REF originals | `gate_dependencies.serving_roster` / `deployment_roster`, retained roster checks, then byte count and digest |
| Typed nested files | Their declared schema or producer parser | Dependency parser before FILE_REF comparison |
| Support files without a declared format | Original bytes | FILE_REF byte count and digest; no added JSON requirement |
| Original remaining shutdown deadline | Matching day/boot CLOSE_RESULT `shutdown_remaining_us`, captured at original attempt start | `gate_load.load_budgets`: original elapsed time and the following result transaction must fit; preparation and wait are already spent at this point |
| Shutdown scalar phases | Matching SHUTDOWN_MEASUREMENT | Separate close/result ceilings, their sum contained in final_pass, preparation + wait + final bounded by the writer's reported grace and nine seconds |
| Declared export passes | Deployment original `export_accounting: DETAIL` and every source page/original it names | `gate_native_accounting.consume` invokes the shared `pack_accounting_stream` parser during loading; G7 consumes the actual native RPC totals from all declared passes |
| Multi-part captured input | C2 `shadow-v2-captured-transport-1` descriptor and its `shadow-v2-pack-plan-1` input inventory | `gate_plan.PlanInput`: every leaf, listed part, predecessor, length and digest checked before one stage evaluation; individual file/page bounds retained |

Scalar shutdown sources do not contain monotonic phase endpoints. Duration sums
check the recorded sequential budget; they cannot establish exact endpoint
containment, overlap, idle gaps or actual phase order. `ScalarShutdownOrder`
names that missing endpoint evidence. Wall timestamps do not fill it.

`FrozenDayValidationCapacity` remains a separate loader seam. The bounded input
leaf reader does not make the frozen exact-dict preimage validator or the gate
context stream a whole day. A complete bounded adapter and a traced end-to-end
day validation are still required. No whole-day memory claim follows from the
leaf-reader test.

## Round 6 shared file checks and oracle groups

The bundle composition failure reproduced both after gates modules and alone: the added A3 dependency walk treated export-spool-relative page paths as graph-relative paths during shared `PayloadGraph.materialize`. Gate loading now calls `materialize_gate` for dependency and stage checks, while shared assembly retains its original file scope; release-plan decoding checks the file format, with required gate policy still checked by `release_checks` during current and prior loading. Cache cleanup was not the cause and remains limited to gates-module teardown. A scratch comparison produces 539 byte-identical assembly files with main’s materializer; the bundle’s saved gate-result comparisons still differ under A3 and remain unresolved without changing those expectations or undoing A3 behavior. Four-group CI runs 38924732345 took 43 to 45+ minutes with 1,252 window oracle cases; six groups per clock reduce estimated single-worker group work to about two thirds while retaining the 45-minute cap. The complete selection also includes 338 frozen-main cases (1,590 total); the installed least-duration splitter estimates maximum/mean group weights of 6,250.625/6,250.574 seconds for four groups and 4,167.104/4,167.050 seconds for six on both clocks, using the existing duration file and its mean fallback for the 338 unmeasured cases.
