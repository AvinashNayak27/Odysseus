"""Rust/Cargo analysis plans for the selected Flock Rust MVP and future crates."""

from __future__ import annotations

import re
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

_PROFILE_KEYS = ("lto", "codegen-units", "incremental", "panic", "target-cpu")


class RustAdapter(AdapterBase, LanguageAdapter):
    """Inspect Cargo configuration without changing Cargo profiles or sources."""

    name = "rust"

    def detect(self, workdir: Path) -> AdapterDetection | None:
        manifest = safe_workdir(workdir) / "Cargo.toml"
        return AdapterDetection(self.name, "high", (manifest,)) if manifest.is_file() else None

    def capture_toolchain(
        self,
        workdir: Path,
        *,
        tool_versions: Mapping[str, str],
        profiler_capabilities: Mapping[str, bool],
        target_architecture: str | None,
    ) -> ToolchainCapture:
        root = safe_workdir(workdir)
        manifest = root / "Cargo.toml"
        if not manifest.is_file():
            raise ValueError("Rust adapter requires Cargo.toml")
        files = locked_files(
            root, ("Cargo.toml", "Cargo.lock", "rust-toolchain", "rust-toolchain.toml")
        )
        settings = tuple(
            sorted(_cargo_profile_settings(manifest.read_text(encoding="utf-8")).items())
        )
        return ToolchainCapture(
            self.name,
            tuple(sorted(tool_versions.items())),
            capture_hashes(root, files),
            target_architecture,
            settings,
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
            (
                "Capture Cargo settings but do not alter lto, codegen-units, "
                "or target-cpu during baseline."
            ),
        )

    def profile_plan(
        self,
        workdir: Path,
        *,
        artifact_root: Path,
        profiler_capabilities: Mapping[str, bool],
    ) -> ProfilePlan:
        root = safe_workdir(workdir)
        artifact = artifact_root.resolve(strict=False) / "rust-perf.data"
        if not profiler_capabilities.get("perf", False):
            return unavailable_plan(
                self.name, root, "perf capability is unavailable or lacks permission"
            )
        return ProfilePlan(
            self.name,
            "available",
            "perf",
            ("perf", "record", "-o", str(artifact), "--", "yukon", "run"),
            root,
            artifact,
            "Profile the same locked Yukon run contract; no Cargo command is inferred from source.",
        )

    def parse_metrics(self, output: str, metric: MetricSpec) -> ParsedMetric:
        return parse_metric(output, metric)


def _cargo_profile_settings(text: str) -> dict[str, str]:
    """Capture known performance settings as text; TOML is never evaluated."""
    values: dict[str, str] = {}
    active_profile = False
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            active_profile = stripped in {"[profile.release]", "[profile.bench]"}
            continue
        if not active_profile:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_-]+)\s*=\s*(.+)", stripped)
        if match and match.group(1) in _PROFILE_KEYS:
            values[match.group(1)] = match.group(2).strip()
    return values
