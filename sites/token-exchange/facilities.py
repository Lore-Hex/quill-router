"""Joseph's approved planned facilities, shared by the buyer and operator pages."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Facility:
    id: str
    name: str
    operator: str
    description: str
    power: str
    launch: str
    lots: int


FACILITIES = (
    Facility("greenference", "Greenference", "Greenference", "solar-powered, air-cooled",
             "20 kW", "February 2027", 1000),
    Facility("submerge", "Submerge", "Submerge, Inc.", "underwater",
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
  <p>A planned {facility.description} data center operated by {facility.operator}.</p>
  <p>{facility.power} · Planned on NVIDIA B200/B300-class GPUs.</p>
  <p>Targeted launch: {facility.launch}.</p>
  <p>First offering: {facility.lots:,} lots.</p>
  <p><a href="/index">Grade B · Professional (DeepSeek-class models)</a></p>
  <p>Each lot is 1 billion tokens delivered over the facility’s first twelve months of operation, not per month.</p>
  {action}
</article>''')
    return '\n'.join(cards)


FACILITY_RATE = (
    '1B tokens over 12 months ≈ 32 tok/s sustained; '
    '1,000 lots ≈ 32,000 tok/s; 50,000 lots ≈ 1.6 million tok/s.'
)
