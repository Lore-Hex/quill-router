from __future__ import annotations

from pathlib import Path

KEY_ALIASES: dict[str, tuple[str, ...]] = {
    "ANTHROPIC_API_KEY": ("CLAUDE_API_KEY",),
    "OPENAI_API_KEY": ("CHATGPT_API_KEY",),
    "STRIPE_SECRET_KEY": ("STRIPE_KEY",),
    "VERTEX_PROJECT_ID": ("GOOGLE_CLOUD_PROJECT", "GCP_PROJECT_ID"),
    "VERTEX_LOCATION": ("GOOGLE_CLOUD_REGION", "GCP_REGION"),
    "VERTEX_ACCESS_TOKEN": ("GOOGLE_OAUTH_ACCESS_TOKEN",),
}


class LocalKeyFile:
    """Read dotenv-style operator keys without logging values."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read_names(self) -> set[str]:
        return set(self._read().keys())

    def get(self, name: str) -> str | None:
        values = self._read()
        for candidate in (name, *KEY_ALIASES.get(name, ())):
            value = values.get(candidate)
            if value:
                return value
        return None

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        out: dict[str, str] = {}
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                out[key] = value
        return out
