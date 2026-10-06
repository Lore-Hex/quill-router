# Recorded main authorization fixtures

`gateway_authorizations_main.json` was generated from **896a9f0d**, using its
actual gateway writer, schemas, normalization, catalog, storage, and test
fixtures in a separate archive outside this checkout. No current gateway
helpers or copied historical functions were injected. IDs and timestamps are
literal outputs from that run, not placeholders or recomputed hashes. API keys
are disposable offline test keys; no production state was used.

Commands run (from the working checkout unless `--directory` specifies otherwise):

```sh
mkdir -p /private/tmp/qr-main-896a9f0d
git archive 896a9f0d | tar -x -C /private/tmp/qr-main-896a9f0d
cp tests/fixtures/generate_gateway_authorizations.py /private/tmp/qr-main-896a9f0d/tests/fixtures/
UV_CACHE_DIR=/private/tmp/qr-r4-uv \
UV_PROJECT_ENVIRONMENT="$PWD/.venv" \
PYTHONPATH=/private/tmp/qr-main-896a9f0d/src:/private/tmp/qr-main-896a9f0d \
QR_MAIN_FIXTURE_OUTPUT=/private/tmp/qr-main-896a9f0d/gateway_authorizations_main.json \
uv run --no-sync --directory /private/tmp/qr-main-896a9f0d pytest -q -p no:cacheprovider tests/fixtures/generate_gateway_authorizations.py
cp /private/tmp/qr-main-896a9f0d/gateway_authorizations_main.json tests/fixtures/
```

The generation run passed (1 test). It recorded catalog video (identical retry
and `max_tokens: 1` to `400000` plus `video_resolution: "1080p"`), user-provided
video (identical retry through main's model preparation), and chat (identical
retry). Each includes raw requests, the stored `idempotency_fingerprint`, every
frozen authorization field, fixture setup state, and a changed-content conflict.
`main` records the observed historical status; `current_status` is the intended
compatibility result. The catalog derived-only retry conflicts on main but must
replay with this change.

The file also records main's outcomes for wrapper/base interleavings in both
directions and admission/retry with inconsistent explicit model identity, on
memory and both typed-store record modes. The small scenario helpers run on
both trees; they call each tree's own writer. They assert the same authorization
is returned, no fresh dispatch nonce is issued, and money is unchanged. Main
returns replay for the prepared wrapper/base equivalence; it also overwrites
inconsistent explicit identity before hashing and replays an identical retry.

Current tests load the literal authorizations directly without running a writer
or calculating a historical hash. The generator is skipped unless explicitly
opted into with `QR_MAIN_FIXTURE_OUTPUT`; it is not a historical writer test
fixture and must only regenerate this file in the pinned main archive.

## Round-4 resolution replay recordings

Regenerated the entire file in the same `/private/tmp/qr-main-896a9f0d` tree,
refreshed with `git archive 896a9f0d`, including six new observations named
`resolution-replay/{memory,typed,legacy}/{user,custom}`:

- User model: set `max_concurrency=1` before admission; retry the identical
  `video_resolution: "1080p"` request while the original occupies its slot.
- Custom wrapper: admit a `video_resolution: "1080p"` request, then remove
  video capability from its base model and retry the identical request.

Main returns 200 and the same authorization in all six cases, without live
candidate selection, slot acquisition, a new nonce, or any authorization/money
change. The generator records the status and live-call trace as well as asserting
that the persisted authorization and money remain unchanged. Tests compare
current observations with those literal recordings.

The six scenario cases and six preparation/validation/fingerprint/replay-order
cases all pass against pinned main and all fail against an archive of c640fdba.
On c640fdba the typed user cases return 429, all custom cases return 400, and
the memory user case still returns 200 but incorrectly performs live routing.
The order cases cover both omitted resolution and `1080p` in each case; only
resolution-bearing creator retries may perform main's pre-routing lookup.

## Creator-path comparison with origin/main

Compared the gateway and its changed helpers line by line with origin/main
`3b8f7a021fbc07ccd63811b2c501e4fcd809b319` (its gateway is identical to
896a9f0d). The follow-up restores the resolution lookup after preparation and
validation, moves creator fingerprinting back after routing validation, and
restores main's omission of the later legacy lookup after a resolution-triggered
lookup miss. Catalog requests retain the expanded pre-preparation lookup and
their second legacy lookup after an early miss.

Other existing branch differences remain and should not be mistaken for exact
equivalence of every creator request:

- `_gateway_authorize_fingerprint` excludes derived video fields for prepared
  creator bodies too, and `_video_cross_region_replay_matches` accepts the
  bounded legacy forms documented in `docs/video-generation.md`. Main only
  relaxes execution region. Thus changed derived values can replay here where
  main conflicts; `test_creator_video_derived_fields_still_replay` explicitly
  covers that existing branch policy.
- The new `X-Quill-Video-Allowed-Providers` header is parsed before key lookup
  and checked against the provider catalog before creator replay. Main ignores
  this header. After a replay miss it restricts custom-wrapper base routes and
  rejects owner-dispatch user models with 400. A valid header on a successful
  resolution replay does not reach those live-routing checks.
- Shared response helpers fall back to the provider slug when its display-name
  catalog entry is missing; main indexes that entry directly.
- Body normalization is extracted into `_gateway_authorize_body` and validates
  tags again before attribution; preparation and fingerprinting are extracted
  into local helpers. Creator model preparation, explicit identity overwrites,
  privacy checks, slot acquisition, and typed admission otherwise keep main's
  logic. Frozen catalog-candidate reconstruction remains catalog-only.
