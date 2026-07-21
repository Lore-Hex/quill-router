import re
from pathlib import Path

from scripts.check_price_coverage import (
    _DISCOVERABLE_MANIFEST_PROVIDERS,
    _GLM_DISCOVERABLE_PROVIDER_APIS,
)

ROOT = Path(__file__).resolve().parents[1]


def test_deploy_provider_secrets_include_priced_glm52_backends() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text()

    expected = {
        "DEEPINFRA_API_KEY": "trustedrouter-deepinfra-api-key",
        "FIREWORKS_API_KEY": "trustedrouter-fireworks-api-key",
        "NOVITA_API_KEY": "trustedrouter-novita-api-key",
        "BASETEN_API_KEY": "trustedrouter-baseten-api-key",
        "THINKING_MACHINES_API_KEY": "trustedrouter-thinking-machines-api-key",
        "WAFER_API_KEY": "trustedrouter-wafer-api-key",
        "CRUSOE_API_KEY": "trustedrouter-crusoe-api-key",
        "MAKORA_API_KEY": "trustedrouter-makora-api-key",
    }
    for env_name, secret_name in expected.items():
        assert f'add_secret_env_if_exists "{env_name}" "{secret_name}"' in rollout
        assert f'ensure_secret_from_env_file "{env_name}" "{secret_name}"' in secrets


def test_deploy_wires_athena_worker_prompt_secret() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text()

    assert "ATHENA_PROMPTS_FILE" in secrets
    assert (
        'ensure_secret_from_prompt_file "trustedrouter-athena-worker-prompt-v1" '
        '"$ATHENA_PROMPTS_FILE" "Worker Prompt V1"'
    ) in secrets
    assert (
        'add_secret_env_if_exists "TR_ATHENA_WORKER_PROMPT" "trustedrouter-athena-worker-prompt-v1"'
    ) in rollout


def test_hourly_kimi_discovery_has_narrow_secret_access_wiring() -> None:
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text()
    workflow = (ROOT / ".github/workflows/refresh-prices.yml").read_text()

    assert 'grant_tr_deploy_secret_access "trustedrouter-kimi-api-key"' in secrets
    assert "KIMI_API_KEY:trustedrouter-kimi-api-key" in workflow
    assert "no project-wide Secret" in workflow


def test_every_authenticated_discovery_feed_is_wired_to_narrow_secret_access() -> None:
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
    workflow_pairs = dict(
        re.findall(r"\b([A-Z][A-Z0-9_]+):(trustedrouter-[a-z0-9-]+)", workflow)
    )
    # Together is intentionally loaded in its own documented workflow step.
    workflow_pairs["TOGETHER_API_KEY"] = "trustedrouter-together-api-key"

    feeds = [
        (provider, env_names)
        for provider, _url, env_names, _normalize in _DISCOVERABLE_MANIFEST_PROVIDERS
        if env_names
    ]
    feeds.extend(
        (provider, env_names)
        for provider, _url, env_names in _GLM_DISCOVERABLE_PROVIDER_APIS
    )

    for provider, env_names in feeds:
        wired_env = next((name for name in env_names if name in workflow_pairs), None)
        assert wired_env is not None, (
            f"{provider} discovery requires one of {env_names}, but the hourly "
            "workflow loads none of them"
        )
        secret_name = workflow_pairs[wired_env]
        assert f'grant_tr_deploy_secret_access "{secret_name}"' in secrets, (
            f"{provider} discovery loads {secret_name}, but secrets.sh does not "
            "grant the refresh service account narrow access"
        )
