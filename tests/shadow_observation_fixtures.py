"""Small standalone fixture reader shared with native restart children."""
import copy
import json
from functools import lru_cache
from pathlib import Path

NOW_US = 1791763200000000

@lru_cache
def authorize_event():
    values = json.loads((Path(__file__).parent/"fixtures/async_settlement/shadow_v2/receipt_pages.json").read_text())["variants"]
    return next(v["object"] for v in values if v["schema"] == "AUTHORIZE_EVENT" and v["expected"] == "accept")

def event_for(reservation):
    value = copy.deepcopy(authorize_event())
    value.update(seq=f"{reservation.event_seq:020}", event_at_us=reservation.observed_us,
                 receipt_at_us=reservation.observed_us, authorization_day=reservation.day,
                 receipt_locator=reservation.coordinates)
    return value
