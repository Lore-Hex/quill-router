# Video generation

TrustedRouter exposes an asynchronous video API at `https://api.trustedrouter.com/v1`.
The request enters the attested gateway, receives an exact provider quote, reserves
the quoted amount in integer microdollars, and is then submitted directly to the
provider. TrustedRouter does not send video requests through OpenRouter.

## Launch models

| TrustedRouter model | Family | Default |
|---|---|---|
| `bytedance/seedance-2.0-fast` | Seedance 2.0 Fast | 5 seconds, 720p |
| `bytedance/seedance-2.0` | Seedance 2.0 | 5 seconds, 720p |
| `lightricks/ltx-2.3-fast` | LTX 2.3 Fast | 6 seconds, 1080p |
| `lightricks/ltx-2.3` | LTX 2.3 | 6 seconds, 1080p |
| `google/gemini-omni-flash` | Gemini Omni Flash | 4 seconds, 720p |
| `minimax/hailuo-3` | MiniMax Hailuo 3, also called H3 | 5 seconds, 2K |

`GET /v1/videos/models` is the source of truth for currently enabled models and
their supported parameters.

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

- The gateway asks the direct provider for a content-free quote before sending
  the prompt or references upstream.
- The exact quote plus TrustedRouter's 20% video fee is reserved and settled as integer
  microdollars. Floating point values never touch the credit ledger.
- Retries with the same `Idempotency-Key` reuse the original authorization and
  job instead of generating and billing twice.
- The TrustedRouter control plane stores only job, provider, timing, and billing
  metadata. It never receives or stores prompts, reference media, generated
  bytes, or provider download URLs.
- The launch provider temporarily stores generated media while the asynchronous
  job is pending and until download or the 24-hour cleanup deadline. These
  routes are not advertised as provider E2EE or provider ZDR.
