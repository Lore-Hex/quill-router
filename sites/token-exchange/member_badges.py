"""Static, script-free NYTE membership marks. Not a security certification."""

from pathlib import Path


def badge_svg(theme: str) -> str:
    assert theme in {"light", "dark"}
    bg, ink, secondary, line, gold = (
        ("#f7f5ee", "#10291e", "#335444", "#bac7bd", "#7b601f")
        if theme == "light"
        else ("#0b1711", "#f1eddf", "#b8d6c3", "#3b5042", "#d6bd7d")
    )
    label = "ENTERPRISE BUYER"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="320" height="88" viewBox="0 0 320 88" role="img" aria-labelledby="title desc">
  <title id="title">NYTE enterprise buyer</title>
  <desc id="desc">New York Token Exchange membership mark. Membership is not a security certification.</desc>
  <rect x=".5" y=".5" width="319" height="87" rx="6" fill="{bg}" stroke="{line}"/>
  <g fill="none" stroke="{gold}">
    <circle cx="44" cy="44" r="28"/>
    <circle cx="44" cy="44" r="21" stroke-opacity=".45"/>
    <path d="M44 16v5m0 46v5M16 44h5m46 0h5M24.2 24.2l3.5 3.5m32.6 32.6 3.5 3.5M24.2 63.8l3.5-3.5m32.6-32.6 3.5-3.5"/>
    <path d="M34 55V33l20 22V33M30 33h8m12 0h8M30 55h8m12 0h8" stroke-width="1.7"/>
  </g>
  <path d="M87 17v54" stroke="{line}"/>
  <text x="104" y="41" fill="{ink}" font-family="Georgia, 'Times New Roman', serif" font-size="33" letter-spacing="2">NYTE</text>
  <text x="105" y="56" fill="{secondary}" font-family="Arial, Helvetica, sans-serif" font-size="9" letter-spacing="1">NEW YORK TOKEN EXCHANGE</text>
  <path d="M105 64h12" stroke="{gold}"/>
  <text x="123" y="75" fill="{gold}" font-family="Arial, Helvetica, sans-serif" font-size="9" font-weight="700" letter-spacing="1.25">{label}</text>
</svg>
'''


def build_badges(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    for theme in ("light", "dark"):
        (output / f"nyte-enterprise-{theme}.svg").write_text(badge_svg(theme))
