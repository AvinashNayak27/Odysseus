"""Canonical path and repository-boundary checks."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

_BLOCKED_PARTS = frozenset({".git", ".yukon", ".yukon-link", "yukon.local-link"})


class PathPolicyError(ValueError):
    pass


def canonical(path: Path) -> Path:
    if not path.is_absolute():
        raise PathPolicyError("path must be absolute")
    return path.resolve(strict=False)


def require_runtime_root(state_root: Path, checkout_roots: tuple[Path, ...] = ()) -> Path:
    root = canonical(state_root)
    for checkout in checkout_roots:
        checked = canonical(checkout)
        if root == checked or checked in root.parents:
            raise PathPolicyError("runtime state must be outside benchmark checkouts")
    if root.name == ".odysseus":
        raise PathPolicyError("implicit .odysseus runtime roots are forbidden")
    return root


def _relative_candidate(relative_path: str) -> PurePosixPath:
    candidate = PurePosixPath(relative_path)
    if not relative_path or candidate.is_absolute() or ".." in candidate.parts:
        raise PathPolicyError("path must be non-empty, relative, and traversal-free")
    if any(part in _BLOCKED_PARTS for part in candidate.parts):
        raise PathPolicyError("path targets protected Git or Yukon metadata")
    return candidate


def assert_editable_path(
    workdir: Path, editable_roots: tuple[Path, ...], relative_path: str
) -> Path:
    """Resolve a proposed relative edit and reject escapes/boundary violations."""
    relative = _relative_candidate(relative_path)
    root = canonical(workdir)
    target = canonical(root / Path(*relative.parts))
    if target != root and root not in target.parents:
        raise PathPolicyError("resolved path escapes workdir")
    resolved_editable = tuple(canonical(item) for item in editable_roots)
    if any(item != root and root not in item.parents for item in resolved_editable):
        raise PathPolicyError("editable path resolves outside workdir")
    if len({str(item).casefold() for item in resolved_editable}) != len(resolved_editable):
        raise PathPolicyError("editablePaths contain a case-normalization ambiguity")
    if not any(target == item or item in target.parents for item in resolved_editable):
        raise PathPolicyError("path is outside editablePaths")
    case_matches = [
        item for item in resolved_editable if str(item).casefold() == str(target).casefold()
    ]
    if case_matches and target not in case_matches:
        raise PathPolicyError("case-normalization ambiguity")
    for parent in (target, *target.parents):
        if parent == root.parent:
            break
        if (parent / ".git").is_file() and parent != root:
            raise PathPolicyError("nested Git/submodule boundary")
    return target


def require_within(path: Path, root: Path) -> Path:
    checked, base = canonical(path), canonical(root)
    if checked != base and base not in checked.parents:
        raise PathPolicyError(f"{checked} is outside {base}")
    return checked


def is_token_present() -> bool:
    """Only permitted token interaction: presence, never value access."""
    return "YUKON_API_TOKEN" in os.environ
