from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from odysseus.commands import CommandPolicyError, CommandRunner
from odysseus.models import CommandSpec
from odysseus.redaction import PublicOutputError, redact_private, scrub_public


class CommandAndRedactionSecurityTests(unittest.TestCase):
    def test_shell_metacharacters_are_data_not_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "owned"
            spec = CommandSpec(
                sys.executable,
                (sys.executable, "-c", "import sys; print(sys.argv[1])", f"; touch {marker}"),
                temporary,
                2,
                (sys.executable,),
            )
            result = CommandRunner().run(spec)
            self.assertEqual(result.exit_code, 0)
            self.assertFalse(marker.exists())
            self.assertIn("touch", result.stdout)

    def test_rejects_unallowlisted_executable_and_limits_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            disallowed = CommandSpec("not-allowed", ("not-allowed",), temporary, 1, ("echo",))
            with self.assertRaises(CommandPolicyError):
                CommandRunner().run(disallowed)
            output = CommandSpec(
                sys.executable,
                (sys.executable, "-c", "print('x' * 200)"),
                temporary,
                2,
                (sys.executable,),
                max_output_bytes=20,
            )
            result = CommandRunner().run(output)
            self.assertTrue(result.output_truncated)
            self.assertLessEqual(len(result.stdout), 50)

    def test_timeout_terminates_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            spec = CommandSpec(
                sys.executable,
                (sys.executable, "-c", "import time; time.sleep(5)"),
                temporary,
                0.1,
                (sys.executable,),
            )
            result = CommandRunner().run(spec)
            self.assertTrue(result.timed_out)

    def test_inherited_token_printed_by_child_is_scrubbed_without_application_read(self) -> None:
        token = "synthetic-yukon-token-should-not-leak"
        with tempfile.TemporaryDirectory() as temporary:
            previous = os.environ.get("YUKON_API_TOKEN")
            os.environ["YUKON_API_TOKEN"] = token
            try:
                spec = CommandSpec(
                    sys.executable,
                    (sys.executable, "-c", "import os; print(os.environ['YUKON_API_TOKEN'])"),
                    temporary,
                    2,
                    (sys.executable,),
                    inherit_environment=True,
                    environment_allowlist=("YUKON_API_TOKEN", "PATH"),
                    extra_redaction_values=(token,),
                )
                result = CommandRunner().run(spec)
            finally:
                if previous is None:
                    del os.environ["YUKON_API_TOKEN"]
                else:
                    os.environ["YUKON_API_TOKEN"] = previous
            self.assertNotIn(token, result.stdout)
            self.assertNotIn(token, result.stderr)

    def test_redaction_preserves_operational_identifiers_and_diffs(self) -> None:
        sha256 = "a3" * 32
        uuid = "123e4567-e89b-12d3-a456-426614174000"
        run_id = "run-20260816T070000000000Z-abcdef123456"
        diff = "diff --git a/src/main.py b/src/main.py\n--- a/src/main.py\n+++ b/src/main.py\n"
        text = f"{sha256} {uuid} {run_id} benchmark-identifier\n{diff}"
        self.assertEqual(redact_private(text), text)
        self.assertEqual(scrub_public(text), text)

    def test_key_value_redaction_excludes_code_expressions(self) -> None:
        code = "let token = self.next_token()\nsecret = compute(x)\n"
        self.assertEqual(redact_private(code), code)
        credential = "api_key = 'sk_abcdefghijklmnopqrstuvwxyz123456'"
        self.assertNotIn("sk_abcdefghijklmnopqrstuvwxyz123456", redact_private(credential))

    def test_private_and_public_scrub(self) -> None:
        token = "synthetic-token-value-1234567890"
        self.assertNotIn(token, redact_private(f"token={token}", (token,)))
        result = scrub_public(
            "mail alice@example.com at /home/alice/work <script>alert(1)</script>"
        )
        self.assertNotIn("alice@example.com", result)
        self.assertNotIn("/home/alice", result)
        with self.assertRaises(PublicOutputError):
            scrub_public("Traceback (most recent call last): private")

    def test_help_has_no_forbidden_command_surface_or_token_option(self) -> None:
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")}
        completed = subprocess.run(
            [sys.executable, "-m", "odysseus", "--help"],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        help_text = completed.stdout.lower()
        for forbidden in (
            "submit",
            "notes add",
            "--force",
            "reset",
            "arbitrary shell",
            "api-token",
            "yukon-api-token",
        ):
            self.assertNotIn(forbidden, help_text)
