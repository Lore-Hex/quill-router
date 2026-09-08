"""Render the production template with deterministic, metadata-only measurements."""

from trusted_router.config import Settings
from trusted_router.dashboard import public_leaderboard_html
from trusted_router.storage_models import ProviderBenchmarkSample
from trusted_router.synthetic.leaderboard import aggregate_leaderboard


def render() -> str:
    samples = []
    for number in range(61):
        provider = ("deepseek", "mistral", "anthropic")[number % 3]
        for attempt in range(10 if number < 60 else 1):
            samples.append(
                ProviderBenchmarkSample(
                    id=f"fixture-{number}-{attempt}",
                    model=f"demo/model-{number:02d}",
                    provider=provider,
                    provider_name=provider,
                    status="success",
                    usage_type="Credits",
                    streamed=True,
                    first_token_milliseconds=100 + number,
                    created_at="2026-09-05T12:00:00Z",
                )
            )
    samples.append(
        ProviderBenchmarkSample(
            id="fixture-config",
            model="demo/needs-configuration",
            provider="mistral",
            provider_name="mistral",
            status="unsupported",
            error_type="probe_config_error",
            usage_type="Credits",
            streamed=True,
            created_at="2026-09-05T12:00:00Z",
        )
    )
    snapshot = aggregate_leaderboard(
        samples, model_rank_min_samples=10, provider_rank_min_samples=30, rank_min_ttft_samples=3
    )
    snapshot["generated_at"] = "2026-09-05T12:00:00Z"
    return public_leaderboard_html(Settings(environment="test"), snapshot)


if __name__ == "__main__":
    print(render())
