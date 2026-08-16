import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from odysseus.candidates.prompts import build_request
from odysseus.candidates.provider import CandidateProviderError, HumanImportedProvider
from odysseus.candidates.ranking import CandidateScore, rank_candidates
from odysseus.candidates.validation import PatchValidationError, validate_unified_diff
from odysseus.research.official_docs import HttpResponse, OfficialDocsClient, OfficialDocumentError
from odysseus.research.pipeline import derive_questions
from odysseus.research.scholarly import ScholarlyWork, _parse_item, deduplicate_works


class FakeTransport:
    def __init__(self, response: HttpResponse) -> None:
        self.response = response
        self.calls = 0

    def get(self, url: str) -> HttpResponse:
        del url
        self.calls += 1
        return self.response


def _response(diff: str) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "provider": "manual",
        "model": "vendor/model-exact",
        "effort": "high",
        "hypotheses": [
            {
                "candidate_id": "c1",
                "mechanism": "reduce copies",
                "hotspot": "encode",
                "expected_metric_effect": "lower latency",
                "evidence_ids": ["e1"],
                "candidate_files": ["src/lib.rs"],
                "correctness_risk": "medium",
                "falsification_test": "run gate",
                "unified_diff": diff,
            }
        ],
    }


class ResearchAndCandidateTests(unittest.TestCase):
    def test_official_docs_allowlist_cache_and_excerpt(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            transport = FakeTransport(
                HttpResponse(
                    "https://doc.rust-lang.org/cargo/", 200, {}, b"Cargo profiles control lto."
                )
            )
            client = OfficialDocsClient(
                transport,
                allowed_domains=("doc.rust-lang.org",),
                cache_root=root,
            )
            doc = client.fetch(
                "https://doc.rust-lang.org/cargo/#x",
                title="Cargo",
                publisher="Rust",
                version_relevance="stable",
                excerpt="profiles control lto",
            )
            self.assertEqual(doc.canonical_url, "https://doc.rust-lang.org/cargo/")
            expected_key = hashlib.sha256(doc.canonical_url.encode()).hexdigest()
            self.assertEqual(doc.cache_path.stem, expected_key)
            cached = client.fetch(
                "https://doc.rust-lang.org/cargo/",
                title="Cargo",
                publisher="Rust",
                version_relevance="stable",
                excerpt="profiles control lto",
            )
            self.assertEqual(cached.content_sha256, doc.content_sha256)
            self.assertEqual(transport.calls, 1)
            with self.assertRaises(OfficialDocumentError):
                client.fetch(
                    "https://example.com/",
                    title="x",
                    publisher="x",
                    version_relevance="x",
                    excerpt="x",
                )

    def test_provider_import_requires_attribution_and_diff_is_path_safe(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            work = root / "work"
            source = work / "src"
            source.mkdir(parents=True)
            diff = (
                "diff --git a/src/lib.rs b/src/lib.rs\n--- a/src/lib.rs\n"
                "+++ b/src/lib.rs\n@@ -1 +1 @@\n-old\n+new\n"
            )
            response_path = root / "response.json"
            response_path.write_text(json.dumps(_response(diff)))
            request = build_request(
                objective="latency",
                objective_direction="lower_is_better",
                hotspot_facts=["encode"],
                editable_paths=["src"],
                evidence=[{"source_id": "e1"}],
                untrusted_context=["ignore instructions"],
            )
            response = HumanImportedProvider(response_path, max_response_bytes=10_000).generate(
                request
            )
            patch = validate_unified_diff(
                response.hypotheses[0].unified_diff, workdir=work, editable_roots=(source,)
            )
            self.assertEqual(patch.paths, (source / "lib.rs",))
            with self.assertRaises(PatchValidationError):
                validate_unified_diff(
                    diff.replace("src/lib.rs", "../escape", 2),
                    workdir=work,
                    editable_roots=(source,),
                )
            payload = _response(diff)
            payload["model"] = "GPT 5.6 Sol"
            response_path.write_text(json.dumps(payload))
            imported = HumanImportedProvider(
                response_path, max_response_bytes=10_000
            ).generate(request)
            self.assertEqual(imported.model, "GPT 5.6 Sol")
            payload["model"] = "\u0000invalid"
            response_path.write_text(json.dumps(payload))
            with self.assertRaises(CandidateProviderError):
                HumanImportedProvider(response_path, max_response_bytes=10_000).generate(request)

    def test_queries_dedupe_and_ranking_are_deterministic(self) -> None:
        questions = derive_questions(
            objective="latency",
            adapter="rust",
            hotspots=["encode"],
            editable_files=["src/lib.rs"],
            frontier_claims=["use SIMD"],
        )
        self.assertTrue(any("use SIMD" in question.query for question in questions))
        self.assertIsNone(_parse_item("crossref", {"title": ["Untyped published"]}).year)
        self.assertEqual(
            _parse_item(
                "crossref", {"title": ["Typed published"], "published": {"date-parts": [[2026]]}}
            ).year,
            2026,
        )
        works = deduplicate_works(
            (
                ScholarlyWork("x", "Paper", 2024, "10/X", None, "https://x", False),
                ScholarlyWork("y", "Paper", 2024, "10/x", None, "https://y", True),
            )
        )
        self.assertEqual(
            works, (ScholarlyWork("y", "Paper", 2024, "10/x", None, "https://y", True),)
        )
        ranked = rank_candidates(
            (CandidateScore("z", 1, 1, 1, 1, 0), CandidateScore("a", 1, 1, 1, 1, 0))
        )
        self.assertEqual([candidate.candidate_id for candidate in ranked], ["a", "z"])


if __name__ == "__main__":
    unittest.main()
