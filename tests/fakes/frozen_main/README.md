# Frozen-main package snapshot

BASE: `4701b1a6da9b2b05df93321a6bbd59ea82188c2b`.

This oracle is a **golden against BASE**. For tests-only F1, the live `src` tree
is byte-for-byte that main parent of the Round-12 merge. A PR that intentionally
changes the legacy path **re-freezes from its own tree in the same PR**, and the
reviewer reads the frozen diff as the intended behavior change.

Re-pin from the repository root:

```bash
python scripts/async_settle/freeze_reference.py --base <commit>
```

Paste the printed archive SHA-256 into `../frozen_package.py`, update this BASE
statement and the design appendix, and regenerate the execution inventory.
`--base` uses read-only Git object commands: `git ls-tree -r --name-only <commit>
-- src/trusted_router` and `git show <commit>:<path>`. It never reads working-tree
source bytes. Tar members are sorted by repository path, with mode 0644, mtime 0,
uid/gid 0 and empty owner names. Gzip has an empty filename and mtime 0. The
script writes `BASE`, `pins.json` and `package.tar.gz` deterministically.

The local gate runs:

```bash
python scripts/async_settle/freeze_reference.py --check
```

This re-derives the archive and pins from the commit recorded in `BASE`, and
fails on any byte difference in these three files. CI's shallow checkout cannot
run `--check`: BASE's Git objects need not exist there. Test execution needs no
Git objects or network access.

The snapshot includes all 405 Python files plus JSON, JSONL, HTML, TXT, SQL, CSS
and JavaScript resources under BASE's `src/trusted_router` (753 files total).
Binary static media are omitted because the tested requests do not need them.
The member-selection policy and repository-path layout are unchanged.

`pins.json` hashes unchanged file bytes. The loader independently pins the archive
SHA-256, checks the exact member set and every digest, and verifies source bytes
again before execution. It extracts outside the repository and imports as
`frozen_main`. Absolute application imports are redirected there; relative
imports already resolve there. Missing imports/resources never fall back to the
live tree. The import, reference and execution guards remain independent of the
observed inventory.

To regenerate execution evidence, set `FROZEN_MAIN_INVENTORY_DIR` to an empty
temporary directory while running `tests/test_async_settle_proof_oracle.py`.
Each process records a JSON set of `(module, qualname, code_firstlineno, member,
sha256)` rows. Union and sort these files, emitting one record per row with keys
`module`, `qualname`, `code_firstlineno`, `frozen_member`, `sha256`, plus
`frozen_module` (replace the `trusted_router` prefix with `frozen_main`). Record
BASE, archive digest, module count, unique `(module, qualname)` count and row
count in `execution-inventory.json`; group the same records by module in
`docs/async-settle-frozen-main-inventory.md`.

Both snapshot setup and HTTP/drain/state-capture execution are guarded and
recorded. Fixture seed preparation and intentional live negative controls are
excluded. Generated dataclass methods belong to the owning class/file; their
code line is the generated function's line, not a literal source line. Module
and class bodies and comprehensions are retained. This is evidence, **not an
allowlist**: any live router call is rejected regardless of the inventory.

## CI selection and frame inspection

`proof_oracle` marks the 336 complete-entry comparisons and the two protected
header comparisons. CI runs them without coverage in the `proof-oracle` job,
with four xdist workers and a 45-minute limit, once for each lifecycle clock.
Its post-cutover clock uses the same computation as `test-post-cutover`.
Both ordinary shard jobs deselect this marker; guards, witnesses and lock-order
tests remain in those shards. The dedicated job has only `contents: read`
permission and does not authenticate to GCP. Collection checks every marked
item against the files in the parsed workflow before deselection or sharding.

On CPython 3.11/3.12, inspecting `frame.f_locals` can invoke a user key's equality
method while synchronizing fast locals. Reference inspection never reads it.
Finished frames expose their locals through native GC traversal. Suspended
frames require their generator/coroutine/async-generator owner to be reachable
in the same bounded walk, where native GC exposes those locals. Frames without
such a native path fail closed with an explicit `opaque frame` reason. Owners
may appear before or after their frames; unresolved frames are checked at the
end of the walk. A code object held only by `f_trace` does not prove native
locals traversal: that independently traversed edge is discounted. No
interpreter-stack or global owner search is used.

On 3.13+, the sealed native frame-locals proxy supplement remains. Every native
GC edge and the existing module/process registry identity boundaries remain in
both implementations. The colliding-key regression checks direct reference
inspection and the full execution guard, with and without a reachable owner;
on old CPython its removal and unsafe-materialization mutations must fail.

The scalar fast path applies only to exact `str`, `int`, `float`, `bytes`,
`bool` and `NoneType` objects. The walk still counts and yields each object and
retains every `gc.get_referents` edge before skipping inapplicable supplemental
field checks. Their provenance is always `builtins`; subclasses keep the full
metadata and reference path. This introduces no cache or state shared between
cases. Synthetic-edge and subclass controls, with removal mutations, protect
both boundaries of the shortcut.
