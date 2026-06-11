from __future__ import annotations

from pathlib import Path

from trusted_router.config import Settings

OG_TITLE = "TrustedRouter | End-to-End Encrypted Router for AI"
OG_DESCRIPTION = (
    "A verifiable AI gateway for hundreds of models. Route through attested "
    "infrastructure with ZDR options, provider failover, BYOK, and no prompt "
    "or output logs by default."
)
OG_IMAGE_WIDTH = 1200
OG_IMAGE_HEIGHT = 630
OG_PNG_PATH = Path(__file__).parent / "static" / "og.png"

_SANS = "Inter, system-ui, -apple-system, 'Segoe UI', sans-serif"
_MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"


def og_image_svg(_settings: Settings) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{OG_IMAGE_WIDTH}" height="{OG_IMAGE_HEIGHT}" viewBox="0 0 {OG_IMAGE_WIDTH} {OG_IMAGE_HEIGHT}" role="img" aria-label="TrustedRouter">
  <title>TrustedRouter End-to-End Encrypted Router for AI</title>
  <desc>Hundreds of models through one verifiable prompt path with no prompt or output logs by default.</desc>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#07131f"/>
      <stop offset="54%" stop-color="#102236"/>
      <stop offset="100%" stop-color="#173d49"/>
    </linearGradient>
    <radialGradient id="glow" cx="78%" cy="8%" r="76%">
      <stop offset="0%" stop-color="#19a06d" stop-opacity="0.34"/>
      <stop offset="48%" stop-color="#2c6ecb" stop-opacity="0.12"/>
      <stop offset="100%" stop-color="#000000" stop-opacity="0"/>
    </radialGradient>
    <pattern id="grid" width="44" height="44" patternUnits="userSpaceOnUse">
      <path d="M 44 0 L 0 0 0 44" fill="none" stroke="#dff5ef" stroke-opacity="0.055" stroke-width="1"/>
    </pattern>
    <linearGradient id="mark" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#2c6ecb"/>
      <stop offset="100%" stop-color="#19a06d"/>
    </linearGradient>
    <linearGradient id="accent" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#2c6ecb"/>
      <stop offset="100%" stop-color="#19a06d"/>
    </linearGradient>
    <linearGradient id="receipt" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#f8fbff"/>
      <stop offset="100%" stop-color="#e9fff4"/>
    </linearGradient>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="20" stdDeviation="22" flood-color="#020b14" flood-opacity="0.30"/>
    </filter>
  </defs>

  <rect width="{OG_IMAGE_WIDTH}" height="{OG_IMAGE_HEIGHT}" fill="url(#bg)"/>
  <rect width="{OG_IMAGE_WIDTH}" height="{OG_IMAGE_HEIGHT}" fill="url(#glow)"/>
  <rect width="{OG_IMAGE_WIDTH}" height="{OG_IMAGE_HEIGHT}" fill="url(#grid)"/>
  <rect x="0" y="0" width="{OG_IMAGE_WIDTH}" height="6" fill="url(#accent)"/>

  <!-- Brand mark + wordmark -->
  <g transform="translate(80 76)" font-family="{_SANS}">
    <rect width="56" height="56" rx="12" fill="url(#mark)" filter="url(#shadow)"/>
    <text x="28" y="38" font-size="22" font-weight="800" fill="#ffffff" text-anchor="middle">TR</text>
    <text x="74" y="36" font-size="22" font-weight="700" fill="#cfe1f1" letter-spacing="0.2">TrustedRouter</text>
  </g>

  <!-- Headline -->
  <g font-family="{_SANS}">
    <text x="80" y="220" font-size="60" font-weight="850" fill="#ffffff">End-to-End</text>
    <text x="80" y="294" font-size="60" font-weight="850" fill="#ffffff">Encrypted Router</text>
    <text x="80" y="368" font-size="60" font-weight="850" fill="#7be0b1">for AI.</text>
    <text x="82" y="424" font-size="22" font-weight="600" fill="#cfe1f1">Hundreds of models. One verifiable prompt path.</text>
  </g>

  <!-- Proof chips -->
  <g transform="translate(80 486)" font-family="{_SANS}" font-size="17" font-weight="700" fill="#d6e8f7">
    <g>
      <rect x="0" y="-24" width="164" height="40" rx="20" fill="#0e2a1d" stroke="#1f6447"/>
      <text x="82" y="3" text-anchor="middle" fill="#8df0bf">no prompt logs</text>
    </g>
    <g transform="translate(178 0)">
      <rect x="0" y="-24" width="158" height="40" rx="20" fill="#0d1f33" stroke="#315f91"/>
      <text x="79" y="3" text-anchor="middle">ZDR routes</text>
    </g>
    <g transform="translate(350 0)">
      <rect x="0" y="-24" width="126" height="40" rx="20" fill="#0d1f33" stroke="#315f91"/>
      <text x="63" y="3" text-anchor="middle">failover</text>
    </g>
    <g transform="translate(490 0)">
      <rect x="0" y="-24" width="78" height="40" rx="20" fill="#0d1f33" stroke="#315f91"/>
      <text x="39" y="3" text-anchor="middle">BYOK</text>
    </g>
  </g>

  <!-- Verification panel -->
  <g transform="translate(726 86)" font-family="{_SANS}" filter="url(#shadow)">
    <rect x="0" y="0" width="394" height="448" rx="28" fill="url(#receipt)" stroke="#ffffff" stroke-opacity="0.72"/>
    <rect x="24" y="24" width="346" height="64" rx="18" fill="#ffffff" stroke="#d7eadf"/>
    <circle cx="62" cy="56" r="18" fill="#19a06d"/>
    <path d="M52 56 L59 63 L73 48" fill="none" stroke="#ffffff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
    <text x="94" y="52" font-size="18" font-weight="800" fill="#102236">Attested gateway</text>
    <text x="94" y="74" font-size="13" font-weight="700" fill="#5e788a">verify code, digest, and region</text>

    <g transform="translate(34 132)" font-size="16" font-weight="760">
      <g>
        <circle cx="12" cy="0" r="10" fill="#19a06d"/>
        <path d="M7 0 L11 4 L18 -5" fill="none" stroke="#ffffff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
        <text x="34" y="5" fill="#102236">Open source control plane</text>
      </g>
      <g transform="translate(0 52)">
        <circle cx="12" cy="0" r="10" fill="#19a06d"/>
        <path d="M7 0 L11 4 L18 -5" fill="none" stroke="#ffffff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
        <text x="34" y="5" fill="#102236">No prompt or output logs</text>
      </g>
      <g transform="translate(0 104)">
        <circle cx="12" cy="0" r="10" fill="#19a06d"/>
        <path d="M7 0 L11 4 L18 -5" fill="none" stroke="#ffffff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
        <text x="34" y="5" fill="#102236">Provider rollover built in</text>
      </g>
      <g transform="translate(0 156)">
        <circle cx="12" cy="0" r="10" fill="#19a06d"/>
        <path d="M7 0 L11 4 L18 -5" fill="none" stroke="#ffffff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
        <text x="34" y="5" fill="#102236">ZDR and E2E route aliases</text>
      </g>
    </g>

    <g transform="translate(28 350)">
      <rect x="0" y="0" width="338" height="66" rx="17" fill="#0b1c2d"/>
      <text x="18" y="41" font-family="{_MONO}" font-size="15" font-weight="700" fill="#a9d5f5">base_url=https://api.quillrouter.com/v1</text>
    </g>
  </g>

  <!-- Footer -->
  <g transform="translate(80 572)" font-family="{_SANS}">
    <text x="0" y="0" font-size="18" font-weight="760" fill="#cfe1f1">trustedrouter.com</text>
    <text x="176" y="0" font-size="18" font-weight="760" fill="#7be0b1">open source</text>
    <text x="304" y="0" font-size="18" font-weight="760" fill="#cfe1f1">attested</text>
    <text x="396" y="0" font-size="18" font-weight="760" fill="#cfe1f1">hundreds of models</text>
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
