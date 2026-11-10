"""Select Google's dated Standard token prices before sandboxed parsing."""

import re
from datetime import UTC, date, datetime, timedelta

_SCHEDULE = re.compile(
    r"\$(\d+(?:\.\d+)?)\s+through\s+([A-Za-z]+ \d{1,2}, \d{4})\.\s*"
    r"\$(\d+(?:\.\d+)?)\s+starting\s+([A-Za-z]+ \d{1,2}, \d{4})\.",
    re.IGNORECASE,
)


def current_price_source(source: str, *, today: date | None = None) -> str:
    """Resolve adjacent dated rates; malformed schedules remain rejected by the parser."""
    today = today or datetime.now(UTC).date()

    def select(match: re.Match[str]) -> str:
        through = datetime.strptime(match[2], "%B %d, %Y").date()
        starting = datetime.strptime(match[4], "%B %d, %Y").date()
        if starting != through + timedelta(days=1):
            raise ValueError("gemini: overlapping or discontinuous price schedule")
        return "$" + (match[1] if today <= through else match[3])

    return _SCHEDULE.sub(select, source)
