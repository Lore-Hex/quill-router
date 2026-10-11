"""Static NYTE privacy seals for eligible enterprise confidential AI features."""

from pathlib import Path


def badge_svg(theme: str) -> str:
    assert theme in {"light", "dark"}
    bg, ink, secondary, line, gold = (
        ("#f7f5ee", "#10291e", "#335444", "#bac7bd", "#7b601f")
        if theme == "light"
        else ("#0b1711", "#f1eddf", "#b8d6c3", "#3b5042", "#d6bd7d")
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="320" height="88" viewBox="0 0 320 88" role="img" aria-labelledby="title desc">
  <title id="title">NYTE Privacy Secured — Confidential AI</title>
  <desc id="desc">For eligible enterprise AI features enforcing confidential routing. See linked route requirements. This is not a product-wide security certification.</desc>
  <rect x=".5" y=".5" width="319" height="87" rx="6" fill="{bg}" stroke="{line}"/>
  <g id="privacy-lock" fill="none" stroke="{gold}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
    <path d="M31 39V29a13 13 0 0 1 26 0v10"/>
    <rect x="23" y="39" width="42" height="33" rx="6"/>
    <circle cx="44" cy="52" r="3" fill="{gold}" stroke="none"/>
    <path d="M44 55v7"/>
  </g>
  <path d="M87 17v54" stroke="{line}"/>
  <text x="104" y="32" fill="{ink}" font-family="Georgia, 'Times New Roman', serif" font-size="27" letter-spacing="2">NYTE</text>
  <text x="105" y="45" fill="{secondary}" font-family="Arial, Helvetica, sans-serif" font-size="8" letter-spacing=".9">NEW YORK TOKEN EXCHANGE</text>
  <text x="104" y="65" fill="{gold}" font-family="Arial, Helvetica, sans-serif" font-size="13" font-weight="700" letter-spacing="1.3">PRIVACY SECURED</text>
  <text x="105" y="79" fill="{secondary}" font-family="Arial, Helvetica, sans-serif" font-size="8" letter-spacing="1.5">CONFIDENTIAL AI</text>
</svg>
'''


def build_badges(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    # Rebuilding an existing output directory must not re-publish retired marks.
    for theme in ("light", "dark"):
        (output / f"nyte-provider-{theme}.svg").unlink(missing_ok=True)
    for theme in ("light", "dark"):
        (output / f"nyte-enterprise-{theme}.svg").write_text(badge_svg(theme))
