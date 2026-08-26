from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ffmpeg_frame_sink  # noqa: E402
import media_validation  # noqa: E402
import project_workspace  # noqa: E402
import render_stream_whiteboard  # noqa: E402
import render_timing  # noqa: E402
import srt_timeline  # noqa: E402
import stream_render  # noqa: E402


TEST_ROOT = Path(tempfile.gettempdir()) / "srt-whiteboard-phase4-render-timing"


class RenderTimingTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.root = (TEST_ROOT / f"d1-{uuid.uuid4().hex}").resolve()
        self.root.mkdir()

    def tearDown(self) -> None:
        if self.root.exists():
            self.root.relative_to(TEST_ROOT.resolve())
            shutil.rmtree(self.root)

    def test_paper_texture_is_normalized_to_unrevealed_canvas_without_rectangular_seams(self) -> None:
        cfg = stream_render.Config(grid_edge=10, cap_long_edge=100)
        canvas = stream_render._hex_to_bgr(cfg.canvas_hex)
        image = np.empty((100, 100, 3), dtype=np.uint8)
        # 模拟独立生图常见的暖纸纹：相对角落中位色产生小幅、确定性的起伏。
        base = np.array([215, 235, 245], dtype=np.int16)
        yy, xx = np.indices((100, 100))
        texture = ((xx * 3 + yy * 5) % 25 - 12).astype(np.int16)
        image[:] = np.clip(base + texture[:, :, None], 0, 255).astype(np.uint8)
        # 模拟触碰边缘的暖色纸纹框；它比普通纸面更深，但没有真实深色线稿。
        image[:14, :] = np.array([170, 205, 235], dtype=np.uint8)
        # 高对比主体不能被背景规范化吞掉。
        image[40:60, 40:60] = np.array([70, 90, 120], dtype=np.uint8)
        annotation = {
            "canvas": {"width": 100, "height": 100},
            "elements": [
                {
                    "region": {"x": 0, "y": 0, "width": 100, "height": 100},
                    "reveal": {"protectedRegions": []},
                }
            ],
        }

        renderer = render_stream_whiteboard.RegionStreamRenderer(
            image, annotation, cfg, None, True, output_size=(100, 100)
        )

        background = np.ones((100, 100), dtype=bool)
        background[37:63, 37:63] = False
        expected = np.broadcast_to(canvas, renderer.color_img.shape)
        self.assertTrue(np.array_equal(renderer.color_img[background], expected[background]))
        self.assertTrue(np.array_equal(renderer.drawn.astype(np.uint8), expected))
        self.assertFalse(np.array_equal(renderer.color_img[50, 50], canvas))

    def _project(self, *, schema_version: int = 2):
        for directory in (
            "source", "planning", "scenes", "manifests", "previews", "output", ".work",
            "audio", "subtitles",
        ):
            (self.root / directory).mkdir(exist_ok=True)
        source = self.root / "source" / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:00,517\n第一幕\n\n"
            "2\n00:00:00,517 --> 00:00:01,034\n第二幕\n",
            encoding="utf-8",
        )
        project_id = str(uuid.uuid4())
        scenes = [
            {
                "sceneId": "scene-01",
                "sourceCueRange": [1, 1],
                "sceneDurationMs": 517,
                "prompt": "第一幕以简洁主体呈现开场内容",
                "outputFile": "scene-01.png",
            },
            {
                "sceneId": "scene-02",
                "sourceCueRange": [2, 2],
                "sceneDurationMs": 517,
                "prompt": "第二幕以简洁主体呈现后续内容",
                "outputFile": "scene-02.png",
            },
        ]
        plan = {
            "schemaVersion": 1,
            "projectId": project_id,
            "outputCanvas": dict(project_workspace.FIXED_CANVAS),
            "globalPrompt": project_workspace.DEFAULT_GLOBAL_PROMPT,
            "constraints": {"forbidText": True},
            "scenesDirectory": "scenes",
            "manifestFile": "manifests/generation-manifest.json",
            "scenes": scenes,
        }
        metadata = {
            "schemaVersion": schema_version,
            "projectId": project_id,
            "projectName": self.root.name,
            "createdAt": "2026-08-14T12:00:00+08:00",
            "source": {"file": "source/source.srt", "sha256": project_workspace.sha256_file(source)},
            "paths": dict(
                project_workspace.PROJECT_PATHS_V2
                if schema_version == 2
                else project_workspace.PROJECT_PATHS_V1
            ),
        }
        if schema_version == 2:
            metadata.update(
                voiceoverMode="disabled",
                renderProfile=dict(project_workspace.FIXED_RENDER_PROFILE),
            )
        (self.root / "planning" / "generation-plan.json").write_text(
            json.dumps(plan, ensure_ascii=False), encoding="utf-8"
        )
        timing = srt_timeline.build_source_timing_plan(
            project_id=project_id,
            source_srt_path=source,
            scene_specs=scenes,
            render_profile=project_workspace.FIXED_RENDER_PROFILE,
            voiceover_mode="disabled",
        )
        if schema_version == 2:
            (self.root / "planning" / "timing-plan.json").write_text(
                json.dumps(timing, ensure_ascii=False), encoding="utf-8"
            )
        (self.root / "project.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        project = project_workspace.load_project(self.root)
        for scene in timing["scenes"]:
            image_path = self.root / "scenes" / f"{scene['sceneId']}.png"
            image = Image.new("RGB", (1920, 1080), "#F5EBD7")
            ImageDraw.Draw(image).rectangle((20, 20, 79, 79), outline="black", width=4)
            image.save(image_path)
            annotation = {
                "sceneId": scene["sceneId"],
                "canvas": {"width": 1920, "height": 1080},
                "sceneDurationMs": scene["sceneDurationMs"],
                "timingPlanSha256": (
                    project_workspace.sha256_file(project.timing_plan_path)
                    if schema_version == 2
                    else project_workspace.sha256_json(project.timing_plan)
                ),
                "renderProfileSha256": project_workspace.sha256_json(project.render_profile),
                "sceneFrameRange": {
                    "startFrame": scene["startFrame"],
                    "endFrameExclusive": scene["endFrameExclusive"],
                    "frameCount": scene["frameCount"],
                },
                "timingSource": {
                    "kind": "source-srt",
                    "timelineFile": "source/source.srt",
                    "timelineSha256": timing["activeTimeline"]["sha256"],
                    "sceneId": scene["sceneId"],
                    "sceneStartMs": scene["startMs"],
                    "sceneEndMs": scene["endMs"],
                },
                "elements": [
                    {
                        "id": "mark",
                        "sequence": 1,
                        "region": {"x": 10, "y": 10, "width": 80, "height": 80},
                        "reveal": {
                            "startMs": 0,
                            "durationMs": 17,
                            "protectedRegions": [],
                        },
                    }
                ],
            }
            if schema_version == 1:
                for key in ("timingPlanSha256", "renderProfileSha256", "sceneFrameRange", "timingSource"):
                    annotation.pop(key)
            (self.root / "scenes" / f"{scene['sceneId']}.annotation.json").write_text(
                json.dumps(annotation, ensure_ascii=False), encoding="utf-8"
            )
        return project_workspace.load_project(self.root)

    def _write_render_manifest(self, project, *, with_receipts: bool = True) -> None:
        scenes = {}
        for context in render_timing.resolve_formal_scenes(
            project, [item["sceneId"] for item in project.plan["scenes"]]
        ):
            context.output_path.write_bytes(f"formal-{context.scene_id}".encode("ascii"))
            media = {"marker": f"old-{context.scene_id}"}
            if with_receipts:
                media["validation"] = {
                    "deepReceipt": {
                        "contractVersion": media_validation.DEEP_MEDIA_RECEIPT_CONTRACT_VERSION,
                        "sceneId": context.scene_id,
                    }
                }
            render_options = {"fixture": True}
            scenes[context.scene_id] = {
                "outputFile": context.output_path.relative_to(project.root).as_posix(),
                "renderOptions": render_options,
                "renderIdentityHash": render_timing.render_identity(
                    context, render_options=render_options
                ),
                "media": media,
            }
        project_workspace.write_json_atomic(
            project.path(render_timing.RENDER_MANIFEST_FILE),
            {
                "schemaVersion": 1,
                "projectId": project.project_id,
                "scenes": scenes,
            },
        )

    def test_global_timing_and_scene_local_element_clock_are_separate(self) -> None:
        project = self._project()
        context = render_timing.resolve_formal_scene(project, "scene-02")
        self.assertEqual(context.timing_scene["startMs"], 517)
        self.assertEqual(context.annotation["elements"][0]["reveal"]["startMs"], 0)
        bad = dict(context.annotation)
        bad["elements"] = json.loads(json.dumps(context.annotation["elements"]))
        bad["elements"][0]["reveal"]["startMs"] = 517
        with self.assertRaisesRegex(render_timing.RenderTimingError, "sceneDurationMs - 500"):
            render_timing.validate_annotation(
                bad,
                project=project,
                timing_scene=context.timing_scene,
                timing_plan_sha256=context.timing_plan_sha256,
                render_profile_sha256=context.render_profile_sha256,
                active_timeline=context.active_timeline,
                audio_sha256=None,
                allow_v1_disabled_compat=False,
            )

    def test_batch_resolution_builds_global_context_once(self) -> None:
        project = self._project()
        with mock.patch.object(
            render_timing,
            "build_formal_validation_context",
            wraps=render_timing.build_formal_validation_context,
        ) as build:
            contexts = render_timing.resolve_formal_scenes(
                project, ["scene-01", "scene-02"]
            )
        self.assertEqual([item.scene_id for item in contexts], ["scene-01", "scene-02"])
        self.assertEqual(build.call_count, 1)

    def test_single_scene_compatibility_reuses_explicit_global_context(self) -> None:
        project = self._project()
        context = render_timing.build_formal_validation_context(project)
        with mock.patch.object(
            render_timing,
            "build_formal_validation_context",
            side_effect=AssertionError("显式 context 不得重复全局深验"),
        ):
            resolved = render_timing.resolve_formal_scene(
                project, "scene-01", context=context
            )
        self.assertEqual(resolved.scene_id, "scene-01")

    def test_two_non_integral_scene_boundaries_use_cumulative_frames(self) -> None:
        project = self._project()
        first, second = project.timing_plan["scenes"]
        self.assertEqual((first["frameCount"], second["frameCount"]), (32, 31))
        self.assertEqual(first["endFrameExclusive"], second["startFrame"])
        self.assertEqual(first["frameCount"] + second["frameCount"], 63)

    def test_tail_boundary_rejects_overrun_and_accepts_exact_limit(self) -> None:
        project = self._project()
        context = render_timing.resolve_formal_scene(project, "scene-01")
        annotation = json.loads(json.dumps(context.annotation))
        annotation["elements"][0]["reveal"]["durationMs"] = 18
        with self.assertRaisesRegex(render_timing.RenderTimingError, "sceneDurationMs - 500"):
            render_timing.validate_annotation(
                annotation,
                project=project,
                timing_scene=context.timing_scene,
                timing_plan_sha256=context.timing_plan_sha256,
                render_profile_sha256=context.render_profile_sha256,
                active_timeline=context.active_timeline,
                audio_sha256=None,
                allow_v1_disabled_compat=False,
            )

    def test_scene_duration_frame_range_and_scene_identity_mismatch_are_rejected(self) -> None:
        project = self._project()
        context = render_timing.resolve_formal_scene(project, "scene-01")
        cases = [
            ("sceneId", "scene-02", "sceneId"),
            ("sceneDurationMs", 518, "sceneDurationMs"),
        ]
        for field, value, message in cases:
            annotation = json.loads(json.dumps(context.annotation))
            annotation[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(render_timing.RenderTimingError, message):
                render_timing.validate_annotation(
                    annotation,
                    project=project,
                    timing_scene=context.timing_scene,
                    timing_plan_sha256=context.timing_plan_sha256,
                    render_profile_sha256=context.render_profile_sha256,
                    active_timeline=context.active_timeline,
                    audio_sha256=None,
                    allow_v1_disabled_compat=False,
                )
        annotation = json.loads(json.dumps(context.annotation))
        annotation["sceneFrameRange"]["frameCount"] += 1
        with self.assertRaisesRegex(render_timing.RenderTimingError, "sceneFrameRange"):
            render_timing.validate_annotation(
                annotation,
                project=project,
                timing_scene=context.timing_scene,
                timing_plan_sha256=context.timing_plan_sha256,
                render_profile_sha256=context.render_profile_sha256,
                active_timeline=context.active_timeline,
                audio_sha256=None,
                allow_v1_disabled_compat=False,
            )

    def test_standalone_phase_plan_never_extends_short_total(self) -> None:
        cfg = stream_render.Config(fps=60, gaze_seconds=3.0)
        plan = stream_render.plan_phases(1000, cfg)
        self.assertEqual(plan.ink_frames + plan.color_frames + plan.gaze_frames, 60)
        self.assertEqual(plan.gaze_frames, 60)

    def test_formal_overrides_and_stale_render_profile_are_rejected(self) -> None:
        project = self._project()
        args = render_stream_whiteboard._parse_args(
            ["--project", str(project.root), "--scene-id", "scene-01", "--fps", "30"]
        )
        with self.assertRaisesRegex(render_timing.RenderTimingError, "未持久化"):
            render_stream_whiteboard._formal_context(args)
        annotation_path = project.path("scenes/scene-01.annotation.json")
        annotation = json.loads(annotation_path.read_text(encoding="utf-8"))
        annotation["renderProfileSha256"] = "0" * 64
        annotation_path.write_text(json.dumps(annotation, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(render_timing.RenderTimingError, "renderProfileSha256 stale"):
            render_timing.resolve_formal_scene(project_workspace.load_project(project.root), "scene-01")

    def test_formal_render_requires_current_annotation_review_approval(self) -> None:
        project = self._project()
        args = render_stream_whiteboard._parse_args(
            ["--project", str(project.root), "--scene-id", "scene-01"]
        )
        with mock.patch.object(
            render_stream_whiteboard.annotation_review,
            "require_current_annotation_review_approval",
            side_effect=render_stream_whiteboard.annotation_review.AnnotationReviewApprovalRequired(
                "缺少 annotation review 人工批准"
            ),
        ):
            with self.assertRaises(
                render_stream_whiteboard.annotation_review.AnnotationReviewApprovalRequired
            ):
                render_stream_whiteboard._formal_context(args)

    def test_formal_batch_reuses_gate_context_and_hand_and_keeps_plan_order(self) -> None:
        project = self._project()
        args = render_stream_whiteboard._parse_args(
            [
                "--project", str(project.root),
                "--scene-ids", "scene-02", "scene-01",
            ]
        )
        config = mock.Mock()
        config.for_stage.return_value = 1
        real_build = render_timing.build_formal_validation_context
        real_resolve = render_timing.resolve_formal_scenes

        def rendered(_args, context, _frozen, _cfg, **_kwargs):
            return context.output_path, context.scene_id.replace("scene-", "") * 32

        with mock.patch.object(
            render_stream_whiteboard.annotation_review,
            "require_current_annotation_review_approval",
            return_value={"approved": True},
        ) as approval, mock.patch.object(
            render_timing,
            "build_formal_validation_context",
            wraps=real_build,
        ) as build, mock.patch.object(
            render_timing,
            "resolve_formal_scenes",
            wraps=real_resolve,
        ) as resolve, mock.patch.object(
            render_stream_whiteboard,
            "_load_formal_hand",
            return_value=(render_stream_whiteboard.DEFAULT_HAND, (mock.sentinel.hand, mock.sentinel.mask), "a" * 64),
        ) as load_hand, mock.patch.object(
            render_stream_whiteboard,
            "_render_formal_context",
            side_effect=rendered,
        ) as render_one, mock.patch.object(
            project_workspace,
            "load_workspace_config",
            return_value=config,
        ):
            result = render_stream_whiteboard._run_formal_batch(args)

        self.assertEqual(build.call_count, 1)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(approval.call_count, 1)
        self.assertEqual(load_hand.call_count, 1)
        self.assertEqual(
            [call.args[1].scene_id for call in render_one.call_args_list],
            ["scene-01", "scene-02"],
        )
        self.assertEqual(result["sceneOrder"], ["scene-01", "scene-02"])
        self.assertEqual(result["effectiveSceneRenderConcurrency"], 1)
        self.assertEqual(result["peakSceneRenderWorkers"], 1)
        self.assertFalse(result["approvalWritten"])

    def test_formal_batch_stale_error_stops_unpublished_scenes(self) -> None:
        project = self._project()
        args = render_stream_whiteboard._parse_args(
            ["--project", str(project.root), "--all"]
        )
        config = mock.Mock()
        config.for_stage.return_value = 1
        with mock.patch.object(
            render_stream_whiteboard.annotation_review,
            "require_current_annotation_review_approval",
            return_value={"approved": True},
        ), mock.patch.object(
            render_stream_whiteboard,
            "_load_formal_hand",
            return_value=(None, None, None),
        ), mock.patch.object(
            render_stream_whiteboard,
            "_render_formal_context",
            side_effect=render_timing.RenderTimingError("batch 期间 timing plan 已变化"),
        ) as render_one, mock.patch.object(
            project_workspace,
            "load_workspace_config",
            return_value=config,
        ):
            with self.assertRaisesRegex(render_timing.RenderTimingError, "timing plan"):
                render_stream_whiteboard._run_formal_batch(args)
        self.assertEqual(render_one.call_count, 1)

    def test_v1_disabled_requires_explicit_readonly_compatibility(self) -> None:
        project = self._project(schema_version=1)
        with self.assertRaisesRegex(render_timing.RenderTimingError, "allow-v1-disabled-compat"):
            render_timing.resolve_formal_scene(project, "scene-01")
        context = render_timing.resolve_formal_scene(
            project, "scene-01", allow_v1_disabled_compat=True
        )
        self.assertEqual(context.compatibility_mode, "schema-v1-disabled-readonly")
        self.assertIsNone(context.timing_plan_file)

    def test_edge_formal_render_refuses_current_timeline_without_approve_full(self) -> None:
        project = self._project()
        timeline_path = project.path("audio/timeline.json")
        timeline_path.write_text("{}", encoding="utf-8")
        metadata = json.loads(project.path("project.json").read_text(encoding="utf-8"))
        metadata["voiceoverMode"] = "edge-tts"
        project_workspace.write_json_atomic(project.path("project.json"), metadata)
        timing = json.loads(project.timing_plan_path.read_text(encoding="utf-8"))
        timing["voiceoverMode"] = "edge-tts"
        timing["activeTimeline"] = {
            "kind": "edge-tts-audio-timeline",
            "file": "audio/timeline.json",
            "sha256": project_workspace.sha256_file(timeline_path),
        }
        project_workspace.write_json_atomic(project.timing_plan_path, timing)
        current = project_workspace.load_project(project.root)
        with self.assertRaisesRegex(render_timing.RenderTimingError, "approve-full"):
            render_timing.resolve_formal_scene(current, "scene-01")

    def test_formal_cli_exit_codes_distinguish_input_stale_and_media_failures(self) -> None:
        argv = ["--project", str(self.root), "--scene-id", "scene-01"]
        cases = [
            (
                render_stream_whiteboard.annotation_review.AnnotationReviewApprovalRequired(
                    "缺少 annotation review 人工批准"
                ),
                5,
            ),
            (project_workspace.ProjectValidationError("timing plan 无效"), 2),
            (render_timing.RenderTimingError("annotation elements 必须是非空数组"), 2),
            (render_timing.RenderTimingError("annotation timingPlanSha256 stale"), 5),
            (render_timing.RenderTimingError("Edge 正式渲染要求 current approve-full"), 5),
            (
                render_timing.RenderTimingError(
                    "annotation timingSource.timelineSha256 与 current timing plan 不一致"
                ),
                5,
            ),
            (media_validation.MediaValidationError("ffmpeg 转码失败"), 4),
            (RuntimeError("实际写入帧数与权威 frameCount 不一致"), 4),
        ]
        for error, expected in cases:
            with self.subTest(error=error, expected=expected):
                with mock.patch.object(render_stream_whiteboard, "_run_formal", side_effect=error):
                    self.assertEqual(render_stream_whiteboard.main(argv), expected)

    def test_publish_binding_failure_restores_previous_formal_scene(self) -> None:
        candidate = self.root / "candidate.mp4"
        formal = self.root / "formal.mp4"
        candidate.write_bytes(b"new-candidate")
        formal.write_bytes(b"previous-formal")
        with mock.patch.object(
            media_validation,
            "bind_validated_video",
            side_effect=media_validation.MediaValidationError("receipt bytes stale"),
        ):
            with self.assertRaisesRegex(media_validation.MediaValidationError, "stale"):
                render_stream_whiteboard._publish_and_bind_scene(
                    candidate,
                    formal,
                    render_profile=dict(project_workspace.FIXED_RENDER_PROFILE),
                    expected_frame_count=1,
                    deep_receipt={"contractVersion": "stale"},
                )
        self.assertEqual(formal.read_bytes(), b"previous-formal")
        self.assertEqual(candidate.read_bytes(), b"new-candidate")

    def test_scene_media_validation_is_bounded_and_commits_in_plan_order(self) -> None:
        project = self._project()
        self._write_render_manifest(project)
        config = project_workspace.WorkspaceConfig(
            root=self.root,
            config_path=self.root / "workspace.fixture.json",
            concurrency=project_workspace.ExecutionConcurrency(
                default=1,
                scene_media_validation=2,
            ),
        )
        lock = threading.Lock()
        active = 0
        peak = 0
        completion_order: list[str] = []

        def bind(path, **kwargs):
            nonlocal active, peak
            scene_id = kwargs["deep_receipt"]["sceneId"]
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05 if scene_id == "scene-01" else 0.01)
            with lock:
                active -= 1
                completion_order.append(scene_id)
            frame_count = kwargs["expected_frame_count"]
            return {
                "marker": f"current-{scene_id}",
                "streams": {
                    "video": [{"frameCount": frame_count, "containerNbFrames": 999}],
                    "audio": [],
                },
                "validation": {
                    "decodedFrameCount": frame_count,
                    "frameCountEvidence": media_validation.FRAME_COUNT_EVIDENCE,
                    "mode": "binding",
                    "deepReceipt": kwargs["deep_receipt"],
                },
            }

        original_write = project_workspace.write_json_atomic
        with mock.patch.object(media_validation, "bind_validated_video", side_effect=bind), mock.patch.object(
            project_workspace, "write_json_atomic", wraps=original_write
        ) as write_manifest:
            report = render_stream_whiteboard.validate_scene_media_batch(
                project,
                workspace_config=config,
            )

        self.assertEqual(report.requested_workers, 2)
        self.assertEqual(report.effective_workers, 2)
        self.assertEqual(report.peak_active_workers, 2)
        self.assertEqual(peak, 2)
        self.assertEqual(completion_order, ["scene-02", "scene-01"])
        self.assertEqual(
            [result.task["sceneId"] for result in report.results],
            ["scene-01", "scene-02"],
        )
        self.assertEqual(write_manifest.call_count, 1)
        manifest = json.loads(
            project.path(render_timing.RENDER_MANIFEST_FILE).read_text(encoding="utf-8")
        )
        self.assertEqual(list(manifest["scenes"]), ["scene-01", "scene-02"])
        first = manifest["scenes"]["scene-01"]["media"]
        self.assertEqual(first["validation"]["decodedFrameCount"], 32)
        self.assertEqual(first["streams"]["video"][0]["frameCount"], 32)
        self.assertEqual(first["streams"]["video"][0]["containerNbFrames"], 999)

    def test_stale_scene_receipt_fails_closed_without_deep_fallback(self) -> None:
        project = self._project()
        self._write_render_manifest(project)
        config = project_workspace.WorkspaceConfig(
            root=self.root,
            config_path=self.root / "workspace.fixture.json",
        )
        with mock.patch.object(
            media_validation,
            "bind_validated_video",
            side_effect=media_validation.MediaValidationError("receipt version stale"),
        ), mock.patch.object(media_validation, "validate_video") as deep, mock.patch.object(
            project_workspace, "write_json_atomic"
        ) as write_manifest:
            report = render_stream_whiteboard.validate_scene_media_batch(
                project,
                ["scene-01"],
                workspace_config=config,
            )
        self.assertEqual(report.results[0].status, "failed")
        self.assertEqual(report.results[0].outcome.error.message, "worker 抛出未处理异常")
        self.assertNotIn(str(project.root), report.results[0].outcome.error.message)
        deep.assert_not_called()
        write_manifest.assert_not_called()
        manifest = json.loads(
            project.path(render_timing.RENDER_MANIFEST_FILE).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["scenes"]["scene-01"]["media"]["marker"], "old-scene-01")

    def test_missing_scene_receipt_runs_one_deep_validation(self) -> None:
        project = self._project()
        self._write_render_manifest(project, with_receipts=False)
        config = project_workspace.WorkspaceConfig(
            root=self.root,
            config_path=self.root / "workspace.fixture.json",
        )
        validated = {
            "marker": "deep-current",
            "validation": {
                "decodedFrameCount": 32,
                "frameCountEvidence": media_validation.FRAME_COUNT_EVIDENCE,
                "deepReceipt": {"contractVersion": media_validation.DEEP_MEDIA_RECEIPT_CONTRACT_VERSION},
            },
        }
        with mock.patch.object(
            media_validation, "validate_video", return_value=validated
        ) as deep, mock.patch.object(media_validation, "bind_validated_video") as bind:
            report = render_stream_whiteboard.validate_scene_media_batch(
                project,
                ["scene-01"],
                workspace_config=config,
            )
        self.assertEqual(report.results[0].status, "succeeded")
        self.assertEqual(deep.call_count, 1)
        bind.assert_not_called()

    def test_ffmpeg_sink_writes_exact_bgr24_bytes_and_explicit_contract(self) -> None:
        output = self.root / "candidate.mp4"
        captured: dict[str, object] = {}

        class PartialStdin:
            def __init__(self) -> None:
                self.data = bytearray()

            def write(self, payload) -> int:
                chunk = bytes(payload[:7])
                self.data.extend(chunk)
                return len(chunk)

            def close(self) -> None:
                return None

        class Process:
            def __init__(self, argv) -> None:
                self.argv = argv
                self.stdin = PartialStdin()
                self.stderr = io.BytesIO(b"")
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                output.write_bytes(b"fake-h264")
                self.returncode = 0
                return 0

            def kill(self):
                self.returncode = -9

        def popen(argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            captured["process"] = Process(argv)
            return captured["process"]

        first = np.arange(24, dtype=np.uint8).reshape(2, 4, 3)
        second = np.flip(first, axis=1).copy()
        sink = ffmpeg_frame_sink.FFmpegFrameSink(
            output,
            width=4,
            height=2,
            fps=60,
            expected_frame_count=2,
            ffmpeg_executable="ffmpeg-fixture",
            popen_factory=popen,
        )
        sink.write(first)
        sink.write(second)
        sink.close()

        process = captured["process"]
        self.assertEqual(bytes(process.stdin.data), first.tobytes() + second.tobytes())
        argv = captured["argv"]
        self.assertEqual(captured["kwargs"]["shell"], False)
        for value in (
            "rawvideo", "bgr24", "4x2", "60", "pipe:0",
            "libx264", "medium", "18", "yuv420p",
        ):
            self.assertIn(value, argv)

    def test_ffmpeg_sink_drains_large_stderr_without_blocking_stdin(self) -> None:
        output = self.root / "large-stderr.mp4"
        drained = threading.Event()

        class SignalingStderr(io.BytesIO):
            def read(self, size=-1):
                chunk = super().read(size)
                if not chunk:
                    drained.set()
                return chunk

        class GatedStdin:
            def write(self, payload):
                if not drained.wait(timeout=2):
                    raise OSError("stderr was not drained")
                return len(payload)

            def close(self):
                return None

        class Process:
            def __init__(self):
                self.stdin = GatedStdin()
                self.stderr = SignalingStderr(b"x" * (2 * 1024 * 1024))
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                output.write_bytes(b"fake-h264")
                self.returncode = 0
                return 0

            def kill(self):
                self.returncode = -9

        sink = ffmpeg_frame_sink.FFmpegFrameSink(
            output,
            width=2,
            height=2,
            fps=60,
            expected_frame_count=1,
            ffmpeg_executable="ffmpeg-fixture",
            popen_factory=lambda *args, **kwargs: Process(),
        )
        sink.write(np.zeros((2, 2, 3), dtype=np.uint8))
        sink.close()
        self.assertTrue(drained.is_set())

    def test_ffmpeg_sink_failures_remove_candidate_and_preserve_formal(self) -> None:
        formal = self.root / "formal.mp4"
        formal.write_bytes(b"previous-formal")

        class Stdin:
            def __init__(self, mode: str) -> None:
                self.mode = mode

            def write(self, payload):
                if self.mode == "broken":
                    raise BrokenPipeError("secret broken pipe")
                if self.mode == "disk":
                    raise OSError("disk full at C:\\secret\\candidate")
                return len(payload)

            def close(self):
                return None

        class Process:
            def __init__(self, mode: str, candidate: Path) -> None:
                self.mode = mode
                self.candidate = candidate
                self.stdin = Stdin(mode)
                self.stderr = io.BytesIO(
                    b"Authorization=super-secret C:\\private\\scene.mp4 https://secret.invalid/x"
                )
                self.returncode = 7 if mode == "early" else None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                if self.mode == "nonzero":
                    self.candidate.write_bytes(b"partial")
                    self.returncode = 3
                elif self.returncode is None:
                    self.returncode = 0
                return self.returncode

            def kill(self):
                self.returncode = -9

        frame = np.zeros((2, 2, 3), dtype=np.uint8)
        for mode in ("early", "broken", "disk", "nonzero"):
            with self.subTest(mode=mode):
                candidate = self.root / f"{mode}.candidate.mp4"
                process = Process(mode, candidate)
                sink = ffmpeg_frame_sink.FFmpegFrameSink(
                    candidate,
                    width=2,
                    height=2,
                    fps=60,
                    expected_frame_count=1,
                    ffmpeg_executable="ffmpeg-fixture",
                    popen_factory=lambda *args, _process=process, **kwargs: _process,
                )
                with self.assertRaises(media_validation.MediaValidationError) as raised:
                    sink.write(frame)
                    sink.close()
                message = str(raised.exception)
                self.assertNotIn("super-secret", message)
                self.assertNotIn("secret.invalid", message)
                self.assertNotIn("private", message)
                self.assertFalse(candidate.exists())
                self.assertEqual(formal.read_bytes(), b"previous-formal")

    def test_ffmpeg_sink_rejects_underwrite_and_overwrite(self) -> None:
        frame = np.zeros((2, 2, 3), dtype=np.uint8)

        class Stdin:
            def write(self, payload):
                return len(payload)

            def close(self):
                return None

        class Process:
            def __init__(self, candidate: Path) -> None:
                self.candidate = candidate
                self.stdin = Stdin()
                self.stderr = io.BytesIO()
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                self.candidate.write_bytes(b"partial")
                self.returncode = 0
                return 0

            def kill(self):
                self.returncode = -9

        under = self.root / "under.mp4"
        sink = ffmpeg_frame_sink.FFmpegFrameSink(
            under, width=2, height=2, fps=60, expected_frame_count=2,
            ffmpeg_executable="ffmpeg-fixture",
            popen_factory=lambda *args, **kwargs: Process(under),
        )
        sink.write(frame)
        with self.assertRaisesRegex(media_validation.MediaValidationError, "frameCount"):
            sink.close()
        self.assertFalse(under.exists())

        over = self.root / "over.mp4"
        sink = ffmpeg_frame_sink.FFmpegFrameSink(
            over, width=2, height=2, fps=60, expected_frame_count=1,
            ffmpeg_executable="ffmpeg-fixture",
            popen_factory=lambda *args, **kwargs: Process(over),
        )
        sink.write(frame)
        with self.assertRaisesRegex(media_validation.MediaValidationError, "超过"):
            sink.write(frame)
        self.assertFalse(over.exists())

    def test_phase7_bgr24_sink_replaces_opencv_mp4v_chain(self) -> None:
        source = (ROOT / "scripts" / "render_stream_whiteboard.py").read_text(encoding="utf-8")
        sink_source = (ROOT / "scripts" / "ffmpeg_frame_sink.py").read_text(encoding="utf-8")
        self.assertIn("ffmpeg_frame_sink.FFmpegFrameSink", source)
        self.assertNotIn("VideoWriter", source)
        self.assertNotIn("mp4v", source.casefold())
        self.assertIn('"rawvideo"', sink_source)
        self.assertIn('"bgr24"', sink_source)

if __name__ == "__main__":
    unittest.main()
