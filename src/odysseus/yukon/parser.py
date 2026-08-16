"""Strict parsers for Yukon v2026.08.15-1 human CLI output.

The observed CLI is ANSI-decorated and human-formatted: benchmark lists use an
aligned table and benchmark details use aligned label/value rows, not JSON.
These parsers are intentionally version-gated and fail closed on layout drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

ANSI_CONTROL_RE: Final = re.compile(
    r"(?:\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]"
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f])"
)
SUPPORTED_CLI_VERSION: Final = "2026.08.15-1"
_TABLE_COLUMNS: Final = ("benchmark", "status", "category", "goal", "best", "closes", "id")
UUID_RE: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_ALIGNED_ROW: Final = re.compile(r"^(?P<label>[A-Za-z][A-Za-z ]*?)(?::\s*|\s{2,})(?P<value>\S.*)$")
_SOURCE_AT_REF: Final = re.compile(r"^(?P<url>\S+)\s+@\s+(?P<ref>\S+)$")


class YukonParseError(ValueError):
    """Raised when the observed human-format contract is not satisfied."""


@dataclass(frozen=True, slots=True)
class YukonVersion:
    value: str


@dataclass(frozen=True, slots=True)
class BenchmarkSummary:
    benchmark: str
    status: str
    category: str
    goal: str
    best: str
    closes: str
    benchmark_id: str


@dataclass(frozen=True, slots=True)
class BenchmarkDetails:
    benchmark_id: str
    category: str
    goal: str
    current_best: str
    closes: str
    source_url: str
    source_ref: str
    source_branch: str
    score_path: str
    setup_command: str
    benchmark_command: str
    description: str


@dataclass(frozen=True, slots=True)
class CloneReport:
    root: str
    workdir: str
    editable_paths: tuple[str, ...]
    restart_requested: bool


def strip_ansi(text: str) -> str:
    """Remove CSI/OSC terminal sequences and other control characters."""
    return ANSI_CONTROL_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def is_benchmark_uuid(value: str) -> bool:
    """Return whether a Yukon discovery identifier has the real UUID shape."""
    return UUID_RE.fullmatch(value) is not None


def parse_version(text: str) -> YukonVersion:
    cleaned = strip_ansi(text).strip()
    match = re.fullmatch(r"(?:yukon\s+)?v?(\d{4}\.\d{2}\.\d{2}-\d+)", cleaned, re.IGNORECASE)
    if not match:
        raise YukonParseError("unrecognized Yukon version output")
    return YukonVersion(match.group(1))


def require_supported_version(version: YukonVersion) -> None:
    if version.value != SUPPORTED_CLI_VERSION:
        raise YukonParseError(
            f"unsupported Yukon CLI version {version.value}; expected {SUPPORTED_CLI_VERSION}"
        )


def _table_header_and_rows(text: str) -> tuple[tuple[int, ...], list[tuple[str, ...]]]:
    lines = [line.strip() for line in strip_ansi(text).splitlines() if line.strip()]
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if "benchmark" in line.casefold() and "status" in line.casefold()
        ),
        None,
    )
    if header_index is None:
        raise YukonParseError("benchmark table header is missing")
    header = lines[header_index]
    columns = tuple(re.split(r"\s{2,}", header.casefold()))
    if columns != _TABLE_COLUMNS:
        raise YukonParseError("benchmark table columns differ from the versioned contract")
    rows: list[tuple[str, ...]] = []
    for line in lines[header_index + 1 :]:
        if set(line.strip()) <= {"-", "─", " ", "┼", "┬", "┴"}:
            continue
        values = tuple(re.split(r"\s{2,}", line))
        if len(values) != len(_TABLE_COLUMNS) or any(not value for value in values):
            raise YukonParseError("ambiguous benchmark table row")
        rows.append(values)
    if not rows:
        raise YukonParseError("benchmark table contains no rows")
    return tuple(), rows


def parse_benchmark_list(text: str, *, cli_version: YukonVersion) -> tuple[BenchmarkSummary, ...]:
    require_supported_version(cli_version)
    _, rows = _table_header_and_rows(text)
    records = tuple(BenchmarkSummary(*row) for row in rows)
    ids = [record.benchmark_id for record in records]
    if any(not is_benchmark_uuid(value) for value in ids) or len(set(ids)) != len(ids):
        raise YukonParseError("benchmark table has missing, malformed, or duplicate UUIDs")
    return records


_SHOW_FIELDS: Final = {
    "category": "category",
    "goal": "goal",
    "current best": "current_best",
    "closes": "closes",
    "source url": "source_url",
    "source branch": "source_branch",
    "score path": "score_path",
    "setup": "setup_command",
    "setup command": "setup_command",
    "benchmark": "benchmark_command",
    "benchmark command": "benchmark_command",
}


def parse_benchmark_show(
    text: str, *, cli_version: YukonVersion, benchmark_id: str
) -> BenchmarkDetails:
    """Parse the versioned aligned detail layout and unlabeled description."""
    require_supported_version(cli_version)
    if not is_benchmark_uuid(benchmark_id):
        raise YukonParseError("benchmark show requires a UUID-shaped benchmark ID")
    values: dict[str, str] = {}
    description_lines: list[str] = []
    fields_started = False
    for raw_line in strip_ansi(text).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _ALIGNED_ROW.fullmatch(line)
        field = _SHOW_FIELDS.get(match.group("label").casefold()) if match else None
        if field is None:
            if fields_started:
                description_lines.append(line)
            continue
        assert match is not None
        if field in values:
            raise YukonParseError(f"duplicate Yukon show field: {match.group('label')}")
        values[field] = match.group("value").strip()
        fields_started = True
    missing = sorted(set(_SHOW_FIELDS.values()) - set(values))
    if missing:
        raise YukonParseError(f"missing Yukon show fields: {', '.join(missing)}")
    source = _SOURCE_AT_REF.fullmatch(values.pop("source_url"))
    if source is None:
        raise YukonParseError("source URL must be rendered as '<url> @ <ref>'")
    description = "\n".join(description_lines).strip()
    if not description:
        raise YukonParseError("missing unlabeled trailing Yukon description")
    return BenchmarkDetails(
        benchmark_id=benchmark_id,
        source_url=source.group("url"),
        source_ref=source.group("ref"),
        description=description,
        **values,
    )


def parse_clone_report(text: str, *, cli_version: YukonVersion) -> CloneReport:
    """Read Yukon-reported aligned paths; never infer them from a clone target."""
    require_supported_version(cli_version)
    fields: dict[str, str] = {}
    editable: list[str] = []
    restart_requested = False
    in_editable = False
    for raw_line in strip_ansi(text).splitlines():
        line = raw_line.strip()
        lowered = line.casefold()
        if "restart" in lowered and ("agent" in lowered or "relaunch" in lowered):
            restart_requested = True
        match = _ALIGNED_ROW.fullmatch(line)
        label = match.group("label").casefold() if match else ""
        value = match.group("value").strip() if match else ""
        if label in {"editable paths", "editablepaths"}:
            in_editable = True
        elif label:
            in_editable = False
        if label in {"root", "repository root"}:
            if "root" in fields:
                raise YukonParseError("duplicate Yukon root")
            fields["root"] = value
        elif label in {"workdir", "work directory", "benchmark workdir"}:
            if "workdir" in fields:
                raise YukonParseError("duplicate Yukon workdir")
            fields["workdir"] = value
        elif label in {"editable paths", "editablepaths"}:
            editable.extend(part.strip() for part in value.split(",") if part.strip())
        elif line.startswith(("-", "*")) and in_editable:
            editable.append(line[1:].strip())
    if not fields.get("root") or not fields.get("workdir"):
        raise YukonParseError("clone output did not provide exact root and workdir")
    if not editable:
        raise YukonParseError("clone output did not provide editablePaths")
    if any(not value for value in editable) or len(set(editable)) != len(editable):
        raise YukonParseError("ambiguous editablePaths")
    return CloneReport(fields["root"], fields["workdir"], tuple(editable), restart_requested)
