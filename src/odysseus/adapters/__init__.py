"""Safe language adapter registry."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from odysseus.adapters.base import AdapterDetection, LanguageAdapter
from odysseus.adapters.generic import GenericAdapter
from odysseus.adapters.go import GoAdapter
from odysseus.adapters.rust import RustAdapter
from odysseus.adapters.typescript import TypeScriptAdapter

DEFAULT_ADAPTERS: tuple[LanguageAdapter, ...] = (
    RustAdapter(),
    GoAdapter(),
    TypeScriptAdapter(),
    GenericAdapter(),
)


def detect_adapters(
    workdir: Path, adapters: Sequence[LanguageAdapter] = DEFAULT_ADAPTERS
) -> tuple[AdapterDetection, ...]:
    """Return all specific detections, or the generic fallback exactly once."""
    detections = tuple(
        detection
        for adapter in adapters
        if adapter.name != "generic"
        if (detection := adapter.detect(workdir)) is not None
    )
    if detections:
        return detections
    generic = next(adapter for adapter in adapters if adapter.name == "generic")
    detection = generic.detect(workdir)
    assert detection is not None
    return (detection,)


__all__ = ("DEFAULT_ADAPTERS", "detect_adapters")
