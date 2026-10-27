"""Build the reserved tokens inquiry flow from the public, dated index snapshot."""
from __future__ import annotations

import html
import runpy
from pathlib import Path

from facilities import FACILITY_RATE, facility_cards

HERE = Path(__file__).resolve().parent


def buy_content() -> str:
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
    return ((HERE / "nyte-v2/buy.html").read_text()
            .replace("{{grades}}", "".join(cards))
            .replace("{{facilities}}", facility_cards(selectable=True))
            .replace("{{facility_rate}}", FACILITY_RATE)
            .replace("{{as_of}}", html.escape(data["AS_OF"]))
            .replace("{{as_of_iso}}", data["AS_OF_ISO"]))
