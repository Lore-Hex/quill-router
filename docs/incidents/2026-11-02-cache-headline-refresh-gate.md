# Cached-Price Contract Blocks Catalog Refresh

## Observed Failure

The guarded refresh run [41512369587](https://github.com/Lore-Hex/quill-router/actions/runs/41512369587)
was dispatched once at 19:42 UTC after confirming no competing refresh or
deployment owner. The last validated refresh was at 12:45 UTC. Both hourly
workflows remained enabled, and other repository schedules ran during the gap;
the underlying reason these schedule events were missed is not established.

The workflow prepared snapshot `bf9f3a369c99357c848e32a2fec852f301c8a827`
from main `99bd5f2f51825b115f99981b9ab1f393579f17d9`. Its exact-SHA CI
[41513071157](https://github.com/Lore-Hex/quill-router/actions/runs/41513071157)
failed `test-post-cutover (7)` at 19:57 UTC. The failed shard reported one failure,
5369 passes, 232 skips and one expected failure. Publication remains gated;
neither preparing the snapshot nor passing its price-source audit is a release.

The unchanged failing test reproduced in an isolated locked environment at the
exact snapshot and CI clock `2027-05-01T00:00:00Z`.

## Cause And Repair

`test_model_shape_omits_cache_read_price_when_absent` selected the first model
whose aggregate tiers lacked a cached-input price. That subject became
`google/gemini-2.5-flash`: its aggregate cached rate is absent, but its first
minimum-prompt-price route, Google AI Studio, has an explicit cached rate.
The public price correctly uses that route's 31,650 microdollars per million
tokens alongside its 316,500 prompt price.

PR 2116 had already made the headline cache rate follow the headline route,
preventing an unrelated provider's cache price from being paired with the
cheapest prompt price. The older omission test still assumed the aggregate
model's cache rate controlled publication. Removing the real route's discount
to satisfy that test would reverse the existing correction.

Replace both catalog-dependent presence/absence tests with 15 constructed
cases: differing aggregate and route rates, absent and explicit zero rates,
both route orders, a more expensive distractor route, and no-route fallback.
No production pricing, billing, timeout, coverage or workflow code changes.

The focused cases pass on current main. On the failed snapshot with the same
test-only patch, the adjacent routing/listing/pricing suite passes 144 tests
under the failing CI clock; 94 normal provider-health exclusions remain.
In-memory fault injection makes the new cases fail for aggregate-model rates
(10 failures), first-route rates instead of the headline route (6 failures),
and dropping explicit zero rates (5 failures). The mutations do not edit source
files or production state.

Full local and exact-head CI gates, review and normal publication are still
required. Do not force-publish or retry the failed snapshot unchanged. A later
fresh recomputation must retain current main and all original release gates.
This repair addresses the reproduced CI blocker, not GitHub scheduling itself.
