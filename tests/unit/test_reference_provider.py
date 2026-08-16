from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

from odysseus.candidates.prompts import build_request
from odysseus.candidates.reference import (
    HttpResponse,
    ReferenceProviderConfig,
    ReferenceProviderError,
    generate_reference_response,
)


class FakeTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.call: tuple[str, Mapping[str, str], bytes, float, int] | None = None

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        self.call = (url, headers, body, timeout_seconds, max_response_bytes)
        return self.response


def _candidate_json(*, model: str = "GPT 5.6 Sol", effort: str = "high") -> str:
    return json.dumps(
        {
            "protocol_version": 1,
            "provider": "openai",
            "model": model,
            "effort": effort,
            "hypotheses": [
                {
                    "candidate_id": "copy-less",
                    "mechanism": "avoid a copy",
                    "hotspot": "encode",
                    "expected_metric_effect": "lower latency",
                    "evidence_ids": ["e1"],
                    "candidate_files": ["src/lib.rs"],
                    "correctness_risk": "medium",
                    "falsification_test": "run gate",
                    "unified_diff": "diff --git a/src/lib.rs b/src/lib.rs\n"
                    "--- a/src/lib.rs\n+++ b/src/lib.rs\n@@ -1 +1 @@\n-old\n+new\n",
                }
            ],
        }
    )


class ReferenceProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = build_request(
            objective="latency",
            objective_direction="lower_is_better",
            hotspot_facts=["encode"],
            editable_paths=["src"],
            evidence=[{"source_id": "e1"}],
            untrusted_context=["quoted context"],
        )
        self.config = ReferenceProviderConfig(
            "https://provider.example/api",
            "GPT 5.6 Sol",
            "gpt-5.6-sol-2026-08-01",
            "high",
            "TEST_PROVIDER_KEY",
            5,
            10_000,
            10_000,
            2,
        )

    def test_public_label_is_preserved_while_api_model_is_requested_offline(self) -> None:
        transport = FakeTransport(
            HttpResponse(
                "https://provider.example/api/v1/responses",
                200,
                {},
                json.dumps({"output_text": _candidate_json()}).encode(),
            )
        )
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "synthetic-test-only-key"}, clear=False):
            response = generate_reference_response(self.request, self.config, transport)
        self.assertEqual(response.model, "GPT 5.6 Sol")
        assert transport.call is not None
        url, headers, body, timeout, cap = transport.call
        self.assertEqual(url, "https://provider.example/api/v1/responses")
        self.assertEqual(headers["Authorization"], "Bearer synthetic-test-only-key")
        self.assertEqual(timeout, 5)
        self.assertEqual(cap, 10_000)
        sent = json.loads(body)
        self.assertEqual(sent["model"], "gpt-5.6-sol-2026-08-01")
        self.assertTrue(sent["text"]["format"]["strict"])

    def test_rejects_redirect_and_changed_public_model_label(self) -> None:
        redirect = FakeTransport(
            HttpResponse("https://attacker.example/v1/responses", 200, {}, b'{"output_text":"{}"}')
        )
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "synthetic-test-only-key"}, clear=False):
            with self.assertRaisesRegex(ReferenceProviderError, "redirect"):
                generate_reference_response(self.request, self.config, redirect)
        wrong_model = FakeTransport(
            HttpResponse(
                "https://provider.example/api/v1/responses",
                200,
                {},
                json.dumps({"output_text": _candidate_json(model="GPT 5.6")}).encode(),
            )
        )
        with patch.dict(os.environ, {"TEST_PROVIDER_KEY": "synthetic-test-only-key"}, clear=False):
            with self.assertRaisesRegex(ReferenceProviderError, "public model label"):
                generate_reference_response(self.request, self.config, wrong_model)

    def test_accepts_public_label_without_slash_and_rejects_non_https(self) -> None:
        self.assertEqual(self.config.public_model, "GPT 5.6 Sol")
        with self.assertRaisesRegex(ReferenceProviderError, "HTTPS"):
            ReferenceProviderConfig(
                "http://provider.example", "GPT 5.6 Sol", "gpt-5.6", "high", "KEY", 1, 1, 1, 1
            )

    def test_reference_script_import_bootstraps_source_checkout(self) -> None:
        script = Path(__file__).parents[2] / "src" / "odysseus" / "candidates" / "reference.py"
        completed = subprocess.run(
            [sys.executable, str(script), "--help"],
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("--public-model", completed.stdout)
        self.assertIn("--api-model", completed.stdout)


if __name__ == "__main__":
    unittest.main()
