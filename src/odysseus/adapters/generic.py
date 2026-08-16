"""Conservative fallback adapter that never infers a command from source text."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from odysseus.adapters.base import (
    AdapterBase,
    AdapterDetection,
    LanguageAdapter,
    MetricSpec,
    ParsedMetric,
    ProfilePlan,
    ToolchainCapture,
    parse_metric,
    safe_workdir,
    unavailable_plan,
)


class GenericAdapter(AdapterBase, LanguageAdapter):
    name = "generic"

    def detect(self, workdir: Path) -> AdapterDetection:
        return AdapterDetection(self.name, "fallback", ())

    def capture_toolchain(
        self,
        workdir: Path,
        *,
        tool_versions: Mapping[str, str],
        profiler_capabilities: Mapping[str, bool],
        target_architecture: str | None,
    ) -> ToolchainCapture:
        safe_workdir(workdir)
        return ToolchainCapture(
            self.name,
            tuple(sorted(tool_versions.items())),
            (),
            target_architecture,
            (),
            tuple(sorted(profiler_capabilities.items())),
        )

    def baseline_plan(self, workdir: Path) -> ProfilePlan:
        return ProfilePlan(
            self.name,
            "canonical_yukon_run_required",
            "yukon",
            ("yukon", "run"),
            safe_workdir(workdir),
            None,
            "Use only the locked Yukon run contract for a generic benchmark.",
        )

    def profile_plan(
        self,
        workdir: Path,
        *,
        artifact_root: Path,
        profiler_capabilities: Mapping[str, bool],
    ) -> ProfilePlan:
        return unavailable_plan(
            self.name, workdir, "No generic profiler is inferred from untrusted source text."
        )

    def parse_metrics(self, output: str, metric: MetricSpec) -> ParsedMetric:
        return parse_metric(output, metric)
