// Package routingsnapshot reads and evaluates the control plane's signed
// routing snapshot (docs/design/routing-snapshot-v1.md, E2 of
// docs/design/fast-admission-enclave-direct.md).
//
//   - Verify checks a manifest the way routing_snapshot_signing.verify_manifest
//     does (Ed25519 under an accepted kid, the public test key only in test
//     mode, the claims bound to the sha256 and size of the bytes received,
//     the verifier's own plane and audience, freshness) and decodes the body
//     strictly (Parse).
//   - Snapshot.Evaluate answers one request as authorize_candidates'
//     plan_route, resolve_candidates and finish_candidates would with no
//     health re-rank: the candidate endpoint ids in order and the model
//     estimate in microdollars, or Python's refusal. A request outside the
//     cohort the snapshot and the golden routing cases cover is passed to
//     Python with its reason, never guessed at.
//   - Cost is stage_d's frozen-candidate pricing, exact to the microdollar.
//
// The tests hold it to fastpath/testdata/routing_cases_v1.json and
// tests/fixtures/usage_cost_vectors.json, which Python generates. At runtime
// only the edge imports it, to poll the snapshot and compare Go's answers
// with Python's in shadow (fastpath/internal/edge); nothing decides with it.
package routingsnapshot
