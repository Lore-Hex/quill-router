"""Golden vectors for the Go port of canonical_router_model_id.

The edge passes any authorize whose model or models entry
canonical_router_model_id (model_aliases.py) rewrites (body.canonical_model,
docs/design/fast-admission-e2-decide.md §3); fastpath/internal/gwauth ports
the function to tell. This script writes Python's answer for every catalog
model id, every _ROUTER_MODEL_ID_ALIASES key and value, every
router_model_aliases spelling of a catalog id, and the system1models,
nyte/ and auto-routing spellings with and without a variant suffix, to
fastpath/testdata/model_alias_vectors_v1.json. The Go test holds the port to
every entry; tests/test_fastpath_model_alias_vectors.py fails when the file
is not what this script writes now, so a new alias or catalog model reaches
the port's test before any release.

    uv run python scripts/fastpath/model_alias_vectors.py [--print]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
# Nothing here may depend on the shell that runs it: settings start from
# defaults.
for _name in [name for name in os.environ if name.startswith("TR_")]:
    del os.environ[_name]

from trusted_router.catalog import MODELS  # noqa: E402
from trusted_router.model_aliases import (  # noqa: E402
    _ROUTER_MODEL_ID_ALIASES,
    canonical_router_model_id,
    router_model_aliases,
)

OUTPUT = ROOT / "fastpath" / "testdata" / "model_alias_vectors_v1.json"

# Spellings around each rule's edges: the publisher with and without a
# separator, the prefixes alone, the auto-routing base with a variant, and
# near misses that must stay as they are.
EDGE_SPELLINGS = (
    "system1models", "system1models/", "system1models/x", "system1models-eu/x", "system1models-eu",
    "system1modelsx/y", "xsystem1models/y", "System1Models/x",
    "nyte/", "nyte", "nyte/auto", "nyte/auto-routing", "nyte/auto-routing:nitro", "nyte/x:floor", "nyte/a/b",
    "Nyte/auto", "xnyte/auto",
    "trustedrouter/auto-routing", "trustedrouter/auto-routing:", "trustedrouter/auto-routing:floor",
    "trustedrouter/auto-routing:a:b", "trustedrouter/auto-routingx", "trustedrouter/auto-routing/x",
    "trustedrouter/auto", "trustedrouter/auto:nitro",
    "decisionmodelsio/x", "decisionmodelsio-eu/x", "", "/", ":", "a", "z-ai/glm-5.3",
)


def ids() -> list[str]:
    found: set[str] = set(EDGE_SPELLINGS)
    for model_id in MODELS:
        found.add(model_id)
        found.update(router_model_aliases(model_id))
        for variant in (":nitro", ":floor", ":free"):
            found.add(model_id + variant)
            found.update(alias + variant for alias in router_model_aliases(model_id))
        publisher, separator, native_id = model_id.partition("/")
        if separator:
            found.add("system1models/" + native_id)
            found.add("system1models-eu/" + native_id)
            found.add("nyte/" + native_id)
    for alias, canonical in _ROUTER_MODEL_ID_ALIASES.items():
        found.update((alias, canonical))
    return sorted(found)


def render() -> str:
    """One vector a line, sorted by id, so a change shows as the ids it changes."""
    lines = [json.dumps({"canonical": canonical_router_model_id(model_id), "id": model_id},
                        sort_keys=True, separators=(",", ":"), ensure_ascii=True)
             for model_id in ids()]
    head = json.dumps({"generator": "scripts/fastpath/model_alias_vectors.py",
                       "aliases": dict(sorted(_ROUTER_MODEL_ID_ALIASES.items()))},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True)[:-1]
    return head + ',\n "cases": [\n  ' + ",\n  ".join(lines) + "\n ]}\n"


if __name__ == "__main__":
    if sys.argv[1:] == ["--print"]:
        sys.stdout.write(render())
    else:
        OUTPUT.write_text(render())
        print(f"wrote {OUTPUT.relative_to(ROOT)}")
