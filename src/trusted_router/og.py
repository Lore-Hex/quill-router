from __future__ import annotations

from pathlib import Path

from trusted_router.config import Settings

OG_TITLE = "TrustedRouter — One API. Every LLM. Provable privacy."
OG_DESCRIPTION = (
    "Hosted, OpenRouter-compatible router for Anthropic, OpenAI, Google, and "
    "more — prepaid or BYOK. Every prompt path terminates inside a measured "
    "Confidential Space workload. $0.01 less per million tokens."
)
OG_IMAGE_WIDTH = 1200
OG_IMAGE_HEIGHT = 630
OG_PNG_PATH = Path(__file__).parent / "static" / "og.png"

_SANS = "Inter, system-ui, -apple-system, 'Segoe UI', sans-serif"
_MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"


def og_image_svg(_settings: Settings) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{OG_IMAGE_WIDTH}" height="{OG_IMAGE_HEIGHT}" viewBox="0 0 {OG_IMAGE_WIDTH} {OG_IMAGE_HEIGHT}" role="img" aria-label="TrustedRouter">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#0a1422"/>
      <stop offset="55%" stop-color="#101f33"/>
      <stop offset="100%" stop-color="#16334a"/>
    </linearGradient>
    <radialGradient id="glow" cx="100%" cy="0%" r="65%">
      <stop offset="0%" stop-color="#19a06d" stop-opacity="0.36"/>
      <stop offset="55%" stop-color="#2c6ecb" stop-opacity="0.10"/>
      <stop offset="100%" stop-color="#000000" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="mark" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#2c6ecb"/>
      <stop offset="100%" stop-color="#19a06d"/>
    </linearGradient>
    <linearGradient id="accent" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#2c6ecb"/>
      <stop offset="100%" stop-color="#19a06d"/>
    </linearGradient>
  </defs>

  <rect width="{OG_IMAGE_WIDTH}" height="{OG_IMAGE_HEIGHT}" fill="url(#bg)"/>
  <rect width="{OG_IMAGE_WIDTH}" height="{OG_IMAGE_HEIGHT}" fill="url(#glow)"/>
  <rect x="0" y="0" width="{OG_IMAGE_WIDTH}" height="6" fill="url(#accent)"/>

  <!-- Brand mark + wordmark -->
  <g transform="translate(80 76)" font-family="{_SANS}">
    <rect width="56" height="56" rx="12" fill="url(#mark)"/>
    <text x="28" y="38" font-size="22" font-weight="800" fill="#ffffff" text-anchor="middle">TR</text>
    <text x="74" y="36" font-size="22" font-weight="700" fill="#cfe1f1" letter-spacing="0.2">TrustedRouter</text>
  </g>

  <!-- Pricing badge, top-right -->
  <g transform="translate({OG_IMAGE_WIDTH - 80} 92)" font-family="{_SANS}" text-anchor="end">
    <rect x="-296" y="-26" width="296" height="44" rx="22" fill="#0e2a1d" stroke="#1f6447"/>
    <text x="-148" y="3" font-size="17" font-weight="700" fill="#7be0b1" text-anchor="middle">$0.01 less per 1M tokens</text>
  </g>

  <!-- Headline -->
  <g font-family="{_SANS}">
    <text x="80" y="244" font-size="78" font-weight="800" fill="#ffffff" letter-spacing="-1.2">One API.</text>
    <text x="80" y="332" font-size="78" font-weight="800" fill="#ffffff" letter-spacing="-1.2">Every LLM.</text>
    <text x="80" y="424" font-size="62" font-weight="800" letter-spacing="-1.0">
      <tspan fill="#7be0b1">Provable</tspan>
      <tspan fill="#ffffff" dx="14">privacy.</tspan>
    </text>
  </g>

  <!-- Provider chips -->
  <g transform="translate(80 488)" font-family="{_SANS}" font-size="18" font-weight="600" fill="#bdd2e6">
    <g>
      <rect x="0" y="-22" width="118" height="36" rx="18" fill="#0e1f30" stroke="#1f3a55"/>
      <text x="59" y="3" text-anchor="middle">Anthropic</text>
    </g>
    <g transform="translate(132 0)">
      <rect x="0" y="-22" width="92" height="36" rx="18" fill="#0e1f30" stroke="#1f3a55"/>
      <text x="46" y="3" text-anchor="middle">OpenAI</text>
    </g>
    <g transform="translate(238 0)">
      <rect x="0" y="-22" width="92" height="36" rx="18" fill="#0e1f30" stroke="#1f3a55"/>
      <text x="46" y="3" text-anchor="middle">Google</text>
    </g>
    <g transform="translate(344 0)">
      <rect x="0" y="-22" width="100" height="36" rx="18" fill="#0e1f30" stroke="#1f3a55"/>
      <text x="50" y="3" text-anchor="middle">Cerebras</text>
    </g>
    <g transform="translate(458 0)">
      <rect x="0" y="-22" width="84" height="36" rx="18" fill="#0e1f30" stroke="#1f3a55"/>
      <text x="42" y="3" text-anchor="middle">+ more</text>
    </g>
  </g>

  <!-- Bottom row: API endpoint + trust attestation pill -->
  <g transform="translate(80 568)" font-family="{_MONO}">
    <rect x="-12" y="-26" width="384" height="42" rx="10" fill="#0d1f33" stroke="#22436a"/>
    <text x="8" y="3" font-size="18" fill="#9bbcd8">api.quillrouter.com</text>
  </g>
  <g transform="translate({OG_IMAGE_WIDTH - 80} 568)" font-family="{_SANS}" text-anchor="end">
    <rect x="-330" y="-26" width="330" height="42" rx="21" fill="#0e2a1d" stroke="#1f6447"/>
    <text x="-165" y="3" font-size="16" font-weight="700" fill="#7be0b1" text-anchor="middle">attested · no prompt logs</text>
  </g>
</svg>
"""


if __name__ == "__main__":
    # Regenerate src/trusted_router/static/og.png from the SVG source:
    #   uv run python -m trusted_router.og \
    #     | rsvg-convert -w 1200 -h 630 -f png \
    #     > src/trusted_router/static/og.png
    import sys

    sys.stdout.write(og_image_svg(Settings()))
