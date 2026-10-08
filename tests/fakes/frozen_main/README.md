# Frozen-main package snapshot

BASE: `4701b1a6da9b2b05df93321a6bbd59ea82188c2b`.

This oracle is a **golden against BASE**. The worktree includes later merged main changes; this tests-only round leaves
production source and the pinned BASE unchanged. A PR that intentionally
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
with four shards per lifecycle clock, four xdist workers per shard and a
45-minute limit per job.
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

On 3.13+, the sealed native frame-locals proxy supplement follows keys and
values. Non-optimized running exec/class/module frames instead return a plain
dict: an exact dict is queued into the normal bounded native walk. A custom
locals mapping requires a demonstrated native GC identity edge from the frame
or fails closed; no user mapping protocol is invoked. Every native
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


## Execution interpreter contract

The frozen execution proof requires **CPython 3.12+**. On CPython 3.11 it raises
`unsupported interpreter: frozen execution_guard requires CPython 3.12+; CPython 3.11 profile/trace trampolines materialize unsafe frame locals`
before preflight, installing hooks, starting workers, or entering the leg. This
is a proof-harness restriction, not a change to the application's Python support.
The 3.11 test branches assert this exact rejection; they do not claim to have
run the frozen leg. Independent reference-walk checks still run on 3.11.

On **3.12 (CI: 3.12.3)** the guard reserves `sys.monitoring` tool ID 4 and uses
`PY_START`, `PY_RESUME`, `PY_THROW` and builtin `CALL` events. This is an
interpreter-wide fence, including raw workers and native exception cleanup.
It never installs a Python `sys.setprofile`/`settrace` callback: their CPython
trampoline itself synchronizes previously materialized locals dictionaries,
even when the Python callback does not read locals. An occupied tool ID rejects
entry without replacing its owner. Exit unregisters callbacks and frees the ID.
On **3.13/3.14**, PEP 667 removed that synchronization; the existing all-thread,
default-thread and raw-bootstrap profile paths remain.

Both callbacks use only sealed native frame/code fields, native builtin and
module namespace descriptors, native dict item iteration with `str.__eq__`,
and metadata normalized to exact strings with `str.__str__` before hashing
or formatting.
Module lookup, filename classification, attribution and worker admission invoke
no user metadata protocols; worker code admission uses integer identities.
Snapshot paths use native string prefix/slicing operations, never path protocols.
Builtin qualnames combine native name/owner slots with the native type qualname
slot; the builtin `__qualname__` getter can itself invoke a metaclass. Native
GC edges on the exact builtin base type retain static owners hidden by `__self__`.
**Neither reads `f_locals`, obtains a locals proxy, nor inspects
the Python local `self` on any interpreter.** Generated dataclass attribution
uses a strong code-identity registry populated after frozen module execution and at guard
entry for loaded router modules. Native function qualnames identify generated
bodies, including bodies held by recursive-repr closures. Unknown generated
code retains its `co_qualname`; attribution never exempts a live call. This
registry carries attribution only, never execution results or admission state.

Regression witnesses resume both `<string>` and ordinary-file generators
*inside* the guard with a colliding key already in their locals dictionaries,
requiring zero key protocols/live calls. Separate isolated running exec-frame
witnesses pin exact-dict traversal and rejection of a warmed live money cache.
Mutations restore the unsafe profile trampoline, explicitly materialize locals,
remove unsupported-interpreter rejection, remove generated-code attribution,
and omit the non-proxy dict edge. Event-metadata witnesses additionally resume
a frozen frame with a hostile module `__file__` property and check native string,
module-owner and worker-code handling. Their mutations restore ordinary module
attribute access, dictionary lookup on hostile keys, string subclass hashing,
code-constant hashing, the unsafe builtin qualname getter and missing static
builtin attribution. A raw-filename cache mutation also verifies that the
tracer exclusion still rejects hashing initiated by the guard.

## Per-event operation inventory

Locations below are in `tests/fakes/frozen_package.py`.

