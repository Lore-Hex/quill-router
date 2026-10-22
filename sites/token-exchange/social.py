#!/usr/bin/env python3
"""Prepare fixed-size social cards for browser capture at 1200 x 630.

Run after build.py, capture each social-{slug}/ page, and save the PNGs in
social-images/og-{slug}.png. The normal build copies those reviewed images.
"""
import argparse
import html
import re
from pathlib import Path
from string import Template

from build import HERE, load_markets


def prepare(output):
    template = Template((HERE / 'social-card.html').read_text())
    for market in load_markets():
        headline = html.escape(market['headline'])
        accent = html.escape(market['headline_accent'] + '.')
        if market['slug'] == 'united-states':
            headline = headline.replace('the United States.', 'the U.S.')
            accent = accent.replace('the United States.', 'the U.S.')
        headline = headline.replace(accent, f'<span class="headline-accent">{accent}</span>', 1)
        # The card breaks its line before the italic accent on every market.
        headline = headline.replace(' <span class="headline-accent">', '<br><span class="headline-accent">', 1)
        folder = output / f"social-{market['slug']}"
        folder.mkdir(parents=True, exist_ok=True)
        card = template
        if market['slug'] == 'new-york':
            card = Template((HERE / 'nyte-v2/social-card.html').read_text())
            headline = re.search(r'<h1>(.*?)</h1>', (HERE / 'nyte-v2/home.html').read_text(), re.S).group(1)
        (folder / 'index.html').write_text(card.substitute(
            name=html.escape(market['name']), headline=headline))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    prepare(parser.parse_args().output)
