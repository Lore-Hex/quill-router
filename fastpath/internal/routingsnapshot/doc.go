// Package routingsnapshot will read the control plane's signed routing
// snapshot (docs/design/routing-snapshot-v1.md, E2 of
// docs/design/fast-admission-enclave-direct.md). Nothing imports it yet: today
// it holds only the checks that the golden files in fastpath/testdata, which
// src/trusted_router/routing_snapshot.py writes, verify as Go will verify a
// published snapshot: the body's sha256 and size, and the Ed25519 manifest
// over them.
package routingsnapshot
