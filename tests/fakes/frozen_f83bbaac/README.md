# f83bbaac package snapshot

Baseline: `f83bbaacb3e91271f7bac9ba26d826532f10962f`.

`package.tar.gz` is one snapshot of the entire baseline `src/trusted_router`
Python package and its text resources. It replaces the thirty selected-function /
module extracts formerly named `async_proof_*_main.txt`. All 402 Python files are
included, plus JSON, JSONL, HTML, TXT, SQL, CSS and JavaScript resources (747 files
in total). Binary static media are unnecessary for the tested HTTP requests and
are not included. Missing resources never fall back to the live tree.

`pins.json` maps original repository paths to SHA-256 hashes of **unchanged file
bytes**, not ASTs. The loader at `../frozen_package.py` pins the archive digest,
checks the exact member set and every file digest, and verifies source bytes again
before executing a module. It extracts outside the repository and loads the
package as `frozen_f83bbaac`. Absolute application imports are redirected to that
namespace; relative imports already resolve there. No Git access is needed to
run the tests or mutation tables.

Reproduction uses read-only Git operations: obtain the baseline using
`git archive --format=tar f83bbaac src/trusted_router`, keep regular members with
suffixes `.py`, `.json`, `.jsonl`, `.html`, `.txt`, `.sql`, `.css`, `.js` in their
original archive order, and preserve each member's metadata. Write with Python's
`tarfile.open(..., mode='w')`; gzip with `filename=''` and `mtime=0`. SHA-256 the
original member bytes into a sorted, two-space-indented JSON object with a final
newline. The archive itself is independently pinned in the loader. An auditor
can compare each member directly to `git show f83bbaac:<member path>`.

`execution-inventory.json` records the union of guarded executions from the
Round-4 requested six-worker test selection. Each record maps the original module
and qualified callable name to its alias, snapshot member and byte pin. Generated
dataclass methods are attributed to the owning class/file; their code line refers
to the generated function, not a literal line in the source file. `<module>`, class
bodies and comprehensions are retained too. This is evidence, **not an allowlist**.
The runtime guard rejects any live router call regardless of this inventory.

To regenerate execution evidence, set `F83BBAAC_INVENTORY_DIR` to an empty temporary
directory when running `tests/test_async_settle_proof_oracle.py`. Each process
writes an independent JSON set of `(module, qualname, code_firstlineno, member,
sha256)` rows. Union and sort these files. Both snapshot setup and request/drain
execution are guarded and recorded. Fixture seed preparation is outside both legs.

See `docs/async-settle-pr-f1-round4.md` and
`docs/async-settle-f83bbaac-inventory.md` for verification and the complete readable
inventory.
