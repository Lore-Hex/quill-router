# Shadow v2 offline bundle package, round 1

This package is dormant and operates on explicitly enumerated captured files.
It adds no runtime imports and does not call a remote service. The six pack
modules produce file plans, coverage pages, measurements, review subjects and
retained outputs. This round is **partial**: it does not produce a complete
first-window transport PASS or implement the release runbook end to end.

The governing A3 text is absent from this checkout's design file, which ends
at section 14. The author read section 15 from the sibling
`/Users/jperla/josh/repos/tr/wt/v2-design-a3/docs/design/async-settle-shadow-v2.md`.
The checkout design, frozen contracts, fixtures, pins, gates, collectors and
exporter are unchanged.

## File construction

`PackFiles` extends the collectors' `Files` implementation, retaining exact
FILE_REF bytes and explicitly tracking created files. `input_plan` partitions
sorted originals using both the 10,000-key and 262,144-byte INPUT_LEAF limits.
It creates frozen INPUT_INVENTORY_ENTRY/DETAIL pages and a producer-specific
`shadow-v2-pack-plan-1` plan with dates, predecessor references, bundle entries,
original count and inventory-page references. `check_plan` reads every expected
leaf and original, and compares the complete DETAIL chain. It never discovers
files by walking a directory.

`assemble_day` constructs F, C and R commitments from supplied original files,
root candidates and coverage pages, writes the proof first and then the day
manifest, and calls the unchanged `validate_preimages` and
`PayloadGraph.materialize`. The manifest commits the original proof bytes.
Declared closure, original inventory completeness, roots and horizons control
its complete flag. Missing preimages cannot be substituted. The structural
unit test uses explicitly incomplete populations and is not a source of
stage evidence.

`select_manifests` selects only the exact `/manifest-v2` subtype. Source cap and
retention bytes are preserved. Coverage retains every distinct original
candidate. All five streams are emitted, including empty pages for an explicitly
empty population. Reads use stream-qualified keys, so a close result and handoff
record with the same day/boot key cannot share a lookup accidentally.

`ExactPages` enumerates keys before reads and caches both present and absent
results. Receipt and scheduled contender chains retain the remaining interval
as a `Tail` on a missing or conflicting predecessor. `populations` derives
close/result expectations from independent writer lifetimes, including idle and
terminated writers; counter absence does not remove the writer. Obligations use
the frozen contextual constructors and terminal join, with original horizons
and explicit forward-coverage completion. Missing replay origins prevent an
obligation projection; they never yield a shortened successful projection.

## Export accounting interface for the gates owner

`pack_daypack.compare_export_accounting(files, reference, bundle, day=...,
object_plan=..., prefix=...)` parses the original
`shadow-v2-live-export-a3-n` root and its source pages. It compares the independent
day/kind and object plan before inspecting presence; original files, ordered
page chains, cursors, snapshots, successful EOF, generation, counts, byte totals,
native RPC totals and failures are retained and compared against bundle records.
The returned `AccountingResult` contains source references, missing reasons and
measured totals. Malformed or absent files raise; failed or inconsistent source
measurements produce a non-complete result. `prefix` locates an unchanged pass
under its own directory; it never rewrites that pass's references or bytes.

`bind_accounting` adds the frozen `export_accounting: DETAIL` file stream to a
copy of the open deployment object. Its serving value is unchanged. The caller
must supply every planned pass, including reruns, before reviewing that new
deployment object. Register the returned source references and all platform and
review dependencies in `input_plan`.

The intended gate call site is `PayloadGraph.inventory` in
`async_settle_shadow_v2_gate_payloads.py`, after reading the deployment object:
follow `export_accounting` with `detail(..., 'files')`, compare every independently
planned pass using this parser, and retain G1 missing states. `budget_view` in
`async_settle_shadow_v2_gate_views.py` should consume those totals plus retained
sink receipts and emit G7 failures. The parser requires native provenance by default; explicitly offline test
plans may call it with `require_native=False`. This package does not silently accept exporter MemoryPort
records as native operation measurements.

## Measurements, external records and history

Budget helpers preserve reviewed ALLOWANCES and TOTALS separately from measured
sources, calculate N=1 nominal reads 1.25/s and pending rows 250.25/s with exact
fractions, split measured intervals at midnight, reject absent PEAKS fields,
and check every component bound. CPU samples name the actual node and explicit
interval, cover the full requested interval, and keep baseline plus positive
approved increment strictly below 45 percent. The supplied CPU source is a
captured Monitoring projection; its native adapter and trial comparison are
still a named seam below. No CPU slope is calculated.

Shutdown calculation imports the collectors' `durations`, checks each measured
field, separate two-second close/result bounds, their four-second enclosing pass,
and preparation + wait + final pass against remaining grace and nine seconds.
Nested spans are counted once. Export cost checks total native reads and sink
receipts separately; reruns consume additional pass capacity.

Budget envelopes use the real retained runner, retain exact output bytes, and
link post to C(pre). External policy names reproduce every separate C4 name,
including deferred policy names and repeated stage requirements. CI helpers
compare run/job/artifact IDs, full revisions, original conclusion and completed
time; a job title cannot supply a result. `capture_ci` reuses CIPort/Pass/Query,
with 100-record pages, 10,000 records per pass and ten-second calls. Proof checks
must match the supplied reviewed check set exactly, and refer to retained sources.
The proof builder is a packaging interface, not an original-results adapter.

