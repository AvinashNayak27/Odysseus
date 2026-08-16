from __future__ import annotations

import pytest

from odysseus.experiments.environment import EnvironmentFingerprintError, fingerprint_environment


def test_environment_fingerprint_is_stable_and_does_not_accept_sensitive_fields() -> None:
    first = fingerprint_environment({"python_version": "3.12", "machine": "x86_64"})
    second = fingerprint_environment({"machine": "x86_64", "python_version": "3.12"})
    assert first == second
    with pytest.raises(EnvironmentFingerprintError):
        fingerprint_environment({"api_token": "synthetic"})
    with pytest.raises(EnvironmentFingerprintError):
        fingerprint_environment({"cwd": "/home/private/work"})
