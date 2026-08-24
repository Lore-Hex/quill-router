from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx

DRACO_DATASET = "perplexity-ai/draco"
DRACO_CONFIG = "default"
DRACO_SPLIT = "test"
DRACO_TASK_COUNT = 100
DRACO_DATASETS_SERVER_URL = "https://datasets-server.huggingface.co/rows"

# Do not let search/fetch see the benchmark answers or the article we are
# reproducing. Keep academic sources available because many DRACO tasks need
# primary papers.
DRACO_EXCLUDED_SEARCH_DOMAINS: tuple[str, ...] = (
    "huggingface.co",
    "datasets-server.huggingface.co",
    "openrouter.ai",
)
DRACO_TASK_FILTERS = ("all", "non-financial")
DracoTaskFilter = Literal["all", "non-financial"]


@dataclass(frozen=True)
class DracoTask:
    id: str
    domain: str
    problem: str
    rubric: dict[str, Any]

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "domain": self.domain,
            "problem": self.problem,
        }

    def cache_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "domain": self.domain,
            "problem": self.problem,
            "rubric": self.rubric,
        }


def fetch_draco_tasks(
    *,
    client: httpx.Client | None = None,
    offset: int = 0,
    length: int = DRACO_TASK_COUNT,
    timeout_seconds: float = 30.0,
) -> tuple[DracoTask, ...]:
    if length < 1:
        raise ValueError("length must be positive")
    if offset < 0:
        raise ValueError("offset cannot be negative")
    close_client = client is None
    http = client or httpx.Client(timeout=timeout_seconds)
    try:
        response = http.get(
            DRACO_DATASETS_SERVER_URL,
            params={
                "dataset": DRACO_DATASET,
                "config": DRACO_CONFIG,
                "split": DRACO_SPLIT,
                "offset": offset,
                "length": length,
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if close_client:
            http.close()
    return parse_draco_rows(payload)


def load_or_fetch_draco_tasks(
    cache_path: Path,
    *,
    refresh: bool = False,
    length: int = DRACO_TASK_COUNT,
    client: httpx.Client | None = None,
) -> tuple[DracoTask, ...]:
    if cache_path.exists() and not refresh:
        cached = load_draco_tasks(cache_path)
        if len(cached) >= length:
            return cached
    tasks = fetch_draco_tasks(client=client, length=length)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(draco_cache_artifact(tasks), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return tasks


def load_draco_tasks(path: Path) -> tuple[DracoTask, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and payload.get("schema") == "trustedrouter.draco.tasks.v1":
        rows = payload.get("tasks")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ValueError("DRACO cache must contain a tasks list")
    tasks = tuple(parse_draco_task(row) for row in rows)
    if not tasks:
        raise ValueError("DRACO cache contained no tasks")
    return tasks


def parse_draco_rows(payload: dict[str, Any]) -> tuple[DracoTask, ...]:
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Hugging Face DRACO response did not contain rows")
    tasks = []
    for item in rows:
        if not isinstance(item, dict) or not isinstance(item.get("row"), dict):
            raise ValueError("Hugging Face DRACO row had an unexpected shape")
        tasks.append(parse_draco_task(item["row"]))
    if not tasks:
        raise ValueError("Hugging Face DRACO response contained no tasks")
    return tuple(tasks)


def parse_draco_task(row: dict[str, Any]) -> DracoTask:
    task_id = _nonempty_string(row, "id")
    domain = _nonempty_string(row, "domain")
    problem = _nonempty_string(row, "problem")
    rubric_value = row.get("rubric")
    if rubric_value is None:
        rubric_value = row.get("answer")
    rubric = _parse_rubric(rubric_value)
    return DracoTask(id=task_id, domain=domain, problem=problem, rubric=rubric)


def draco_cache_artifact(tasks: tuple[DracoTask, ...]) -> dict[str, Any]:
    return {
        "schema": "trustedrouter.draco.tasks.v1",
        "dataset": DRACO_DATASET,
        "config": DRACO_CONFIG,
        "split": DRACO_SPLIT,
        "task_count": len(tasks),
        "excluded_search_domains": list(DRACO_EXCLUDED_SEARCH_DOMAINS),
        "tasks": [task.cache_dict() for task in tasks],
    }


def draco_public_task_artifact(tasks: tuple[DracoTask, ...]) -> dict[str, Any]:
    return {
        "schema": "trustedrouter.draco.public_tasks.v1",
        "dataset": DRACO_DATASET,
        "config": DRACO_CONFIG,
        "split": DRACO_SPLIT,
        "task_count": len(tasks),
        "tasks": [task.public_dict() for task in tasks],
    }


def filter_draco_tasks(
    tasks: tuple[DracoTask, ...], *, task_filter: DracoTaskFilter
) -> tuple[DracoTask, ...]:
    """Return the benchmark subset selected for the run.

    `non-financial` intentionally removes the DRACO finance slice, where the
    current bottleneck is table/PDF/filing extraction rather than model routing.
    """
    if task_filter == "all":
        return tasks
    if task_filter == "non-financial":
        return tuple(task for task in tasks if task.domain.lower() != "finance")
    raise ValueError(f"unsupported DRACO task filter: {task_filter}")


def _nonempty_string(row: dict[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"DRACO row is missing {field}")
    return value.strip()


def _parse_rubric(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("DRACO row is missing a JSON rubric")
