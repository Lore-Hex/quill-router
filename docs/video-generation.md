# Video generation

TrustedRouter exposes an asynchronous video API at `https://api.trustedrouter.com/v1`.
The request enters the attested gateway, reserves credits in integer microdollars,
and is submitted directly to the selected provider. Fixed-price routes use an
exact provider quote; token-billed routes reserve an upper bound and settle the
actual usage. TrustedRouter does not send video requests through OpenRouter.

## Launch models

| TrustedRouter model | Family | Default |
|---|---|---|
| `bytedance/seedance-2.5` | Seedance 2.5 | 5 seconds, 720p |
| `bytedance/seedance-2.0-fast` | Seedance 2.0 Fast | 5 seconds, 720p |
| `bytedance/seedance-2.0` | Seedance 2.0 | 5 seconds, 720p |
| `lightricks/ltx-2.3-fast` | LTX 2.3 Fast | 6 seconds, 1080p |
| `lightricks/ltx-2.3` | LTX 2.3 | 6 seconds, 1080p |
| `google/gemini-omni-flash` | Gemini Omni Flash | 4 seconds, 720p |
| `minimax/hailuo-3` | MiniMax Hailuo 3, also called H3 | 5 seconds, 2K |
| `minimax/h3-max` | MiniMax H3 Max on fal | 5 seconds, 768p |

`GET /v1/videos/models` is the source of truth for currently enabled models and
their supported parameters.

## Direct BytePlus Seedance

Seedance 2.5, 2.0 and 2.0 Fast are available directly through BytePlus ModelArk.
Select `"provider": {"only": ["byteplus"]}` to require BytePlus without routing
through Venice. These routes support text or a first-frame image, 4-15 seconds,
and 480p or 720p. Seedance 2.5 and 2.0 also support 1080p on the direct BytePlus
route with the resolution-aware enclave upgrade; 2.0 Fast stops at 720p.
Video/audio references, last-frame input, reference-image sets, and 4K are not
supported on these direct routes.

```json
{
  "model": "bytedance/seedance-2.5",
  "provider": {"only": ["byteplus"]},
  "prompt": "A slow camera pan across a sunlit mountain landscape",
  "duration": 4,
  "resolution": "480p",
  "aspect_ratio": "16:9"
}
```

BytePlus bills generated **video output tokens**, not prompt text tokens or a
fixed fee per second. The route's per-million-token price is frozen when the job
is authorized, using the requested resolution. Before router markup, Seedance
2.5 costs $10.70/M at 480p/720p and $11.70/M at 1080p; Seedance 2.0 costs
$7.00/M and $7.70/M respectively. Fast costs $5.60/M at 480p/720p. These are
list rates for text or first-frame image input; temporary discounts do not apply.
A conservative credit reservation is released at settlement;
only the completed task's actual output tokens are charged. Polling a completed
job does not charge again. Its response includes the final token usage and cost.

The separate Venice route for Seedance 2.5 supports text, first/last-frame image, and
image/audio-reference input. It accepts 4-30 seconds, 480p/720p/1080p, and an
optional `generate_audio` switch. Image-to-video inherits the source image's
aspect ratio. Prompts may contain up to 15,000 characters. Video references
are not enabled on this route. Pricing is quoted live per job, not per text
token; the route is neither ZDR nor confidential.

## Create and download

```bash
curl https://api.trustedrouter.com/v1/videos \
  -H "Authorization: Bearer $TRUSTEDROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: launch-video-001" \
  -d '{
    "model": "minimax/hailuo-3",
    "prompt": "A camera glides through a quiet neon city at night",
    "duration": 5,
    "resolution": "2K",
    "aspect_ratio": "16:9",
    "generate_audio": true
  }'
```

The create call returns `202 Accepted`:

```json
{
  "id": "job-...",
  "polling_url": "/v1/videos/job-...",
  "status": "pending"
}
```

Poll until `status` is `completed`, then stream the first URL in
`unsigned_urls`:

```bash
curl -H "Authorization: Bearer $TRUSTEDROUTER_API_KEY" \
  https://api.trustedrouter.com/v1/videos/job-.../content \
  --output result.mp4
```

The first successful full download deletes the provider copy and subsequent
content requests return `410 Gone`. If content is never downloaded,
TrustedRouter requests provider deletion after 24 hours. Status and billing
metadata remain available without retaining the prompt or generated media.

## Image and reference input

Use `frame_images` for image-to-video:

```json
{
  "model": "bytedance/seedance-2.0-fast",
  "prompt": "The subject turns toward the camera as the light changes",
  "frame_images": [
    {"frame_type": "first_frame", "image_url": "https://example.com/start.jpg"}
  ]
}
```

