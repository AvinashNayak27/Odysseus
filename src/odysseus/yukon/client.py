"""Narrow Yukon CLI façade with no public write or destructive operations."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from odysseus.approvals import verify_approval
from odysseus.models import ApprovalPlan, CommandResult, CommandSpec
from odysseus.paths import canonical, is_token_present
from odysseus.yukon.parser import (
    SUPPORTED_CLI_VERSION,
    YukonVersion,
    is_benchmark_uuid,
    parse_version,
)
from odysseus.yukon.workspace import RestartGate

# The command runner owns values and must inherit only these variables.  This
# module records names only, never values. Trace/session hooks are retained so
# an approved Yukon invocation has the same platform context as a human CLI.
YUKON_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "YUKON_API_TOKEN",
    "YUKON_API_URL",
    "YUKON_TRACE",
    "YUKON_TRACE_ENABLED",
    "YUKON_SESSION_ID",
    "YUKON_AGENT",
    "YUKON_AGENT_SESSION",
    "YUKON_HOOK",
    "YUKON_HOOKS",
)

# Retry only failures that the local process result identifies as transient.
# Authentication, authorization, parsing, and all ordinary nonzero exits fail
# closed and are never retried.
_TRANSIENT_EXIT_CODES = frozenset({69, 75})
_TRANSIENT_OUTPUT_MARKERS = (
    "connection reset",
    "connection refused",
    "connection timed out",
    "network is unreachable",
    "temporary failure",
    "temporarily unavailable",
    "try again later",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
)


class YukonRunner(Protocol):
    """The secure foundation runner enforces the environment names in each command spec."""

    def run(self, spec: CommandSpec) -> CommandResult: ...


class YukonClientError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DoctorReport:
    binary: str
    binary_present: bool
    version: str | None
    version_supported: bool
    token_present: bool
    installation_guidance: str | None


class YukonClient:
    """Build only allowlisted Yukon requests; it intentionally has no write API."""

    def __init__(
        self,
        runner: YukonRunner,
        *,
        binary: str = "yukon",
        timeout_seconds: float = 30.0,
        read_retries: int = 0,
        max_output_bytes: int = 1_048_576,
    ):
        if not binary or Path(binary).name != binary:
            raise YukonClientError("Yukon binary must be a bare executable name")
        if timeout_seconds <= 0:
            raise YukonClientError("Yukon timeout must be positive")
        if isinstance(read_retries, bool) or not isinstance(read_retries, int) or read_retries < 0:
            raise YukonClientError("Yukon read_retries must be a non-negative integer")
        if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int) or max_output_bytes <= 0:
            raise YukonClientError("Yukon max_output_bytes must be a positive integer")
        self._runner = runner
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._read_retries = read_retries
        self._max_output_bytes = max_output_bytes

    def _spec(
        self, argv: tuple[str, ...], *, cwd: Path, timeout_seconds: float | None = None
    ) -> CommandSpec:
        return CommandSpec(
            executable=self._binary,
            argv=(self._binary, *argv),
            cwd=str(canonical(cwd)),
            timeout_seconds=timeout_seconds or self._timeout_seconds,
            allowed_executables=(self._binary,),
            allowed_argv_prefixes=(
                (self._binary, "version"),
                (self._binary, "benchmark", "list"),
                (self._binary, "benchmark", "show"),
                (self._binary, "clone"),
                (self._binary, "setup"),
                (self._binary, "run"),
                (self._binary, "submissions"),
                (self._binary, "sync", "--harness-only"),
            ),
            max_output_bytes=self._max_output_bytes,
            inherit_environment=True,
            environment_allowlist=YUKON_ENV_ALLOWLIST,
        )

    def _run(
        self, argv: tuple[str, ...], *, cwd: Path, timeout_seconds: float | None = None
    ) -> CommandResult:
        """Execute one attempt; callers opt into bounded retries explicitly."""
        return self._runner.run(self._spec(argv, cwd=cwd, timeout_seconds=timeout_seconds))

    def _read(
        self, argv: tuple[str, ...], *, cwd: Path, timeout_seconds: float | None = None
    ) -> CommandResult:
        """Retry only idempotent read commands after a timeout/transient result."""
        for attempt in range(self._read_retries + 1):
            result = self._run(argv, cwd=cwd, timeout_seconds=timeout_seconds)
            if not self._is_transient(result) or attempt == self._read_retries:
                return result
        raise AssertionError("bounded read retry loop must return")

    @staticmethod
    def _is_transient(result: CommandResult) -> bool:
        if result.timed_out or result.exit_code in _TRANSIENT_EXIT_CODES:
            return True
        output = f"{result.stdout}\n{result.stderr}".casefold()
        return result.exit_code != 0 and any(marker in output for marker in _TRANSIENT_OUTPUT_MARKERS)

    def doctor(self, *, cwd: Path) -> DoctorReport:
        """Read version facts only; missing Yukon yields human installation guidance."""
        present = shutil.which(self._binary) is not None
        if not present:
            return DoctorReport(
                binary=self._binary,
                binary_present=False,
                version=None,
                version_supported=False,
                token_present=is_token_present(),
                installation_guidance=(
                    "Install Yukon manually using its current official instructions, "
                    "then rerun doctor."
                ),
            )
        result = self._read(("version",), cwd=cwd)
        if result.exit_code != 0 or result.timed_out:
            return DoctorReport(self._binary, True, None, False, is_token_present(), None)
        version = parse_version(result.stdout)
        return DoctorReport(
            self._binary,
            True,
            version.value,
            version.value == SUPPORTED_CLI_VERSION,
            is_token_present(),
            None,
        )

    def version(self, *, cwd: Path) -> YukonVersion:
        result = self._read(("version",), cwd=cwd)
        if result.exit_code != 0 or result.timed_out:
            raise YukonClientError("Yukon version failed")
        return parse_version(result.stdout)

    def benchmark_list(self, *, cwd: Path) -> CommandResult:
        return self._read(("benchmark", "list"), cwd=cwd)

    def benchmark_show(self, benchmark_id: str, *, cwd: Path) -> CommandResult:
        return self._read(("benchmark", "show", _identifier(benchmark_id)), cwd=cwd)

    def clone_spec(
        self, benchmark_id: str, target: Path, *, cwd: Path, timeout_seconds: float
    ) -> CommandSpec:
        """Return the exact clone command for a human approval plan; do not execute it."""
        return self._spec(
            ("clone", _identifier(benchmark_id), str(canonical(target))),
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )

    def clone(
        self, benchmark_id: str, target: Path, *, cwd: Path, timeout_seconds: float
    ) -> CommandResult:
        return self._runner.run(
            self.clone_spec(benchmark_id, target, cwd=cwd, timeout_seconds=timeout_seconds)
        )

    def setup_spec(self, *, workdir: Path, timeout_seconds: float) -> CommandSpec:
        """Render setup for approval; execution is additionally restart-gated."""
        return self._spec(("setup",), cwd=workdir, timeout_seconds=timeout_seconds)

    def setup(
        self,
        *,
        workdir: Path,
        timeout_seconds: float,
        restart_gate: RestartGate,
        process_root: Path | None,
    ) -> CommandResult:
        """Run setup at workdir only after the clone restart contract is satisfied."""
        restart_gate.require_relaunched_at(process_root)
        return self._runner.run(self.setup_spec(workdir=workdir, timeout_seconds=timeout_seconds))

    def run_spec(self, *, workdir: Path, timeout_seconds: float) -> CommandSpec:
        """Render the canonical Yukon run; callers enforce topology separately."""
        return self._spec(("run",), cwd=workdir, timeout_seconds=timeout_seconds)

    def run(
        self,
        *,
        workdir: Path,
        timeout_seconds: float,
        restart_gate: RestartGate,
        process_root: Path | None,
    ) -> CommandResult:
        restart_gate.require_relaunched_at(process_root)
        return self._runner.run(self.run_spec(workdir=workdir, timeout_seconds=timeout_seconds))

    def submissions(self, benchmark_id: str, *, workdir: Path) -> CommandResult:
        return self._read(("submissions", _identifier(benchmark_id), "--all"), cwd=workdir)

    def sync_harness_only_spec(self, *, workdir: Path, timeout_seconds: float) -> CommandSpec:
        """Return the sole allowed Yukon sync command for hash-bound approval."""
        return self._spec(("sync", "--harness-only"), cwd=workdir, timeout_seconds=timeout_seconds)

    def sync_harness_only(
        self,
        *,
        workdir: Path,
        timeout_seconds: float,
        approval: ApprovalPlan,
        approval_hash: str,
    ) -> CommandResult:
        """Run only the hash-approved Yukon harness refresh command.

        The caller must snapshot editable-path hashes before and after this
        operation; a changed editable hash is a stop-and-quarantine condition.
        """
        expected = self.sync_harness_only_spec(workdir=workdir, timeout_seconds=timeout_seconds)
        verify_approval(approval, approval_hash)
        if approval.action != "yukon-sync-harness-only" or approval.command != expected:
            raise YukonClientError("approval does not bind the exact harness-only sync command")
        return self._runner.run(expected)


def _identifier(value: str) -> str:
    if not is_benchmark_uuid(value):
        raise YukonClientError("benchmark identifier must have the Yukon UUID shape")
    return value
