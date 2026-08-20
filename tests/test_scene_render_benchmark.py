from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
BENCHMARKS = ROOT / "benchmarks"
SCRIPTS = ROOT / "scripts"
for entry in (BENCHMARKS, SCRIPTS):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import annotation_review  # noqa: E402
import project_workspace  # noqa: E402
import run_scene_render_benchmark as benchmark  # noqa: E402


TEST_ROOT = Path(tempfile.gettempdir()) / "srt-whiteboard-scene-render-benchmark-tests"


class SceneRenderBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = TEST_ROOT / uuid.uuid4().hex
        self.root.mkdir()

    def tearDown(self) -> None:
        if self.root.exists():
            self.root.relative_to(TEST_ROOT)
            shutil.rmtree(self.root)

    def test_fixed_fixture_sizes_canvas_annotation_and_hand_contract(self) -> None:
        small, small_path = benchmark.load_fixture("fixture-small")
        medium, medium_path = benchmark.load_fixture("fixture-medium")
        self.assertEqual(small["sceneCount"], 2)
        self.assertEqual(medium["sceneCount"], 8)
        for fixture, path in ((small, small_path), (medium, medium_path)):
            with self.subTest(fixture=fixture["fixtureId"]):
                self.assertTrue(path.is_file())
                self.assertEqual(
                    fixture["canvas"],
                    {"width": 1920, "height": 1080, "background": "#F5EBD7"},
                )
                self.assertEqual(fixture["annotation"]["protectedRegions"], [])
                self.assertEqual(fixture["handAsset"], "assets/drawing-hand.png")
                self.assertFalse(
                    any(
                        "provider" in key.casefold()
                        for value in (fixture, fixture["annotation"], fixture["renderArguments"])
                        for key in value
                    )
                )

    def test_fixture_builder_produces_current_synthetic_formal_gate(self) -> None:
        fixture, _path = benchmark.load_fixture("fixture-small")
        project_root = benchmark.build_fixture_project(self.root / "base", fixture)
        project = project_workspace.load_project(project_root)
        self.assertEqual(len(project.plan["scenes"]), 2)
        approval = annotation_review.require_current_annotation_review_approval(project)
        self.assertTrue(approval["approved"])
        for scene in project.plan["scenes"]:
            image = project.root / "scenes" / scene["outputFile"]
            annotation = project.root / "scenes" / f"{Path(scene['outputFile']).stem}.annotation.json"
            self.assertTrue(image.is_file())
            self.assertTrue(annotation.is_file())
            value = json.loads(annotation.read_text(encoding="utf-8"))
            self.assertEqual(value["canvas"], {"width": 1920, "height": 1080})
            self.assertEqual(value["elements"][0]["reveal"]["protectedRegions"], [])

    def test_stability_uses_serial_cold_without_millisecond_thresholds(self) -> None:
        serial_fingerprint = {
            "sceneOrder": ["scene-01", "scene-02"],
            "identitySetSha256": "a" * 64,
            "outputShaSetSha256": "b" * 64,
            "scenes": [],
        }
        runs = [
            {
                "configured": 1,
                "temperature": "cold",
                "status": "PASS",
                "wallMs": 1000.0,
                "outputFingerprint": serial_fingerprint,
            },
            {
                "configured": 4,
                "temperature": "warm",
                "status": "PASS",
                "wallMs": 1.0,
                "outputFingerprint": dict(serial_fingerprint),
            },
        ]
        result = benchmark._stability(runs)
        self.assertTrue(result["identityStableAcrossRuns"])
        self.assertTrue(result["outputShaStableAcrossRuns"])
        self.assertTrue(result["sceneOrderStableAcrossRuns"])
        self.assertNotIn("threshold", json.dumps(result).casefold())
        self.assertTrue(all(run["identityStableAgainstSerial"] for run in runs))

    def test_report_distinguishes_cold_warm_and_preserves_missing_metrics_as_null(self) -> None:
        fixture, _path = benchmark.load_fixture("fixture-small")

        def fake_build(destination: Path, _fixture: dict) -> Path:
            project = destination / "project"
            project.mkdir(parents=True)
            (project / ".work").mkdir()
            return project

        def fake_run(project_root: Path, _fixture: dict, *, concurrency: int, temperature: str):
            fingerprint = {
                "sceneOrder": ["scene-01", "scene-02"],
                "scenes": [],
                "identitySetSha256": "a" * 64,
                "outputShaSetSha256": "b" * 64,
            }
            return {
                "temperature": temperature,
                "configured": concurrency,
                "effective": min(concurrency, 2),
                "peak": min(concurrency, 2),
                "taskCount": 2,
                "wallMs": 12.5,
                "peakRssBytes": None,
                "ffmpegProcessCount": None,
                "candidateBytes": None,
                "status": "PASS",
                "outputFingerprint": fingerprint,
            }

        args = argparse.Namespace(
            fixture="fixture-small",
            concurrency=[1, 2],
            temperature=["cold", "warm"],
            output=None,
            workspace=self.root / "workspace",
            keep_workspace=True,
            no_failure_probe=True,
        )
        with mock.patch.object(benchmark, "build_fixture_project", side_effect=fake_build), mock.patch.object(
            benchmark, "run_measured", side_effect=fake_run
        ), mock.patch.object(benchmark, "environment_report", return_value={"fixture": True}):
            report = benchmark.run_benchmark(args)
        self.assertEqual(
            [(run["configured"], run["temperature"]) for run in report["runs"]],
            [(1, "cold"), (1, "warm"), (2, "cold"), (2, "warm")],
        )
        self.assertTrue(report["stability"]["identityStableAcrossRuns"])
        self.assertIsNone(report["runs"][0]["peakRssBytes"])
        self.assertTrue(report["resourceWarnings"])
        self.assertEqual(report["fixture"]["fixtureId"], fixture["fixtureId"])

    def test_cli_contract_supports_raw_json_and_failure_probe_opt_out(self) -> None:
        help_text = benchmark.build_parser().format_help()
        for token in ("--fixture", "--concurrency", "--temperature", "--output", "--no-failure-probe"):
            self.assertIn(token, help_text)
        self.assertEqual(benchmark._validate_concurrency([1, 2, 4, 5]), [1, 2, 4, 5])
        with self.assertRaises(benchmark.BenchmarkError):
            benchmark._validate_concurrency([0])

    def test_warm_cannot_be_mislabeled_without_preceding_cold(self) -> None:
        args = argparse.Namespace(
            fixture="fixture-small",
            concurrency=[1],
            temperature=["warm"],
            output=None,
            workspace=self.root / "workspace",
            keep_workspace=False,
            no_failure_probe=True,
        )
        with self.assertRaisesRegex(benchmark.BenchmarkError, "cold"):
            benchmark.run_benchmark(args)


if __name__ == "__main__":
    unittest.main()
