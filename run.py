"""Headless entry point for the experiment matrix.

    python run.py --config configuration.yaml

Runs the k x i matrix (outer_repeats x inner_repeats) unattended. Same logic as
main.ipynb -- both only call lib.workflow.run_matrix.

k = number of allocations (the machines are reserved k times); i = how often
the single experiment.command runs per allocation. A brought-along lease is
reused for all k allocations; a self-created lease is created and deleted per
allocation.

Phases:
  0) Load + validate config, prepare site/keypair
  1) run_matrix: for each k -> select machines, provision/configure/validate,
     run the i commands + collect results, then tear the allocation down again

The rules below are passed to run_matrix, which selects once per allocation and
never reuses the previous allocation's machines.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from lib.chameleon import (
    get_node_name,
    node_has_at_most_ram_gib,
    node_has_mellanox,
)
from lib.config import load_config
from lib.paths import repo_root
from lib.workflow import prepare_site_and_keypair, run_matrix


# --- Selection rules (None/None = first available nodes) ------------------- #
def PREDICATE(node: object) -> bool:
    return node_has_mellanox(node) and node_has_at_most_ram_gib(node, 256)


def SORT_KEY(node: object) -> str:
    return str(get_node_name(node) or "")


# Machines that are never selected, in any allocation, e.g. ["P3-CPU-010"].
BLACKLIST: list[str] = []
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the experiment matrix (k x i).")
    parser.add_argument(
        "--config",
        default="configuration.yaml",
        help="Path to the YAML config (default: configuration.yaml)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    config_path = repo_root() / args.config if not Path(args.config).is_absolute() else Path(args.config)
    config = load_config(config_path)
    print("Loaded config:", config_path)

    # Phase 0: site/keypair (once).
    prepare_site_and_keypair(config)

    # Phase 1: matrix.
    now = datetime.now(timezone.utc)
    experiment_id = run_matrix(
        config,
        now=now,
        predicate=PREDICATE,
        sort_key=SORT_KEY,
        blacklist=BLACKLIST,
    )

    print("Finished experiment:", experiment_id)
    print("Results in:", repo_root() / "results" / experiment_id)


if __name__ == "__main__":
    main()
