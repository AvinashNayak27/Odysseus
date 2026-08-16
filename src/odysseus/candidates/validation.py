"""Strict unified-diff validation for untrusted provider output."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from odysseus.paths import PathPolicyError, assert_editable_path


class PatchValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ValidatedPatch:
    diff: str
    paths: tuple[Path, ...]
    additions: int
    deletions: int


_DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
_FILE_HEADER = re.compile(r"^(---|\+\+\+) ([ab]/.+|/dev/null)$")
_FORBIDDEN_DIFF = (
    "GIT binary patch",
    "new file mode",
    "deleted file mode",
    "old mode",
    "new mode",
    "Subproject commit",
)


def validate_unified_diff(
    diff: str, *, workdir: Path, editable_roots: tuple[Path, ...], max_bytes: int = 200_000
) -> ValidatedPatch:
    """Validate diffs as data. This function never applies the patch."""
    if not diff or len(diff.encode("utf-8")) > max_bytes:
        raise PatchValidationError("patch is empty or exceeds the configured byte limit")
    if "\x00" in diff or any(marker in diff for marker in _FORBIDDEN_DIFF):
        raise PatchValidationError("binary, mode, or submodule patch forms are forbidden")
    paths: list[Path] = []
    additions = deletions = 0
    expected_headers: list[str] = []
    saw_header = False
    for line in diff.splitlines():
        header = _DIFF_HEADER.match(line)
        if header:
            left, right = header.groups()
            if left != right:
                raise PatchValidationError("rename and copy diffs are forbidden")
            expected_headers.append(left)
            saw_header = True
            continue
        file_header = _FILE_HEADER.match(line)
        if file_header:
            marker, raw_path = file_header.groups()
            if raw_path == "/dev/null":
                raise PatchValidationError(
                    "file creation and deletion are forbidden for MVP candidates"
                )
            path = raw_path[2:]
            if not expected_headers or path != expected_headers[0]:
                raise PatchValidationError("diff file headers do not match diff --git header")
            if marker == "+++":
                expected_headers.pop(0)
                try:
                    resolved = assert_editable_path(workdir, editable_roots, path)
                except PathPolicyError as error:
                    raise PatchValidationError(str(error)) from error
                paths.append(resolved)
            continue
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    if not saw_header or expected_headers or not paths:
        raise PatchValidationError("patch must contain complete unified diff file headers")
    if len(set(paths)) != len(paths):
        raise PatchValidationError("a candidate may modify each file only once")
    return ValidatedPatch(diff, tuple(paths), additions, deletions)
