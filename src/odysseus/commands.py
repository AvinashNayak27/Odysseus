"""Shell-free, bounded subprocess execution for predeclared commands."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import BinaryIO, cast

from odysseus.models import CommandResult, CommandSpec
from odysseus.paths import canonical
from odysseus.redaction import redact_private


class CommandPolicyError(ValueError):
    pass


class CommandRunner:
    """Run only declared command specifications; never execute user-provided shell text."""

    def run(self, spec: CommandSpec, *, stdin: bytes | None = None) -> CommandResult:
        if spec.executable not in spec.allowed_executables:
            raise CommandPolicyError("executable is not allowlisted")
        if not spec.argv or spec.argv[0] != spec.executable:
            raise CommandPolicyError("argv must begin with the declared executable")
        if any("\x00" in part for part in spec.argv):
            raise CommandPolicyError("NUL bytes are not valid command arguments")
        if spec.allowed_argv_prefixes and not any(
            spec.argv[: len(prefix)] == prefix for prefix in spec.allowed_argv_prefixes
        ):
            raise CommandPolicyError("argv does not match an allowlisted command shape")
        cwd = canonical(Path(spec.cwd))
        if not cwd.is_dir():
            raise CommandPolicyError("command cwd must be an existing directory")
        if spec.timeout_seconds <= 0 or spec.max_output_bytes <= 0:
            raise CommandPolicyError("timeout and output cap must be positive")

        # env=None preserves inherited auth without exposing credential values to this API.
        # Captured child output is scrubbed before it becomes a result record.
        env = (
            None
            if spec.inherit_environment and spec.environment_allowlist is None
            else (
                self._inherited_environment(spec.environment_allowlist)
                if spec.inherit_environment
                else self._minimal_environment()
            )
        )
        started = time.monotonic()
        process = subprocess.Popen(
            list(spec.argv),
            cwd=str(cwd),
            env=env,
            shell=False,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_capture = _BoundedCapture(cast(BinaryIO, process.stdout), spec.max_output_bytes)
        stderr_capture = _BoundedCapture(cast(BinaryIO, process.stderr), spec.max_output_bytes)
        stdout_capture.start()
        stderr_capture.start()
        stdin_writer = (
            _StdinWriter(cast(BinaryIO | None, process.stdin), stdin) if stdin is not None else None
        )
        if stdin_writer is not None:
            stdin_writer.start()
        timed_out = False
        try:
            process.wait(timeout=spec.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate_group(process)
        if stdin_writer is not None:
            stdin_writer.join()
        stdout_capture.join()
        stderr_capture.join()
        process.stdout.close()
        process.stderr.close()
        duration = time.monotonic() - started
        stdout = stdout_capture.text()
        stderr = stderr_capture.text()
        return CommandResult(
            argv=tuple(redact_private(item, spec.extra_redaction_values) for item in spec.argv),
            cwd=str(cwd),
            exit_code=process.returncode,
            timed_out=timed_out,
            stdout=redact_private(stdout, spec.extra_redaction_values),
            stderr=redact_private(stderr, spec.extra_redaction_values),
            duration_seconds=duration,
            output_truncated=stdout_capture.truncated or stderr_capture.truncated,
        )

    @staticmethod
    def _minimal_environment() -> dict[str, str]:
        allowed = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "WINDIR")
        return {key: value for key in allowed if (value := os.environ.get(key)) is not None}

    @staticmethod
    def _inherited_environment(allowlist: tuple[str, ...] | None) -> dict[str, str]:
        if not allowlist:
            raise CommandPolicyError(
                "credential inheritance requires an explicit environment allowlist"
            )
        return {key: value for key in allowlist if (value := os.environ.get(key)) is not None}

    @staticmethod
    def _terminate_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=2)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class _StdinWriter(threading.Thread):
    """Write bounded declared stdin without blocking output-draining threads."""

    def __init__(self, stream: BinaryIO | None, payload: bytes) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.payload = payload

    def run(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.write(self.payload)
            self.stream.close()
        except BrokenPipeError:
            pass


class _BoundedCapture(threading.Thread):
    """Drain a child stream while retaining at most one configured output cap."""

    def __init__(self, stream: BinaryIO, limit: int) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.limit = limit
        self.buffer = bytearray()
        self.truncated = False

    def run(self) -> None:
        reader = self.stream
        while True:
            chunk = reader.read(65_536)
            if not chunk:
                return
            remaining = self.limit - len(self.buffer)
            if remaining > 0:
                self.buffer.extend(chunk[:remaining])
            if len(chunk) > remaining:
                self.truncated = True

    def text(self) -> str:
        text = bytes(self.buffer).decode("utf-8", errors="replace")
        return f"{text}\n[OUTPUT TRUNCATED]" if self.truncated else text