| Operation | Native primitive / invariant | File:line |
| --- | --- | --- |
| Python/C event selection | CPython-supplied exact string event; literal comparison | `tests/fakes/frozen_package.py:535` |
| Frame and code fields | Sealed `FrameType` / `CodeType` native fields; no locals reads | `tests/fakes/frozen_package.py:536` |
| Globals name lookup | `dict.items` and `_metadata`'s native `str.__eq__`, never key hashing/equality dispatch | `tests/fakes/frozen_package.py:536`, helper `:173` |
| Module-valued owner | `ModuleType.__dict__` slot `__get__`, native dict items, native string comparison | `tests/fakes/frozen_package.py:226` |
| String metadata normalization | `type`, native `issubclass(..., str)`, `str.__str__` produce exact strings or empty string | `tests/fakes/frozen_package.py:221` |
| Builtin identity / admission | `id(arg)` lookup in native integer-key dict; `is`; `id(frame.f_code)` membership in integer set | `tests/fakes/frozen_package.py:539` |
| Builtin module and qualname | Native builtin subtype admission; native `__module__` descriptor; qualname assembled from native `__name__`, `__self__` and type `__qualname__` slots (not builtin `__qualname__`); exact base builtin GC edges recover hidden static owners | `tests/fakes/frozen_package.py:543`, `:561`; descriptors `:236`, qualname helper `:241` |
| Hidden static builtin owner | Captured native `gc.get_referents` on exact `BuiltinFunctionType`; native list indexing/pop; trailing module edge removed with `is` | `tests/fakes/frozen_package.py:250` |
| Provenance cache | Exact tuple of normalized strings in guard-owned dict; flags are native booleans | `tests/fakes/frozen_package.py:550` |
| Namespace / live filename classification | `str.__eq__`, `str.startswith`, `str.__contains__`; precomputed exact root prefix | `tests/fakes/frozen_package.py:212`, `:216`, `:553` |
| Python qualname | Sealed code field then `str.__str__` normalization | `tests/fakes/frozen_package.py:561` |
| Generated attribution | Integer code-id lookup; registry retains strong code references and exact native qualnames | `tests/fakes/frozen_package.py:564`, registration `:139` |
| Live-call record | Captured native `_thread.get_ident`; integer-key `dict.setdefault`; exact-string formatting | `tests/fakes/frozen_package.py:568` |
| Inventory deduplication | Guard-owned set of exact strings and native line-number integer | `tests/fakes/frozen_package.py:569` |
| Current module registry | Native module namespace descriptor on `sys`; native dict items; native dict type admission | `tests/fakes/frozen_package.py:574` |
| Frozen module lookup | Native dict items plus native string equality, followed by native ModuleType admission | `tests/fakes/frozen_package.py:576` |
| Frozen module filename | Native module namespace descriptor, native dict items/equality, exact-string normalization | `tests/fakes/frozen_package.py:578` |
| Snapshot relative path | Native `str.startswith`, `str.__getitem__`, `slice`, `len`; no `Path` or `__fspath__` dispatch | `tests/fakes/frozen_package.py:579` |
| Inventory output / pin lookup | Exact-string `replace`; exact relative-string lookup in JSON-derived native pin dict; native set insertion | `tests/fakes/frozen_package.py:581` |
| Monitoring thread / frame admission | Captured native `_thread.get_ident` and `sys._getframe`; native builtin subtype check; native boolean/integer comparison | `tests/fakes/frozen_package.py:589`, `:593` |

The metadata witness attributes every protocol call to its immediate Python
caller. Calls from `tests/fakes/frozen_package.py` (the callback or any helper)
are failures. A third-party C tracer can hash and compare a resumed frame's
raw `co_filename` when that frame resumes. Coverage 7.13.5 uses
CTracer on CPython 3.12 with this repository's branch-coverage configuration;
requesting its sys.monitoring core falls back because that core cannot measure
branches on 3.12. These C calls have the resumed owner or test as their immediate
Python caller, without a guard caller frame. They are outside the guard's
no-user-protocol contract. The witness permits only filename hash/equality
calls from those two exact code identities and still requires zero
calls attributed to the guard, plus the original inventory assertions.
