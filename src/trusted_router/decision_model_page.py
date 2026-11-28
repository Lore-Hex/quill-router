"""What /docs/decide says about each decision model's hosts and latency.

The page used to type which hosts a name runs on, how many it fails over
across and how fast it answers. Hosts retire models, and every one of those
sentences went stale. Here they are read from the catalog when the page is
rendered: a chain host counts only while the catalog has a current prepaid
route for the name's backing model there, which is the question authorize
asks before it serves the name. The lookup costs under a millisecond, so the
context is built per request and a retirement shows on the page at its
effective time, with no deploy.

Latency is published per host, because a figure measured on one host says
nothing about the next. A row shows the figure of its first live host or says
that host has not been measured.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from trusted_router.catalog import PROVIDERS, endpoints_for_model
from trusted_router.catalog_data import (
    DEV_1_0_MODEL_ID,
    GEMMEV_1_0_MODEL_ID,
    GEV_1_0_MODEL_ID,
    LEV_1_0_MODEL_ID,
    MEV_1_0_MODEL_ID,
    NAMED_DECISION_MODELS,
    NATIVE_DECISION_MODEL_IDS,
    OEV_1_0_MODEL_ID,
    TREV_1_0_MODEL_ID,
    ZEV_1_0_MODEL_ID,
)

JEV_MODEL_ID = "typesafe-ai/jev"
# Jev is hosted by its vendor and has no chain in NAMED_DECISION_MODELS.
_HOSTED_CHAINS: dict[str, tuple[str, ...]] = {JEV_MODEL_ID: ("typesafe",)}

# "About a third of a second", as the page's opening sentence counts it.
THIRD_OF_A_SECOND_MS = 400

_COUNT_WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


def count_word(count: int) -> str:
    return _COUNT_WORDS[count] if 0 <= count < len(_COUNT_WORDS) else str(count)


@dataclass(frozen=True)
class Latency:
    # The number the text shows, in milliseconds: rows are ordered and counted by it.
    ms: int
    text: str
    # True for a figure taken with one host pinned, end to end through the
    # gateway, when the row itself has to say so.
    row_says_where: bool = False


def _pinned_median(ms: int) -> Latency:
    """A median measured end to end with the host pinned, rounded as the page
    rounds: to 10 ms below 700 ms, to a tenth of a second from there."""
    if ms < 700:
        shown = (ms + 5) // 10 * 10
        return Latency(shown, f"about {shown} ms", row_says_where=True)
    shown = (ms + 50) // 100 * 100
    seconds = f"{shown // 1000}.{shown % 1000 // 100}".removesuffix(".0")
    return Latency(shown, f"about {seconds} s", row_says_where=True)


# Latency by model and host. A chain's first host carries the figure the page
# has always shown for the name. A later host carries its own pinned median:
# measured 2026-09-25 (gemmev 2026-09-28) through /api/alpha/decide with
# fallbacks off, as recorded beside NAMED_DECISION_MODELS. A host missing here
# has not been measured, and the page says so instead of borrowing a figure.
MEASURED_LATENCY: dict[str, dict[str, Latency]] = {
    JEV_MODEL_ID: {"typesafe": Latency(250, "about 250 ms")},
    TREV_1_0_MODEL_ID: {
        "cerebras": Latency(300, "about 300 ms"),
        "sambanova": _pinned_median(522),
        "fireworks": _pinned_median(941),
        "together": _pinned_median(1125),
    },
    MEV_1_0_MODEL_ID: {"inception": Latency(370, "about 370 ms")},
    LEV_1_0_MODEL_ID: {
        "sambanova": Latency(390, "about 390 ms"),
        "parasail": _pinned_median(927),
        "together": _pinned_median(1576),
    },
    # Baseten answered 529 at every effort on the day Fireworks was measured.
    ZEV_1_0_MODEL_ID: {"fireworks": Latency(800, "about 0.8 s (0.7 to 1.1 s)")},
    GEV_1_0_MODEL_ID: {"google-ai-studio": Latency(800, "about 0.8 s")},
    DEV_1_0_MODEL_ID: {
        "wafer": Latency(1000, "about 1 s (0.7 to 2.4 s)"),
        "deepinfra": _pinned_median(1858),
        "wandb": _pinned_median(1987),
    },
    OEV_1_0_MODEL_ID: {"deepinfra": Latency(1600, "about 1.6 s")},
    GEMMEV_1_0_MODEL_ID: {
        "nextbit": Latency(2000, "about 2 s"),
        "wandb": _pinned_median(1940),
        "io-net": _pinned_median(1964),
    },
}


@dataclass(frozen=True)
class DecisionModelHosts:
    """One decision model as the catalog serves it now."""

    model_id: str
    # The chat model whose routes serve the name; the id itself for Jev.
    backing_model_id: str
    chain: tuple[str, ...]
    # The chain hosts with a current prepaid route, in chain order.
    live: tuple[str, ...]
    # Their display names, in the same order.
    live_hosts: tuple[str, ...]
    # The first live host's figure. None when no host is live or when that
    # host has not been measured.
    latency: Latency | None

    @property
    def on_first_host(self) -> bool:
        return bool(self.live) and self.live[0] == self.chain[0]

    @property
    def first_live_host(self) -> str | None:
        return self.live_hosts[0] if self.live_hosts else None


def _configured_chains() -> dict[str, tuple[str, tuple[str, ...]]]:
    chains = {model_id: (model_id, chain) for model_id, chain in _HOSTED_CHAINS.items()}
    chains.update(
        {named.id: (named.backing_model_id, named.chain) for named in NAMED_DECISION_MODELS}
    )
    return chains


def _host_name(slug: str) -> str:
    provider = PROVIDERS.get(slug)
    return provider.name if provider is not None else slug


def decision_model_hosts() -> list[DecisionModelHosts]:
    """Every decision model on the page, in the page's reading order, with the
    hosts that can serve it now."""
    chains = _configured_chains()
    found: list[DecisionModelHosts] = []
    for model_id in (row.model_id for row in _ROWS):
        if model_id not in chains:
            continue
        backing_model_id, chain = chains[model_id]
        prepaid = {
            endpoint.provider
            for endpoint in endpoints_for_model(backing_model_id)
            if endpoint.usage_type == "Credits"
        }
        live = tuple(host for host in chain if host in prepaid)
        found.append(
            DecisionModelHosts(
                model_id=model_id,
                backing_model_id=backing_model_id,
                chain=chain,
                live=live,
                live_hosts=tuple(_host_name(host) for host in live),
                latency=MEASURED_LATENCY.get(model_id, {}).get(live[0]) if live else None,
            )
        )
    return found


def _failover_sentence(hosts: DecisionModelHosts) -> str:
    """The sentence a row ends with: "Fails over across N hosts.", "Served by
    X alone.", or nothing while no host is live."""
    if len(hosts.live) >= 2:
        return f" Fails over across {count_word(len(hosts.live))} hosts."
    if hosts.first_live_host is not None:
        return f" Served by {hosts.first_live_host} alone."
    return ""


