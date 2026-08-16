"""Offline-only command and source-surface policy checks."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

from odysseus.config import load_schema

ROOT = Path(__file__).parents[2]


def cli(*args: str, state_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, "-m", "odysseus", *args),
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT / "src")},
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_requires_configuration_and_has_no_installer_surface(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    missing_config = cli("doctor", state_root=state_root)
    assert missing_config.returncode == 2
    assert "--config" in missing_config.stderr

    help_result = cli("--help", state_root=state_root)
    assert help_result.returncode == 0
    assert "inspect-installer" not in help_result.stdout
    assert "\n    install " not in help_result.stdout
    sync_help = cli("yukon", "--help", state_root=state_root)
    assert "sync-harness" in sync_help.stdout

    missing = cli(
        "--config",
        str(tmp_path / "missing.json"),
        "verify-run",
        "run-not-created",
        state_root=state_root,
    )
    assert missing.returncode == 2
    assert "cannot load configuration" in missing.stderr


def test_all_checked_in_schemas_are_valid_draft_2020_12() -> None:
    schemas = sorted((ROOT / "schemas").glob("*.schema.json"))
    assert schemas
    assert {item.name.removesuffix(".schema.json") for item in schemas} == {
        "benchmark-lock",
        "candidate",
        "config",
        "event",
        "evidence",
        "experiment",
        "report-manifest",
        "workspace",
    }
    for path in schemas:
        Draft202012Validator.check_schema(load_schema(path.name.removesuffix(".schema.json")))


def test_source_has_no_public_or_destructive_yukon_or_git_invocations() -> None:
    forbidden_yukon = {
        "submit",
        "publish",
        "note",
        "reset",
        "force",
        "clean",
        "remove",
        "delete",
    }
    forbidden_git = {
        "reset",
        "clean",
        "restore",
        "checkout",
        "switch",
        "rebase",
        "merge",
        "push",
        "rm",
        "worktree remove",
    }
    violations: list[str] = []
    for path in (ROOT / "src" / "odysseus").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Tuple) or len(node.elts) < 2:
                continue
            values = [
                element.value
                for element in node.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            ]
            if not values:
                continue
            if values[0] == "yukon" and any(value in forbidden_yukon for value in values[1:]):
                violations.append(f"{path}:{node.lineno}: {values}")
            if values[0] == "git":
                command = " ".join(values[1:3])
                if command in forbidden_git or (len(values) > 1 and values[1] in forbidden_git):
                    violations.append(f"{path}:{node.lineno}: {values}")
    assert not violations, "forbidden command tuples:\n" + "\n".join(violations)


def test_no_live_yukon_token_is_consumed_by_test_environment(tmp_path: Path) -> None:
    # CLI validation does not need a token; discovery itself is an authenticated Yukon prerequisite.
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    completed = cli("--config", str(config), "doctor", state_root=Path("/tmp/unused"))
    assert completed.returncode == 2
    assert "YUKON_API_TOKEN" not in completed.stdout
