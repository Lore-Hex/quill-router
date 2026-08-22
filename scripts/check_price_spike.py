#!/usr/bin/env python3
"""Compare two catalog snapshots; fail on suspicious provider price spikes.

Used in `.github/workflows/refresh-prices.yml` between the
provider-direct refresh step and the auto-commit step. The hourly
auto-rollback already catches catalog-shape regressions, so this only
needs to defend against literal-2x parsing-bug spikes.

The safety decision compares the same provider endpoint over time, not a
model's aggregate headline. A model headline can legitimately jump when its
cheapest route disappears or when a new route has a different input/output
mix. Comparing headlines froze every provider refresh in July 2026 even though
no continuing provider endpoint changed price.

Fails (exit 1) when the same stable provider endpoint:
  * increases prompt OR completion cost by ≥ 2×; OR
  * changes both prompt and completion to literal 0.

Removed models are noted in --summary output but do not fail.

Usage:
    python scripts/check_price_spike.py BEFORE.json AFTER.json
    python scripts/check_price_spike.py BEFORE.json AFTER.json --summary
    python scripts/check_price_spike.py BEFORE.json AFTER.json \
        --before-provider-manifests BEFORE_DIR \
        --after-provider-manifests AFTER_DIR
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

DEFAULT_SPIKE_RATIO = 2.0  # 100% increase = ≥2× the previous value

# Exact, provider-endpoint-scoped transitions confirmed against an upstream
# source. These approvals do not weaken the general spike gate: a different
# route, dimension, old value, or new value still fails closed.
APPROVED_ENDPOINT_PRICE_TRANSITIONS = frozenset(
    {
        # DeepSeek's first-party schedule effective 2026-08-16. The public
        # pricing table is the source of truth for both off-peak baselines:
        # https://api-docs.deepseek.com/quick_start/pricing/
        (
            "deepseek/deepseek-v4-flash [deepseek:deepseek:deepseek-v4-flash]",
            "completion",
            Decimal("0.00000028"),
            Decimal("0.00000066"),
        ),
        (
            "deepseek/deepseek-v4-pro [deepseek:deepseek:deepseek-v4-pro]",
            "completion",
            Decimal("0.00000087"),
            Decimal("0.00000198"),
        ),
        # GMI's public billing API changed the V4 Pro discount from roughly
        # 80% to 60%. Keep this approval pinned to the exact payable values:
        # https://console.gmicloud.ai/api/v1/billing/model_prices
        (
            "deepseek/deepseek-v4-pro "
            "[gmi:gmicloud/fp8:deepseek-ai/DeepSeek-V4-Pro]",
            "prompt",
            Decimal("0.000000347999"),
            Decimal("0.000000696"),
        ),
        (
            "deepseek/deepseek-v4-pro "
            "[gmi:gmicloud/fp8:deepseek-ai/DeepSeek-V4-Pro]",
            "completion",
            Decimal("0.000000695999"),
            Decimal("0.000001392"),
        ),
        # Atlas's authenticated /v1/models feed distinguishes the cheap
        # unversioned route from the newer 0731 weights. The old snapshot had
        # inherited the unversioned rate; approve only the exact correction.
        (
            "deepseek/deepseek-v4-flash-0731 "
            "[atlas-cloud:atlas-cloud:deepseek/deepseek-v4-flash-0731]",
            "prompt",
            Decimal("0.00000014"),
            Decimal("0.00000044"),
        ),
        (
            "deepseek/deepseek-v4-flash-0731 "
            "[atlas-cloud:atlas-cloud:deepseek/deepseek-v4-flash-0731]",
            "completion",
            Decimal("0.00000028"),
            Decimal("0.00000132"),
        ),
        # Novita's authenticated catalog and current public pricing table both
        # list the 0731 FP8 revision separately from the cheaper unversioned route:
        # https://novita.ai/pricing
        (
            "deepseek/deepseek-v4-flash-0731 "
            "[novita:novita/fp8:deepseek/deepseek-v4-flash-0731]",
            "prompt",
            Decimal("0.00000014"),
            Decimal("0.00000044"),
        ),
        (
            "deepseek/deepseek-v4-flash-0731 "
            "[novita:novita/fp8:deepseek/deepseek-v4-flash-0731]",
            "completion",
            Decimal("0.00000028"),
            Decimal("0.00000132"),
        ),
        # Fireworks' announced 2026-08-22 price change for DeepSeek V4 Flash
        # 0731. The input increase remains below the generic 2x gate; pin the
        # completion transition exactly so a parser error still fails closed:
        # https://docs.fireworks.ai/serverless/pricing
        (
            "deepseek/deepseek-v4-flash-0731 "
            "[fireworks:fireworks:accounts/fireworks/models/deepseek-v4-flash-0731]",
            "completion",
            Decimal("0.00000028"),
            Decimal("0.00000066"),
        ),
        (
            "moonshotai/kimi-k3 [tinfoil:tinfoil:kimi-k3]",
            "prompt",
            Decimal("0.000002"),
            Decimal("0.000004"),
        ),
        (
            "moonshotai/kimi-k3 [tinfoil:tinfoil:kimi-k3]",
            "completion",
            Decimal("0.000006"),
            Decimal("0.00002"),
        ),
    }
)


def _load(path: Path) -> dict[str, dict[str, str]]:
    """Return {model_id: {"prompt": str, "completion": str}} from a snapshot file."""
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, str]] = {}
    for model in snapshot.get("models", []):
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        pricing = model.get("pricing") or {}
        if not isinstance(model_id, str) or not isinstance(pricing, dict):
            continue
        out[model_id] = {
            "prompt": str(pricing.get("prompt") or "0"),
            "completion": str(pricing.get("completion") or "0"),
        }
    return out


def _endpoint_key(model_id: str, endpoint: dict[str, object]) -> str | None:
    provider = endpoint.get("tr_provider_slug") or endpoint.get("provider_name")
    upstream_model = endpoint.get("model_id")
    tag = endpoint.get("tag") or ""
    if not isinstance(provider, str) or not provider:
        return None
    if not isinstance(upstream_model, str) or not upstream_model:
        return None
    return f"{model_id} [{provider}:{tag}:{upstream_model}]"


def _load_endpoints(path: Path) -> dict[str, dict[str, str]]:
    """Return prices keyed by a stable model/provider/native-id route."""
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict[str, str]] = {}
    for model in snapshot.get("models", []):
        if not isinstance(model, dict) or not isinstance(model.get("id"), str):
            continue
        model_id = model["id"]
        for endpoint in model.get("endpoints") or []:
            if not isinstance(endpoint, dict):
                continue
            key = _endpoint_key(model_id, endpoint)
            pricing = endpoint.get("pricing") or {}
            if key is None or not isinstance(pricing, dict):
                continue
            out[key] = {
                "prompt": str(pricing.get("prompt") or "0"),
                "completion": str(pricing.get("completion") or "0"),
            }
    return out


def _manifest_price(
    value: object,
    *,
    scale: Decimal,
    location: str,
) -> str:
    """Normalize a provider-manifest rate to snapshot dollars per token."""
    if isinstance(value, bool):
        raise ValueError(f"{location}: price must be a non-negative number")
    try:
        parsed = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{location}: price must be a non-negative number") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{location}: price must be a non-negative number")

    # Provider manifests store microdollars per million tokens. A few legacy
    # manifests carry an explicit multiplier to reach that unit. Normalize to
    # the OpenRouter snapshot's dollars-per-token representation so approved
    # exact transitions and diagnostics remain comparable across both inputs.
    return str((parsed * scale) / Decimal("1000000000000"))


def _provider_manifest_paths(directory: Path) -> tuple[Path, ...]:
    if not directory.is_dir():
        raise ValueError(f"provider manifest directory is missing: {directory}")
    paths = tuple(sorted(directory.glob("*.json")))
    if not paths:
        raise ValueError(f"provider manifest directory has no JSON files: {directory}")
    return paths


def _manifest_scale(raw: dict[str, object], path: Path) -> Decimal:
    scale_value = raw.get(
        "price_scale_to_microdollars_per_million_tokens",
        1,
    )
    if isinstance(scale_value, bool):
        raise ValueError(f"provider manifest has an invalid price scale: {path}")
    try:
        scale = Decimal(str(scale_value))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"provider manifest has an invalid price scale: {path}") from exc
    if not scale.is_finite() or scale <= 0:
        raise ValueError(f"provider manifest has an invalid price scale: {path}")
    return scale


def _add_manifest_rate_pair(
    out: dict[str, dict[str, str]],
    *,
    key: str,
    pricing: dict[str, object],
    scale: Decimal,
    location: str,
    required: bool,
) -> None:
    has_prompt = "input_token_price_per_m" in pricing
    has_completion = "output_token_price_per_m" in pricing
    if not has_prompt and not has_completion:
        if required or "cached_input_token_price_per_m" in pricing:
            raise ValueError(f"{location} must publish both input and output prices")
        return
    if not has_prompt or not has_completion:
        raise ValueError(f"{location} must publish both input and output prices")
    if key in out:
        raise ValueError(f"duplicate provider-manifest rate: {key}")
    out[key] = {
        "prompt": _manifest_price(
            pricing["input_token_price_per_m"],
            scale=scale,
            location=f"{location}.input_token_price_per_m",
        ),
        "completion": _manifest_price(
            pricing["output_token_price_per_m"],
            scale=scale,
            location=f"{location}.output_token_price_per_m",
        ),
    }

    if "cached_input_token_price_per_m" not in pricing:
        return
    cached_key = f"{key} cached-input"
    if cached_key in out:
        raise ValueError(f"duplicate provider-manifest rate: {cached_key}")
    out[cached_key] = {
        "prompt": _manifest_price(
            pricing["cached_input_token_price_per_m"],
            scale=scale,
            location=f"{location}.cached_input_token_price_per_m",
        ),
        # Reuse check() without weakening its prompt-or-completion semantics.
        # Cached input has one billable dimension, so the unused side is zero.
        "completion": "0",
    }


def _manifest_tier_label(value: object, *, location: str) -> str:
    if value is None:
        return "uncapped"
    if isinstance(value, bool):
        raise ValueError(f"{location}: tier threshold must be a positive integer")
    try:
        threshold = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            f"{location}: tier threshold must be a positive integer"
        ) from exc
    if (
        not threshold.is_finite()
        or threshold <= 0
        or threshold != threshold.to_integral_value()
    ):
        raise ValueError(f"{location}: tier threshold must be a positive integer")
    return f"max={int(threshold)}"


def _load_provider_manifests(directory: Path) -> dict[str, dict[str, str]]:
    """Return every explicit rate from all supplemental provider manifests.

    The loader is intentionally strict: once the workflow opts into manifest
    comparison, a missing directory, malformed JSON, partial price pair, or
    duplicate stable route is an input failure rather than an empty successful
    comparison. Unpriced discovery/tombstone rows are skipped because they do
    not publish a token rate to compare.
    """
    out: dict[str, dict[str, str]] = {}
    for path in _provider_manifest_paths(directory):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read provider manifest {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"provider manifest must be an object: {path}")
        provider = raw.get("provider")
        if not isinstance(provider, str) or not provider:
            raise ValueError(f"provider manifest has no provider slug: {path}")
        if provider != path.stem:
            raise ValueError(
                f"provider manifest slug {provider!r} does not match {path.name}"
            )
        rows = raw.get("models")
        if not isinstance(rows, list):
            raise ValueError(f"provider manifest has no models list: {path}")

        scale = _manifest_scale(raw, path)

        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"{path}: models[{index}] must be an object")
            model_id = row.get("id")
            if not isinstance(model_id, str) or not model_id:
                raise ValueError(f"{path}: models[{index}] has no model id")
            upstream_model = row.get("upstream_id") or model_id
            if not isinstance(upstream_model, str) or not upstream_model:
                raise ValueError(f"{path}: models[{index}] has no upstream id")
            tag = row.get("tag") or provider
            if not isinstance(tag, str) or not tag:
                raise ValueError(f"{path}: models[{index}] has an invalid tag")
            key = f"{model_id} [{provider}:{tag}:{upstream_model}]"
            location = f"{path}: models[{index}]"
            _add_manifest_rate_pair(
                out,
                key=key,
                pricing=row,
                scale=scale,
                location=location,
                required=False,
            )

            raw_tiers = row.get("price_tiers")
            if raw_tiers is None:
                continue
            if not isinstance(raw_tiers, list) or not raw_tiers:
                raise ValueError(f"{location}.price_tiers must be a non-empty list")
            for tier_index, tier in enumerate(raw_tiers):
                tier_location = f"{location}.price_tiers[{tier_index}]"
                if not isinstance(tier, dict):
                    raise ValueError(f"{tier_location} must be an object")
                tier_label = _manifest_tier_label(
                    tier.get("max_prompt_tokens"),
                    location=tier_location,
                )
                _add_manifest_rate_pair(
                    out,
                    key=f"{key} tier[{tier_label}]",
                    pricing=tier,
                    scale=scale,
                    location=tier_location,
                    required=True,
                )
    return out


def _to_decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except Exception:  # noqa: BLE001
        return Decimal("0")


def check(
    before: dict[str, dict[str, str]],
    after: dict[str, dict[str, str]],
    spike_ratio: float = DEFAULT_SPIKE_RATIO,
) -> tuple[list[str], list[str], list[str]]:
    """Return (failures, changes, removed).

    failures: list of human-readable reasons why the workflow should fail
    changes: list of price-change lines (for --summary)
    removed: list of model ids present in before but not after
    """
    failures: list[str] = []
    changes: list[str] = []
    removed: list[str] = []
    spike = Decimal(str(spike_ratio))

    for model_id, prev in before.items():
        if model_id not in after:
            removed.append(model_id)
            continue
        cur = after[model_id]
        prev_p = _to_decimal(prev["prompt"])
        cur_p = _to_decimal(cur["prompt"])
        prev_c = _to_decimal(prev["completion"])
        cur_c = _to_decimal(cur["completion"])

        # Literal 2× spike on either dimension.
        for dim, prv, curv in (
            ("prompt", prev_p, cur_p),
            ("completion", prev_c, cur_c),
        ):
            if prv > 0 and curv >= prv * spike:
                if (
                    model_id,
                    dim,
                    prv,
                    curv,
                ) in APPROVED_ENDPOINT_PRICE_TRANSITIONS:
                    continue
                ratio = curv / prv
                failures.append(
                    f"{model_id} {dim}: {prv} → {curv} (×{ratio:.2f} ≥ ×{spike})"
                )

        # Both dimensions zeroed out.
        if prev_p > 0 and prev_c > 0 and cur_p == 0 and cur_c == 0:
            failures.append(
                f"{model_id}: both prompt and completion went to 0 "
                f"(was prompt={prev_p}, completion={prev_c})"
            )

        if prev_p != cur_p or prev_c != cur_c:
            direction_p = (
                "+" if cur_p > prev_p else "-" if cur_p < prev_p else "="
            )
            direction_c = (
                "+" if cur_c > prev_c else "-" if cur_c < prev_c else "="
            )
            changes.append(
                f"{model_id}: prompt {prev_p}{direction_p}{cur_p}, "
                f"completion {prev_c}{direction_c}{cur_c}"
            )

    return failures, changes, removed


def _summary_line(changes: list[str], removed: list[str]) -> str:
    n_changed = len(changes)
    if n_changed == 0 and not removed:
        return "no price changes"
    parts = [f"{n_changed} prices changed"]
    if removed:
        parts.append(f"{len(removed)} models removed")
    return ", ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument(
        "--summary",
        action="store_true",
        help="emit a one-line summary suitable for the commit body",
    )
    parser.add_argument(
        "--spike-ratio",
        type=float,
        default=DEFAULT_SPIKE_RATIO,
        help=f"fail when after/before >= this (default {DEFAULT_SPIKE_RATIO})",
    )
    parser.add_argument(
        "--before-provider-manifests",
        type=Path,
        help="directory of supplemental provider manifests before refresh",
    )
    parser.add_argument(
        "--after-provider-manifests",
        type=Path,
        help="directory of supplemental provider manifests after refresh",
    )
    args = parser.parse_args(argv)

    if (args.before_provider_manifests is None) != (
        args.after_provider_manifests is None
    ):
        print(
            "PRICE SPIKE INPUT ERROR: both provider-manifest directories are required",
            file=sys.stderr,
        )
        return 2

    before = _load(args.before)
    after = _load(args.after)
    _headline_failures, changes, removed = check(before, after, args.spike_ratio)
    before_endpoints = _load_endpoints(args.before)
    after_endpoints = _load_endpoints(args.after)
    if before_endpoints and after_endpoints:
        failures, _endpoint_changes, _removed_endpoints = check(
            before_endpoints,
            after_endpoints,
            args.spike_ratio,
        )
    else:
        # Keep the utility useful for compact fixtures and older snapshots that
        # predate endpoint pricing.
        failures = _headline_failures

    if args.before_provider_manifests is not None:
        try:
            before_manifest_names = {
                path.name
                for path in _provider_manifest_paths(args.before_provider_manifests)
            }
            after_manifest_names = {
                path.name
                for path in _provider_manifest_paths(args.after_provider_manifests)
            }
            missing_manifests = sorted(before_manifest_names - after_manifest_names)
            if missing_manifests:
                raise ValueError(
                    "provider manifests disappeared after refresh: "
                    + ", ".join(missing_manifests)
                )
            before_provider_routes = _load_provider_manifests(
                args.before_provider_manifests
            )
            after_provider_routes = _load_provider_manifests(
                args.after_provider_manifests
            )
        except ValueError as exc:
            print(f"PRICE SPIKE INPUT ERROR: {exc}", file=sys.stderr)
            return 2
        provider_failures, provider_changes, provider_removed = check(
            before_provider_routes,
            after_provider_routes,
            args.spike_ratio,
        )
        failures.extend(provider_failures)
        changes.extend(provider_changes)
        removed.extend(provider_removed)

    if args.summary:
        print(_summary_line(changes, removed))
        # Surface the actual per-model deltas (old → new), not just the
        # count — these land in the commit body + the Actions run summary
        # so a price change is VISIBLE without diffing the snapshot by hand.
        # (Drift-visibility fix: previously only the count was emitted, so
        # any sub-2x change was effectively silent.)
        for line in changes:
            print(line)
        if removed:
            print(f"removed: {', '.join(removed[:20])}" + (
                f" (+{len(removed) - 20} more)" if len(removed) > 20 else ""
            ))
        if failures:
            print("PRICE SPIKE FAILURES:")
            for line in failures:
                print(f"  {line}")
        return 1 if failures else 0

    for line in changes:
        print(line)
    if removed:
        print(f"removed ({len(removed)}): {', '.join(removed[:10])}" + (
            f" ... and {len(removed) - 10} more" if len(removed) > 10 else ""
        ))
    if failures:
        print("", file=sys.stderr)
        print("PRICE SPIKE FAILURES:", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
