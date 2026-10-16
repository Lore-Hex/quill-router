# Audio transcription

`POST https://api.trustedrouter.com/v1/audio/transcriptions` runs in the attested gateway. Audio is parsed in memory, never in the parent relay or control plane. Transcripts and uploaded audio are not retained in router logs, billing records, or broadcast events. The selected provider receives the audio according to that route's privacy policy.

## OpenAI SDK

```python
import os
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["TRUSTEDROUTER_API_KEY"],
    base_url="https://api.trustedrouter.com/v1",
)
with open("recording.wav", "rb") as audio:
    result = client.audio.transcriptions.create(
        model="openai/whisper-large-v3-turbo",
        file=audio,
        response_format="verbose_json",
        timestamp_granularities=["segment"],
        extra_body={"provider": {"only": ["tinfoil"], "min_privacy": "confidential"}},
    )
print(result.text)
```

JSON requests accept `input_audio: {"data": "<base64>", "format": "wav"}` instead of `file`. Other fields: `model`, `language`, `prompt`, `temperature`, `response_format`, `timestamp_granularities`, `diarize`, `keyterms`, `provider`, `user`, `session_id`, and `trace`. Provider filtering and API-key restrictions apply before any provider call.

## Models and billing

| Model | Provider | Upstream billing unit |
| --- | --- | --- |
| `openai/whisper-large-v3-turbo` | Tinfoil | Request |
| `openai/whisper-1` | OpenAI | Audio duration |
| `mistralai/voxtral-mini-2602` | Mistral | Audio duration |

Read current customer prices and limits from `/v1/models?output_modalities=transcription`. Duration is measured inside the enclave, rounded up to whole seconds, and reserved before invoking the provider. The reserved rate is frozen through settlement. A successful response includes `usage.cost` in USD and `usage.seconds`, plus an `X-Generation-Id` header. Silence can legitimately return empty text and is billed as a successful transcription. Provider failures refund the hold. Settlement must succeed before the transcript is returned.

Tinfoil uses verified attestation and pinned TLS and fails closed if verification fails. Only Tinfoil supports `provider.min_privacy="confidential"` for this release. The confidential-only API hostname requires that explicit privacy setting. OpenAI and Mistral transcription do not inherit any chat-specific ZDR claim.

The router does not silently switch these models or providers. BYOK, signed inference receipts, stored response replay, realtime streaming, translation, and URL fetching are not supported. Reusing a completed idempotency key returns 409 rather than storing or regenerating the transcript.

## Formats and limits

- MP3 and PCM16 WAV only. WAV must have one or two channels at 8 to 48 kHz.
- At most 7 MiB uploaded audio, ten minutes, and 24 MB decoded MP3 PCM. MP3 is decoded to canonical mono WAV inside the enclave, so billing duration and the audio received by the provider agree.
- JSON or verbose JSON responses. `word` and `segment` timestamps require verbose JSON and provider support.
- `prompt` and `temperature` are supported by Whisper. Mistral instead supports `keyterms` (up to 100 terms) and `diarize=true`; diarization requires verbose JSON. Mistral cannot combine `language` with timestamps and accepts one timestamp granularity per request.
- Only two transcription validations/provider calls can run concurrently per enclave. Excess requests receive a retryable 503 without a billing hold.
- Unsupported fields and formats fail explicitly before authorization. No temporary upload files or external codec processes are used in production.

## Sources

- [OpenRouter transcription request format](https://openrouter.ai/docs/api/api-reference/stt/create-transcription)
- [Tinfoil audio guide](https://docs.tinfoil.sh/guides/processing-audio)
- [OpenAI Whisper](https://developers.openai.com/api/docs/models/whisper-1)
- [Mistral Voxtral Mini Transcribe 2](https://docs.mistral.ai/models/voxtral-mini-transcribe-26-02)
