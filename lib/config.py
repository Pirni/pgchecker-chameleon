from pathlib import Path
from typing import Any

import yaml


TYPE = "type"
REQUIRED = "required"
MIN = "min"


CONFIG_SCHEMA = {
    "chameleon": {
        "site": {TYPE: str, REQUIRED: True},
        "image_id": {TYPE: str, REQUIRED: True},
        "keypair_name": {TYPE: str, REQUIRED: True},
        "network": {TYPE: str, REQUIRED: True},
        "skip_validate": {TYPE: bool, REQUIRED: False},
    },
    "reservation": {
        "name": {TYPE: str, REQUIRED: True},
        "node_type": {TYPE: str, REQUIRED: True},
        "node_count": {TYPE: int, REQUIRED: True, MIN: 1},
        "lease_hours": {TYPE: int, REQUIRED: True, MIN: 1},
        "reservation_id": {TYPE: (str, type(None)), REQUIRED: False},
    },
    "cluster": {
        "name_prefix": {TYPE: str, REQUIRED: True},
        "processes_per_node": {TYPE: int, REQUIRED: True, MIN: 1},
    },
    "copy_to": {
        "files": {TYPE: list, REQUIRED: False},
    },
    "results": {
        "remote_dir": {TYPE: str, REQUIRED: True},
        "local_dir": {TYPE: str, REQUIRED: True},
    },
    # Matrix block (required): k (outer_repeats) allocations x i (inner_repeats)
    # runs of a single command per allocation.
    "experiment": {
        "name": {TYPE: str, REQUIRED: True},
        "working_dir": {TYPE: str, REQUIRED: True},
        "outer_repeats": {TYPE: int, REQUIRED: True, MIN: 1},
        "inner_repeats": {TYPE: int, REQUIRED: True, MIN: 1},
        "command": {TYPE: str, REQUIRED: True},
    },
}


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path)

    with path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    if config is None:
        raise ValueError(f"Config file is empty: {path}")

    if not isinstance(config, dict):
        raise ValueError(f"Config root must be a mapping/dict: {path}")

    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    validate_mapping(config, CONFIG_SCHEMA, path="")
    validate_experiment(config)


def validate_experiment(config: dict[str, Any]) -> None:
    experiment = config.get("experiment")

    if experiment is None:
        raise ValueError("Missing required config section: experiment")

    if not isinstance(experiment.get("command"), str) or not experiment["command"]:
        raise ValueError("experiment.command must be a non-empty string")


def experiment_plan(
    config: dict[str, Any],
) -> tuple[str, int, int, str]:
    """Return the matrix as (name, outer_repeats, inner_repeats, command).

    The experiment block is required. `outer_repeats` (k) is the number of
    allocations; `inner_repeats` (i) is how often the single `command` runs per
    allocation; `working_dir` is the shared working directory.
    """
    experiment = config["experiment"]

    return (
        experiment["name"],
        experiment["outer_repeats"],
        experiment["inner_repeats"],
        experiment["command"],
    )


def validate_mapping(data: dict[str, Any], schema: dict[str, Any], path: str) -> None:
    for key, rule in schema.items():
        full_key = join_path(path, key)

        if key not in data:
            if rule.get(REQUIRED, False):
                raise ValueError(f"Missing required config key: {full_key}")
            continue

        value = data[key]

        if is_nested_schema(rule):
            if not isinstance(value, dict):
                raise ValueError(f"Config key must be a mapping/dict: {full_key}")
            validate_mapping(value, rule, full_key)
        else:
            validate_value(full_key, value, rule)


def validate_value(key: str, value: Any, rule: dict[str, Any]) -> None:
    expected_type = rule.get(TYPE)

    if expected_type is not None and not isinstance(value, expected_type):
        expected = format_expected_type(expected_type)
        actual = type(value).__name__
        raise ValueError(f"Invalid type for {key}: expected {expected}, got {actual}")

    if value is None:
        return

    if MIN in rule and value < rule[MIN]:
        raise ValueError(f"Invalid value for {key}: expected >= {rule[MIN]}, got {value}")


def is_nested_schema(rule: Any) -> bool:
    return isinstance(rule, dict) and TYPE not in rule


def join_path(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def format_expected_type(expected_type: Any) -> str:
    if isinstance(expected_type, tuple):
        return " or ".join(t.__name__ for t in expected_type)

    return expected_type.__name__