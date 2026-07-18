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


def choose_og_image_svg(_settings: Settings) -> str:
    """Social card for /choose — the iron-triangle model picker. Same brand
    chrome as og_image_svg (bg gradient, TR wordmark, footer), with the
    smart/cheap/fast ternary chart as the hero motif. No emoji: rsvg-convert
    has no colour-emoji support, so vertex meaning is carried by labels."""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{OG_IMAGE_WIDTH}" height="{OG_IMAGE_HEIGHT}" viewBox="0 0 {OG_IMAGE_WIDTH} {OG_IMAGE_HEIGHT}" role="img" aria-label="Choose a model on the iron triangle">
  <title>Choose a model — smart, cheap, fast</title>
  <desc>TrustedRouter compares independently scored models with exact live provider route facts.</desc>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#07131f"/>
      <stop offset="54%" stop-color="#102236"/>
      <stop offset="100%" stop-color="#173d49"/>
    </linearGradient>
    <radialGradient id="glow" cx="74%" cy="14%" r="74%">
      <stop offset="0%" stop-color="#19a06d" stop-opacity="0.30"/>
      <stop offset="48%" stop-color="#2c6ecb" stop-opacity="0.12"/>
      <stop offset="100%" stop-color="#000000" stop-opacity="0"/>
    </radialGradient>
    <pattern id="grid" width="44" height="44" patternUnits="userSpaceOnUse">
      <path d="M 44 0 L 0 0 0 44" fill="none" stroke="#dff5ef" stroke-opacity="0.055" stroke-width="1"/>
    </pattern>
    <linearGradient id="mark" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#2c6ecb"/><stop offset="100%" stop-color="#19a06d"/>
    </linearGradient>
    <linearGradient id="accent" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#2c6ecb"/><stop offset="100%" stop-color="#19a06d"/>
    </linearGradient>
    <linearGradient id="triedge" x1="0.5" y1="0" x2="0.12" y2="1">
      <stop offset="0%" stop-color="#a78bfa"/>
      <stop offset="55%" stop-color="#34d399"/>
      <stop offset="100%" stop-color="#f59e0b"/>
    </linearGradient>
    <radialGradient id="floor" cx="50%" cy="58%" r="62%">
      <stop offset="0%" stop-color="#16273f" stop-opacity="0.85"/>
      <stop offset="100%" stop-color="#0a1424" stop-opacity="0.12"/>
    </radialGradient>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="20" stdDeviation="22" flood-color="#020b14" flood-opacity="0.30"/>
    </filter>
    <filter id="dotglow" x="-160%" y="-160%" width="420%" height="420%">
      <feGaussianBlur stdDeviation="5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
  </defs>

  <rect width="{OG_IMAGE_WIDTH}" height="{OG_IMAGE_HEIGHT}" fill="url(#bg)"/>
  <rect width="{OG_IMAGE_WIDTH}" height="{OG_IMAGE_HEIGHT}" fill="url(#glow)"/>
  <rect width="{OG_IMAGE_WIDTH}" height="{OG_IMAGE_HEIGHT}" fill="url(#grid)"/>
  <rect x="0" y="0" width="{OG_IMAGE_WIDTH}" height="6" fill="url(#accent)"/>

  <!-- Brand mark + wordmark -->
  <g transform="translate(80 72)" font-family="{_SANS}">
    <rect width="56" height="56" rx="12" fill="url(#mark)" filter="url(#shadow)"/>
    <text x="28" y="38" font-size="22" font-weight="800" fill="#ffffff" text-anchor="middle">TR</text>
    <text x="74" y="36" font-size="22" font-weight="700" fill="#cfe1f1" letter-spacing="0.2">TrustedRouter</text>
  </g>

  <!-- Headline -->
  <g font-family="{_SANS}">
    <text x="80" y="214" font-size="21" font-weight="800" fill="#7be0b1" letter-spacing="3">THE IRON TRIANGLE OF LLMs</text>
    <text x="80" y="292" font-size="64" font-weight="850" fill="#ffffff">Choose a model.</text>
    <text x="80" y="362" font-size="46" font-weight="850"><tspan fill="#a78bfa">Smart</tspan><tspan fill="#5e7290"> &#183; </tspan><tspan fill="#34d399">Cheap</tspan><tspan fill="#5e7290"> &#183; </tspan><tspan fill="#f59e0b">Fast</tspan><tspan fill="#cfe1f1" font-weight="700" font-size="34"> &#8212; pick two.</tspan></text>
    <text x="82" y="414" font-size="22" font-weight="600" fill="#cfe1f1">Independent quality. Exact route price, privacy, and speed.</text>
  </g>

  <!-- Footer -->
  <g transform="translate(80 566)" font-family="{_SANS}">
    <text x="0" y="0" font-size="19" font-weight="800" fill="#ffffff">trustedrouter.com<tspan fill="#7be0b1">/choose</tspan></text>
    <text x="276" y="0" font-size="18" font-weight="740" fill="#9fb6cf">Open &#183; ZDR &#183; TEE &#183; attested</text>
  </g>

  <!-- Iron triangle -->
  <g>
    <polygon points="890,168 700,496 1080,496" fill="url(#floor)"/>
    <polygon points="890,168 700,496 1080,496" fill="none" stroke="url(#triedge)" stroke-width="3" stroke-linejoin="round" opacity="0.92"/>
    <g stroke="#0a1120" stroke-width="1.4">
      <circle cx="892" cy="252" r="10" fill="#22d3ee" filter="url(#dotglow)"/>
      <circle cx="853" cy="305" r="8" fill="#8aa0c4"/>
      <circle cx="935" cy="298" r="9" fill="#22c55e"/>
      <circle cx="905" cy="348" r="9" fill="#22d3ee"/>
      <circle cx="835" cy="384" r="7" fill="#8aa0c4"/>
      <circle cx="967" cy="378" r="9" fill="#22c55e" filter="url(#dotglow)"/>
      <circle cx="878" cy="410" r="7" fill="#8aa0c4"/>
      <circle cx="998" cy="432" r="8" fill="#22d3ee"/>
      <circle cx="788" cy="470" r="9" fill="#22c55e"/>
      <circle cx="1016" cy="468" r="9" fill="#22d3ee" filter="url(#dotglow)"/>
      <circle cx="905" cy="462" r="7" fill="#8aa0c4"/>
      <circle cx="845" cy="452" r="6" fill="#8aa0c4"/>
    </g>
    <g transform="translate(930 400)">
      <circle r="22" fill="#fbbf24" fill-opacity="0.12"/>
      <circle r="13" fill="none" stroke="#fbbf24" stroke-width="2.6" filter="url(#shadow)"/>
      <circle r="4.5" fill="#fbbf24"/>
      <line x1="-19" y1="0" x2="-13" y2="0" stroke="#fbbf24" stroke-width="2.4" stroke-linecap="round"/>
      <line x1="19" y1="0" x2="13" y2="0" stroke="#fbbf24" stroke-width="2.4" stroke-linecap="round"/>
      <line x1="0" y1="-19" x2="0" y2="-13" stroke="#fbbf24" stroke-width="2.4" stroke-linecap="round"/>
      <line x1="0" y1="19" x2="0" y2="13" stroke="#fbbf24" stroke-width="2.4" stroke-linecap="round"/>
      <text x="0" y="-26" text-anchor="middle" font-family="{_SANS}" font-size="14" font-weight="800" fill="#fbbf24" letter-spacing="1.5">YOU</text>
    </g>
    <g font-family="{_SANS}" font-weight="800" letter-spacing="1">
      <text x="890" y="144" text-anchor="middle" font-size="26" fill="#a78bfa">SMART</text>
      <text x="700" y="532" text-anchor="middle" font-size="26" fill="#34d399">CHEAP</text>
      <text x="1080" y="532" text-anchor="middle" font-size="26" fill="#f59e0b">FAST</text>
    </g>
  </g>
