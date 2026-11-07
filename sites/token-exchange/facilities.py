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


def facility_cards_v3() -> str:
    """Brand v3 facility table for the accelerator (kit DataTable): the facts of facility_cards().

    The GPU and lot notes are shown once under the table by the page, not per row.
    On phones the head row hides and each cell shows its own label."""
    head = ('<thead><tr><th scope="col">Facility</th><th scope="col">Targeted launch</th>'
            '<th scope="col">Grade</th><th scope="col" class="num">Power</th>'
            '<th scope="col" class="num">First offering</th><td></td></tr></thead>')
    ladder = '<span class="sv3-ladder g-b" aria-hidden="true"><i class="on"></i><i class="on"></i><i></i><i></i></span>'
    rows = []
    for facility in FACILITIES:
        rows.append(f'''<tr>
  <th scope="row"><b>{facility.name}</b><small>A planned {facility.description} data center.</small></th>
  <td><span class="sv3-ml">Targeted launch</span>{facility.launch}</td>
  <td><span class="sv3-ml">Grade</span><span class="sv3-grade"><span class="v3-chip">B</span>{ladder}<a href="/index">Professional (DeepSeek-class models)</a></span></td>
  <td class="num"><span class="sv3-ml">Power</span>{facility.power}</td>
  <td class="num"><span class="sv3-ml">First offering</span>{facility.lots:,} lots</td>
  <td class="act"><a href="/buy?facility={facility.id}">Discuss a reservation →</a></td>
</tr>''')
    return '<table class="sv3-table sv3-fac-table">' + head + '<tbody>\n' + '\n'.join(rows) + '\n</tbody></table>'


FACILITY_RATE = (
    '1B tokens over 12 months ≈ 32 tok/s sustained; '
    '1,000 lots ≈ 32,000 tok/s; 50,000 lots ≈ 1.6 million tok/s.'
)
