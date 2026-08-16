"""TypeScript/Node adapter that separates compiler and runtime profiling plans."""

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


class TypeScriptAdapter(AdapterBase, LanguageAdapter):
    name = "typescript"

    def detect(self, workdir: Path) -> AdapterDetection | None:
        root = safe_workdir(workdir)
        package = root / "package.json"
        configs = tuple(sorted(root.glob("tsconfig*.json")))
        if package.is_file() and configs:
            return AdapterDetection(self.name, "high", (package, *configs))
        return None

    def capture_toolchain(
        self,
        workdir: Path,
        *,
        tool_versions: Mapping[str, str],
        profiler_capabilities: Mapping[str, bool],
        target_architecture: str | None,
    ) -> ToolchainCapture:
        root = safe_workdir(workdir)
        files = list(
            locked_files(root, ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"))
        )
        files.extend(sorted(root.glob("tsconfig*.json")))
        if not (root / "package.json").is_file():
            raise ValueError("TypeScript adapter requires package.json")
        return ToolchainCapture(
            self.name,
            tuple(sorted(tool_versions.items())),
            capture_hashes(root, files),
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
            "Distinguish a compiler-time metric from a Node runtime metric before profiling.",
        )

    def profile_plan(
        self, workdir: Path, *, artifact_root: Path, profiler_capabilities: Mapping[str, bool]
    ) -> ProfilePlan:
        root = safe_workdir(workdir)
        artifact = artifact_root.resolve(strict=False) / "typescript-trace"
        if profiler_capabilities.get("tsc", False):
            return ProfilePlan(
                self.name,
                "available",
                "tsc",
                ("tsc", "--extendedDiagnostics", f"--generateTrace={artifact}"),
                root,
                artifact,
                "Compiler-only profile plan; do not use for a Node runtime objective.",
            )
        if profiler_capabilities.get("node", False):
            return ProfilePlan(
                self.name,
                "available",
                "node",
                ("node", "--cpu-prof", f"--cpu-prof-dir={artifact}"),
                root,
                artifact,
                "Runtime profile requires the separately locked Node entrypoint.",
            )
        return unavailable_plan(
            self.name, root, "Neither TypeScript nor Node profiler capability is available"
        )

    def parse_metrics(self, output: str, metric: MetricSpec) -> ParsedMetric:
        return parse_metric(output, metric)
