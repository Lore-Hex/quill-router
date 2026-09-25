"""Conservative USD billing for first-party EUR prices."""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal, InvalidOperation

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

ECB_FX_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FX_RESERVE = Decimal("1.05")


def usd_per_eur(xml: str) -> Decimal:
    try:
        root = ElementTree.fromstring(xml)
        values = [Decimal(node.attrib["rate"]) for node in root.iter()
                  if node.attrib.get("currency") == "USD"]
    except (ElementTree.ParseError, DefusedXmlException, KeyError, InvalidOperation) as exc:
        raise RuntimeError("ECB USD/EUR feed is invalid") from exc
    if not values:
        raise RuntimeError("ECB feed has no USD/EUR rate")
    if len(values) != 1 or not values[0].is_finite() or values[0] <= 0:
        raise RuntimeError("ECB feed must contain one positive USD/EUR rate")
    return values[0]


def eur_microdollars_per_million(eur: Decimal, rate: Decimal) -> int:
    if not eur.is_finite() or eur < 0 or not rate.is_finite() or rate <= 0:
        raise ValueError("EUR price and USD/EUR rate must be finite and nonnegative")
    return int((eur * rate * FX_RESERVE * Decimal("1000000")).to_integral_value(ROUND_CEILING))
