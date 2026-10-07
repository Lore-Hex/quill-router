# fastpath

The Go service of the fast-admission design
(`docs/design/fast-admission-and-batched-settlement.md`), built first as the
spike its rollout's step 5 describes (`docs/design/fast-admission-spike.md`).

## Layout

- `internal/leaselifecycle` and `internal/terminalorder`: the shadows of
  `proofs/LeaseLifecycle.tla` and `proofs/TerminalOrder.tla`. Each action of a
  spec is a function under the spec's name, and each invariant a method.
  `AuditorCommit` gets a package of its own (`internal/auditorcommit`) when it
  is written; `proofs/manifest.toml` names each spec's package and tests.
- `internal/tlc`: for tests only. It runs the pinned TLC from `proofs/`, reads
  the state graph TLC writes with `-dump dot,actionlabels`, and reads a
  spec's `.cfg` and the `[states]` of its guard table.

## How a shadow is held to its spec

- On a small instance, TLC writes its whole state graph, and the test checks
  that from every state the shadow takes the same actions to the same states.
- On the instance the spec's `.cfg` checks, the shadow must reach as many
  distinct states as TLC counted (`[states]` in `proofs/<Spec>.guards.toml`).
- Random walks on a larger instance check every invariant and step property.
- A test changes the shadow by one step and checks that the comparison
  notices.

## Running the tests

They run TLC, so they need Java as well as Go, as CI's `fastpath` job has:

```bash
cd fastpath && go test ./...
```

The whole-graph comparisons are in files built only without the race
detector (`//go:build !race`): they are single-threaded and some ten times
slower under it. CI runs `go test -race ./...` without them and `go test
./...` with them. A large instance's graph is read as it streams
(`tlc.Compare`), and TLC runs with a 1 GB heap.
