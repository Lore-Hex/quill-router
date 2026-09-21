"""Every copy of the closed client-telemetry host vocabulary must agree.

The vocabulary is written down six times. Twice in `client_context`: a tuple,
and a `Literal` for the settle schema, which cannot be built from the tuple
without losing the type. Once in `client_reliability`, where the beacon schema
turns it into a pattern. And three times in prose: two lines of
docs/client-telemetry.md and the public /docs/telemetry page.

A copy that lags is not a cosmetic defect. An unknown host is not a soft
failure: it drops the whole settle client context and rejects a whole beacon
batch (tests/test_settle_client_context.py and tests/test_client_events_route.py
pin both), so telemetry is lost silently for exactly the region that was left
out. The route tests prove the two code copies accept a gateway region; this
module is what makes the remaining copies move with them.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

from trusted_router.client_context import CLIENT_PREV_HOSTS, ClientPrevHost
from trusted_router.client_reliability import HOSTS
from trusted_router.enclave_regions import ENCLAVE_REGIONS

ROOT = Path(__file__).resolve().parents[1]


def _pipe_separated(line: str) -> list[str]:
    return [value.strip() for value in line.split("|")]


def test_settle_literal_and_tuple_are_the_same_vocabulary() -> None:
    assert typing.get_args(ClientPrevHost) == CLIENT_PREV_HOSTS


def test_previous_host_is_the_beacon_vocabulary_plus_none() -> None:
    """`ph` describes a PREVIOUS attempt, so it alone can say there was none."""
    assert CLIENT_PREV_HOSTS == ("none", *HOSTS)


def test_every_gateway_region_has_a_host() -> None:
    """The SDKs map api-<region>.quillrouter.com to the region's enum member.

    The server side has to know a region before any SDK reports it, so the
    gateway inventory is what drives this rather than a second list here.
    """
    missing = [
        region for region in ENCLAVE_REGIONS if region.replace("-", "_") not in HOSTS
    ]
    assert missing == []
    # The control: a GCP region that is not a gateway region has no member, so
    # the check above cannot be passing because everything region-shaped does.
    assert "us_west2" not in HOSTS


def test_the_protocol_document_lists_the_same_hosts() -> None:
    document = (ROOT / "docs" / "client-telemetry.md").read_text(encoding="utf-8")

    previous_host = re.search(r"^ph = previous host: (.+)$", document, re.M)
    assert previous_host is not None
    assert _pipe_separated(previous_host.group(1)) == [*HOSTS, "none"]

    host_enum = re.search(r"^Host\s+= (.+)$", document, re.M)
    assert host_enum is not None
    assert _pipe_separated(host_enum.group(1)) == list(HOSTS)


def test_the_public_telemetry_page_lists_the_same_hosts() -> None:
    template = (
        ROOT / "src" / "trusted_router" / "templates" / "public" / "telemetry.html"
    ).read_text(encoding="utf-8")

    rows = [line for line in template.splitlines() if "<td><code>ph</code></td>" in line]
    assert len(rows) == 1
    # The first <code> is the key itself; the rest are its allowed values.
    assert re.findall(r"<code>([^<]+)</code>", rows[0])[1:] == [*HOSTS, "none"]
