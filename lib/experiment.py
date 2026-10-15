"""Experiment identity and result layout.

One experiment run (one invocation of the root file) produces an
`experiment_id`. Below it, each allocation `k` and each run `i` within that
allocation gets a unique directory. Each `k` is its own allocation (servers,
and -- unless a lease is brought along -- lease), so `allocation.json` lives
per k:

    results/{experiment_id}/
        experiment.json          # matrix plan
        config_resolved.yaml     # resolved base config + experiment_id
        manifest.json            # index of all runs (written after each k)
        k{k}/
            allocation.json      # reservation_id, nodes, timestamps for this k
            i{i}/
                run.json         # run_id, command, timings, exit_code
                <raw data>

The timestamp for the `experiment_id` is passed in from the outside (`now`) so
that this module stays time-free and testable.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from lib.paths import repo_root


def new_experiment_id(name: str, now: datetime) -> str:
    """Unique id per experiment run: <name>-<timestamp>."""
    return f"{name}-{now.strftime('%Y%m%dT%H%M%SZ')}"


def run_id(experiment_id: str, k: int, i: int) -> str:
    """Unique id per k x i cell (allocation k, repeat i)."""
    return f"{experiment_id}__k{k}-i{i}"


def results_root() -> Path:
    path = repo_root() / "results"
    path.mkdir(parents=True, exist_ok=True)
    return path


def experiment_dir(experiment_id: str) -> Path:
    path = results_root() / experiment_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def allocation_dir(experiment_id: str, k: int) -> Path:
    path = experiment_dir(experiment_id) / f"k{k}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_dir(experiment_id: str, k: int, i: int) -> Path:
    path = allocation_dir(experiment_id, k) / f"i{i}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: str | Path, data: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
