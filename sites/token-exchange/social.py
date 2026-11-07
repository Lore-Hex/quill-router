#!/usr/bin/env python3
"""Prepare fixed-size social cards for browser capture at 1200 x 630.

Run after build.py, capture each social-{slug}/ page, and save the PNGs in
social-images/og-{slug}.png (social-v3-{slug}/ to og-v3-{slug}.png for brand v3). The normal build copies those reviewed images.
"""
import argparse
import html
import re
from pathlib import Path
from string import Template

from build import HERE, load_markets
from nyte_v2 import uses_nyte


def prepare(output):
    template = Template((HERE / 'social-card.html').read_text())
    nyte_card = Template((HERE / 'nyte-v2/social-card.html').read_text())
    nyte_v3_card = Template((HERE / 'nyte-v2/social-card-v3.html').read_text())
    nyte_headline = re.search(r'<h1>(.*?)</h1>', (HERE / 'nyte-v2/home.html').read_text(), re.S).group(1)
    # The v3 card matches the v3 home hero: its headline (accent in <em>) and the purple Band.
    nyte_v3_headline = re.search(r'<h1>(.*?)</h1>', (HERE / 'nyte-v2/home-v3.html').read_text(), re.S).group(1)
    band = (HERE / 'nyte-v2/band-purple.svg').read_text().strip()
    for market in load_markets():
        if uses_nyte(market):
            card, headline = nyte_card, nyte_headline
        else:
            card = template
            headline = html.escape(market['headline'])
            accent = html.escape(market['headline_accent'] + '.')
            headline = headline.replace(accent, f'<span class="headline-accent">{accent}</span>', 1)
            # The card breaks its line before the italic accent on every market.
            headline = headline.replace(' <span class="headline-accent">', '<br><span class="headline-accent">', 1)
        folder = output / f"social-{market['slug']}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'index.html').write_text(card.substitute(
            name=html.escape(market['name']), headline=headline))
        if uses_nyte(market):
            # Brand v3 card, used by pages switched on in nyte-brand-v3.json.
            logo = 'lockup-horizontal-ink.svg' if market['slug'] == 'new-york' else 'mark-ink.svg'
            v3 = output / f"social-v3-{market['slug']}"
            v3.mkdir(parents=True, exist_ok=True)
            (v3 / 'index.html').write_text(nyte_v3_card.substitute(
                name=html.escape(market['name']), headline=nyte_v3_headline, slug=market['slug'], band=band,
                domain=html.escape(market['domain']), logo=f'/assets/nyte-v2/logos/{logo}'))

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    prepare(parser.parse_args().output)
