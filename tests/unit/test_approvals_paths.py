from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from odysseus.approvals import approval_hash, verify_approval
from odysseus.models import ApprovalPlan, CommandSpec
from odysseus.paths import PathPolicyError, assert_editable_path, require_runtime_root


class ApprovalAndPathTests(unittest.TestCase):
    def test_approval_is_bound_to_all_action_fields(self) -> None:
        command = CommandSpec("echo", ("echo", "approved"), "/tmp", 1, ("echo",))
        plan = ApprovalPlan("setup", "setup plan", "benchmark", "/src", "/dst", command)
        digest = approval_hash(plan)
        verify_approval(plan, digest)
        stale = ApprovalPlan("setup", "modified plan", "benchmark", "/src", "/dst", command)
        with self.assertRaises(PermissionError):
            verify_approval(stale, digest)

    def test_runtime_root_is_not_a_checkout_or_dot_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary) / "checkout"
            checkout.mkdir()
            with self.assertRaises(PathPolicyError):
                require_runtime_root(checkout / ".odysseus", (checkout,))
            with self.assertRaises(PathPolicyError):
                require_runtime_root(Path(temporary) / ".odysseus")

    def test_editable_path_blocks_traversal_metadata_and_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "work"
            editable = root / "src"
            editable.mkdir(parents=True)
            (root / ".git").mkdir()
            outside = Path(temporary) / "outside"
            outside.mkdir()
            (editable / "escape").symlink_to(outside, target_is_directory=True)
            self.assertEqual(
                assert_editable_path(root, (editable,), "src/file.py"), editable / "file.py"
            )
            for path in ("../outside/x", "/etc/passwd", ".git/config", "src/escape/x"):
                with self.assertRaises(PathPolicyError, msg=path):
                    assert_editable_path(root, (editable,), path)
