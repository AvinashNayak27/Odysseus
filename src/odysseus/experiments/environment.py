"""Deterministic, privacy-safe environment fingerprinting."""

from __future__ import annotations

import json
import platform
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256


class EnvironmentFingerprintError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EnvironmentFingerprint:
    fields: tuple[tuple[str, str], ...]
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {"fields": dict(self.fields), "sha256": self.sha256}


def fingerprint_environment(fields: Mapping[str, object]) -> EnvironmentFingerprint:
    """Hash explicit non-secret facts; reject paths and credential-like field names."""
    normalized: list[tuple[str, str]] = []
    for key, value in fields.items():
        clean_key = str(key).strip()
        clean_value = str(value).strip()
        if not clean_key or any(
            marker in clean_key.casefold()
            for marker in ("token", "secret", "password", "credential")
        ):
            raise EnvironmentFingerprintError("environment fingerprints cannot include credentials")
        if clean_value.startswith(("/home/", "/Users/")) or "\\Users\\" in clean_value:
            raise EnvironmentFingerprintError(
                "environment fingerprints cannot include private paths"
            )
        normalized.append((clean_key, clean_value))
    ordered = tuple(sorted(normalized))
    payload = json.dumps(dict(ordered), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return EnvironmentFingerprint(ordered, sha256(payload.encode("utf-8")).hexdigest())


def local_environment_fingerprint(
    extra: Mapping[str, object] | None = None,
) -> EnvironmentFingerprint:
    """Capture small host/tool facts without inspecting the process environment."""
    fields: dict[str, object] = {
        "machine": platform.machine(),
        "os_release": platform.release(),
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "system": platform.system(),
    }
    fields.update(extra or {})
    return fingerprint_environment(fields)
