# Text to Speech

`POST https://api.trustedrouter.com/v1/audio/speech` follows the OpenRouter
speech request format. Use your normal TrustedRouter API key and credits.
Success is raw audio, not JSON. Errors are ordinary JSON API errors.

```bash
curl --fail-with-body https://api.trustedrouter.com/v1/audio/speech \
  -H "Authorization: Bearer $TRUSTEDROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"x-ai/grok-voice-tts-1.0","input":"Hello from TrustedRouter.","voice":"eve","response_format":"mp3"}' \
  --output speech.mp3
```

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["TRUSTEDROUTER_API_KEY"],
    base_url="https://api.trustedrouter.com/v1",
)
with client.audio.speech.with_streaming_response.create(
    model="mistralai/voxtral-mini-tts-2603",
    input="Hello from TrustedRouter.",
    voice="en_paul_neutral",
    response_format="mp3",
) as response:
    response.stream_to_file("speech.mp3")
```

## Models and Parameters

Discover current prices, voices and limits with
`GET /v1/models?output_modalities=speech`. `pricing.input_character` is USD per
input character for character-billed models. Tinfoil uses `pricing.request`, in
USD per successful synthesis request. Gemini instead exposes text-input
and audio-output token prices. `trustedrouter.speech.billing_unit` distinguishes
them and includes supported formats, voices, and limits.

| Model | Voices | Formats | Speed | Input limit |
| --- | --- | --- | --- | --- |
| `qwen/qwen3-tts` | 9 presets, including `serena` | WAV | 1 | 4,096 characters |
| `mistralai/voxtral-4b-tts-2603` | 20 presets, including `neutral_female` | WAV | 1 | 4,096 characters |
| `x-ai/grok-voice-tts-1.0` | `eve`, `ara`, `rex`, `sal`, `leo` | MP3, PCM | 0.7-1.5 | 60,000 characters |
| `mistralai/voxtral-mini-tts-2603` | `en_paul_neutral` | MP3 | 1 | 10,000 characters |
| `google/gemini-3.8-flash-tts` | 30 voices, including `Kore` | PCM, WAV | 1 | 8,192 characters and tokens |
| `google/gemini-3.8-flash-lite-tts` | 30 voices, including `Kore` | PCM, WAV | 1 | 8,192 characters and tokens |
| `microsoft/mai-voice-2.1` | `en-US-Harper`, `en-US-Grant` | PCM, MP3 | 1 | 5,000 characters |
| `microsoft/mai-voice-2.1-flash` | `en-US-Harper`, `en-US-Grant` | PCM, MP3 | 1 | 5,000 characters |
| `elevenlabs/eleven-v3` | [Eight shared voices](#elevenlabs-voices) | PCM, MP3 | 1 | 5,000 characters |
| `elevenlabs/eleven-multilingual-v2` | [Eight shared voices](#elevenlabs-voices) | PCM, MP3 | 1 | 10,000 characters |
| `elevenlabs/eleven-flash-v2.5` | [Eight shared voices](#elevenlabs-voices) | PCM, MP3 | 1 | 40,000 characters |
| `elevenlabs/eleven-turbo-v2.5` | [Eight shared voices](#elevenlabs-voices) | PCM, MP3 | 1 | 40,000 characters |

Required: `model`, `input` (a nonempty string), `voice`.
Optional: `response_format` (defaults to `wav` on Tinfoil, otherwise `pcm`), `speed` (defaults to 1),
`provider`, `user`, `session_id`, `metadata`, and `tags`.
Specify MP3 for Mistral; its PCM default is rejected rather than silently changed.
PCM is 24 kHz, mono, signed 16-bit little-endian. MP3 returns `audio/mpeg`.

The initial release buffers audio before settlement and delivery. The SDK's
streaming-response interface works, but this is not incremental synthesis streaming.
Voice cloning, input arrays, instructions, provider-specific options, and BYOK are
not supported; unsupported fields fail explicitly. Normal provider restrictions,
privacy floors and API-key budgets remain enforced. The two Tinfoil routes use
an attested, certificate-pinned connection and fail closed if verification fails.
The other speech routes are not confidential inference. The attested gateway
does not by itself make an upstream provider confidential.

### Confidential speech on Tinfoil

```bash
curl --fail-with-body https://api.trustedrouter.com/v1/audio/speech \
  -H "Authorization: Bearer $TRUSTEDROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen/qwen3-tts","input":"Hello from TrustedRouter.","voice":"serena","provider":{"only":["tinfoil"],"min_privacy":"confidential"}}' \
  --output speech.wav
```

Use `mistralai/voxtral-4b-tts-2603` with `neutral_female` for Voxtral TTS.
Both return WAV; alternative codecs, voice cloning and custom speed are not
supported. The 4,096-character limit is TrustedRouter's admission limit.
The complete preset lists are in `/v1/models?output_modalities=speech` and
[Tinfoil's speech guide](https://docs.tinfoil.sh/guides/processing-audio).

### ElevenLabs voices

All four ElevenLabs models support these eight voices in the order shown.
Pass the voice ID as `voice`; pricing is the same for every voice on a given model.

| Voice ID | Name |
| --- | --- |
| `JBFqnCBsd6RMkjVDRZzb` | George |
| `EXAVITQu4vr4xnSDxMaL` | Sarah |
| `Xb7hH8MSUJpSbSDYk0k2` | Alice |
| `XrExE9yKIg1WjnnlVkGX` | Matilda |
| `hpp4J3VqNfWAUOO0d1Us` | Bella |
| `cgSgspJ2msm6clMCkdW9` | Jessica |
| `FGY2WhTYpPnrIDTdsKH5` | Laura |
| `pFZP5JQG7iQjIQuC4Bku` | Lily |

## Billing and Retries

Character charges count input Unicode code points, including spaces and punctuation, at the
catalog character rate, rounded up to a microdollar. Ordinary TrustedRouter markup
applies. The complete charge is reserved before synthesis; failure releases it.
There are no fabricated input/output token counts. The hourly pricing workflow
reads each provider's official character tariff with unit and price-change guards.

Tinfoil reserves one fixed request charge, independent of text length within the
input limit. It is charged once only after valid audio is received. Attestation,
provider and audio-validation failures release the hold. The hourly refresh reads
Tinfoil's `requestPrice` separately from token tariffs and applies the same
price-change guard. Current customer prices are published in the model catalog.

Gemini reserves the maximum 8,192 text-input and 16,384 audio-output tokens,
then charges only provider-reported tokens and releases the unused hold. Token
rates are frozen at authorization and use ordinary token-billing rounding.
Gemini requests set `store:false`. Azure Speech preview and ElevenLabs are not
advertised as ZDR; enterprise agreements for other products are not inherited.

ElevenLabs v4 was probed successfully but is not published in this initial
catalog: its short-lived promotional price needs a dated tariff contract before
it can be billed safely. The four stable-price models above are supported.

Responses include `X-Generation-Id`, `X-Request-Id`, `X-Usage-Input-Characters`,
and `X-Usage-Cost` (USD). Query `/v1/generation?id=<X-Generation-Id>` for billing
metadata; audio content is not retained. Input text stays inside the enclave and
the selected provider, not the control plane, activity records or broadcasts.

An `Idempotency-Key` prevents duplicate paid generation. Repeating a used key
returns 409 because there is no stored audio to replay. A new key intentionally
generates and charges for new audio. Do not retry with a new key unless that is
what you intend.