def _jev(hosts: DecisionModelHosts) -> str:
    return "A purpose-built decision model from TypeSafe AI, called at TypeSafe's own API."


def _trev(hosts: DecisionModelHosts) -> str:
    # The response for this name omits the host that served it, and so does
    # this row: it gives the number of hosts and never their names.
    hardware = " on the fastest hardware we could measure" if hosts.on_first_host else ""
    if len(hosts.live) >= 2:
        failover = f", with automatic failover across {count_word(len(hosts.live))} hosts"
    elif hosts.live:
        failover = ", served by a single host"
    else:
        failover = ""
    return f"The default. An open-weight 120B reasoning model{hardware}{failover}."


def _mev(hosts: DecisionModelHosts) -> str:
    return (
        "Speed at a fifth of the price. Mercury 3, a diffusion model, answering "
        "without deliberating."
    )


def _lev(hosts: DecisionModelHosts) -> str:
    # "The steadiest tail" and the 565 ms answer were both measured on
    # SambaNova: they are shown only while SambaNova, the chain's first host,
    # is the one that answers first.
    if hosts.on_first_host:
        lead = (
            "The steadiest tail. Llama 3.3 70B does not deliberate, so its slowest "
            "answer in the eval took 565 ms."
        )
    else:
        lead = "Llama 3.3 70B, answering without deliberating."
    return f"{lead}{_failover_sentence(hosts)}"


