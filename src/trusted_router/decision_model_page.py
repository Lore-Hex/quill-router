"""What /docs/decide says about each decision model's hosts, latency and cost.

The page used to type which hosts a name runs on, how many it fails over
across, how fast it answers and what a decision costs. Hosts retire models
and change prices, and those sentences went stale. Here they are read from
the catalog when the page is rendered: a chain host counts only while the
catalog has a current prepaid route for the name's backing model there, which
is the question authorize asks before it serves the name. The lookup costs
about two milliseconds, so the context is built per request and a retirement
or a price change shows on the page at its effective time, with no deploy.

Latency is published per host, because a figure measured on one host says
nothing about the next. A row shows the figure of its first live host or says
that host has not been measured. Cost is the measured token counts at the
first live host's catalog price.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any

from trusted_router.catalog import PROVIDERS, ModelEndpoint, endpoints_for_model
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
from trusted_router.pricing import resolve_request_rates

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


def _about(ms: int) -> Latency:
    """A median rounded as the page rounds: to 10 ms below 700 ms, to a tenth
    of a second from there."""
    if ms < 700:
        shown = (ms + 5) // 10 * 10
        return Latency(shown, f"about {shown} ms")
    shown = (ms + 50) // 100 * 100
    seconds = f"{shown // 1000}.{shown % 1000 // 100}".removesuffix(".0")
    return Latency(shown, f"about {seconds} s")


@dataclass(frozen=True)
class HostMedian:
    """The median time one host took to answer, with the number of requests
    the median is over."""

    ms: int
    samples: int


# A median over fewer joined requests than this is not published.
MIN_SAMPLES = 6

# Host time by model and host, measured 2026-11-28: the eval's 8 labeled
# tickets and 29 checks through POST /api/alpha/decide, each host pinned with
# fallbacks off. Each call's x-request-id was joined to the gateway's own
# request_end record, whose upstream_ms is the time the host took; the
# gateway's own time is not in it. Those records are sampled, so `samples` is
# the number of joined calls. Every figure here is a chain's first host.
# Later hosts had fewer than MIN_SAMPLES joined calls and carry no figure:
# the page says "not yet measured" there instead of borrowing one.
MEASURED_LATENCY: dict[str, dict[str, HostMedian]] = {
    TREV_1_0_MODEL_ID: {"cerebras": HostMedian(335, 8)},
    MEV_1_0_MODEL_ID: {"inception": HostMedian(344, 7)},
    ZEV_1_0_MODEL_ID: {"fireworks": HostMedian(410, 10)},
    LEV_1_0_MODEL_ID: {"wandb": HostMedian(805, 13)},
    GEV_1_0_MODEL_ID: {"google-ai-studio": HostMedian(864, 6)},
    DEV_1_0_MODEL_ID: {"wafer": HostMedian(371, 10)},
    OEV_1_0_MODEL_ID: {"deepinfra": HostMedian(1417, 13)},
    GEMMEV_1_0_MODEL_ID: {"nextbit": HostMedian(450, 11)},
}
# The hosted path records no host time for Jev. Its figure is the median
# measured from one client in September 2026, and the page says so.
_CLIENT_MEDIAN_MS: dict[str, dict[str, int]] = {JEV_MODEL_ID: {"typesafe": 250}}


def published_latency(model_id: str, host: str) -> Latency | None:
    """The figure the page shows for `model_id` answered by `host`, or None
    when that host has no figure over enough requests."""
    client_ms = _CLIENT_MEDIAN_MS.get(model_id, {}).get(host)
    if client_ms is not None:
        return _about(client_ms)
    median = MEASURED_LATENCY.get(model_id, {}).get(host)
    if median is None or median.samples < MIN_SAMPLES:
        return None
    return _about(median.ms)


# Input and output tokens of one decision (the eval's four-question request),
# as each model reported them on 2026-11-28.
DECISION_TOKENS: dict[str, tuple[int, int]] = {
    JEV_MODEL_ID: (445, 86),
    TREV_1_0_MODEL_ID: (478, 207),
    MEV_1_0_MODEL_ID: (418, 64),
    ZEV_1_0_MODEL_ID: (418, 68),
    LEV_1_0_MODEL_ID: (440, 62),
    GEV_1_0_MODEL_ID: (429, 76),
    DEV_1_0_MODEL_ID: (411, 74),
    OEV_1_0_MODEL_ID: (465, 135),
    GEMMEV_1_0_MODEL_ID: (445, 72),
}
_MICRODOLLARS_PER_MILLION_TOKENS_TO_DOLLARS = Decimal(10) ** 12


def decision_cost(model_id: str, endpoint: ModelEndpoint) -> Decimal | None:
    """What one decision costs on `endpoint`, in dollars: the measured tokens
    at the route's prepaid price, which already carries the fee. None when
    the tokens are not recorded or the route states no token price."""
    tokens = DECISION_TOKENS.get(model_id)
    if tokens is None:
        return None
    prompt_tokens, completion_tokens = tokens
    rates = resolve_request_rates(
        endpoint.price_tiers,
        headline_prompt_micro_per_m=endpoint.prompt_price_microdollars_per_million_tokens,
        headline_completion_micro_per_m=endpoint.completion_price_microdollars_per_million_tokens,
        total_prompt_tokens=prompt_tokens,
    )
    microdollars = (
        prompt_tokens * rates.prompt_price_microdollars_per_million_tokens
        + completion_tokens * rates.completion_price_microdollars_per_million_tokens
    )
    if microdollars <= 0:
        return None
    return Decimal(microdollars) / _MICRODOLLARS_PER_MILLION_TOKENS_TO_DOLLARS


def _two_figures(dollars: Decimal) -> str:
    """Dollars to two significant figures, as the cost column shows them."""
    rounded = dollars.quantize(
        Decimal(1).scaleb(dollars.adjusted() - 1), rounding=ROUND_HALF_UP
    )
    text = format(rounded, "f")
    return "$" + (text.rstrip("0").rstrip(".") if "." in text else text)


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
    # One decision on the first live host, in dollars. None when no host is
    # live or its route states no token price.
    cost: Decimal | None

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
        prepaid: dict[str, ModelEndpoint] = {}
        for endpoint in endpoints_for_model(backing_model_id):
            if endpoint.usage_type == "Credits":
                prepaid.setdefault(endpoint.provider, endpoint)
        live = tuple(host for host in chain if host in prepaid)
        found.append(
            DecisionModelHosts(
                model_id=model_id,
                backing_model_id=backing_model_id,
                chain=chain,
                live=live,
                live_hosts=tuple(_host_name(host) for host in live),
                latency=published_latency(model_id, live[0]) if live else None,
                cost=decision_cost(model_id, prepaid[live[0]]) if live else None,
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


def _jev(hosts: DecisionModelHosts, trev_cost: Decimal | None) -> str:
    return "A purpose-built decision model from TypeSafe AI, called at TypeSafe's own API."


def _trev(hosts: DecisionModelHosts, trev_cost: Decimal | None) -> str:
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


_FRACTION_WORDS = {2: "half", 3: "a third", 4: "a quarter", 5: "a fifth", 6: "a sixth",
                   7: "a seventh", 8: "an eighth", 9: "a ninth", 10: "a tenth"}


def trev_price_comparison(cost: Decimal | None, trev_cost: Decimal | None) -> str | None:
    """How one decision's cost compares with trev's, in words, from the two
    costs the table shows. None when either is unknown or the ratio has no
    plain wording (beyond ten times or a tenth)."""
    if cost is None or trev_cost is None or cost <= 0 or trev_cost <= 0:
        return None
    ratio = cost / trev_cost
    if ratio >= 1:
        times = int((ratio + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR))
        if times == 1:
            return "about trev's price"
        if times <= 10:
            return f"about {count_word(times)} times trev's price"
        return None
    parts = int((1 / ratio + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR))
    if parts == 1:
        return "about trev's price"
    if parts in _FRACTION_WORDS:
        return f"about {_FRACTION_WORDS[parts]} of trev's price"
    return None


def _mev(hosts: DecisionModelHosts, trev_cost: Decimal | None) -> str:
    comparison = trev_price_comparison(hosts.cost, trev_cost)
    speed = f"Speed at {comparison}." if comparison and "times" not in comparison else "Speed."
    return f"{speed} Mercury 3, a diffusion model, answering without deliberating."


def _lev(hosts: DecisionModelHosts, trev_cost: Decimal | None) -> str:
    # No speed or tail claim: on 2026-11-28 zev's tail was tighter than lev's,
    # so "the steadiest tail" stopped being true when SambaNova left the chain.
    return (
        "A widely deployed open model. Llama 3.3 70B, answering without deliberating."
        f"{_failover_sentence(hosts)}"
    )


def _zev(hosts: DecisionModelHosts, trev_cost: Decimal | None) -> str:
    if len(hosts.live) == 2:
        failover = ", with failover to a second host"
    elif len(hosts.live) > 2:
        failover = f", with failover across {count_word(len(hosts.live))} hosts"
    elif hosts.first_live_host is not None:
        failover = f", served by {hosts.first_live_host} alone"
    else:
        failover = ""
    comparison = trev_price_comparison(hosts.cost, trev_cost)
    price = f" {comparison[0].upper()}{comparison[1:]}." if comparison else ""
    return f"A larger fast model. GLM 5.3 Fast, which reasons briefly{failover}.{price}"


def _gev(hosts: DecisionModelHosts, trev_cost: Decimal | None) -> str:
    return "A Google model. Small, from the Gemini family, with the format enforced by the host."


def _dev(hosts: DecisionModelHosts, trev_cost: Decimal | None) -> str:
    if len(hosts.live) >= 2:
        failover = f" Fails over from {' to '.join(hosts.live_hosts)}."
    else:
        failover = _failover_sentence(hosts)
    return (
        "Harder judgments on a budget. A DeepSeek flash model: strong judgment, "
        f"cheap input.{failover}"
    )


def _oev(hosts: DecisionModelHosts, trev_cost: Decimal | None) -> str:
    return "A cheap all-rounder. An open-weight 20B model that reasons briefly."


def _gemmev(hosts: DecisionModelHosts, trev_cost: Decimal | None) -> str:
    return (
        "An open Gemma model: Gemma 4 26B with 4B parameters active, answering "
        f"without deliberating.{_failover_sentence(hosts)}"
    )


@dataclass(frozen=True)
class _RowCopy:
    model_id: str
    pick_it_for: Callable[[DecisionModelHosts, Decimal | None], str]
    accuracy: str = "29 / 29"
    # Shown only when the first live host's route states no token price.
    typed_cost: str | None = None
    # False for a name whose hosts the page counts and does not name.
    names_hosts: bool = True


# The page's reading order. The table is ordered by the latency shown; this
# order breaks ties and places the rows that show no figure.
_ROWS: tuple[_RowCopy, ...] = (
    _RowCopy(JEV_MODEL_ID, _jev, typed_cost="$0.00002"),
    _RowCopy(TREV_1_0_MODEL_ID, _trev, names_hosts=False),
    _RowCopy(MEV_1_0_MODEL_ID, _mev),
    _RowCopy(LEV_1_0_MODEL_ID, _lev),
    _RowCopy(ZEV_1_0_MODEL_ID, _zev),
    _RowCopy(GEV_1_0_MODEL_ID, _gev),
    _RowCopy(DEV_1_0_MODEL_ID, _dev),
    _RowCopy(OEV_1_0_MODEL_ID, _oev),
    _RowCopy(GEMMEV_1_0_MODEL_ID, _gemmev),
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


def _row(hosts: DecisionModelHosts, trev_cost: Decimal | None = None) -> DecisionModelRow:
    copy = _ROW_COPY[hosts.model_id]
    pick_it_for = copy.pick_it_for(hosts, trev_cost)
    host = hosts.first_live_host
    if host is None:
        pick_it_for += " Temporarily unavailable."
        latency = cost = "unavailable"
    else:
        if hosts.latency is None:
            where = host if copy.names_hosts else "its current host"
            latency = f"not yet measured on {where}"
        else:
            latency = hosts.latency.text
        if hosts.cost is not None:
            cost = _two_figures(hosts.cost)
        else:
            cost = copy.typed_cost or "not stated"
    return DecisionModelRow(
        model_id=hosts.model_id,
        live_hosts=hosts.live_hosts,
        pick_it_for=pick_it_for,
        latency=latency,
        latency_ms=hosts.latency.ms if hosts.latency is not None else None,
        accuracy=copy.accuracy,
        cost=cost,
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
    by_id = {hosts.model_id: hosts for hosts in found}
    trev = by_id.get(TREV_1_0_MODEL_ID)
    # Price comparisons in the rows are against the cost the table shows for
    # trev, so they move with prices and with trev's first live host.
    trev_cost = trev.cost if trev is not None and trev.live else None
    rows = [_row(hosts, trev_cost) for hosts in found]
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