History requires a supplied review for the independent history file, compares
it with the complete retained input before date selection, then invokes the
existing `run_retained` callable. Both collect_rollbacks and report calls must
be observed. BLOCKED output bytes remain unchanged. Support pages and the
RETAINED_OUTPUT capture are retained. No v1 elapsed time becomes v2 seconds.

## Offline CLI

```sh
.venv/bin/python -m scripts.async_settle.shadow_v2_pack plan \
  --plan captured-plan.json --input captured --out new-plan
.venv/bin/python -m scripts.async_settle.shadow_v2_pack review-inputs \
  --plan review-inputs.json --input captured --out review-subjects
.venv/bin/python -m scripts.async_settle.shadow_v2_pack seal \
  --plan sealing-inputs.json --input captured --out sealed \
  --release-plan-sha256 INDEPENDENT_REVIEW_DIGEST
.venv/bin/python -m scripts.async_settle.shadow_v2_pack pack \
  --plan assembly-inputs.json --input captured --out packed
```

Each input plan has `files`, an explicit FILE_REF list. `plan` also takes `days`
and `predecessors`. `review-inputs` takes `subjects`, `criteria`, `required_stages`
and `verifiers`; it produces no human decision and exits nonzero while review is
pending. Sealing takes `review` and supplied `approvals`; the final externally
reviewed release plan must include the subject and review-record digests.
Changed review bytes need a new independently communicated release-plan digest.

`pack` accepts `assembly` containing manifest/proof fields, qualified artifact
FILE_REFs, root candidates with original body FILE_REFs, expected sources and
horizons, plus the original `transport` members. `accounting_passes` must enumerate every
planned day/pass with `root` and `bundle` FILE_REFs, `day`, `object_plan` and optional
`prefix`. Accounting is compared before assembly; absent inputs return
`ExportAccountingInputs`. Captured transport files exceeding FILE_REF's byte
bound need the unfinished streaming stage adapter. It assembles one day. With an independently supplied release digest and the
original public descriptor files, it invokes the existing released port wiring,
real loader, evaluator and renderer. It retains validated report/detail/output
files and exits nonzero on BLOCKED or unavailable output. Without that review
it returns `ReleaseReview`. Missing assembly returns `StageAssemblyInputs`.
Every invocation requires a new output directory, including after a failure.

## Round 8 claims and seams

Producer acceptance uses fake external ports and real package producers. No
whole-window PASS, configured largest-day result, or completed semantic matrix
replacement is claimed. A missing reviewed individual output plan returns
`MissingLaterFilePlan` and no output measurement. Native export work requires
complete reviewed call times for every compared read and an exact receipt
population. Composed completion does not extend the native horizon.

| Seam | Owner | Retained result / remaining work |
|---|---|---|
| FrozenDayValidationCapacity | Gates corrections | The frozen call still materializes a whole-day dictionary. Producer bodies use disk records; no configured largest-day measurement is claimed. |
| MultiBundleLoading | Gates corrections | Combine bounded daily and predecessor plans; current combined loader limit remains 10000 objects. |
| ExportAccountingConsumption | Gates corrections | Adopt comparison in PayloadGraph.inventory and consume compared costs in budget_view with G1/G7 results. |
| Obligation projection attachment shape | Gates corrections | Existing parsing accepts pages only; consume the preserved writer/day projection. |
| Frozen proof attachment walk | Gates corrections | The recorded reverse references retain an old digest. No byte-consistent digest-cycle rejection is demonstrated. |
| Cap-as-manifest selection | Gates corrections | reset_view parses the retained cap as a MANIFEST. |
| Budget counter/segment join | Gates corrections | budget_view at gate_views.py:121 requires a whole-life counter interval to equal each load segment. Consume the preserved counter with the required CPU/role segment boundaries. |
| Stage-specific proof checks | Gates corrections | Consume the applicable stage scope and deferred names from the retained whole-policy criteria. |
| Required input file | Gates corrections | The current signature_views requires loading/signatures.json; no substitute file is produced. |
| Binary original transport | Gates corrections | String transport cannot carry archive chunks; exact files remain in input plans. |
| Native CI/Monitoring clients and released originals | P6 | Fake ports remain test inputs; actual source records and bounded native clients remain external. |
| Finite first-window schedule | P6 / release plan | Supply caps, surge, K, finite launches, churn and pass schedule. |
| Continuous eight-date producer example | C2, P2-02 | The current real-writer example has short daily populations. Its window and both inner aggregate flags stay false; continuous scheduled writer generation remains unfinished. |
| Configured largest-day generator and allocation | C2, P2-03 | Disk-backed bodies do not establish the missing finite-day generation and separate largest-day allocation result. No accepted measurement is emitted for that absent input. |
| Semantic matrix replacements | C2, P2-04 | Exact current vectors are retained; idle/terminated, predecessor boundaries, unseeded-region and referring-record replacements remain unfinished. Those cases remain BLOCKED and are not positive producer acceptance. |

This table supersedes the historical round-one limitations previously listed here.
