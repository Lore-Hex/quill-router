"""Select Google's dated Standard token prices before sandboxed parsing."""

import re
from datetime import UTC, date, datetime, timedelta

# A rate may name the input modalities it covers, "$1.00 (text / image / video /
# audio) through ...", and Markdoc may end the first line with a "\\" hard break
# (Gemini Robotics ER 2 Preview uses both).
_MODALITIES = r"(?:\s*\([^()\n]*\))?"
_SCHEDULE = re.compile(
    r"\$(\d+(?:\.\d+)?)" + _MODALITIES + r"\s+through\s+([A-Za-z]+ \d{1,2}, \d{4})\.\s*"
    r"(?:\\\\\s*)?"
    r"\$(\d+(?:\.\d+)?)" + _MODALITIES + r"\s+starting\s+([A-Za-z]+ \d{1,2}, \d{4})\.",
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
