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
QR_MAIN_FIXTURE_OUTPUT=/private/tmp/qr-main-896a9f0d/gateway_authorizations_main.json \
uv run --directory /private/tmp/qr-main-896a9f0d pytest -q -p no:cacheprovider tests/fixtures/generate_gateway_authorizations.py
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