def _zev(hosts: DecisionModelHosts) -> str:
    if len(hosts.live) == 2:
        failover = ", with failover to a second host"
    elif len(hosts.live) > 2:
        failover = f", with failover across {count_word(len(hosts.live))} hosts"
    elif hosts.first_live_host is not None:
        failover = f", served by {hosts.first_live_host} alone"
    else:
        failover = ""
    # The page's figure for this name was taken end to end on its first host.
    measured = (
        " Measured end to end through the gateway, like gemmev." if hosts.on_first_host else ""
    )
    return (
        f"A larger fast model. GLM 5.3 Fast, which reasons briefly{failover}. "
        f"About four times the price of the others here.{measured}"
    )


def _gev(hosts: DecisionModelHosts) -> str:
    return "A Google model. Small, from the Gemini family, with the format enforced by the host."


def _dev(hosts: DecisionModelHosts) -> str:
    if len(hosts.live) >= 2:
        failover = f" Fails over from {' to '.join(hosts.live_hosts)}."
    else:
        failover = _failover_sentence(hosts)
    return (
        "Harder judgments on a budget. A DeepSeek flash model: strong judgment, "
        f"cheap input.{failover}"
    )


def _oev(hosts: DecisionModelHosts) -> str:
    return "A cheap all-rounder. An open-weight 20B model that reasons briefly."


def _gemmev(hosts: DecisionModelHosts) -> str:
    return (
        "An open Gemma model: Gemma 4 26B with 4B parameters active, answering "
        f"without deliberating.{_failover_sentence(hosts)}"
    )


@dataclass(frozen=True)
class _RowCopy:
    model_id: str
    pick_it_for: Callable[[DecisionModelHosts], str]
    cost: str
    accuracy: str = "29 / 29"
    # False for a name whose hosts the page counts and does not name.
    names_hosts: bool = True


# The page's reading order. The table is ordered by the latency shown; this
# order breaks ties and places the rows that show no figure.
_ROWS: tuple[_RowCopy, ...] = (
    _RowCopy(JEV_MODEL_ID, _jev, "$0.00002"),
    _RowCopy(TREV_1_0_MODEL_ID, _trev, "$0.00034", names_hosts=False),
    _RowCopy(MEV_1_0_MODEL_ID, _mev, "$0.00007"),
    _RowCopy(LEV_1_0_MODEL_ID, _lev, "$0.00036"),
    _RowCopy(ZEV_1_0_MODEL_ID, _zev, "$0.0014"),
    _RowCopy(GEV_1_0_MODEL_ID, _gev, "$0.00023"),
    _RowCopy(DEV_1_0_MODEL_ID, _dev, "$0.00013"),
    _RowCopy(OEV_1_0_MODEL_ID, _oev, "$0.00004"),
    _RowCopy(GEMMEV_1_0_MODEL_ID, _gemmev, "$0.00007"),
)
_ROW_COPY: Mapping[str, _RowCopy] = {row.model_id: row for row in _ROWS}


@dataclass(frozen=True)
class DecisionModelRow:
    model_id: str
    live_hosts: tuple[str, ...]
    pick_it_for: str
    latency: str
    # None when the row shows no figure.
    latency_ms: int | None
    accuracy: str
    cost: str


