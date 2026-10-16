"""Content-free transcription pricing and admission contracts.

The enclave measures audio; neither audio nor transcript enters this module.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from trusted_router.pricing import customer_fixed_price_microdollars


@dataclass(frozen=True)
class TranscriptionModel:
    id: str
    name: str
    provider: str
    upstream_id: str
    minute_cost_microdollars: int = 0
    request_cost_microdollars: int = 0
    pricing_source: str = ""

    def quote(self, duration_ms: int) -> int:
        if type(duration_ms) is not int or not 1 <= duration_ms <= 600_000:
            raise ValueError("Audio must contain at most ten minutes")
        if self.request_cost_microdollars:
            return customer_fixed_price_microdollars(self.request_cost_microdollars)
        # Bill whole seconds, never a complete minute for a partial clip.
        seconds = (duration_ms + 999) // 1000
        rate = customer_fixed_price_microdollars(self.minute_cost_microdollars)
        return (seconds * rate + 59) // 60

    @property
    def pricing(self) -> dict[str, str]:
        if self.request_cost_microdollars:
            return {"request": str(Decimal(customer_fixed_price_microdollars(self.request_cost_microdollars)) / 1_000_000)}
        return {"input_audio_minute": str(Decimal(customer_fixed_price_microdollars(self.minute_cost_microdollars)) / 1_000_000)}

    @property
    def price_label(self) -> str:
        if self.request_cost_microdollars:
            return "$" + self.pricing["request"] + "/request"
        return "$" + self.pricing["input_audio_minute"] + "/audio minute"


_PRICES = json.loads((Path(__file__).parent / "data/transcription_pricing.json").read_text())
if _PRICES["unit"] != "microdollars":
    raise ValueError("Invalid transcription price unit")


TRANSCRIPTION_MODELS = {
    spec.id: spec for spec in (
        TranscriptionModel("openai/whisper-large-v3-turbo", "Whisper Large V3 Turbo", "tinfoil", "whisper-large-v3-turbo", request_cost_microdollars=_PRICES["request_prices"]["openai/whisper-large-v3-turbo"], pricing_source="https://inference.tinfoil.sh/v1/models"),
        TranscriptionModel("openai/whisper-1", "OpenAI Whisper", "openai", "whisper-1", minute_cost_microdollars=_PRICES["minute_prices"]["openai/whisper-1"], pricing_source="https://developers.openai.com/api/docs/models/whisper-1"),
        TranscriptionModel("mistralai/voxtral-mini-2602", "Voxtral Mini Transcribe 2", "mistral", "voxtral-mini-2602", minute_cost_microdollars=_PRICES["minute_prices"]["mistralai/voxtral-mini-2602"], pricing_source="https://docs.mistral.ai/models/voxtral-mini-transcribe-26-02"),
    )
}
for _model in TRANSCRIPTION_MODELS.values():
    _rate = _model.request_cost_microdollars or _model.minute_cost_microdollars
    if type(_rate) is not int or _rate <= 0:
        raise ValueError("Invalid transcription price")


def transcription_metadata(model_id: str) -> dict[str, object]:
    spec = TRANSCRIPTION_MODELS[model_id]
    return {
        "endpoint": "/v1/audio/transcriptions",
        "billing_unit": "request" if spec.request_cost_microdollars else "audio_seconds",
        "pricing": spec.pricing,
        "input_formats": ["mp3", "wav"],
        "response_formats": ["json", "verbose_json"],
        "max_audio_bytes": 7 * 1024 * 1024,
        "max_duration_seconds": 600,
        "max_decoded_audio_bytes": 24_000_000,
        "pricing_source": spec.pricing_source,
    }
