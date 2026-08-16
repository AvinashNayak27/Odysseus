from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from typing import cast

from odysseus.config import ConfigError, load_config
from odysseus.models import StageStatus
from odysseus.state import StateStore


class ConfigAndStateTests(unittest.TestCase):
    def _config(self, root: Path) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "workspace": {
                "state_root": str(root),
                "clone_root": "/var/tmp/clones",
                "artifact_retention_days": 1,
                "max_output_bytes": 1024,
            },
            "yukon": {
                "binary": "yukon",
                "api_url_env": "YUKON_API_URL",
                "read_retries": 0,
                "setup_timeout_seconds": 1,
                "run_timeout_seconds": 1,
                "notes_lookup_limit": 1,
                "installer_url": "https://example.invalid/install",
            },
            "selection": {"active_window_days": 1, "supported_objectives": ["minimize"]},
            "research": {"allowed_domains": ["go.dev"], "max_sources_per_query": 1},
            "experiments": {"pilot_samples": 3, "min_repetitions": 5, "max_repetitions": 5},
            "candidate_provider": {
                "mode": "human-import",
                "timeout_seconds": 1,
                "max_request_bytes": 1024,
                "max_candidates": 1,
                "max_response_bytes": 1024,
            },
            "reporting": {
                "private_path_aliases": {"/tmp": "$TMP"},
                "note_min_bytes": 5120,
                "note_max_bytes": 5120,
            },
        }

    def test_config_rejects_unknown_and_token_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            data = self._config(Path(temporary) / "state")
            data["token"] = "synthetic"
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)
            data = self._config(Path(temporary) / "state")
            data.pop("schemaVersion")
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)
            data = self._config(Path(temporary) / "clones" / ".odysseus")
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_state_preserves_code_identifiers_but_redacts_credential_literals(self) -> None:
        source_diff = (
            "diff --git a/src/parser.py b/src/parser.py\n"
            "--- a/src/parser.py\n"
            "+++ b/src/parser.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-let token = self.next_token()\n"
            "+secret = compute(x)\n"
        )
        credential = "api_key = 'sk_abcdefghijklmnopqrstuvwxyz123456'\n"
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state")
            run_id = store.create_run()
            store.write_artifact(run_id, "candidate.diff", source_diff.encode("utf-8"))
            store.write_artifact(run_id, "child-output.txt", credential.encode("utf-8"))
            artifacts = store.run_path(run_id) / "artifacts"
            self.assertEqual(
                (artifacts / "candidate.diff").read_bytes(), source_diff.encode("utf-8")
            )
            self.assertNotIn(
                "sk_abcdefghijklmnopqrstuvwxyz123456",
                (artifacts / "child-output.txt").read_text(encoding="utf-8"),
            )

    def test_state_is_append_only_and_artifacts_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state")
            run_id = store.create_run()
            store.append_event(run_id, "doctor", StageStatus.SUCCEEDED, {"detail": "ok"})
            artifact = store.write_artifact(run_id, "result.txt", b"immutable")
            self.assertEqual(artifact["bytes"], 9)
            scrubbed = store.write_artifact(run_id, "output.txt", b"token=synthetic-token-value")
            self.assertNotIn(
                "synthetic-token-value",
                (store.run_path(run_id) / "artifacts" / "output.txt").read_text(encoding="utf-8"),
            )
            self.assertLess(cast(int, scrubbed["bytes"]), 30)
            with self.assertRaisesRegex(Exception, "already exists"):
                store.write_artifact(run_id, "result.txt", b"replacement")
            events = (
                (store.run_path(run_id) / "events.jsonl").read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(json.loads(events[0])["schemaVersion"], 1)

    def test_concurrent_event_writes_remain_complete_json_lines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state")
            run_id = store.create_run()
            threads = [
                threading.Thread(
                    target=store.append_event,
                    args=(run_id, "test", StageStatus.SUCCEEDED, {"n": number}),
                )
                for number in range(20)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            lines = (
                (store.run_path(run_id) / "events.jsonl").read_text(encoding="utf-8").splitlines()
            )
            self.assertEqual(len(lines), 20)
            self.assertEqual({json.loads(line)["payload"]["n"] for line in lines}, set(range(20)))
