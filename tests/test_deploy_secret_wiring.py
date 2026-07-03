from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_deploy_provider_secrets_include_priced_glm52_backends() -> None:
    rollout = (ROOT / "scripts/deploy/rollout.sh").read_text()
    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text()

    expected = {
        "DEEPINFRA_API_KEY": "trustedrouter-deepinfra-api-key",
        "FIREWORKS_API_KEY": "trustedrouter-fireworks-api-key",
        "NOVITA_API_KEY": "trustedrouter-novita-api-key",
        "BASETEN_API_KEY": "trustedrouter-baseten-api-key",
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
