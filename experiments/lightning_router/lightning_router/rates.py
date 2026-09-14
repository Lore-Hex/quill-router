import threading
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

import httpx

from .money import MSATS_PER_BTC, msats


@dataclass(frozen=True)
class Rate:
    usd_per_btc: Decimal
    as_of: int

    def __post_init__(self) -> None:
        if not self.usd_per_btc.is_finite() or not Decimal("1") <= self.usd_per_btc <= Decimal("100000000"):
            raise ValueError("Invalid BTC exchange rate")

    def invoice_msats(self, cents: int) -> int:
        # Whole sat invoices: round the USD target up once, then freeze it.
        if isinstance(cents, bool) or not 1 <= cents <= 100_000:
            raise ValueError("Amount must be $0.01 to $1,000")
        sats = (Decimal(cents) * 1_000_000 / self.usd_per_btc).to_integral_value(rounding=ROUND_CEILING)
        return msats(int(sats) * 1000)

    def usd_estimate(self, amount_msat: int) -> str:
        value = Decimal(msats(amount_msat)) * self.usd_per_btc / MSATS_PER_BTC
        return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class Rates:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self._rate: Rate | None = None
        self._lock = threading.Lock()

    def current(self) -> Rate:
        with self._lock:
            now = int(time.time())
            if self._rate and 0 <= now - self._rate.as_of < 60:
                return self._rate
            response = self.client.get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
            response.raise_for_status()
            data = response.json()["data"]
            if data["base"] != "BTC" or data["currency"] != "USD" or not isinstance(data["amount"], str):
                raise ValueError("Unexpected exchange-rate response")
            self._rate = Rate(Decimal(data["amount"]), now)
            return self._rate
