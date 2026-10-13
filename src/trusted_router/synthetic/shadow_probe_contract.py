"""Fixed request matrix and private sample names for the shadow probe."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Shape:
    provider: str
    model: str
    route: str
    stream: bool

    @property
    def name(self) -> str:
        route = "chat" if self.route == "/chat/completions" else "responses"
        mode = "stream" if self.stream else "buffered"
        return f"async_settle_probe_{self.provider}_{route}_{mode}"

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "provider": {"only": [self.provider], "allow_fallbacks": False},
            "stream": self.stream,
        }
        if self.route == "/chat/completions":
            body.update(messages=[{"role": "user", "content": "Reply OK."}], max_tokens=8)
        else:
            body.update(input="Reply OK.", max_output_tokens=8)
        return body

    def validate_body(self, body: dict[str, Any]) -> None:
        if body != self.body():
            raise ValueError("shadow probe body must match its fixed shape")


SHAPES = tuple(
    Shape(provider, model, route, stream)
    for provider, model in (
        ("openai", "openai/gpt-4.1-mini"),
        ("anthropic", "anthropic/claude-haiku-4.5"),
    )
    for route in ("/chat/completions", "/responses")
    for stream in (False, True)
)
PROBE_NAMES = frozenset(shape.name for shape in SHAPES)

