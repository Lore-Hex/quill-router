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
| `Budget.pre_input` | Approved pre/post BUDGET_ENVELOPE retained payloads and recomputed outputs, their pinned dependencies and monitoring bytes | BUDGET_ENVELOPE; DELEGATED_PAYLOAD; gate_retained.delegate; gate_views.budget_view; release_checks; validate_preimages |
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
| `Budget.close_elapsed_us` | Single verified Shutdown.measurements population | SHUTDOWN_MEASUREMENT; PayloadGraph.materialize_one; gate_views.budget_view |
| `Budget.handoff_elapsed_us` | Single verified Shutdown.measurements population | SHUTDOWN_MEASUREMENT; PayloadGraph.materialize_one; gate_views.budget_view |
| `Budget.grace_us` | Single verified Shutdown.measurements population | SHUTDOWN_MEASUREMENT; PayloadGraph.materialize_one; gate_views.budget_view |
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
| `Budget.projected_load_segments` | Pre/post v2_load segments DETAIL originals and approved limits FILE_REF originals | BUDGET_ENVELOPE; LOAD_SEGMENT; LIMITS/PEAKS; validate_detail; gate_views.budget_view; compare_peak_limits |
| `Budget.peak_limits` | Pre/post v2_load segments DETAIL originals and approved limits FILE_REF originals | BUDGET_ENVELOPE; LOAD_SEGMENT; LIMITS/PEAKS; validate_detail; gate_views.budget_view; compare_peak_limits |
| `ExternalProof.name` | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory | EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.router_revision` | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory | EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.enclave_revision` | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory | EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.raw` | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory | EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.sha256` | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory | EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.passed` | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory | EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `ExternalProof.stage` | EXTERNAL_PROOFS requirement FILE_REF original PROOF_RESULT and independently approved criterion/check inventory | EXTERNAL_PROOFS; PROOF_RESULT; gate_release.validate_proof; validate_preimages |
| `External.proofs` | EXTERNAL_PROOFS original bytes, approved requirements and recomputed retained pilot output | EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.required` | EXTERNAL_PROOFS original bytes, approved requirements and recomputed retained pilot output | EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.drain_selected` | EXTERNAL_PROOFS original bytes, approved requirements and recomputed retained pilot output | EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.signer_checked` | EXTERNAL_PROOFS original bytes, approved requirements and recomputed retained pilot output | EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.attestation_checked` | EXTERNAL_PROOFS original bytes, approved requirements and recomputed retained pilot output | EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.exposure_accepted` | EXTERNAL_PROOFS original bytes, approved requirements and recomputed retained pilot output | EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.post_input` | EXTERNAL_PROOFS original bytes, approved requirements and recomputed retained pilot output | EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `External.required_stages` | EXTERNAL_PROOFS original bytes, approved requirements and recomputed retained pilot output | EXTERNAL_PROOFS; gate_views.external_view; gate_retained.delegate; validate_preimages |
| `ResetSupport.rows` | RESOLUTION_DAG retained original payload, dependency files, recomputed report/rollback results and history support FILE_REFs | RESOLUTION_DAG; RESET_HISTORY_ENTRY; gate_views.reset_view; gate_retained.delegate; validate_preimages |
| `ResetSupport.proof` | RESOLUTION_DAG retained original payload, dependency files, recomputed report/rollback results and history support FILE_REFs | RESOLUTION_DAG; RESET_HISTORY_ENTRY; gate_views.reset_view; gate_retained.delegate; validate_preimages |
| `ResetSupport.resets` | RESOLUTION_DAG retained original payload, dependency files, recomputed report/rollback results and history support FILE_REFs | RESOLUTION_DAG; RESET_HISTORY_ENTRY; gate_views.reset_view; gate_retained.delegate; validate_preimages |
| `ResetSupport.support_files` | RESOLUTION_DAG retained original payload, dependency files, recomputed report/rollback results and history support FILE_REFs | RESOLUTION_DAG; RESET_HISTORY_ENTRY; gate_views.reset_view; gate_retained.delegate; validate_preimages |
| `Signature.object_id` | Approved loading/signatures.json originals, referenced receipt bytes and independently pinned public keys | gate_views.signature_views; binding.verify_binding; certificate.verify_certificate; verify_original_receipt |
| `Signature.token` | Approved loading/signatures.json originals, referenced receipt bytes and independently pinned public keys | gate_views.signature_views; binding.verify_binding; certificate.verify_certificate; verify_original_receipt |
| `Signature.keys` | Approved loading/signatures.json originals, referenced receipt bytes and independently pinned public keys | gate_views.signature_views; binding.verify_binding; certificate.verify_certificate; verify_original_receipt |
| `Signature.now` | Retained signature metadata; verification time comes from the original event, not this redundant value | gate_views.signature_views; gate_accounting event-time receipt verification |
| `Signature.expected` | Approved loading/signatures.json originals, referenced receipt bytes and independently pinned public keys | gate_views.signature_views; binding.verify_binding; certificate.verify_certificate; verify_original_receipt |
| `Signature.attempt` | Approved loading/signatures.json originals, referenced receipt bytes and independently pinned public keys | gate_views.signature_views; binding.verify_binding; certificate.verify_certificate; verify_original_receipt |
| `Signature.server_attempt` | Approved loading/signatures.json originals, referenced receipt bytes and independently pinned public keys | gate_views.signature_views; binding.verify_binding; certificate.verify_certificate; verify_original_receipt |
| `Signature.claims` | Approved loading/signatures.json originals, referenced receipt bytes and independently pinned public keys | gate_views.signature_views; binding.verify_binding; certificate.verify_certificate; verify_original_receipt |
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
| `Bundle.signatures` | Original graph decoded by gate_loader.load; see corresponding type rows in this table | PayloadGraph.materialize; gate_views; validate_original_bytes; validate_preimages |
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
| `Bundle.a2_streams` | Loader-generated diagnostics and materialized original DETAIL streams; diagnostics are not evidence and are never proof preimages | LoadingIssue; GAP_NAME; validate_detail; validate_original_bytes |
| `RetainedResult.output` | Actual retained callable execution on original payload/dependencies through finite file port, with counted calls | gate_retained.delegate; RETAINED_OUTPUT; capture/output byte equality; check_calls |
| `RetainedResult.opened` | Actual retained callable execution on original payload/dependencies through finite file port, with counted calls | gate_retained.delegate; RETAINED_OUTPUT; capture/output byte equality; check_calls |
| `RetainedResult.calls` | Actual retained callable execution on original payload/dependencies through finite file port, with counted calls | gate_retained.delegate; RETAINED_OUTPUT; capture/output byte equality; check_calls |
| `RetainedResult.components` | Actual retained callable execution on original payload/dependencies through finite file port, with counted calls | gate_retained.delegate; RETAINED_OUTPUT; capture/output byte equality; check_calls |
| `ReleasePlan.path` | Independent CLI release-plan bytes, pinned by explicit external digest | read_release_plan; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.raw` | Independent CLI release-plan bytes, pinned by explicit external digest | read_release_plan; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.artifacts` | Independent CLI release-plan bytes, pinned by explicit external digest | read_release_plan; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.criteria` | Independent CLI release-plan bytes, pinned by explicit external digest | read_release_plan; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.verifiers` | Independent CLI release-plan bytes, pinned by explicit external digest | read_release_plan; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.required` | Independent CLI release-plan bytes, pinned by explicit external digest | read_release_plan; FILE_REF; digest; gate_release.release_checks |
| `ReleasePlan.required_stages` | Independent CLI release-plan bytes, pinned by explicit external digest | read_release_plan; FILE_REF; digest; gate_release.release_checks |
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

## P5 test data lifetime and oracle scheduling

The production input fields above are unchanged in round 16. The all-paths decoder
property retains all ten sibling-check lexemes and all twenty agreement-check
lexemes at the same native paths. Its test ids divide the original traversal
into sixteen disjoint ranges for each lexeme; the root remains omitted only
from the sibling check. Per-case timing lives in `.test_durations`. Default checks
collect the complete lexeme/range product, compare its path union with the
unsplit matrix, and require timing entries for every selected window oracle ID,
including loadgroup suffixes. The timing check does not generate entries.

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

The generated boundary family and seven-day family use class-scoped data.
Their large loaded graphs are released when each family completes; the
seven-day family is collected last. The assertions and generated cases are
unchanged.
