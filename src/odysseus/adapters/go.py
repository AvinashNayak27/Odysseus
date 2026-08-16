"""Go analysis plans based on verified manifests and explicit profiler capability."""

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
    capture_hashes,
    locked_files,
    parse_metric,
    safe_workdir,
    unavailable_plan,
)


class GoAdapter(AdapterBase, LanguageAdapter):
    name = "go"

    def detect(self, workdir: Path) -> AdapterDetection | None:
        manifest = safe_workdir(workdir) / "go.mod"
        return AdapterDetection(self.name, "high", (manifest,)) if manifest.is_file() else None

    def capture_toolchain(
        self,
        workdir: Path,
        *,
        tool_versions: Mapping[str, str],
        profiler_capabilities: Mapping[str, bool],
        target_architecture: str | None,
    ) -> ToolchainCapture:
        files = locked_files(workdir, ("go.mod", "go.sum", "go.work"))
        if not files or files[0].name != "go.mod":
            raise ValueError("Go adapter requires go.mod")
        return ToolchainCapture(
            self.name,
            tuple(sorted(tool_versions.items())),
            capture_hashes(workdir, files),
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
            "Record the Yukon correctness gate before any Go-specific profile.",
        )

    def profile_plan(
        self, workdir: Path, *, artifact_root: Path, profiler_capabilities: Mapping[str, bool]
    ) -> ProfilePlan:
        root = safe_workdir(workdir)
        artifact = artifact_root.resolve(strict=False) / "go-cpu.pprof"
        if not profiler_capabilities.get("go_test", False):
            return unavailable_plan(self.name, root, "go test profiler capability is unavailable")
        return ProfilePlan(
            self.name,
            "available",
            "go",
            ("go", "test", "./...", "-run=^$", "-bench=.", f"-cpuprofile={artifact}"),
            root,
            artifact,
            "Run only after confirming the benchmark's approved Go test surface.",
        )

    def parse_metrics(self, output: str, metric: MetricSpec) -> ParsedMetric:
        return parse_metric(output, metric)