</svg>
"""


def synth_og_image_svg(_settings: Settings) -> str:
    """Social card for /synth: panel inputs flowing through judge and
    synthesizer. Kept as deterministic SVG so preview text stays crisp."""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{OG_IMAGE_WIDTH}" height="{OG_IMAGE_HEIGHT}" viewBox="0 0 {OG_IMAGE_WIDTH} {OG_IMAGE_HEIGHT}" role="img" aria-label="TrustedRouter Synth">
  <title>TrustedRouter Synth</title>
  <desc>Compare a model panel, stream raw thinking, and return one answer through the attested gateway.</desc>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#07131f"/>
      <stop offset="50%" stop-color="#102236"/>
      <stop offset="100%" stop-color="#132f45"/>
    </linearGradient>
    <radialGradient id="glowA" cx="73%" cy="18%" r="66%">
      <stop offset="0%" stop-color="#19a06d" stop-opacity="0.34"/>
      <stop offset="52%" stop-color="#2c6ecb" stop-opacity="0.12"/>
      <stop offset="100%" stop-color="#000000" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="glowB" cx="88%" cy="80%" r="52%">
      <stop offset="0%" stop-color="#8b5cf6" stop-opacity="0.20"/>
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
    <linearGradient id="card" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#101c2f"/>
      <stop offset="100%" stop-color="#0a1322"/>
    </linearGradient>
    <linearGradient id="answer" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#f8fbff"/>
      <stop offset="100%" stop-color="#eafff4"/>
    </linearGradient>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="20" stdDeviation="22" flood-color="#020b14" flood-opacity="0.32"/>
    </filter>
    <filter id="softglow" x="-120%" y="-120%" width="340%" height="340%">
      <feGaussianBlur stdDeviation="7" result="blur"/>
      <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
    </filter>
    <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" fill="#69d6a4"/>
    </marker>
  </defs>

  <rect width="{OG_IMAGE_WIDTH}" height="{OG_IMAGE_HEIGHT}" fill="url(#bg)"/>
  <rect width="{OG_IMAGE_WIDTH}" height="{OG_IMAGE_HEIGHT}" fill="url(#glowA)"/>
  <rect width="{OG_IMAGE_WIDTH}" height="{OG_IMAGE_HEIGHT}" fill="url(#glowB)"/>
  <rect width="{OG_IMAGE_WIDTH}" height="{OG_IMAGE_HEIGHT}" fill="url(#grid)"/>
  <rect x="0" y="0" width="{OG_IMAGE_WIDTH}" height="6" fill="url(#accent)"/>

  <g transform="translate(80 72)" font-family="{_SANS}">
    <rect width="56" height="56" rx="12" fill="url(#mark)" filter="url(#shadow)"/>
    <text x="28" y="38" font-size="22" font-weight="800" fill="#ffffff" text-anchor="middle">TR</text>
    <text x="74" y="36" font-size="22" font-weight="700" fill="#cfe1f1">TrustedRouter</text>
  </g>

  <g font-family="{_SANS}">
    <text x="80" y="205" font-size="21" font-weight="800" fill="#7be0b1" letter-spacing="3">TRUSTEDROUTER SYNTH</text>
    <text x="80" y="286" font-size="64" font-weight="850" fill="#ffffff">Compare a panel.</text>
    <text x="80" y="360" font-size="64" font-weight="850" fill="#ffffff">Return one answer.</text>
    <text x="82" y="418" font-size="22" font-weight="650" fill="#cfe1f1">Panel · judge · synthesizer · fallbacks inside the attested gateway.</text>
  </g>

  <g transform="translate(80 488)" font-family="{_SANS}" font-size="17" font-weight="760">
    <g>
      <rect x="0" y="-25" width="150" height="42" rx="21" fill="#0e2a1d" stroke="#1f6447"/>
      <text x="75" y="3" text-anchor="middle" fill="#8df0bf">raw thinking</text>
    </g>
    <g transform="translate(164 0)">
      <rect x="0" y="-25" width="160" height="42" rx="21" fill="#0d1f33" stroke="#315f91"/>
      <text x="80" y="3" text-anchor="middle" fill="#d6e8f7">non-refusals</text>
    </g>
    <g transform="translate(338 0)">
      <rect x="0" y="-25" width="154" height="42" rx="21" fill="#0d1f33" stroke="#315f91"/>
      <text x="77" y="3" text-anchor="middle" fill="#d6e8f7">model fallback</text>
    </g>
  </g>

  <g transform="translate(80 572)" font-family="{_SANS}">
    <text x="0" y="0" font-size="19" font-weight="800" fill="#ffffff">trustedrouter.com<tspan fill="#7be0b1">/synth</tspan></text>
    <text x="260" y="0" font-size="18" font-weight="740" fill="#9fb6cf">OpenAI compatible · end-to-end encrypted router</text>
  </g>

  <!-- Synthesis graph -->
  <g transform="translate(610 86)" font-family="{_SANS}">
    <g opacity="0.82" fill="none" stroke="#69d6a4" stroke-width="2.4" marker-end="url(#arrow)">
      <path d="M64 96 C132 96 142 146 184 158"/>
      <path d="M64 178 C126 178 144 168 184 168"/>
      <path d="M64 260 C132 260 144 194 184 180"/>
      <path d="M286 168 C318 168 326 226 342 238"/>
    </g>

    <g filter="url(#shadow)">
      <g>
        <rect x="0" y="50" width="170" height="64" rx="16" fill="url(#card)" stroke="#2b405f"/>
        <circle cx="28" cy="82" r="14" fill="#2c6ecb"/>
        <text x="28" y="87" font-size="12" font-weight="850" fill="#ffffff" text-anchor="middle">M3</text>
        <text x="52" y="79" font-size="15" font-weight="800" fill="#f7fbff">MiniMax M3</text>
        <text x="52" y="99" font-size="11" font-weight="700" fill="#8fb1cd">panel answer</text>
      </g>
      <g>
        <rect x="0" y="132" width="170" height="64" rx="16" fill="url(#card)" stroke="#2b405f"/>
        <circle cx="28" cy="164" r="14" fill="#19a06d"/>
        <text x="28" y="169" font-size="11" font-weight="850" fill="#ffffff" text-anchor="middle">K2</text>
        <text x="52" y="161" font-size="15" font-weight="800" fill="#f7fbff">Kimi K2.7</text>
        <text x="52" y="181" font-size="11" font-weight="700" fill="#8fb1cd">judge fallback</text>
      </g>
      <g>
        <rect x="0" y="214" width="170" height="64" rx="16" fill="url(#card)" stroke="#2b405f"/>
        <circle cx="28" cy="246" r="14" fill="#8b5cf6"/>
        <text x="28" y="251" font-size="11" font-weight="850" fill="#ffffff" text-anchor="middle">G5</text>
        <text x="52" y="243" font-size="15" font-weight="800" fill="#f7fbff">GLM 5.2</text>
        <text x="52" y="263" font-size="11" font-weight="700" fill="#8fb1cd">synthesizer</text>
      </g>
    </g>

    <g filter="url(#shadow)">
      <rect x="198" y="112" width="120" height="112" rx="22" fill="#0d1f33" stroke="#4778ad"/>
      <text x="258" y="152" text-anchor="middle" font-size="18" font-weight="850" fill="#ffffff">Judge</text>
      <text x="258" y="178" text-anchor="middle" font-size="12" font-weight="750" fill="#9cc1df">filters refusals</text>
      <circle cx="258" cy="205" r="7" fill="#69d6a4" filter="url(#softglow)"/>
    </g>

    <g filter="url(#shadow)">
      <rect x="350" y="168" width="238" height="198" rx="26" fill="url(#answer)" stroke="#ffffff" stroke-opacity="0.7"/>
      <text x="378" y="214" font-size="17" font-weight="850" fill="#102236">One final answer</text>
      <text x="378" y="244" font-size="13" font-weight="750" fill="#5e788a">selected evidence</text>
      <g transform="translate(378 282)" stroke="#15304b" stroke-opacity="0.22" stroke-width="8" stroke-linecap="round">
        <path d="M0 0 H174"/>
        <path d="M0 30 H142"/>
        <path d="M0 60 H188"/>
      </g>
      <g transform="translate(378 334)" font-family="{_MONO}" font-size="13" font-weight="800" fill="#0f7d55">
        <text x="0" y="0">data: {{ thinking }}</text>
      </g>
    </g>
  </g>
</svg>
"""


if __name__ == "__main__":
    # Regenerate the social cards from their SVG sources:
    #   uv run python -m trusted_router.og \
    #     | rsvg-convert -w 1200 -h 630 -f png > src/trusted_router/static/og.png
    #   uv run python -m trusted_router.og choose \
    #     | rsvg-convert -w 1200 -h 630 -f png > src/trusted_router/static/og/choose.png
    import sys

    which = sys.argv[1] if len(sys.argv) > 1 else "default"
    if which == "choose":
        svg = choose_og_image_svg(Settings())
    elif which == "synth":
        svg = synth_og_image_svg(Settings())
    else:
        svg = og_image_svg(Settings())
    sys.stdout.write(svg)
