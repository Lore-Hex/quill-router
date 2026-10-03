"""Export the approved marks and package public assets. Requires rsvg-convert."""

import shutil
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/trusted_router/static"
OUTPUT = STATIC / "media-kit"

COPY = """TrustedRouter
Privacy with proof.

TrustedRouter gives developers and enterprises one OpenAI-compatible API for hundreds of AI models, with provider fallback and privacy controls backed by an attested gateway.

TrustedRouter is operated by Lore Hex Corp, a Delaware corporation founded by Joseph Perla. Teams use one API to compare models, manage costs, and route across providers. The gateway runs in confidential compute and publishes hardware-attestation evidence that customers and their agents can verify. TrustedRouter keeps no prompt or output logs. Upstream provider retention and confidential-compute protections depend on the selected route.

Website: https://trustedrouter.com
Media kit: https://trustedrouter.com/media-kit
Contact: help@trustedrouter.com
Founder and CEO: Joseph Perla
Company details: https://trustedrouter.com/about
Provider privacy policies: https://trustedrouter.com/providers
Live trust evidence: https://trustedrouter.com/trust
The Token Exchange: https://thetokenexchange.com
New York Token Exchange: https://nytokenexchange.com

Use these assets to identify TrustedRouter in articles, integrations, videos, and partner materials. Keep the proportions and colors intact, leave clear space around the mark, and choose a contrasting background. Logos identify the brand; they are not certifications or endorsements. All trademark rights remain with Lore Hex Corp.
"""


def build() -> None:
    renderer = shutil.which("rsvg-convert")
    if renderer is None:
        raise SystemExit("Install librsvg (rsvg-convert) to export the transparent PNGs.")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    files = []
    for name, source in {
        "light": "trustedrouter-mark.svg",
        "dark": "trustedrouter-mark-dark.svg",
        "mono": "trustedrouter-mark-mono.svg",
    }.items():
        svg = OUTPUT / f"trustedrouter-{name}.svg"
        png = svg.with_suffix(".png")
        shutil.copyfile(STATIC / source, svg)
        subprocess.run([renderer, "--width", "1024", "--height", "1024", "--output", str(png), str(svg)], check=True)  # noqa: S603
        files.extend([svg, png])
    for source, target in {
        STATIC / "og.png": "trustedrouter-social.png",
        ROOT / "sites/token-exchange/social-images/og-global.png": "token-exchange-social.png",
        ROOT / "sites/token-exchange/social-images/og-new-york.png": "ny-token-exchange-social.png",
    }.items():
        dest = OUTPUT / target
        shutil.copyfile(source, dest)
        files.append(dest)
    about = OUTPUT / "about-trustedrouter.txt"
    about.write_text(COPY)
    files.append(about)
    with ZipFile(OUTPUT / "trustedrouter-media-kit.zip", "w", compression=ZIP_DEFLATED) as archive:
        for file in sorted(files):
            entry = ZipInfo(file.name, date_time=(2026, 10, 3, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, file.read_bytes())
    print(f"Packaged {len(files)} public assets in {OUTPUT}")


if __name__ == "__main__":
    build()
