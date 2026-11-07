"""Joseph's approved planned facilities, shared by the buyer and operator pages."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Facility:
    id: str
    name: str
    description: str
    power: str
    launch: str
    lots: int


FACILITIES = (
    Facility("solar", "Solar Powered Datacenter", "solar-powered, air-cooled",
             "20 kW", "February 2027", 1000),
    Facility("underwater", "Underwater Datacenter", "underwater",
             "1 MW", "August 2027", 50000),
)


def facility_cards(*, selectable: bool = False) -> str:
    cards = []
    for facility in FACILITIES:
        heading = f'<h3>{facility.name}</h3>'
        action = f'<a href="/buy?facility={facility.id}">Discuss a reservation →</a>'
        if selectable:
            heading = (
                f'<label class="facility-select"><input type="radio" name="facility"'
                f' value="{facility.id}" data-name="{facility.name}"'
                f' data-launch="{facility.launch}" data-lots="{facility.lots}"'
                + (' checked' if facility == FACILITIES[0] else '')
                + f'> <strong>{facility.name}</strong></label>'
            )
            action = ''
        cards.append(f'''<article class="facility-card">
  {heading}
  <span class="facility-badge">Planned</span>
  <p>A planned {facility.description} data center.</p>
  <p>{facility.power} · Planned on NVIDIA B200/B300-class GPUs.</p>
  <p>Targeted launch: {facility.launch}.</p>
  <p>First offering: {facility.lots:,} lots.</p>
  <p><a href="/index">Grade B · Professional (DeepSeek-class models)</a></p>
  <p>Each lot is 1 billion tokens delivered over the facility’s first twelve months of operation, not per month.</p>
  {action}
</article>''')
    return '\n'.join(cards)


# Kit icon arrow-right, drawn in the text colour.
ARROW_RIGHT = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M3 12 L15.75 12"/><path d="M14 6 L20 12 L14 18"/></svg>'


def facility_cards_v3() -> str:
    """Brand v3 facility columns for the accelerator: the facts of facility_cards() as three Stats.

    The lot note is shown once under both columns by the page, not per card."""
    cards = []
    for facility in FACILITIES:
        cards.append(f'''<article class="sv3-facility">
  <div class="sv3-facility-hd"><h3>{facility.name}</h3><span class="v3-chip">Planned</span></div>
  <p>A planned {facility.description} data center.</p>
  <div class="sv3-stats">
    <div class="sv3-stat bar-yellow"><span class="sv3-fig">{facility.power}</span><span class="sv3-note">Planned on NVIDIA B200/B300-class GPUs.</span></div>
    <div class="sv3-stat bar-purple"><span class="sv3-fig">{facility.launch}</span><span class="v3-label">Targeted launch</span></div>
    <div class="sv3-stat bar-lime"><span class="sv3-fig">{facility.lots:,} lots</span><span class="v3-label">First offering</span></div>
  </div>
  <p class="sv3-grade"><span class="sv3-ladder g-b" aria-hidden="true"><i class="on"></i><i class="on"></i><i></i><i></i></span><a href="/index">Grade B · Professional (DeepSeek-class models)</a></p>
  <div class="btns"><a class="btn btn-secondary" href="/buy?facility={facility.id}">Discuss a reservation{ARROW_RIGHT}</a></div>
</article>''')
    return '\n'.join(cards)


FACILITY_RATE = (
    '1B tokens over 12 months ≈ 32 tok/s sustained; '
    '1,000 lots ≈ 32,000 tok/s; 50,000 lots ≈ 1.6 million tok/s.'
)