Models advertising reference support also accept `input_references` with
`type` set to `image`, `audio`, or `video`. References must be HTTPS URLs or
base64 data URLs. Local, private-network, and cloud-metadata URLs are rejected.

## Billing and privacy

- For fixed-price routes, the gateway asks the provider for a content-free quote
  before sending the prompt or references upstream. The exact quote plus
  TrustedRouter's 20% video fee is reserved and settled as integer microdollars.
- For token-billed BytePlus routes, the catalog price already includes the
  applicable markup. Settlement uses the actual output tokens at the authorized
  tariff, without adding a second fixed-price fee. Missing or out-of-bound usage
  fails closed. Floating point values never touch the credit ledger.
- Retries with the same `Idempotency-Key` reuse the original authorization and
  job instead of generating and billing twice.
- The TrustedRouter control plane stores only job, provider, timing, and billing
  metadata. It never receives or stores prompts, reference media, generated
  bytes, or provider download URLs.
- The launch provider temporarily stores generated media while the asynchronous
  job is pending and until download or the 24-hour cleanup deadline. These
  routes are not advertised as provider E2EE or provider ZDR.

### Internal resolution tariff contract

The enclave sends `video_resolution: "480p" | "720p" | "1080p"` on
`POST /v1/internal/gateway/authorize` with `route_type: "videos"` and the job's
output-token ceiling in `max_tokens` (the existing output-limit aliases also
work). The router filters unsupported token-billed endpoints and uses the
selected resolution rate for both the credit hold and frozen settlement tariff.
Fixed-quote endpoints such as Venice retain their existing pricing.

A successful response includes `data.video_tariff_resolution` equal to the
requested resolution. The enclave must require this acknowledgment before
dispatching 1080p to BytePlus. The router change can deploy first: requests
without `video_resolution` retain their existing pricing, snapshots, and response
shape, and receive no acknowledgment. Idempotent retries return the frozen
acknowledgment. For `route_type: videos`, the authorization fingerprint excludes
all derived execution fields: `video_resolution`, `max_tokens`,
`max_output_tokens`, `max_completion_tokens` (the accepted output-limit alias),
and `additional_cost_reservation_microdollars`. The enclave's
`request_fingerprint` binds the logical request, including resolution and
duration; the router continues binding the model and original caller provider
policy within the workspace, API-key, and idempotency-key scope. A changed
logical fingerprint, model, or caller policy conflicts with HTTP 409 in that
scope. Cross-region retries retain the existing region compatibility rule;
caller region restrictions inside provider policy remain bound. Other route
types retain their existing fingerprint rules.

Existing stored hashes are not rewritten. Replay also compares legacy hashes
using the frozen snapshot's original token limit and resolution, with the
historical alias-presence combinations and omitted-resolution form (including
the earlier resolution-only exclusion). Pre-snapshot fixed-quote jobs use the
historical token-limit sentinel of 1. Every comparison still requires an exact
hash match with the retry's logical identity and original caller policy. Thus
a fixed-quote Venice authorization with token limits of 1 can replay after an
enclave rollout sends 400000 and `video_resolution: "1080p"` because BytePlus
became eligible. Replay returns the original authorization, snapshot, and hold,
without a new reservation or invented tariff acknowledgment. Settlement uses
only the authorization's frozen tariff, even after a catalog price refresh.

The authenticated enclave may send `X-Quill-Video-Allowed-Providers` on this
endpoint for video requests only. It supplies derived capability constraints
(for example, `byteplus` for seeded Seedance), never a public caller header.
The router accepts one header value of at most 4096 characters, containing
1–64 distinct, comma-separated, known canonical provider IDs matching
`[a-z0-9]+(?:-[a-z0-9]+)*`. Spaces and tabs around IDs are allowed; empty
members, duplicates, aliases, unknown IDs, repeated header fields, and use on
another route type return HTTP 400 (`bad_request`). Absence preserves existing
routing. The header stays outside the authorization body and persisted logical
identity; the original caller `provider` policy remains fingerprinted.

Every keyed video request carrying `request_fingerprint` looks up and validates
the existing authorization before live capability, tariff, or provider filtering.
Valid retries return the original hold with `idempotent_replay: true`, including
across header rollout, changed capability lists, or execution regions. A replay
grants no fresh dispatch authority and cannot recreate a missing job. On a miss,
the header intersects the effective caller policy (`only`, `ignore`, and nonempty
`order` when fallbacks are disabled) before route selection. All selected,
fallback, and frozen routes obey that intersection; no eligible route returns a
4xx before any funds are reserved. Transactional duplicate checks still arbitrate
concurrent first requests.

An invalid resolution or its use on any other route returns HTTP 400
(`bad_request`). If resolution filtering removes every candidate, authorization
returns HTTP 400 (`provider_not_supported`). Token endpoints without a resolution
table retain their base rate only for 480p/720p.
