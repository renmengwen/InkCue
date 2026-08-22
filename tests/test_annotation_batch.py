from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import generate_voiceover  # noqa: E402
import project_workspace  # noqa: E402
import render_timing  # noqa: E402
import srt_timeline  # noqa: E402
import validate_annotations  # noqa: E402


TEST_ROOT = Path(tempfile.gettempdir()) / "p4ab"


class AnnotationBatchFixture(unittest.TestCase):
    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.workspace_root = (TEST_ROOT / f"w-{uuid.uuid4().hex[:8]}").resolve()
        self.project_root = self.workspace_root / "projects" / "p"
        self.project_root.mkdir(parents=True)

    def tearDown(self) -> None:
        if self.workspace_root.exists():
            self.workspace_root.relative_to(TEST_ROOT.resolve())
            shutil.rmtree(self.workspace_root)

    def make_project(self, count: int = 3, *, edge: bool = False):
        root = self.project_root
        for directory in (
            "source", "planning", "scenes", "manifests", "previews", "output",
            ".work", "audio", "subtitles",
        ):
            (root / directory).mkdir(exist_ok=True)
        source = root / "source" / "source.srt"
        source.write_text(
            "\n\n".join(
                f"{index}\n00:00:{index - 1:02d},000 --> 00:00:{index:02d},000\n第{index}幕"
                for index in range(1, count + 1)
            )
            + "\n",
            encoding="utf-8",
        )
        project_id = str(uuid.uuid4())
        scenes = [
            {
                "sceneId": f"scene-{index:02d}",
                "sourceCueRange": [index, index],
                "sceneDurationMs": 1000,
                "prompt": f"第{index}幕简洁线稿",
                "outputFile": f"scene-{index:02d}.png",
                "coreIdea": f"第{index}幕",
                "visualSubject": f"主体{index}",
            }
            for index in range(1, count + 1)
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
            "schemaVersion": 2,
            "projectId": project_id,
            "projectName": root.name,
            "createdAt": "2026-08-15T12:00:00+08:00",
            "source": {"file": "source/source.srt", "sha256": project_workspace.sha256_file(source)},
            "paths": dict(project_workspace.PROJECT_PATHS_V2),
            "voiceoverMode": "edge-tts" if edge else "disabled",
            "renderProfile": dict(project_workspace.FIXED_RENDER_PROFILE),
        }
        (root / "planning" / "generation-plan.json").write_text(
            json.dumps(plan, ensure_ascii=False), encoding="utf-8"
        )
        timing = srt_timeline.build_source_timing_plan(
            project_id=project_id,
            source_srt_path=source,
            scene_specs=scenes,
            render_profile=project_workspace.FIXED_RENDER_PROFILE,
            voiceover_mode="edge-tts" if edge else "disabled",
        )
        audio_sha = None
        full_identity = None
        if edge:
            timeline_path = root / "audio" / "timeline.json"
            timeline_path.write_text('{"fixture":"timeline"}', encoding="utf-8")
            timing["activeTimeline"] = {
                "kind": "edge-tts-audio-timeline",
                "file": "audio/timeline.json",
                "sha256": project_workspace.sha256_file(timeline_path),
            }
            audio_path = root / "audio" / "narration.wav"
            audio_path.write_bytes(b"fixture-canonical-wave")
            audio_sha = project_workspace.sha256_file(audio_path)
            full_identity = "a" * 64
            (root / "manifests" / "voice-manifest.json").write_text(
                json.dumps(
                    {
                        "fullApproval": {
                            "approved": True,
                            "identityHash": full_identity,
                            "reviewPolicy": "user_first",
                        }
                    }
                ),
                encoding="utf-8",
            )
        (root / "planning" / "timing-plan.json").write_text(
            json.dumps(timing, ensure_ascii=False), encoding="utf-8"
        )
        (root / "project.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )
        project = project_workspace.load_project(root)
        for scene in project.timing_plan["scenes"]:
            Image.new("RGB", (1920, 1080), "#F5EBD7").save(
                root / "scenes" / f"{scene['sceneId']}.png"
            )
        return project, audio_sha, full_identity

    def workspace(self, *, annotation_validation: int = 3, agents: int = 3):
        return project_workspace.WorkspaceConfig(
            root=self.workspace_root,
            config_path=self.workspace_root / "fixture-config.json",
            concurrency=project_workspace.ExecutionConcurrency(
                annotation_validation=annotation_validation
            ),
            agents=project_workspace.ExecutionAgentConcurrency(
                annotation_drafting=agents
            ),
        )

    def annotation(self, project, scene_id: str, context):
        timing = next(
            scene for scene in project.timing_plan["scenes"] if scene["sceneId"] == scene_id
        )
        source = {
            "kind": context.active_timeline["kind"],
            "timelineFile": context.active_timeline["file"],
            "timelineSha256": context.active_timeline["sha256"],
            "sceneId": scene_id,
            "sceneStartMs": timing["startMs"],
            "sceneEndMs": timing["endMs"],
        }
        if context.audio_sha256 is not None:
            source["audioSha256"] = context.audio_sha256
        return {
            "sceneId": scene_id,
            "canvas": {"width": 1920, "height": 1080},
            "sceneDurationMs": timing["sceneDurationMs"],
            "timingPlanSha256": context.timing_plan_sha256,
            "renderProfileSha256": context.render_profile_sha256,
            "sceneFrameRange": {
                "startFrame": timing["startFrame"],
                "endFrameExclusive": timing["endFrameExclusive"],
                "frameCount": timing["frameCount"],
            },
            "timingSource": source,
            "elements": [
                {
                    "id": "subject",
                    "sequence": 1,
                    "region": {"x": 10, "y": 20, "width": 200, "height": 180},
                    "reveal": {
                        "startMs": 0,
                        "durationMs": 200,
                        "protectedRegions": [],
                    },
                }
            ],
        }

    def prepare(self, project, context, *, count=None):
        scene_ids = [scene["sceneId"] for scene in project.plan["scenes"]]
        if count is not None:
            scene_ids = scene_ids[:count]
        return validate_annotations.prepare_annotation_drafting_tasks(
            self.workspace(),
            project,
            images_confirmed=True,
            context=context,
            scene_ids=scene_ids,
        )

    def write_unvalidated_completed_result(self, drafting, annotation):
        project_workspace.write_json_atomic(drafting.candidate_path, annotation)
        task = drafting.task
        project_workspace.write_json_atomic(
            task.context.result_json,
            {
                "contractVersion": "whiteboard-agent-result-v1",
                "taskId": task.data["taskId"],
                "taskKind": task.data["taskKind"],
                "scopeKind": task.data["scopeKind"],
                "attempt": task.data["attempt"],
                "taskSha256": task.task_sha256,
                "roleContractVersion": task.data["roleContractVersion"],
                "roleContractSha256": task.data["roleContractSha256"],
                "sequence": task.data["sequence"],
                "status": "completed",
                "inspectedInputs": list(task.data["inputs"]),
                "outputs": [
                    {
                        "file": task.context.relative_posix(drafting.candidate_path),
                        "sha256": project_workspace.sha256_file(drafting.candidate_path),
                    }
                ],
                "findings": [],
                "warnings": [],
                "error": None,
            },
        )


class AnnotationBatchTests(AnnotationBatchFixture):
    def test_eight_edge_scenes_validate_current_voiceover_once(self) -> None:
        project, audio_sha, full_identity = self.make_project(8, edge=True)
        current = {
            "fullApproved": True,
            "audioSha256": audio_sha,
            "fullIdentityHash": full_identity,
            "reviewPolicy": "user_first",
        }
        with mock.patch.object(
            generate_voiceover, "validate_current_voiceover", return_value=current
        ) as validate_voice:
            context = render_timing.build_formal_validation_context(project)
            tasks, _ = self.prepare(project, context)
            for drafting in tasks:
                validate_annotations.record_coordinator_annotation_candidate(
                    drafting,
                    self.annotation(project, drafting.scene_id, context),
                    project=project,
                    context=context,
                )
            summary = validate_annotations.validate_and_publish_annotation_batch(
                project,
                tasks,
                context=context,
                configured_concurrency=4,
            )
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(validate_voice.call_count, 1)

    def test_completion_order_does_not_change_formal_publish_order(self) -> None:
        project, _, _ = self.make_project()
        context = render_timing.build_formal_validation_context(project)
        tasks, _ = self.prepare(project, context)
        for index in (2, 0, 1):
            drafting = tasks[index]
            validate_annotations.record_coordinator_annotation_candidate(
                drafting,
                self.annotation(project, drafting.scene_id, context),
                project=project,
                context=context,
            )
        actual_publish: list[str] = []
        original = validate_annotations._publish_bytes_atomic

        def tracked(candidate, target):
            actual_publish.append(target.stem.split(".annotation")[0])
            return original(candidate, target)

        with mock.patch.object(validate_annotations, "_publish_bytes_atomic", side_effect=tracked):
            summary = validate_annotations.validate_and_publish_annotation_batch(
                project,
                [tasks[2], tasks[0], tasks[1]],
                context=context,
                configured_concurrency=3,
            )
        expected = ["scene-01", "scene-02", "scene-03"]
        self.assertEqual(summary["publishedOrder"], expected)
        self.assertEqual(actual_publish, expected)
        self.assertEqual(summary["nextHumanGate"], "annotation_content_confirmation")
        self.assertFalse(summary["globalAnnotationConfirmationWritten"])
        self.assertFalse(summary["fullPreviewStarted"])

    def test_partial_success_is_fail_and_never_advances_global_gate_or_preview(self) -> None:
        project, _, _ = self.make_project()
        context = render_timing.build_formal_validation_context(project)
        tasks, _ = self.prepare(project, context)
        for drafting in tasks[:2]:
            validate_annotations.record_coordinator_annotation_candidate(
                drafting,
                self.annotation(project, drafting.scene_id, context),
                project=project,
                context=context,
            )
        invalid = self.annotation(project, tasks[2].scene_id, context)
        invalid["elements"][0]["region"]["x"] = 1900
        self.write_unvalidated_completed_result(tasks[2], invalid)
        summary = validate_annotations.validate_and_publish_annotation_batch(
            project,
            tasks,
            context=context,
            configured_concurrency=3,
        )
        self.assertEqual(summary["status"], "FAIL")
        self.assertTrue(summary["partialSuccess"])
        self.assertEqual(summary["publishedOrder"], ["scene-01", "scene-02"])
        self.assertFalse(summary["globalAnnotationConfirmationWritten"])
        self.assertFalse(summary["fullPreviewStarted"])
        self.assertFalse(summary["allTechnicalCurrent"])
        self.assertIsNone(summary["nextHumanGate"])


if __name__ == "__main__":
    unittest.main()
