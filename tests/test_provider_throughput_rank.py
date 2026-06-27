from __future__ import annotations

import datetime as dt

from scripts.update_provider_throughput_rank import (
    build_rank_block,
    measured_rank,
    parse_provider_rows,
    replace_rank_block,
)

LEADERBOARD_HTML = """
<table class="leaderboard-table">
  <thead>
    <tr>
      <th>#</th><th>Provider</th><th>Models</th><th>p50 TTFT</th>
      <th>Throughput</th><th>Uptime</th><th>Errors</th><th>Config excluded</th><th>Samples</th>
    </tr>
  </thead>
  <tbody>
    <tr><td>1</td><td>deepseek</td><td>2</td><td>3521 ms</td><td>53 tok/s</td><td>100.00%</td><td>—</td><td>—</td><td>111</td></tr>
    <tr><td>2</td><td>baseten</td><td>11</td><td>3349 ms</td><td>55 tok/s</td><td>98.40%</td><td>—</td><td>—</td><td>125</td></tr>
    <tr><td>3</td><td>novita</td><td>27</td><td>3534 ms</td><td>9 tok/s</td><td>92.11%</td><td>provider_error 8%</td><td>—</td><td>38</td></tr>
    <tr><td>4</td><td>openai</td><td>11</td><td>2579 ms</td><td>—</td><td>100.00%</td><td>—</td><td>—</td><td>44</td></tr>
    <tr><td>5</td><td>crusoe</td><td>12</td><td>2888 ms</td><td>2 tok/s</td><td>100.00%</td><td>—</td><td>—</td><td>24</td></tr>
  </tbody>
</table>
"""


def test_parse_provider_rows_from_public_leaderboard_table() -> None:
    rows = parse_provider_rows(LEADERBOARD_HTML)

    assert rows[0].provider == "deepseek"
    assert rows[0].throughput_tokens_per_second == 53
    assert rows[0].uptime == 1.0
    assert rows[0].samples == 111
    assert rows[0].p50_ttft_ms == 3521
    assert rows[3].throughput_tokens_per_second is None


def test_measured_rank_uses_positive_throughput_with_sample_and_uptime_guards() -> None:
    rows = parse_provider_rows(LEADERBOARD_HTML)

    assert measured_rank(rows) == ["baseten", "deepseek"]


def test_build_rank_block_places_measured_providers_before_secondary_priors() -> None:
    block = build_rank_block(
        ["baseten", "deepseek"], generated_date=dt.date(2026, 6, 27)
    )

    assert '"baseten": 0' in block
    assert '"deepseek": 1' in block
    assert '"cerebras": 20' in block
    assert '"trustedrouter": 99' in block


def test_replace_rank_block_is_targeted() -> None:
    source = '''
_OTHER = {}

# Throughput-first routing rank. Lower values are tried first for
# `provider.sort = "throughput"` and `:nitro`.
_THROUGHPUT_RANK = {
    "old": 0,
}

def keep_me() -> None:
    pass
'''

    updated = replace_rank_block(source, '_THROUGHPUT_RANK = {\n    "new": 0,\n}')

    assert '"old"' not in updated
    assert '"new": 0' in updated
    assert "def keep_me" in updated
