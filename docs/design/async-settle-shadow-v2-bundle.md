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

## Named seams and current limitations

- `StageAssemblyInputs`: complete collector/readiness/budget/external joins and
  reviewed source closure are not orchestrated end to end. Released port wiring
  and stage execution are available for explicitly supplied complete graphs. The CLI does not yet implement the section 15.8
  `shadow_v2_window` command paths. That module is outside the new-files ownership
  list; release/trial/probe also require actions outside this offline task.
- `StreamingStageAssembly`: the plan uses bounded leaves, but DayPack's validator
  call still materializes an original-byte dictionary for a day. A streaming
  complete-stage adapter is unfinished; no bounded-memory production claim.
- `BasisReferenceIndex`: caller-supplied explicit retained basis/page references
  are required; complete derivation from all reader references is unfinished.
- `TerminalReconciliation`: obligation helpers check frozen contextual joins;
  independent terminal backing and complete support-day selection must precede
  the supplied callback. The real-writer test exercises compared originals only.
- `BudgetSourceAssembly`: measured operation/counter/trace joins, full publisher
  configured-cap arithmetic, trial/post scope, exact allowances across pre/post,
  and configured PEAKS derivation are not wired into a complete stage producer.
- `MonitoringSeriesCapture`: read `monitoring.projects.timeSeries.list` for the
  exact node, interval, metric and sample interval, retain the original pages and
  complete boundary, then compare baseline and trial series. This package checks
  a supplied projection; it does not yet parse native Monitoring response shapes.
- `CIArtifactCapture` / `ExternalSourceValidation`: the native owner uses explicit
  GET `/repos/{owner}/{repo}/actions/runs/{run_id}`, GET
  `/repos/{owner}/{repo}/actions/runs/{run_id}/jobs`, GET
  `/repos/{owner}/{repo}/actions/artifacts/{artifact_id}` and GET
  `/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/{archive_format}`.
  Original archive members and trial formats require their own reviewed adapters;
  the helper checks explicit captured metadata/result files. Full check-set
  derivation from section 11 and original trial results is not implemented.
- `ReleaseReviewAssembly`: subjects and supplied reviews are checked, but complete
  release-plan source/fixture registration and per-day review orchestration still
  depend on the caller's reviewed finite inputs.
- `MultiBundleLoading`: this producer writes and validates more than 10,000
  originals across bounded frozen leaves. Current `gate_loader.load` still applies
  the combined 10,000-object ceiling. Complete-stage evaluation is the gates round.
- `ExportAccountingConsumption`: producer comparison exists; the gate call sites
  above do not yet invoke it. Not every A3-I negative has a frozen rendered
  subject/count assertion through the real loader in this round.

Tests include a real P3/P4a writer history, native export on the fake database,
C1 independent lifecycle capture including an idle boot, receipt/slot chains,
obligations, coverage and finite packaging. That path is not a transport PASS:
full budget, boot-readiness and external graphs are still absent. With the
retained cap and no day manifest the current loader returns one
`evidence_schema_gap` at `transport/container`, detail `'router_writers'`, and no
proof. This is reproduced without filtering the cap or altering gates.

The separate grammar test exercises unchanged contract validation/materialization
and parsing with explicitly incomplete inputs. It is not the positive producer
acceptance path. The full deletion matrix, all A3-I rendered counts/subjects,
real native trials and complete first-window stage chain remain unfinished.
See `/tmp/v2-c2-author-1/report.md` for exact verification counts and scratch-copy
checks. No section 15 erratum is established by this round.