def _row(hosts: DecisionModelHosts) -> DecisionModelRow:
    copy = _ROW_COPY[hosts.model_id]
    pick_it_for = copy.pick_it_for(hosts)
    host = hosts.first_live_host
    if host is None:
        pick_it_for += " Temporarily unavailable."
        latency = "unavailable"
    elif hosts.latency is None:
        where = host if copy.names_hosts else "its current host"
        latency = f"not yet measured on {where}"
    else:
        latency = hosts.latency.text
        if hosts.latency.row_says_where:
            where = f" on {host}" if copy.names_hosts else ""
            like = "" if hosts.model_id == GEMMEV_1_0_MODEL_ID else ", like gemmev"
            pick_it_for += f" Measured end to end through the gateway{where}{like}."
    return DecisionModelRow(
        model_id=hosts.model_id,
        live_hosts=hosts.live_hosts,
        pick_it_for=pick_it_for,
        latency=latency,
        latency_ms=hosts.latency.ms if hosts.latency is not None else None,
        accuracy=copy.accuracy,
        cost=copy.cost,
    )


def _fastest_first(rows: list[DecisionModelRow]) -> list[DecisionModelRow]:
    """By the latency shown; rows with no figure last. The sort is stable, so
    equal figures and the rows without one keep the page's reading order."""
    return sorted(rows, key=lambda row: (row.latency_ms is None, row.latency_ms or 0))


def _fast_sentence(rows: list[DecisionModelRow]) -> str | None:
    fast = sum(
        1 for row in rows if row.latency_ms is not None and row.latency_ms <= THIRD_OF_A_SECOND_MS
    )
    if fast == 0:
        return None
    verb = "answers" if fast == 1 else "answer"
    return f"{count_word(fast)} of these {verb} in about a third of a second."


def _series(clauses: list[str]) -> str:
    if len(clauses) <= 2:
        return " and ".join(clauses)
    return f"{', '.join(clauses[:-1])}, and {clauses[-1]}"


def _start_with(hosts: DecisionModelHosts | None, row: DecisionModelRow | None) -> str | None:
    """Why the page recommends trev first, or None while no host serves it."""
    if hosts is None or row is None or not hosts.live:
        return None
    clauses = []
    if row.latency_ms is not None and row.latency_ms <= THIRD_OF_A_SECOND_MS:
        clauses.append("it is among the fastest")
    clauses.append("it reasons briefly before it answers")
    if len(hosts.live) >= 2:
        clauses.append(f"it fails over across {count_word(len(hosts.live))} hosts")
    else:
        clauses.append("it is served by a single host")
    return f"{_series(clauses)}."


def _runs_on(found: list[DecisionModelHosts]) -> list[dict[str, str]]:
    """The tuned chat models callable under their own ids, with their live hosts."""
    return [
        {
            "model_id": hosts.backing_model_id,
            "hosts": ", then ".join(hosts.live_hosts) or "None at present",
        }
        for hosts in found
        if hosts.backing_model_id != hosts.model_id
        and hosts.backing_model_id in NATIVE_DECISION_MODEL_IDS
    ]


def decide_page_context() -> dict[str, Any]:
    found = decision_model_hosts()
    rows = [_row(hosts) for hosts in found]
    by_id = {hosts.model_id: hosts for hosts in found}
    trev = by_id.get(TREV_1_0_MODEL_ID)
    trev_row = next((row for row in rows if row.model_id == TREV_1_0_MODEL_ID), None)
    return {
        "decide_models": {
            "rows": _fastest_first(rows),
            "fast_sentence": _fast_sentence(rows),
            "start_with": _start_with(trev, trev_row),
            "runs_on": _runs_on(found),
            # The sample response is trev's: its candidate count is the number
            # of hosts that can serve trev now.
            "trev_candidate_count": max(1, len(trev.live)) if trev is not None else 1,
        }
    }
