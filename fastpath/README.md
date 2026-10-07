# fastpath

The Go service of the fast-admission design
(`docs/design/fast-admission-and-batched-settlement.md`), built first as the
spike its rollout's step 5 describes (`docs/design/fast-admission-spike.md`).

## Layout

- `internal/leaselifecycle`, `internal/terminalorder` and
  `internal/auditorcommit`: the shadows of `proofs/LeaseLifecycle.tla`,
  `proofs/TerminalOrder.tla` and `proofs/AuditorCommit.tla`. Each action of a
  spec is a function under the spec's name, and each invariant a method.
  `proofs/manifest.toml` names each spec's package and tests.
- `schema/spike.sql`: the spike's schema (`docs/design/fast-admission-spike.md`,
  §4), and the package that splits it into statements.
- `internal/store`: the spike's Spanner store, each operation's guards in the
  transaction that writes. `internal/store/storetest` runs its tests against
  the Spanner emulator.
- `internal/tlc`: for tests only. It runs the pinned TLC from `proofs/`, reads
  the state graph TLC writes with `-dump dot,actionlabels` as it streams, has
  TLC judge whether a `.cfg` has the constants a test declares, and reads the
  `[states]` of a guard table.

## How a shadow is held to its spec

- TLC writes the whole state graph of an instance, and the test checks that
  from every state the shadow takes the same actions to the same states. The
  tests do this for small instances and for every configuration `proofs/`
  checks.
- The tests declare each configuration in Go rather than read its `.cfg`,
  and TLC binds each declaration to its file (`tlc.BindConfiguration`). TLC's
  own parser reads the file, which may assign the spec's constants and list
  invariants and properties, and nothing else that changes the graph TLC
  explores: no override of a definition, constraint, symmetry or view. TLC
  then evaluates the declaration as an assumption against the file. The
  shadow must also reach the count of distinct states in the spec's guard
  table (`[states]` in `proofs/<Spec>.guards.toml`).
- Random walks on a larger instance check every invariant and step property.
- Controls: a test changes the shadow by one step and checks that the
  comparison notices, and a declaration one constant off is refused.

## Running the tests

They run TLC, so they need Java as well as Go, as CI's `fastpath` job has:

```bash
cd fastpath && go test ./...
```

The store's tests run against the Spanner emulator, as CI's
`fastpath-spanner` job does, at the image it pins. Without
`FASTPATH_SPANNER_EMULATOR=1` they skip; with it, an emulator they cannot reach
fails them. They create an instance of their own and delete it when they end.
The emulator runs one read-write transaction at a time, so run them one
package at a time:

```bash
docker run -d --rm --name fastpath-spanner -p 127.0.0.1:9010:9010 -p 127.0.0.1:9020:9020 gcr.io/cloud-spanner-emulator/emulator@sha256:c6f3402f2599684f295a0fdefb6fbbbfb18a0e43e309ff5456ccb452a4570a79
```

```bash
cd fastpath && FASTPATH_SPANNER_EMULATOR=1 SPANNER_EMULATOR_HOST=127.0.0.1:9010 go test -race -count=1 -p 1 ./internal/store/...
```

The module stays on Go 1.24, the enclave's, and on `cloud.google.com/go/spanner`
v1.88.0, its last release for Go 1.24; CI sets `GOTOOLCHAIN=local`, so a
dependency that needs a later Go fails the build.

The whole-graph comparisons are in files built only without the race
detector (`//go:build !race`): they are single-threaded and some ten times
slower under it. CI runs `go test -race ./...` without them and `go test
./...` with them. A large instance's graph is read as it streams
(`tlc.Compare`), and TLC runs with a 1 GB heap. AuditorCommit's take about
five minutes on a laptop and longer on a CI runner, past Go's default timeout
of ten minutes for a package's tests, so CI passes `-timeout 30m`, as a slower
machine should too.
