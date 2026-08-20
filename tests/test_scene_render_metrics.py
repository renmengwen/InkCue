from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import project_workspace  # noqa: E402
import render_stream_whiteboard  # noqa: E402
import render_timing  # noqa: E402
import stream_render as sr  # noqa: E402
import test_render_timing as render_fixture  # noqa: E402


class SceneRenderMetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = render_fixture.RenderTimingTests("runTest")
        self.fixture.setUp()
        self.project = self.fixture._project()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _batch_args(self):
        return render_stream_whiteboard._parse_args(
            ["--project", str(self.project.root), "--all"]
        )

    @staticmethod
    def _worker_result(scene_id: str, candidate_bytes: int) -> dict:
        return {
            "sceneId": scene_id,
            "candidatePath": f"candidate-{scene_id}",
            "status": "succeeded",
            "deepReceipt": {"sceneId": scene_id},
            "phaseDurationsMs": {
                "prepare": 1.0,
                "render": 2.0,
                "validation": 3.0,
            },
            "ffmpegProcessCount": 1,
            "candidateBytes": candidate_bytes,
        }

    def _batch_patches(self, configured: int):
        cfg = mock.Mock()
        cfg.for_stage.return_value = configured
        return (
            mock.patch.object(
                render_stream_whiteboard.annotation_review,
                "require_current_annotation_review_approval",
                return_value={"approved": True},
            ),
            mock.patch.object(project_workspace, "load_workspace_config", return_value=cfg),
            mock.patch.object(
                render_stream_whiteboard,
                "_load_formal_hand",
                return_value=(None, None, None),
            ),
        )

    def test_parallel_batch_summary_exposes_comparable_runtime_metrics(self) -> None:
        contexts = render_timing.resolve_formal_scenes(
            self.project,
            [item["sceneId"] for item in self.project.plan["scenes"]],
        )
        worker_results = {
            "scene-01": self._worker_result("scene-01", 101),
            "scene-02": self._worker_result("scene-02", 202),
        }

        def execute(_tasks, *, max_workers, **_kwargs):
            self.assertEqual(max_workers, 2)
            return worker_results, 2

        def publish(_candidate, destination, **_kwargs):
            return {"marker": f"current-{destination.name}"}

        def update_manifest(context, **_kwargs):
            return {
                "scenes": {
                    context.scene_id: {
                        "renderIdentityHash": f"identity-{context.scene_id}"
                    }
                }
            }

        approval_patch, config_patch, hand_patch = self._batch_patches(configured=4)
        with approval_patch, config_patch, hand_patch, mock.patch.object(
            render_stream_whiteboard,
            "_execute_formal_candidate_tasks",
            side_effect=execute,
        ), mock.patch.object(
            render_stream_whiteboard,
            "_publish_and_bind_scene",
            side_effect=publish,
        ), mock.patch.object(
            render_timing,
            "update_render_manifest",
            side_effect=update_manifest,
        ):
            result = render_stream_whiteboard._run_formal_batch(self._batch_args())

        self.assertEqual(result["configured"], 4)
        self.assertEqual(result["effective"], 2)
        self.assertEqual(result["peak"], 2)
        self.assertEqual(result["taskCount"], len(contexts))
        self.assertGreaterEqual(result["wallMs"], 0.0)
        self.assertEqual(
            set(result["stageDurationsMs"]),
            {"context", "prepare", "candidateExecution", "coordinatorPublish"},
        )
        self.assertTrue(
            all(value >= 0.0 for value in result["stageDurationsMs"].values())
        )
        self.assertEqual(result["ffmpegProcessCount"], 2)
        self.assertEqual(
            result["ffmpegProcessCountMetric"], "sceneEncoderStarts"
        )
        self.assertEqual(result["candidateBytes"], 303)
        self.assertEqual(
            result["candidateBytesByScene"], {"scene-01": 101, "scene-02": 202}
        )
        self.assertEqual(
            result["workerPhaseDurationsMs"],
            {"prepare": 2.0, "render": 4.0, "validation": 6.0},
        )
        self.assertEqual(result["workerPhaseDurationAggregation"], "sumAcrossScenes")

    def test_serial_batch_returns_the_same_metric_shape(self) -> None:
        candidate_bytes = {"scene-01": 101, "scene-02": 202}

        def render_context(_args, context, _frozen, _cfg, *, runtime_metrics, **_kwargs):
            runtime_metrics.update(
                {
                    "sceneId": context.scene_id,
                    "phaseDurationsMs": {
                        "prepare": 1.0,
                        "render": 2.0,
                        "validation": 3.0,
                    },
                    "coordinatorPublishMs": 0.5,
                    "ffmpegProcessCount": 1,
                    "candidateBytes": candidate_bytes[context.scene_id],
                }
            )
            return context.output_path, f"identity-{context.scene_id}"

        approval_patch, config_patch, hand_patch = self._batch_patches(configured=1)
        with approval_patch, config_patch, hand_patch, mock.patch.object(
            render_stream_whiteboard,
            "_render_formal_context",
            side_effect=render_context,
        ):
            result = render_stream_whiteboard._run_formal_batch(self._batch_args())

        self.assertEqual(result["configured"], 1)
        self.assertEqual(result["effective"], 1)
        self.assertEqual(result["peak"], 1)
        self.assertEqual(result["candidateBytes"], 303)
        self.assertEqual(result["ffmpegProcessCount"], 2)
        self.assertEqual(
            set(result["stageDurationsMs"]),
            {"context", "prepare", "candidateExecution", "coordinatorPublish"},
        )
        self.assertEqual(
            result["workerPhaseDurationsMs"],
            {"prepare": 2.0, "render": 4.0, "validation": 6.0},
        )

    def test_worker_records_encoder_start_validation_time_and_candidate_bytes(self) -> None:
        candidate = self.project.root / ".work" / "metrics" / "candidate.mp4"
        task = {
            "sceneId": "scene-01",
            "candidatePath": str(candidate),
            "imagePath": str(self.project.root / "scenes" / "scene-01.png"),
            "annotationPath": str(
                self.project.root / "scenes" / "scene-01.annotation.json"
            ),
            "handPath": None,
            "handSha256": None,
            "bareTip": True,
            "imageSha256": "image-sha",
            "annotationSha256": "annotation-sha",
            "renderProfile": {"width": 4, "height": 4, "fps": 60},
            "timingScene": {
                "sceneDurationMs": 100,
                "frameCount": 6,
                "startMs": 0,
                "startFrame": 0,
            },
            "annotation": {"canvas": {"width": 4, "height": 4}, "elements": []},
            "config": vars(sr.Config()).copy(),
        }

        renderer = mock.Mock()

        def render_to(output, _duration, *, sink_factory, **_kwargs):
            sink_factory(
                output,
                width=4,
                height=4,
                fps=60,
                expected_frame_count=6,
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"candidate-bytes")

        renderer.render_to.side_effect = render_to
        deep_receipt = {"candidateSha256": "candidate-sha", "candidateBytes": 15}

        def sha_for(path):
            return (
                "annotation-sha"
                if Path(path).name.endswith("annotation.json")
                else "image-sha"
            )

        with mock.patch.object(
            project_workspace,
            "sha256_file",
            side_effect=sha_for,
        ), mock.patch.object(
            sr,
            "_imread_any",
            return_value=np.zeros((4, 4, 3), dtype=np.uint8),
        ), mock.patch.object(
            render_stream_whiteboard,
            "RegionStreamRenderer",
            return_value=renderer,
        ), mock.patch.object(
            render_stream_whiteboard.ffmpeg_frame_sink,
            "FFmpegFrameSink",
            return_value=mock.Mock(),
        ), mock.patch.object(
            render_stream_whiteboard.media_validation,
            "validate_video",
            return_value={"validation": {"deepReceipt": deep_receipt}},
        ):
            result = render_stream_whiteboard._render_formal_candidate_worker(task)

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["candidateBytes"], len(b"candidate-bytes"))
        self.assertEqual(result["ffmpegProcessCount"], 1)
        self.assertEqual(
            set(result["phaseDurationsMs"]), {"prepare", "render", "validation"}
        )
        self.assertTrue(
            all(value >= 0.0 for value in result["phaseDurationsMs"].values())
        )


if __name__ == "__main__":
    unittest.main()
