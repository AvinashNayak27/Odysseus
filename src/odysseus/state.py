"""Immutable run directories and append-only JSONL event records."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from odysseus.config import validate_record
from odysseus.models import EventRecord, StageStatus
from odysseus.paths import require_runtime_root
from odysseus.redaction import redact_private


class StateError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class StateStore:
    """Private append-only events plus sealed artifact manifests for each run."""

    def __init__(self, state_root: Path, checkout_roots: tuple[Path, ...] = ()) -> None:
        self.root = require_runtime_root(state_root, checkout_roots)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise StateError("state root is not a directory")

    def create_run(self) -> str:
        for _ in range(32):
            run_id = f"run-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}-{uuid.uuid4().hex[:12]}"
            path = self.root / "runs" / run_id
            try:
                path.mkdir(mode=0o700, parents=True)
            except FileExistsError:
                continue
            self._fsync_dir(path.parent)
            return run_id
        raise StateError("could not allocate collision-safe run ID")

    def run_path(self, run_id: str) -> Path:
        if not run_id.startswith("run-") or "/" in run_id or "\\" in run_id:
            raise StateError("invalid run ID")
        path = self.root / "runs" / run_id
        if not path.is_dir():
            raise StateError("unknown run ID")
        return path

    def append_event(
        self, run_id: str, stage: str, status: StageStatus, payload: dict[str, Any]
    ) -> EventRecord:
        event = EventRecord(
            uuid.uuid4().hex, run_id, stage, status, utc_now(), self._scrub(payload)
        )
        record = event.as_dict()
        validate_record(record, "event")
        events_path = self.run_path(run_id) / "events.jsonl"
        self._append_locked(
            events_path, json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        )
        return event

    def write_artifact(
        self, run_id: str, relative_name: str, payload: bytes
    ) -> dict[str, str | int]:
        if (
            not relative_name
            or Path(relative_name).is_absolute()
            or ".." in Path(relative_name).parts
        ):
            raise StateError("artifact name must be a relative containment-safe path")
        directory = self.run_path(run_id) / "artifacts"
        if (directory / "manifest.json").exists():
            raise StateError("run is sealed; immutable artifacts cannot be added")
        target = directory / relative_name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.exists():
            raise StateError("immutable artifact already exists")
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise StateError(
                "foundation artifacts must be UTF-8 so they can be secret-scrubbed"
            ) from error
        scrubbed = redact_private(text).encode("utf-8")
        self._atomic_write(target, scrubbed)
        return {"path": str(target), "sha256": sha256_bytes(scrubbed), "bytes": len(scrubbed)}

    def seal_run(self, run_id: str) -> dict[str, object]:
        """Write the one immutable manifest against which ``verify-run`` compares bytes."""
        directory = self.run_path(run_id) / "artifacts"
        manifest_path = directory / "manifest.json"
        if manifest_path.exists():
            raise StateError("run is already sealed")
        entries: list[dict[str, str]] = []
        if directory.exists():
            for path in sorted(directory.rglob("*")):
                if path.is_file() and path != manifest_path:
                    entries.append(
                        {
                            "name": str(path.relative_to(directory)),
                            "sha256": sha256_bytes(path.read_bytes()),
                        }
                    )
        manifest = {
            "schemaVersion": 1,
            "report_id": run_id,
            "run_id": run_id,
            "recorded_at": utc_now(),
            "artifacts": entries,
        }
        validate_record(manifest, "report-manifest")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._atomic_write(
            manifest_path, (json.dumps(manifest, sort_keys=True) + "\n").encode("utf-8")
        )
        return manifest

    def verify_run(self, run_id: str) -> tuple[bool, list[dict[str, str]]]:
        directory = self.run_path(run_id) / "artifacts"
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise StateError("run has no recorded artifact manifest")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            validate_record(manifest, "report-manifest")
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise StateError(f"artifact manifest is invalid: {error}") from error
        mismatches: list[dict[str, str]] = []
        expected = {item["name"]: item["sha256"] for item in manifest["artifacts"]}
        actual = {
            str(path.relative_to(directory)): sha256_bytes(path.read_bytes())
            for path in directory.rglob("*")
            if path.is_file() and path != manifest_path
        }
        for name in sorted(set(expected) | set(actual)):
            if expected.get(name) != actual.get(name):
                mismatches.append(
                    {
                        "name": name,
                        "expected": expected.get(name, "<missing>"),
                        "actual": actual.get(name, "<missing>"),
                    }
                )
        return not mismatches, mismatches

    @staticmethod
    def _scrub(value: Any) -> Any:
        if isinstance(value, str):
            return redact_private(value)
        if isinstance(value, dict):
            return {str(key): StateStore._scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [StateStore._scrub(item) for item in value]
        return value

    @staticmethod
    def _append_locked(path: Path, text: str) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            payload = memoryview(text.encode("utf-8"))
            while payload:
                payload = payload[os.write(descriptor, payload) :]
            os.fsync(descriptor)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        StateStore._fsync_dir(path.parent)

    @staticmethod
    def _atomic_write(target: Path, payload: bytes) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            StateStore._fsync_dir(target.parent)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
