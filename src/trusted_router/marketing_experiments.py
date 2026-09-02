"""Deterministic paid-acquisition experiment catalog and wave selection.

The catalog contains hundreds of coherent candidate cells, but only one small,
balanced wave is eligible at a time. This preserves enough traffic per cell to
measure product activation instead of spreading a finite budget across hundreds
of simultaneous variants.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

GOOGLE_SEARCH_EXPERIMENT_ID = "google_search_messages_v3"
GOOGLE_SEARCH_ACTIVE_WAVE = 0
GOOGLE_SEARCH_CELLS_PER_WAVE = 4

EXPERIMENT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
EXPERIMENT_CELL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,95}$")


@dataclass(frozen=True)
class Audience:
    code: str
    kicker: str
    lead: str
    challenge: str


@dataclass(frozen=True)
class Promise:
    code: str
    headline: str
    detail: str
    model_id: str
    card_heading: str


@dataclass(frozen=True)
class Proof:
    code: str
    value: str
    label: str
    detail: str
    secondary_label: str
    secondary_href: str


@dataclass(frozen=True)
class CallToAction:
    code: str
    label: str
    final_headline: str


@dataclass(frozen=True)
class GoogleSearchExperimentCell:
    experiment_id: str
    cell_id: str
    audience: Audience
    promise: Promise
    proof: Proof
    call_to_action: CallToAction

    @property
    def slug(self) -> str:
        return self.cell_id

    @property
    def title(self) -> str:
        return f"{self.promise.headline} | TrustedRouter"

    @property
    def description(self) -> str:
        return f"{self.audience.lead} {self.promise.detail}"

    @property
    def kicker(self) -> str:
        return self.audience.kicker

    @property
    def headline(self) -> str:
        return self.promise.headline

    @property
    def lead(self) -> str:
        return f"{self.audience.lead} {self.promise.detail}"

    @property
    def cta(self) -> str:
        return self.call_to_action.label

    @property
    def secondary_label(self) -> str:
        return self.proof.secondary_label

    @property
    def secondary_href(self) -> str:
        return self.proof.secondary_href

    @property
    def microcopy(self) -> str:
        return "Keep your OpenAI SDK. Start with $0.30 in API credit."

    @property
    def terminal_label(self) -> str:
        return "One compatible request"

    @property
    def model_id(self) -> str:
        return self.promise.model_id

    @property
    def proof_items(self) -> tuple[tuple[str, str], ...]:
        return (
            (self.proof.value, self.proof.label),
            ("550+", "AI model routes."),
            ("One", "OpenAI-compatible interface."),
            ("$0", "Monthly subscription."),
        )

    @property
    def cards(self) -> tuple[tuple[str, str, str], ...]:
        return (
            ("Your workload", self.audience.challenge, self.audience.lead),
            ("The route", self.promise.card_heading, self.promise.detail),
            ("The evidence", self.proof.label, self.proof.detail),
        )

    @property
    def left_eyebrow(self) -> str:
        return "First request"

    @property
    def left_headline(self) -> str:
        return "Change one base URL."

    @property
    def left_copy(self) -> str:
        return (
            "Use the OpenAI client you already have, create a scoped key, and "
            "make the first request from the sample above."
        )

    @property
    def right_eyebrow(self) -> str:
        return "Production path"

    @property
    def right_headline(self) -> str:
        return self.promise.card_heading

    @property
    def right_copy(self) -> str:
        return self.proof.detail

    @property
    def final_headline(self) -> str:
        return self.call_to_action.final_headline

    @property
    def final_copy(self) -> str:
        return "Create a key, run one request, and inspect the route that served it."


AUDIENCES: tuple[Audience, ...] = (
    Audience(
        "or",
        "For teams comparing OpenRouter alternatives",
        "Move a live application without rebuilding its model integration.",
        "A router migration should fit in one code review.",
    ),
    Audience(
        "dev",
        "For production AI developers",
        "Ship model choice, fallback, and usage controls behind one familiar API.",
        "Production teams need an interface that survives model churn.",
    ),
    Audience(
        "sec",
        "For security-conscious AI teams",
        "Verify the gateway build handling each request and choose the downstream policy.",
        "Sensitive prompts deserve evidence stronger than a policy page.",
    ),
    Audience(
        "fin",
        "For financial AI teams",
        "Protect proprietary research while keeping model and provider choice open.",
        "Private research needs controlled routes and inspectable usage.",
    ),
    Audience(
        "legal",
        "For legal AI teams",
        "Route privileged work through explicit privacy controls and a verifiable gateway.",
        "Legal workloads need privacy evidence and predictable controls.",
    ),
    Audience(
        "health",
        "For healthcare AI teams",
        "Evaluate capable models through one controlled integration and documented route policy.",
        "Clinical and operational workloads need clear boundaries.",
    ),
)

PROMISES: tuple[Promise, ...] = (
    Promise(
        "migrate",
        "Switch model routers in one base URL.",
        "Keep the OpenAI request shape while TrustedRouter supplies the model catalog and routes.",
        "trustedrouter/auto",
        "Keep the client. Change the route.",
    ),
    Promise(
        "privacy",
        "Verify the code handling every prompt.",
        "The live attestation identifies the open-source gateway build before prompt traffic flows.",
        "trustedrouter/zdr",
        "Privacy with proof.",
    ),
    Promise(
        "uptime",
        "Keep serving when a provider fails.",
        "Eligible fallback routes let one request move past retryable provider failures.",
        "trustedrouter/auto",
        "Automatic provider fallback.",
    ),
    Promise(
        "models",
        "Use hundreds of models through one API.",
        "Compare exact models and providers, then switch with one model string.",
        "trustedrouter/auto",
        "One integration for the model market.",
    ),
    Promise(
        "price",
        "Pay for tokens without a router subscription.",
        "Published model prices and per-key limits keep usage costs visible and bounded.",
        "trustedrouter/cheap",
        "Route by cost when cost matters.",
    ),
    Promise(
        "speed",
        "Choose the fastest healthy route automatically.",
        "Measured provider routes make latency an explicit routing input.",
        "trustedrouter/fast",
        "Route with live performance evidence.",
    ),
    Promise(
        "nolog",
        "Keep prompts and outputs out of router logs.",
        "Realtime inference records operational metadata while prompt and output content stays out.",
        "trustedrouter/zdr",
        "Use metadata without content surveillance.",
    ),
    Promise(
        "open",
        "Run through an open-source attested gateway.",
        "Inspect the prompt-path source and match the published build to fresh hardware evidence.",
        "trustedrouter/e2e",
        "Check the running system, not only the claim.",
    ),
)

PROOFS: tuple[Proof, ...] = (
    Proof(
        "attest",
        "Live",
        "Hardware attestation.",
        "The trust page binds a fresh nonce to the live gateway and publishes the release evidence.",
        "Verify the gateway",
        "https://trust.trustedrouter.com",
    ),
    Proof(
        "catalog",
        "Public",
        "Model and provider catalog.",
        "Model pages expose route providers, context, customer pricing, and privacy posture.",
        "Browse models",
        "/models",
    ),
    Proof(
        "status",
        "Measured",
        "Provider performance.",
        "Status and leaderboard pages publish metadata-only success, latency, and throughput samples.",
        "See live status",
        "/status",
    ),
    Proof(
        "source",
        "Open",
        "Prompt-path source.",
        "The gateway, API, deployment configuration, and verification tooling are published for review.",
        "Read the source",
        "https://github.com/Lore-Hex/quill-router",
    ),
)

CALLS_TO_ACTION: tuple[CallToAction, ...] = (
    CallToAction("key", "Create my API key", "Make the first request through TrustedRouter."),
    CallToAction("pong", "Run a live PONG", "Prove the integration with one small request."),
)


def build_google_search_cells() -> tuple[GoogleSearchExperimentCell, ...]:
    cells = tuple(
        GoogleSearchExperimentCell(
            experiment_id=GOOGLE_SEARCH_EXPERIMENT_ID,
            cell_id=(
                f"g3_{audience.code}_{promise.code}_{proof.code}_{call_to_action.code}"
            ),
            audience=audience,
            promise=promise,
            proof=proof,
            call_to_action=call_to_action,
        )
        for audience in AUDIENCES
        for promise in PROMISES
        for proof in PROOFS
        for call_to_action in CALLS_TO_ACTION
    )
    if len({cell.cell_id for cell in cells}) != len(cells):
        raise RuntimeError("Google Search experiment cell IDs are not unique")
    return cells


GOOGLE_SEARCH_CELLS = build_google_search_cells()
GOOGLE_SEARCH_CELLS_BY_ID = {cell.cell_id: cell for cell in GOOGLE_SEARCH_CELLS}
GOOGLE_SEARCH_WAVE_COUNT = len(GOOGLE_SEARCH_CELLS) // GOOGLE_SEARCH_CELLS_PER_WAVE


def google_search_wave(wave_index: int) -> tuple[GoogleSearchExperimentCell, ...]:
    """Return four cells with no repeats across the 96-wave catalog."""
    if not 0 <= wave_index < GOOGLE_SEARCH_WAVE_COUNT:
        raise ValueError(
            f"Google Search experiment wave must be between 0 and "
            f"{GOOGLE_SEARCH_WAVE_COUNT - 1}"
        )
    cells: list[GoogleSearchExperimentCell] = []
    pair_index = wave_index // 2
    first_promise = (wave_index % 2) * GOOGLE_SEARCH_CELLS_PER_WAVE
    for promise_index in range(
        first_promise,
        first_promise + GOOGLE_SEARCH_CELLS_PER_WAVE,
    ):
        promise = PROMISES[promise_index]
        candidates = tuple(cell for cell in GOOGLE_SEARCH_CELLS if cell.promise == promise)
        # 17 is coprime to 48, so every promise visits every audience/proof/CTA
        # combination exactly once before the 96-wave catalog is exhausted.
        candidate_index = (pair_index * 17 + promise_index * 5) % len(candidates)
        cells.append(candidates[candidate_index])
    return tuple(cells)


def assigned_google_search_cell(
    seed: str | None,
    *,
    wave_index: int = GOOGLE_SEARCH_ACTIVE_WAVE,
) -> GoogleSearchExperimentCell:
    wave = google_search_wave(wave_index)
    if not seed:
        return wave[0]
    digest = hashlib.sha256(
        f"{GOOGLE_SEARCH_EXPERIMENT_ID}:{wave_index}:{seed}".encode()
    ).digest()
    return wave[int.from_bytes(digest[:8], "big") % len(wave)]


def valid_experiment_identity(experiment_id: str, cell_id: str) -> bool:
    if not (
        EXPERIMENT_ID_RE.fullmatch(experiment_id)
        and EXPERIMENT_CELL_ID_RE.fullmatch(cell_id)
    ):
        return False
    return (
        experiment_id == GOOGLE_SEARCH_EXPERIMENT_ID
        and cell_id in GOOGLE_SEARCH_CELLS_BY_ID
    )


__all__ = [
    "GOOGLE_SEARCH_ACTIVE_WAVE",
    "GOOGLE_SEARCH_CELLS",
    "GOOGLE_SEARCH_CELLS_BY_ID",
    "GOOGLE_SEARCH_EXPERIMENT_ID",
    "GOOGLE_SEARCH_WAVE_COUNT",
    "GoogleSearchExperimentCell",
    "assigned_google_search_cell",
    "google_search_wave",
    "valid_experiment_identity",
]
