"""Never pass balances through floating point or JavaScript Number."""

MSATS_PER_BTC = 100_000_000_000
MAX_MSATS = 21_000_000 * MSATS_PER_BTC
MICRODOLLARS_PER_DOLLAR = 1_000_000
MAX_MICRODOLLARS = 2**63 - 1
# Wire contract with trusted_router.storage_lightning; root CI checks parity.
MAX_CREDIT_RECEIPT = 2_002_000_000


def msats(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Expected an integer millisatoshi amount")
    if isinstance(value, str) and (not value.isascii() or not value.isdigit()):
        raise ValueError("Expected an unsigned integer string")
    amount = int(value)
    if not 0 <= amount <= MAX_MSATS:
        raise ValueError("Millisatoshi amount is outside the supported range")
    return amount


def btc(value: int) -> str:
    amount = msats(value)
    whole, fraction = divmod(amount, MSATS_PER_BTC)
    return f"{whole}.{fraction:011d}"


def microdollars(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Expected integer microdollars")
    if isinstance(value, str) and (not value.isascii() or not value.isdigit()):
        raise ValueError("Expected an unsigned integer string")
    amount = int(value)
    if not 0 <= amount <= MAX_MICRODOLLARS:
        raise ValueError("USD amount is outside the supported range")
    return amount


def usd(value: int) -> str:
    whole, fraction = divmod(microdollars(value), MICRODOLLARS_PER_DOLLAR)
    return f"{whole}.{fraction:06d}"
