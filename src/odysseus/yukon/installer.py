"""Installer policy: document manual installation only, never execute an installer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InstallerGuidance:
    supported: bool
    message: str


def installation_guidance() -> InstallerGuidance:
    """Return review-safe guidance without downloading or executing remote bytes."""
    return InstallerGuidance(
        supported=False,
        message=(
            "Integrated Yukon installation is intentionally unavailable. Review and run the "
            "current official Yukon installation instructions manually, then rerun doctor."
        ),
    )
