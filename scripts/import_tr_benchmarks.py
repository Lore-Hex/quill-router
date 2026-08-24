"""Import TrustedRouter's own reproducible benchmark numbers into
benchmark_scores.json (source_class "T").

These are the panel runs from github.com/Lore-Hex/trustedrouter-benchmarks —
first-party but fully reproducible: every score links to the published per-item
replay JSON in that repo. We parse the rendered result tables out of that repo's
README (the source of truth for the published numbers), keep only models that
exist in our catalog, and merge the rows in idempotently (existing class A/B
vendor/leaderboard rows are preserved; previously-imported "T" rows are replaced).

Run: python scripts/import_tr_benchmarks.py /path/to/trustedrouter-benchmarks
Re-run whenever the panel is refreshed or new benchmarks (AIME/MATH-500/tau2/...)
are added to BENCHMARKS below.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from trusted_router.catalog import MODELS

HERE = Path(__file__).resolve().parents[1]
DATA = HERE / "src" / "trusted_router" / "data" / "benchmark_scores.json"
REPO = "https://github.com/Lore-Hex/trustedrouter-benchmarks"
SOURCE_NAME = "TrustedRouter Benchmarks"
SOURCE_CLASS = "T"  # first-party, reproducible (replays published)
AS_OF = "2026-06-18"

# (benchmark_key, README splice marker, headline-column index among data cells,
#  replay JSON in the repo, public config note). headline_col counts data cells
# AFTER rank+model, 0-based — every table here puts the headline score first.
BENCHMARKS = [
    ("ifeval", "IFEVAL", 0, "results/ifeval_panel.json",
     "100-prompt subset, 0-shot; Google's deterministic verifiers (no judge); score = avg of strict/loose x prompt/instruction"),
    ("gsm8k", "GSM8K", 0, "results/gsm8k_panel.json",
     "30-problem subset, deterministic numeric match (no judge); near-saturated, kept as a sanity check"),
    ("aider_polyglot", "AIDER_POLYGLOT", 0, "results/aider_polyglot_panel.json",
     "34 Exercism exercises (Python), pass@1, real unit tests (no judge)"),
    ("simpleqa_verified", "SIMPLEQA_VERIFIED", 0, "results/simpleqa_verified_panel.json",
     "250 closed-book questions, no tools; GPT-4.1 autorater (Google's exact prompt); 32768-token budget"),
    ("mmlu_pro", "MMLU_PRO", 0, "results/mmlu_pro_panel.json",
     "200-question stride-sampled subset (TIGER-Lab/MMLU-Pro), 10-choice CoT, letter-match; no judge"),
]


def _table_rows(readme: str, marker: str) -> list[list[str]]:
    block = re.search(rf"<!-- {marker}_RESULTS_START -->(.*?)<!-- {marker}_RESULTS_END -->", readme, re.S)
    if not block:
        return []
    rows = []
    for line in block.group(1).splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or cells[0] in ("Rank", "---:") or cells[0].startswith("---"):
            continue
        rows.append(cells)
    return rows


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit("usage: import_tr_benchmarks.py /path/to/trustedrouter-benchmarks")
    readme = (Path(argv[1]) / "README.md").read_text(encoding="utf-8")

    tr_rows: list[dict] = []
    for key, marker, headline_col, replay, note in BENCHMARKS:
        n = 0
        for cells in _table_rows(readme, marker):
            # cells: [rank, `model`, headline, ...]
            model_id = cells[1].strip("`")
            if model_id not in MODELS:
                continue
            try:
                score = float(cells[2 + headline_col])
            except (ValueError, IndexError):
                continue
            tr_rows.append({
                "model_id": model_id, "benchmark_key": key, "score": score, "unit": "percent",
                "source_name": SOURCE_NAME, "source_url": f"{REPO}/blob/main/{replay}",
                "source_class": SOURCE_CLASS, "as_of_date": AS_OF, "config_note": note,
            })
            n += 1
        print(f"  {key}: {n} models")

    existing = json.loads(DATA.read_text(encoding="utf-8"))
    kept = [r for r in existing.get("scores", []) if r.get("source_class") != SOURCE_CLASS]
    merged = kept + tr_rows
    DATA.write_text(json.dumps({"scores": merged}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {DATA}: {len(kept)} kept + {len(tr_rows)} TrustedRouter rows = {len(merged)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
