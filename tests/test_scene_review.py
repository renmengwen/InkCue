from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import approve_scene_review  # noqa: E402
import merge_scenes  # noqa: E402
import project_workspace  # noqa: E402
import render_timing  # noqa: E402
import scene_review  # noqa: E402
import srt_timeline  # noqa: E402


class SceneReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="srt-whiteboard-scene-review-"))
        for relative in ("source", "planning", "scenes", "manifests", "previews", "output", ".work"):
            (self.root / relative).mkdir()
        self.project_id = str(uuid.uuid4())
        source = self.root / "source" / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n第一幕\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n第二幕\n",
            encoding="utf-8",
        )
        scenes = [
            {
                "sceneId": f"scene-{index:02d}",
                "sourceCueRange": [index, index],
                "sceneDurationMs": 1000,
                "prompt": f"第{index}幕简洁白板线稿",
                "outputFile": f"scene-{index:02d}.png",
            }
            for index in (1, 2)
        ]
        plan = {
            "schemaVersion": 1,
            "projectId": self.project_id,
            "outputCanvas": dict(project_workspace.FIXED_CANVAS),
            "globalPrompt": "统一白板线稿，不含文字",
            "constraints": {"forbidText": True},
            "scenesDirectory": "scenes",
            "manifestFile": "manifests/generation-manifest.json",
            "scenes": scenes,
        }
        timing = srt_timeline.build_source_timing_plan(
            project_id=self.project_id,
            source_srt_path=source,
            scene_specs=scenes,
            render_profile=project_workspace.FIXED_RENDER_PROFILE,
            voiceover_mode="disabled",
        )
        metadata = {
            "schemaVersion": 2,
            "projectId": self.project_id,
            "projectName": self.root.name,
            "createdAt": "2026-08-17T00:00:00+08:00",
            "voiceoverMode": "disabled",
            "renderProfile": dict(project_workspace.FIXED_RENDER_PROFILE),
            "source": {"file": "source/source.srt", "sha256": project_workspace.sha256_file(source)},
            "paths": dict(project_workspace.PROJECT_PATHS_V2),
        }
        project_workspace.write_json_atomic(self.root / "planning/generation-plan.json", plan)
        project_workspace.write_json_atomic(self.root / "planning/timing-plan.json", timing)
        project_workspace.write_json_atomic(self.root / "project.json", metadata)
        project = project_workspace.load_project(self.root)
        timing_sha = project_workspace.sha256_file(project.timing_plan_path)
        profile_sha = project_workspace.sha256_json(project.render_profile)
        for generation_scene, timing_scene in zip(scenes, timing["scenes"], strict=True):
            stem = Path(generation_scene["outputFile"]).stem
            image = self.root / "scenes" / generation_scene["outputFile"]
            image.write_bytes(f"image-{stem}".encode("ascii"))
            annotation = {
                "sceneId": generation_scene["sceneId"],
                "sceneDurationMs": timing_scene["sceneDurationMs"],
                "canvas": {"width": 1920, "height": 1080},
                "timingPlanSha256": timing_sha,
                "renderProfileSha256": profile_sha,
                "sceneFrameRange": {
                    "startFrame": timing_scene["startFrame"],
                    "endFrameExclusive": timing_scene["endFrameExclusive"],
                    "frameCount": timing_scene["frameCount"],
                },
                "timingSource": {
                    "kind": timing["activeTimeline"]["kind"],
                    "timelineFile": timing["activeTimeline"]["file"],
                    "timelineSha256": timing["activeTimeline"]["sha256"],
                    "sceneId": generation_scene["sceneId"],
                    "sceneStartMs": timing_scene["startMs"],
                    "sceneEndMs": timing_scene["endMs"],
                },
                "elements": [
                    {
                        "id": "subject",
                        "sequence": 1,
                        "region": {"x": 20, "y": 20, "width": 200, "height": 200},
                        "reveal": {"startMs": 0, "durationMs": 400, "protectedRegions": []},
                    }
                ],
            }
            project_workspace.write_json_atomic(
                self.root / "scenes" / f"{stem}.annotation.json", annotation
            )
            (self.root / "scenes" / f"{stem}-whiteboard.mp4").write_bytes(
                f"video-{stem}".encode("ascii")
            )
        self.project = project_workspace.load_project(self.root)
        self.contexts = render_timing.resolve_formal_scenes(
            self.project, [scene["sceneId"] for scene in scenes]
        )
        self.render_options = {"fixture": True}
        for context in self.contexts:
            media = {
                "sha256": project_workspace.sha256_file(context.output_path),
                "bytes": context.output_path.stat().st_size,
                "validation": {"deepReceipt": {"fixture": context.scene_id}},
            }
            render_timing.update_render_manifest(
                context, media=media, render_options=self.render_options
            )

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    @staticmethod
    def _bind(path: Path, **_: object) -> dict[str, object]:
        return {"sha256": project_workspace.sha256_file(path), "bytes": path.stat().st_size}

    def _workspace(self):
        workspace = mock.Mock()
        workspace.load_project.side_effect = project_workspace.load_project
        workspace.config.for_stage.return_value = 1
        return workspace

    def _approve(self) -> dict[str, object]:
        with mock.patch.object(scene_review, "bind_validated_video", side_effect=self._bind):
            bundle = scene_review.build_scene_review_bundle(self.project)
        with mock.patch.object(
            approve_scene_review.ProjectWorkspace,
            "from_config",
            return_value=self._workspace(),
        ), mock.patch.object(scene_review, "bind_validated_video", side_effect=self._bind):
            approve_scene_review.approve_scene_review(
                str(self.root), bundle["identityHash"]
            )
        return bundle

    def test_bundle_approval_is_ordered_current_and_media_change_is_stale(self) -> None:
        bundle = self._approve()
        self.assertEqual(bundle["sceneOrder"], ["scene-01", "scene-02"])
        self.assertEqual(
            [item["sceneId"] for item in bundle["scenes"]],
            ["scene-01", "scene-02"],
        )
        with mock.patch.object(scene_review, "bind_validated_video", side_effect=self._bind):
            current = scene_review.assert_current_scene_review_approval(self.project)
        self.assertEqual(current["identityHash"], bundle["identityHash"])

        self.contexts[1].output_path.write_bytes(b"changed-after-review")
        with mock.patch.object(scene_review, "bind_validated_video", side_effect=self._bind):
            with self.assertRaisesRegex(scene_review.SceneReviewStaleError, "媒体字节 stale"):
                scene_review.assert_current_scene_review_approval(self.project)

    def test_successful_rerender_clears_prior_batch_approval(self) -> None:
        self._approve()
        context = self.contexts[0]
        media = {
            "sha256": project_workspace.sha256_file(context.output_path),
            "bytes": context.output_path.stat().st_size,
            "validation": {"deepReceipt": {"fixture": context.scene_id}},
        }
        render_timing.update_render_manifest(
            context, media=media, render_options=self.render_options
        )
        manifest = json.loads(
            self.project.path(render_timing.RENDER_MANIFEST_FILE).read_text(encoding="utf-8")
        )
        self.assertIsNone(manifest["sceneReviewApproval"])

    def test_missing_approval_blocks_merge_with_exit_code_five(self) -> None:
        inputs = [context.output_path for context in self.contexts]
        with mock.patch.object(
            merge_scenes.ProjectWorkspace,
            "from_config",
            return_value=self._workspace(),
        ), mock.patch.object(scene_review, "bind_validated_video", side_effect=self._bind):
            exit_code = merge_scenes.main(
                ["--project", str(self.root), "--inputs", *[str(path) for path in inputs]]
            )
        self.assertEqual(exit_code, 5)
        self.assertFalse((self.root / "output/final-video-only.mp4").exists())

    def test_approval_rejects_noncurrent_identity_and_merge_order_is_bound(self) -> None:
        with mock.patch.object(
            approve_scene_review.ProjectWorkspace,
            "from_config",
            return_value=self._workspace(),
        ), mock.patch.object(scene_review, "bind_validated_video", side_effect=self._bind):
            with self.assertRaisesRegex(scene_review.SceneReviewGateError, "current bundle"):
                approve_scene_review.approve_scene_review(str(self.root), "0" * 64)
        self._approve()
        with mock.patch.object(scene_review, "bind_validated_video", side_effect=self._bind):
            with self.assertRaisesRegex(scene_review.SceneReviewGateError, "顺序"):
                scene_review.assert_current_scene_review_approval(
                    self.project,
                    inputs=[self.contexts[1].output_path, self.contexts[0].output_path],
                )

    def test_approval_gate_returns_single_pass_bound_media_for_merge(self) -> None:
        self._approve()
        with mock.patch.object(
            scene_review,
            "bind_validated_video",
            side_effect=self._bind,
        ) as bind:
            current = scene_review.assert_current_scene_review_approval(
                self.project,
                inputs=[context.output_path for context in self.contexts],
            )
        self.assertEqual(bind.call_count, len(self.contexts))
        self.assertEqual(
            [item["sceneId"] for item in current["validatedSceneMedia"]],
            ["scene-01", "scene-02"],
        )

    def test_force_deep_is_performed_once_per_scene_inside_approval_gate(self) -> None:
        self._approve()
        with mock.patch.object(
            scene_review,
            "bind_validated_video",
            side_effect=AssertionError("force-deep 不得先做一遍普通 binding"),
        ), mock.patch.object(
            scene_review,
            "validate_video",
            side_effect=self._bind,
        ) as validate:
            current = scene_review.assert_current_scene_review_approval(
                self.project,
                inputs=[context.output_path for context in self.contexts],
                force_deep=True,
            )
        self.assertEqual(validate.call_count, len(self.contexts))
        self.assertTrue(all(call.kwargs["force_deep"] is True for call in validate.call_args_list))
        self.assertEqual(len(current["validatedSceneMedia"]), len(self.contexts))

    def test_user_first_records_semantic_review_skipped_without_spawn(self) -> None:
        workspace = self._workspace()
        workspace.config.root = self.root.parent
        workspace.config.for_role.return_value = 1
        with mock.patch.object(scene_review, "bind_validated_video", side_effect=self._bind):
            result = scene_review.inspect_scene_review(
                self.project,
                review_policy="user_first",
                workspace=workspace,
            )
        self.assertEqual(result["reviewPolicy"], "user_first")
        self.assertEqual(result["semanticReview"]["status"], "skipped_by_user")
        self.assertIsNone(result["semanticReview"]["spawnPackage"])

    def test_agent_first_prepares_one_scene_bundle_spawn_package(self) -> None:
        workspace = self._workspace()
        workspace.config.root = self.root.parent
        workspace.config.for_role.return_value = 1
        with mock.patch.object(scene_review, "bind_validated_video", side_effect=self._bind):
            result = scene_review.inspect_scene_review(
                self.project,
                review_policy="agent_first",
                workspace=workspace,
            )
        self.assertEqual(result["reviewPolicy"], "agent_first")
        review = result["semanticReview"]
        self.assertEqual(review["status"], "ready_for_host_spawn")
        package = review["spawnPackage"]
        self.assertIsNotNone(package)
        self.assertEqual(package["taskKind"], "visualReview")
        self.assertTrue(package["preparedOnly"])
        task = json.loads(Path(package["taskJsonPath"]).read_text(encoding="utf-8"))
        self.assertEqual(task["taskKind"], "visualReview")
        self.assertEqual(
            [item["file"] for item in task["inputs"] if item["file"].endswith(".mp4")],
            ["scenes/scene-01-whiteboard.mp4", "scenes/scene-02-whiteboard.mp4"],
        )


if __name__ == "__main__":
    unittest.main()
