"""Content-free transcription pricing and admission contracts.

The enclave measures audio; neither audio nor transcript enters this module.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from trusted_router.pricing import customer_fixed_price_microdollars

MAX_INPUT_TOKENS = 28_672
MAX_OUTPUT_TOKENS = 4_096


@dataclass(frozen=True)
class TranscriptionModel:
    id: str
    name: str
    provider: str
    upstream_id: str
    minute_cost_microdollars: int = 0
    request_cost_microdollars: int = 0
    pricing_source: str = ""
    input_token_cost: int = 0
    output_token_cost: int = 0

    @property
    def token_billed(self) -> bool:
        return self.input_token_cost > 0

    @property
    def response_formats(self) -> tuple[str, ...]:
        return ("json",) if self.token_billed or self.id == "mistralai/voxtral-mini-3b-2507" else ("json", "verbose_json")

    def quote(self, duration_ms: int) -> int:
        if type(duration_ms) is not int or not 1 <= duration_ms <= 600_000:
            raise ValueError("Audio must contain at most ten minutes")
        if self.token_billed:
            return 0  # Token reservation uses the catalog's frozen input/output rates.
        if self.request_cost_microdollars:
            return customer_fixed_price_microdollars(self.request_cost_microdollars)
        # Bill whole seconds, never a complete minute for a partial clip.
        seconds = (duration_ms + 999) // 1000
        rate = customer_fixed_price_microdollars(self.minute_cost_microdollars)
        return (seconds * rate + 59) // 60

    @property
    def pricing(self) -> dict[str, str]:
        if self.token_billed:
            return {
                "prompt": str(Decimal(customer_fixed_price_microdollars(self.input_token_cost)) / 1_000_000_000_000),
                "completion": str(Decimal(customer_fixed_price_microdollars(self.output_token_cost)) / 1_000_000_000_000),
            }
        if self.request_cost_microdollars:
            return {"request": str(Decimal(customer_fixed_price_microdollars(self.request_cost_microdollars)) / 1_000_000)}
        return {"input_audio_minute": str(Decimal(customer_fixed_price_microdollars(self.minute_cost_microdollars)) / 1_000_000)}

    @property
    def price_label(self) -> str:
        if self.token_billed:
            prompt = (Decimal(self.pricing["prompt"]) * 1_000_000).normalize()
            completion = (Decimal(self.pricing["completion"]) * 1_000_000).normalize()
            return f"${prompt:f} input / ${completion:f} output per million tokens"
        if self.request_cost_microdollars:
            return "$" + self.pricing["request"] + "/request"
        return "$" + self.pricing["input_audio_minute"] + "/audio minute"


_PRICES = json.loads((Path(__file__).parent / "data/transcription_pricing.json").read_text())
if _PRICES["unit"] != "microdollars":
    raise ValueError("Invalid transcription price unit")


TRANSCRIPTION_MODELS = {
    spec.id: spec for spec in (
        TranscriptionModel("mistralai/voxtral-small-24b", "Voxtral Small 24B", "tinfoil", "voxtral-small-24b", input_token_cost=_PRICES["token_prices"]["mistralai/voxtral-small-24b"]["input"], output_token_cost=_PRICES["token_prices"]["mistralai/voxtral-small-24b"]["output"], pricing_source="https://inference.tinfoil.sh/v1/models"),
        TranscriptionModel("openai/whisper-large-v3-turbo", "Whisper Large V3 Turbo", "tinfoil", "whisper-large-v3-turbo", request_cost_microdollars=_PRICES["request_prices"]["openai/whisper-large-v3-turbo"], pricing_source="https://inference.tinfoil.sh/v1/models"),
        TranscriptionModel("openai/whisper-1", "OpenAI Whisper", "openai", "whisper-1", minute_cost_microdollars=_PRICES["minute_prices"]["openai/whisper-1"], pricing_source="https://developers.openai.com/api/docs/models/whisper-1"),
        TranscriptionModel("mistralai/voxtral-mini-2602", "Voxtral Mini Transcribe 2", "mistral", "voxtral-mini-2602", minute_cost_microdollars=_PRICES["minute_prices"]["mistralai/voxtral-mini-2602"], pricing_source="https://docs.mistral.ai/models/voxtral-mini-transcribe-26-02"),
        TranscriptionModel("mistralai/voxtral-mini-3b-2507", "Voxtral Mini 3B", "privatemode", "voxtral-mini-3b", minute_cost_microdollars=_PRICES["minute_prices"]["mistralai/voxtral-mini-3b-2507"], pricing_source="https://docs.privatemode.ai/pricing/"),
        TranscriptionModel("openai/whisper-large-v3", "Whisper Large V3", "privatemode", "whisper-large-v3", minute_cost_microdollars=_PRICES["minute_prices"]["openai/whisper-large-v3"], pricing_source="https://docs.privatemode.ai/pricing/"),
    )
}
for _model in TRANSCRIPTION_MODELS.values():
    _rates = (_model.input_token_cost, _model.output_token_cost) if _model.token_billed else (_model.request_cost_microdollars or _model.minute_cost_microdollars,)
    if any(type(rate) is not int or rate <= 0 for rate in _rates):
        raise ValueError("Invalid transcription price")


def transcription_metadata(model_id: str) -> dict[str, object]:
    spec = TRANSCRIPTION_MODELS[model_id]
    return {
        "endpoint": "/v1/audio/transcriptions",
        "billing_unit": "tokens" if spec.token_billed else "request" if spec.request_cost_microdollars else "audio_seconds",
        "pricing": spec.pricing,
        "input_formats": ["mp3", "wav"],
        "response_formats": list(spec.response_formats),
        "max_audio_bytes": 7 * 1024 * 1024,
        "max_duration_seconds": 600,
        "max_decoded_audio_bytes": 24_000_000,
        "pricing_source": spec.pricing_source,
    }
