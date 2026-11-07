"""Build card reservations and call inquiries from the public, dated index snapshot."""
from __future__ import annotations

import html
import runpy
from pathlib import Path

from facilities import FACILITY_RATE, facility_cards

HERE = Path(__file__).resolve().parent


def _swap(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"buy.html changed; update brand_v3_layout for {old!r}")
    return text.replace(old, new)


def brand_v3_layout(body: str) -> str:
    """Brand v3 layout: the hero column keeps the kicker, headline and lede on a yellow
    field closed by the ink Band; the notes, the discussion list (shown open) and the
    fine print move to a section below it. The request flow markup is unchanged."""
    start = body.index('<div class="buy-notes">')
    end = body.index('</aside>')
    about = body[start:end].strip()
    body = body[:start].rstrip() + '\n  </div>' + body[end + len('</aside>'):]
    body = _swap(body, '<aside class="buy-brand" aria-label="About reserved tokens">', '<div class="buy-brand">')
    about = _swap(about, '<details class="buy-contract"><summary>What we’ll discuss</summary>',
                  '<div class="buy-contract"><h2 class="v3-label">What we’ll discuss</h2>')
    about = _swap(about, '</dl></details>', '</dl></div>')
    return ('<div class="buy-stage">\n' + body.rstrip() + '\n{{band_ink}}\n</div>\n'
            '<section class="buy-about" aria-label="About reserved tokens"><div class="wrap">\n'
            + about + '\n</div></section>\n')


def buy_content(*, v3: bool = False) -> str:
    data = runpy.run_path(str(HERE.parents[1] / "src/trusted_router/content/token_index.py"))
    cards = []
    for grade in data["GRADES"]:
        code = grade["code"]
        price = data["QUOTES"][code]["value"]
        cards.append(
            f'<label class="buy-choice"><input type="radio" name="grade" value="{code}"'
            f' data-price="{price}"' + (' checked' if code == 'A' else '') + '>'
            f'<span><strong>{code} {html.escape(grade["name"])}</strong>'
            f'<span class="buy-price">${price:,}<small> / billion tokens</small></span>'
            f'<small>{html.escape(grade["use"])}</small></span></label>'
        )
    body = (HERE / "nyte-v2/buy.html").read_text()
    if v3:
        body = brand_v3_layout(body)
    return (body
            .replace("{{grades}}", "".join(cards))
            .replace("{{facilities}}", facility_cards(selectable=True))
            .replace("{{facility_rate}}", FACILITY_RATE)
            .replace("{{as_of}}", html.escape(data["AS_OF"]))
            .replace("{{as_of_iso}}", data["AS_OF_ISO"]))
