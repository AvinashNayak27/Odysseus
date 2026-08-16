"""Safe discovery of declarative agent-skill prompt context.

Skills are data files only. This module never imports, evaluates, or executes
skill content, and only searches explicit configured directories plus the
repository/package skill roots shipped with Odysseus.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}(?:-[a-z0-9.-]+)?$")
_TERM = re.compile(r"^[a-z0-9][a-z0-9+.#-]{0,31}$")
_MANIFEST_FILES = frozenset(("skill.json", "SKILL.md"))
_DEFAULTS: dict[str, object] = {
    "directories": [],
    "enabled": [],
    "auto_select": False,
    "max_discovered_skills": 64,
    "max_selected_skills": 16,
    "max_skill_bytes": 16_384,
    "max_total_bytes": 131_072,
}


class SkillError(ValueError):
    """A declarative skill failed a security or format check."""


@dataclass(frozen=True, slots=True)
class SkillRecord:
    name: str
    description: str
    version: str
    languages: tuple[str, ...]
    categories: tuple[str, ...]
    tags: tuple[str, ...]
    content: str
    content_sha256: str
    source: str

    def prompt_record(self) -> dict[str, object]:
        """Return bounded provider data without leaking local filesystem paths."""
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "languages": list(self.languages),
            "categories": list(self.categories),
            "tags": list(self.tags),
            "content": self.content,
            "content_sha256": self.content_sha256,
        }

    def provenance(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version, "content_sha256": self.content_sha256}


def skill_directories(settings: dict[str, object]) -> tuple[Path, ...]:
    """Return deterministic allowed roots; never infer a home/global skills directory."""
    merged = _settings(settings)
    configured = merged["directories"]
    assert isinstance(configured, list)
    roots: list[Path] = []
    for value in configured:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise SkillError("skill directories must be absolute paths")
        roots.append(Path(value))
    # Repository skills exist only in a source checkout. Installed wheels use
    # package-bundled skills and must not mistake an unrelated parent for a repo.
    package_root = Path(__file__).resolve().parents[1]
    checkout_root = package_root.parents[1]
    repository_root = checkout_root / "skills"
    bundled_root = Path(__file__).resolve().parent / "bundled"
    shipped_root = (
        repository_root
        if (checkout_root / "pyproject.toml").is_file() and repository_root.is_dir()
        else bundled_root
    )
    if shipped_root.exists():
        roots.append(shipped_root)
    unique: dict[str, Path] = {}
    for root in roots:
        _safe_directory(root, "skill directory")
        resolved = root.resolve(strict=True)
        unique.setdefault(str(resolved), resolved)
    return tuple(unique[key] for key in sorted(unique))


def discover_skills(settings: dict[str, object]) -> tuple[SkillRecord, ...]:
    """Read only regular ``skill.json``/``SKILL.md`` pairs from allowed roots."""
    merged = _settings(settings)
    max_count = _positive(merged, "max_discovered_skills")
    max_skill_bytes = _positive(merged, "max_skill_bytes")
    max_total_bytes = _positive(merged, "max_total_bytes")
    records: list[SkillRecord] = []
    total_bytes = 0
    for root in skill_directories(merged):
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if entry.name.startswith("."):
                continue
            if entry.is_symlink():
                raise SkillError(f"skill entry must not be a symlink: {entry}")
            if not entry.is_dir():
                continue  # Root-level documentation is not a skill.
            record, byte_count = _read_skill(entry, root, max_skill_bytes)
            records.append(record)
            total_bytes += byte_count
            if len(records) > max_count:
                raise SkillError("discovered skill count exceeds configured limit")
            if total_bytes > max_total_bytes:
                raise SkillError("discovered skill content exceeds configured total byte limit")
    records.sort(key=lambda item: (item.name, item.version, item.source))
    duplicates = sorted(
        name for name in {item.name for item in records} if sum(item.name == name for item in records) > 1
    )
    if duplicates:
        raise SkillError(f"duplicate skill name: {', '.join(duplicates)}")
    return tuple(records)


def select_skills(
    records: tuple[SkillRecord, ...],
    settings: dict[str, object],
    *,
    language: str | None = None,
    category: str | None = None,
    tags: tuple[str, ...] = (),
) -> tuple[SkillRecord, ...]:
    """Select only explicit enabled records, optionally narrowing by trusted metadata."""
    merged = _settings(settings)
    enabled = merged["enabled"]
    assert isinstance(enabled, list)
    enabled_names = _terms(enabled, "enabled skill names", pattern=_NAME)
    if len(enabled_names) != len(set(enabled_names)):
        raise SkillError("enabled skill names must be unique")
    by_name = {record.name: record for record in records}
    unknown = sorted(set(enabled_names) - set(by_name))
    if unknown:
        raise SkillError(f"enabled skill was not discovered: {', '.join(unknown)}")
    selected = [by_name[name] for name in sorted(enabled_names)]
    if merged["auto_select"] is True and selected:
        if not language or not category:
            raise SkillError("skills.auto_select requires trusted benchmark language and category")
        requires_tags = any(record.tags for record in selected)
        if requires_tags and not tags:
            raise SkillError("skills.auto_select requires trusted benchmark tags for enabled tagged skills")
        trusted_tags = set(_terms(list(tags), "benchmark tags"))
        trusted_language = _term_or_none(language, "benchmark language")
        trusted_category = _term_or_none(category, "benchmark category")
        selected = [
            record
            for record in selected
            if _applicable(record, trusted_language, trusted_category, trusted_tags)
        ]
    limit = _positive(merged, "max_selected_skills")
    if len(selected) > limit:
        raise SkillError("selected skill count exceeds configured limit")
    return tuple(selected)


def _read_skill(directory: Path, root: Path, max_skill_bytes: int) -> tuple[SkillRecord, int]:
    _safe_directory(directory, "skill directory")
    try:
        directory.relative_to(root)
    except ValueError as error:
        raise SkillError("skill directory escapes configured root") from error
    entries = list(directory.iterdir())
    if {item.name for item in entries} != _MANIFEST_FILES:
        raise SkillError(
            f"skill directory must contain only skill.json and SKILL.md (no scripts/hooks/commands): {directory}"
        )
    files: dict[str, Path] = {}
    for item in entries:
        if item.is_symlink() or not item.is_file():
            raise SkillError(f"skill files must be regular non-symlink files: {item}")
        resolved = item.resolve(strict=True)
        if root not in resolved.parents:
            raise SkillError(f"skill path escapes configured root: {item}")
        files[item.name] = resolved
    manifest_raw = _read_bytes(files["skill.json"], max_skill_bytes, "skill manifest")
    content_raw = _read_bytes(files["SKILL.md"], max_skill_bytes, "skill body")
    if len(manifest_raw) + len(content_raw) > max_skill_bytes:
        raise SkillError("skill manifest and body exceed configured byte limit")
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
        content = content_raw.decode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SkillError(f"skill files must be UTF-8 JSON/Markdown: {directory}") from error
    if not content.strip() or "\x00" in content:
        raise SkillError(f"skill body must be non-empty and NUL-free: {directory}")
    if not isinstance(manifest, dict) or set(manifest) != {
        "name", "description", "version", "languages", "categories", "tags"
    }:
        raise SkillError(f"skill manifest has unknown or missing fields: {directory}")
    name = _manifest_string(manifest, "name", _NAME)
    description = _manifest_string(manifest, "description", None, max_length=512)
    version = _manifest_string(manifest, "version", _VERSION)
    languages = _terms_value(manifest, "languages")
    categories = _terms_value(manifest, "categories")
    tags = _terms_value(manifest, "tags")
    return (
        SkillRecord(
            name,
            description,
            version,
            languages,
            categories,
            tags,
            content,
            sha256(content_raw).hexdigest(),
            str(directory),
        ),
        len(manifest_raw) + len(content_raw),
    )


def _applicable(
    record: SkillRecord, language: str | None, category: str | None, tags: set[str]
) -> bool:
    return (
        (not record.languages or language in record.languages)
        and (not record.categories or category in record.categories)
        and (not record.tags or bool(tags.intersection(record.tags)))
    )


def _settings(settings: dict[str, object]) -> dict[str, object]:
    merged = dict(_DEFAULTS)
    merged.update(settings)
    expected = set(_DEFAULTS)
    if set(merged) != expected:
        raise SkillError("skill configuration has unknown fields")
    if not isinstance(merged["directories"], list) or not isinstance(merged["enabled"], list):
        raise SkillError("skill directories and enabled names must be arrays")
    if not isinstance(merged["auto_select"], bool):
        raise SkillError("skills.auto_select must be a boolean")
    for key in expected - {"directories", "enabled", "auto_select"}:
        _positive(merged, key)
    return merged


def _safe_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise SkillError(f"{label} must be an existing non-symlink directory: {path}")


def _read_bytes(path: Path, maximum: int, label: str) -> bytes:
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise SkillError(f"cannot read {label}: {path}") from error
    if not payload or len(payload) > maximum:
        raise SkillError(f"{label} is empty or exceeds configured byte limit: {path}")
    return payload


def _manifest_string(
    manifest: dict[str, Any], key: str, pattern: re.Pattern[str] | None, *, max_length: int = 64
) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value or len(value) > max_length or "\x00" in value:
        raise SkillError(f"skill manifest {key} must be a bounded non-empty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise SkillError(f"skill manifest {key} has an invalid format")
    return value


def _terms_value(manifest: dict[str, Any], key: str) -> tuple[str, ...]:
    value = manifest.get(key)
    if not isinstance(value, list):
        raise SkillError(f"skill manifest {key} must be an array")
    terms = _terms(value, f"skill manifest {key}")
    if len(terms) > 32 or len(set(terms)) != len(terms):
        raise SkillError(f"skill manifest {key} must contain at most 32 unique terms")
    return tuple(sorted(terms))


def _terms(values: list[object], label: str, *, pattern: re.Pattern[str] = _TERM) -> list[str]:
    if any(not isinstance(value, str) or pattern.fullmatch(value) is None for value in values):
        raise SkillError(f"{label} must contain lowercase stable terms")
    return [str(value) for value in values]


def _term_or_none(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    return _terms([value], label)[0]


def _positive(settings: dict[str, object], key: str) -> int:
    value = settings[key]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise SkillError(f"skills.{key} must be a positive integer")
    return value
