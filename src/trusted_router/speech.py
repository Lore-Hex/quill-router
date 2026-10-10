"""Speech catalog and admission; never accepts speech text."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from trusted_router.pricing import customer_fixed_price_microdollars


@dataclass(frozen=True)
class SpeechModel:
    id: str
    name: str
    provider: str
    upstream_id: str
    cost_microdollars_per_million_characters: int
    voices: tuple[str, ...]
    formats: tuple[str, ...]
    max_characters: int
    min_speed: float
    max_speed: float
    pricing_source: str
    input_token_cost: int = 0
    output_token_cost: int = 0

    @property
    def token_billed(self) -> bool:
        return self.output_token_cost > 0

    @property
    def customer_rate(self) -> int:
        return customer_fixed_price_microdollars(self.cost_microdollars_per_million_characters)

    def quote(self, characters: int) -> int:
        if type(characters) is not int or not 1 <= characters <= self.max_characters:
            raise ValueError("speech input character count is outside the model limit")
        return 0 if self.token_billed else (characters * self.customer_rate + 999_999) // 1_000_000


def _load_prices() -> dict[str, int]:
    snapshot = json.loads((Path(__file__).parent / "data/speech_pricing.json").read_text())
    if snapshot.get("unit") != "microdollars_per_million_input_characters":
        raise ValueError("Speech pricing snapshot has the wrong unit")
    prices = snapshot["prices"]
    if not isinstance(prices, dict) or any(
        type(rate) is not int or rate <= 0 for rate in prices.values()
    ):
        raise ValueError("Speech pricing must be positive integer microdollars")
    return prices


_PRICES = _load_prices()
_TOKEN_PRICES = json.loads((Path(__file__).parent / "data/speech_pricing.json").read_text())[
    "token_prices"
]
GEMINI_INPUT_LIMIT = 8192
GEMINI_OUTPUT_LIMIT = 16384
GEMINI_VOICES = tuple(
    "Zephyr Puck Charon Kore Fenrir Leda Orus Aoede Callirrhoe Autonoe Enceladus Iapetus Umbriel Algieba Despina Erinome Algenib Rasalgethi Laomedeia Achernar Alnilam Schedar Gacrux Pulcherrima Achird Zubenelgenubi Vindemiatrix Sadachbia Sadaltager Sulafat".split()
)
for _rates in _TOKEN_PRICES.values():
    if set(_rates) != {"input", "output"} or any(
        type(v) is not int or v <= 0 for v in _rates.values()
    ):
        raise ValueError("Speech token pricing must be positive integer microdollars")

SPEECH_MODELS = {
    spec.id: spec
    for spec in (
        SpeechModel(
            "x-ai/grok-voice-tts-1.0",
            "xAI: Grok Voice TTS 1.0",
            "grok",
            "grok-voice-tts-1.0",
            _PRICES["x-ai/grok-voice-tts-1.0"],
            ("eve", "ara", "rex", "sal", "leo"),
            ("mp3", "pcm"),
            60_000,
            0.7,
            1.5,
            "https://docs.x.ai/developers/models/text-to-speech",
        ),
        SpeechModel(
            "mistralai/voxtral-mini-tts-2603",
            "Mistral: Voxtral Mini TTS",
            "mistral",
            "voxtral-mini-tts-2603",
            _PRICES["mistralai/voxtral-mini-tts-2603"],
            ("en_paul_neutral",),
            ("mp3",),
            10_000,
            1.0,
            1.0,
            "https://docs.mistral.ai/inference/pricing",
        ),
    )
}
for _model in ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"):
    _id = "google/" + _model
    SPEECH_MODELS[_id] = SpeechModel(
        _id,
        "Google: " + _model.replace("-", " ").title().replace("Tts", "TTS"),
        "google-ai-studio",
        _model,
        0,
        GEMINI_VOICES,
        ("pcm", "wav"),
        8192,
        1,
        1,
        "https://ai.google.dev/gemini-api/docs/pricing",
        _TOKEN_PRICES[_id]["input"],
        _TOKEN_PRICES[_id]["output"],
    )

for _slug, _native, _limit in (
    ("eleven-v3", "eleven_v3", 5000),
    ("eleven-multilingual-v2", "eleven_multilingual_v2", 10000),
    ("eleven-flash-v2.5", "eleven_flash_v2_5", 40000),
    ("eleven-turbo-v2.5", "eleven_turbo_v2_5", 40000),
):
    _id = "elevenlabs/" + _slug
    SPEECH_MODELS[_id] = SpeechModel(
        _id,
        "ElevenLabs: " + _slug.replace("-", " ").title(),
        "elevenlabs",
        _native,
        _PRICES[_id],
        ("JBFqnCBsd6RMkjVDRZzb",),
        ("pcm", "mp3"),
        _limit,
        1,
        1,
        "https://elevenlabs.io/pricing/api",
    )

for _slug, _native in (
    ("mai-voice-2.1", "MAI-Voice-2.1"),
    ("mai-voice-2.1-flash", "MAI-Voice-2.1-Flash"),
):
    _id = "microsoft/" + _slug
    SPEECH_MODELS[_id] = SpeechModel(
        _id,
        "Microsoft: " + _native,
        "azure",
        _native,
        _PRICES[_id],
        ("en-US-Harper", "en-US-Grant"),
        ("pcm", "mp3"),
        5000,
        1,
        1,
        "https://microsoft.ai/models/mai-voice-2-1/",
    )


def speech_metadata(model_id: str) -> dict[str, object]:
    spec = SPEECH_MODELS[model_id]
    return {
        "endpoint": "/v1/audio/speech",
        "billing_unit": "tokens" if spec.token_billed else "input_characters",
        **(
            {
                "input_token_price_per_million": str(
                    Decimal(customer_fixed_price_microdollars(spec.input_token_cost)) / 1_000_000
                ),
                "output_audio_token_price_per_million": str(
                    Decimal(customer_fixed_price_microdollars(spec.output_token_cost)) / 1_000_000
                ),
                "max_input_tokens": GEMINI_INPUT_LIMIT,
                "max_output_audio_tokens": GEMINI_OUTPUT_LIMIT,
            }
            if spec.token_billed
            else {
                "input_character_price_per_million": str(Decimal(spec.customer_rate) / 1_000_000),
            }
        ),
        "supported_voices": list(spec.voices),
        "response_formats": list(spec.formats),
        "default_response_format": "pcm",
        "max_input_characters": spec.max_characters,
        "speed": {"min": spec.min_speed, "max": spec.max_speed, "default": 1},
        "pricing_source": spec.pricing_source,
    }
