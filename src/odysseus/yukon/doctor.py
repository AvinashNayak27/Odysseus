"""Read-only Yukon doctor facts with no installer execution path."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from odysseus.yukon.client import DoctorReport, YukonClient


@dataclass(frozen=True, slots=True)
class YukonDoctorFacts:
    report: DoctorReport
    workspace_writable: bool
    inspected_version: str | None


def inspect_yukon(client: YukonClient, *, cwd: Path, state_root: Path) -> YukonDoctorFacts:
    state_root.mkdir(parents=True, exist_ok=True)
    report = client.doctor(cwd=cwd)
    return YukonDoctorFacts(report, state_root.is_dir() and state_root.exists(), report.version)
