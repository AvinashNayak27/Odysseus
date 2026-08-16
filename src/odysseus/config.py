"""Strict JSON configuration loading and packaged JSON Schema validation."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from odysseus.paths import PathPolicyError, require_runtime_root


class ConfigError(ValueError):
    pass


def load_schema(name: str) -> dict[str, Any]:
    """Load schemas packaged with the installed ``odysseus`` distribution."""
    resource = files("odysseus").joinpath("schemas", f"{name}.schema.json")
    try:
        with resource.open(encoding="utf-8") as handle:
            schema = cast(dict[str, Any], json.load(handle))
    except FileNotFoundError as error:
        raise ConfigError(f"packaged schema is missing: {name}") from error
    Draft202012Validator.check_schema(schema)
    return schema


def validate_record(record: dict[str, Any], schema_name: str) -> None:
    try:
        Draft202012Validator(load_schema(schema_name)).validate(record)
    except ValidationError as error:
        path = ".".join(str(part) for part in error.absolute_path) or "<root>"
        raise ConfigError(f"{schema_name} validation failed at {path}: {error.message}") from error


def load_config(path: Path, checkout_roots: tuple[Path, ...] = ()) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot load configuration: {error}") from error
    if not isinstance(config, dict):
        raise ConfigError("configuration root must be an object")
    validate_record(config, "config")
    try:
        workspace = cast(dict[str, Any], config["workspace"])
        require_runtime_root(
            Path(cast(str, workspace["state_root"])),
            (*checkout_roots, Path(cast(str, workspace["clone_root"]))),
        )
        reporting = cast(dict[str, Any], config["reporting"])
        if cast(int, reporting["note_min_bytes"]) > cast(int, reporting["note_max_bytes"]):
            raise ConfigError("reporting.note_min_bytes cannot exceed note_max_bytes")
    except (KeyError, TypeError, PathPolicyError) as error:
        raise ConfigError(f"invalid runtime state_root: {error}") from error
    return cast(dict[str, Any], config)
