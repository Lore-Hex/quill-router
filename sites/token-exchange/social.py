#!/usr/bin/env python3
"""Prepare fixed-size social cards for browser capture at 1200 x 630.

Run after build.py, capture each social-{slug}/ page, and save the PNGs in
social-images/og-{slug}.png. The normal build copies those reviewed images.
"""
import argparse
import html
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
        headline = headline.replace(' frontier AI ', '<br>frontier AI ', 1)
        folder = output / f"social-{market['slug']}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'index.html').write_text(template.substitute(
            name=html.escape(market['name']), headline=headline))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    prepare(parser.parse_args().output)
