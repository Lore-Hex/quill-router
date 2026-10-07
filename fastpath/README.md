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

The whole-graph comparisons are in files built only without the race
detector (`//go:build !race`): they are single-threaded and some ten times
slower under it. CI runs `go test -race ./...` without them and `go test
./...` with them. A large instance's graph is read as it streams
(`tlc.Compare`), and TLC runs with a 1 GB heap.
