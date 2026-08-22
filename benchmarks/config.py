"""Loader for `orbit/config/vision_benchmark.yaml`.

Lives next to its consumer rather than in `orbit/policy.py`, following
`browser_policy_tools._load_blocklist`: policy.py holds loaders for files the
*safety layer* reads, and this one is read only by the benchmark. Read at
call time and never cached, matching every other config loader in this
project — an edit takes effect on the next run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[1] / "orbit" / "config" / "vision_benchmark.yaml"

VALID_SHAPES = {"point", "set_of_mark"}


@dataclass(frozen=True)
class Arm:
    id: str
    prompt_shape: str
    model: str | None


@dataclass(frozen=True)
class BenchmarkConfig:
    arms: list[Arm]
    max_marks: int
    request_timeout_s: float
    scenes: list[str]
    results_dir: Path
    max_retries: int
    retry_backoff_s: float
    concurrency: int


def load_benchmark_config(path: Path | None = None) -> BenchmarkConfig:
    raw = yaml.safe_load((path or CONFIG_PATH).read_text(encoding="utf-8")) or {}

    arms: list[Arm] = []
    for entry in raw.get("arms") or []:
        shape = entry.get("prompt_shape")
        if shape not in VALID_SHAPES:
            # Fail loudly rather than skipping: a typo'd shape would
            # otherwise drop an entire arm from the report and the missing
            # column would look like a result.
            raise ValueError(
                f"arm {entry.get('id')!r} has prompt_shape {shape!r}; "
                f"expected one of {sorted(VALID_SHAPES)}"
            )
        arms.append(Arm(id=entry["id"], prompt_shape=shape, model=entry.get("model")))
    if not arms:
        raise ValueError(f"{CONFIG_PATH} defines no arms to run")

    root = CONFIG_PATH.resolve().parents[2]
    run = raw.get("run", {})
    results = run.get("results_dir", "benchmarks/results")
    return BenchmarkConfig(
        arms=arms,
        max_marks=int(raw.get("candidates", {}).get("max_marks", 12)),
        request_timeout_s=float(run.get("request_timeout_s", 900)),
        scenes=list(run.get("scenes") or []),
        results_dir=(root / results),
        max_retries=int(run.get("max_retries", 3)),
        retry_backoff_s=float(run.get("retry_backoff_s", 20)),
        concurrency=int(run.get("concurrency", 1)),
    )
