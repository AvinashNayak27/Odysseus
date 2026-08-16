import unittest
from pathlib import Path

from odysseus.adapters import detect_adapters
from odysseus.adapters.base import AdapterError, MetricDirection, MetricSpec
from odysseus.adapters.generic import GenericAdapter
from odysseus.adapters.go import GoAdapter
from odysseus.adapters.rust import RustAdapter
from odysseus.adapters.typescript import TypeScriptAdapter


class AdapterTests(unittest.TestCase):
    def test_detects_rust_and_captures_cargo_settings_without_mutation(self) -> None:
        with self.subTest("rust"):
            from tempfile import TemporaryDirectory

            with TemporaryDirectory() as temporary:
                root = Path(temporary)
                manifest = root / "Cargo.toml"
                manifest.write_text(
                    "[package]\nname='fixture'\n[profile.release]\n"
                    "lto = 'thin'\ncodegen-units = 1\n"
                )
                (root / "Cargo.lock").write_text("lock")
                adapter = RustAdapter()
                self.assertEqual(adapter.detect(root).adapter, "rust")  # type: ignore[union-attr]
                capture = adapter.capture_toolchain(
                    root,
                    tool_versions={"rustc": "1.80"},
                    profiler_capabilities={"perf": True},
                    target_architecture="x86_64",
                )
                self.assertIn(("lto", "'thin'"), capture.compiler_settings)
                self.assertTrue(manifest.read_text().startswith("[package]"))
                plan = adapter.profile_plan(
                    root, artifact_root=root / "artifacts", profiler_capabilities={"perf": False}
                )
                self.assertFalse(plan.available)
                self.assertIn("permission", plan.reason or "")

    def test_go_typescript_and_generic_safe_plans(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            go = root / "go"
            go.mkdir()
            (go / "go.mod").write_text("module fixture")
            self.assertIsNotNone(GoAdapter().detect(go))
            self.assertEqual(
                GoAdapter()
                .profile_plan(go, artifact_root=go / "out", profiler_capabilities={"go_test": True})
                .argv[:2],
                ("go", "test"),
            )

            ts = root / "ts"
            ts.mkdir()
            (ts / "package.json").write_text("{}")
            (ts / "tsconfig.json").write_text("{}")
            self.assertIsNotNone(TypeScriptAdapter().detect(ts))
            self.assertEqual(
                TypeScriptAdapter()
                .profile_plan(ts, artifact_root=ts / "out", profiler_capabilities={"tsc": True})
                .executable,
                "tsc",
            )

            empty = root / "empty"
            empty.mkdir()
            self.assertEqual(detect_adapters(empty)[0].adapter, "generic")
            self.assertFalse(
                GenericAdapter()
                .profile_plan(empty, artifact_root=empty / "out", profiler_capabilities={})
                .available
            )

    def test_metric_parsing_requires_exactly_one_finite_match(self) -> None:
        metric = MetricSpec(
            "latency",
            MetricDirection.LOWER_IS_BETTER,
            r"latency=(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms)",
        )
        result = RustAdapter().parse_metrics("latency=12.5ms", metric)
        self.assertEqual((result.value, result.unit), (12.5, "ms"))
        with self.assertRaises(AdapterError):
            RustAdapter().parse_metrics("latency=1ms latency=2ms", metric)


if __name__ == "__main__":
    unittest.main()
