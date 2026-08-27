from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from PIL import Image


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
TEST_RUNS = Path(tempfile.gettempdir()) / "srt-whiteboard-image-cli-tests"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import merge_scenes  # noqa: E402
import generate_images  # noqa: E402
import image_generation  # noqa: E402
import project_workspace  # noqa: E402
import validate_generated_images  # noqa: E402


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ImageGenerationCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertEqual(TEST_RUNS.resolve().drive.upper(), "C:")
        TEST_RUNS.mkdir(parents=True, exist_ok=True)
        self.root = (TEST_RUNS / str(uuid.uuid4())).resolve()
        self.root.mkdir()
        for relative in [
            "source",
            "planning",
            "scenes",
            "manifests",
            "previews",
            "output",
            ".work",
        ]:
            (self.root / relative).mkdir()
        source = self.root / "source" / "source.srt"
        source.write_text("1\n00:00:00,000 --> 00:00:01,000\n测试\n", encoding="utf-8")
        self.project_id = str(uuid.uuid4())
        self.metadata = {
            "schemaVersion": 1,
            "projectId": self.project_id,
            "projectName": self.root.name,
            "createdAt": "2026-08-14T12:00:00+08:00",
            "source": {"file": "source/source.srt", "sha256": _sha256(source)},
            "paths": {
                "planning": "planning",
                "scenes": "scenes",
                "manifests": "manifests",
                "previews": "previews",
                "output": "output",
                "work": ".work",
            },
        }
        (self.root / "project.json").write_text(
            json.dumps(self.metadata, ensure_ascii=False), encoding="utf-8"
        )

    def tearDown(self) -> None:
        resolved = self.root.resolve()
        self.assertEqual(resolved.parent, TEST_RUNS.resolve())
        shutil.rmtree(resolved)

    def test_project_generation_lock_blocks_concurrent_coordinator_and_releases(self) -> None:
        lock = generate_images._acquire_generation_lock(self.root)
        try:
            with self.assertRaises(generate_images.ManifestError) as caught:
                generate_images._acquire_generation_lock(self.root)
            self.assertIn("image_generation_in_progress", str(caught.exception))
        finally:
            generate_images._release_generation_lock(lock)
        self.assertFalse(lock.exists())

    def _write_plan(self, scenes: list[dict[str, object]]) -> Path:
        plan = {
            "schemaVersion": 1,
            "projectId": self.project_id,
            "outputCanvas": {
                "width": 1920,
                "height": 1080,
                "background": "#F5EBD7",
                "fit": "contain",
            },
            "globalPrompt": "统一白板线稿，不含文字",
            "constraints": {"forbidText": True},
            "scenesDirectory": "scenes",
            "manifestFile": "manifests/generation-manifest.json",
            "scenes": scenes,
        }
        path = self.root / "planning" / "generation-plan.json"
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return path

    def _configured_workspace(
        self,
        module: object,
        *,
        image_generation_concurrency: int = 1,
        image_validation_concurrency: int = 1,
    ):
        workspace = mock.Mock()
        def for_stage(stage: str) -> int:
            return (
                image_generation_concurrency
                if stage == "imageGeneration"
                else image_validation_concurrency
            )
        workspace.config = SimpleNamespace(
            root=self.root.parent,
            for_stage=for_stage,
            for_role=lambda _role: 1,
        )
        workspace.load_project.side_effect = project_workspace.load_project
        return mock.patch.object(
            module.ProjectWorkspace,
            "from_config",
            return_value=workspace,
        )

    def _write_valid_scene(self) -> tuple[Path, dict[str, object]]:
        scene = {
            "sceneId": "scene-01",
            "name": "概念",
            "subtitleRange": {"startMs": 0, "endMs": 1000},
            "sceneDurationMs": 1000,
            "prompt": "一个清晰概念",
            "outputFile": "scene-01-概念.png",
        }
        plan_path = self._write_plan([scene])
        image_path = self.root / "scenes" / scene["outputFile"]
        Image.new("RGB", (1920, 1080), "#F5EBD7").save(image_path, "PNG")
        manifest = {
            "schemaVersion": 1,
            "projectId": self.project_id,
            "generationPlan": {
                "file": "planning/generation-plan.json",
                "sha256": _sha256(plan_path),
            },
            "createdAt": "2026-08-14T12:00:00+00:00",
            "updatedAt": "2026-08-14T12:00:00+00:00",
            "completedAt": "2026-08-14T12:00:00+00:00",
            "summary": {"sceneTotal": 1, "successCount": 1, "failedCount": 0},
            "runs": [],
            "scenes": [
                {
                    "sceneId": "scene-01",
                    "outputFile": scene["outputFile"],
                    "status": "validated",
                    "imageSha256": _sha256(image_path),
                }
            ],
        }
        (self.root / "manifests" / "generation-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        return image_path, scene

    def _provider(self) -> image_generation.ProviderConfig:
        return image_generation.ProviderConfig(
            name="primary",
            protocol="openai-images-generations",
            base_url="https://example.invalid/v1",
            endpoint="https://example.invalid/v1/images/generations",
            api_key="test-secret",
            model="test-image-model",
            size="1024x1024",
            response_format="b64_json",
            request_timeout_seconds=1,
            download_timeout_seconds=1,
            max_bytes=10_000_000,
            extra_body={},
            config_path=(self.root / "provider.local.json"),
        )

    def test_provider_argument_is_optional_and_defaults_to_active_provider(self) -> None:
        args = generate_images.build_parser().parse_args(
            ["--project", str(self.root)]
        )
        self.assertIsNone(args.provider)

        skill_root = Path(generate_images.SKILL_ROOT)
        for relative in (
            "SKILL.md",
            "README.md",
            "references/image-generation.md",
        ):
            content = (skill_root / relative).read_text(encoding="utf-8")
            self.assertNotIn("--provider " + "primary", content)

    def test_scene_scope_is_explicit_validated_and_keeps_plan_order(self) -> None:
        scenes = [
            {
                "sceneId": f"scene-{index:02d}",
                "name": f"场景{index}",
                "subtitleRange": {"startMs": (index - 1) * 1000, "endMs": index * 1000},
                "sceneDurationMs": 1000,
                "prompt": f"第{index}幕有明确主体",
                "outputFile": f"scene-{index:02d}.png",
            }
            for index in range(1, 4)
        ]
        self.assertEqual(
            [
                scene["sceneId"]
                for scene in generate_images._scoped_plan_scenes(
                    scenes, ["scene-03", "scene-01"]
                )
            ],
            ["scene-01", "scene-03"],
        )
        with self.assertRaisesRegex(generate_images.CliArgumentError, "不得重复"):
            generate_images._scoped_plan_scenes(scenes, ["scene-01", "scene-01"])
        with self.assertRaisesRegex(generate_images.CliArgumentError, "不属于 generation plan"):
            generate_images._scoped_plan_scenes(scenes, ["scene-99"])

    def test_scene_scope_only_requests_and_overwrites_selected_scene(self) -> None:
        scenes = [
            {
                "sceneId": f"scene-{index:02d}",
                "name": f"场景{index}",
                "subtitleRange": {"startMs": (index - 1) * 1000, "endMs": index * 1000},
                "sceneDurationMs": 1000,
                "prompt": f"第{index}幕有明确主体",
                "outputFile": f"scene-{index:02d}.png",
            }
            for index in range(1, 3)
        ]
        self._write_plan(scenes)
        first_payload = self._image_payload()
        first_client = mock.Mock()
        first_client.generate.return_value = first_payload
        with self._configured_workspace(generate_images), mock.patch.object(
            generate_images, "verify_config_git_safety", return_value=[]
        ), mock.patch.object(
            generate_images, "load_provider_config", return_value=self._provider()
        ), mock.patch.object(
            generate_images, "ImagesGenerationsClient", return_value=first_client
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(generate_images.main(["--project", str(self.root)]), 0)

        untouched_path = self.root / "scenes" / "scene-01.png"
        selected_path = self.root / "scenes" / "scene-02.png"
        untouched_hash = _sha256(untouched_path)
        selected_hash = _sha256(selected_path)
        replacement_buffer = io.BytesIO()
        Image.new("RGB", (512, 512), "#8B3A2B").save(replacement_buffer, "PNG")
        replacement = image_generation.ImagePayload(
            data=replacement_buffer.getvalue(), source="b64_json", attempts=1
        )
        second_client = mock.Mock()
        second_client.generate.return_value = replacement
        stdout = io.StringIO()
        with self._configured_workspace(generate_images), mock.patch.object(
            generate_images, "verify_config_git_safety", return_value=[]
        ), mock.patch.object(
            generate_images, "load_provider_config", return_value=self._provider()
        ), mock.patch.object(
            generate_images, "ImagesGenerationsClient", return_value=second_client
        ), contextlib.redirect_stdout(stdout):
            exit_code = generate_images.main(
                [
                    "--project",
                    str(self.root),
                    "--scene-id",
                    "scene-02",
                    "--overwrite",
                ]
            )
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0, summary)
        self.assertEqual(summary["targeted"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(second_client.generate.call_count, 1)
        self.assertEqual(_sha256(untouched_path), untouched_hash)
        self.assertNotEqual(_sha256(selected_path), selected_hash)
        manifest = json.loads(
            (self.root / "manifests" / "generation-manifest.json").read_text(encoding="utf-8")
        )
        by_id = {scene["sceneId"]: scene for scene in manifest["scenes"]}
        self.assertEqual(len(by_id["scene-01"]["attemptRecords"]), 1)
        self.assertEqual(len(by_id["scene-02"]["attemptRecords"]), 2)

    def _image_payload(self) -> image_generation.ImagePayload:
        source_buffer = io.BytesIO()
        Image.new("RGB", (512, 512), "#F5EBD7").save(source_buffer, "PNG")
        return image_generation.ImagePayload(
            data=source_buffer.getvalue(), source="b64_json", attempts=1
        )

    def test_validate_generated_images_success(self) -> None:
        self._write_valid_scene()
        stdout = io.StringIO()
        with self._configured_workspace(validate_generated_images), contextlib.redirect_stdout(stdout):
            exit_code = validate_generated_images.main(["--project", str(self.root)])
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0, summary)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["validated"], 1)
        self.assertTrue(summary["userConfirmationRequired"])
        self.assertEqual(summary["lineArtReview"]["sceneCount"], 1)
        self.assertFalse(summary["lineArtReview"]["approvalWritten"])

    def test_validation_writes_identity_bound_line_art_file_handoff(self) -> None:
        self._write_valid_scene()
        stdout = io.StringIO()
        with self._configured_workspace(validate_generated_images), contextlib.redirect_stdout(stdout):
            exit_code = validate_generated_images.main(["--project", str(self.root)])
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0, summary)

        review = summary["lineArtReview"]
        identity = review["lineArtReviewIdentitySha256"]
        self.assertEqual(len(identity), 64)
        self.assertRegex(review["reviewFile"], rf"^reviews/line-art-review-{identity[:12]}\.md$")
        review_path = self.root / Path(review["reviewFile"])
        technical_path = self.root / Path(review["manifestFile"])
        self.assertTrue(review_path.is_file())
        self.assertTrue(technical_path.is_file())

        markdown = review_path.read_text(encoding="utf-8")
        self.assertIn(f"lineArtReviewIdentitySha256: {identity}", markdown)
        self.assertIn("打开全分辨率原图", markdown)
        self.assertIn("确认线稿", markdown)
        self.assertNotRegex(markdown, r"(?i)[a-z]:[\\/]")

        technical = json.loads(technical_path.read_text(encoding="utf-8"))
        self.assertEqual(technical["status"], "current_technical")
        self.assertEqual(technical["identityHash"], identity)
        self.assertEqual(technical["identityPayload"]["sceneOrder"], ["scene-01"])
        self.assertFalse(technical["approvalWritten"])
        self.assertTrue(technical["userConfirmationRequired"])

    def test_line_art_review_identity_changes_with_current_image_bytes(self) -> None:
        image_path, _ = self._write_valid_scene()
        first_stdout = io.StringIO()
        with self._configured_workspace(validate_generated_images), contextlib.redirect_stdout(first_stdout):
            first_exit = validate_generated_images.main(["--project", str(self.root)])
        first = json.loads(first_stdout.getvalue())
        self.assertEqual(first_exit, 0, first)
        first_review_path = self.root / Path(first["lineArtReview"]["reviewFile"])

        Image.new("RGB", (1920, 1080), "#EAD8B8").save(image_path, "PNG")
        manifest_path = self.root / "manifests" / "generation-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["scenes"][0]["imageSha256"] = _sha256(image_path)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        second_stdout = io.StringIO()
        with self._configured_workspace(validate_generated_images), contextlib.redirect_stdout(second_stdout):
            second_exit = validate_generated_images.main(["--project", str(self.root)])
        second = json.loads(second_stdout.getvalue())
        self.assertEqual(second_exit, 0, second)
        self.assertNotEqual(
            first["lineArtReview"]["lineArtReviewIdentitySha256"],
            second["lineArtReview"]["lineArtReviewIdentitySha256"],
        )
        self.assertTrue(first_review_path.is_file())
        current = json.loads(
            (self.root / "manifests" / "line-art-review-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            current["identityHash"],
            second["lineArtReview"]["lineArtReviewIdentitySha256"],
        )

    def test_validate_generated_images_defaults_to_user_first(self) -> None:
        self._write_valid_scene()
        stdout = io.StringIO()
        with self._configured_workspace(validate_generated_images), contextlib.redirect_stdout(stdout):
            exit_code = validate_generated_images.main(["--project", str(self.root)])
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0, summary)
        self.assertEqual(summary["reviewPolicy"], "user_first")
        self.assertEqual(summary["semanticReview"]["status"], "skipped_by_user")
        self.assertNotIn("visualReview", summary)

    def test_validate_review_policy_user_first_skips_visual_review(self) -> None:
        self._write_valid_scene()
        stdout = io.StringIO()
        with self._configured_workspace(validate_generated_images), mock.patch.object(
            validate_generated_images,
            "prepare_visual_review_dispatch",
            side_effect=AssertionError("user_first 不应创建 visualReview"),
        ), contextlib.redirect_stdout(stdout):
            exit_code = validate_generated_images.main(
                ["--project", str(self.root), "--review-policy", "user_first"]
            )
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0, summary)
        self.assertEqual(summary["reviewPolicy"], "user_first")
        self.assertEqual(summary["semanticReview"]["status"], "skipped_by_user")
        self.assertFalse(summary["semanticReview"]["approvalWritten"])
        self.assertTrue(summary["semanticReview"]["userConfirmationRequired"])
        self.assertNotIn("visualReview", summary)

    def test_validate_review_policy_agent_first_prepares_visual_review(self) -> None:
        self._write_valid_scene()
        stdout = io.StringIO()
        with self._configured_workspace(validate_generated_images), contextlib.redirect_stdout(stdout):
            exit_code = validate_generated_images.main(
                ["--project", str(self.root), "--review-policy", "agent_first"]
            )
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0, summary)
        self.assertEqual(summary["reviewPolicy"], "agent_first")
        self.assertIn(summary["semanticReview"]["status"], {"ready_for_host_spawn", "pending_child_result"})
        self.assertFalse(summary["semanticReview"]["approvalWritten"])
        self.assertEqual(summary["visualReview"]["taskKind"], "visualReview")

    def test_prepare_visual_review_remains_agent_first_compatibility_alias(self) -> None:
        self._write_valid_scene()
        stdout = io.StringIO()
        with self._configured_workspace(validate_generated_images), contextlib.redirect_stdout(stdout):
            exit_code = validate_generated_images.main(
                ["--project", str(self.root), "--prepare-visual-review"]
            )
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0, summary)
        self.assertEqual(summary["reviewPolicy"], "agent_first")
        self.assertEqual(summary["visualReview"]["taskKind"], "visualReview")

    def test_agent_first_accepts_redundant_legacy_prepare_flag(self) -> None:
        self._write_valid_scene()
        stdout = io.StringIO()
        with self._configured_workspace(validate_generated_images), contextlib.redirect_stdout(stdout):
            exit_code = validate_generated_images.main(
                [
                    "--project",
                    str(self.root),
                    "--review-policy",
                    "agent_first",
                    "--prepare-visual-review",
                ]
            )
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0, summary)
        self.assertEqual(summary["reviewPolicy"], "agent_first")
        self.assertEqual(summary["semanticReview"]["status"], "ready_for_host_spawn")

    def test_review_policy_user_first_conflicts_with_legacy_prepare_flag(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = validate_generated_images.main(
                [
                    "--project",
                    str(self.root),
                    "--review-policy",
                    "user_first",
                    "--prepare-visual-review",
                ]
            )
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2, summary)
        self.assertEqual(summary["reviewPolicy"], "user_first")
        self.assertEqual(summary["semanticReview"]["status"], "invalid_combination")
        self.assertFalse(summary["approvalWritten"])

    def test_validate_detects_modified_image(self) -> None:
        image_path, _ = self._write_valid_scene()
        Image.new("RGB", (1920, 1080), "black").save(image_path, "PNG")
        stdout = io.StringIO()
        with self._configured_workspace(validate_generated_images), contextlib.redirect_stdout(stdout):
            exit_code = validate_generated_images.main(["--project", str(self.root)])
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["failed"], 1)
        self.assertIn("SHA-256", summary["failures"][0]["error"])

    def test_generate_empty_plan_is_not_reported_as_success(self) -> None:
        self._write_plan([])
        import generate_images

        stdout = io.StringIO()
        with self._configured_workspace(generate_images), contextlib.redirect_stdout(stdout):
            exit_code = generate_images.main(["--project", str(self.root)])
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["total"], 0)

    def test_missing_scene_prompt_fails_before_provider_client_is_created(self) -> None:
        scene = {
            "sceneId": "scene-01",
            "name": "缺提示词",
            "subtitleRange": {"startMs": 0, "endMs": 1000},
            "sceneDurationMs": 1000,
            "prompt": "   ",
            "outputFile": "scene-01.png",
        }
        self._write_plan([scene])
        import generate_images

        stdout = io.StringIO()
        with self._configured_workspace(generate_images), mock.patch.object(
            generate_images, "ImagesGenerationsClient"
        ) as client_type, contextlib.redirect_stdout(stdout):
            exit_code = generate_images.main(["--project", str(self.root)])
        self.assertEqual(exit_code, 2)
        client_type.assert_not_called()

    def test_generate_success_writes_manifest_and_consumable_png(self) -> None:
        scene = {
            "sceneId": "scene-01",
            "name": "概念",
            "subtitleRange": {"startMs": 0, "endMs": 1000},
            "sceneDurationMs": 1000,
            "prompt": "一个清晰概念",
            "outputFile": "scene-01-概念.png",
        }
        self._write_plan([scene])
        import generate_images

        provider = self._provider()
        client = mock.Mock()
        client.generate.return_value = self._image_payload()
        stdout = io.StringIO()
        with self._configured_workspace(generate_images), mock.patch.object(
            generate_images, "verify_config_git_safety", return_value=[]
        ), mock.patch.object(
            generate_images, "load_provider_config", return_value=provider
        ), mock.patch.object(
            generate_images, "ImagesGenerationsClient", return_value=client
        ), contextlib.redirect_stdout(stdout):
            exit_code = generate_images.main(["--project", str(self.root)])
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0, summary)
        self.assertEqual(summary["succeeded"], 1)
        self.assertEqual(client.generate.call_count, 1)
        self.assertTrue((self.root / "scenes" / scene["outputFile"]).is_file())
        manifest = json.loads(
            (self.root / "manifests" / "generation-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["scenes"][0]["status"], "validated")
        self.assertNotIn("test-secret", json.dumps(manifest, ensure_ascii=False))
        self.assertEqual(list((self.root / ".work").iterdir()), [])

        validate_stdout = io.StringIO()
        with self._configured_workspace(validate_generated_images), contextlib.redirect_stdout(
            validate_stdout
        ):
            validate_exit = validate_generated_images.main(["--project", str(self.root)])
        self.assertEqual(validate_exit, 0)

    def test_image_generation_uses_configured_bounded_concurrency_and_keeps_plan_order(self) -> None:
        source = self.root / "source" / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n第一幕\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n第二幕\n\n"
            "3\n00:00:02,000 --> 00:00:03,000\n第三幕\n",
            encoding="utf-8",
        )
        self.metadata["source"]["sha256"] = _sha256(source)
        (self.root / "project.json").write_text(
            json.dumps(self.metadata, ensure_ascii=False), encoding="utf-8"
        )
        scenes = [
            {
                "sceneId": f"scene-{index:02d}",
                "name": f"场景{index}",
                "subtitleRange": {"startMs": (index - 1) * 1000, "endMs": index * 1000},
                "sceneDurationMs": 1000,
                "prompt": f"第{index}幕有明确主体",
                "outputFile": f"scene-{index:02d}.png",
            }
            for index in range(1, 4)
        ]
        self._write_plan(scenes)
        import generate_images

        barrier = threading.Barrier(3)
        lock = threading.Lock()
        active = 0
        peak = 0
        completion_order: list[int] = []
        payload = self._image_payload()

        class ControlledClient:
            def generate(self, prompt: str, max_attempts: int = 3):
                nonlocal active, peak
                scene_number = next(index for index in range(1, 4) if f"第{index}幕" in prompt)
                with lock:
                    active += 1
                    peak = max(peak, active)
                barrier.wait(timeout=3)
                time.sleep({3: 0.0, 1: 0.05, 2: 0.1}[scene_number])
                with lock:
                    active -= 1
                    completion_order.append(scene_number)
                return payload

        stdout = io.StringIO()
        with self._configured_workspace(
            generate_images, image_generation_concurrency=4
        ), mock.patch.object(
            generate_images, "verify_config_git_safety", return_value=[]
        ), mock.patch.object(
            generate_images, "load_provider_config", return_value=self._provider()
        ), mock.patch.object(
            generate_images, "ImagesGenerationsClient", return_value=ControlledClient()
        ), contextlib.redirect_stdout(stdout):
            exit_code = generate_images.main(["--project", str(self.root)])
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0, summary)
        self.assertEqual(summary["configuredConcurrency"], 4)
        self.assertEqual(summary["effectiveConcurrency"], 3)
        self.assertEqual(summary["taskCount"], 3)
        self.assertEqual(peak, 3)
        self.assertEqual(completion_order, [3, 1, 2])
        manifest = json.loads(
            (self.root / "manifests" / "generation-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            [item["sceneId"] for item in manifest["scenes"]],
            [item["sceneId"] for item in scenes],
        )
        validation_stdout = io.StringIO()
        with self._configured_workspace(
            validate_generated_images, image_validation_concurrency=4
        ), contextlib.redirect_stdout(validation_stdout):
            validation_exit = validate_generated_images.main(["--project", str(self.root)])
        validation_summary = json.loads(validation_stdout.getvalue())
        self.assertEqual(validation_exit, 0, validation_summary)
        self.assertEqual(validation_summary["configuredConcurrency"], 4)
        self.assertEqual(validation_summary["effectiveConcurrency"], 3)
        self.assertEqual(validation_summary["taskCount"], 3)

    def test_existing_validated_scene_conflict_does_not_downgrade_manifest(self) -> None:
        self._write_valid_scene()
        import generate_images

        client = mock.Mock()
        stdout = io.StringIO()
        with self._configured_workspace(generate_images), mock.patch.object(
            generate_images, "verify_config_git_safety", return_value=[]
        ), mock.patch.object(
            generate_images, "load_provider_config", return_value=self._provider()
        ), mock.patch.object(
            generate_images, "ImagesGenerationsClient", return_value=client
        ), contextlib.redirect_stdout(stdout):
            exit_code = generate_images.main(["--project", str(self.root)])
        summary = json.loads(stdout.getvalue())
        manifest = json.loads(
            (self.root / "manifests" / "generation-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(client.generate.call_count, 0)
        self.assertEqual(manifest["scenes"][0]["status"], "validated")
        self.assertEqual(manifest["runs"][-1]["status"], "failed")

    def test_requesting_without_candidate_becomes_unknown_and_is_not_retried(self) -> None:
        image_path, _ = self._write_valid_scene()
        image_path.unlink()
        manifest_path = self.root / "manifests" / "generation-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["scenes"][0]["status"] = "requesting"
        manifest["scenes"][0]["imageSha256"] = None
        manifest["scenes"][0]["currentAttemptId"] = "scene-01-attempt-0001"
        manifest["scenes"][0]["attemptRecords"] = [
            {
                "attemptId": "scene-01-attempt-0001",
                "status": "requesting",
                "inputIdentitySha256": "a" * 64,
                "candidateFile": ".work/old/external-tasks/scene-01/a0001/candidate.png",
                "receiptFile": ".work/old/external-tasks/scene-01/a0001/candidate-receipt.json",
                "formalFile": "scenes/scene-01-概念.png",
                "externalOutcome": "not_started",
                "overwrite": False,
            }
        ]
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        import generate_images

        client = mock.Mock()
        client.generate.return_value = self._image_payload()
        stdout = io.StringIO()
        with self._configured_workspace(generate_images), mock.patch.object(
            generate_images, "verify_config_git_safety", return_value=[]
        ), mock.patch.object(
            generate_images, "load_provider_config", return_value=self._provider()
        ), mock.patch.object(
            generate_images, "ImagesGenerationsClient", return_value=client
        ), contextlib.redirect_stdout(stdout):
            exit_code = generate_images.main(
                ["--project", str(self.root), "--retry-failed"]
            )
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(summary["succeeded"], 0)
        self.assertEqual(summary["unknownExternalOutcomeCount"], 1)
        self.assertEqual(client.generate.call_count, 0)
        persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["scenes"][0]["status"], "unknown_external_outcome")

    def test_requesting_with_complete_candidate_is_adopted_without_provider_call(self) -> None:
        _, scene = self._write_valid_scene()
        formal = self.root / "scenes" / scene["outputFile"]
        formal.unlink()
        attempt_rel = Path(".work/old/external-tasks/scene-01/a0001")
        attempt_root = self.root / attempt_rel
        candidate = image_generation.normalize_image_candidate(
            self._image_payload().data,
            attempt_root / "candidate.png",
            attempt_root,
            "scene-01",
            attempt_id="scene-01-attempt-0001",
            formal_file=f"scenes/{scene['outputFile']}",
            input_identity_sha256="a" * 64,
            source="b64_json",
            provider_attempts=1,
        )
        manifest_path = self.root / "manifests" / "generation-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        record = manifest["scenes"][0]
        record.update(
            {
                "status": "requesting",
                "imageSha256": None,
                "currentAttemptId": "scene-01-attempt-0001",
                "attemptRecords": [
                    {
                        "attemptId": "scene-01-attempt-0001",
                        "status": "requesting",
                        "inputIdentitySha256": "a" * 64,
                        "candidateFile": candidate.path.relative_to(self.root).as_posix(),
                        "receiptFile": candidate.receipt_path.relative_to(self.root).as_posix(),
                        "formalFile": f"scenes/{scene['outputFile']}",
                        "externalOutcome": "not_started",
                        "overwrite": False,
                    }
                ],
            }
        )
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        import generate_images

        client = mock.Mock()
        stdout = io.StringIO()
        with self._configured_workspace(generate_images), mock.patch.object(
            generate_images, "verify_config_git_safety", return_value=[]
        ), mock.patch.object(
            generate_images, "load_provider_config", return_value=self._provider()
        ), mock.patch.object(
            generate_images, "ImagesGenerationsClient", return_value=client
        ), contextlib.redirect_stdout(stdout):
            exit_code = generate_images.main(["--project", str(self.root), "--retry-failed"])
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0, summary)
        self.assertEqual(client.generate.call_count, 0)
        self.assertEqual(summary["adoptedCandidateCount"], 1)
        self.assertTrue(formal.is_file())

    def test_validation_uses_one_image_open_per_png(self) -> None:
        self._write_valid_scene()
        real_open = validate_generated_images.Image.open
        calls = 0

        def counted_open(*args: object, **kwargs: object):
            nonlocal calls
            calls += 1
            return real_open(*args, **kwargs)

        stdout = io.StringIO()
        with self._configured_workspace(validate_generated_images), mock.patch.object(
            validate_generated_images.Image, "open", side_effect=counted_open
        ), contextlib.redirect_stdout(stdout):
            exit_code = validate_generated_images.main(["--project", str(self.root)])
        self.assertEqual(exit_code, 0, stdout.getvalue())
        self.assertEqual(calls, 1)

    def test_image_validation_rejects_format_size_mode_and_truncated_bytes(self) -> None:
        cases: list[tuple[str, Path]] = []
        jpeg = self.root / "scenes" / "wrong-format.jpg"
        Image.new("RGB", (1920, 1080), "white").save(jpeg, "JPEG")
        cases.append(("format", jpeg))
        wrong_size = self.root / "scenes" / "wrong-size.png"
        Image.new("RGB", (100, 100), "white").save(wrong_size, "PNG")
        cases.append(("size", wrong_size))
        wrong_mode = self.root / "scenes" / "wrong-mode.png"
        Image.new("L", (1920, 1080), 255).save(wrong_mode, "PNG")
        cases.append(("mode", wrong_mode))
        damaged = self.root / "scenes" / "damaged.png"
        payload = self._image_payload().data
        damaged.write_bytes(payload[: len(payload) // 2])
        cases.append(("decode", damaged))
        for expected_category, path in cases:
            with self.subTest(expected_category=expected_category):
                outcome = validate_generated_images._validate_image(
                    validate_generated_images.ImageValidationTask(
                        scene_id="scene-01",
                        file=f"scenes/{path.name}",
                        path=path,
                        expected_hash=_sha256(path),
                    )
                )
                self.assertFalse(outcome.ok)
                self.assertEqual(outcome.error.category, expected_category)

    def test_visual_review_entry_uses_agent_contract_and_never_writes_approval(self) -> None:
        self._write_valid_scene()
        stdout = io.StringIO()
        with self._configured_workspace(validate_generated_images), contextlib.redirect_stdout(stdout):
            exit_code = validate_generated_images.main(
                ["--project", str(self.root), "--prepare-visual-review"]
            )
        summary = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0, summary)
        review = summary["visualReview"]
        self.assertEqual(review["preparationMode"], "artifact_only", review)
        self.assertEqual(review["status"], "ready_for_coordinator_dispatch")
        self.assertTrue(review["preparedOnly"])
        self.assertFalse(review["approvalWritten"])
        self.assertEqual(
            review["preparedTask"]["contractVersion"],
            "whiteboard-prepared-agent-task-v1",
        )
        task_path = self.root / Path(review["taskFile"])
        task = json.loads(task_path.read_text(encoding="utf-8"))
        self.assertEqual(task["taskKind"], "visualReview")
        self.assertFalse(task["formalWritesAllowed"])
        self.assertFalse(task["approvalWritesAllowed"])
        serialized = json.dumps(task, ensure_ascii=False)
        self.assertNotIn("test-secret", serialized)
        self.assertNotRegex(serialized, r"(?i)[a-z]:[\\/]")

    def test_visual_review_coordinator_findings_are_result_validated_and_not_approval(self) -> None:
        _, scene = self._write_valid_scene()
        project = project_workspace.load_project(self.root)
        workspace = SimpleNamespace(
            config=SimpleNamespace(
                root=self.root.parent,
                for_role=lambda _role: 1,
            )
        )
        task, preparation = validate_generated_images.create_visual_review_task(
            workspace=workspace,
            project=project,
            manifest_path=self.root / "manifests" / "generation-manifest.json",
        )
        self.assertEqual(preparation["preparationMode"], "artifact_only")
        result = validate_generated_images.record_visual_review_fallback(
            task,
            scene_order=["scene-01"],
            findings=[
                {
                    "sceneId": "scene-01",
                    "priority": 1,
                    "code": "cross_scene_review",
                    "message": "需要用户继续逐图确认",
                    "file": f"scenes/{scene['outputFile']}",
                }
            ],
        )
        self.assertEqual(result.data["status"], "completed")
        findings_document = json.loads(
            (task.context.task_dir / "findings.json").read_text(encoding="utf-8")
        )
        self.assertFalse(findings_document["approvalWritten"])

    def test_visual_review_prepare_does_not_encode_host_dispatch(self) -> None:
        self._write_valid_scene()
        project = project_workspace.load_project(self.root)
        workspace = SimpleNamespace(
            config=SimpleNamespace(
                root=self.root.parent,
                for_role=lambda _role: 3,
            )
        )
        task, audit = validate_generated_images.create_visual_review_task(
            workspace=workspace,
            project=project,
            manifest_path=self.root / "manifests" / "generation-manifest.json",
        )
        self.assertEqual(task.data["taskKind"], "visualReview")
        self.assertEqual(audit["preparationMode"], "artifact_only")
        self.assertEqual(audit["status"], "ready_for_coordinator_dispatch")
        self.assertNotIn("dispatchAllowed", audit)
        self.assertNotIn("effectiveAgentConcurrency", audit)
        self.assertNotIn("mode", audit)

    def test_crash_boundaries_recover_without_unsafe_provider_retry(self) -> None:
        import generate_images

        class InjectedCrash(BaseException):
            pass

        cases = {
            "after_prepared": (1, 0),
            "after_requesting": (0, 1),
            "after_provider_returned_before_candidate": (0, 1),
            "after_candidate_persisted": (0, 0),
            "after_candidate_ready": (0, 0),
            "after_publishing_checkpoint": (0, 0),
            "after_formal_published": (0, 0),
            "after_validated_before_cleanup": (0, 0),
        }
        for crash_stage, (expected_provider_calls, expected_unknown) in cases.items():
            with self.subTest(crash_stage=crash_stage):
                case = ImageGenerationCliTests()
                case.setUp()
                try:
                    scene = {
                        "sceneId": "scene-01",
                        "name": "恢复",
                        "subtitleRange": {"startMs": 0, "endMs": 1000},
                        "sceneDurationMs": 1000,
                        "prompt": "恢复边界中的明确画面主体",
                        "outputFile": "scene-01.png",
                    }
                    case._write_plan([scene])
                    first_client = mock.Mock()
                    first_client.generate.return_value = case._image_payload()

                    def crash_hook(stage: str, _scene_id: str) -> None:
                        if stage == crash_stage:
                            raise InjectedCrash(stage)

                    with case._configured_workspace(generate_images), mock.patch.object(
                        generate_images, "verify_config_git_safety", return_value=[]
                    ), mock.patch.object(
                        generate_images, "load_provider_config", return_value=case._provider()
                    ), mock.patch.object(
                        generate_images, "ImagesGenerationsClient", return_value=first_client
                    ), mock.patch.object(
                        generate_images, "_checkpoint_hook", side_effect=crash_hook
                    ), self.assertRaises(InjectedCrash):
                        generate_images.main(["--project", str(case.root)])

                    recovery_client = mock.Mock()
                    recovery_client.generate.return_value = case._image_payload()
                    stdout = io.StringIO()
                    with case._configured_workspace(generate_images), mock.patch.object(
                        generate_images, "verify_config_git_safety", return_value=[]
                    ), mock.patch.object(
                        generate_images, "load_provider_config", return_value=case._provider()
                    ), mock.patch.object(
                        generate_images, "ImagesGenerationsClient", return_value=recovery_client
                    ), contextlib.redirect_stdout(stdout):
                        exit_code = generate_images.main(["--project", str(case.root)])
                    summary = json.loads(stdout.getvalue())
                    self.assertEqual(recovery_client.generate.call_count, expected_provider_calls)
                    self.assertEqual(summary["unknownExternalOutcomeCount"], expected_unknown)
                    self.assertEqual(exit_code, 1 if expected_unknown else 0, summary)
                finally:
                    case.tearDown()

    def test_merge_concat_list_uses_only_current_project_work_dir(self) -> None:
        source = self.root / "source" / "source.srt"
        source.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\n第一幕\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n第二幕\n",
            encoding="utf-8",
        )
        self.metadata["source"]["sha256"] = _sha256(source)
        (self.root / "project.json").write_text(
            json.dumps(self.metadata, ensure_ascii=False), encoding="utf-8"
        )
        self._write_plan(
            [
                {
                    "sceneId": "scene-01",
                    "sourceCueRange": [1, 1],
                    "sceneDurationMs": 1000,
                    "prompt": "第一幕以一个清晰的起点主体展开画面",
                    "outputFile": "scene-01.png",
                },
                {
                    "sceneId": "scene-02",
                    "sourceCueRange": [2, 2],
                    "sceneDurationMs": 1000,
                    "prompt": "第二幕以一个承接前幕的结果主体完成画面",
                    "outputFile": "scene-02.png",
                },
            ]
        )
        first = self.root / "scenes" / "one.mp4"
        second = self.root / "scenes" / "two.mp4"
        first.write_bytes(b"one")
        second.write_bytes(b"two")
        output = self.root / "output" / "final-video-only.mp4"
        seen: list[Path] = []

        def fake_concat(inputs: list[Path], output_path: Path, list_path: Path) -> bool:
            seen.append(list_path)
            self.assertEqual(list_path.parent.parent, self.root / ".work")
            list_path.write_text("test", encoding="utf-8")
            output_path.write_bytes(b"merged")
            return True

        def fake_validate(path: Path, **kwargs: object) -> dict[str, object]:
            media_path = Path(path)
            expected_frame_count = kwargs.get("expected_frame_count", 120)
            fps = {"numerator": 60, "denominator": 1, "value": 60.0}
            streams = {
                "video": [
                    {
                        "index": 0,
                        "codec": "h264",
                        "width": 1920,
                        "height": 1080,
                        "pixelFormat": "yuv420p",
                        "fps": fps,
                        "frameCount": expected_frame_count,
                        "durationMs": 2000,
                    }
                ],
                "audio": [],
                "subtitle": [],
                "other": [],
            }
            media = {
                "bytes": media_path.stat().st_size,
                "sha256": _sha256(media_path),
                "durationMs": 2000,
                "formatName": "mov,mp4",
                "streams": streams,
                "validation": {
                    "contractVersion": "media-validation-v2",
                    "validated": True,
                    "validationMode": "deep",
                    "expectedFrameCount": expected_frame_count,
                    "expectedAudioStreams": 0,
                    "renderProfileSha256": "0" * 64,
                    "decodedFrameCount": expected_frame_count,
                    "frameCountEvidence": "decoded_frames_v1",
                    "containerNbFrames": None,
                    "fullDecode": True,
                },
            }
            media["validation"]["deepReceipt"] = {
                "contractVersion": "media-deep-receipt-v1",
                "validatorContractVersion": "media-validation-v2",
                "mediaSha256": media["sha256"],
                "bytes": media["bytes"],
                "durationMs": media["durationMs"],
                "formatName": media["formatName"],
                "streams": streams,
                "videoCodec": "h264",
                "width": 1920,
                "height": 1080,
                "pixelFormat": "yuv420p",
                "fps": fps,
                "videoDurationMs": 2000,
                "containerNbFrames": None,
                "decodedFrameCount": expected_frame_count,
                "frameCountEvidence": "decoded_frames_v1",
                "fullDecode": {"passed": True, "progressEnd": True},
            }
            return media

        def fake_bind(path: Path, **kwargs: object) -> dict[str, object]:
            receipt = kwargs["deep_receipt"]
            self.assertIn("deepReceipt", receipt)
            media = fake_validate(path, **kwargs)
            media["validation"]["validationMode"] = "binding"
            return media

        with self._configured_workspace(merge_scenes), mock.patch.object(
            merge_scenes, "_ffmpeg_concat_copy", side_effect=fake_concat
        ), mock.patch.object(
            merge_scenes, "validate_video", side_effect=fake_validate
        ), mock.patch.object(
            merge_scenes, "bind_validated_video", side_effect=fake_bind
        ), mock.patch.object(
            merge_scenes,
            "assert_current_scene_review_approval",
            return_value={"identityHash": "fixture-scene-review"},
        ):
            exit_code = merge_scenes.main(
                [
                    "--project",
                    str(self.root),
                    "--inputs",
                    str(first),
                    str(second),
                    "--output",
                    str(output),
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].parent.exists())


if __name__ == "__main__":
    unittest.main()
