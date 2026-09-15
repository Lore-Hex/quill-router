import threading
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

import httpx

from .money import MICRODOLLARS_PER_DOLLAR, MSATS_PER_BTC, microdollars, msats, usd

FX_MARGIN_BPS = 1000


@dataclass(frozen=True)
class Rate:
    # Persist the effective credit rate so old workers can also settle it.
    usd_per_btc: Decimal
    as_of: int
    fx_margin_bps: int = 0

    def __post_init__(self) -> None:
        if type(self.fx_margin_bps) is not int or not 0 <= self.fx_margin_bps < 10_000:
            raise ValueError("Invalid FX margin")
        if not self.usd_per_btc.is_finite() or not Decimal("1") <= self.usd_per_btc <= Decimal("100000000"):
            raise ValueError("Invalid BTC exchange rate")

    @classmethod
    def from_spot(cls, spot: Decimal, as_of: int, *, fx_margin_bps: int = FX_MARGIN_BPS) -> "Rate":
        cls(spot, as_of, fx_margin_bps)
        return cls(spot * (10_000 - fx_margin_bps) / 10_000, as_of, fx_margin_bps)

    @property
    def spot_usd_per_btc(self) -> Decimal:
        return self.usd_per_btc * 10_000 / (10_000 - self.fx_margin_bps)

    def quote_fields(self, amount_msat: int) -> dict[str, str | int]:
        gross = Rate(self.spot_usd_per_btc, self.as_of).credit_microdollars(amount_msat)
        return {"fx_margin_bps": self.fx_margin_bps, "usd_per_btc": str(self.usd_per_btc),
                "spot_usd_per_btc": str(self.spot_usd_per_btc), "invoice_spot_usd": usd(gross)}

    def invoice_msats(self, cents: int) -> int:
        # Whole sat invoices: round the USD target up once, then freeze it.
        if isinstance(cents, bool) or not 1 <= cents <= 100_000:
            raise ValueError("Amount must be $0.01 to $1,000")
        sats = (Decimal(cents) * 1_000_000 / self.usd_per_btc).to_integral_value(rounding=ROUND_CEILING)
        return msats(int(sats) * 1000)

    def credit_microdollars(self, amount_msat: int) -> int:
        # Conversion happens once, using the immutable invoice quote. Never
        # revalue account balances or inference charges when BTC prices move.
        value = Decimal(msats(amount_msat)) * self.usd_per_btc * MICRODOLLARS_PER_DOLLAR / MSATS_PER_BTC
        return microdollars(int(value.to_integral_value(rounding=ROUND_FLOOR)))


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
            self._rate = Rate.from_spot(Decimal(data["amount"]), now)
            return self._rate
